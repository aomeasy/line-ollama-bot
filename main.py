import os
import json
import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = FastAPI()

# --- Load env variables ---
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
HF_TOKEN = os.getenv("HF_TOKEN")
HF_MODEL = os.getenv("HF_MODEL", "Qwen/Qwen2.5-1.5B-Instruct")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- Health check ---
@app.get("/healthz")
def healthz():
    return {"ok": True}

# --- Hugging Face check ---
@app.get("/hfcheck")
def hfcheck():
    if not HF_TOKEN:
        return {"ok": False, "error": "HF_TOKEN not set"}

    url = f"https://api-inference.huggingface.co/models/{HF_MODEL}"
    headers = {"Authorization": f"Bearer {HF_TOKEN}"}
    try:
        r = requests.get(url, headers=headers, timeout=30)
        status = r.status_code
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text[:300]}
        return {
            "ok": (200 <= status < 300) or ("loading" in json.dumps(data).lower()),
            "status": status,
            "model": HF_MODEL,
            "hint": "ถ้าขึ้น loading ให้ลองใหม่อีก 10–20 วินาที",
            "data": data,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

# --- Ask Hugging Face ---
def ask_llm(prompt: str) -> str:
    if not HF_TOKEN:
        return "HF_TOKEN not set"
    url = f"https://api-inference.huggingface.co/models/{HF_MODEL}"
    headers = {"Authorization": f"Bearer {HF_TOKEN}"}
    payload = {"inputs": prompt, "options": {"wait_for_model": True}}
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and "generated_text" in data[0]:
            return data[0]["generated_text"]
        return json.dumps(data, ensure_ascii=False)
    except Exception as e:
        return f"HF error: {e}"

# --- LINE webhook ---
@app.post("/callback")
async def callback(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    try:
        handler.handle(body.decode("utf-8"), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return "OK"

# --- LINE message handler ---
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_text = event.message.text.strip()
    reply = ask_llm(user_text)
    try:
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=reply)
        )
    except Exception as e:
        print("LINE reply error:", e)

# --- Root route (optional) ---
@app.get("/")
def root():
    return {"msg": "LINE + Hugging Face bot is running"}
