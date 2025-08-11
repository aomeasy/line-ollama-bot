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

# ── ENV ───────────────────────────────────────────────────────────────────────
OLLAMA_API_URL = os.getenv("OLLAMA_API_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:8b")

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

PROMPT_SYSTEM = os.getenv(
    "PROMPT_SYSTEM",
    (
        "คุณคือผู้ช่วย AI สำหรับ LINE OA\n"
        "ทุกคำตอบต้องเป็นภาษาไทย 100% เท่านั้น\n"
        "ห้ามใช้ภาษาอังกฤษแม้แต่ตัวเดียว เว้นแต่เป็นชื่อเฉพาะ (แต่ระบบจะลบทิ้งให้อยู่ดี)\n"
        "ตอบอย่างกวนๆ ฮาๆ เหมือนเพื่อนสนิท\n"
        "ลงท้ายทุกคำตอบด้วย \"จร้าาาาา\"\n"
        "ห้ามละเมิดกฎนี้เด็ดขาด"
    ),
)
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "350"))  # ประมาณความยาวคำตอบจากโมเดล
MAX_CHARS = int(os.getenv("MAX_CHARS", "1000"))   # กันยาวเกินเวลาส่งกลับ LINE

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    print("⚠️ Missing LINE env variables: LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET")

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="LINE × Ollama (TH only)", version="1.1.0")

@app.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        "ollama_url": OLLAMA_API_URL,
        "model": OLLAMA_MODEL,
        "has_line_token": bool(LINE_CHANNEL_ACCESS_TOKEN),
        "has_line_secret": bool(LINE_CHANNEL_SECRET),
        "max_tokens": MAX_TOKENS,
        "max_chars": MAX_CHARS,
    }

# ── Helpers: LINE signature ───────────────────────────────────────────────────
def verify_line_signature(body: bytes, signature: str, secret: str) -> bool:
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected_signature = base64.b64encode(mac).decode("utf-8")
    return hmac.compare_digest(expected_signature, signature or "")

# ── Helpers: Thai-only postprocess ────────────────────────────────────────────
THAI_ALLOWED_EXTRA = r"0-9๐-๙\s\.\,\!\?\:\;\-\+\=\(\)\[\]{}\"'\/\n\r\t。、，！？：；…"  # เผื่อสัญลักษณ์ทั่วไป
ENG_PATTERN = re.compile(r"[A-Za-z]+")

def _strip_english(s: str) -> str:
    # ตัดตัวอักษรอังกฤษทั้งหมดออกแบบเด็ดขาด
    return ENG_PATTERN.sub("", s)

def _postprocess_thai(reply: str) -> str:
    reply = (reply or "").strip()

    # ลบภาษาอังกฤษทั้งหมด
    reply = _strip_english(reply)

    # ล้างช่องว่างซ้ำๆ ที่เกิดจากการลบ
    reply = re.sub(r"[ \t]{2,}", " ", reply)
    reply = re.sub(r"\n{3,}", "\n\n", reply)

    # จำกัดความยาวก่อนส่งกลับ
    if len(reply) > MAX_CHARS:
        reply = reply[: MAX_CHARS - 1] + "…"

    # บังคับลงท้ายด้วย "จร้าาาาา"
    end_tag = "จร้าาาาา"
    if not reply.endswith(end_tag):
        reply = reply.rstrip("!?. \n\r\t") + f" {end_tag}"

    return reply

def _fallback_extract_content(data: Dict[str, Any]) -> Optional[str]:
    if isinstance(data.get("message"), dict):
        return data["message"].get("content")
    if isinstance(data.get("messages"), list) and data["messages"]:
        last = data["messages"][-1]
        if isinstance(last, dict):
            return last.get("content")
    if "response" in data:
        return str(data.get("response"))
    return None

# ── Core: call Ollama ─────────────────────────────────────────────────────────
async def ask_ollama(prompt: str, user_id: Optional[str] = None) -> str:
    url = f"{OLLAMA_API_URL}/api/chat"
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": PROMPT_SYSTEM},
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        "options": {
            "num_predict": MAX_TOKENS,
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
            return _postprocess_thai("ขออภัย ระบบ AI ตอบไม่ได้ชั่วคราว ลองอีกครั้งได้ไหมคะ")

    try:
        data = resp.json()
    except Exception as e:
        print(f"❌ Ollama JSON parse error: {e} / body={resp.text[:300]}")
        return _postprocess_thai("ขออภัย ระบบ AI ตอบมีปัญหาในการประมวลผล")

    content = _fallback_extract_content(data) or "ขออภัย ไม่พบคำตอบที่เหมาะสมค่ะ"
    return _postprocess_thai(content)

# ── LINE reply ────────────────────────────────────────────────────────────────
async def reply_to_line(reply_token: str, text: str) -> None:
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    body = {
        "replyToken": reply_token,
        "messages": [{"type": "text", "text": text[:4900]}],
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(url, headers=headers, json=body)
        if resp.status_code != 200:
            print(f"❌ LINE reply error {resp.status_code}: {resp.text}")

# ── Webhook ───────────────────────────────────────────────────────────────────
@app.post("/callback")
async def line_callback(
    request: Request,
    x_line_signature: str = Header(None),
):
    if not LINE_CHANNEL_SECRET or not LINE_CHANNEL_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="LINE config missing")

    body_bytes = await request.body()
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

        if etype == "message" and event.get("message", {}).get("type") == "text":
            user_text = (event["message"]["text"] or "").strip()

            # คำสั่งสั้น ๆ
            if user_text.lower() in {"ping", "health", "status", "เช็คบอท"}:
                await reply_to_line(reply_token, _postprocess_thai("บอทยังทำงานปกติดีค่า ✅"))
                continue

            ai_reply = await ask_ollama(user_text, user_id=user_id)
            await reply_to_line(reply_token, ai_reply)

        elif etype in {"follow", "join"}:
            await reply_to_line(reply_token, _postprocess_thai("สวัสดีค่า พิมพ์คำถามมาได้เลย"))

        else:
            # เงียบสำหรับ event อื่น ๆ
            pass

    return {"ok": True}

# ── Local run ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
