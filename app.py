import os
import traceback
from flask import Flask, jsonify, render_template
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import gspread
from yandexcloud import SDK  # ✅ исправленный импорт
import requests
import time

app = Flask(__name__)

REQUIRED_ENV_VARS = ["YANDEX_API_KEY", "SPREADSHEET_ID", "TO_ANALYZE_FOLDER_ID", "ANALYZED_FOLDER_ID"]

HEADERS = [
    "Catalog Number",
    "Description",
    "Machine Type",
    "Manufacturer",
    "Analogs",
    "Detail Description",
    "Machine Model",
    "File URL",
]

BATCH_SIZE = 5

def check_requirements():
    print("[INFO] Проверка окружения...")
    missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
    if missing:
        print(f"[WARNING] Не заданы: {', '.join(missing)}")
    credentials_path = os.getenv("GOOGLE_CREDENTIALS_PATH", "/run/secrets/credentials.json")
    if not os.path.exists(credentials_path):
        print(f"[ERROR] Нет файла с сервисными учетными данными Google: {credentials_path}")

def get_google_services():
    credentials_path = os.getenv("GOOGLE_CREDENTIALS_PATH", "/run/secrets/credentials.json")
    creds = Credentials.from_service_account_file(
        credentials_path,
        scopes=["https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/spreadsheets"],
    )
    drive_service = build("drive", "v3", credentials=creds)
    sheet = gspread.authorize(creds).open_by_key(os.getenv("SPREADSHEET_ID")).sheet1

    try:
        existing = sheet.row_values(1)
        if not existing:
            sheet.insert_row(HEADERS, 1)
        elif existing != HEADERS:
            sheet.delete_rows(1)
            sheet.insert_row(HEADERS, 1)
    except Exception as e:
        print(f"[ERROR] Ошибка проверки заголовков: {e}")

    return drive_service, sheet

def get_yandex_client():
    token = os.getenv("YANDEX_API_KEY")
    sdk = SDK(iam_token=token)
    return sdk.client(  # ✅ создаём Vision клиент
        service_name="ai.vision.v1.ImageAnalyzer"
    )

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"status": "ok", "message": "pong"})

@app.route("/analyze", methods=["POST"])
def analyze():
    try:
        drive, sheet = get_google_services()
        vision_client = get_yandex_client()
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500

    TO_ANALYZE = os.getenv("TO_ANALYZE_FOLDER_ID")
    ANALYZED = os.getenv("ANALYZED_FOLDER_ID")
    processed = []

    try:
        results = drive.files().list(
            q=f"'{TO_ANALYZE}' in parents and mimeType contains 'image/'",
            fields="files(id, name, webViewLink, webContentLink)",
        ).execute()
        files = results.get("files", [])
    except Exception:
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Google Drive недоступен"}), 500

    for i in range(0, len(files), BATCH_SIZE):
        batch = files[i:i + BATCH_SIZE]
        for f in batch:
            file_id, file_name = f["id"], f["name"]
            file_url = f.get("webContentLink") or f"https://drive.google.com/uc?export=download&id={file_id}"

            catalog_number = description = machine_type = manufacturer = analogs = detail_description = machine_model = "UNKNOWN"

            try:
                print(f"[INFO] Анализ {file_name} ...")
                # Простая проверка через Vision API
                response = vision_client.Analyze(
                    folder_id=os.getenv("YANDEX_FOLDER_ID"),
                    analyze_specs=[{
                        "content": requests.get(file_url).content,
                        "features": [{"type": "TEXT_DETECTION"}]
                    }]
                )

                texts = []
                for result in response.results:
                    for text_block in result.text_detection.pages[0].blocks:
                        for line in text_block.lines:
                            line_text = "".join([el.text for el in line.elements])
                            texts.append(line_text)

                full_text = " ".join(texts)

                if "Catalog" in full_text:
                    catalog_number = full_text.split("Catalog")[1].split()[0]
                if "Description" in full_text:
                    description = full_text.split("Description")[1].split("\n")[0]

            except Exception as e:
                print(f"[ERROR] Ошибка анализа {file_name}: {e}")
                traceback.print_exc()

            try:
                file_info = drive.files().get(fileId=file_id, fields="parents").execute()
                prev_parents = ",".join(file_info.get("parents", []))
                drive.files().update(
                    fileId=file_id,
                    addParents=ANALYZED,
                    removeParents=prev_parents,
                    fields="id, parents"
                ).execute()
            except Exception:
                print(f"[ERROR] Не удалось переместить {file_name}")
                traceback.print_exc()

            try:
                sheet.append_row([catalog_number, description, machine_type, manufacturer, analogs, detail_description, machine_model, file_url])
            except Exception:
                print(f"[ERROR] Не удалось записать строку для {file_name}")
                traceback.print_exc()

            processed.append({
                "file": file_name,
                "catalog_number": catalog_number,
                "description": description
            })

        time.sleep(1)

    return jsonify({"status": "done", "processed_count": len(processed), "processed": processed})

if __name__ == "__main__":
    check_requirements()
    port = int(os.getenv("PORT", 5000))
    print(f"[INFO] Запуск Flask на порту {port}...")
    app.run(host="0.0.0.0", port=port, debug=True)
