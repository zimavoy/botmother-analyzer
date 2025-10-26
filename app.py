import os
import traceback
import time
import threading
from flask import Flask, jsonify, send_from_directory, request
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import gspread
from openai import OpenAI

# --- app init ---
app = Flask(__name__, static_folder="static", static_url_path="/static")

# --- настройки и константы (не трогаем версии) ---
REQUIRED_ENV_VARS = ["OPENAI_API_KEY", "SPREADSHEET_ID", "TO_ANALYZE_FOLDER_ID", "ANALYZED_FOLDER_ID"]
HEADERS = [
    "Catalog Number", "Description", "Machine Type", "Manufacturer",
    "Analogs", "Detail Description", "Machine Model", "File URL",
]
BATCH_SIZE = 5
MODELS = ["gpt-4o-mini", "gpt-4o", "gpt-4o-mini-1", "gpt-3.5-turbo"]

# --- глобальные объекты прогресса (thread-safe с простым lock) ---
progress = {
    "state": "idle",   # idle | running | done | error
    "total": 0,
    "processed": 0,
    "last_message": "",
    "result_summary": "",  # will contain final message and sheet link
}
_progress_lock = threading.Lock()
_worker_thread = None

# --- вспомогательные функции из твоей стабильной версии ---
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
        scopes=["https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/spreadsheets"],
    )
    drive_service = build("drive", "v3", credentials=creds)
    sheet = gspread.authorize(creds).open_by_key(os.getenv("SPREADSHEET_ID")).sheet1

    # Проверка/обновление заголовков
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

def get_openai_client():
    return OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# разбор ответа модели (как было)
def parse_model_answer(answer_text):
    catalog_number = description = machine_type = manufacturer = analogs = detail_description = machine_model = "UNKNOWN"
    if not answer_text:
        return catalog_number, description, machine_type, manufacturer, analogs, detail_description, machine_model

    for line in answer_text.splitlines():
        ln = line.strip()
        lower = ln.lower()
        if lower.startswith("catalog number"):
            catalog_number = ln.split(":", 1)[1].strip() if ":" in ln else catalog_number
        elif lower.startswith("description"):
            description = ln.split(":", 1)[1].strip() if ":" in ln else description
        elif lower.startswith("machine type"):
            machine_type = ln.split(":", 1)[1].strip() if ":" in ln else machine_type
        elif lower.startswith("manufacturer"):
            manufacturer = ln.split(":", 1)[1].strip() if ":" in ln else manufacturer
        elif lower.startswith("analogs"):
            analogs = ln.split(":", 1)[1].strip() if ":" in ln else analogs
        elif lower.startswith("detail description"):
            detail_description = ln.split(":", 1)[1].strip() if ":" in ln else detail_description
        elif lower.startswith("machine model"):
            machine_model = ln.split(":", 1)[1].strip() if ":" in ln else machine_model

    return catalog_number, description, machine_type, manufacturer, analogs, detail_description, machine_model

# вызов модели с failover по моделям (как просил)
def analyze_with_failover(client, file_url):
    for model in MODELS:
        try:
            print(f"[INFO] Попытка анализа через модель: {model}")
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "Ты эксперт по запчастям строительной техники. Строго соблюдай формат."},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text",
                             "text": (
                                 "Проанализируй изображение детали и верни строго в формате:\n"
                                 "Catalog Number: <номер>\n"
                                 "Description: <короткое описание>\n"
                                 "Machine Type: <тип техники>\n"
                                 "Manufacturer: <производитель>\n"
                                 "Analogs: <артикулы аналогов через запятую>\n"
                                 "Detail Description: <текстовое описание детали>\n"
                                 "Machine Model: <модель техники>\n"
                                 "Строго 7 строк, без пояснений и лишнего текста."
                             )},
                            {"type": "image_url", "image_url": {"url": file_url}},
                        ]
                    }
                ],
                max_tokens=400,
            )
            text = resp.choices[0].message.content.strip()
            return text
        except Exception as e:
            print(f"[WARN] Модель {model} вернула ошибку: {e}")
            # лог стек-трейса ради диагностики, но продолжаем
            traceback.print_exc()
            time.sleep(0.5)
    # если все модели упали
    return None

