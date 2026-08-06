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
    generation_config={
        "response_mime_type": "application/json",
        # Deterministic output — this is a structured-extraction task, not a
        # creative one. Letting temperature float is a big part of why the
        # same kind of caption ("B2-506 Main, Master Bedroom, ...") could be
        # split inconsistently run to run.
        "temperature": 0,
    },
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


def display_name(user: dict) -> str:
    """
    Small helper so a genuinely-missing name always renders as
    'Not specified' — including for legacy user docs that predate the
    'name' field, where user['name'] may not exist at all, AND for docs
    where it exists but is an empty string. `dict.get(key, default)` only
    covers the first case; this covers both.
    """
    name = (user.get("name") or "").strip()
    return name if name else "Not specified"


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


# --- LOCATION / FAULT SPLITTING ---
#
# Shared regex vocabulary for "this token is a location, not a fault", used
# both by the pure-regex fallback AND as a backstop pass after the AI
# extraction (see extract_fault_details_with_ai). Keeping ONE list means the
# AI path and the no-AI path can never disagree about what counts as a
# location.
LOCATION_PATTERNS = [
    # B-804, B2-506, A102-style codes, optionally followed by a qualifier
    # word that's really still part of the location ("B2-506 Main",
    # "B2-506 Phase 2", "B2-506 Annex") rather than the start of the fault.
    r'\b[A-Za-z]\d?-\d{2,4}(?:\s+(?:Main|Annex|Extension|Phase\s*\d+))?\b',
    r'\bTower\s+[A-Za-z0-9]+\b',                    # Tower C
    r'\bBlock\s+[A-Za-z0-9]+\b',                    # Block B2
    r'\bWing\s+[A-Za-z0-9]+\b',                     # Wing A
    r'\bFlat\s*\d+\b',                              # Flat 12
    r'\bUnit\s*\d+\b',                              # Unit 5
    r'\b\d+(?:st|nd|rd|th)\s+Floor\b',              # 3rd Floor
    r'\bGround\s+Floor\b',                          # Ground Floor
    r'\bMaster\s+Bedroom\b|\bBedroom\s*\d*\b|\bKitchen\b|\bBathroom\b|'
    r'\bBalcony\b|\bLiving\s+Room\b|\bDining\s+Room\b|\bTerrace\b|\bLobby\b|'
    r'\bStaircase\b|\bCorridor\b|\bParking\b|\bBasement\b|\bTerrace\b',
]


def strip_known_locations(text: str):
    """
    Scans `text` for known location patterns (unit/flat codes, tower/block/
    wing names, floor references, room names, etc.), removes them, and
    cleans up whatever punctuation/connector words they leave behind.

    Returns (found_locations: list[str], remaining_text: str).

    This is used two ways:
      1. As the entire fallback path when the AI call fails outright.
      2. As a backstop AFTER a successful AI call, to catch any location
         tokens the model left behind in fault_description instead of
         moving to specific_area (this is exactly the "bedroom went to
         location, but B2-506 Main stayed in the fault text" bug).
    """
    found = []
    remaining = text
    for pattern in LOCATION_PATTERNS:
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

    return found, remaining


def regex_fallback_extract(caption: str) -> dict:
    """Pure non-AI backstop used when the Gemini call fails outright."""
    found, remaining = strip_known_locations(caption)
    area = ", ".join(dict.fromkeys(found)) if found else "Not specified"  # dedupe, preserve order
    fault = remaining if remaining else caption.strip()
    return {"specific_area": area, "fault_description": fault}


