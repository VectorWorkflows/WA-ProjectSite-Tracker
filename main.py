import os
import io
import re
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

# Create a list of admin phone numbers from the comma-separated string
ADMIN_NUMBERS = [num.strip() for num in os.getenv("ADMIN_NUMBERS", "").split(",") if num.strip()]

app = FastAPI(title="WhatsApp Site Fault Logger")

# --- INITIALIZE MONGODB ---
mongo_client = pymongo.MongoClient(MONGODB_URI, tlsCAFile=certifi.where())
db = mongo_client["wa_logger_db"]
users_collection = db["users"]
engineers_collection = db["engineers"] 

# --- INITIALIZE GEMINI AI ---
genai.configure(api_key=GEMINI_API_KEY)
ai_model = genai.GenerativeModel(
    "gemini-2.0-flash",
    generation_config={
        "response_mime_type": "application/json",
        "temperature": 0,
    },
)

# --- GOOGLE APIS SETUP ---
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

_sheet_id_cache = {"id": None}

def get_google_services():
    creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    gc = gspread.authorize(creds)
    drive_service = build("drive", "v3", credentials=creds)
    return gc, drive_service

def display_name(user: dict) -> str:
    name = (user.get("name") or "").strip()
    return name if name else "Not specified"

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
    resp = requests.post(url, headers=headers, json=payload)
    if resp.status_code >= 300:
        print(f"⚠️ WhatsApp send failed ({resp.status_code}): {resp.text}")

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

def get_sheet_and_link(gc):
    spreadsheet = gc.open(GOOGLE_SHEET_NAME)
    _sheet_id_cache["id"] = spreadsheet.id
    sheet_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet.id}/edit#gid={spreadsheet.sheet1.id}"
    return spreadsheet.sheet1, sheet_url

def log_to_sheet(row_data: list) -> str:
    gc, _ = get_google_services()
    worksheet, sheet_url = get_sheet_and_link(gc)
    worksheet.append_row(row_data)
    return sheet_url

# --- LOCATION / FAULT PATTERNS ---
LOCATION_PATTERNS = [
    r'\b[A-Za-z0-9]{1,6}-\d{2,5}(?:\s+(?:Main|Annex|Extension|Phase\s*\d+))?\b',
    r'\bTower\s+[A-Za-z0-9]+\b',
    r'\bBlock\s+[A-Za-z0-9]+\b',
    r'\bWing\s+[A-Za-z0-9]+\b',
    r'\bFlat\s*\d+\b',
    r'\bUnit\s*\d+\b',
    r'\b\d+(?:st|nd|rd|th)\s+Floor\b',
    r'\bGround\s+Floor\b',
    r'\bMaster\s+Bedroom\b|\bBedroom\s*\d*\b|\bKitchen\b|\bBathroom\b|'
    r'\bBalcony\b|\bLiving\s+Room\b|\bDining\s+Room\b|\bTerrace\b|\bLobby\b|'
    r'\bStaircase\b|\bCorridor\b|\bParking\b|\bBasement\b',
]

def strip_known_locations(text: str):
    found = []
    remaining = text
    for pattern in LOCATION_PATTERNS:
        matches = re.findall(pattern, remaining, flags=re.IGNORECASE)
        if matches:
            found.extend(matches)
            remaining = re.sub(pattern, '', remaining, flags=re.IGNORECASE)

    prev = None
    while prev != remaining:
        prev = remaining
        remaining = re.sub(r'\s*,\s*,\s*', ', ', remaining)
        remaining = re.sub(r'^\s*,\s*', '', remaining)
        remaining = re.sub(r'\s*,\s*$', '', remaining)
        remaining = re.sub(r'\s*\b(in|at|near|on)\b\s*,\s*', ' ', remaining, flags=re.IGNORECASE)
        remaining = re.sub(r'\s*\b(in|at|near|on)\b\s*$', '', remaining, flags=re.IGNORECASE)
        remaining = re.sub(r'\s{2,}', ' ', remaining).strip(" ,-")

    return found, remaining

def regex_fallback_extract(caption: str) -> dict:
    found, remaining = strip_known_locations(caption)
    area = ", ".join(dict.fromkeys(found)) if found else "Not specified"
    fault = remaining if remaining else caption.strip()
    return {"specific_area": area, "fault_description": fault}

