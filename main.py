# main.py
import base64
import hashlib
import hmac
import json
import os
import re
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

# ─── Environment ──────────────────────────────────────────────────────────────
OLLAMA_API_URL = os.getenv("OLLAMA_API_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:8b")

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

PROMPT_SYSTEM = os.getenv(
    "PROMPT_SYSTEM",
    (
        "คุณคือผู้ช่วย AI สำหรับ LINE OA พูดคุยเป็นภาษาไทยเท่านั้น "
        "ตอบอย่างกวนๆ ฮาๆ เหมือนเป็นเพื่อนสนิท "
        "ทุกคำตอบต้องเป็นภาษาไทย 100% และลงท้ายด้วยคำว่า \"จร้าาาาา\" เสมอ "
        "ห้ามใช้ภาษาอังกฤษ ยกเว้นชื่อเฉพาะที่จำเป็นเท่านั้น และห้ามละเมิดกฎนี้เด็ดขาด"
    ),
)

MAX_TOKENS = int(os.getenv("MAX_TOKENS", "400"))  # จำกัดความยาวคำตอบจากโมเดล
MAX_CHARS = int(os.getenv("MAX_CHARS", "1200"))   # กันข้อความยาวเกินสำหรับ LINE

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    print("⚠️  Missing LINE env variables: LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET")


# ─── FastAPI App ──────────────────────────────────────────────────────────────
app = FastAPI(title="LINE × Ollama Bot", version="1.0.0")


@app.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        "ollama_url": OLLAMA_API_URL,
        "model": OLLAMA_MODEL,
        "has_line_token": bool(LINE_CHANNEL_ACCESS_TOKEN),
        "has_line_secret": bool(LINE_CHANNEL_SECRET),
    }


# ─── Helpers: LINE signature ──────────────────────────────────────────────────
def verify_line_signature(body: bytes, signature: str, secret: str) -> bool:
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected_signature = base64.b64encode(mac).decode("utf-8")
    return hmac.compare_digest(expected_signature, signature or "")


# ─── Helpers: Post-process reply ──────────────────────────────────────────────
def _postprocess_thai(reply: str) -> str:
    reply = (reply or "").strip()

    # ตัดความยาวเพื่อไม่ให้เกินข้อจำกัดของ LINE (และกันโมเดลพูดยืด)
    if len(reply) > MAX_CHARS:
        reply = reply[: MAX_CHARS - 1] + "…"

    # ถ้าตัวอักษรอังกฤษเกิน ~30% ให้เตือนตัวเอง (soft check) เพื่อย้ำภาษาไทย
    letters = re.findall(r"[A-Za-z]", reply)
    if letters and (len(letters) / max(1, len(reply)) > 0.3):
        reply += "\n(ขออภัย จะตอบเป็นภาษาไทยเท่านั้นตามนโยบาย จร้าาาาา)"

    # บังคับลงท้ายด้วย "จร้าาาาา"
    if not reply.endswith("จร้าาาาา"):
        reply = reply.rstrip("!?. \n") + " จร้าาาาา"

    return reply


def _fallback_extract_content(data: Dict[str, Any]) -> Optional[str]:
    # รองรับรูปแบบรีสปอนส์ที่ต่างเวอร์ชัน
    if "message" in data and isinstance(data["message"], dict):
        return data["message"].get("content")
    if "messages" in data and isinstance(data["messages"], list) and data["messages"]:
        last = data["messages"][-1]
        if isinstance(last, dict):
            return last.get("content")
    if "response" in data:
        return str(data.get("response"))
    return None


# ─── Core: Call Ollama ────────────────────────────────────────────────────────
async def ask_ollama(prompt: str, user_id: Optional[str] = None) -> str:
    """
    เรียก Ollama /api/chat แบบ non-stream
    """
    url = f"{OLLAMA_API_URL}/api/chat"

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": PROMPT_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "options": {
            # จำกัดการเดาคำถัดไป (ใกล้เคียง max_tokens)
            "num_predict": MAX_TOKENS,
            # ลดความฟุ้ง/ความสุ่ม เพื่อให้ทำตามสไตล์มากขึ้น
            "temperature": 0.3,
            "top_p": 0.9,
        },
    }

    timeout = httpx.Timeout(30.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            print(f"❌ Ollama HTTP error: {e}")
            return "ขออภัย ระบบ AI ตอบไม่ได้ชั่วคราว ลองอีกครั้งได้ไหมคะ จร้าาาาา"

    try:
        data = resp.json()
    except Exception as e:
        print(f"❌ Ollama JSON parse error: {e} / body={resp.text[:300]}")
        return "ขออภัย ระบบ AI ตอบมีปัญหาในการประมวลผล จร้าาาาา"

    content = _fallback_extract_content(data) or "ขออภัย ไม่พบคำตอบที่เหมาะสมค่ะ"
    return _postprocess_thai(content)


# ─── LINE Reply ───────────────────────────────────────────────────────────────
async def reply_to_line(reply_token: str, text: str) -> None:
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    body = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text[:4900]}],  # กันยาวเกิน
    }

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(url, headers=headers, json=body)
        if resp.status_code != 200:
            print(f"❌ LINE reply error {resp.status_code}: {resp.text}")


# ─── Webhook ──────────────────────────────────────────────────────────────────
@app.post("/callback")
async def line_callback(
    request: Request,
    x_line_signature: str = Header(None),
):
    if not LINE_CHANNEL_SECRET or not LINE_CHANNEL_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="LINE config missing")

    body_bytes = await request.body()

    # ตรวจสอบลายเซ็นจาก LINE
    if not x_line_signature or not verify_line_signature(body_bytes, x_line_signature, LINE_CHANNEL_SECRET):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(body_bytes.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    events: List[Dict[str, Any]] = payload.get("events", [])

    for event in events:
        etype = event.get("type")
        reply_token = event.get("replyToken")
        source = event.get("source", {})
        user_id = source.get("userId")

        # ข้อความปกติ
        if etype == "message" and event.get("message", {}).get("type") == "text":
            user_text = (event["message"]["text"] or "").strip()

            # คำสั่งง่าย ๆ สำหรับตรวจสถานะ
            if user_text.lower() in {"ping", "health", "status", "เช็คบอท"}:
                await reply_to_line(reply_token, "บอทยังทำงานปกติดีค่ะ ✅ จร้าาาาา")
                continue

            # ส่งไปให้ Ollama
            ai_reply = await ask_ollama(user_text, user_id=user_id)
            await reply_to_line(reply_token, ai_reply)

        # กรณี follow / join
        elif etype in {"follow", "join"}:
            await reply_to_line(reply_token, "สวัสดีค่า พิมพ์คำถามมาได้เลย จร้าาาาา")

        # เงียบสำหรับ event อื่น ๆ
        else:
            pass

    return {"ok": True}


# ─── Local run (Render ใช้ start command เอง) ────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
