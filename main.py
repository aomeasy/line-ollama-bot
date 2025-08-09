# main.py — LINE Webhook + Hugging Face Inference API
import os
import json
import requests
from fastapi import FastAPI, Request, HTTPException
from dotenv import load_dotenv

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

load_dotenv()

# ====== ENV VARS (ต้องตั้งใน Render → Environment) ======
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
HF_TOKEN = os.getenv("HF_TOKEN")  # จาก Hugging Face (Role: Read)
HF_MODEL = os.getenv("HF_MODEL", "Qwen/Qwen2.5-3B-Instruct")

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    raise RuntimeError("ต้องตั้ง LINE_CHANNEL_ACCESS_TOKEN และ LINE_CHANNEL_SECRET")
if not HF_TOKEN:
    # ไม่ถึงกับ raise เพื่อให้ /healthz ยังใช้ได้ แต่จะบอกตอนเรียกใช้งาน
    print("⚠️ ยังไม่ได้ตั้ง HF_TOKEN — บอทจะตอบ: 'ยังไม่ได้ตั้งค่า HF_TOKEN บนเซิร์ฟเวอร์'")

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

app = FastAPI()


def ask_llm(prompt: str) -> str:
    """ เรียก Hugging Face Inference API (ฟรี) """
    if not HF_TOKEN:
        return "ยังไม่ได้ตั้งค่า HF_TOKEN บนเซิร์ฟเวอร์"

    url = f"https://api-inference.huggingface.co/models/{HF_MODEL}"
    headers = {
        "Authorization": f"Bearer {HF_TOKEN}",
        "Content-Type": "application/json",
    }

    system = "คุณคือผู้ช่วยภาษาไทย ตอบสั้น กระชับ ชัดเจน และสุภาพ"
    chat_prompt = f"{system}\n\nผู้ใช้: {prompt}\nผู้ช่วย:"

    payload = {
        "inputs": chat_prompt,
        "parameters": {
            "max_new_tokens": 256,
            "temperature": 0.7,
            "top_p": 0.9,
            "return_full_text": False,
        },
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=60)
        r.raise_for_status()
        data = r.json()

        # รูปแบบผลลัพธ์ของ Inference API (text-generation)
        if isinstance(data, list) and data and "generated_text" in data[0]:
            return (data[0]["generated_text"] or "").strip()[:4800] or "..."
        if isinstance(data, dict) and "generated_text" in data:
            return (data["generated_text"] or "").strip()[:4800] or "..."

        # กรณี HF แจ้งสถานะโหลดโมเดล/ข้อผิดพลาด
        if isinstance(data, dict) and "error" in data:
            msg = data.get("error", "")
            if "loading" in msg.lower() or "currently loading" in msg.lower():
                return "โมเดลกำลังโหลดที่ฝั่ง Hugging Face (ลองใหม่อีก 10–20 วินาที)"
            return f"HF error: {msg}"

        return "ไม่สามารถแปลผลลัพธ์จากโมเดลได้"
    except requests.HTTPError as e:
        return f"เรียก HF ล้มเหลว: {e}"
    except Exception as e:
        return f"ข้อผิดพลาดไม่คาดคิด: {e}"


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/callback")
async def callback(request: Request):
    # ตรวจลายเซ็นจาก LINE
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
    # กันข้อความยาวเกิน limit ของ LINE (~5000 ตัวอักษร)
    reply = reply[:4900] if reply else "ขออภัย ระบบยังไม่พร้อมตอบตอนนี้"
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
