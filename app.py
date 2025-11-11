import os
import traceback
import requests
import time
from flask import Flask, jsonify, render_template
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import gspread

app = Flask(__name__)

# ======== Конфигурация ========
REQUIRED_ENV_VARS = [
    "YANDEX_API_KEY",
    "SPREADSHEET_ID",
    "TO_ANALYZE_FOLDER_ID",
    "ANALYZED_FOLDER_ID",
    "YANDEX_FOLDER_ID",
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

BATCH_SIZE = 5
YANDEX_VISION_URL = "https://vision.api.cloud.yandex.net/vision/v1/batchAnalyze"


# ======== Проверка окружения ========
def check_requirements():
    print("[INFO] Проверка окружения...")
    missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
    if missing:
        print(f"[WARNING] Не заданы: {', '.join(missing)}")

    credentials_path = os.getenv("GOOGLE_CREDENTIALS_PATH", "/run/secrets/credentials.json")
    if not os.path.exists(credentials_path):
        print(f"[ERROR] Нет файла с сервисными учетными данными Google: {credentials_path}")
    else:
        print(f"[INFO] Найден файл сервисных данных: {credentials_path}")


# ======== Авторизация Google ========
def get_google_services():
    credentials_path = os.getenv("GOOGLE_CREDENTIALS_PATH", "/run/secrets/credentials.json")
    creds = Credentials.from_service_account_file(
        credentials_path,
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
            sheet.insert_row(HEADERS, 1)
        elif existing != HEADERS:
            sheet.delete_rows(1)
            sheet.insert_row(HEADERS, 1)
    except Exception as e:
        print(f"[ERROR] Ошибка проверки заголовков таблицы: {e}")

    return drive_service, sheet


# ======== Вызов Яндекс Vision через REST ========
def analyze_image_with_yandex(image_bytes):
    headers = {
        "Authorization": f"Api-Key {os.getenv('YANDEX_API_KEY')}",
    }
    folder_id = os.getenv("YANDEX_FOLDER_ID")

    body = {
        "folderId": folder_id,
        "analyze_specs": [
            {
                "content": image_bytes.decode("latin1"),
                "features": [{"type": "TEXT_DETECTION"}],
            }
        ],
    }

    try:
        response = requests.post(YANDEX_VISION_URL, headers=headers, json=body, timeout=30)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"[ERROR] Ошибка запроса к Яндекс Vision: {e}")
        traceback.print_exc()
        return None


# ======== Flask маршруты ========

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
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": f"Google Auth: {e}"}), 500

    TO_ANALYZE = os.getenv("TO_ANALYZE_FOLDER_ID")
    ANALYZED = os.getenv("ANALYZED_FOLDER_ID")
    processed = []

    try:
        results = drive.files().list(
            q=f"'{TO_ANALYZE}' in parents and mimeType contains 'image/'",
            fields="files(id, name, webViewLink, webContentLink)",
        ).execute()
        files = results.get("files", [])
        if not files:
            return jsonify({"status": "success", "message": "Нет изображений для анализа", "processed_count": 0})
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
                image_data = requests.get(file_url).content
                analysis = analyze_image_with_yandex(image_data)

                if not analysis:
                    raise Exception("Пустой ответ от Vision API")

                texts = []
                for result in analysis.get("results", []):
                    for page in result.get("results", []):
                        blocks = page.get("textDetection", {}).get("pages", [{}])[0].get("blocks", [])
                        for block in blocks:
                            for line in block.get("lines", []):
                                text = "".join([el.get("text", "") for el in line.get("elements", [])])
                                texts.append(text)

                full_text = " ".join(texts)

                if "Catalog" in full_text:
                    catalog_number = full_text.split("Catalog")[1].split()[0]
                if "Description" in full_text:
                    description = full_text.split("Description")[1].split("\n")[0]

            except Exception as e:
                print(f"[ERROR] Ошибка анализа {file_name}: {e}")
                traceback.print_exc()

            # Перемещение файла
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

            # Запись в таблицу
            try:
                sheet.append_row([
                    catalog_number,
                    description,
                    machine_type,
                    manufacturer,
                    analogs,
                    detail_description,
                    machine_model,
                    file_url,
                ])
            except Exception:
                print(f"[ERROR] Не удалось записать строку для {file_name}")
                traceback.print_exc()

            processed.append({
                "file": file_name,
                "catalog_number": catalog_number,
                "description": description,
            })

        time.sleep(1)

    return jsonify({
        "status": "success",
        "message": "Анализ завершён",
        "processed_count": len(processed),
        "processed": processed,
    })


if __name__ == "__main__":
    check_requirements()
    port = int(os.getenv("PORT", 5000))
    print(f"[INFO] Запуск Flask на порту {port}...")
    app.run(host="0.0.0.0", port=port, debug=True)