def extract_fault_details_with_ai(caption: str) -> dict:
    """
    Uses Gemini (JSON mode, temperature 0) to split a free-text caption into
    a fault location and a fault description, then runs a regex backstop
    over the result so any location fragments the model left inside
    fault_description get pulled into specific_area instead of silently
    staying put.
    """
    if not caption or caption.strip().lower() in ("", "no description provided."):
        return {"specific_area": "Not specified", "fault_description": "No description provided."}

    prompt = f"""You are a strict data-extraction engine for construction site fault reports sent by site workers over WhatsApp. You split ONE short, messy, informally-written caption into exactly two fields. You do not add commentary, do not guess facts that aren't in the text, and do not skip this step even when the caption looks simple — short captions are exactly where naive splitting goes wrong.

Return ONLY a JSON object, nothing else (no markdown fences, no preamble, no trailing text), matching exactly this shape:
{{"specific_area": "...", "fault_description": "..."}}

DEFINITIONS

"specific_area" = every phrase in the caption that answers WHERE the fault is. This includes, and you must actively scan for ALL of the following categories, even when several appear in the same caption:
  - Unit / flat / apartment codes, in any format: "B2-506", "B-804", "A102", "Flat 12", "Unit 5"
  - Tower / block / wing names: "Tower C", "Block B2", "Wing A", or a bare qualifier attached to a code like "B2-506 Main" (treat "Main" as part of the location when it modifies a tower/block/unit reference, not the fault)
  - Floor references: "3rd Floor", "Ground Floor", "2nd floor"
  - Room / area names: "Master Bedroom", "Bedroom 2", "Kitchen", "Bathroom", "Balcony", "Living Room", "Dining Room", "Terrace", "Lobby", "Staircase", "Corridor", "Parking", "Basement"

"fault_description" = the actual defect/problem/issue being reported, and ONLY that — what is broken, damaged, leaking, missing, incomplete, or otherwise wrong. It must never contain a code, tower/block/wing name, floor reference, or room name from the categories above.

CRITICAL RULES — read carefully, these are the rules that get violated most often:

1. A single caption very often contains MULTIPLE location fragments at once (e.g. a unit code AND a tower name AND a room name, in any order, sometimes separated by commas, sometimes just run together). You must collect EVERY one of them into specific_area as a single combined string, in the order they appear, comma-separated. Do NOT stop after finding the first location fragment and do NOT leave any remaining location fragment behind in fault_description.
   Example of the failure mode to avoid: caption "B2-506 Main, Master Bedroom, ply coming off near window" must NOT produce specific_area "Master Bedroom" while leaving "B2-506 Main" sitting inside fault_description. The correct specific_area is "B2-506 Main, Master Bedroom" and fault_description is "Ply coming off near window".

2. Room names (Bedroom, Kitchen, Bathroom, etc.) are ALWAYS part of specific_area, never part of fault_description, even when they appear right next to the defect word (e.g. "kitchen leaking" -> area "Kitchen", fault "Leaking"; NOT area "Not specified", fault "Kitchen leaking").

3. Unit/flat/tower codes are ALWAYS part of specific_area in full, exactly as written (don't reformat, abbreviate, or drop qualifier words next to them like "Main" or "Phase 2" when those words are clearly modifying the code/tower rather than describing the defect).

4. After you remove every location fragment, fault_description must read as a grammatically clean, standalone description of the problem — no dangling connector words like a stray leading/trailing "in", "at", "near", "on", and no orphaned commas.

5. If, and only if, no location information of any kind is present anywhere in the caption, use "Not specified" for specific_area — but double-check the whole caption first; do not default to "Not specified" just because the location isn't at the start of the sentence.

6. Keep fault_description in the reporter's own words/phrasing (light cleanup only, e.g. capitalization and removing the location text) — do not rewrite or embellish the defect description.

WORKED EXAMPLES

Report: "Ply coming off in B-804, Master Bedroom"
{{"specific_area": "B-804, Master Bedroom", "fault_description": "Ply coming off"}}

Report: "B2-506 Main, Master Bedroom, ply coming off near window"
{{"specific_area": "B2-506 Main, Master Bedroom", "fault_description": "Ply coming off near window"}}

Report: "Leaking pipe near kitchen sink, Tower C 2nd floor"
{{"specific_area": "Tower C, 2nd floor, kitchen", "fault_description": "Leaking pipe near sink"}}

Report: "Tower B Flat 12 bathroom tiles cracked"
{{"specific_area": "Tower B, Flat 12, bathroom", "fault_description": "Tiles cracked"}}

Report: "kitchen leaking"
{{"specific_area": "Kitchen", "fault_description": "Leaking"}}

Report: "Paint peeling"
{{"specific_area": "Not specified", "fault_description": "Paint peeling"}}

Report: "A102 Tower A 3rd floor balcony railing loose, also door handle broken"
{{"specific_area": "A102, Tower A, 3rd floor, balcony", "fault_description": "Railing loose, also door handle broken"}}

Now extract from this report. Re-read it once looking specifically for any unit code, tower/block/wing name, floor, or room name you might have missed before writing your answer:
"{caption}"
"""

    try:
        response = ai_model.generate_content(prompt)
        raw_text = response.text.strip()
        # Strip markdown code fences defensively, in case the model wraps
        # the JSON despite being told not to.
        raw_text = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw_text.strip())
        data = json.loads(raw_text)

        area = str(data.get("specific_area", "")).strip() or "Not specified"
        fault = str(data.get("fault_description", "")).strip() or caption.strip()

        # --- Backstop pass ---
        # Don't just trust the model's split. Re-scan whatever it put in
        # fault_description for location patterns it may have missed or
        # left half-stripped, and move anything found over to specific_area.
        # This is what actually fixes the "bedroom went to area, B2-506
        # Main stayed in the fault text" bug — even a perfect prompt can't
        # guarantee zero misses from a probabilistic model, so we verify.
        leftover_locations, cleaned_fault = strip_known_locations(fault)
        if leftover_locations:
            combined = [area] if area and area != "Not specified" else []
            combined.extend(leftover_locations)
            area = ", ".join(dict.fromkeys(combined))
            fault = cleaned_fault if cleaned_fault else fault

        return {"specific_area": area, "fault_description": fault}

    except Exception as e:
        print(f"AI Parsing Error: {e} | raw response: {getattr(response, 'text', 'N/A') if 'response' in locals() else 'no response'}")
        return regex_fallback_extract(caption)


