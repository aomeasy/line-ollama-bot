import base64
import hashlib
import hmac
import json
import os
import re
import random
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

# ── ENV ───────────────────────────────────────────────────────────────────────
OLLAMA_API_URL = os.getenv("OLLAMA_API_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:8b")

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")

# PROMPT_BASE: กติกาหลักของบอท (ไทยล้วน + ลงท้าย ฯลฯ)
PROMPT_BASE = os.getenv(
    "PROMPT_SYSTEM",
    (
        "คุณคือผู้ช่วย AI สำหรับ LINE OA\n"
        "ทุกคำตอบต้องเป็นภาษาไทย 100% เท่านั้น\n"
        "ห้ามใช้ภาษาอังกฤษแม้แต่ตัวเดียว เว้นแต่เป็นชื่อเฉพาะ (ระบบจะลบทิ้งให้อยู่ดี)\n"
        "ตอบอย่างกวนๆ ฮาๆ เหมือนเพื่อนสนิท\n"
        "ลงท้ายทุกคำตอบด้วย \"จร้าาาาา\"\n"
        "ห้ามละเมิดกฎนี้เด็ดขาด"
    ),
)

MAX_TOKENS = int(os.getenv("MAX_TOKENS", "350"))  # จำกัดความยาวจากโมเดล
MAX_CHARS  = int(os.getenv("MAX_CHARS",  "1000")) # กันยาวเกินเวลาแสดงใน LINE

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    print("⚠️ Missing LINE env: LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET")

# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI(title="LINE × Ollama (TH + Utilities)", version="2.0.0")

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

# ── LINE Signature ────────────────────────────────────────────────────────────
def verify_line_signature(body: bytes, signature: str, secret: str) -> bool:
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected_signature = base64.b64encode(mac).decode("utf-8")
    return hmac.compare_digest(expected_signature, signature or "")

# ── Personas (System Prompts) ─────────────────────────────────────────────────
SYSTEM_PROMPTS: Dict[str, Dict[str, str]] = {
    "general":   {"name": "🤖 ผู้ช่วยทั่วไป",      "prompt": "คุณเป็นผู้ช่วย AI ที่เป็นมิตรและใช้ภาษาไทย ตอบสุภาพ กระชับ ได้ใจความ"},
    "teacher":   {"name": "👨‍🏫 ครูสอนพิเศษ",       "prompt": "คุณเป็นครูใจดี อธิบายเรื่องยากให้เข้าใจง่าย ยกตัวอย่าง ถามย้ำเพื่อความเข้าใจ"},
    "consultant":{"name": "💼 ที่ปรึกษาธุรกิจ",     "prompt": "คุณเป็นที่ปรึกษามืออาชีพ วิเคราะห์เป็นระบบ เสนอมาตรการที่ทำได้จริง"},
    "programmer":{"name": "💻 โปรแกรมเมอร์",        "prompt": "คุณเป็นโปรแกรมเมอร์ อธิบายเทคนิคให้เข้าใจง่าย พร้อมตัวอย่างโค้ด และ best practices"},
    "doctor":    {"name": "👩‍⚕️ หมอให้คำปรึกษา",   "prompt": "คุณให้ความรู้สุขภาพเบื้องต้น และเตือนให้ปรึกษาแพทย์จริงสำหรับการวินิจฉัย"},
    "chef":      {"name": "👨‍🍳 เชฟครัวไทย",       "prompt": "คุณเป็นเชฟอาหารไทย แนะนำเมนู วิธีทำ เทคนิค และเคล็ดลับในครัว"},
    "counselor": {"name": "🧠 นักจิตวิทยา",        "prompt": "คุณรับฟังด้วยความเข้าใจ ให้คำปรึกษาอย่างอบอุ่นและสร้างสรรค์"},
    "fitness":   {"name": "💪 โค้ชฟิตเนส",          "prompt": "คุณแนะนำการออกกำลังกาย โภชนาการ และเป็นกำลังใจ"},
    "travel":    {"name": "✈️ ไกด์ท่องเที่ยว",     "prompt": "คุณเป็นไกด์ รู้สถานที่ วัฒนธรรม อาหาร และเคล็ดลับการเดินทาง"},
    "comedian":  {"name": "😄 นักตลก",             "prompt": "คุณตอบแบบสนุก มีอารมณ์ขัน แต่ไม่หยาบ และยังให้ข้อมูลได้"},
}

