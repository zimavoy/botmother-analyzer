```python
import os
import json
import requests
from flask import Flask, request, jsonify
from openai import OpenAI
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Flask-приложение
app = Flask(__name__)

# --- Проверка переменных окружения ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
TO_ANALYZE_FOLDER_ID = os.getenv("TO_ANALYZE_FOLDER_ID")
ANALYZED_FOLDER_ID = os.getenv("ANALYZED_FOLDER_ID")

missing = [var for var in ["OPENAI_API_KEY", "SPREADSHEET_ID",
                           "TO_ANALYZE_FOLDER_ID", "ANALYZED_FOLDER_ID"]
           if os.getenv(var) is None]
if missing:
    print(f"[WARN] Missing environment variables: {missing}")

# --- OpenAI client ---
client = None
if OPENAI_API_KEY:
    client = OpenAI(api_key=OPENAI_API_KEY)
    print("[INFO] OpenAI client initialized")
else:
    print("[ERROR] OPENAI_API_KEY not set")

# --- Google API clients ---
creds = None
drive_service = None
sheets_client = None
if os.path.exists("credentials.json"):
    try:
        creds = Credentials.from_service_account_file(
            "credentials.json",
            scopes=["https://www.googleapis.com/auth/drive",
                    "https://www.googleapis.com/auth/spreadsheets"]
        )
        drive_service = build("drive", "v3", credentials=creds)
        sheets_client = gspread.authorize(creds)
        print("[INFO] Google API clients initialized successfully")
    except Exception as e:
        print(f"[ERROR] Failed to initialize Google API clients: {e}")
else:
    print("[WARN] credentials.json not found, Google API won't work")


# --- Healthcheck ---
@app.route("/ping", methods=["GET"])
def ping():
    print("[INFO] /ping called")
    return jsonify({"status": "ok", "message": "Service is running"})


# --- Analyze endpoint ---
@app.route("/analyze", methods=["POST"])
def analyze():
    try:
        data = request.get_json(force=True)
        photo_url = data.get("photo_url")
        print(f"[INFO] /analyze called with photo_url={photo_url}")

        if not photo_url:
            print("[ERROR] photo_url missing in request")
            return jsonify({"status": "error", "message": "photo_url is required"}), 400

        if not client:
            return jsonify({"status": "error", "message": "OpenAI client not initialized"}), 500

        # --- 1. Скачиваем фото ---
        filename = "temp.jpg"
        print("[INFO] Downloading photo...")
        r = requests.get(photo_url)
        r.raise_for_status()
        with open(filename, "wb") as f:
            f.write(r.content)
        print("[INFO] Photo downloaded successfully")

        # --- 2. Отправляем в OpenAI Vision ---
        print("[INFO] Sending photo to OpenAI Vision...")
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты — эксперт по запчастям спецтехники. "
                        "Определи каталожный номер, описание и для какой техники подходит."
                    )
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Определи запчасть"},
                        {"type": "image_url", "image_url": {"url": photo_url}}
                    ]
                }
            ],
            max_tokens=300
        )

        ai_text = response.choices[0].message.content.strip()
        print(f"[INFO] OpenAI response: {ai_text}")

        # --- 3. Парсим ответ ---
        part_number, description, machine = "N/A", "N/A", "N/A"
        try:
            parsed = json.loads(ai_text)
            part_number = parsed.get("part_number", "N/A")
            description = parsed.get("description", "N/A")
            machine = parsed.get("machine", "N/A")
            print(f"[INFO] Parsed JSON: {parsed}")
        except Exception:
            description = ai_text
            print("[WARN] OpenAI response is not JSON, using raw text")

        # --- 4. Загружаем фото в Google Drive ---
        if creds and drive_service and ANALYZED_FOLDER_ID:
            try:
                print("[INFO] Uploading photo to Google Drive...")
                file_metadata = {
                    "name": os.path.basename(filename),
                    "parents": [ANALYZED_FOLDER_ID]
                }
                media = MediaFileUpload(filename, mimetype="image/jpeg")
                uploaded_file = drive_service.files().create(
                    body=file_metadata,
                    media_body=media,
                    fields="id"
                ).execute()
                photo_url = f"https://drive.google.com/file/d/{uploaded_file.get('id')}/view"
                print(f"[INFO] Photo uploaded to Drive: {photo_url}")
            except Exception as e:
                print(f"[ERROR] Failed to upload to Drive: {e}")

        # --- 5. Записываем в Google Sheets ---
        if creds and sheets_client and SPREADSHEET_ID:
            try:
                print("[INFO] Writing row to Google Sheets...")
                sheet = sheets_client.open_by_key(SPREADSHEET_ID).sheet1
                sheet.append_row([part_number, description, machine, photo_url])
                print("[INFO] Row written to Google Sheets")
            except Exception as e:
                print(f"[ERROR] Failed to write to Google Sheets: {e}")

        print("[INFO] Returning success response")
        return jsonify({
            "status": "ok",
            "processed": 1,
            "part_number": part_number,
            "description": description,
            "machine": machine,
            "photo_url": photo_url
        })

    except Exception as e:
        print(f"[ERROR] Exception during analyze: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    print("[INFO] Starting Flask app...")
    app.run(host="0.0.0.0", port=5000)
```
