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
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

load_dotenv()

import database          # noqa: E402  (local module, imported after load_dotenv)
import sheets_service   # noqa: E402  (local module, imported after load_dotenv)

# --- ENVIRONMENT VARIABLES ---
VERIFY_TOKEN = os.getenv("VERIFY_TOKEN", "vector_secret_2026")
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")
GOOGLE_SHEET_NAME = os.getenv("GOOGLE_SHEET_NAME", "Site Fault Reports")
DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID")  # legacy; photo uploads now use sheets_service.PHOTOS_FOLDER_ID
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
    # Use PHOTOS_FOLDER_ID from sheets_service so all fault images land in the Photos folder.
    photos_folder = sheets_service.PHOTOS_FOLDER_ID or DRIVE_FOLDER_ID
    file_metadata = {
        "name": filename,
        "parents": [photos_folder] if photos_folder else []
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

# ---------------------------------------------------------------------------
# Multi-Tenant: payload parser & background worker
# ---------------------------------------------------------------------------

def parse_whatsapp_payload(data: dict) -> tuple[str | None, str | None, str]:
    """Extract (phone_number, image_id, caption) from a raw Meta webhook payload.

    Returns (None, None, "") when the payload contains no image message.
    """
    try:
        entries = data.get("entry", [])
        for entry in entries:
            for change in entry.get("changes", []):
                value = change.get("value", {})
                if "messages" not in value:
                    continue
                msg = value["messages"][0]
                if msg.get("type") != "image":
                    continue
                phone_number = msg["from"]
                image_id     = msg["image"]["id"]
                caption      = msg["image"].get("caption", "No description provided.")
                return phone_number, image_id, caption
    except Exception as exc:
        print(f"[parse_whatsapp_payload] ⚠️ Parse error: {exc}")
    return None, None, ""


def extract_audit_fields_with_ai(caption: str, image_bytes: bytes) -> dict:
    """Call Gemini Vision AI to extract structured audit fields from an image.

    Returns a dict with keys: floor, room, fault_description, severity.
    Falls back to sensible defaults on any error.
    """
    import google.generativeai as genai

    prompt = """You are a construction-site safety audit AI.
Analyse the provided site photograph together with the caption below.

Return ONLY a valid JSON object with exactly these four keys:
{
  "floor": "...",
  "room": "...",
  "fault_description": "...",
  "severity": "Low | Medium | High | Critical"
}

Rules:
- "floor": The floor number / name (e.g. "Ground Floor", "3rd Floor"). Use "Not specified" if unknown.
- "room": The room or area (e.g. "Kitchen", "Master Bedroom", "Lobby"). Use "Not specified" if unknown.
- "fault_description": A concise one-sentence description of the defect visible in the photo.
- "severity": Choose the single most appropriate label — Low, Medium, High, or Critical.

Do NOT include any location codes, unit numbers, or markdown fences in your response.

Caption: "{caption}"
""".format(caption=caption)

    try:
        import PIL.Image
        import io as _io

        image_part = {"mime_type": "image/jpeg", "data": image_bytes}
        response   = ai_model.generate_content([prompt, image_part])

        # 1. Get raw text from Gemini response
        raw_text = response.text.strip() if hasattr(response, "text") else str(response)

        # 2. Strip leading/trailing markdown code fences (```json ... ```)
        raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text, flags=re.IGNORECASE)
        raw_text = re.sub(r"\s*```$", "", raw_text)
        raw_text = raw_text.strip()

        # 3. Parse JSON safely with fallback dictionary
        try:
            data = json.loads(raw_text)
        except Exception as json_exc:
            print(f"[Gemini] JSON parsing failed: {json_exc}. Raw text was:\n{raw_text}")
            data = {}

        # 4. Extract fields safely using .get() with defaults
        floor             = data.get("floor", "N/A")
        room              = data.get("room", "N/A")
        fault_description = data.get("fault_description", data.get("fault", "Site Fault Reported"))
        severity          = data.get("severity", "Medium")

        return {
            "floor":             str(floor).strip()             or "Not specified",
            "room":              str(room).strip()              or "Not specified",
            "fault_description": str(fault_description).strip() or caption.strip(),
            "severity":          str(severity).strip()          or "Medium",
        }
    except Exception as exc:
        print(f"[extract_audit_fields_with_ai] ⚠️ AI error: {exc}")
        return {
            "floor":             "Not specified",
            "room":              "Not specified",
            "fault_description": caption.strip() or "No description provided.",
            "severity":          "Medium",
        }