# In-memory session (สำหรับ demo)
user_sessions: Dict[str, Dict[str, str]] = {}

# ── Utilities (เหมือนในโค้ด Flask เดิม) ────────────────────────────────────
MOTIVATIONAL_QUOTES = [
    "💪 ความสำเร็จเริ่มจากการลงมือทำ",
    "🌟 วันนี้คือโอกาสใหม่ที่จะทำให้ดีขึ้น",
    "🚀 อย่ายอมแพ้ เพราะสิ่งดีๆ กำลังจะมา",
    "💎 คุณแข็งแกร่งกว่าที่คิด",
    "🌈 หลังฝนย่อมมีรุ้ง"
]

async def get_exchange_rate_text() -> str:
    url = "https://api.exchangerate-api.com/v4/latest/USD"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
        thb = data["rates"].get("THB", 0)
        eur = data["rates"].get("EUR", 0)
        jpy = data["rates"].get("JPY", 0)
        gbp = data["rates"].get("GBP", 0)
        return f"💱 อัตราแลกเปลี่ยนวันนี้\n1 USD = {thb:.2f} THB\n\nอัตราอื่นๆ:\n• EUR: {eur:.4f}\n• JPY: {jpy:.2f}\n• GBP: {gbp:.4f}"
    except Exception:
        return "❌ ไม่สามารถดึงข้อมูลอัตราแลกเปลี่ยนได้ในขณะนี้"

def get_thai_time_text() -> str:
    from datetime import datetime, timezone, timedelta
    thai_tz = timezone(timedelta(hours=7))
    now = datetime.now(thai_tz)
    thai_day = ['จันทร์','อังคาร','พุธ','พฤหัสบดี','ศุกร์','เสาร์','อาทิตย์']
    thai_month = ['', 'มกราคม','กุมภาพันธ์','มีนาคม','เมษายน','พฤษภาคม','มิถุนายน',
                  'กรกฎาคม','สิงหาคม','กันยายน','ตุลาคม','พฤศจิกายน','ธันวาคม']
    return f"🕐 เวลาปัจจุบัน (เขตเวลาไทย)\nวัน{thai_day[now.weekday()]}ที่ {now.day} {thai_month[now.month]} {now.year+543}\n{now.strftime('%H:%M:%S')}"

def generate_password_text(length: int = 12) -> str:
    import string
    length = max(4, min(length, 50))
    chars = string.ascii_letters + string.digits + "!@#$%&*"
    pwd = "".join(random.choice(chars) for _ in range(length))
    return f"🔐 รหัสผ่านที่สร้างให้:\n{pwd}\n\n💡 คำแนะนำ:\n• เก็บรหัสผ่านในที่ปลอดภัย\n• ไม่แชร์ให้ใคร\n• เปลี่ยนเป็นระยะ"

def calculate_bmi_text(weight: float, height_cm: float) -> str:
    try:
        bmi = weight / (height_cm / 100) ** 2
        if bmi < 18.5: status, advice = "น้ำหนักต่ำกว่าเกณฑ์", "เพิ่มพลังงานและสร้างกล้ามเนื้อ"
        elif bmi < 25: status, advice = "น้ำหนักปกติ", "รักษาไลฟ์สไตล์ให้ดีต่อเนื่อง"
        elif bmi < 30: status, advice = "น้ำหนักเกิน", "ควบคุมอาหาร + ออกกำลังกายสม่ำเสมอ"
        else:          status, advice = "อ้วน", "ปรึกษาผู้เชี่ยวชาญและวางแผนลดน้ำหนัก"
        return f"📊 BMI\nค่า: {bmi:.1f}\nสถานะ: {status}\n💡 คำแนะนำ: {advice}"
    except Exception:
        return "❌ รูปแบบไม่ถูกต้อง | ตัวอย่าง: BMI 70 175"

