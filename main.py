# main.py
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

# กติกาหลัก (สั้น กระชับ สุภาพ ไทย และลงท้าย "งับ")
PROMPT_BASE = os.getenv(
    "PROMPT_SYSTEM",
    (
        "คุณคือผู้ช่วย AI สำหรับทีมภายในบน LINE OA ที่ตอบภาษาไทย สุภาพ กระชับ และถูกต้อง\n"
        "เป้าหมาย: ช่วยสรุปข้อความ/เขียนคำชี้แจง/ร่างประกาศ/ตอบคำถามทั่วไปตามที่ทีมต้องการ\n"
        "ห้ามใส่ข้อมูลที่ไม่แน่ใจว่าเป็นจริง หากไม่ทราบให้บอกอย่างตรงไปตรงมาและเสนอแนวทางถัดไป\n"
        "ถ้าเหมาะสมสามารถใช้ bullet point เพื่อให้อ่านง่าย\n"
        "ลงท้ายด้วยคำว่า \"งับ\" เสมอ"
    ),
)

MAX_TOKENS = int(os.getenv("MAX_TOKENS", "350"))
SNAPSHOT_API = os.getenv("SNAPSHOT_API", "").rstrip("/")  # e.g. https://snap.run/snapshot?url=

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    print("⚠️ Missing LINE env: LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET")

# ── FastAPI ───────────────────────────────────────────────────────────────────
app = FastAPI(title="LINE Internal Dashboard Bot", version="1.0.0")

@app.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        "ollama_url": OLLAMA_API_URL,
        "model": OLLAMA_MODEL,
        "has_line_token": bool(LINE_CHANNEL_ACCESS_TOKEN),
        "has_line_secret": bool(LINE_CHANNEL_SECRET),
        "max_tokens": MAX_TOKENS,
        "snapshot_api": SNAPSHOT_API or None,
    }

# ── LINE Signature ────────────────────────────────────────────────────────────
def verify_line_signature(body: bytes, signature: str, secret: str) -> bool:
    mac = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected_signature = base64.b64encode(mac).decode("utf-8")
    return hmac.compare_digest(expected_signature, signature or "")

# ── Post-process: ล้าง <think> + จัดวรรคตอน + บังคับลงท้าย ───────────────
RE_THINK = re.compile(r"<think>.*?</think>", flags=re.DOTALL | re.IGNORECASE)

def _remove_reasoning(s: str) -> str:
    return RE_THINK.sub("", s or "")

def _tidy_text(s: str) -> str:
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = re.sub(r"[，、]", ",", s)
    s = re.sub(r"[。]", ".", s)
    s = re.sub(r"([,\.!?])\1{1,}", r"\1", s)
    s = re.sub(r"\s+([,\.!?])", r"\1", s)
    s = re.sub(r"([,\.!?])([^\s])", r"\1 \2", s)
    return s.strip()

def _postprocess(reply: str) -> str:
    reply = _remove_reasoning(reply)
    reply = _tidy_text(reply)
    if not reply.endswith("งับ"):
        reply = reply.rstrip("!?. \n\r\t") + " งับ"
    return reply

# ── Helpers: LINE replies ────────────────────────────────────────────────────
def quick_reply_items(labels_texts: List[Dict[str, str]]) -> Dict[str, Any]:
    return {
        "items": [
            {"type": "action", "action": {"type": "message", "label": it["label"], "text": it["text"]}}
            for it in labels_texts
        ]
    }

async def reply_text_with_quickreply(reply_token: str, text: str, items: List[Dict[str, str]]):
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "replyToken": reply_token,
        "messages": [{
            "type": "text",
            "text": text,
            "quickReply": quick_reply_items(items)
        }]
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(url, headers=headers, json=payload)
        if r.status_code != 200:
            print(f"❌ LINE reply error {r.status_code}: {r.text}")

