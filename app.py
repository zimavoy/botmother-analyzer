import os
import traceback
from flask import Flask, request, jsonify

app = Flask(__name__)

# === Переменные окружения ===
REQUIRED_ENV_VARS = [
    "OPENAI_API_KEY",
    "SPREADSHEET_ID",
    "TO_ANALYZE_FOLDER_ID",
    "ANALYZED_FOLDER_ID"
]

@app.before_first_request
def check_env():
    missing = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]
    if missing:
        print(f"[WARNING] Missing environment variables: {', '.join(missing)}")
    else:
        print("[INFO] All required environment variables found.")


# === Пинг для проверки сервиса ===
@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"status": "ok", "message": "pong"})


# === Ленивое подключение Google API ===
def get_google_services():
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
        import gspread

        creds_file = "credentials.json"
        if not os.path.exists(creds_file):
            raise FileNotFoundError("credentials.json не найден!")

        creds = Credentials.from_service_account_file(
            creds_file,
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
    except Exception as e:
        print("[ERROR] Ошибка подключения Google API:")
        traceback.print_exc()
        raise


# === Ленивое подключение OpenAI ===
def get_openai_client():
    try:
        from openai import OpenAI
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY не задан!")
        print("[INFO] OpenAI клиент создан успешно.")
        return OpenAI(api_key=api_key)
    except Exception as e:
        print("[ERROR] Ошибка подключения OpenAI:")
        traceback.print_exc()
        raise


# === Работа с Google Drive / Sheets / OpenAI ===
def move_file(drive_service, file_id, new_folder_id):
    try:
        file = drive_service.files().get(fileId=file_id, fields="parents").execute()
        previous_parents = ",".join(file.get("parents"))
        updated_file = drive_service.files().update(
            fileId=file_id,
            addParents=new_folder_id,
            removeParents=previous_parents,
            fields="id, parents"
        ).execute()
        return updated_file
    except Exception:
        print(f"[ERROR] Ошибка при перемещении файла {file_id}:")
        traceback.print_exc()
        raise


def add_row_to_sheet(sheet, catalog_number, description, machine_type, photo_url):
    try:
        sheet.append_row([catalog_number, description, machine_type, photo_url])
    except Exception:
        print(f"[ERROR] Ошибка при добавлении строки для фото {photo_url}:")
        traceback.print_exc()
        raise


def analyze_photo_openai(client, image_url):
    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Ты эксперт по запчастям строительной техники."},
                {"role": "user", "content": [
                    {"type": "text", "text": "Определи каталожный номер, описание и для какой техники подходит эта деталь."},
                    {"type": "image_url", "image_url": {"url": image_url}}
                ]}
            ],
            max_tokens=300
        )
        return response.choices[0].message.content.strip()
    except Exception:
        print(f"[ERROR] Ошибка анализа фото {image_url}:")
        traceback.print_exc()
        raise


# === Эндпоинт анализа ===
@app.route("/analyze", methods=["POST"])
def analyze():
    try:
        drive_service, sheet = get_google_services()
        openai_client = get_openai_client()
    except Exception as e:
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

        try:
            analysis = analyze_photo_openai(openai_client, file_url)
            catalog_number, description, machine_type = analysis, "-", "-"
        except Exception:
            catalog_number, description, machine_type = "-", "-", "-"

        try:
            move_file(drive_service, file_id, ANALYZED_FOLDER_ID)
        except Exception:
            pass

        try:
            add_row_to_sheet(sheet, catalog_number, description, machine_type, file_url)
        except Exception:
            pass

        processed.append({
            "file": f["name"],
            "catalog_number": catalog_number,
            "description": description,
            "machine_type": machine_type
        })

    return jsonify({
        "status": "done",
        "processed_count": len(processed),
        "processed": processed
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print(f"[INFO] Flask запускается на порту {port}...")
    app.run(host="0.0.0.0", port=port, debug=True)
