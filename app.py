import os
import traceback
from flask import Flask, jsonify

app = Flask(__name__)

# --- Обязательные переменные окружения ---
REQUIRED_ENV_VARS = [
    "OPENAI_API_KEY",
    "SPREADSHEET_ID",
    "TO_ANALYZE_FOLDER_ID",
    "ANALYZED_FOLDER_ID"
]

def check_requirements():
    print("[INFO] Проверка требований перед запуском приложения...")

    # Проверка переменных окружения
    missing_vars = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]
    if missing_vars:
        print(f"[WARNING] Отсутствуют переменные окружения: {', '.join(missing_vars)}")
    else:
        print("[INFO] Все обязательные переменные окружения заданы.")

    # Проверка credentials.json
    if not os.path.exists("credentials.json"):
        print("[WARNING] credentials.json не найден! Google API не будет работать до его добавления.")
    else:
        print("[INFO] credentials.json найден.")

# --- /ping эндпоинт ---
@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"status": "ok", "message": "pong"})

# --- Ленивое подключение Google API ---
def get_google_services():
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    import gspread

    creds_path = "credentials.json"
    if not os.path.exists(creds_path):
        raise FileNotFoundError("credentials.json не найден!")

    creds = Credentials.from_service_account_file(
        creds_path,
        scopes=[
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets"
        ]
    )

    drive_service = build("drive", "v3", credentials=creds)
    sheets_client = gspread.authorize(creds)
    sheet = sheets_client.open_by_key(os.getenv("SPREADSHEET_ID")).sheet1

    print("[INFO] Google API подключены успешно.")
    return drive_service, sheet

# --- Ленивое подключение OpenAI ---
def get_openai_client():
    from openai import OpenAI
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY не задан!")
    print("[INFO] OpenAI клиент готов.")
    return OpenAI(api_key=api_key)

# --- Эндпоинт /analyze ---
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
        print("[ERROR] Ошибка получения списка файлов из Google Drive:")
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Не удалось получить файлы из Google Drive"}), 500

    for f in files:
        file_id = f["id"]
        file_url = f["webViewLink"]
        catalog_number = description = machine_type = "UNKNOWN"

        # --- Анализ фото через OpenAI ---
        try:
            response = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "Ты эксперт по запчастям строительной техники."},
                    {"role": "user", "content": [
                        {"type": "text", "text": "Определи каталожный номер, описание и для какой техники подходит эта деталь."},
                        {"type": "image_url", "image_url": {"url": file_url}}
                    ]}
                ],
                max_tokens=300
            )
            result_text = response.choices[0].message.content.strip()
            catalog_number, description, machine_type = result_text, "-", "-"
        except Exception:
            print(f"[ERROR] Ошибка анализа фото {file_url}:")
            traceback.print_exc()

        # --- Перенос файла в analyzed ---
        try:
            file_info = drive_service.files().get(fileId=file_id, fields="parents").execute()
            previous_parents = ",".join(file_info.get("parents"))
            drive_service.files().update(
                fileId=file_id,
                addParents=ANALYZED_FOLDER_ID,
                removeParents=previous_parents,
                fields="id, parents"
            ).execute()
        except Exception:
            print(f"[ERROR] Ошибка перемещения файла {file_id}:")
            traceback.print_exc()

        # --- Добавление строки в Google Sheets ---
        try:
            sheet.append_row([catalog_number, description, machine_type, file_url])
        except Exception:
            print(f"[ERROR] Ошибка добавления строки для {file_url}:")
            traceback.print_exc()

        processed.append({
            "file": f["name"],
            "catalog_number": catalog_number,
            "description": description,
            "machine_type": machine_type
        })

    return jsonify({"status": "done", "processed_count": len(processed), "processed": processed})

# --- Запуск Flask ---
if __name__ == "__main__":
    check_requirements()  # ✅ теперь вызываем напрямую при старте
    port = int(os.getenv("PORT", 5000))
    print(f"[INFO] Flask запускается на порту {port}...")
    app.run(host="0.0.0.0", port=port, debug=True)