def extract_fault_details_with_ai(caption: str) -> dict:
    if not caption or caption.strip().lower() in ("", "no description provided."):
        return {"specific_area": "Not specified", "fault_description": "No description provided."}

    prompt = f"""You are a strict data-extraction engine for construction site fault reports. Split ONE short caption into exactly two fields.

Return ONLY a JSON object:
{{"specific_area": "...", "fault_description": "..."}}

DEFINITIONS:
"specific_area": All physical location tokens, including:
  - Complex alphanumeric flat/unit/tower codes: "H2i-1107", "B2-506", "T5-1204", "A102", "Flat 402"
  - Tower/Block/Wing names: "Tower H2i", "Block B", "Wing A"
  - Rooms/Areas: "Kitchen", "Master Bedroom", "Bathroom", "Balcony", "Living Room", "Corridor", "Lobby"
  - Floors: "11th Floor", "Ground Floor", "3rd Floor"

"fault_description": ONLY the defect/damage/issue (e.g., "Wiring and ply coming off", "Pipe leaking"). NEVER contain location/unit codes or room names.

EXAMPLES:
Caption: "Wiring and ply coming off in H2i-1107 Kitchen."
{{"specific_area": "H2i-1107, Kitchen", "fault_description": "Wiring and ply coming off"}}

Caption: "B2-506 Main, Master Bedroom, ply coming off near window"
{{"specific_area": "B2-506 Main, Master Bedroom", "fault_description": "Ply coming off near window"}}

Caption: "Leaking pipe near kitchen sink, Tower C 2nd floor"
{{"specific_area": "Tower C, 2nd floor, Kitchen", "fault_description": "Leaking pipe near sink"}}

Analyze this report:
"{caption}"
"""

    try:
        response = ai_model.generate_content(prompt)
        raw_text = response.text.strip()
        raw_text = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw_text.strip())
        data = json.loads(raw_text)

        area = str(data.get("specific_area", "")).strip() or "Not specified"
        fault = str(data.get("fault_description", "")).strip() or caption.strip()

        # Backstop pass
        leftover_locations, cleaned_fault = strip_known_locations(fault)
        if leftover_locations:
            combined = [area] if area and area != "Not specified" else []
            combined.extend(leftover_locations)
            area = ", ".join(dict.fromkeys(combined))
            fault = cleaned_fault if cleaned_fault else fault

        return {"specific_area": area, "fault_description": fault}

    except Exception as e:
        print(f"AI Parsing Error: {e}")
        return regex_fallback_extract(caption)

# --- PROFILE HELPER ---
REQUIRED_FIELDS = [
    ("name", "awaiting_name", "✏️ What is your *Full Name*?"),
    ("project", "awaiting_project", "🏢 What is your *Project Name*? (e.g., Vector Heights)"),
    ("site", "awaiting_site", "📍 What is the *Site Location*? (e.g., Tower B)"),
]

