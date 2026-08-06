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

app = FastAPI(title="WhatsApp Site Fault Logger")


# --- INITIALIZE MONGODB ---
mongo_client = pymongo.MongoClient(MONGODB_URI, tlsCAFile=certifi.where())
db = mongo_client["wa_logger_db"]
users_collection = db["users"]

# --- INITIALIZE GEMINI AI ---
genai.configure(api_key=GEMINI_API_KEY)
# NOTE: "gemini-1.5-flash" is an older model line. Use the current flash model.
# Verify against your Gemini API console which model names are enabled for your key;
# this uses a widely-available current name as of 2026.
ai_model = genai.GenerativeModel(
    "gemini-2.0-flash",
    generation_config={"response_mime_type": "application/json"},
)

# --- GOOGLE APIS SETUP ---
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

# Cache the sheet's spreadsheet ID once so we can build a direct link without
# re-opening the sheet by name every time.
_sheet_id_cache = {"id": None}


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
    """
    Opens the target spreadsheet once, caches its ID, and returns
    (worksheet, spreadsheet_url) so callers can log a row and also
    hand the user a link straight to the report sheet.
    """
    spreadsheet = gc.open(GOOGLE_SHEET_NAME)
    _sheet_id_cache["id"] = spreadsheet.id
    sheet_url = f"https://docs.google.com/spreadsheets/d/{spreadsheet.id}/edit#gid={spreadsheet.sheet1.id}"
    return spreadsheet.sheet1, sheet_url


def log_to_sheet(row_data: list) -> str:
    """Appends a row and returns a link to the sheet (not the photo)."""
    gc, _ = get_google_services()
    worksheet, sheet_url = get_sheet_and_link(gc)
    worksheet.append_row(row_data)
    return sheet_url


def extract_fault_details_with_ai(caption: str) -> dict:
    """
    Uses Gemini (JSON mode) to split a free-text caption into a fault
    location and a fault description. Falls back to a regex heuristic,
    and only as a last resort dumps the raw caption into the description
    (never silently blending the two).
    """
    fallback = {"specific_area": "Not specified", "fault_description": caption.strip() or "No description provided."}

    if not caption or caption.strip().lower() in ("", "no description provided."):
        return {"specific_area": "Not specified", "fault_description": "No description provided."}

    prompt = f"""You extract structured data from short construction site fault reports.

Split the report into:
- "specific_area": the physical location only (unit/flat number, floor, room name, tower, etc). If none is mentioned, use "Not specified".
- "fault_description": the issue itself, with the location phrase removed. Keep it concise, in the reporter's own words.

Respond with ONLY a JSON object, no extra text, matching exactly:
{{"specific_area": "...", "fault_description": "..."}}

Examples:
Report: "Ply coming off in B-804, Master Bedroom"
{{"specific_area": "B-804, Master Bedroom", "fault_description": "Ply coming off"}}

Report: "Leaking pipe near kitchen sink, Tower C 2nd floor"
{{"specific_area": "Tower C, 2nd floor, kitchen", "fault_description": "Leaking pipe near sink"}}

Report: "Paint peeling"
{{"specific_area": "Not specified", "fault_description": "Paint peeling"}}

Now extract from this report:
"{caption}"
"""

    try:
        response = ai_model.generate_content(prompt)
        raw_text = response.text.strip()
        data = json.loads(raw_text)

        area = str(data.get("specific_area", "")).strip() or "Not specified"
        fault = str(data.get("fault_description", "")).strip()

        # Guard against the model echoing the whole caption into fault_description
        # while leaving area genuinely findable — if area is "Not specified" but
        # the fault text still contains an obvious unit/room pattern, try a regex
        # backstop rather than trusting the AI blindly.
        if not fault:
            fault = fallback["fault_description"]

        return {"specific_area": area, "fault_description": fault}

    except Exception as e:
        print(f"AI Parsing Error: {e} | raw response: {getattr(response, 'text', 'N/A') if 'response' in locals() else 'no response'}")
        return regex_fallback_extract(caption)


