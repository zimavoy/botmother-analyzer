import os
import json
import traceback
import requests
from flask import Flask, jsonify
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import gspread

app = Flask(__name__)

# Обязательные переменные окружения
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

YANDEX_VISION_URL = "https://vision.api.cloud.yandex.net/vision/v1/batchAnalyze"


def check_requirements():
    print("[INFO] Проверка окружения...")
    missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
    if missing:
        print(f"[WARNING] Не заданы: {', '.join(missing)}")
    if not os.path.exists("credentials.json"):
        print("[ERROR] Нет файла credentials.json — Google API работать не будет.")


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
        print(f"[ERROR] Ошибка при проверке заголовков: {e}")

    return drive_service, sheet


def analyze_with_yandex_vision(image_url):
    """
    Анализ изображения через Yandex Vision API
    """
    headers = {
        "Authorization": f"Api-Key {os.getenv('YANDEX_API_KEY')}",
        "Content-Type": "application/json",
    }

    payload = {
        "analyze_specs": [
            {
                "features": [{"type": "TEXT_DETECTION"}],
                "content": None,
                "uri": image_url,
            }
        ]
    }

    try:
        response = requests.post(YANDEX_VISION_URL, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()

        if "results" not in data or not data["results"]:
            return "UNKNOWN"

        annotations = data["results"][0]["results"][0].get("textDetection", {}).get("pages", [])
        text_content = []
        for page in annotations:
            for block in page.get("blocks", []):
                for line in block.get("lines", []):
                    line_text = " ".join([word["text"] for word in line.get("words", [])])
                    text_content.append(line_text)
        return "\n".join(text_content) if text_content else "UNKNOWN"

    except Exception as e:
        print(f"[ERROR] Ошибка Vision API: {e}")
        return "UNKNOWN"


@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"status": "ok", "message": "pong"})


@app.route("/analyze", methods=["POST"])
def analyze():
    try:
        drive, sheet = get_google_services()
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
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Google Drive недоступен"}), 500

    for f in files:
        file_id, file_name = f["id"], f["name"]
        file_url = f.get("webContentLink") or f"https://drive.google.com/uc?export=download&id={file_id}"

        print(f"[INFO] Анализ изображения {file_name} ({file_url})")

        try:
            recognized_text = analyze_with_yandex_vision(file_url)
            catalog_number = recognized_text[:30] if recognized_text != "UNKNOWN" else "UNKNOWN"
            description = recognized_text[:100] if recognized_text != "UNKNOWN" else "UNKNOWN"
            machine_type = manufacturer = analogs = detail_description = machine_model = "UNKNOWN"

            sheet.append_row(
                [
                    catalog_number,
                    description,
                    machine_type,
                    manufacturer,
                    analogs,
                    detail_description,
                    machine_model,
                    file_url,
                ]
            )

            drive.files().update(
                fileId=file_id, addParents=ANALYZED, fields="id, parents"
            ).execute()

            processed.append(
                {"file": file_name, "recognized_text": recognized_text[:200]}
            )

        except Exception as e:
            print(f"[ERROR] Ошибка при анализе {file_name}: {e}")
            traceback.print_exc()

    return jsonify(
        {"status": "done", "processed_count": len(processed), "processed": processed}
    )


if __name__ == "__main__":
    check_requirements()
    port = int(os.getenv("PORT", 5000))
    print(f"[INFO] Flask сервер запущен на порту {port}...")
    app.run(host="0.0.0.0", port=port, debug=True)
