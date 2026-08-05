import os
import requests
import uvicorn
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv

load_dotenv()

# Load our credentials
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "vector_secret_2026")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")

app = FastAPI(title="WhatsApp Site Fault Logger")

# ==========================================
# OUTGOING MESSAGE FUNCTION
# ==========================================
def send_whatsapp_message(to_number: str, message_text: str):
    """Sends a text message back to the user."""
    # We use Meta's Graph API v19.0 endpoint
    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": message_text}
    }
    
    # Send the request to Meta
    response = requests.post(url, headers=headers, json=payload)
    if response.status_code != 200:
        print(f"❌ Failed to send message: {response.text}")
    return response.status_code


# ==========================================
# INCOMING WEBHOOK ROUTES
# ==========================================
@app.get("/")
def home():
    return {"status": "online", "system": "WhatsApp Site Fault Logger"}

@app.get("/webhook")
async def verify_webhook(request: Request):
    """Meta Webhook Verification Handshake."""
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode and token:
        if mode == "subscribe" and token == VERIFY_TOKEN:
            return PlainTextResponse(content=challenge)
        else:
            raise HTTPException(status_code=403, detail="Verification token mismatch")
    raise HTTPException(status_code=400, detail="Missing parameters")

@app.post("/webhook")
async def receive_message(request: Request):
    """Receives and parses incoming WhatsApp messages."""
    try:
        body = await request.json()
        
        # Meta sends a heavily nested JSON payload. We have to dig into it carefully.
        if "object" in body and body["object"] == "whatsapp_business_account":
            for entry in body.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    
                    # 1. Check if this is an actual message (and not just a "read receipt")
                    if "messages" in value:
                        msg = value["messages"][0]
                        sender_phone = msg["from"] # The phone number of the person who texted the bot
                        
                        # 2. Handle TEXT messages
                        if msg["type"] == "text":
                            incoming_text = msg["text"]["body"]
                            print(f"💬 TEXT received from {sender_phone}: {incoming_text}")
                            
                            # Have the bot reply!
                            send_whatsapp_message(sender_phone, f"✅ Received your report: '{incoming_text}'. I am logging it now.")
                            
                        # 3. Handle IMAGE messages
                        elif msg["type"] == "image":
                            image_id = msg["image"]["id"]
                            print(f"📸 IMAGE received! ID: {image_id}")
                            
                            # Have the bot reply!
                            send_whatsapp_message(sender_phone, f"📸 Image received! (ID: {image_id}). Fetching it for AI analysis...")

        # We must return a 200 OK immediately so Meta doesn't think the server crashed
        return {"status": "success"}

    except Exception as e:
        print(f"❌ Error handling payload: {e}")
        return {"status": "error"}

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)