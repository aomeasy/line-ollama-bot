import os
import json
import requests
from threading import Thread
from typing import List, Optional, Tuple

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# ----- load .env (สะดวกเวลา dev) -----
load_dotenv()

app = FastAPI()

# ====== ENV & GLOBALS ======
LINE_CHANNEL_ACCESS_TOKEN = (os.getenv("LINE_CHANNEL_ACCESS_TOKEN") or "").strip()
LINE_CHANNEL_SECRET = (os.getenv("LINE_CHANNEL_SECRET") or "").strip()

# ใส่ได้ 2 แบบ:
#   1) HF_MODEL="Qwen/Qwen2.5-1.5B-Instruct"
#   2) HF_MODELS="Qwen/Qwen2.5-1.5B-Instruct, Qwen/Qwen2.5-3B-Instruct, HuggingFaceH4/zephyr-7b-beta"
HF_TOKEN = (os.getenv("HF_TOKEN") or "").strip()
HF_MODEL = (os.getenv("HF_MODEL") or "Qwen/Qwen2.5-1.5B-Instruct").strip()
HF_MODELS = (os.getenv("HF_MODELS") or "").strip()

# default/fallback list (แก้เรียงลำดับตามชอบได้)
DEFAULT_FALLBACK_MODELS: List[str] = [
    HF_MODEL,
    "Qwen/Qwen2.5-3B-Instruct",
    "HuggingFaceH4/zephyr-7b-beta",
    "microsoft/DialoGPT-medium",
    "gpt2",  # โมเดลเล็ก ไว้เช็คระบบ (คุณภาพตอบไม่ดีนัก)
]

MAX_LINE_CHARS = 5000  # กันข้อความยาวเกินลิมิตของ LINE

line_bot_api: Optional[LineBotApi] = None
handler: Optional[WebhookHandler] = None

# จำโมเดลที่ใช้งานได้ล่าสุด เพื่อลดการลองหลายรอบทุกครั้ง
ACTIVE_MODEL: Optional[str] = None


# ====== UTILITIES ======
def parse_model_list() -> List[str]:
    """
    รวมรายการโมเดลจาก HF_MODELS (comma-separated) + DEFAULT_FALLBACK_MODELS
    และลบซ้ำ/trim
    """
    models = []
    if HF_MODELS:
        for m in HF_MODELS.split(","):
            s = m.strip()
            if s:
                models.append(s)
    # ต่อด้วยชุด default
    models.extend(DEFAULT_FALLBACK_MODELS)

    # unique ตามลำดับแรกพบ
    seen = set()
    uniq = []
    for m in models:
        if m not in seen:
            uniq.append(m)
            seen.add(m)
    return uniq


