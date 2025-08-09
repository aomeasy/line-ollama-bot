# main.py — เวอร์ชันทดสอบเส้นทาง
from fastapi import FastAPI, Request

app = FastAPI()

@app.get("/healthz")
def healthz():
    return {"ok": True}

@app.post("/callback")
async def callback(request: Request):
    # แค่รับแล้วตอบ 200 OK เพื่อยืนยันว่า route ทำงาน
    body = await request.body()
    # คุณจะ print ลง log ดูก็ได้:
    # print("Headers:", dict(request.headers))
    # print("Body:", body[:200])
    return {"status": "ok", "received_bytes": len(body)}