# --- фоновой worker (запускает реальную обработку файлов) ---
def background_worker():
    global progress
    with _progress_lock:
        progress.update({"state": "running", "total": 0, "processed": 0, "last_message": ""})

    try:
        drive, sheet = get_google_services()
        client = get_openai_client()
    except Exception as e:
        with _progress_lock:
            progress["state"] = "error"
            progress["last_message"] = f"Init error: {e}"
        traceback.print_exc()
        return

    TO_ANALYZE = os.getenv("TO_ANALYZE_FOLDER_ID")
    ANALYZED = os.getenv("ANALYZED_FOLDER_ID")

    try:
        results = drive.files().list(
            q=f"'{TO_ANALYZE}' in parents and mimeType contains 'image/'",
            fields="files(id, name, webViewLink, webContentLink)",
        ).execute()
        files = results.get("files", [])
    except Exception as e:
        with _progress_lock:
            progress["state"] = "error"
            progress["last_message"] = f"Drive list error: {e}"
        traceback.print_exc()
        return

    total = len(files)
    with _progress_lock:
        progress["total"] = total
        progress["processed"] = 0
        progress["last_message"] = f"Found {total} files"

    processed_list = []

    # process in batches of BATCH_SIZE to avoid memory pressure
    for i in range(0, total, BATCH_SIZE):
        batch = files[i:i + BATCH_SIZE]
        print(f"[INFO] Обрабатываем пакет {i//BATCH_SIZE + 1} (size={len(batch)})")

        for f in batch:
            file_id = f["id"]
            file_name = f["name"]
            file_url = f.get("webContentLink") or f"https://drive.google.com/uc?export=download&id={file_id}"

            # defaults
            catalog_number = description = machine_type = manufacturer = analogs = detail_description = machine_model = "UNKNOWN"

            try:
                print(f"[INFO] Анализ {file_name} ({file_url}) ...")
                answer = analyze_with_failover(client, file_url)
                print(f"[DEBUG] Raw answer:\n{answer}")

                if answer:
                    (catalog_number, description, machine_type,
                     manufacturer, analogs, detail_description, machine_model) = parse_model_answer(answer)

            except Exception as e:
                print(f"[ERROR] Ошибка анализа {file_name}: {e}")
                traceback.print_exc()

            # try move file to analyzed (best-effort)
            try:
                file_info = drive.files().get(fileId=file_id, fields="parents").execute()
                prev_parents = ",".join(file_info.get("parents", []))
                drive.files().update(
                    fileId=file_id, addParents=ANALYZED, removeParents=prev_parents, fields="id, parents"
                ).execute()
            except Exception:
                print(f"[ERROR] Не удалось переместить {file_name}")
                traceback.print_exc()

            # write to sheet
            try:
                sheet.append_row([catalog_number, description, machine_type, manufacturer, analogs, detail_description, machine_model, file_url])
            except Exception:
                print(f"[ERROR] Не удалось записать строку для {file_name}")
                traceback.print_exc()

            processed_list.append({
                "file": file_name,
                "catalog_number": catalog_number,
                "description": description,
                "machine_type": machine_type,
                "manufacturer": manufacturer,
                "analogs": analogs,
                "detail_description": detail_description,
                "machine_model": machine_model,
            })

            with _progress_lock:
                progress["processed"] += 1
                progress["last_message"] = f"Processed {progress['processed']} / {progress['total']}"

        # small pause to let memory settle
        print("[INFO] Пауза между пакетами для экономии памяти...")
        time.sleep(1)

    # done
    with _progress_lock:
        progress["state"] = "done"
        progress["result_summary"] = f"Processed {len(processed_list)} files."
        progress["last_message"] = progress["result_summary"]

    print("[INFO] Background worker finished.")

# --- HTTP endpoints ---
@app.route("/", methods=["GET"])
def index():
    # serve static HTML UI
    return send_from_directory("static", "index.html")

@app.route("/start", methods=["POST"])
def start_analysis():
    global _worker_thread
    with _progress_lock:
        if progress["state"] == "running":
            return jsonify({"status": "running", "message": "Анализ уже запущен"}), 409
        # reset progress
        progress.update({"state": "idle", "total": 0, "processed": 0, "last_message": "", "result_summary": ""})

    # start worker thread
    _worker_thread = threading.Thread(target=background_worker, daemon=True)
    _worker_thread.start()
    return jsonify({"status": "started", "message": "Анализ запущен"}), 202

@app.route("/status", methods=["GET"])
def status():
    # return current progress
    with _progress_lock:
        data = dict(progress)  # shallow copy
    # add sheet link if done or even if not
    sheet_id = os.getenv("SPREADSHEET_ID")
    if sheet_id:
        data["sheet_url"] = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
    return jsonify(data)

# keep /analyze for compatibility — runs synchronously (deprecated for UI usage)
@app.route("/analyze", methods=["POST"])
def analyze_sync_endpoint():
    # call the background worker directly (blocking)
    # this keeps backward compatibility for scripts that POST /analyze
    background_worker()
    with _progress_lock:
        data = dict(progress)
    sheet_id = os.getenv("SPREADSHEET_ID")
    if sheet_id:
        data["sheet_url"] = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
    return jsonify(data)

# static assets (fallback)
@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory("static", filename)

# --- main ---
if __name__ == "__main__":
    check_requirements()
    port = int(os.getenv("PORT", 5000))
    print(f"[INFO] Запуск Flask на порту {port}...")
    app.run(host="0.0.0.0", port=port, debug=True)
