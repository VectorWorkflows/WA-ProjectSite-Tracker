<div align="center">

<br />

<pre>
 ███████╗██╗████████╗███████╗    ████████╗██████╗  █████╗  ██████╗██╗  ██╗███████╗██████╗
 ██╔════╝██║╚══██╔══╝██╔════╝    ╚══██╔══╝██╔══██╗██╔══██╗██╔════╝██║ ██╔╝██╔════╝██╔══██╗
 ███████╗██║   ██║   █████╗         ██║   ██████╔╝███████║██║     █████╔╝ █████╗  ██████╔╝
 ╚════██║██║   ██║   ██╔══╝         ██║   ██╔══██╗██╔══██║██║     ██╔═██╗ ██╔══╝  ██╔══██╗
 ███████║██║   ██║   ███████╗       ██║   ██║  ██║██║  ██║╚██████╗██║  ██╗███████╗██║  ██║
 ╚══════╝╚═╝   ╚═╝   ╚══════╝       ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝
</pre>

### WA · SITE TRACKER

**Snap a photo. Send it on WhatsApp. Watch a construction fault log write itself.**

<sub>by <a href="https://vectorworkflows.com"><b>VECTOR WORKFLOWS</b></a> — precision-engineered automation</sub>

<br />

[![Python](https://img.shields.io/badge/PYTHON-3.11+-000000?style=for-the-badge&logo=python&logoColor=00D9FF)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FASTAPI-Webhook_Server-000000?style=for-the-badge&logo=fastapi&logoColor=00D9FF)](https://fastapi.tiangolo.com/)
[![WhatsApp](https://img.shields.io/badge/WHATSAPP-Cloud_API-000000?style=for-the-badge&logo=whatsapp&logoColor=00D9FF)](https://developers.facebook.com/docs/whatsapp)
[![Gemini](https://img.shields.io/badge/GEMINI-Vision_AI-000000?style=for-the-badge&logo=googlegemini&logoColor=00D9FF)](https://ai.google.dev/)
[![MongoDB](https://img.shields.io/badge/MONGODB-Multi--Tenant-000000?style=for-the-badge&logo=mongodb&logoColor=00D9FF)](https://www.mongodb.com/)
[![License](https://img.shields.io/badge/LICENSE-MIT-000000?style=for-the-badge&logoColor=00D9FF)](#license)

<br />

`photo + caption` → `AI reads the site` → `row lands in your own live spreadsheet`

<br />

</div>

<br />

## ▍ What is this

Site engineers don't fill out forms — they take photos and move on. **WA Site Tracker** meets them exactly there.

Send a WhatsApp photo of a cracked tile, a leaking pipe, or exposed wiring with a one-line caption like *"H2i-1107 Kitchen, ply coming off near window."* Behind the scenes, **Gemini Vision** looks at the photo *and* the caption together, decides the floor, the room, the fault, and the severity, uploads the image to Drive, and appends a clean audit row to a **Google Sheet that was auto-created just for that engineer** — no shared spreadsheet, no manual setup, no admin overhead.

Every engineer gets their own private, permanent, always-current fault log. Every photo becomes a data row. Every report is answered on WhatsApp in seconds.

<br />

## ▍ How it actually works

<div align="center">

```
┌──────────────┐   photo +   ┌────────────────────┐   vision +   ┌──────────────────┐
│   WHATSAPP   │   caption   │   FASTAPI WEBHOOK   │   caption    │   GEMINI VISION   │
│  engineer 📱 │ ──────────► │   < 100ms ack ⚡     │ ───────────► │  floor·room·      │
└──────────────┘             └────────────────────┘              │  fault·severity   │
                                       │                          └──────────────────┘
                                       │  background task                  │
                                       ▼                                   ▼
                              ┌─────────────────┐              ┌────────────────────┐
                              │   GOOGLE DRIVE   │◄────────────│   ROW ASSEMBLED     │
                              │  photo archive   │   image     │                     │
                              └─────────────────┘              └──────────┬─────────┘
                                                                          ▼
                              ┌──────────────────────────────────────────────────┐
                              │      YOUR PERSONAL "Fault Log" GOOGLE SHEET       │
                              │   (auto-provisioned on first message, MongoDB-    │
                              │           cached for every message after)        │
                              └──────────────────────────────────────────────────┘
```

</div>

1. **Meta pings the webhook.** The FastAPI server acknowledges in under 100ms — always, no matter what — because Meta kills anything slower than 5 seconds. Every heavy step runs afterward as a background task.
2. **The engineer is identified or onboarded.** First contact ever? A short state-machine walks them through *Name → Project → Site* once. Already registered by an admin? Their name is pre-filled and they skip straight to setup.
3. **Their personal spreadsheet is resolved.** `get_or_create_user_sheet()` checks MongoDB first. First time logging a fault, it provisions a brand-new Google Sheet on the spot — titled, tabbed `Fault Log`, headered, and shared — then remembers it forever.
4. **Gemini reads the photo.** Vision AI is handed the image *and* the caption together and returns strict JSON: floor, room, fault description, severity — no location codes leaking into the fault text, no markdown fences, no guessing.
5. **The row lands, the photo is archived, the reply goes out.** The image is pushed to Drive, the row is appended to the engineer's own sheet, and a WhatsApp confirmation — complete with a live link to their log — lands back in the same chat.

<br />

## ▍ What it handles for you

<table>
<tr><td width="30%"><b>🏗️ Zero-setup onboarding</b></td><td>New engineer texts the bot → guided through Name, Project, and Site in three quick replies. No app to install, no login screen.</td></tr>
<tr><td><b>👑 Admin-registered engineers</b></td><td><code>/add_user [phone] [name]</code> pre-registers a team member so their name auto-fills the moment they first message in.</td></tr>
<tr><td><b>🧠 Vision + language, together</b></td><td>Gemini reads the image and the caption as one signal — a caption like <i>"B2-506 Main, Master Bedroom, ply coming off"</i> is split cleanly into location vs. fault, with a regex-based fallback if the AI call ever fails.</td></tr>
<tr><td><b>📊 One private sheet per engineer</b></td><td>Every phone number gets its own Google Spreadsheet, created on demand and cached in MongoDB — nobody's fault log is ever mixed with anyone else's.</td></tr>
<tr><td><b>☁️ Automatic photo archive</b></td><td>Every submitted image is uploaded to a shared Drive folder and linked directly from its audit row.</td></tr>
<tr><td><b>⚡ Sub-second webhook</b></td><td>The AI call, the Drive upload, and the Sheets write all happen in a FastAPI <code>BackgroundTask</code> — Meta always gets its 200 OK instantly.</td></tr>
<tr><td><b>💬 Full command surface</b></td><td><code>/status</code>, <code>/update</code>, <code>/name</code>, <code>/reset</code>, <code>/help</code> — engineers can check or correct their profile without ever leaving the chat.</td></tr>
</table>

<br />

## ▍ Stack

<div align="center">

| Layer | Technology |
|:--|:--|
| **Messaging channel** | `WhatsApp Cloud API` — webhook verification, text + image handling |
| **Server** | `FastAPI` + `uvicorn` — instant webhook ack, background job dispatch |
| **Vision & language AI** | `google-generativeai` (Gemini) — structured JSON extraction from photo + caption |
| **Spreadsheets** | `gspread` + Sheets/Drive APIs — per-user sheet provisioning, header rows, row appends |
| **Persistence** | `MongoDB` (`pymongo`) — users, registered engineers, phone → sheet mapping |
| **Storage** | `Google Drive` — permanent, publicly-linkable fault photo archive |

</div>

<br />

## ▍ Get it running

```bash
# 1 — clone
git clone https://github.com/VectorWorkflows/WA-ProjectSite-Tracker.git
cd WA-ProjectSite-Tracker

# 2 — install
pip install -r requirements.txt

# 3 — configure
cp .env.example .env
# fill in WHATSAPP_TOKEN, PHONE_NUMBER_ID, VERIFY_TOKEN, WABA_ID,
# MONGODB_URI, GEMINI_API_KEY, ADMIN_NUMBERS

# 4 — bring your own Google service account
# drop your Google Cloud service-account credentials.json in the project root
# (needs Sheets + Drive scopes)

# 5 — link Meta's webhook to your WhatsApp Business Account
python force_subscribe.py

# 6 — launch
python main.py
```

<details>
<summary><b>Environment variables</b></summary>

<br />

| Variable | Purpose |
|:--|:--|
| `WHATSAPP_TOKEN` | Access token for the WhatsApp Cloud API |
| `PHONE_NUMBER_ID` | Meta's ID for your sending phone number |
| `VERIFY_TOKEN` | Shared secret used during webhook verification |
| `WABA_ID` | WhatsApp Business Account ID (used by `force_subscribe.py`) |
| `DRIVE_FOLDER_ID` | Legacy fallback photo folder (superseded by `PHOTOS_FOLDER_ID`) |
| `GOOGLE_SHEET_NAME` | Legacy single-tenant sheet name |
| `MONGODB_URI` | MongoDB Atlas connection string |
| `GEMINI_API_KEY` | API key for Gemini Vision |
| `ADMIN_NUMBERS` | Comma-separated phone numbers allowed to run `/add_user` |

</details>

<br />

## ▍ Repository map

```
WA-ProjectSite-Tracker/
├── main.py                → FastAPI webhook, state machine, Gemini extraction, job orchestration
├── sheets_service.py       → Per-user spreadsheet provisioning + audit row writer
├── database.py             → MongoDB mapping: phone number → personal sheet
├── force_subscribe.py      → One-time script to bind Meta's webhook to your WABA
├── requirements.txt
└── src/
    └── config.py            → WhatsApp environment configuration
```

<br />

## ▍ Philosophy

Vector Workflows builds automation that disappears into the background — the best interface is the one you stop noticing. A site engineer shouldn't have to learn a tool to report a fault. They already know how to send a photo on WhatsApp — **everything downstream of that should just happen.**

<br />

---

<div align="center">

<sub>Crafted by <a href="https://vectorworkflows.com"><b>Vector Workflows</b></a></sub>

<sub>MIT Licensed — build on it, fork it, make it yours.</sub>

</div>
