# main.py
import base64
import hashlib
import hmac
import json
import os
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

# ─── Env ──────────────────────────────────────────────────────────────────────
OLLAMA_API_URL = os.getenv("OLLAMA_API_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:8b")
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    # เตือนตอนสตาร์ท (บน Render จะเห็นใน Logs)
    print("⚠️  Missing LINE env variables: LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET")

# ─── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(title="LINE × Ollama Bot", version="0.1.0")


@app.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        "ollama": OLLAMA_API_URL,
        "model": OLLAMA_MODEL,
    }


# ─── Utils: LINE Signature ────────────────────────────────────────────────────
def verify_line_signature(body: bytes, signature: str, secret: str) -> bool:
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected_signature = base64.b64encode(mac).decode("utf-8")
    # LINE ส่งเป็น Base64; เทียบแบบคงที่
    return hmac.compare_digest(expected_signature, signature)


# ─── Utils: Call Ollama ───────────────────────────────────────────────────────
async def ask_ollama(prompt: str, user_id: Optional[str] = None) -> str:
    """
    เรียก Ollama /api/chat (Ollama >= 0.1x รองรับ) แบบ non-stream
    """
    url = f"{OLLAMA_API_URL}/api/chat"
    system_prompt = (
        "You are a helpful Thai assistant for LINE OA. "
        "Answer clearly in Thai by default, be concise, and use bullet points when helpful."
    )

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            # สามารถแทรก user-specific context ผ่าน user_id ได้ถ้าต้องการในอนาคต
            {"role": "user", "content": prompt},
        ],
        "stream": False,
        # เผื่ออนาคต: "options": {"temperature": 0.3}
    }

    timeout = httpx.Timeout(30.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            print(f"❌ Ollama error: {e}")
            return "ขออภัย ระบบ AI ตอบไม่ได้ชั่วคราว ลองอีกครั้งได้ไหมคะ"

    data = resp.json()
    # รูปแบบตอบกลับของ /api/chat: {"message":{"role":"assistant","content":"..."},"done":true,...}
    content = (
        (data.get("message") or {}).get("content")
        or _fallback_extract_content(data)
        or "ขออภัย ไม่พบคำตอบที่เหมาะสมค่ะ"
    )
    return content.strip()


def _fallback_extract_content(data: Dict[str, Any]) -> Optional[str]:
    # เผื่อบางเวอร์ชัน/รีสปอนส์ มีฟิลด์อื่น
    if "messages" in data and isinstance(data["messages"], list) and data["messages"]:
        last = data["messages"][-1]
        return last.get("content")
    if "response" in data:
        return data.get("response")
    return None


# ─── Utils: Reply to LINE ─────────────────────────────────────────────────────
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
            # Log ไว้ดูบน Render
            print(f"❌ LINE reply error {resp.status_code}: {resp.text}")
            # ไม่ raise เพื่อให้ webhook ตอบ 200 ให้ LINE ไม่ต้อง retry ยาว


# ─── Webhook Endpoint ─────────────────────────────────────────────────────────
@app.post("/callback")
async def line_callback(
    request: Request,
    x_line_signature: str = Header(None),
):
    if not LINE_CHANNEL_SECRET or not LINE_CHANNEL_ACCESS_TOKEN:
        raise HTTPException(status_code=500, detail="LINE config missing")

    body_bytes = await request.body()

    # ตรวจสอบลายเซ็น
    if not x_line_signature or not verify_line_signature(body_bytes, x_line_signature, LINE_CHANNEL_SECRET):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(body_bytes.decode("utf-8"))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    events: List[Dict[str, Any]] = payload.get("events", [])
    # รองรับหลาย event ในครั้งเดียว
    for event in events:
        etype = event.get("type")
        reply_token = event.get("replyToken")
        source = event.get("source", {})
        user_id = source.get("userId")

        # รับเฉพาะข้อความ
        if etype == "message" and event.get("message", {}).get("type") == "text":
            user_text = event["message"]["text"].strip()

            # เคสง่าย ๆ: คำสั่งเช็คสถานะ
            if user_text in ("ping", "health", "status", "เช็คบอท"):
                await reply_to_line(reply_token, "บอทยังทำงานปกติดีค่ะ ✅")
                continue

            # เรียก Ollama
            ai_reply = await ask_ollama(user_text, user_id=user_id)
            await reply_to_line(reply_token, ai_reply)

        # กรณีอื่น ๆ (join / follow / postback ฯลฯ) — ตอบสั้น ๆ
        elif etype in ("follow", "join"):
            await reply_to_line(reply_token, "สวัสดีค่ะ พิมพ์คำถามมาได้เลยนะคะ 🤖")
        else:
            # เงียบไว้เพื่อไม่ให้วงจรตอบกลับไม่จำเป็น
            pass

    # LINE ต้องการ 200 ภายใน ~1 วินาที
    return {"ok": True}
    

# ─── Local run (Render ใช้ start command เอง) ────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
