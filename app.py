import os
import io
import base64
import traceback
import requests
from flask import Flask, jsonify, render_template, Response
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import gspread
from time import sleep

# ---------------- CONFIG ---------------- #

app = Flask(__name__, static_folder="static", template_folder="static")

REQUIRED_ENV_VARS = [
    "YANDEX_API_KEY",
    "YANDEX_FOLDER_ID",
    "SPREADSHEET_ID",
    "TO_ANALYZE_FOLDER_ID",
    "ANALYZED_FOLDER_ID",
]

YANDEX_API_KEY = os.getenv("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")
YANDEX_VISION_URL = "https://vision.api.cloud.yandex.net/vision/v1/batchAnalyze"

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

# -------- Utility -------- #
def emit_event(event: str, data: str):
    """Send event for frontend via SSE (console fallback)"""
    print(f"[EVENT] {event}: {data}")

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

def list_drive_images(drive):
    folder = os.getenv("TO_ANALYZE_FOLDER_ID")
    results = drive.files().list(
        q=f"'{folder}' in parents and mimeType contains 'image/'",
        fields="files(id, name, webViewLink, webContentLink)"
    ).execute()
    return results.get("files", [])

def download_drive_file_bytes(drive, file_id: str):
    """Скачать файл как bytes"""
    try:
        request = drive.files().get_media(fileId=file_id)
        from googleapiclient.http import MediaIoBaseDownload
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
        buf.seek(0)
        return buf.read()
    except Exception as e:
        emit_event("log", f"Ошибка скачивания файла {file_id}: {e}")
        return b""

# ----------- Yandex Vision API ----------- #

def call_yandex_vision_with_url(image_url: str, retries=2):
    headers = {
        "Authorization": f"Api-Key {YANDEX_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "folderId": YANDEX_FOLDER_ID,
        "analyze_specs": [
            {
                "features": [{"type": "TEXT_DETECTION"}],
                "source": {"uri": image_url}
            }
        ]
    }

    for attempt in range(retries + 1):
        try:
            r = requests.post(YANDEX_VISION_URL, headers=headers, json=payload, timeout=55)
            if r.status_code == 200:
                return True, r.json(), 200
            emit_event("log", f"Yandex(url) code={r.status_code} body={r.text[:300]}")
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                sleep(2 ** attempt)
                continue
            return False, r.text, r.status_code
        except requests.RequestException as e:
            emit_event("log", f"Yandex(url) exception: {e}")
            sleep(2)
    return False, "request failed", None

def call_yandex_vision_with_bytes(image_bytes: bytes, retries=1):
    headers = {
        "Authorization": f"Api-Key {YANDEX_API_KEY}",
        "Content-Type": "application/json",
    }
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    payload = {
        "folderId": YANDEX_FOLDER_ID,
        "analyze_specs": [
            {
                "features": [{"type": "TEXT_DETECTION"}],
                "content": {"bytes": b64}
            }
        ]
    }

    for attempt in range(retries + 1):
        try:
            r = requests.post(YANDEX_VISION_URL, headers=headers, json=payload, timeout=70)
            if r.status_code == 200:
                return True, r.json(), 200
            emit_event("log", f"Yandex(bytes) code={r.status_code} body={r.text[:300]}")
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                sleep(2 ** attempt)
                continue
            return False, r.text, r.status_code
        except requests.RequestException as e:
            emit_event("log", f"Yandex(bytes) exception: {e}")
            sleep(1)
    return False, "request failed", None

def parse_text_from_yandex_response(resp_json):
    try:
        collected = []
        for result in resp_json.get("results", []):
            for rr in result.get("results", []):
                td = rr.get("textDetection")
                if not td:
                    continue
                for p in td.get("pages", []):
                    for block in p.get("blocks", []):
                        for line in block.get("lines", []):
                            words = [w.get("text", "") for w in line.get("words", [])]
                            if words:
                                collected.append(" ".join(words))
        return "\n".join(collected) if collected else "UNKNOWN"
    except Exception as e:
        emit_event("log", f"Ошибка парсинга: {e}")
        traceback.print_exc()
        return "UNKNOWN"

# ---------- Flask Routes ---------- #

@app.route("/")
def index():
    return app.send_static_file("index.html")

@app.route("/debug_first", methods=["GET"])
def debug_first():
    """Берёт первую картинку и возвращает подробности анализа"""
    try:
        drive, _ = get_google_services()
        files = list_drive_images(drive)
        if not files:
            return jsonify({"ok": False, "error": "Нет файлов"}), 400

        f = files[0]
        url = f.get("webContentLink")
        file_id = f["id"]
        file_name = f["name"]
        result = {"file": file_name, "url": url}

        ok, resp, code = call_yandex_vision_with_url(url)
        result["url_result"] = {"ok": ok, "code": code, "snippet": str(resp)[:1000]}

        if not ok:
            emit_event("log", f"URL-анализ не удался, пробую байты для {file_name}")
            data = download_drive_file_bytes(drive, file_id)
            ok2, resp2, code2 = call_yandex_vision_with_bytes(data)
            result["bytes_result"] = {"ok": ok2, "code": code2, "snippet": str(resp2)[:1000]}

        return jsonify(result)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/analyze", methods=["POST"])
def analyze():
    try:
        drive, sheet = get_google_services()
        files = list_drive_images(drive)
        processed = []
        total = len(files)
        emit_event("log", f"Найдено {total} файлов для анализа.")

        for idx, f in enumerate(files, start=1):
            file_name, file_id = f["name"], f["id"]
            url = f.get("webContentLink")
            emit_event("progress", f"{idx}/{total}")
            emit_event("log", f"Анализ {file_name}...")

            # анализ изображения
            text = "UNKNOWN"
            try:
                ok, resp, _ = call_yandex_vision_with_url(url)
                if ok:
                    text = parse_text_from_yandex_response(resp)
                else:
                    data = download_drive_file_bytes(drive, file_id)
                    ok2, resp2, _ = call_yandex_vision_with_bytes(data)
                    text = parse_text_from_yandex_response(resp2) if ok2 else "ERROR"
            except Exception as e:
                emit_event("log", f"Ошибка анализа {file_name}: {e}")
                traceback.print_exc()

            sheet.append_row(["-", text, "-", "-", "-", "-", "-", url])
            processed.append({"file": file_name, "text": text})
            sleep(1)  # разгрузка памяти

        return jsonify({"status": "done", "processed_count": len(processed), "processed": processed})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": str(e)}), 500

# ---------- Main ---------- #
if __name__ == "__main__":
    check_requirements()
    port = int(os.getenv("PORT", 5000))
    print(f"[INFO] Запуск Flask на порту {port}...")
    app.run(host="0.0.0.0", port=port, debug=True)
