import os
import traceback
import threading
from flask import Flask, jsonify, render_template
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import gspread
from openai import OpenAI
import time

app = Flask(__name__, template_folder="templates")

# Переменные окружения
REQUIRED_ENV_VARS = ["OPENAI_API_KEY", "SPREADSHEET_ID", "TO_ANALYZE_FOLDER_ID", "ANALYZED_FOLDER_ID"]

HEADERS = [
    "Catalog Number", "Description", "Machine Type", "Manufacturer", 
    "Analogs", "Detail Description", "Machine Model", "File URL"
]

# Прогресс
progress_log = []
progress_percent = 0
done_flag = False

# Проверка окружения
def check_requirements():
    print("[INFO] Проверка окружения...")
    missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
    if missing:
        print(f"[WARNING] Не заданы: {', '.join(missing)}")
    if not os.path.exists("credentials.json"):
        print("[ERROR] Нет файла credentials.json — Google API работать не будет.")

# Google API
def get_google_services():
    creds = Credentials.from_service_account_file(
        "credentials.json",
        scopes=["https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/spreadsheets"],
    )
    drive_service = build("drive", "v3", credentials=creds)
    sheet = gspread.authorize(creds).open_by_key(os.getenv("SPREADSHEET_ID")).sheet1

    try:
        existing = sheet.row_values(1)
        if not existing or existing != HEADERS:
            sheet.delete_rows(1)
            sheet.insert_row(HEADERS, 1)
    except Exception as e:
        print(f"[ERROR] Заголовки: {e}")

    return drive_service, sheet

# OpenAI
def get_openai_client():
    return OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# UI
@app.route("/")
def index():
    return render_template("index.html")

# API для прогресса
@app.route("/progress", methods=["GET"])
def get_progress():
    return jsonify({"percent": progress_percent, "log": progress_log, "done": done_flag})

# API для запуска анализа
@app.route("/start", methods=["POST"])
def start_analysis():
    global progress_log, progress_percent, done_flag
    progress_log = []
    progress_percent = 0
    done_flag = False

    threading.Thread(target=background_analysis).start()
    return jsonify({"status": "started"})

# Фоновый анализ
def background_analysis():
    global progress_log, progress_percent, done_flag
    drive, sheet = get_google_services()
    client = get_openai_client()
    TO_ANALYZE = os.getenv("TO_ANALYZE_FOLDER_ID")
    ANALYZED = os.getenv("ANALYZED_FOLDER_ID")

    try:
        results = drive.files().list(
            q=f"'{TO_ANALYZE}' in parents and mimeType contains 'image/'",
            fields="files(id, name, webContentLink)"
        ).execute()
        files = results.get("files", [])
    except Exception as e:
        progress_log.append(f"[ERROR] Google Drive: {e}")
        done_flag = True
        return

    batch_size = 5
    total = len(files)

    for i in range(0, total, batch_size):
        batch = files[i:i+batch_size]
        for f in batch:
            file_id, file_name = f["id"], f["name"]
            file_url = f.get("webContentLink") or f"https://drive.google.com/uc?export=download&id={file_id}"

            catalog_number = description = machine_type = manufacturer = analogs = detail_description = machine_model = "UNKNOWN"
            model_used = "gpt-4o-mini"

            try:
                progress_log.append(f"Анализ {file_name} моделью {model_used}")
                resp = client.chat.completions.create(
                    model=model_used,
                    messages=[
                        {"role": "system", "content": "Ты эксперт по запчастям строительной техники."},
                        {"role": "user", "content": [
                            {"type": "text", "text": (
                                "Проанализируй изображение и верни строго:\n"
                                "Catalog Number: <номер>\n"
                                "Description: <короткое описание>\n"
                                "Machine Type: <тип техники>\n"
                                "Manufacturer: <производитель>\n"
                                "Analogs: <артикулы через запятую>\n"
                                "Detail Description: <текстовое описание>\n"
                                "Machine Model: <модель>\n"
                            )},
                            {"type": "image_url", "image_url": {"url": file_url}}
                        ]}
                    ],
                    max_tokens=400,
                )

                answer = resp.choices[0].message.content.strip()
                for line in answer.splitlines():
                    if line.lower().startswith("catalog number"): catalog_number = line.split(":",1)[1].strip()
                    elif line.lower().startswith("description"): description = line.split(":",1)[1].strip()
                    elif line.lower().startswith("machine type"): machine_type = line.split(":",1)[1].strip()
                    elif line.lower().startswith("manufacturer"): manufacturer = line.split(":",1)[1].strip()
                    elif line.lower().startswith("analogs"): analogs = line.split(":",1)[1].strip()
                    elif line.lower().startswith("detail description"): detail_description = line.split(":",1)[1].strip()
                    elif line.lower().startswith("machine model"): machine_model = line.split(":",1)[1].strip()

            except Exception as e:
                progress_log.append(f"[ERROR] {file_name}: {e}")
                if "limit" in str(e).lower():
                    progress_log.append(f"[INFO] Лимит исчерпан. Переключаемся на GPT-3.5")
                    model_used = "gpt-3.5-mini"
                    continue

            # Перенос файла
            try:
                file_info = drive.files().get(fileId=file_id, fields="parents").execute()
                prev_parents = ",".join(file_info.get("parents"))
                drive.files().update(
                    fileId=file_id, addParents=ANALYZED, removeParents=prev_parents, fields="id, parents"
                ).execute()
            except Exception:
                progress_log.append(f"[ERROR] Не удалось переместить {file_name}")

            # Запись в Google Sheets
            try:
                sheet.append_row([catalog_number, description, machine_type, manufacturer, analogs, detail_description, machine_model, file_url])
            except Exception:
                progress_log.append(f"[ERROR] Не удалось записать {file_name}")

        progress_percent = int((i + len(batch)) / total * 100)
        time.sleep(1)  # небольшая пауза для безопасности

    progress_percent = 100
    done_flag = True
    progress_log.append("Анализ завершён!")
    

if __name__ == "__main__":
    check_requirements()
    port = int(os.getenv("PORT", 5000))
    print(f"[INFO] Запуск Flask на порту {port}...")
    app.run(host="0.0.0.0", port=port, debug=True)