def start_missing_field_collection(sender_phone: str, user: dict) -> bool:
    for field, wait_state, prompt in REQUIRED_FIELDS:
        if not (user.get(field) or "").strip():
            users_collection.update_one(
                {"phone": sender_phone},
                {"$set": {"state": wait_state, "return_state": "active"}}
            )
            send_whatsapp_message(sender_phone, prompt)
            return True
    return False

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

                        # Check Engineer Registry for auto-name setup
                        registered_engineer = engineers_collection.find_one({"phone": sender_phone})
                        auto_name = registered_engineer.get("name", "") if registered_engineer else ""

                        # Fetch or Create User
                        user = users_collection.find_one({"phone": sender_phone})
                        if not user:
                            initial_state = "awaiting_project" if auto_name else "awaiting_name"
                            user = {
                                "phone": sender_phone,
                                "state": initial_state,
                                "return_state": None,
                                "name": auto_name,
                                "project": "",
                                "site": ""
                            }
                            users_collection.insert_one(user)

                            if auto_name:
                                send_whatsapp_message(
                                    sender_phone,
                                    f"👋 Welcome back, *{auto_name}*!\n\n🏢 Please reply with your *Project Name* (e.g., Vector Heights)."
                                )
                            else:
                                send_whatsapp_message(
                                    sender_phone,
                                    "👋 Welcome to the Site Tracker!\n\nTo get started, please reply with your *Full Name*."
                                )
                            return {"status": "success"}

                        # --- HANDLE COMMANDS & TEXT MESSAGES ---
                        if msg_type == "text":
                            user_text = msg["text"]["body"].strip()
                            cmd = user_text.lower()

                            # --- NEW: ADMIN /ADD_USER COMMAND ---
                            if cmd.startswith("/add_user"):
                                if sender_phone not in ADMIN_NUMBERS:
                                    send_whatsapp_message(sender_phone, "⛔ You do not have permission to use admin commands.")
                                    return {"status": "success"}

                                # Parse the command: /add_user 919876543210 John Doe
                                parts = user_text.split(" ", 2)
                                if len(parts) < 3:
                                    send_whatsapp_message(sender_phone, "⚠️ Invalid format.\nUse: */add_user [Phone] [Name]*\nExample: `/add_user 919876543210 John Doe`")
                                    return {"status": "success"}

                                new_phone = parts[1].strip()
                                new_name = parts[2].strip()

                                # Add/update in engineers database
                                engineers_collection.update_one(
                                    {"phone": new_phone},
                                    {"$set": {"name": new_name}},
                                    upsert=True
                                )
                                
                                # If they already texted the bot before being added, update their active profile too
                                users_collection.update_one(
                                    {"phone": new_phone},
                                    {"$set": {"name": new_name}}
                                )

                                send_whatsapp_message(sender_phone, f"✅ *{new_name}* ({new_phone}) has been registered to the engineer database.")
                                return {"status": "success"}
                            # ------------------------------------

                            # Command: /reset or /start
                            if cmd in ("/reset", "/start"):
                                new_state = "awaiting_project" if auto_name else "awaiting_name"
                                users_collection.update_one(
                                    {"phone": sender_phone},
                                    {"$set": {"state": new_state, "return_state": None, "name": auto_name, "project": "", "site": ""}}
                                )
                                send_whatsapp_message(
                                    sender_phone,
                                    "🧹 *Session Reset Complete.*\n\n" + 
                                    (f"Welcome, *{auto_name}*! 🏢 What is your *Project Name*?" if auto_name else "✏️ What is your *Full Name*?")
                                )
                                return {"status": "success"}

                            # Command: /status
                            if cmd == "/status":
                                send_whatsapp_message(
                                    sender_phone,
                                    f"📋 *Current Profile Status:*\n\n"
                                    f"👤 *Name:* {display_name(user)}\n"
                                    f"🏢 *Project:* {user.get('project') or 'Not set'}\n"
                                    f"📍 *Site:* {user.get('site') or 'Not set'}\n"
                                    f"🔄 *Status:* {user.get('state')}\n\n"
                                    f"Commands: /update (Change Site), /name (Change Name), /reset (Start Over)"
                                )
                                return {"status": "success"}

                            # Command: /help
                            if cmd == "/help":
                                help_msg = (
                                    "🛠 *Available Commands:*\n\n"
                                    "• */status* - Show your active profile settings\n"
                                    "• */update* - Change your Project & Site Location\n"
                                    "• */name* - Update your registered Name\n"
                                    "• */reset* or */start* - Clear session and start setup over\n"
                                    "• Send a *Photo + Caption* to log a fault report"
                                )
                                if sender_phone in ADMIN_NUMBERS:
                                    help_msg += "\n\n👑 *Admin Commands:*\n• */add_user [Phone] [Name]* - Register a new engineer"
                                    
                                send_whatsapp_message(sender_phone, help_msg)
                                return {"status": "success"}

                            # Command: /update
                            if cmd == "/update":
                                users_collection.update_one(
                                    {"phone": sender_phone},
                                    {"$set": {"state": "awaiting_project", "return_state": None}}
                                )
                                send_whatsapp_message(sender_phone, "🔄 Location update initiated.\n\n🏢 What is the new *Project Name*?")
                                return {"status": "success"}

                            # Command: /name
                            if cmd == "/name":
                                users_collection.update_one(
                                    {"phone": sender_phone},
                                    {"$set": {"state": "awaiting_name", "return_state": "active"}}
                                )
                                send_whatsapp_message(sender_phone, "✏️ What should we log your name as?")
                                return {"status": "success"}

                            # Setup States
                            if user["state"] == "awaiting_name":
                                return_state = user.get("return_state")
                                if return_state == "active":
                                    users_collection.update_one(
                                        {"phone": sender_phone},
                                        {"$set": {"name": user_text, "state": "active", "return_state": None}}
                                    )
                                    send_whatsapp_message(sender_phone, f"✅ Name updated to *{user_text}*.")
                                else:
                                    users_collection.update_one(
                                        {"phone": sender_phone},
                                        {"$set": {"name": user_text, "state": "awaiting_project"}}
                                    )
                                    send_whatsapp_message(sender_phone, f"Thanks, *{user_text}*.\n\n🏢 What is your *Project Name*?")
                                return {"status": "success"}

                            if user["state"] == "awaiting_project":
                                return_state = user.get("return_state")
                                if return_state == "active":
                                    users_collection.update_one(
                                        {"phone": sender_phone},
                                        {"$set": {"project": user_text, "state": "active", "return_state": None}}
                                    )
                                    send_whatsapp_message(sender_phone, f"✅ Project updated to *{user_text}*.")
                                else:
                                    users_collection.update_one(
                                        {"phone": sender_phone},
                                        {"$set": {"project": user_text, "state": "awaiting_site"}}
                                    )
                                    send_whatsapp_message(sender_phone, f"Got it. Project: *{user_text}*.\n\n📍 What is the *Site Location*? (e.g., Tower B)")
                                return {"status": "success"}

                            elif user["state"] == "awaiting_site":
                                users_collection.update_one(
                                    {"phone": sender_phone},
                                    {"$set": {"site": user_text, "state": "active", "return_state": None}}
                                )
                                send_whatsapp_message(
                                    sender_phone,
                                    "✅ *Setup Complete!*\n\n"
                                    "Send any photo with a caption to log a fault report.\n"
                                    "Type */help* for a list of commands."
                                )
                                return {"status": "success"}

                            elif user["state"] == "active":
                                if start_missing_field_collection(sender_phone, user):
                                    return {"status": "success"}

                                send_whatsapp_message(
                                    sender_phone,
                                    "📸 Please attach a photo when reporting a fault. Type the fault description in the photo caption!\n\n"
                                    "*(Type /help to see commands)*"
                                )
                                return {"status": "success"}

                        # --- HANDLE IMAGES ---
                        elif msg_type == "image":
                            if user["state"] != "active":
                                send_whatsapp_message(sender_phone, "⚠️ Please complete setup first before sending photos.")
                                return {"status": "success"}

                            if start_missing_field_collection(sender_phone, user):
                                send_whatsapp_message(sender_phone, "Please resend the photo once you've replied above.")
                                return {"status": "success"}

                            image_id = msg["image"]["id"]
                            caption = msg["image"].get("caption", "No description provided.")

                            send_whatsapp_message(sender_phone, "⏳ Processing report...")

                            ai_data = extract_fault_details_with_ai(caption)

                            _, drive_service = get_google_services()
                            image_bytes = download_whatsapp_media(image_id)
                            filename = f"Site_Fault_{sender_phone}_{int(datetime.datetime.now().timestamp())}.jpg"
                            drive_link = upload_to_drive(drive_service, image_bytes, filename)

                            # Column order matches Google Sheet headers:
                            # Timestamp | Phone Number | Project Name | Site Location | Name | Fault Location | Fault Description | Photo Link | Status
                            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            row_data = [
                                timestamp,
                                sender_phone,
                                user["project"],
                                user["site"],
                                display_name(user),
                                ai_data["specific_area"],
                                ai_data["fault_description"],
                                drive_link,
                                "Pending Review"
                            ]
                            sheet_link = log_to_sheet(row_data)

                            reply_msg = (
                                f"✅ *Report Logged!*\n\n"
                                f"👤 *Logged by:* {display_name(user)}\n"
                                f"🏢 *Project:* {user['project']}\n"
                                f"📍 *Site:* {user['site']}\n"
                                f"🚪 *Fault Location:* {ai_data['specific_area']}\n"
                                f"⚠️ *Fault Description:* {ai_data['fault_description']}\n\n"
                                f"📸 *Photo:* {drive_link}\n"
                                f"📋 *Report Sheet:* {sheet_link}"
                            )
                            send_whatsapp_message(sender_phone, reply_msg)

        return {"status": "success"}

    except Exception as e:
        print(f"❌ Error handling payload: {e}")
        return {"status": "error"}

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)