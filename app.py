import os
import traceback
from flask import Flask, jsonify, send_from_directory
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import gspread
from openai import OpenAI

app = Flask(__name__, static_folder="static", static_url_path="")

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
    except Exception as e:
        print(f"[ERROR] Ошибка при проверке заголовков: {e}")

    return drive_service, sheet

def get_openai_client():
    return OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")

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
            fields="files(id, name, webViewLink, webContentLink)",
        ).execute()
        files = results.get("files", [])
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Google Drive недоступен"}), 500

    batch_size = 5
    total = len(files)
    remaining = total

    for i in range(0, total, batch_size):
        batch = files[i:i + batch_size]
        print(f"[INFO] Анализ партии {i // batch_size + 1} из {len(batch)} файлов")

        for f in batch:
            file_id, file_name = f["id"], f["name"]
            file_url = f.get("webContentLink") or f"https://drive.google.com/uc?export=download&id={file_id}"
            fields = ["UNKNOWN"] * 7
            models = ["gpt-4o-mini", "gpt-4-turbo", "gpt-3.5-turbo"]

            for model in models:
                try:
                    print(f"[INFO] Анализ {file_name} с моделью {model}")
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
                                            "Machine Model: <текстовое описание модели>"
                                        ),
                                    },
                                    {"type": "image_url", "image_url": {"url": file_url}},
                                ],
                            },
                        ],
                        max_tokens=400,
                    )
                    answer = resp.choices[0].message.content.strip()
                    print(f"[DEBUG] Ответ модели:\n{answer}")

                    parsed = {}
                    for line in answer.splitlines():
                        if ":" in line:
                            k, v = line.split(":", 1)
                            parsed[k.strip()] = v.strip()

                    fields = [
                        parsed.get("Catalog Number", "UNKNOWN"),
                        parsed.get("Description", "UNKNOWN"),
                        parsed.get("Machine Type", "UNKNOWN"),
                        parsed.get("Manufacturer", "UNKNOWN"),
                        parsed.get("Analogs", "UNKNOWN"),
                        parsed.get("Detail Description", "UNKNOWN"),
                        parsed.get("Machine Model", "UNKNOWN"),
                    ]
                    break
                except Exception as e:
                    print(f"[WARNING] Ошибка анализа {file_name} с {model}: {e}")
                    continue

            try:
                sheet.append_row(fields + [file_url])
            except Exception:
                print(f"[ERROR] Не удалось записать строку для {file_name}")

            processed.append({
                "file": file_name,
                "model_used": model,
                "fields": fields,
            })
            remaining -= 1
            print(f"[PROGRESS] Осталось: {remaining}/{total}")

    return jsonify({"status": "done", "processed_count": len(processed), "processed": processed})

if __name__ == "__main__":
    check_requirements()
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
