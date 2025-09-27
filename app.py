import os
from flask import Flask, request, jsonify

# === Flask ===
app = Flask(__name__)

# === Проверка обязательных переменных окружения ===
REQUIRED_ENV_VARS = [
    "OPENAI_API_KEY",
    "SPREADSHEET_ID",
    "TO_ANALYZE_FOLDER_ID",
    "ANALYZED_FOLDER_ID"
]

missing_vars = [var for var in REQUIRED_ENV_VARS if not os.getenv(var)]
if missing_vars:
    print(f"WARNING: Missing environment variables: {', '.join(missing_vars)}")
    print("Эндпоинт /analyze будет работать только после их настройки.")


# === Пинг для проверки сервиса ===
@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"status": "ok", "message": "pong"})


# === Функция: ленивое подключение Google API ===
def get_google_services():
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    import gspread

    creds_file = "credentials.json"
    if not os.path.exists(creds_file):
        raise FileNotFoundError("credentials.json не найден в контейнере!")

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

    return drive_service, sheet


# === Функция: ленивое подключение OpenAI ===
def get_openai_client():
    from openai import OpenAI
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY не задан!")
    return OpenAI(api_key=api_key)


# === Функции работы с Google Drive / Sheets / OpenAI ===
def move_file(drive_service, file_id, new_folder_id):
    file = drive_service.files().get(fileId=file_id, fields="parents").execute()
    previous_parents = ",".join(file.get("parents"))

    updated_file = drive_service.files().update(
        fileId=file_id,
        addParents=new_folder_id,
        removeParents=previous_parents,
        fields="id, parents"
    ).execute()

    return updated_file


def add_row_to_sheet(sheet, catalog_number, description, machine_type, photo_url):
    sheet.append_row([catalog_number, description, machine_type, photo_url])


def analyze_photo_openai(client, image_url):
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

    # Получаем список файлов для анализа
    results = drive_service.files().list(
        q=f"'{TO_ANALYZE_FOLDER_ID}' in parents and mimeType contains 'image/'",
        fields="files(id, name, webViewLink)"
    ).execute()

    files = results.get("files", [])
    processed = []

    for f in files:
        file_id = f["id"]
        file_url = f["webViewLink"]

        # Анализ через OpenAI
        try:
            analysis = analyze_photo_openai(openai_client, file_url)
            catalog_number, description, machine_type = analysis, "-", "-"  # Здесь можно парсить ответ
        except Exception as e:
            analysis = f"Ошибка анализа: {e}"
            catalog_number, description, machine_type = "-", "-", "-"

        # Переносим файл в папку analyzed
        try:
            move_file(drive_service, file_id, ANALYZED_FOLDER_ID)
        except Exception as e:
            print(f"Не удалось переместить файл {f['name']}: {e}")

        # Записываем в таблицу
        try:
            add_row_to_sheet(sheet, catalog_number, description, machine_type, file_url)
        except Exception as e:
            print(f"Не удалось добавить строку для {f['name']}: {e}")

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


# === Запуск Flask ===
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
