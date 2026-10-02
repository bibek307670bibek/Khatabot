"""
WhatsApp Voice Khata Assistant - शुरुआती वर्शन

काम:
  दुकानदार वॉइस नोट भेजता है -> आवाज़ से टेक्स्ट -> एंट्री समझना -> SQLite में सेव -> जवाब

सेटअप:
  pip install flask requests openai anthropic

  नीचे ये environment variables सेट करें:
    WHATSAPP_TOKEN     (Meta से मिला access token)
    PHONE_NUMBER_ID    (Meta के WhatsApp Business में आपके नंबर की ID)
    VERIFY_TOKEN       (कोई भी गुप्त शब्द, Meta में webhook जोड़ते समय वही डालना)
    OPENAI_API_KEY     (आवाज़ -> टेक्स्ट के लिए)
    ANTHROPIC_API_KEY  (एंट्री समझने के लिए)

  चलाने के लिए:  python app.py
  टेस्ट के लिए ngrok से अपना लोकल सर्वर इंटरनेट पर खोलें, और उस लिंक के
  आगे /webhook जोड़कर Meta में webhook URL की जगह डालें।
"""

import io
import json
import os
import sqlite3

import requests
from anthropic import Anthropic
from flask import Flask, request
from openai import OpenAI

app = Flask(__name__)

WHATSAPP_TOKEN = os.environ["WHATSAPP_TOKEN"]
PHONE_NUMBER_ID = os.environ["PHONE_NUMBER_ID"]
VERIFY_TOKEN = os.environ["VERIFY_TOKEN"]

GRAPH = "https://graph.facebook.com/v20.0"
HEADERS = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}

openai_client = OpenAI()
claude = Anthropic()
CLAUDE_MODEL = "claude-sonnet-5-5"
DB_PATH = "khata.db"


# ---------- डेटाबेस ----------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_phone TEXT,
            customer TEXT,
            kind TEXT,          -- udhaar या payment
            amount REAL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    return conn


def add_entry(shop, customer, kind, amount):
    with db() as conn:
        conn.execute(
            "INSERT INTO entries (shop_phone, customer, kind, amount) VALUES (?,?,?,?)",
            (shop, customer, kind, amount),
        )


def balance(shop, customer):
    with db() as conn:
        row = conn.execute(
            """SELECT COALESCE(SUM(CASE WHEN kind='udhaar' THEN amount ELSE -amount END), 0)
               FROM entries WHERE shop_phone=? AND customer=?""",
            (shop, customer),
        ).fetchone()
    return row[0]


# ---------- WhatsApp ----------
def send_text(to, text):
    requests.post(
        f"{GRAPH}/{PHONE_NUMBER_ID}/messages",
        headers=HEADERS,
        json={
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text},
        },
        timeout=30,
    )


def download_media(media_id):
    meta = requests.get(f"{GRAPH}/{media_id}", headers=HEADERS, timeout=30).json()
    audio = requests.get(meta["url"], headers=HEADERS, timeout=60)
    return audio.content


# ---------- आवाज़ -> टेक्स्ट ----------
def transcribe(audio_bytes):
    f = io.BytesIO(audio_bytes)
    f.name = "voice.ogg"
    result = openai_client.audio.transcriptions.create(
        model="whisper-1", file=f, language="hi"
    )
    return result.text


# ---------- टेक्स्ट -> एंट्री ----------
SYSTEM_PROMPT = """तुम एक दुकान के हिसाब-किताब का सहायक हो।
दुकानदार का संदेश पढ़कर सिर्फ JSON लौटाओ, और कुछ नहीं:
{"type": "udhaar" | "payment" | "balance_query" | "unknown",
 "customer": "ग्राहक का नाम",
 "amount": संख्या या null}

- udhaar: ग्राहक को सामान उधार दिया
- payment: ग्राहक ने पैसे चुकाए
- balance_query: दुकानदार पूछ रहा है कि ग्राहक का कितना बाकी है
- समझ न आए तो type "unknown" रखो"""


def parse_message(text):
    resp = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=300,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": text}],
    )
    raw = resp.content[0].text.strip().replace("```json", "").replace("```", "")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"type": "unknown"}


def handle_text(shop, text):
    data = parse_message(text)
    kind = data.get("type")
    customer = data.get("customer")
    amount = data.get("amount")

    if kind in ("udhaar", "payment") and customer and amount:
        add_entry(shop, customer, kind, float(amount))
        total = balance(shop, customer)
        word = "उधार" if kind == "udhaar" else "जमा"
        return f"✅ एंट्री हो गई: {customer} - ₹{amount:g} {word}।\n{customer} पर अब कुल ₹{total:g} बाकी है।"

    if kind == "balance_query" and customer:
        return f"{customer} पर कुल ₹{balance(shop, customer):g} बाकी है।"

    return "माफ़ कीजिए, मैं समझ नहीं पाया। कृपया दोबारा बोलें, जैसे: 'रमेश को पांच सौ रुपए का सामान दिया, उधार'।"


# ---------- Webhook ----------
@app.get("/webhook")
def verify():
    if (
        request.args.get("hub.mode") == "subscribe"
        and request.args.get("hub.verify_token") == VERIFY_TOKEN
    ):
        return request.args.get("hub.challenge", ""), 200
    return "forbidden", 403


@app.post("/webhook")
def incoming():
    body = request.get_json(silent=True) or {}
    try:
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                for msg in change.get("value", {}).get("messages", []):
                    shop = msg["from"]
                    if msg["type"] == "audio":
                        text = transcribe(download_media(msg["audio"]["id"]))
                    elif msg["type"] == "text":
                        text = msg["text"]["body"]
                    else:
                        send_text(shop, "कृपया वॉइस नोट या टेक्स्ट भेजें।")
                        continue
                    send_text(shop, handle_text(shop, text))
    except Exception as e:
        print("error:", e)
    return "ok", 200


if __name__ == "__main__":
    app.run(port=5000)