def convert_units_text(value: float, from_unit: str, to_unit: str) -> str:
    conv = {
        # Length
        'cm_to_m': lambda x: x/100,    'm_to_cm': lambda x: x*100,
        'km_to_m': lambda x: x*1000,   'm_to_km': lambda x: x/1000,
        'inch_to_cm': lambda x: x*2.54,'cm_to_inch': lambda x: x/2.54,
        # Weight
        'kg_to_g': lambda x: x*1000,   'g_to_kg': lambda x: x/1000,
        'lb_to_kg': lambda x: x*0.453592,'kg_to_lb': lambda x: x/0.453592,
        # Temp
        'c_to_f': lambda x: (x*9/5)+32,'f_to_c': lambda x: (x-32)*5/9,
    }
    key = f"{from_unit}_to_{to_unit}".lower()
    if key in conv:
        try:
            res = conv[key](value)
            return f"🔄 แปลงหน่วย\n{value} {from_unit.upper()} = {res:.2f} {to_unit.upper()}"
        except Exception:
            return "❌ ตัวเลขไม่ถูกต้อง"
    avail = ", ".join(k.replace('_to_', '→') for k in conv.keys())
    return f"❌ ไม่รองรับ {from_unit}→{to_unit}\nรองรับ: {avail}"

def get_qr_text(text: str) -> str:
    from urllib.parse import quote
    encoded = quote(text)
    url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data={encoded}"
    return f"📱 QR Code ของคุณ:\n{url}\n\nข้อความ: {text}"

def color_code_info_text(code: str) -> str:
    c = code.strip()
    if not c: return "❌ กรุณาใส่โค้ดสี เช่น #FF5733 หรือ FF5733"
    if not c.startswith("#"): c = "#"+c
    hexpart = c[1:]
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", hexpart):
        return "❌ รูปแบบโค้ดสีไม่ถูกต้อง\nตัวอย่าง: #FF5733 หรือ FF5733"
    return f"🎨 ข้อมูลสี\nโค้ด: {c.upper()}\nดูตัวอย่าง: https://www.color-hex.com/color/{hexpart.lower()}"

def loan_calc_text(principal: float, rate: float, years: float) -> str:
    try:
        mr = rate/100/12
        months = int(years*12)
        if mr > 0:
            mp = principal * (mr * (1+mr)**months) / ((1+mr)**months - 1)
        else:
            mp = principal / months
        total = mp*months
        interest = total - principal
        return (f"💰 คำนวณสินเชื่อ\nเงินกู้: {principal:,.0f} บาท\n"
                f"ดอกเบี้ย: {rate}% ต่อปี | ระยะเวลา: {years} ปี\n\n"
                f"📊 ผลลัพธ์\nค่างวด/เดือน: {mp:,.0f} บาท\n"
                f"ดอกเบี้ยรวม: {interest:,.0f} บาท\nจ่ายรวม: {total:,.0f} บาท")
    except Exception:
        return "❌ รูปแบบไม่ถูกต้อง | ตัวอย่าง: สินเชื่อ 1000000 5 30"

# ── LINE Reply Helpers (Text + Quick Reply) ───────────────────────────────────
async def reply_text(reply_token: str, text: str) -> None:
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"replyToken": reply_token, "messages": [{"type": "text", "text": text[:4900]}]}
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(url, headers=headers, json=payload)
        if r.status_code != 200:
            print(f"❌ LINE reply error {r.status_code}: {r.text}")

def quick_reply_items(labels_texts: List[Dict[str, str]]) -> Dict[str, Any]:
    # labels_texts: [{"label": "xxx", "text": "yyy"}, ...]
    return {
        "items": [
            {"type": "action", "action": {"type": "message", "label": it["label"], "text": it["text"]}}
            for it in labels_texts
        ]
    }

