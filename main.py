import os, json, requests
from threading import Thread
from typing import List, Optional, Tuple

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

load_dotenv()
app = FastAPI()

# ---------- LINE ENV ----------
LINE_CHANNEL_ACCESS_TOKEN = (os.getenv("LINE_CHANNEL_ACCESS_TOKEN") or "").strip()
LINE_CHANNEL_SECRET = (os.getenv("LINE_CHANNEL_SECRET") or "").strip()

# ---------- HF ENV ----------
HF_TOKEN = (os.getenv("HF_TOKEN") or "").strip()
HF_MODEL = (os.getenv("HF_MODEL") or "Qwen/Qwen2.5-1.5B-Instruct").strip()
HF_MODELS = (os.getenv("HF_MODELS") or "").strip()

# ---------- OLLAMA ENV ----------
# ตัวอย่าง: OLLAMA_BASE_URL="http://127.0.0.1:11434"
OLLAMA_BASE_URL = (os.getenv("OLLAMA_BASE_URL") or "").strip().rstrip("/")
# ตัวอย่าง: OLLAMA_MODEL="qwen2.5:3b" หรือ "llama3.1:8b-instruct"
OLLAMA_MODEL = (os.getenv("OLLAMA_MODEL") or "llama3.1:8b-instruct").strip()

MAX_LINE_CHARS = 5000

line_bot_api: Optional[LineBotApi] = None
handler: Optional[WebhookHandler] = None

# จำโมเดล HF ที่เวิร์คล่าสุด เพื่อลดการลองซ้ำ
ACTIVE_HF_MODEL: Optional[str] = None

# ===== Helpers =====
def parse_hf_models() -> List[str]:
    default = [
        HF_MODEL,
        "Qwen/Qwen2.5-3B-Instruct",
        "HuggingFaceH4/zephyr-7b-beta",
        "microsoft/DialoGPT-medium",
        "gpt2",
    ]
    given = [m.strip() for m in HF_MODELS.split(",") if m.strip()]
    models = given + default
    uniq, seen = [], set()
    for m in models:
        if m not in seen:
            uniq.append(m); seen.add(m)
    return uniq

