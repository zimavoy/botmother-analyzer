import os
from flask import Flask, request, jsonify
from openai import OpenAI
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# === Flask ===
app = Flask(__name__)

# === Конфигурация ===
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
TO_ANALYZE_FOLDER_ID = os.getenv("TO_ANALYZE_FOLDER_ID")
ANALYZED_FOLDER_ID = os.getenv("ANALYZED_FOLDER_ID")

# === Google авторизация ===
creds = Credentials.from_service_account_file(
    "credentials.json",
    scopes=["https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/spreadsheets"]
)

drive_service = build("drive", "v3", credentials=creds)
sheets_client = gspread.authorize(creds)
sheet = sheets_client.open_by_key(SPREADSHEET_ID).sheet1

# === OpenAI клиент ===
client = OpenAI(api_key=OPENAI_API_KEY)


# --- Функция: анализ фото ---
def analyze_photo_openai(image_url):
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


# --- Функция: перенос файла в другую папку на Google Drive ---
def move_file(file_id, new_folder_id):
    file = drive_service.files().get(fileId=file_id, fields="parents").execute()
    previous_parents = ",".join(file.get("parents"))

    updated_file = drive_service.files().update(
        fileId=file_id,
        addParents=new_folder_id,
        removeParents=previous_parents,
        fields="id, parents"
    ).execute()

    return updated_file


# --- Функция: добавить строку в Google Sheets ---
def add_row_to_sheet(catalog_number, description, machine_type, photo_url):
    sheet.append_row([catalog_number, description, machine_type, photo_url])


# === Маршруты ===
@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"status": "ok", "message": "pong"})


@app.route("/analyze", methods=["POST"])
def analyze():
    """
    Эндпоинт, который перебирает все фото в папке TO_ANALYZE,
    отправляет их в OpenAI, переносит в ANALYZED и пишет данные в Google Sheets.
    """
    # 1. Получаем список файлов
    results = drive_service.files().list(
        q=f"'{TO_ANALYZE_FOLDER_ID}' in parents and mimeType contains 'image/'",
        fields="files(id, name, webViewLink)"
    ).execute()

    files = results.get("files", [])
    processed = []

    for f in files:
        file_id = f["id"]
        file_url = f["webViewLink"]

        # 2. Анализируем фото через OpenAI
        try:
            analysis = analyze_photo_openai(file_url)
            # Здесь можно парсить ответ в JSON, пока просто сохраняем строку
            catalog_number, description, machine_type = analysis, "-", "-"
        except Exception as e:
            analysis = f"Ошибка анализа: {e}"
            catalog_number, description, machine_type = "-", "-", "-"

        # 3. Переносим файл в папку ANALYZED
        move_file(file_id, ANALYZED_FOLDER_ID)

        # 4. Записываем в Google Sheets
        add_row_to_sheet(catalog_number, description, machine_type, file_url)

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
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
