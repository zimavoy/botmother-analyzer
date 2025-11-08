import os
import json
import traceback
import requests
from flask import Flask, jsonify, render_template, send_from_directory
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import gspread

# Загружаем переменные окружения
load_dotenv()

app = Flask(__name__, static_folder='static', template_folder='static')

REQUIRED_ENV_VARS = [
    "YANDEX_API_KEY",
    "SPREADSHEET_ID",
    "TO_ANALYZE_FOLDER_ID",
    "ANALYZED_FOLDER_ID",
]

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

YANDEX_API_KEY = os.getenv("YANDEX_API_KEY")
YANDEX_VISION_URL = "https://vision.api.cloud.yandex.net/vision/v1/batchAnalyze"

# --- Проверка окружения ---
def check_requirements():
    print("[INFO] Проверка окружения...")
    missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
    if missing:
        print(f"[WARNING] Не заданы: {', '.join(missing)}")
    if not os.path.exists("credentials.json"):
        print("[ERROR] Нет файла credentials.json — Google API работать не будет.")

# --- Инициализация сервисов Google ---
def get_google_services():
    creds = Credentials.from_service_account_file(
        "credentials.json",
        scopes=[
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets",
        ],
    )
    drive_service = build("drive", "v3", credentials=creds)
    sheet = gspread.authorize(creds).open_by_key(os.getenv("SPREADSHEET_ID")).sheet1

    # Проверяем или создаем заголовки
    try:
        existing = sheet.row_values(1)
        if not existing:
            print("[INFO] Заголовки отсутствуют, добавляю...")
            sheet.insert_row(HEADERS, 1)
        elif existing != HEADERS:
            print("[WARNING] Заголовки не совпадают, обновляю...")
            sheet.delete_rows(1)
            sheet.insert_row(HEADERS, 1)
        else:
            print("[INFO] Заголовки корректны.")
    except Exception as e:
        print(f"[ERROR] Ошибка проверки заголовков: {e}")

    return drive_service, sheet

# --- Эндпоинты интерфейса ---
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/ping")
def ping():
    return jsonify({"status": "ok", "message": "pong"})

# --- Проверка лимитов Yandex Vision ---
@app.route("/api/limits")
def api_limits():
    try:
        headers = {"Authorization": f"Api-Key {YANDEX_API_KEY}"}
        resp = requests.get(
            "https://vision.api.cloud.yandex.net/vision/v1/quotas", headers=headers
        )
        if resp.status_code != 200:
            return jsonify({"total": "—", "remaining": "—"})

        data = resp.json()
        total = data.get("analyze_image", {}).get("limit", 0)
        remaining = data.get("analyze_image", {}).get("remaining", 0)
        return jsonify({"total": total, "remaining": remaining})
    except Exception as e:
        print("[ERROR] Не удалось получить лимиты:", e)
        return jsonify({"total": "—", "remaining": "—"})

# --- Основной анализ изображений ---
@app.route("/analyze", methods=["POST"])
def analyze():
    try:
        drive, sheet = get_google_services()
        TO_ANALYZE = os.getenv("TO_ANALYZE_FOLDER_ID")
        ANALYZED = os.getenv("ANALYZED_FOLDER_ID")

        results = drive.files().list(
            q=f"'{TO_ANALYZE}' in parents and mimeType contains 'image/'",
            fields="files(id, name, webViewLink, webContentLink)",
        ).execute()

        files = results.get("files", [])
        if not files:
            return jsonify({"status": "error", "message": "Нет изображений для анализа"})

        processed = []
        for f in files[:5]:  # Анализ 5 изображений за раз
            file_id, file_name = f["id"], f["name"]
            file_url = (
                f.get("webContentLink")
                or f"https://drive.google.com/uc?export=download&id={file_id}"
            )

            print(f"[INFO] Анализ {file_name} через Yandex Vision API...")

            try:
                image_resp = requests.get(file_url)
                image_bytes = image_resp.content
                b64_image = image_bytes.encode("base64") if hasattr(image_bytes, "encode") else None

                data = {
                    "analyze_specs": [
                        {
                            "content": b64_image,
                            "features": [{"type": "TEXT_DETECTION", "text_detection_config": {"language_codes": ["*"]}}],
                        }
                    ]
                }

                headers = {
                    "Authorization": f"Api-Key {YANDEX_API_KEY}",
                    "Content-Type": "application/json",
                }

                response = requests.post(YANDEX_VISION_URL, headers=headers, data=json.dumps(data))
                if response.status_code != 200:
                    raise Exception(f"Yandex Vision error: {response.text}")

                vision_data = response.json()
                text_blocks = vision_data["results"][0]["results"][0]["textDetection"]["pages"][0]["blocks"]
                detected_text = " ".join(
                    [
                        word["text"]
                        for block in text_blocks
                        for line in block.get("lines", [])
                        for word in line.get("words", [])
                    ]
                )

                print(f"[INFO] Распознанный текст: {detected_text[:80]}...")

                # Простая фильтрация
                catalog_number = "UNKNOWN"
                description = detected_text[:100] or "UNKNOWN"

                sheet.append_row(
                    [
                        catalog_number,
                        description,
                        "UNKNOWN",
                        "UNKNOWN",
                        "UNKNOWN",
                        detected_text,
                        "UNKNOWN",
                        file_url,
                    ]
                )

                processed.append({"file": file_name, "text": detected_text})

            except Exception as e:
                print(f"[ERROR] Ошибка анализа {file_name}: {e}")
                traceback.print_exc()

        return jsonify(
            {"status": "success", "processed_count": len(processed), "processed": processed}
        )

    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    check_requirements()
    port = int(os.getenv("PORT", 5000))
    print(f"[INFO] Flask запущен на порту {port}")
    app.run(host="0.0.0.0", port=port)

