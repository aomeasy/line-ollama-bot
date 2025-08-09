import os
import requests
from fastapi import FastAPI, Request, HTTPException
from dotenv import load_dotenv

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

load_dotenv()

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
OLLAMA_URL = os.getenv("OLLAMA_URL")  # ใช้ URL ที่คุณได้จาก Cloudflare Tunnel

if not (LINE_CHANNEL_ACCESS_TOKEN and LINE_CHANNEL_SECRET and OLLAMA_URL):
    raise RuntimeError("Missing env: LINE_CHANNEL_ACCESS_TOKEN/LINE_CHANNEL_SECRET/OLLAMA_URL")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

app = FastAPI()

def ask_llm(prompt: str) -> str:
    url = f"{OLLAMA_URL.rstrip('/')}/api/chat"
    payload = {
        "model": os.getenv("OLLAMA_MODEL", "qwen2.5:3b"),
        "stream": False,
        "messages": [
            {"role": "system", "content": "ตอบภาษาไทย กระชับ ชัดเจน สุภาพ"},
            {"role": "user", "content": prompt}
        ],
    }
    try:
        r = requests.post(url, json=payload, timeout=60)
        r.raise_for_status()
        data = r.json()
        content = (data.get("message") or {}).get("content") or data.get("response") or ""
        return content[:4800] if content else "ขออภัย ระบบไม่พร้อมตอบตอนนี้"
    except Exception as e:
        return f"เกิดข้อผิดพลาดเรียก LLM: {e}"

@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.post("/callback")
async def callback(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    body_text = body.decode("utf-8")
    try:
        handler.handle(body_text, signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_text(event):
    user_msg = (event.message.text or "").strip()
    reply = ask_llm(user_msg)
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
