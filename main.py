import os
import io
import datetime
import requests
import uvicorn
import gspread
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

load_dotenv()

# --- ENVIRONMENT VARIABLES ---
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "vector_secret_2026")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Site Fault Reports")
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID")

app = FastAPI(title="WhatsApp Site Fault Logger")

# --- GOOGLE APIS SETUP ---
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

def get_google_services():
    """Authenticates and returns Google Sheets and Drive clients."""
    creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    gc = gspread.authorize(creds)
    drive_service = build("drive", "v3", credentials=creds)
    return gc, drive_service

# --- HELPER FUNCTIONS ---
def send_whatsapp_message(to_number: str, message_text: str):
    """Sends a text reply back to the user."""
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
    requests.post(url, headers=headers, json=payload)

def download_whatsapp_media(media_id: str) -> bytes:
    """Gets the image URL from Meta and downloads the raw file."""
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    
    # 1. Ask Meta for the actual download URL
    meta_url = f"https://graph.facebook.com/v19.0/{media_id}"
    res = requests.get(meta_url, headers=headers).json()
    download_url = res.get("url")
    
    if not download_url:
        raise Exception("Failed to retrieve media URL from Meta.")

    # 2. Download the bytes using the Bearer token
    media_res = requests.get(download_url, headers=headers)
    return media_res.content

def upload_to_drive(drive_service, file_bytes: bytes, filename: str) -> str:
    """Uploads bytes to Drive and makes the link viewable."""
    file_metadata = {
        "name": filename,
        "parents": [DRIVE_FOLDER_ID] if DRIVE_FOLDER_ID else []
    }
    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype="image/jpeg")
    
    # Upload the file (Notice the supportsAllDrives=True parameter)
    uploaded_file = drive_service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id, webViewLink",
        supportsAllDrives=True
    ).execute()
    
    file_id = uploaded_file.get("id")
    
    # Change permissions so anyone with the link can view it
    drive_service.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"},
        supportsAllDrives=True
    ).execute()
    
    return uploaded_file.get("webViewLink")

def log_to_sheet(phone: str, text: str, photo_url: str = "N/A"):
    """Appends the formatted row into Google Sheets."""
    gc, _ = get_google_services()
    sheet = gc.open(GOOGLE_SHEET_NAME).sheet1
    
    # Generate timestamp in IST/Local format
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Append the row exactly matching our columns
    sheet.append_row([timestamp, phone, text, photo_url, "Pending Review"])

# --- WEBHOOK ROUTES ---
@app.get("/")
@app.head("/")
def home():
    return {"status": "online", "system": "WhatsApp Site Logger"}

@app.get("/webhook")
async def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return PlainTextResponse(content=challenge)
    raise HTTPException(status_code=403, detail="Verification failed")

@app.post("/webhook")
async def receive_message(request: Request):
    try:
        body = await request.json()
        
        if body.get("object") == "whatsapp_business_account":
            for entry in body.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    
                    if "messages" in value:
                        msg = value["messages"][0]
                        sender_phone = msg["from"]
                        
                        _, drive_service = get_google_services()

                        # --- HANDLE TEXT MESSAGES ---
                        if msg["type"] == "text":
                            user_text = msg["text"]["body"]
                            log_to_sheet(sender_phone, user_text)
                            send_whatsapp_message(sender_phone, f"✅ Report logged to spreadsheet:\n\n\"{user_text}\"")

                        # --- HANDLE IMAGES ---
                        elif msg["type"] == "image":
                            image_id = msg["image"]["id"]
                            # Extract the text they typed with the photo (or default to a placeholder)
                            caption = msg["image"].get("caption", "No description provided.")
                            
                            send_whatsapp_message(sender_phone, "📸 Receiving photo and generating report. Please wait a moment...")
                            
                            # Download & Upload
                            image_bytes = download_whatsapp_media(image_id)
                            filename = f"Site_Fault_{sender_phone}_{int(datetime.datetime.now().timestamp())}.jpg"
                            drive_link = upload_to_drive(drive_service, image_bytes, filename)
                            
                            # Log to Sheets
                            log_to_sheet(sender_phone, caption, drive_link)
                            send_whatsapp_message(sender_phone, f"✅ Photo & report successfully logged!\n\nLink: {drive_link}")

        return {"status": "success"}

    except Exception as e:
        print(f"❌ Error handling payload: {e}")
        return {"status": "error"}

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)