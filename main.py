def ask_llm(prompt: str) -> str:
    """
    เรียก Hugging Face Inference API (ฟรี) ของโมเดลโอเพนซอร์ส
    ดีพอสำหรับทดสอบ production แบบไม่ต้องพึ่ง tunnel
    """
    HF_TOKEN = os.getenv("HF_TOKEN")
    HF_MODEL = os.getenv("HF_MODEL", "Qwen/Qwen2.5-3B-Instruct")
    if not HF_TOKEN:
        return "ยังไม่ได้ตั้งค่า HF_TOKEN บนเซิร์ฟเวอร์"

    url = f"https://api-inference.huggingface.co/models/{HF_MODEL}"
    headers = {
        "Authorization": f"Bearer {HF_TOKEN}",
        "Content-Type": "application/json",
    }

    # จัด prompt แบบง่ายให้โมเดลสไตล์ chat
    system = "คุณคือผู้ช่วยภาษาไทย ตอบสั้น กระชับ ชัดเจน และสุภาพ"
    chat_prompt = f"{system}\n\nผู้ใช้: {prompt}\nผู้ช่วย:"

    payload = {
        "inputs": chat_prompt,
        "parameters": {
            "max_new_tokens": 256,
            "temperature": 0.7,
            "top_p": 0.9,
            "return_full_text": False
        }
    }

    try:
        r = requests.post(url, headers=headers, json=payload, timeout=60)
        r.raise_for_status()
        data = r.json()

        # รูปแบบผลลัพธ์ของ Inference API สำหรับ text-generation เป็น list ของ dict
        # เช่น [{'generated_text': '...'}]
        if isinstance(data, list) and data and "generated_text" in data[0]:
            return (data[0]["generated_text"] or "").strip()[:4800] or "..."
        # บางโมเดลคืนรูปแบบอื่น
        if isinstance(data, dict) and "generated_text" in data:
            return (data["generated_text"] or "").strip()[:4800] or "..."
        # กรณีรอโหลดโมเดล (cold start) หรือเจอ error message ของ HF
        if isinstance(data, dict) and "error" in data:
            msg = data.get("error", "")
            # ถ้าขึ้นว่าโมเดลกำลังโหลด ให้ลองใหม่อีกครั้ง
            if "loading" in msg.lower() or "currently loading" in msg.lower():
                return "โมเดลกำลังโหลดที่ฝั่ง Hugging Face (ลองอีกครั้งในไม่กี่วินาที)"
            return f"HF error: {msg}"
        return "ไม่สามารถแปลผลลัพธ์จากโมเดลได้"
    except requests.HTTPError as e:
        return f"เรียก HF ล้มเหลว: {e}"
    except Exception as e:
        return f"ข้อผิดพลาดไม่คาดคิด: {e}"
