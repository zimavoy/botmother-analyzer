import os
import traceback
from datetime import datetime, timedelta
from flask import Flask, jsonify, send_from_directory
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import gspread
from openai import OpenAI
import requests
import time

app = Flask(__name__, static_folder="static", static_url_path="/static")

REQUIRED_ENV_VARS = ["OPENAI_API_KEY", "SPREADSHEET_ID", "TO_ANALYZE_FOLDER_ID", "ANALYZED_FOLDER_ID"]

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

# ===== Лимиты кэша =====
last_limit_check = None
cached_limits = {}

# ===== Проверка окружения =====
def check_requirements():
    print("[INFO] Проверка окружения...")
    missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
    if missing:
        print(f"[WARNING] Не заданы: {', '.join(missing)}")
    if not os.path.exists("credentials.json"):
        print("[ERROR] Нет файла credentials.json — Google API работать не будет.")

# ===== Google API =====
def get_google_services():
    creds = Credentials.from_service_account_file(
        "credentials.json",
        scopes=["https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/spreadsheets"],
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
    except Exception as e:
        print(f"[ERROR] Ошибка проверки заголовков: {e}")

    return drive_service, sheet

# ===== OpenAI API =====
def get_openai_client(model="gpt-4o-mini"):
    return OpenAI(api_key=os.getenv("OPENAI_API_KEY")), model

def get_model_limits():
    global last_limit_check, cached_limits
    try:
        if last_limit_check and datetime.now() - last_limit_check < timedelta(seconds=60):
            return cached_limits

        api_key = os.getenv("OPENAI_API_KEY")
        headers = {"Authorization": f"Bearer {api_key}"}
        resp = requests.get("https://api.openai.com/v1/dashboard/billing/credit_grants", headers=headers, timeout=10)

        if resp.status_code != 200:
            return {"status": "error", "message": f"Ошибка запроса: {resp.status_code}"}

        data = resp.json()
        total = data.get("total_granted", 0)
        used = data.get("total_used", 0)
        remaining = data.get("total_available", 0)

        cached_limits = {
            "status": "ok",
            "total_granted": round(total, 2),
            "total_used": round(used, 2),
            "remaining": round(remaining, 2),
            "updated": datetime.now().strftime("%H:%M:%S")
        }
        last_limit_check = datetime.now()
        return cached_limits

    except Exception as e:
        print(f"[ERROR] Ошибка получения лимитов: {e}")
        return {"status": "error", "message": str(e)}

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/model_limits", methods=["GET"])
def model_limits():
    return jsonify(get_model_limits())

@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"status": "ok", "message": "pong"})

@app.route("/analyze", methods=["POST"])
def analyze():
    try:
        drive, sheet = get_google_services()
        client, model = get_openai_client()
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

    print(f"[INFO] Найдено {len(files)} файлов для анализа")

    batch_size = 5
    for i in range(0, len(files), batch_size):
        batch = files[i:i+batch_size]
        print(f"[INFO] Обработка пакета {i//batch_size + 1} из {len(files)//batch_size + 1}")

        for f in batch:
            file_id, file_name = f["id"], f["name"]
            file_url = f.get("webContentLink") or f"https://drive.google.com/uc?export=download&id={file_id}"

            catalog_number = description = machine_type = manufacturer = analogs = detail_description = machine_model = "UNKNOWN"

            try:
                limits = get_model_limits()
                if limits.get("remaining", 0) < 0.5:
                    print("[WARNING] Лимит почти исчерпан — переключаемся на GPT-3.5")
                    client, model = get_openai_client("gpt-3.5-turbo")

                print(f"[INFO] Анализ {file_name} ({model}) ...")

                resp = client.chat.completions.create(
                    model=model,
                    messages=[
                        {"role": "system", "content": "Ты эксперт по запчастям строительной техники."},
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": (
                                    "Проанализируй изображение детали и верни строго в формате:\n"
                                    "Catalog Number: <номер>\n"
                                    "Description: <короткое описание>\n"
                                    "Machine Type: <тип техники>\n"
                                    "Manufacturer: <производитель>\n"
                                    "Analogs: <артикулы аналогов через запятую>\n"
                                    "Detail Description: <текстовое описание детали>\n\n"
                                    "Machine Model: <текстовое описание модели>\n\n"
                                    "Строго семь строк, без пояснений и лишнего текста. Ответ пиши на русском языке, где это возможно"
                                )},
                                {"type": "image_url", "image_url": {"url": file_url}},
                            ],
                        },
                    ],
                    max_tokens=400,
                )

                answer = resp.choices[0].message.content.strip()
                print(f"[DEBUG] Ответ модели:\n{answer}")

                for line in answer.splitlines():
                    key, _, value = line.partition(":")
                    key = key.lower().strip()
                    value = value.strip()
                    if key == "catalog number": catalog_number = value
                    elif key == "description": description = value
                    elif key == "machine type": machine_type = value
                    elif key == "manufacturer": manufacturer = value
                    elif key == "analogs": analogs = value
                    elif key == "detail description": detail_description = value
                    elif key == "machine model": machine_model = value

            except Exception as e:
                print(f"[ERROR] Ошибка анализа {file_name}: {e}")
                traceback.print_exc()

            try:
                file_info = drive.files().get(fileId=file_id, fields="parents").execute()
                prev_parents = ",".join(file_info.get("parents"))
                drive.files().update(
                    fileId=file_id, addParents=ANALYZED, removeParents=prev_parents, fields="id, parents"
                ).execute()
            except Exception:
                print(f"[ERROR] Не удалось переместить {file_name}")
                traceback.print_exc()

            try:
                sheet.append_row([catalog_number, description, machine_type, manufacturer,
                                  analogs, detail_description, machine_model, file_url])
            except Exception:
                print(f"[ERROR] Не удалось записать строку для {file_name}")
                traceback.print_exc()

            processed.append({
                "file": file_name,
                "catalog_number": catalog_number,
                "description": description,
                "machine_type": machine_type,
                "manufacturer": manufacturer,
                "analogs": analogs,
                "detail_description": detail_description,
                "machine model": machine_model,
            })

        time.sleep(3)

    return jsonify({"status": "done", "processed_count": len(processed), "processed": processed})

if __name__ == "__main__":
    check_requirements()
    port = int(os.getenv("PORT", 5000))
    print(f"[INFO] Запуск Flask на порту {port}...")
    app.run(host="0.0.0.0", port=port, debug=True)