# --- ONBOARDING / PROFILE COMPLETENESS ---
#
# name/project/site are collected once via the awaiting_name -> awaiting_project
# -> awaiting_site chain. But a user can end up "active" without all three
# actually being set — e.g. a legacy Mongo doc from before the "name" field
# existed, or any future write that only partially completes. Rather than
# silently logging "Not specified" forever, we check completeness before
# treating the user as active and, if something's missing, collect it (and
# ONLY it) before continuing.
REQUIRED_FIELDS = [
    ("name", "awaiting_name", "✏️ Before you continue, what's your *Full Name*?"),
    ("project", "awaiting_project", "🏢 Before you continue, what is your *Project Name*? (e.g., Vector Heights)"),
    ("site", "awaiting_site", "📍 Before you continue, what is the *Site Location*? (e.g., Tower B)"),
]


def start_missing_field_collection(sender_phone: str, user: dict) -> bool:
    """
    If name/project/site is missing or blank, switch the user into the
    matching awaiting_* state (remembering to return to "active" afterwards,
    NOT restart the whole onboarding chain), prompt for it, and return True.
    Returns False if the profile is already complete.
    """
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

                        # --- 1. FETCH OR CREATE USER IN MONGODB ---
                        user = users_collection.find_one({"phone": sender_phone})
                        if not user:
                            user = {
                                "phone": sender_phone,
                                "state": "awaiting_name",
                                "return_state": None,
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

                            # The /update command — changes project/site ONLY, never name.
                            if user_text.lower() == "/update":
                                users_collection.update_one(
                                    {"phone": sender_phone},
                                    {"$set": {"state": "awaiting_project", "return_state": None}}
                                )
                                send_whatsapp_message(sender_phone, "🔄 Location update initiated.\n\n🏢 What is the new *Project Name*?")
                                return {"status": "success"}

                            # The /name command — lets a user correct/change their logged name
                            # WITHOUT forcing them to redo project/site. return_state remembers
                            # "active" so they land right back where they were.
                            if user_text.lower() == "/name":
                                users_collection.update_one(
                                    {"phone": sender_phone},
                                    {"$set": {"state": "awaiting_name", "return_state": "active"}}
                                )
                                send_whatsapp_message(sender_phone, "✏️ What should we log your name as?")
                                return {"status": "success"}

                            # State: Awaiting Name (first-time setup, /name, or the
                            # missing-field safety net)
                            if user["state"] == "awaiting_name":
                                return_state = user.get("return_state")
                                if return_state == "active":
                                    # Correcting/backfilling name only — go straight
                                    # back to active, don't touch project/site.
                                    users_collection.update_one(
                                        {"phone": sender_phone},
                                        {"$set": {"name": user_text, "state": "active", "return_state": None}}
                                    )
                                    send_whatsapp_message(sender_phone, f"✅ Name updated to *{user_text}*. You're all set to keep reporting.")
                                else:
                                    # Fresh onboarding — continue the chain to project.
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
                                return_state = user.get("return_state")
                                if return_state == "active":
                                    users_collection.update_one(
                                        {"phone": sender_phone},
                                        {"$set": {"project": user_text, "state": "active", "return_state": None}}
                                    )
                                    send_whatsapp_message(sender_phone, f"✅ Project set to *{user_text}*. You're all set to keep reporting.")
                                else:
                                    users_collection.update_one(
                                        {"phone": sender_phone},
                                        {"$set": {"project": user_text, "state": "awaiting_site"}}
                                    )
                                    send_whatsapp_message(sender_phone, f"Got it. Project set to *{user_text}*.\n\n📍 Now, what is the *Site Location*? (e.g., Tower B)")
                                return {"status": "success"}

                            # State: Awaiting Site Location
                            elif user["state"] == "awaiting_site":
                                users_collection.update_one(
                                    {"phone": sender_phone},
                                    {"$set": {"site": user_text, "state": "active", "return_state": None}}
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
                                # Safety net: legacy/partial docs might be "active"
                                # without name/project/site actually set. Catch it
                                # here instead of silently logging "Not specified".
                                if start_missing_field_collection(sender_phone, user):
                                    return {"status": "success"}

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

                            # Same safety net as above — don't log a report with a
                            # missing name/project/site, ask for it first and have
                            # the reporter resend the photo.
                            if start_missing_field_collection(sender_phone, user):
                                send_whatsapp_message(
                                    sender_phone,
                                    "Please resend the photo once you've replied above."
                                )
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
                                display_name(user),
                                ai_data["specific_area"],
                                ai_data["fault_description"],
                                drive_link,
                                "Pending Review"
                            ]
                            sheet_link = log_to_sheet(row_data)

                            # D. Send confirmation with BOTH the photo link and the sheet link
                            reply_msg = (
                                f"✅ *Report Logged!*\n\n"
                                f"🙋 *Logged by:* {display_name(user)}\n"
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