def hf_call(model: str, prompt: str) -> Tuple[bool, str, int]:
    if not HF_TOKEN:
        return False, "HF_TOKEN not set", 0
    url = f"https://api-inference.huggingface.co/models/{model}"
    headers = {
        "Authorization": f"Bearer {HF_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"inputs": prompt, "options": {"wait_for_model": True}}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=60)
        status = r.status_code
        if status in (404, 503):
            # 404/503 → ข้ามไปลองตัวถัดไป
            try:
                data = r.json()
            except Exception:
                data = {"raw": r.text[:300]}
            return False, f"HF {status} on {model}: {json.dumps(data)[:300]}", status
        if not (200 <= status < 300):
            return False, f"HF {status} on {model}: {r.text[:300]}", status

        data = r.json()
        # เคสยอดฮิต: list[{generated_text: ...}]
        if isinstance(data, list):
            for it in data:
                if isinstance(it, dict) and "generated_text" in it:
                    return True, str(it["generated_text"])[:MAX_LINE_CHARS], status
        if isinstance(data, dict) and "generated_text" in data:
            return True, str(data["generated_text"])[:MAX_LINE_CHARS], status
        return True, json.dumps(data, ensure_ascii=False)[:MAX_LINE_CHARS], status
    except Exception as e:
        return False, f"HF exception on {model}: {e}", 0

def ollama_call(prompt: str) -> Tuple[bool, str]:
    if not OLLAMA_BASE_URL or not OLLAMA_MODEL:
        return False, "Ollama not configured"
    try:
        # ใช้ endpoint /api/chat (แนะนำสำหรับโมเดลสไตล์ instruct)
        url = f"{OLLAMA_BASE_URL}/api/chat"
        payload = {
            "model": OLLAMA_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        r = requests.post(url, json=payload, timeout=60)
        if not r.ok:
            return False, f"Ollama {r.status_code}: {r.text[:300]}"
        data = r.json()
        # โครงสร้างมาตรฐานของ Ollama
        content = (data.get("message") or {}).get("content") or ""
        if not content:
            # เผื่อบางรุ่นคืนรูปแบบอื่น
            content = json.dumps(data, ensure_ascii=False)
        return True, content[:MAX_LINE_CHARS]
    except Exception as e:
        return False, f"Ollama exception: {e}"

def ask_llm(prompt: str) -> str:
    """ลำดับการลอง: HF (active→fallback list) → Ollama"""
    global ACTIVE_HF_MODEL
    tried_msgs = []

    # 1) HF active
    if ACTIVE_HF_MODEL:
        ok, text, _ = hf_call(ACTIVE_HF_MODEL, prompt)
        if ok: return text
        tried_msgs.append(text)

    # 2) HF fallback list
    for m in parse_hf_models():
        if ACTIVE_HF_MODEL and m == ACTIVE_HF_MODEL:
            continue
        ok, text, _ = hf_call(m, prompt)
        if ok:
            ACTIVE_HF_MODEL = m
            return text
        tried_msgs.append(text)

    # 3) Ollama
    ok, text = ollama_call(prompt)
    if ok:
        return text
    tried_msgs.append(text)

    return "(LLM) ไม่สามารถเรียกใช้งานผู้ให้บริการใดได้ตอนนี้\n" + "\n".join(tried_msgs[-3:])

def get_target_id(event) -> Optional[str]:
    src = event.source
    for k in ("user_id", "group_id", "room_id"):
        v = getattr(src, k, None)
        if v: return v
    return None

# ===== FastAPI lifecycle =====
@app.on_event("startup")
def on_startup():
    global line_bot_api, handler
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
        print("⚠️ Missing LINE env")
    else:
        line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
        handler = WebhookHandler(LINE_CHANNEL_SECRET)
        handler.add(MessageEvent, message=TextMessage)(handle_message)

@app.get("/healthz")
def healthz():
    return {
        "ok": True,
        "active_hf_model": ACTIVE_HF_MODEL,
        "ollama": bool(OLLAMA_BASE_URL),
    }

@app.get("/hfcheck")
def hfcheck():
    res = []
    for m in parse_hf_models():
        ok, text, status = hf_call(m, "ping")
        res.append({"model": m, "ok": ok, "status": status, "sample": text[:160]})
    return {"results": res, "active_hf_model": ACTIVE_HF_MODEL}

@app.get("/ollamacheck")
def ollamacheck():
    ok, text = ollama_call("ping")
    return {"ok": ok, "sample": text[:160], "model": OLLAMA_MODEL, "base": OLLAMA_BASE_URL}

@app.get("/")
def root():
    return {"msg": "LINE + AI bot is running", "active_hf_model": ACTIVE_HF_MODEL}

@app.post("/callback")
async def callback(request: Request):
    if handler is None:
        raise HTTPException(status_code=500, detail="LINE handler not initialized")
    sig = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    try:
        handler.handle(body.decode("utf-8"), sig)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return JSONResponse({"status": "ok"})

# ===== LINE handler =====
def handle_message(event):
    user_text = (event.message.text or "").strip()

    # ตอบกลับเร็ว ๆ เพื่อกัน timeout
    try:
        if line_bot_api:
            line_bot_api.reply_message(
                event.reply_token,
                TextSendMessage(text="กำลังคิดคำตอบให้ครับ… เดี๋ยวส่งตามไปอีกข้อความ"),
            )
    except Exception as e:
        print("Quick reply error:", e)

    def work():
        try:
            answer = ask_llm(user_text)
            to_id = get_target_id(event)
            if to_id and line_bot_api:
                line_bot_api.push_message(to_id, TextSendMessage(text=answer[:MAX_LINE_CHARS]))
            else:
                print("⚠️ Cannot push (no id/api)")
        except Exception as e:
            print("BG error:", e)

    Thread(target=work, daemon=True).start()
