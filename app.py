import os
import traceback
from flask import Flask, jsonify, request
import requests

app = Flask(__name__)

# --- Обязательные переменные окружения ---
REQUIRED_ENV_VARS = [
    "OPENAI_API_KEY",
    "SPREADSHEET_ID",
    "TO_ANALYZE_FOLDER_ID",
    "ANALYZED_FOLDER_ID"
]

def check_requirements():
    print("[INFO] Проверка переменных окружения...")
    missing = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]
    if missing:
        print(f"[WARNING] Отсутствуют переменные: {missing}")
    else:
        print("[INFO] Все обязательные переменные заданы.")
    if not os.path.exists("credentials.json"):
        print("[WARNING] credentials.json не найден!")
    else:
        print("[INFO] credentials.json найден.")

@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"status": "ok", "message": "pong"})

def get_google_services():
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    import gspread

    creds = Credentials.from_service_account_file(
        "credentials.json",
        scopes=[
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets"
        ]
    )

    drive_service = build("drive", "v3", credentials=creds)
    sheets_client = gspread.authorize(creds)
    sheet = sheets_client.open_by_key(os.getenv("SPREADSHEET_ID")).sheet1
    print("[INFO] Google API подключены")
    return drive_service, sheet

def get_openai_client():
    from openai import OpenAI
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY не задан!")
    print("[INFO] OpenAI клиент готов")
    return OpenAI(api_key=api_key)  # ✅ только api_key, никаких proxies

@app.route("/analyze", methods=["POST"])
def analyze():
    try:
        drive_service, sheet = get_google_services()
        openai_client = get_openai_client()
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500

    TO_ANALYZE_FOLDER_ID = os.getenv("TO_ANALYZE_FOLDER_ID")
    ANALYZED_FOLDER_ID = os.getenv("ANALYZED_FOLDER_ID")
    processed = []

    try:
        results = drive_service.files().list(
            q=f"'{TO_ANALYZE_FOLDER_ID}' in parents and mimeType contains 'image/'",
            fields="files(id, name, webViewLink)"
        ).execute()
        files = results.get("files", [])
    except Exception:
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Ошибка получения файлов из Google Drive"}), 500

    for f in files:
        file_id = f["id"]
        file_url = f["webViewLink"]
        catalog_number = description = machine_type = "UNKNOWN"

        try:
            response = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "Ты эксперт по запчастям спецтехники."},
                    {"role": "user", "content": [
                        {"type": "text", "text": "Определи каталожный номер, описание и технику для этой детали."},
                        {"type": "image_url", "image_url": {"url": file_url}}
                    ]}
                ],
                max_tokens=300
            )
            result_text = response.choices[0].message.content.strip()
            catalog_number, description, machine_type = result_text, "-", "-"
            print(f"[INFO] OpenAI ответ для {file_url}: {result_text}")
        except Exception:
            traceback.print_exc()

        try:
            file_info = drive_service.files().get(fileId=file_id, fields="parents").execute()
            prev_parents = ",".join(file_info.get("parents"))
            drive_service.files().update(
                fileId=file_id,
                addParents=ANALYZED_FOLDER_ID,
                removeParents=prev_parents,
                fields="id, parents"
            ).execute()
            print(f"[INFO] Файл {f['name']} перемещен в analyzed")
        except Exception:
            traceback.print_exc()

        try:
            sheet.append_row([catalog_number, description, machine_type, file_url])
            print(f"[INFO] Строка для {f['name']} добавлена в Google Sheets")
        except Exception:
            traceback.print_exc()

        processed.append({"file": f["name"], "catalog_number": catalog_number, "description": description, "machine_type": machine_type})

    return jsonify({"status": "done", "processed_count": len(processed), "processed": processed})

if __name__ == "__main__":
    check_requirements()
    port = int(os.getenv("PORT", 5000))
    print(f"[INFO] Flask запускается на порту {port}...")
    app.run(host="0.0.0.0", port=port, debug=True)
