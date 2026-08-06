import os
import io
import datetime
import json
import requests
import uvicorn
import gspread
import certifi
import pymongo
import google.generativeai as genai
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
MONGODB_URI = os.getenv("MONGODB_URI")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

app = FastAPI(title="WhatsApp Site Fault Logger")


# --- INITIALIZE MONGODB ---
mongo_client = pymongo.MongoClient(MONGODB_URI, tlsCAFile=certifi.where())
db = mongo_client["wa_logger_db"]
users_collection = db["users"]

# --- INITIALIZE GEMINI AI ---
genai.configure(api_key=GEMINI_API_KEY)
ai_model = genai.GenerativeModel('gemini-1.5-flash')

# --- GOOGLE APIS SETUP ---
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

def get_google_services():
    creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    gc = gspread.authorize(creds)
    drive_service = build("drive", "v3", credentials=creds)
    return gc, drive_service

# --- HELPER FUNCTIONS ---
def send_whatsapp_message(to_number: str, message_text: str):
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
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    meta_url = f"https://graph.facebook.com/v19.0/{media_id}"
    res = requests.get(meta_url, headers=headers).json()
    download_url = res.get("url")
    if not download_url:
        raise Exception("Failed to retrieve media URL from Meta.")
    media_res = requests.get(download_url, headers=headers)
    return media_res.content

def upload_to_drive(drive_service, file_bytes: bytes, filename: str) -> str:
    file_metadata = {
        "name": filename,
        "parents": [DRIVE_FOLDER_ID] if DRIVE_FOLDER_ID else []
    }
    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype="image/jpeg")
    uploaded_file = drive_service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id, webViewLink",
        supportsAllDrives=True
    ).execute()
    file_id = uploaded_file.get("id")
    drive_service.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"},
        supportsAllDrives=True
    ).execute()
    return uploaded_file.get("webViewLink")

def log_to_sheet(row_data: list):
    gc, _ = get_google_services()
    sheet = gc.open(GOOGLE_SHEET_NAME).sheet1
    sheet.append_row(row_data)

def extract_fault_details_with_ai(caption: str) -> dict:
    """Uses Gemini to parse the caption into location and fault description."""
    if not caption or caption == "No description provided.":
        return {"specific_area": "Not specified", "fault_description": "No description provided."}
    
    prompt = f"""
    Analyze this construction/site fault report: "{caption}"
    Extract the specific physical location details (like floor, apartment, house no, room) and the actual fault issue.
    Respond ONLY with a valid JSON object exactly matching this format:
    {{"specific_area": "extracted location", "fault_description": "extracted fault"}}
    If no specific area is mentioned, use "Not specified" for specific_area.
    """
    try:
        response = ai_model.generate_content(prompt)
        raw_text = response.text.replace('```json', '').replace('```', '').strip()
        return json.loads(raw_text)
    except Exception as e:
        print(f"AI Parsing Error: {e}")
        return {"specific_area": "Not specified", "fault_description": caption}

# --- WEBHOOK ROUTES ---
@app.get("/")
@app.head("/")
def home():
    return {"status": "online", "system": "WhatsApp Site Logger DB"}

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
                        msg_type = msg["type"]
                        
                        # --- 1. FETCH OR CREATE USER IN MONGODB ---
                        user = users_collection.find_one({"phone": sender_phone})
                        if not user:
                            user = {"phone": sender_phone, "state": "awaiting_project", "project": "", "site": ""}
                            users_collection.insert_one(user)
                            send_whatsapp_message(sender_phone, "👋 Welcome to the Site Tracker!\n\nTo get started, please reply with your *Project Name* (e.g., Vector Heights).")
                            return {"status": "success"}

                        # --- 2. HANDLE TEXT MESSAGES (COMMANDS & SETUP) ---
                        if msg_type == "text":
                            user_text = msg["text"]["body"].strip()
                            
                            # The /update command
                            if user_text.lower() == "/update":
                                users_collection.update_one({"phone": sender_phone}, {"$set": {"state": "awaiting_project"}})
                                send_whatsapp_message(sender_phone, "🔄 Location update initiated.\n\n🏢 What is the new *Project Name*?")
                                return {"status": "success"}
                            
                            # State: Awaiting Project Name
                            if user["state"] == "awaiting_project":
                                users_collection.update_one({"phone": sender_phone}, {"$set": {"project": user_text, "state": "awaiting_site"}})
                                send_whatsapp_message(sender_phone, f"Got it. Project set to *{user_text}*.\n\n📍 Now, what is the *Site Location*? (e.g., Tower B)")
                                return {"status": "success"}
                                
                            # State: Awaiting Site Location
                            elif user["state"] == "awaiting_site":
                                users_collection.update_one({"phone": sender_phone}, {"$set": {"site": user_text, "state": "active"}})
                                send_whatsapp_message(sender_phone, "✅ *Setup Complete!*\n\nYou can now send photos with captions. To change your location anytime, just type */update*.")
                                return {"status": "success"}
                                
                            # State: Active (Sending text without photo)
                            elif user["state"] == "active":
                                send_whatsapp_message(sender_phone, "📸 Please attach a photo when reporting a fault. You can type the description in the photo's caption!\n\n*(Type /update if you need to change your site)*")
                                return {"status": "success"}

                        # --- 3. HANDLE IMAGE MESSAGES (ACTIVE REPORTING) ---
                        elif msg_type == "image":
                            if user["state"] != "active":
                                send_whatsapp_message(sender_phone, "⚠️ Please finish your setup first before sending photos.")
                                return {"status": "success"}
                                
                            image_id = msg["image"]["id"]
                            caption = msg["image"].get("caption", "No description provided.")
                            
                            send_whatsapp_message(sender_phone, "⚙️ Processing your report using AI...")
                            
                            # A. Extract Location & Fault using Gemini
                            ai_data = extract_fault_details_with_ai(caption)
                            
                            # B. Process Image to Google Drive
                            _, drive_service = get_google_services()
                            image_bytes = download_whatsapp_media(image_id)
                            filename = f"Site_Fault_{sender_phone}_{int(datetime.datetime.now().timestamp())}.jpg"
                            drive_link = upload_to_drive(drive_service, image_bytes, filename)
                            
                            # C. Log to Google Sheets
                            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            row_data = [
                                timestamp, 
                                sender_phone, 
                                user["project"], 
                                user["site"], 
                                ai_data["specific_area"], 
                                ai_data["fault_description"], 
                                drive_link, 
                                "Pending Review"
                            ]
                            log_to_sheet(row_data)
                            
                            # D. Send Clean Formatted Reply
                            reply_msg = (
                                f"✅ *Report Logged!*\n\n"
                                f"🏢 *Project:* {user['project']}\n"
                                f"📍 *Site:* {user['site']}\n"
                                f"🚪 *Area:* {ai_data['specific_area']}\n"
                                f"⚠️ *Fault:* {ai_data['fault_description']}\n\n"
                                f"📸 *Photo Link:*\n{drive_link}"
                            )
                            send_whatsapp_message(sender_phone, reply_msg)

        return {"status": "success"}

    except Exception as e:
        print(f"❌ Error handling payload: {e}")
        return {"status": "error"}

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)