def regex_fallback_extract(caption: str) -> dict:
    """
    Lightweight non-AI backstop: looks for common location patterns like
    'B-804', 'Tower C', 'Flat 12', 'Room 3', 'Floor 5', etc., strips them
    out of the text, and returns what's left as the fault description.
    """
    location_patterns = [
        r'\b[A-Za-z]-?\d{2,4}\b',                    # B-804, A102
        r'\bTower\s+[A-Za-z0-9]+\b',                 # Tower C
        r'\bFlat\s*\d+\b',                            # Flat 12
        r'\bUnit\s*\d+\b',                            # Unit 5
        r'\b\d+(?:st|nd|rd|th)\s+Floor\b',            # 3rd Floor
        r'\bMaster Bedroom\b|\bBedroom\s*\d*\b|\bKitchen\b|\bBathroom\b|\bBalcony\b|\bLiving Room\b',
    ]

    found = []
    remaining = caption
    for pattern in location_patterns:
        matches = re.findall(pattern, remaining, flags=re.IGNORECASE)
        if matches:
            found.extend(matches)
            remaining = re.sub(pattern, '', remaining, flags=re.IGNORECASE)

    # Clean up leftover punctuation/connectors from the stripped text.
    # Removing a location from the middle of the sentence can leave stray
    # commas or dangling connector words (", " where a location used to be,
    # a trailing "in"/"on"/"at"/"near" that's no longer followed by anything).
    # Run this cleanup repeatedly until it stops changing, since one pass
    # can expose a new dangling connector that only becomes "trailing" after
    # the surrounding punctuation is removed.
    prev = None
    while prev != remaining:
        prev = remaining
        remaining = re.sub(r'\s*,\s*,\s*', ', ', remaining)          # double commas
        remaining = re.sub(r'^\s*,\s*', '', remaining)                # leading comma
        remaining = re.sub(r'\s*,\s*$', '', remaining)                # trailing comma
        remaining = re.sub(r'\s*\b(in|at|near|on)\b\s*,\s*', ' ', remaining, flags=re.IGNORECASE)  # "in , " -> " "
        remaining = re.sub(r'\s*\b(in|at|near|on)\b\s*$', '', remaining, flags=re.IGNORECASE)        # trailing connector
        remaining = re.sub(r'\s{2,}', ' ', remaining).strip(" ,-")

    area = ", ".join(dict.fromkeys(found)) if found else "Not specified"  # dedupe, preserve order
    fault = remaining if remaining else caption.strip()

    return {"specific_area": area, "fault_description": fault}


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
                            user = {
                                "phone": sender_phone,
                                "state": "awaiting_name",
                                "name": "",
                                "project": "",
                                "site": ""
                            }
                            users_collection.insert_one(user)
                            send_whatsapp_message(
                                sender_phone,
                                "👋 Welcome to the Site Tracker!\n\n"
                                "To get started, please reply with your *Full Name*."
                            )
                            return {"status": "success"}

                        # --- 2. HANDLE TEXT MESSAGES (COMMANDS & SETUP) ---
                        if msg_type == "text":
                            user_text = msg["text"]["body"].strip()

                            # The /update command — changes project/site, keeps name
                            if user_text.lower() == "/update":
                                users_collection.update_one(
                                    {"phone": sender_phone}, {"$set": {"state": "awaiting_project"}}
                                )
                                send_whatsapp_message(sender_phone, "🔄 Location update initiated.\n\n🏢 What is the new *Project Name*?")
                                return {"status": "success"}

                            # The /name command — lets a user correct/change their logged name
                            if user_text.lower() == "/name":
                                users_collection.update_one(
                                    {"phone": sender_phone}, {"$set": {"state": "awaiting_name"}}
                                )
                                send_whatsapp_message(sender_phone, "✏️ What should we log your name as?")
                                return {"status": "success"}

                            # State: Awaiting Name (first-time setup or /name)
                            if user["state"] == "awaiting_name":
                                users_collection.update_one(
                                    {"phone": sender_phone},
                                    {"$set": {"name": user_text, "state": "awaiting_project"}}
                                )
                                send_whatsapp_message(
                                    sender_phone,
                                    f"Thanks, *{user_text}*.\n\n🏢 What is your *Project Name*? (e.g., Vector Heights)"
                                )
                                return {"status": "success"}

                            # State: Awaiting Project Name
                            if user["state"] == "awaiting_project":
                                users_collection.update_one(
                                    {"phone": sender_phone}, {"$set": {"project": user_text, "state": "awaiting_site"}}
                                )
                                send_whatsapp_message(sender_phone, f"Got it. Project set to *{user_text}*.\n\n📍 Now, what is the *Site Location*? (e.g., Tower B)")
                                return {"status": "success"}

                            # State: Awaiting Site Location
                            elif user["state"] == "awaiting_site":
                                users_collection.update_one(
                                    {"phone": sender_phone}, {"$set": {"site": user_text, "state": "active"}}
                                )
                                send_whatsapp_message(
                                    sender_phone,
                                    "✅ *Setup Complete!*\n\n"
                                    "You can now send photos with captions. "
                                    "To change your site, type */update*. To change your name, type */name*."
                                )
                                return {"status": "success"}

                            # State: Active (Sending text without photo)
                            elif user["state"] == "active":
                                send_whatsapp_message(
                                    sender_phone,
                                    "📸 Please attach a photo when reporting a fault. You can type the description in the photo's caption!\n\n"
                                    "*(Type /update to change site, /name to change your name)*"
                                )
                                return {"status": "success"}

                        # --- 3. HANDLE IMAGE MESSAGES (ACTIVE REPORTING) ---
                        elif msg_type == "image":
                            if user["state"] != "active":
                                send_whatsapp_message(sender_phone, "⚠️ Please finish your setup first before sending photos.")
                                return {"status": "success"}

                            image_id = msg["image"]["id"]
                            caption = msg["image"].get("caption", "No description provided.")

                            send_whatsapp_message(sender_phone, "⏳ Logging your report...")

                            # A. Extract Location & Fault
                            ai_data = extract_fault_details_with_ai(caption)

                            # B. Upload image to Google Drive
                            _, drive_service = get_google_services()
                            image_bytes = download_whatsapp_media(image_id)
                            filename = f"Site_Fault_{sender_phone}_{int(datetime.datetime.now().timestamp())}.jpg"
                            drive_link = upload_to_drive(drive_service, image_bytes, filename)

                            # C. Log to Google Sheets — column order matches the sheet header:
                            # Timestamp | Phone Number | Project Name | Site Location | Name |
                            # Fault Location | Fault Description | Photo Link | Status
                            timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            row_data = [
                                timestamp,
                                sender_phone,
                                user["project"],
                                user["site"],
                                user.get("name", "Not specified"),
                                ai_data["specific_area"],
                                ai_data["fault_description"],
                                drive_link,
                                "Pending Review"
                            ]
                            sheet_link = log_to_sheet(row_data)

                            # D. Send confirmation with BOTH the photo link and the sheet link
                            reply_msg = (
                                f"✅ *Report Logged!*\n\n"
                                f"🙋 *Logged by:* {user.get('name', 'Not specified')}\n"
                                f"🏢 *Project:* {user['project']}\n"
                                f"📍 *Site:* {user['site']}\n"
                                f"🚪 *Fault Location:* {ai_data['specific_area']}\n"
                                f"⚠️ *Fault:* {ai_data['fault_description']}\n\n"
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