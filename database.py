"""
database.py
-----------
MongoDB helpers for the multi-tenant demo sheet system.

Manages the `user_demo_sheets` collection inside the existing
`wa_logger_db` database.  Each document maps a WhatsApp phone number
to its own dedicated Google Spreadsheet.

Schema:
    {
        "phone_number": str,   # Primary key / index
        "sheet_id":    str,    # Google Spreadsheet ID
        "sheet_url":   str,    # Public edit URL
        "created_at":  str,    # ISO-8601 UTC timestamp
    }
"""

import os
import datetime

import certifi
import pymongo
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Connection (re-uses the same Atlas URI as main.py)
# ---------------------------------------------------------------------------
MONGODB_URI = os.getenv("MONGODB_URI")

_mongo_client = pymongo.MongoClient(MONGODB_URI, tlsCAFile=certifi.where())
_db = _mongo_client["wa_logger_db"]

# Ensure a unique index on phone_number for fast look-ups and upserts
user_demo_sheets: pymongo.collection.Collection = _db["user_demo_sheets"]
user_demo_sheets.create_index("phone_number", unique=True)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def get_user_sheet(phone_number: str) -> dict | None:
    """Return the sheet record for *phone_number*, or None if not found.

    Returns a dict with keys: phone_number, sheet_id, sheet_url, created_at.
    """
    return user_demo_sheets.find_one(
        {"phone_number": phone_number},
        {"_id": 0}          # exclude internal Mongo _id from the result
    )


def save_user_sheet(phone_number: str, sheet_id: str, sheet_url: str) -> None:
    """Persist (or update) the sheet mapping for *phone_number*.

    Uses upsert so it is safe to call even if a record already exists.
    """
    user_demo_sheets.update_one(
        {"phone_number": phone_number},
        {
            "$set": {
                "sheet_id":  sheet_id,
                "sheet_url": sheet_url,
            },
            "$setOnInsert": {
                "phone_number": phone_number,
                "created_at":   datetime.datetime.utcnow().isoformat() + "Z",
            },
        },
        upsert=True,
    )
