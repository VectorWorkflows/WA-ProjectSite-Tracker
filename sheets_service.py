"""
sheets_service.py
-----------------
Fully-automated, per-user Google Spreadsheet provisioner.

Calling `get_or_create_user_sheet(phone_number)` will:
  1. Return the cached (sheet_id, sheet_url) from MongoDB if the user
     already has a sheet.
  2. Otherwise:
      a. Create a Google Spreadsheet directly inside REPORTS_FOLDER_ID via
         the Drive API (avoids 403 move-permission errors).
      b. Rename the default tab to 'Fault Log' via Sheets batchUpdate.
      c. Write the 6-column audit header row into Row 1.
      d. Grant anyone-with-link / reader access via the Drive API.
      e. Persist the mapping to MongoDB via database.save_user_sheet().
      f. Return (sheet_id, sheet_url).

No template sheet ID is required.  Headers are hardcoded below.
"""

import os

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

import database  # local module

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

HEADERS = ["Timestamp", "Floor", "Room", "Fault Description", "Severity", "Image URL"]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

CREDS_FILE = "credentials.json"

# Drive folder IDs (optional – read from environment at import time)
# If REPORTS_FOLDER_ID is set, newly created spreadsheets are moved there.
# If PHOTOS_FOLDER_ID is set, fault images are uploaded into that folder.
REPORTS_FOLDER_ID = os.getenv("REPORTS_FOLDER_ID")
PHOTOS_FOLDER_ID  = os.getenv("PHOTOS_FOLDER_ID")


# ---------------------------------------------------------------------------
# Internal: build API service clients
# ---------------------------------------------------------------------------

def _get_services():
    """Return authenticated (sheets_service, drive_service) tuple."""
    creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
    sheets_svc = build("sheets", "v4", credentials=creds)
    drive_svc  = build("drive",  "v3", credentials=creds)
    return sheets_svc, drive_svc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_or_create_user_sheet(phone_number: str) -> tuple[str, str]:
    """Return *(sheet_id, sheet_url)* for *phone_number*.

    Creates a new Google Spreadsheet (with headers) on first call for a
    given number; subsequent calls return the cached values from MongoDB.

    Args:
        phone_number: The user's WhatsApp number (e.g. "919876543210").

    Returns:
        A 2-tuple ``(sheet_id, sheet_url)`` where *sheet_url* is the
        publicly-accessible edit URL of the spreadsheet.
    """
    # ── 1. Check the cache ──────────────────────────────────────────────
    record = database.get_user_sheet(phone_number)
    if record:
        print(f"[sheets_service] ✅ Returning cached sheet for {phone_number}")
        return record["sheet_id"], record["sheet_url"]

    # ── 2. Create spreadsheet directly inside REPORTS_FOLDER_ID ────────
    #    Using the Drive API with 'parents' avoids the two-step
    #    create-then-move pattern that triggers a 403 when the service
    #    account lacks organizer rights on the shared drive root.
    last4 = phone_number[-4:] if len(phone_number) >= 4 else phone_number
    title = f"Vector_Demo_Log_{last4}"

    sheets_svc, drive_svc = _get_services()

    print(f"[sheets_service] 🆕 Creating new spreadsheet via Drive API: {title}")
    file_metadata = {
        "name": title,
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "parents": [REPORTS_FOLDER_ID] if REPORTS_FOLDER_ID else [],
    }
    created_file = (
        drive_svc.files()
        .create(
            body=file_metadata,
            fields="id",
            supportsAllDrives=True,
        )
        .execute()
    )
    new_sheet_id: str = created_file["id"]
    print(f"[sheets_service] 📁 Spreadsheet created (id={new_sheet_id}" +
          (f" in folder {REPORTS_FOLDER_ID})" if REPORTS_FOLDER_ID else ")"))

    # ── 3. Rename default 'Sheet1' tab to 'Fault Log' ───────────────────
    #    Drive API create gives us a plain spreadsheet with one tab named
    #    'Sheet1'.  We need to rename it so range references work.
    sheet_meta = sheets_svc.spreadsheets().get(spreadsheetId=new_sheet_id).execute()
    first_sheet_id = sheet_meta["sheets"][0]["properties"]["sheetId"]
    sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=new_sheet_id,
        body={
            "requests": [{
                "updateSheetProperties": {
                    "properties": {"sheetId": first_sheet_id, "title": "Fault Log"},
                    "fields": "title",
                }
            }]
        },
    ).execute()
    print(f"[sheets_service] 🏷️  Tab renamed to 'Fault Log'.")

    # ── 4. Write the header row (Row 1) ─────────────────────────────────
    sheets_svc.spreadsheets().values().update(
        spreadsheetId=new_sheet_id,
        range="Fault Log!A1",
        valueInputOption="RAW",
        body={"values": [HEADERS]},
    ).execute()
    print(f"[sheets_service] 📝 Header row written.")

    # ── 5. Make the sheet publicly readable ─────────────────────────────
    drive_svc.permissions().create(
        fileId=new_sheet_id,
        body={"type": "anyone", "role": "reader"},
        supportsAllDrives=True,
    ).execute()
    print(f"[sheets_service] 🔓 Public read permission granted.")

    # ── 6. Build the shareable URL ───────────────────────────────────────
    sheet_url = f"https://docs.google.com/spreadsheets/d/{new_sheet_id}/edit"

    # ── 6. Persist to MongoDB ────────────────────────────────────────────
    database.save_user_sheet(phone_number, new_sheet_id, sheet_url)
    print(f"[sheets_service] 💾 Saved to DB. Sheet URL: {sheet_url}")

    return new_sheet_id, sheet_url


def append_audit_row(sheet_id: str, row: list) -> None:
    """Append a single data row to the 'Fault Log' sheet.

    Args:
        sheet_id: The Google Spreadsheet ID to write to.
        row:      A list of 6 values matching HEADERS order:
                  [Timestamp, Floor, Room, Fault Description, Severity, Image URL]
    """
    sheets_svc, _ = _get_services()
    sheets_svc.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range="Fault Log!A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()
    print(f"[sheets_service] ✅ Row appended to sheet {sheet_id}")
