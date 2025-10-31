import os
import time
import gc
import psutil
import traceback
from flask import Flask, jsonify, render_template
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import gspread
from openai import OpenAI

app = Flask(__name__, static_folder="static", template_folder="templates")

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

BATCH_SIZE = 2
MEMORY_LIMIT_MB = 400


# --------------------------------------------------
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
            sheet.insert_row(HEADERS, 1)
        elif existing != HEADERS:
            sheet.delete_rows(1)
            sheet.insert_row(HEADERS, 1)
    except Exception as e:
        print(f"[ERROR] Ошибка при проверке заголовков: {e}")

    return drive_service, sheet


def get_openai_client():
    return OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


# --------------------------------------------------
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
        client = get_openai_client()
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500

    TO_ANALYZE = os.getenv("TO_ANALYZE_FOLDER_ID")
    ANALYZED = os.getenv("ANALYZED_FOLDER_ID")
    processed = []

    try:
        results = drive.files().list(
            q=f"'{TO_ANALYZE}' in parents and mimeType contains 'image/'",
            fields="files(id, name, webViewLink, webContentLink)"
        ).execute()
        files = results.get("files", [])
        print(f"[INFO] Найдено {len(files)} изображений для анализа")
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Google Drive недоступен"}), 500

    # Обработка пакетами по 2
    for i in range(0, len(files), BATCH_SIZE):
        batch = files[i:i + BATCH_SIZE]
        print(f"[INFO] Обрабатываем пакет {i//BATCH_SIZE + 1} ({len(batch)} файлов)")

        for f in batch:
            file_id, file_name = f["id"], f["name"]
            file_url = f.get("webContentLink") or f"https://drive.google.com/uc?export=download&id={file_id}"

            data = {
                "catalog_number": "UNKNOWN",
                "description": "UNKNOWN",
                "machine_type": "UNKNOWN",
                "manufacturer": "UNKNOWN",
                "analogs": "UNKNOWN",
                "detail_description": "UNKNOWN",
                "machine_model": "UNKNOWN",
            }

            models = ["gpt-4o-mini", "gpt-3.5-turbo"]
            model_used = None

            for model in models:
                try:
                    print(f"[INFO] Анализ {file_name} ({model})")
                    resp = client.chat.completions.create(
                        model=model,
                        messages=[
                            {"role": "system", "content": "Ты эксперт по запчастям строительной техники."},
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": (
                                            "Проанализируй изображение детали и верни строго в формате:\n"
                                            "Catalog Number: <номер>\n"
                                            "Description: <короткое описание>\n"
                                            "Machine Type: <тип техники>\n"
                                            "Manufacturer: <производитель>\n"
                                            "Analogs: <артикулы аналогов через запятую>\n"
                                            "Detail Description: <текстовое описание детали>\n"
                                            "Machine Model: <модель техники>\n\n"
                                            "Строго семь строк, без пояснений и лишнего текста."
                                        ),
                                    },
                                    {"type": "image_url", "image_url": {"url": file_url}},
                                ],
                            },
                        ],
                        max_tokens=400,
                    )
                    answer = resp.choices[0].message.content.strip()
                    model_used = model
                    print(f"[DEBUG] Ответ модели ({model}):\n{answer}")

                    for line in answer.splitlines():
                        line = line.strip()
                        if ":" not in line:
                            continue
                        key, val = line.split(":", 1)
                        key, val = key.lower().strip(), val.strip()
                        if "catalog number" in key:
                            data["catalog_number"] = val
                        elif "description" == key:
                            data["description"] = val
                        elif "machine type" in key:
                            data["machine_type"] = val
                        elif "manufacturer" in key:
                            data["manufacturer"] = val
                        elif "analogs" in key:
                            data["analogs"] = val
                        elif "detail description" in key:
                            data["detail_description"] = val
                        elif "machine model" in key:
                            data["machine_model"] = val
                    break  # успех, выходим из цикла моделей

                except Exception as e:
                    print(f"[WARNING] Ошибка модели {model}: {e}")
                    continue

            # Перемещение файла
            try:
                file_info = drive.files().get(fileId=file_id, fields="parents").execute()
                prev_parents = ",".join(file_info.get("parents"))
                drive.files().update(
                    fileId=file_id,
                    addParents=ANALYZED,
                    removeParents=prev_parents,
                    fields="id, parents"
                ).execute()
            except Exception:
                print(f"[ERROR] Не удалось переместить {file_name}")
                traceback.print_exc()

            # Запись в Google Sheets
            try:
                sheet.append_row([
                    data["catalog_number"],
                    data["description"],
                    data["machine_type"],
                    data["manufacturer"],
                    data["analogs"],
                    data["detail_description"],
                    data["machine_model"],
                    file_url,
                ])
            except Exception:
                print(f"[ERROR] Не удалось записать строку для {file_name}")
                traceback.print_exc()

            processed.append({
                "file": file_name,
                **data,
                "model_used": model_used,
            })

            # Контроль памяти
            used_mb = psutil.Process(os.getpid()).memory_info().rss / (1024 ** 2)
            print(f"[DEBUG] Память: {used_mb:.1f} MB")
            if used_mb > MEMORY_LIMIT_MB:
                print("[WARNING] Память превышает лимит, выполняется очистка...")
                gc.collect()
                time.sleep(2)

            del resp
            gc.collect()

    return jsonify({
        "status": "done",
        "processed_count": len(processed),
        "processed": processed,
    })


# --------------------------------------------------
if __name__ == "__main__":
    check_requirements()
    port = int(os.getenv("PORT", 5000))
    print(f"[INFO] Запуск Flask на порту {port}...")
    app.run(host="0.0.0.0", port=port)
