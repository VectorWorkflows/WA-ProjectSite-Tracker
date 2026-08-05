from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from src.config import VERIFY_TOKEN

app = FastAPI(title="WhatsApp Site Fault Logger")

@app.get("/")
def home():
    return {"status": "online", "system": "WhatsApp Site Fault Logger"}

@app.get("/webhook")
async def verify_webhook(request: Request):
    """
    Meta Webhook Verification Handshake.
    """
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode and token:
        if mode == "subscribe" and token == VERIFY_TOKEN:
            print("✅ Webhook Verified by Meta!")
            return PlainTextResponse(content=challenge)
        else:
            print("❌ Verification Token Mismatch")
            raise HTTPException(status_code=403, detail="Verification token mismatch")

    raise HTTPException(status_code=400, detail="Missing parameters")

@app.post("/webhook")
async def receive_message(request: Request):
    """
    Receives incoming WhatsApp messages (Text, Images, Documents, Fault Reports).
    """
    try:
        body = await request.json()
        print("\n📥 --- INCOMING WHATSAPP PAYLOAD ---")
        print(body)
        print("------------------------------------\n")

        # Meta requires an immediate 200 OK response
        return {"status": "success"}

    except Exception as e:
        print(f"❌ Error handling payload: {e}")
        return {"status": "error"}