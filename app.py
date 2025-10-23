import os
import traceback
import time
import base64
import requests
from io import BytesIO
from flask import Flask, jsonify
from PIL import Image

app = Flask(__name__)

# --- Обязательные переменные окружения ---
REQUIRED_ENV_VARS = [
    "OPENAI_API_KEY",
    "SPREADSHEET_ID",
    "TO_ANALYZE_FOLDER_ID",
    "ANALYZED_FOLDER_ID"
]


def check_requirements():
    print("[INFO] Проверка требований...")
    missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
    if missing:
        print(f"[ERROR] Не заданы переменные окружения: {', '.join(missing)}")
    if not os.path.exists("credentials.json"):
        print("[ERROR] credentials.json не найден!")
    else:
        print("[INFO] credentials.json найден.")


# --- Конвертация изображения в Base64 PNG ---
def get_image_base64(url):
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        img = Image.open(BytesIO(response.content))

        # Приводим к PNG при необходимости
        if img.format.lower() not in ["png", "jpeg", "jpg", "gif", "webp"]:
            print(f"[INFO] Конвертация изображения {url} → PNG")
            buf = BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            data = buf.read()
        else:
            data = response.content

        return "data:image/png;base64," + base64.b64encode(data).decode("utf-8")

    except Exception as e:
        print(f"[ERROR] Не удалось обработать изображение {url}: {e}")
        return None


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

    drive = build("drive", "v3", credentials=creds)
    sheets = gspread.authorize(creds)
    sheet = sheets.open_by_key(os.getenv("SPREADSHEET_ID")).sheet1
    return drive, sheet


# --- OpenAI ---
def get_openai_client():
    from openai import OpenAI
    api_key = os.getenv("OPENAI_API_KEY")
    return OpenAI(api_key=api_key)


# --- /ping ---
@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"status": "ok"})


# --- /analyze ---
@app.route("/analyze", methods=["POST"])
def analyze():
    try:
        drive, sheet = get_google_services()
        openai_client = get_openai_client()
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500

    to_analyze = os.getenv("TO_ANALYZE_FOLDER_ID")
    analyzed = os.getenv("ANALYZED_FOLDER_ID")

    try:
        results = drive.files().list(
            q=f"'{to_analyze}' in parents and mimeType contains 'image/'",
            fields="files(id, name, webViewLink)"
        ).execute()
        files = results.get("files", [])
    except Exception as e:
        print(f"[ERROR] Не удалось получить список файлов: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

    processed = []
    batch_size = 5

    for i in range(0, len(files), batch_size):
        batch = files[i:i + batch_size]
        print(f"[INFO] Обработка пакета {i // batch_size + 1} из {len(files) // batch_size + 1}...")

        for f in batch:
            file_id, name, link = f["id"], f["name"], f["webViewLink"]
            print(f"[INFO] Анализ {name}")

            image_b64 = get_image_base64(link)
            if not image_b64:
                continue

            try:
                response = openai_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": "Ты эксперт по запчастям спецтехники."},
                        {"role": "user", "content": [
                            {
                                "type": "text",
                                "text": (
                                    "Проанализируй фото и определи: "
                                    "1. Каталожный номер детали\n"
                                    "2. Описание детали\n"
                                    "3. Производитель\n"
                                    "4. Артикулы аналогов\n"
                                    "5. Модель машины, для которой подходит деталь\n"
                                    "Ответ верни в JSON формате с полями: "
                                    "catalog_number, description, manufacturer, analogs, machine_model."
                                )
                            },
                            {"type": "image_url", "image_url": {"url": image_b64}}
                        ]}
                    ],
                    max_tokens=600
                )

                result = response.choices[0].message.content.strip()
                print(f"[INFO] Ответ модели:\n{result}")

                # Простая попытка извлечь значения
                catalog_number = description = manufacturer = analogs = machine_model = "UNKNOWN"
                for key in ["catalog_number", "description", "manufacturer", "analogs", "machine_model"]:
                    if key in result:
                        start = result.find(key) + len(key) + 2
                        end = result.find("\n", start)
                        value = result[start:end].strip().strip('"').strip(",")
                        locals()[key] = value

            except Exception as e:
                print(f"[ERROR] Ошибка анализа {name}: {e}")
                continue

            try:
                # Перемещаем файл в папку analyzed
                info = drive.files().get(fileId=file_id, fields="parents").execute()
                prev_parents = ",".join(info.get("parents", []))
                drive.files().update(
                    fileId=file_id,
                    addParents=analyzed,
                    removeParents=prev_parents,
                    fields="id, parents"
                ).execute()
            except Exception as e:
                print(f"[ERROR] Ошибка переноса {name}: {e}")

            try:
                sheet.append_row([catalog_number, description, manufacturer, analogs, machine_model, link])
            except Exception as e:
                print(f"[ERROR] Ошибка записи в таблицу: {e}")

            processed.append({
                "file": name,
                "catalog_number": catalog_number,
                "description": description,
                "manufacturer": manufacturer,
                "analogs": analogs,
                "machine_model": machine_model
            })

        # задержка между батчами
        print("[INFO] Пауза 10 секунд перед следующим пакетом...")
        time.sleep(10)

    return jsonify({
        "status": "done",
        "processed_count": len(processed),
        "processed": processed
    })


if __name__ == "__main__":
    check_requirements()
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