async def reply_image_with_quickreply(reply_token: str, original_url: str, preview_url: Optional[str], items: List[Dict[str, str]]):
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}", "Content-Type": "application/json"}
    if not preview_url:
        preview_url = original_url
    payload = {
        "replyToken": reply_token,
        "messages": [{
            "type": "image",
            "originalContentUrl": original_url,
            "previewImageUrl": preview_url,
            "quickReply": quick_reply_items(items)
        }]
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(url, headers=headers, json=payload)
        if r.status_code != 200:
            print(f"❌ LINE reply image error {r.status_code}: {r.text}")

async def reply_sticker(reply_token: str, package_id: str = "11537", sticker_id: str = "52002734"):
    # สติ๊กเกอร์ (มักเป็น animated) เพื่อสร้างความรู้สึก "animate" ตอนทักทาย
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {"Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}", "Content-Type": "application/json"}
    payload = {"replyToken": reply_token, "messages": [{"type": "sticker", "packageId": package_id, "stickerId": sticker_id}]}
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(url, headers=headers, json=payload)
        if r.status_code != 200:
            print(f"❌ LINE reply sticker error {r.status_code}: {r.text}")

# ── Always-attach Quick Reply (เมนูหลัก) ─────────────────────────────────────
def main_quick_items() -> List[Dict[str, str]]:
    return [
        {"label": "📊 คุณภาพบริการ รบ.", "text": "เมนู:คุณภาพบริการ"},
        {"label": "🗓️ Broadband Daily Report", "text": "เมนู:BB Daily"},
        {"label": "🧾 Out task Section C", "text": "เมนู:OutTask"},
        {"label": "🛠️ OLT ONU", "text": "เมนู:OLT"},
        {"label": "🔀 Switch NT", "text": "เมนู:SwitchNT"},
        {"label": "🌐 กลุ่มบริการ Broadband", "text": "เมนู:Broadband"},
        {"label": "🛰️ กลุ่มบริการ Datacom", "text": "เมนู:Datacom"},
        {"label": "🧩 อื่น ๆ", "text": "เมนู:อื่นๆ"},
    ]

async def reply_text_with_main_quick(reply_token: str, text: str):
    await reply_text_with_quickreply(reply_token, text, main_quick_items())

# ── Submenus ──────────────────────────────────────────────────────────────────
def submenu_quality_items() -> List[Dict[str, str]]:
    return [
        {"label": "🏗️ ติดตั้ง", "text": "รายงานการติดตั้ง"},
        {"label": "🧯 แก้เหตุเสีย", "text": "รายงานการแก้ไขเหตุเสีย"},
        {"label": "🔌 เหตุเสีย/พอร์ท", "text": "เหตุเสียต่อพอร์ท"},
        {"label": "♻️ เสียซ้ำ", "text": "อัตราเสียซ้ำ"},
        {"label": "🛰️ SA (Datacom)", "text": "SA (Datacom)"},
    ]

def submenu_bb_daily_items() -> List[Dict[str, str]]:
    return [
        {"label": "🖼️ TTS → รูป", "text": "BB TTS"},
        {"label": "🖼️ SCOMS → รูป", "text": "BB SCOMS"},
    ]

def submenu_others_items() -> List[Dict[str, str]]:
    return [
        {"label": "✍️ ร่างสรุปวันนี้", "text": "ร่างสรุปวันนี้"},
        {"label": "🧠 Q&A ผู้ช่วย AI", "text": "Q&A"},
        {"label": "📌 Pin ลิงก์สำคัญ", "text": "Pins"},
        {"label": "🧪 Mock KPIs", "text": "Mock KPIs"},
    ]

# ── Mock / Draft / Pins ──────────────────────────────────────────────────────
def draft_summary_text() -> str:
    from datetime import datetime, timezone, timedelta
    th = timezone(timedelta(hours=7))
    now = datetime.now(th)
    date_txt = now.strftime("%d/%m/%Y")
    # โครงร่างสั้นๆ เอาไปโพสต์ในไลน์กลุ่มได้ (ไม่มี DB)
    return _postprocess(
        f"สรุปสถานการณ์ประจำวัน {date_txt}\n"
        f"• ภาพรวม: การให้บริการเป็นไปตามปกติ\n"
        f"• ประเด็นเด่น: ไม่มีเหตุล่มวงกว้าง, มีรายงานปัญหาเฉพาะจุดบางพื้นที่\n"
        f"• การสื่อสาร: ทีมพร้อมอัปเดตหากมีเหตุสำคัญเพิ่มเติม"
    )

def mock_kpis_text() -> str:
    # สุ่มตัวเลขเล็ก ๆ เพื่อเดโม (ไม่ใช่ข้อมูลจริง)
    total = random.randint(120, 260)
    closed = random.randint(int(total*0.6), int(total*0.9))
    sla = round(random.uniform(90.0, 97.5), 1)
    mtta = random.randint(12, 28)
    mttr = round(random.uniform(1.8, 3.2), 1)
    csat = round(random.uniform(4.1, 4.6), 2)
    top_issue = random.choice(["อินเทอร์เน็ตช้า", "ขัดข้องเฉพาะพื้นที่", "บิล/ชำระเงิน", "ตั้งค่าราวเตอร์"])
    return _postprocess(
        "Mock KPIs (เดโม)\n"
        f"• งานรับเข้า: {total} เคส | ปิดแล้ว: {closed}\n"
        f"• SLA on-time: {sla}% | MTTA: {mtta} นาที | MTTR: {mttr} ชม.\n"
        f"• CSAT เฉลี่ย: {csat}\n"
        f"• อาการบ่อย: {top_issue}"
    )

def pinned_links_text() -> str:
    return _postprocess(
        "📌 ลิงก์สำคัญ\n"
        "• Looker (TTS): https://lookerstudio.google.com/reporting/b893918e-8fff-4cdb-8847-22273278669a/page/B03KD\n"
        "• Looker (SCOMS): https://lookerstudio.google.com/reporting/b893918e-8fff-4cdb-8847-22273278669a/page/p_m4ex303otd\n"
        "• แนวทางสื่อสารเหตุขัดข้อง (Template): https://example.com/comm-guide\n"
        "• เกณฑ์ SLA สรุปย่อ: https://example.com/sla-brief"
    )

# ── Ollama chat (Q&A TH, ไม่มี persona) ──────────────────────────────────────
async def ask_ollama(user_text: str) -> str:
    url = f"{OLLAMA_API_URL}/api/chat"
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": PROMPT_BASE},
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
            return _postprocess("ขออภัย ระบบ AI ตอบไม่ได้ชั่วคราว ลองอีกครั้งได้ไหมคะ")
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
    return _postprocess(content)

# ── Snapshot helper ───────────────────────────────────────────────────────────
async def get_snapshot_image_url(target_url: str) -> Optional[str]:
    if not SNAPSHOT_API:
        return None
    # สมมติ SNAPSHOT_API เป็น base ที่ต่อท้ายด้วย URL ได้เลย เช่น https://snap.run/snapshot?url=
    query_url = f"{SNAPSHOT_API}{target_url}"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.get(query_url)
            r.raise_for_status()
            data = r.json()
            return data.get("image_url")
    except Exception as e:
        print(f"❌ Snapshot error: {e}")
        return None

# ── Webhook ───────────────────────────────────────────────────────────────────
@app.post("/callback")
async def line_callback(request: Request, x_line_signature: str = Header(None)):
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
        if not reply_token:
            continue

        # ทักทายตอน follow/join ด้วยสติ๊กเกอร์ + เมนูหลัก
        if etype in {"follow", "join"}:
            await reply_sticker(reply_token)
            await reply_text_with_main_quick(reply_token, _postprocess("สวัสดีค่ะ เลือกเมนูด้านล่างเพื่อเริ่มใช้งานได้เลย"))
            continue

        # เฉพาะข้อความ
        if etype == "message" and event.get("message", {}).get("type") == "text":
            user_text = (event["message"]["text"] or "").strip()
            lower = user_text.lower()

            # คำทักทายทั่วไป → ส่งสติ๊กเกอร์ + เมนู
            if lower in {"start", "เริ่ม", "สวัสดี", "hello", "hi"}:
                await reply_sticker(reply_token)
                await reply_text_with_main_quick(reply_token, _postprocess("ยินดีช่วยครับ เลือกเมนูด้านล่างได้เลย"))
                continue

            # ── Show submenus ────────────────────────────────────────────────
            if user_text == "เมนู:คุณภาพบริการ":
                await reply_text_with_quickreply(reply_token, _postprocess("เลือกหัวข้อคุณภาพบริการ รบ."), submenu_quality_items())
                continue

            if user_text == "เมนู:BB Daily":
                await reply_text_with_quickreply(reply_token, _postprocess("เลือกหัวข้อ Broadband Daily Report"), submenu_bb_daily_items())
                continue

            if user_text == "เมนู:อื่นๆ":
                await reply_text_with_quickreply(reply_token, _postprocess("เมนูเสริม (ไม่แตะฐานข้อมูล)"), submenu_others_items())
                continue

            # ── Leaf actions (static wait replies) ──────────────────────────
            if user_text in {
                "รายงานการติดตั้ง", "รายงานการแก้ไขเหตุเสีย", "เหตุเสียต่อพอร์ท", "อัตราเสียซ้ำ", "SA (Datacom)",
                "เมนู:OutTask", "เมนู:OLT", "เมนู:SwitchNT", "เมนู:Broadband", "เมนู:Datacom"
            }:
                await reply_text_with_main_quick(reply_token, _postprocess("รอ update แปปงับ"))
                continue

            # ── Looker snapshots → image into LINE ─────────────────────────
            if user_text == "BB TTS":
                tts_url = "https://lookerstudio.google.com/reporting/b893918e-8fff-4cdb-8847-22273278669a/page/B03KD"
                img = await get_snapshot_image_url(tts_url)
                if img:
                    await reply_image_with_quickreply(reply_token, img, None, main_quick_items())
                else:
                    await reply_text_with_main_quick(reply_token, _postprocess("ยังแคปรูปไม่ได้ (ไม่พบ SNAPSHOT_API) งับ"))
                continue

            if user_text == "BB SCOMS":
                scoms_url = "https://lookerstudio.google.com/reporting/b893918e-8fff-4cdb-8847-22273278669a/page/p_m4ex303otd"
                img = await get_snapshot_image_url(scoms_url)
                if img:
                    await reply_image_with_quickreply(reply_token, img, None, main_quick_items())
                else:
                    await reply_text_with_main_quick(reply_token, _postprocess("ยังแคปรูปไม่ได้ (ไม่พบ SNAPSHOT_API) งับ"))
                continue

            # ── Others submenu actions ─────────────────────────────────────
            if user_text == "ร่างสรุปวันนี้":
                await reply_text_with_main_quick(reply_token, draft_summary_text())
                continue

            if user_text == "Pins":
                await reply_text_with_main_quick(reply_token, pinned_links_text())
                continue

            if user_text == "Mock KPIs":
                await reply_text_with_main_quick(reply_token, mock_kpis_text())
                continue

            if user_text == "Q&A":
                await reply_text_with_main_quick(
                    reply_token,
                    _postprocess("พิมพ์คำถามหรือประเด็นที่อยากให้ช่วยร่างคำตอบได้เลย (เช่น ขอร่างประกาศสั้นๆ เรื่องอินเทอร์เน็ตช้าในเขตเหนือ)")
                )
                continue

            # ── Default: ส่งให้ AI ช่วย (ไม่มี persona) ────────────────────
            ai_reply = await ask_ollama(user_text)
            await reply_text_with_main_quick(reply_token, ai_reply)

    return {"ok": True}

# ── Local run ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