def hf_call(model: str, prompt: str) -> Tuple[bool, str, int]:
    """
    เรียก HF Inference API ที่โมเดลระบุ
    return: (ok, text_or_error, status_code)
    """
    if not HF_TOKEN:
        return False, "HF_TOKEN not set", 0

    url = f"https://api-inference.huggingface.co/models/{model}"
    headers = {
        "Authorization": f"Bearer {HF_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"inputs": prompt, "options": {"wait_for_model": True}}

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        status = resp.status_code

        # เคส 404/503: ถือว่า model ใช้ไม่ได้ตอนนี้ → ให้ fallback
        if status in (404, 503):
            # แนบข้อความสั้นๆ ไว้ debug
            try:
                data = resp.json()
            except Exception:
                data = {"raw": resp.text[:300]}
            return False, f"HF {status} on {model}: {json.dumps(data)[:300]}", status

        # อื่นๆ ถ้าไม่ 2xx → เป็น error
        if status < 200 or status >= 300:
            return False, f"HF {status} on {model}: {resp.text[:300]}", status

        # Parse ผลลัพธ์
        data = resp.json()
        # รูปแบบที่พบบ่อย
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and "generated_text" in item:
                    text = str(item["generated_text"])[:MAX_LINE_CHARS]
                    return True, text, status

        if isinstance(data, dict) and "generated_text" in data:
            text = str(data["generated_text"])[:MAX_LINE_CHARS]
            return True, text, status

        # fallback: แสดงดิบ (ตัดความยาว)
        txt = json.dumps(data, ensure_ascii=False)[:MAX_LINE_CHARS]
        return True, txt, status

    except Exception as e:
        return False, f"HF exception on {model}: {e}", 0


def ask_llm_with_fallback(prompt: str) -> str:
    """
    ลองเรียกตามลำดับ:
      1) ACTIVE_MODEL (ถ้ามี)
      2) รายการ model จาก parse_model_list()
    เก็บ ACTIVE_MODEL ใหม่เมื่อสำเร็จ
    """
    global ACTIVE_MODEL

    tried_msgs: List[str] = []

    # 1) ถ้ามีโมเดลที่เคยสำเร็จล่าสุด ให้ลองก่อน
    if ACTIVE_MODEL:
        ok, text, status = hf_call(ACTIVE_MODEL, prompt)
        if ok:
            return text
        tried_msgs.append(text)  # เก็บไว้ debug ถ้าไม่สำเร็จ

    # 2) ลองชุด fallback ทีละตัว
    for model in parse_model_list():
        # ถ้าเท่ากับ ACTIVE_MODEL ที่เพิ่งลองแล้วข้าม
        if ACTIVE_MODEL and model == ACTIVE_MODEL:
            continue
        ok, text, status = hf_call(model, prompt)
        if ok:
            ACTIVE_MODEL = model  # จำตัวที่เวิร์ค
            return text
        tried_msgs.append(text)

    # ถ้าไม่สำเร็จทั้งหมด
    joined = "\n".join(tried_msgs[-3:])  # แนบ error ล่าสุดสั้นๆ
    return f"(HF) ไม่สามารถเรียกใช้งานโมเดลใดๆ ได้ในตอนนี้\n{joined}"


def get_target_id(event) -> Optional[str]:
    """
    คืน user_id / group_id / room_id อันใดอันหนึ่งสำหรับ push_message
    (ต้องเปิดสิทธิ์ Push ใน LINE Console ด้วย)
    """
    src = event.source
    for attr in ("user_id", "group_id", "room_id"):
        val = getattr(src, attr, None)
        if val:
            return val
    return None


# ====== FASTAPI LIFECYCLE ======
@app.on_event("startup")
def on_startup():
    global line_bot_api, handler

    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
        print("⚠️  Missing LINE env: LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET")
    else:
        line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
        handler = WebhookHandler(LINE_CHANNEL_SECRET)

        # ผูก handler แบบ dynamic (หลีกเลี่ยง @handler.add ตอน handler ยังเป็น None)
        handler.add(MessageEvent, message=TextMessage)(handle_message)

    if not HF_TOKEN:
        print("⚠️  HF_TOKEN is not set; LLM calls will fail")

    # เตรียม ACTIVE_MODEL ถ้าอยาก warm-up (optional)
    # try:
    #     txt = ask_llm_with_fallback("ping")
    #     print("Warm-up LLM ok")
    # except Exception as e:
    #     print("Warm-up failed:", e)


# ====== ROUTES ======
@app.get("/healthz")
def healthz():
    return {"ok": True, "active_model": ACTIVE_MODEL}


@app.get("/hfcheck")
def hfcheck():
    """
    เช็คสถานะหลายโมเดลในคราวเดียว
    """
    results = []
    for model in parse_model_list():
        ok, text, status = hf_call(model, "ping")
        results.append(
            {"model": model, "ok": ok, "status": status, "sample": text[:200]}
        )
    return {"results": results, "active_model": ACTIVE_MODEL}


@app.get("/")
def root():
    return {"msg": "LINE + HF AI bot is running", "active_model": ACTIVE_MODEL}


@app.post("/callback")
async def callback(request: Request):
    if handler is None:
        raise HTTPException(status_code=500, detail="LINE handler not initialized")

    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    try:
        handler.handle(body.decode("utf-8"), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    # ตอบทันทีเพื่อกัน timeout
    return JSONResponse({"status": "ok"})


# ====== LINE HANDLER (ลงทะเบียนใน startup) ======
def handle_message(event):
    """
    1) ตอบทันทีสั้นๆ เพื่อให้ webhook เร็ว
    2) ประมวลผล LLM แบบ background แล้ว push ตามไป (user/group/room)
    """
    user_text = (event.message.text or "").strip()

    # ตอบเร็วๆ ก่อน
    try:
        if line_bot_api:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="กำลังคิดคำตอบให้ครับ… เดี๋ยวส่งตามไปอีกข้อความ"),
            )
    except Exception as e:
        print("Quick reply error:", e)

    # ทำงานเบื้องหลัง
    def work():
        try:
            answer = ask_llm_with_fallback(user_text)[:MAX_LINE_CHARS]
            to_id = get_target_id(event)
            if to_id and line_bot_api:
                line_bot_api.push_message(to_id, TextSendMessage(text=answer))
            else:
                print("⚠️ Cannot push (no target id or api not ready)")
        except Exception as e:
            print("Background LLM error:", e)

    Thread(target=work, daemon=True).start()
