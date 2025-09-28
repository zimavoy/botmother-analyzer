import os
import traceback
from flask import Flask, jsonify
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import gspread
from openai import OpenAI

app = Flask(__name__)

REQUIRED_ENV_VARS = ["OPENAI_API_KEY", "SPREADSHEET_ID", "TO_ANALYZE_FOLDER_ID", "ANALYZED_FOLDER_ID"]

HEADERS = [
    "Catalog Number",
    "Description",
    "Machine Type",
    "Manufacturer",
    "Analogs",
    "Detail Description",
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

    for f in files:
        file_id, file_name = f["id"], f["name"]

        file_url = f.get("webContentLink") or f"https://drive.google.com/uc?export=download&id={file_id}"

        catalog_number = description = machine_type = manufacturer = analogs = detail_description = "UNKNOWN"

        try:
            print(f"[INFO] Анализ {file_name} ({file_url}) ...")
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
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
                                    "Detail Description: <текстовое описание детали>\n\n"
                                    "Строго шесть строк, без пояснений и лишнего текста."
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

            for line in answer.splitlines():
                if line.lower().startswith("catalog number"):
                    catalog_number = line.split(":", 1)[1].strip()
                elif line.lower().startswith("description"):
                    description = line.split(":", 1)[1].strip()
                elif line.lower().startswith("machine type"):
                    machine_type = line.split(":", 1)[1].strip()
                elif line.lower().startswith("manufacturer"):
                    manufacturer = line.split(":", 1)[1].strip()
                elif line.lower().startswith("analogs"):
                    analogs = line.split(":", 1)[1].strip()
                elif line.lower().startswith("detail description"):
                    detail_description = line.split(":", 1)[1].strip()

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
            sheet.append_row([catalog_number, description, machine_type, manufacturer, analogs, detail_description, file_url])
        except Exception:
            print(f"[ERROR] Не удалось записать строку для {file_name}")
            traceback.print_exc()

        processed.append(
            {
                "file": file_name,
                "catalog_number": catalog_number,
                "description": description,
                "machine_type": machine_type,
                "manufacturer": manufacturer,
                "analogs": analogs,
                "detail_description": detail_description,
            }
        )

    return jsonify({"status": "done", "processed_count": len(processed), "processed": processed})

if __name__ == "__main__":
    check_requirements()
    port = int(os.getenv("PORT", 5000))
    print(f"[INFO] Запуск Flask на порту {port}...")
    app.run(host="0.0.0.0", port=port, debug=True)