async def reply_text_with_quickreply(reply_token: str, text: str, items: List[Dict[str, str]]) -> None:
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "replyToken": reply_token,
        "messages": [{
            "type": "text",
            "text": text[:4900],
            "quickReply": quick_reply_items(items)
        }]
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(url, headers=headers, json=payload)
        if r.status_code != 200:
            print(f"❌ LINE reply error {r.status_code}: {r.text}")

def get_persona_quickreply_message() -> (str, List[Dict[str, str]]):
    text = "🎭 เลือกบุคลิกที่ต้องการ:"
    items = [{"label": v["name"], "text": f"เลือก:{k}"} for k, v in SYSTEM_PROMPTS.items()]
    return text, items

def get_tools_quickreply_message() -> (str, List[Dict[str, str]]):
    text = "🛠️ เครื่องมือที่มีให้ใช้ (แตะปุ่ม):"
    items = [
        {"label": "💱 อัตราแลกเปลี่ยน", "text": "อัตราแลกเปลี่ยน"},
        {"label": "🕐 เวลาไทย", "text": "เวลา"},
        {"label": "💪 กำลังใจ", "text": "กำลังใจ"},
        {"label": "🔐 รหัสผ่าน", "text": "รหัสผ่าน"},
        {"label": "📊 BMI", "text": "BMI"},
        {"label": "🔄 แปลงหน่วย", "text": "แปลง 100 cm m"},
        {"label": "📱 สร้าง QR", "text": "QR hello"},
        {"label": "🎨 โค้ดสี", "text": "สี #FF5733"},
        {"label": "💰 สินเชื่อ", "text": "สินเชื่อ 1000000 5 30"},
        {"label": "🤖 กลับโหมด AI", "text": "AI"},
    ]
    return text, items

def get_current_system_info(user_id: str) -> Dict[str, str]:
    key = user_sessions.get(user_id, {}).get("system_prompt", "general")
    return SYSTEM_PROMPTS.get(key, SYSTEM_PROMPTS["general"])

# ── Thai-only Postprocess ─────────────────────────────────────────────────────
THAI_RANGE = r"\u0E00-\u0E7F"
RE_THINK   = re.compile(r"<think>.*?</think>", flags=re.DOTALL | re.IGNORECASE)

def _remove_reasoning(s: str) -> str:
    return RE_THINK.sub("", s)

def _keep_thai_digits_punct(s: str) -> str:
    return re.sub(
        rf"[^{THAI_RANGE}0-9๐-๙\s\.\,\!\?\:\;\-\+\=\(\)\[\]{{}}\"'\/…%]",
        "",
        s
    )

def _tidy_text(s: str) -> str:
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = re.sub(r"[，、]", ",", s)
    s = re.sub(r"[。]", ".", s)
    s = re.sub(r"([,\.!?])\1{1,}", r"\1", s)
    s = re.sub(r"\s+([,\.!?])", r"\1", s)
    s = re.sub(r"([,\.!?])([^\s])", r"\1 \2", s)
    return s.strip()

def _postprocess_thai(reply: str) -> str:
    reply = (reply or "").strip()
    reply = _remove_reasoning(reply)
    reply = _keep_thai_digits_punct(reply)
    reply = _tidy_text(reply)

    thai_count = len(re.findall(rf"[{THAI_RANGE}]", reply))
    if thai_count < 10:
        reply = "".join(re.findall(rf"[{THAI_RANGE}0-9๐-๙\s\.\,\!\?\…%]", reply)).strip()

    if len(reply) > MAX_CHARS:
        reply = reply[:MAX_CHARS - 1] + "…"

    if not reply.endswith("จร้าาาาา"):
        reply = reply.rstrip("!?. \n\r\t") + " จร้าาาาา"
    return reply

