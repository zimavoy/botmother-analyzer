import os
import traceback
import threading
import time
from flask import Flask, jsonify, render_template
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import gspread
from openai import OpenAI

app = Flask(__name__, static_url_path='', static_folder='static', template_folder='templates')

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

# Хранилище лимитов
model_limits = {
    "model": "gpt-4o-mini",
    "remaining_requests": None,
    "remaining_tokens": None,
    "limit_reset": None
}

progress_data = {"total": 0, "processed": 0, "status": "idle"}


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
            "https://www.googleapis.com/auth/spreadsheets"
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
        print(f"[ERROR] Ошибка проверки заголовков: {e}")

    return drive_service, sheet


def get_openai_client():
    return OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/ping")
def ping():
    return jsonify({"status": "ok"})


@app.route("/progress")
def get_progress():
    return jsonify(progress_data)


@app.route("/limits")
def get_limits():
    return jsonify(model_limits)


@app.route("/analyze", methods=["POST"])
def analyze():
    thread = threading.Thread(target=run_analysis)
    thread.start()
    return jsonify({"status": "started"})


def run_analysis():
    global model_limits
    progress_data.update({"status": "running", "processed": 0})

    try:
        drive, sheet = get_google_services()
        client = get_openai_client()

        TO_ANALYZE = os.getenv("TO_ANALYZE_FOLDER_ID")
        ANALYZED = os.getenv("ANALYZED_FOLDER_ID")

        results = drive.files().list(
            q=f"'{TO_ANALYZE}' in parents and mimeType contains 'image/'",
            fields="files(id, name, webViewLink, webContentLink)",
        ).execute()
        files = results.get("files", [])
        progress_data["total"] = len(files)

        batch_size = 5
        for i in range(0, len(files), batch_size):
            batch = files[i:i + batch_size]
            for f in batch:
                file_id, file_name = f["id"], f["name"]
                file_url = f.get("webContentLink") or f"https://drive.google.com/uc?export=download&id={file_id}"

                try:
                    resp = client.chat.completions.create(
                        model=model_limits["model"],
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
                                            "Machine Model: <модель техники>"
                                        ),
                                    },
                                    {"type": "image_url", "image_url": {"url": file_url}},
                                ],
                            },
                        ],
                        max_tokens=400,
                    )

                    # === обновляем лимиты ===
                    headers = getattr(resp, "response", {}).headers if hasattr(resp, "response") else {}
                    model_limits.update({
                        "model": model_limits["model"],
                        "remaining_requests": headers.get("x-ratelimit-remaining-requests"),
                        "remaining_tokens": headers.get("x-ratelimit-remaining-tokens"),
                        "limit_reset": headers.get("x-ratelimit-reset-requests"),
                    })

                    answer = resp.choices[0].message.content.strip()
                    parsed = parse_answer(answer)
                    sheet.append_row(parsed + [file_url])

                    drive.files().update(
                        fileId=file_id,
                        addParents=os.getenv("ANALYZED_FOLDER_ID"),
                        removeParents=os.getenv("TO_ANALYZE_FOLDER_ID"),
                        fields="id, parents"
                    ).execute()

                except Exception as e:
                    print(f"[ERROR] Ошибка анализа {file_name}: {e}")

                progress_data["processed"] += 1
                time.sleep(1)

    except Exception as e:
        traceback.print_exc()
    finally:
        progress_data["status"] = "done"


def parse_answer(answer):
    fields = {
        "Catalog Number": "UNKNOWN",
        "Description": "UNKNOWN",
        "Machine Type": "UNKNOWN",
        "Manufacturer": "UNKNOWN",
        "Analogs": "UNKNOWN",
        "Detail Description": "UNKNOWN",
        "Machine Model": "UNKNOWN",
    }
    for line in answer.splitlines():
        for key in fields.keys():
            if line.lower().startswith(key.lower()):
                fields[key] = line.split(":", 1)[1].strip()
    return list(fields.values())


if __name__ == "__main__":
    check_requirements()
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
