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
client = OpenAI(api_key=OPENAI_API_KEY)

# --- Google API clients ---
creds = None
drive_service = None
sheets_client = None
if os.path.exists("credentials.json"):
    creds = Credentials.from_service_account_file(
        "credentials.json",
        scopes=["https://www.googleapis.com/auth/drive",
                "https://www.googleapis.com/auth/spreadsheets"]
    )
    drive_service = build("drive", "v3", credentials=creds)
    sheets_client = gspread.authorize(creds)
    print("[INFO] Google API clients initialized successfully")
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
        data = request.get_json()
        photo_url = data.get("photo_url")
        print(f"[INFO] /analyze called with photo_url={photo_url}")

        if not photo_url:
            print("[ERROR] photo_url missing in request")
            return jsonify({"status": "error", "message": "photo_url is required"}), 400

        # --- 1. Скачиваем фото локально ---
        filename = "temp.jpg"
        print("[INFO] Downloading photo...")
        r = requests.get(photo_url)
        with open(filename, "wb") as f:
            f.write(r.content)
        print("[INFO] Photo downloaded successfully")

        # --- 2. Отправляем в OpenAI Vision ---
        print("[INFO] Sending photo to OpenAI Vision...")
        with open(filename, "rb") as f:
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {
                        "role": "system",
                        "content": "Ты — эксперт по запчастям спецтехники. Определи каталожный номер, описание и для какой техники под
```