def process_site_report_job(
    phone_number: str,
    image_id: str,
    caption: str,
) -> None:
    """Background worker — runs after the webhook has already returned 200.

    Steps:
      1. Get or create the caller's personal Google Sheet.
      2. Download the site image from Meta's Graph API.
      3. Call Gemini Vision AI (audit schema).
      4. Upload image to Google Drive.
      5. Append structured row to the caller's sheet.
      6. Reply to the caller on WhatsApp.
    """
    print(f"[background] 🚀 Starting job for {phone_number}")

    try:
        # ── 1. Provision sheet ──────────────────────────────────────────
        sheet_id, sheet_url = sheets_service.get_or_create_user_sheet(phone_number)

        # ── 2. Download image ───────────────────────────────────────────
        image_bytes = download_whatsapp_media(image_id)
        print(f"[background] 📷 Image downloaded ({len(image_bytes)} bytes)")

        # ── 3. Vision AI — extract audit fields ─────────────────────────
        # Safe defaults — used as-is if Gemini call or JSON parsing fails.
        user_caption     = caption.strip() if caption and caption.strip() else "Site Fault Logged"
        floor            = "N/A"
        room             = "N/A"
        fault            = user_caption
        severity         = "Medium"

        try:
            audit = extract_audit_fields_with_ai(caption, image_bytes)

            # Overwrite defaults only when the helper returned real values
            floor    = audit.get("floor")    or floor
            room     = audit.get("room")     or room
            fault    = audit.get("fault_description") or audit.get("fault") or fault
            severity = audit.get("severity") or severity

        except Exception as parse_error:
            print(f"[background] ⚠️ Gemini JSON parsing skipped/failed: {parse_error}. Using fallback defaults.")

        # Ensure all fields are populated strings before writing to the sheet
        row_data = {
            "floor":             str(floor),
            "room":              str(room),
            "fault_description": str(fault),
            "severity":          str(severity),
        }

        # ── 4. Upload image to Google Drive ─────────────────────────────
        _, drive_service = get_google_services()
        filename   = f"Site_Fault_{phone_number}_{int(datetime.datetime.now().timestamp())}.jpg"
        drive_link = upload_to_drive(drive_service, image_bytes, filename)
        print(f"[background] ☁️  Drive upload: {drive_link}")

        # ── 5. Append audit row to the user's sheet ──────────────────────
        timestamp  = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = [
            timestamp,
            row_data["floor"],
            row_data["room"],
            row_data["fault_description"],
            row_data["severity"],
            drive_link,
        ]
        sheets_service.append_audit_row(sheet_id, row)

        # ── 6. WhatsApp confirmation ─────────────────────────────────────
        reply = (
            "✅ *Site Fault Logged Successfully!*\n\n"
            "📋 *Extracted Details:*\n"
            f"• *Floor:* {row_data['floor']}\n"
            f"• *Room:* {row_data['room']}\n"
            f"• *Fault:* {row_data['fault_description']}\n"
            f"• *Severity:* {row_data['severity']}\n\n"
            f"🔗 *View your live dynamic log sheet here:*\n{sheet_url}"
        )
        send_whatsapp_message(phone_number, reply)
        print(f"[background] ✅ Job completed for {phone_number}")

    except Exception as exc:
        print(f"[background] ❌ Job failed for {phone_number}: {exc}")
        try:
            send_whatsapp_message(
                phone_number,
                "⚠️ We encountered an issue processing your report. Please try again."
            )
        except Exception:
            pass


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
async def whatsapp_webhook(request: Request, background_tasks: BackgroundTasks):
    """WhatsApp POST webhook — returns 200 in < 100 ms.

    Heavy operations (image download, Vision AI, Drive upload, Sheet logging,
    WhatsApp reply) are delegated to a FastAPI BackgroundTask so that Meta's
    5-second HTTP timeout is never breached.

    Text messages and state-machine commands are still handled synchronously
    here because they consist only of fast MongoDB reads/writes.
    """
    try:
        body = await request.json()

        if body.get("object") != "whatsapp_business_account":
            return {"status": "ok"}

        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})

                if "messages" not in value:
                    continue

                msg          = value["messages"][0]
                sender_phone = msg["from"]
                msg_type     = msg["type"]

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
                    return {"status": "ok"}

                # --- HANDLE COMMANDS & TEXT MESSAGES (synchronous — fast DB ops only) ---
                if msg_type == "text":
                    user_text = msg["text"]["body"].strip()
                    cmd = user_text.lower()

                    # --- ADMIN /ADD_USER COMMAND ---
                    if cmd.startswith("/add_user"):
                        if sender_phone not in ADMIN_NUMBERS:
                            send_whatsapp_message(sender_phone, "⛔ You do not have permission to use admin commands.")
                            return {"status": "ok"}

                        parts = user_text.split(" ", 2)
                        if len(parts) < 3:
                            send_whatsapp_message(sender_phone, "⚠️ Invalid format.\nUse: */add_user [Phone] [Name]*\nExample: `/add_user 919876543210 John Doe`")
                            return {"status": "ok"}

                        new_phone = parts[1].strip()
                        new_name  = parts[2].strip()

                        engineers_collection.update_one(
                            {"phone": new_phone},
                            {"$set": {"name": new_name}},
                            upsert=True
                        )
                        users_collection.update_one(
                            {"phone": new_phone},
                            {"$set": {"name": new_name}}
                        )
                        send_whatsapp_message(sender_phone, f"✅ *{new_name}* ({new_phone}) has been registered to the engineer database.")
                        return {"status": "ok"}

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
                        return {"status": "ok"}

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
                        return {"status": "ok"}

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
                        return {"status": "ok"}

                    # Command: /update
                    if cmd == "/update":
                        users_collection.update_one(
                            {"phone": sender_phone},
                            {"$set": {"state": "awaiting_project", "return_state": None}}
                        )
                        send_whatsapp_message(sender_phone, "🔄 Location update initiated.\n\n🏢 What is the new *Project Name*?")
                        return {"status": "ok"}

                    # Command: /name
                    if cmd == "/name":
                        users_collection.update_one(
                            {"phone": sender_phone},
                            {"$set": {"state": "awaiting_name", "return_state": "active"}}
                        )
                        send_whatsapp_message(sender_phone, "✏️ What should we log your name as?")
                        return {"status": "ok"}

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
                        return {"status": "ok"}

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
                        return {"status": "ok"}

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
                        return {"status": "ok"}

                    elif user["state"] == "active":
                        if start_missing_field_collection(sender_phone, user):
                            return {"status": "ok"}
                        send_whatsapp_message(
                            sender_phone,
                            "📸 Please attach a photo when reporting a fault. Type the fault description in the photo caption!\n\n"
                            "*(Type /help to see commands)*"
                        )
                        return {"status": "ok"}

                # --- HANDLE IMAGES (delegated to background) ---
                elif msg_type == "image":
                    if user["state"] != "active":
                        send_whatsapp_message(sender_phone, "⚠️ Please complete setup first before sending photos.")
                        return {"status": "ok"}

                    if start_missing_field_collection(sender_phone, user):
                        send_whatsapp_message(sender_phone, "Please resend the photo once you've replied above.")
                        return {"status": "ok"}

                    image_id = msg["image"]["id"]
                    caption  = msg["image"].get("caption", "No description provided.")

                    # Acknowledge immediately so the user knows we received it
                    send_whatsapp_message(sender_phone, "⏳ *Report received!* Processing in the background — we'll send your live sheet link shortly.")

                    # Offload ALL heavy work to a background task
                    background_tasks.add_task(
                        process_site_report_job,
                        phone_number=sender_phone,
                        image_id=image_id,
                        caption=caption,
                    )

        return {"status": "ok"}

    except Exception as exc:
        print(f"❌ Webhook error: {exc}")
        return {"status": "ok"}   # Always return 200 to Meta

if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)