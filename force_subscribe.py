import os
import requests
from dotenv import load_dotenv

load_dotenv()
WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WABA_ID = os.getenv("WABA_ID")

if not WABA_ID:
    print("❌ Error: WABA_ID is missing from your .env file!")
    exit()

# Meta requires the WhatsApp Business Account ID for subscribing webhooks
url = f"https://graph.facebook.com/v19.0/{WABA_ID}/subscribed_apps"

headers = {
    "Authorization": f"Bearer {WHATSAPP_TOKEN}",
    "Content-Type": "application/json"
}

print(f"Forcing Meta to link WABA Account ID {WABA_ID} to your Webhook...")

response = requests.post(url, headers=headers)

print(f"Status Code: {response.status_code}")
res_json = response.json()
print(res_json)

if response.status_code == 200 and res_json.get("success"):
    print("✅ SUCCESS! Your WhatsApp Business Account is now forcefully linked to your Webhook.")
else:
    print(f"❌ Error: {response.text}")