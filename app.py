import os
import gc
import json
import time
import traceback
import psutil
from flask import Flask, jsonify
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import gspread
from openai import OpenAI

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

# --- Обязательные переменные окружения ---
REQUIRED_ENV_VARS = [
    "OPENAI_API_KEY",
    "SPREADSHEET_ID",
    "TO_ANALYZE_FOLDER_ID",
    "ANALYZED_FOLDER_ID",
]


# --- Проверка окружения ---
def check_requirements():
    print("[INFO] Проверка окружения...")
    missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
    if missing:
        print(f"[WARN] Нет переменных: {', '.join(missing)}")
    if not os.path.exists("credentials.json"):
        print("[WARN] credentials.json отсутствует!")
    else:
        print("[INFO] credentials.json найден.")


# --- Подключение Google API ---
def get_google_services():
    creds = Credentials.from_service_account_file(
        "credentials.json",
        scopes=[
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets",
        ],
    )
    drive = build("drive", "v3", credentials=creds)
    sheet = gspread.authorize(creds).open_by_key(os.getenv("SPREADSHEET_ID")).sheet1
    return drive, sheet


# --- Подключение OpenAI ---
def get_openai_client():
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY не задан")
    return OpenAI(api_key=api_key)


# --- Проверка живости ---
@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"status": "ok", "message": "pong"})


# --- Основной эндпоинт ---
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
    processed_total = []

    # --- Получаем список всех изображений ---
    try:
        files = (
            drive.files()
            .list(
                q=f"'{TO_ANALYZE}' in parents and mimeType contains 'image/'",
                fields="files(id, name, webViewLink)",
                pageSize=1000
            )
            .execute()
            .get("files", [])
        )
        print(f"[INFO] Найдено {len(files)} файлов для анализа.")
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Ошибка получения файлов"}), 500

    # --- Пакетная обработка ---
    batch_size = 5
    for batch_index in range(0, len(files), batch_size):
        batch = files[batch_index: batch_index + batch_size]
        print(f"[INFO] Обработка пакета {batch_index // batch_size + 1} ({len(batch)} файлов)")

        processed_batch = []

        for f in batch:
            file_id = f["id"]
            file_name = f["name"]
            file_url = f["webViewLink"]

            print(f"[INFO] Анализ файла: {file_name}")
            result = {
                "file": file_name,
                "catalog_number": "UNKNOWN",
                "description": "UNKNOWN",
                "manufacturer": "UNKNOWN",
                "analogs": "UNKNOWN",
                "machine_type": "UNKNOWN",
                "model": "UNKNOWN"
            }

            try:
                response = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "Ты — эксперт по запчастям строительной техники. "
                                "На вход даётся фото запчасти. "
                                "Нужно определить: "
                                "1) Каталожный номер (catalog_number), "
                                "2) Описание детали (description), "
                                "3) Производителя (manufacturer), "
                                "4) Аналогичные артикулы (analogs), "
                                "5) Тип техники (machine_type), "
                                "6) Модель машины (model). "
                                "Ответ верни строго в JSON без текста вокруг."
                            ),
                        },
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "Проанализируй эту деталь и верни JSON."},
                                {"type": "image_url", "image_url": {"url": file_url}},
                            ],
                        },
                    ],
                    max_tokens=500,
                )

                reply = response.choices[0].message.content.strip()
                print(f"[DEBUG] Ответ от OpenAI: {reply}")

                try:
                    data = json.loads(reply)
                    for key in result.keys():
                        result[key] = data.get(key, result[key])
                except Exception:
                    print(f"[WARN] Не удалось распарсить ответ как JSON: {reply[:100]}...")

            except Exception:
                print(f"[ERROR] Ошибка анализа {file_name}")
                traceback.print_exc()

            # --- Перемещение в analyzed ---
            try:
                parents = drive.files().get(fileId=file_id, fields="parents").execute().get("parents", [])
                drive.files().update(
                    fileId=file_id,
                    addParents=ANALYZED,
                    removeParents=",".join(parents),
                    fields="id, parents"
                ).execute()
            except Exception:
                print(f"[ERROR] Не удалось переместить {file_name}")
                traceback.print_exc()

            # --- Добавляем в Google Sheets ---
            try:
                sheet.append_row([
                    result["catalog_number"],
                    result["description"],
                    result["manufacturer"],
                    result["analogs"],
                    result["machine_type"],
                    result["model"],
                    file_url,
                ])
                print(f"[INFO] {file_name} добавлен в таблицу.")
            except Exception:
                print(f"[ERROR] Ошибка записи {file_name}")
                traceback.print_exc()

            processed_batch.append(result)

            # Очистка памяти после каждого файла
            gc.collect()
            mem = psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
            print(f"[MEM] RAM: {mem:.2f} MB")

        processed_total.extend(processed_batch)

        print(f"[INFO] Пакет {batch_index // batch_size + 1} завершён. Очистка памяти...")
        gc.collect()
        time.sleep(2)  # легкая пауза между пакетами

    print(f"[DONE] Обработка завершена. Всего обработано: {len(processed_total)} файлов.")
    return jsonify({"status": "done", "processed_count": len(processed_total), "processed": processed_total})


# --- Точка входа ---
if __name__ == "__main__":
    check_requirements()
    port = int(os.getenv("PORT", 5000))
    print(f"[INFO] Flask запускается на порту {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
