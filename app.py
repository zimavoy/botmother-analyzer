import os
import traceback
from flask import Flask, jsonify, request

app = Flask(__name__)

# --- Обязательные переменные окружения ---
REQUIRED_ENV_VARS = [
    "OPENAI_API_KEY",
    "SPREADSHEET_ID",
    "TO_ANALYZE_FOLDER_ID",
    "ANALYZED_FOLDER_ID"
]

def check_requirements():
    print("[INFO] Проверка переменных окружения...")
    missing = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]
    if missing:
        print(f"[WARNING] Отсутствуют переменные: {missing}")
    else:
        print("[INFO] Все обязательные переменные заданы.")
    if not os.path.exists("credentials.json"):
        print("[WARNING] credentials.json не найден!")
    else:
        print("[INFO] credentials.json найден.")

# --- /ping ---
@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"status": "ok", "message": "pong"})

# --- Google API ---
def get_google_services():
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    import gspread

    creds = Credentials.from_service_account_file(
        "credentials.json",
        scopes=[
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets"
        ]
    )

    drive_service = build("drive", "v3", credentials=creds)
    sheets_client = gspread.authorize(creds)
    sheet = sheets_client.open_by_key(os.getenv("SPREADSHEET_ID")).sheet1
    print("[INFO] Google API подключены")
    return drive_service, sheet

# --- OpenAI Vision ---
def get_openai_client():
    from openai import OpenAI
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY не задан!")
    print("[INFO] OpenAI клиент готов")
    return OpenAI(api_key=api_key)  # ✅ только api_key

def analyze_image(openai_client, file_url):
    """
    Отправляет изображение в OpenAI Vision и получает текстовый результат.
    """
    try:
        resp = openai_client.responses.create(
            model="gpt-4.1-mini",
            input=[
                {"role": "user", "content": "Ты эксперт по запчастям спецтехники. "
                                            "Определи каталожный номер, описание и технику для этой детали."},
                {"role": "user", "content": {"type": "input_image", "image_url": file_url}}
            ]
        )

        result_text = resp.output_text.strip()
        print(f"[INFO] OpenAI ответ для {file_url}: {result_text}")

        catalog_number = description = machine_type = "UNKNOWN"
        lines = result_text.split("\n")
        for line in lines:
            line_lower = line.lower()
            if "каталог" in line_lower or "номер" in line_lower:
                catalog_number = line.split(":")[-1].strip()
            elif "описание" in line_lower:
                description = line.split(":")[-1].strip()
            elif "техника" in line_lower or "машина" in line_lower:
                machine_type = line.split(":")[-1].strip()

        return catalog_number, description, machine_type

    except Exception as e:
        print(f"[ERROR] Ошибка анализа изображения {file_url}: {e}")
        return "UNKNOWN", "UNKNOWN", "UNKNOWN"

# --- /analyze ---
@app.route("/analyze", methods=["POST"])
def analyze():
    try:
        drive_service, sheet = get_google_services()
        openai_client = get_openai_client()
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500

    TO_ANALYZE_FOLDER_ID = os.getenv("TO_ANALYZE_FOLDER_ID")
    ANALYZED_FOLDER_ID = os.getenv("ANALYZED_FOLDER_ID")
    processed = []

    try:
        results = drive_service.files().list(
            q=f"'{TO_ANALYZE_FOLDER_ID}' in parents and mimeType contains 'image/'",
            fields="files(id, name, webViewLink)"
        ).execute()
        files = results.get("files", [])
    except Exception:
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Ошибка получения файлов из Google Drive"}), 500

    for f in files:
        file_id = f["id"]
        file_url = f["webViewLink"]

        catalog_number, description, machine_type = analyze_image(openai_client, file_url)

        # Перемещаем файл в analyzed
        try:
            file_info = drive_service.files().get(fileId=file_id, fields="parents").execute()
            prev_parents = ",".join(file_info.get("parents"))
            drive_service.files().update(
                fileId=file_id,
                addParents=ANALYZED_FOLDER_ID,
                removeParents=prev_parents,
                fields="id, parents"
            ).execute()
            print(f"[INFO] Файл {f['name']} перемещен в analyzed")
        except Exception:
            traceback.print_exc()

        # Добавляем в Google Sheets
        try:
            sheet.append_row([catalog_number, description, machine_type, file_url])
            print(f"[INFO] Строка для {f['name']} добавлена в Google Sheets")
        except Exception:
            traceback.print_exc()

        processed.append({
            "file": f["name"],
            "catalog_number": catalog_number,
            "description": description,
            "machine_type": machine_type
        })

    return jsonify({"status": "done", "processed_count": len(processed), "processed": processed})

# --- Запуск ---
if __name__ == "__main__":
    check_requirements()
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