# ── Call Ollama (/api/chat) ───────────────────────────────────────────────────
async def ask_ollama(user_text: str, persona_prompt: str) -> str:
    url = f"{OLLAMA_API_URL}/api/chat"
    # รวม persona + base prompt เพื่อ “ย้ำ” บุคลิกพร้อมกติกาหลัก
    system_prompt = f"{PROMPT_BASE}\n\n---\nโหมดปัจจุบัน:\n{persona_prompt}".strip()

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
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
            r = await client.post(url, json=payload)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as e:
            print(f"❌ Ollama HTTP error: {e}")
            return _postprocess_thai("ขออภัย ระบบ AI ตอบไม่ได้ชั่วคราว ลองอีกครั้งได้ไหมคะ")

    # รองรับหลายรูปแบบรีสปอนส์
    content = None
    if isinstance(data.get("message"), dict):
        content = data["message"].get("content")
    if not content and isinstance(data.get("messages"), list) and data["messages"]:
        last = data["messages"][-1]
        if isinstance(last, dict):
            content = last.get("content")
    if not content and "response" in data:
        content = str(data.get("response"))
    if not content:
        content = "ขออภัย ไม่พบคำตอบที่เหมาะสมค่ะ"

    return _postprocess_thai(content)

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
        user_id = source.get("userId", "anonymous")

        if user_id not in user_sessions:
            user_sessions[user_id] = {"system_prompt": "general"}  # ค่าเริ่มต้น

        if etype == "message" and event.get("message", {}).get("type") == "text":
            user_text = (event["message"]["text"] or "").strip()
            lower = user_text.lower()

            # ——— คำสั่งควบคุมโหมด/เมนู
            if lower in {"ai", "แชท", "chat"}:
                name = get_current_system_info(user_id)["name"]
                msg = f"🤖 กลับสู่โหมด AI แล้ว!\nบุคลิกปัจจุบัน: {name}\n\nพิมพ์ 'เมนู' เพื่อเปลี่ยนบุคลิก หรือถามคำถามได้เลย"
                await reply_text(reply_token, _postprocess_thai(msg))
                continue

            if lower in {"เมนู", "menu", "เลือก", "เปลี่ยน"}:
                text, items = get_persona_quickreply_message()
                await reply_text_with_quickreply(reply_token, _postprocess_thai(text), items)
                continue

            if lower in {"เครื่องมือ", "tools", "ฟังก์ชัน", "functions", "utils"}:
                text, items = get_tools_quickreply_message()
                await reply_text_with_quickreply(reply_token, _postprocess_thai(text), items)
                continue

            if user_text.startswith("เลือก:"):
                key = user_text.replace("เลือก:", "").strip()
                if key in SYSTEM_PROMPTS:
                    user_sessions[user_id]["system_prompt"] = key
                    name = SYSTEM_PROMPTS[key]["name"]
                    msg = f"✅ เปลี่ยนบุคลิกเป็น {name} เรียบร้อยแล้ว!\nลองถามได้เลย หรือพิมพ์ 'เมนู' เพื่อเปลี่ยนอีกครั้ง"
                    await reply_text(reply_token, _postprocess_thai(msg))
                else:
                    await reply_text(reply_token, _postprocess_thai("❌ ไม่มีบุคลิกนี้นะ ลองพิมพ์ 'เมนู' เพื่อดูรายการ"))
                continue

            if lower in {"help", "ช่วย", "สถานะ", "status"}:
                current = get_current_system_info(user_id)
                msg = (
                    f"🤖 สถานะปัจจุบัน\nบุคลิก: {current['name']}\n\n"
                    "📋 คำสั่ง:\n"
                    "• 'เมนู' – เปลี่ยนบุคลิก\n"
                    "• 'เครื่องมือ' – ฟังก์ชันเสริม\n"
                    "• 'AI' – กลับโหมดแชท\n\n"
                    "🛠️ เครื่องมือ:\n"
                    "• อัตราแลกเปลี่ยน, เวลา, กำลังใจ\n"
                    "• BMI, แปลงหน่วย, QR, สี, สินเชื่อ, รหัสผ่าน"
                )
                await reply_text(reply_token, _postprocess_thai(msg))
                continue

            # ——— เครื่องมือเสริม
            if lower == "อัตราแลกเปลี่ยน":
                text = await get_exchange_rate_text()
                await reply_text(reply_token, _postprocess_thai(text))
                continue

            if lower == "เวลา":
                await reply_text(reply_token, _postprocess_thai(get_thai_time_text()))
                continue

            if lower in {"กำลังใจ", "motivate"}:
                await reply_text(reply_token, _postprocess_thai(random.choice(MOTIVATIONAL_QUOTES)))
                continue

            if lower.startswith("รหัสผ่าน"):
                parts = user_text.split()
                length = 12
                if len(parts) > 1 and parts[1].isdigit():
                    length = int(parts[1])
                await reply_text(reply_token, _postprocess_thai(generate_password_text(length)))
                continue

            if lower.startswith("bmi"):
                parts = user_text.split()
                if len(parts) == 3:
                    try:
                        w = float(parts[1]); h = float(parts[2])
                        await reply_text(reply_token, _postprocess_thai(calculate_bmi_text(w, h)))
                    except Exception:
                        await reply_text(reply_token, _postprocess_thai("❌ รูปแบบไม่ถูกต้อง | ตัวอย่าง: BMI 70 175"))
                else:
                    await reply_text(reply_token, _postprocess_thai("📊 วิธีใช้ BMI: พิมพ์ 'BMI [น้ำหนักกก.] [ส่วนสูงซม.]'"))
                continue

            if lower.startswith("แปลง"):
                parts = user_text.split()
                if len(parts) >= 4:
                    try:
                        val = float(parts[1]); frm = parts[2]; to = parts[3]
                        await reply_text(reply_token, _postprocess_thai(convert_units_text(val, frm, to)))
                    except Exception:
                        await reply_text(reply_token, _postprocess_thai("❌ รูปแบบไม่ถูกต้อง"))
                else:
                    await reply_text(reply_token, _postprocess_thai("🔄 แปลง [ตัวเลข] [หน่วยเดิม] [หน่วยใหม่]\nเช่น: แปลง 100 cm m"))
                continue

            if lower.startswith("qr "):
                text = user_text[3:].strip()
                if text:
                    await reply_text(reply_token, _postprocess_thai(get_qr_text(text)))
                else:
                    await reply_text(reply_token, _postprocess_thai("📱 พิมพ์: QR ข้อความ"))
                continue

            if lower.startswith("สี ") or user_text.startswith("#"):
                code = user_text[2:].strip() if user_text.startswith("สี ") else user_text.strip()
                await reply_text(reply_token, _postprocess_thai(color_code_info_text(code)))
                continue

            if lower.startswith("สินเชื่อ"):
                parts = user_text.split()
                if len(parts) == 4:
                    try:
                        p = float(parts[1]); r = float(parts[2]); y = float(parts[3])
                        await reply_text(reply_token, _postprocess_thai(loan_calc_text(p, r, y)))
                    except Exception:
                        await reply_text(reply_token, _postprocess_thai("❌ รูปแบบไม่ถูกต้อง"))
                else:
                    await reply_text(reply_token, _postprocess_thai("💰 สินเชื่อ [เงินกู้] [ดอกเบี้ย%] [ปี]\nเช่น: สินเชื่อ 1000000 5 30"))
                continue

            # ——— ปกติ: ส่งให้ AI ตาม persona ปัจจุบัน
            persona = get_current_system_info(user_id)
            ai_reply = await ask_ollama(user_text, persona["prompt"])
            await reply_text(reply_token, ai_reply)

        elif etype in {"follow", "join"}:
            await reply_text(reply_token, _postprocess_thai("สวัสดีค่า พิมพ์ 'เมนู' เพื่อเลือกบุคลิก หรือพิมพ์ 'เครื่องมือ' เพื่อดูฟังก์ชันเสริม"))

        # event อื่นๆ เงียบ
    return {"ok": True}

# ── Local run ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
