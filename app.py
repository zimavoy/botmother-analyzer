import os
import io
import time
import base64
import json
import traceback
import threading
import gc
from datetime import datetime, timedelta

from flask import Flask, jsonify, send_from_directory, request
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import gspread
import requests

# ---------- конфиг ----------
APP = Flask(__name__, static_folder="static", static_url_path="")

# env required
REQUIRED_ENV = ["SPREADSHEET_ID", "TO_ANALYZE_FOLDER_ID", "ANALYZED_FOLDER_ID", "YC_IAM_TOKEN"]
# google credentials: credentials.json file expected in working dir

# processing params
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "2"))
SLEEP_BETWEEN_BATCHES = float(os.getenv("SLEEP_BETWEEN_BATCHES", "0.6"))
MEMORY_GC_THRESHOLD_MB = int(os.getenv("MEMORY_GC_THRESHOLD_MB", "400"))

# yandex vision endpoint
YANDEX_VISION_URL = "https://vision.api.cloud.yandex.net/vision/v1/batchAnalyze"  # batch endpoint

# runtime state for progress UI
state = {
    "total": 0,
    "processed": 0,
    "current_file": "",
    "logs": [],
    "done": False,
    "error": None,
    "model_info": "YandexVision"
}

_state_lock = threading.Lock()
_worker_thread = None


# ---------- helpers ----------
def append_log(text):
    ts = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    line = f"[{ts}] {text}"
    with _state_lock:
        state["logs"].append(line)
        # bound logs size
        if len(state["logs"]) > 1000:
            state["logs"] = state["logs"][-800:]


def check_requirements():
    missing = [v for v in REQUIRED_ENV if not os.getenv(v)]
    if missing:
        append_log(f"[WARN] Не заданы переменные: {', '.join(missing)}")
    if not os.path.exists("credentials.json"):
        append_log("[WARN] credentials.json не найден в рабочем каталоге — Google API не будет работать.")


def get_google_services():
    """
    Возвращает (drive_service, sheet)
    Требует credentials.json рядом с app.py (service account JSON).
    """
    creds = Credentials.from_service_account_file(
        "credentials.json",
        scopes=[
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets",
        ],
    )
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    sh_client = gspread.authorize(creds)
    sheet = sh_client.open_by_key(os.getenv("SPREADSHEET_ID")).sheet1
    return drive, sheet


def download_file_bytes(drive_service, file_id):
    """
    Загружает файл из Google Drive в bytes (использует MediaIoBaseDownload).
    Возвращает bytes.
    """
    try:
        request = drive_service.files().get_media(fileId=file_id)
        fh = io.BytesIO()
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
        fh.seek(0)
        data = fh.read()
        return data
    except Exception as e:
        append_log(f"[ERROR] Ошибка скачивания файла {file_id}: {e}")
        traceback.print_exc()
        return None


def call_yandex_vision(image_bytes, features=None):
    """
    Отправляет изображение (bytes) в Yandex Vision batchAnalyze.
    Возвращает десериализованный JSON-объект ответа или None при ошибке.
    features: список строк, например ["LABEL_DETECTION", "OBJECT_LOCALIZATION", "TEXT_DETECTION"]
    """
    if features is None:
        features = ["LABEL_DETECTION", "TEXT_DETECTION", "OBJECT_LOCALIZATION"]

    # base64-encode bytes
    b64 = base64.b64encode(image_bytes).decode("utf-8")

    analyze_spec = {
        "content": {"bytes": b64},
        "features": [{"type": f} for f in features],
        # mimeType optional
    }

    body = {"analyze_specs": [analyze_spec]}

    headers = {
        "Authorization": f"Bearer {os.getenv('YC_IAM_TOKEN')}",
        "Content-Type": "application/json"
    }

    try:
        resp = requests.post(YANDEX_VISION_URL, headers=headers, data=json.dumps(body), timeout=60)
        if resp.status_code != 200:
            append_log(f"[ERROR] Yandex Vision returned {resp.status_code}: {resp.text}")
            return None
        return resp.json()
    except Exception as e:
        append_log(f"[ERROR] Exception calling Yandex Vision: {e}")
        traceback.print_exc()
        return None


def parse_yandex_result(resp_json):
    """
    Парсим ответ Yandex Vision и пытаемся вытянуть:
    - labels (labels description)
    - detected text (concatenate)
    - objects (names)
    Возвращаем dict с полями, которые позже попадут в Google Sheet.
    """
    if not resp_json:
        return {
            "catalog_number": "UNKNOWN",
            "description": "UNKNOWN",
            "machine_type": "UNKNOWN",
            "manufacturer": "UNKNOWN",
            "analogs": "UNKNOWN",
            "detail_description": "UNKNOWN",
            "machine_model": "UNKNOWN",
        }

    labels = []
    texts = []
    objects = []

    # Response structure: { "results": [ { "results": [ { "entities": [...], "labels": [...], "annotations": [...] } ] } ] }
    # Yandex docs: analyze_specs -> results -> ... various fields
    try:
        # try several possible nesting shapes robustly
        top_results = resp_json.get("results") or resp_json.get("responses") or []
        for r in top_results:
            # r may contain 'results' array
            inner = r.get("results") or r.get("entities") or []
            # labels:
            for it in inner:
                if "labels" in it:
                    for lab in it["labels"]:
                        desc = lab.get("description") or lab.get("name")
                        if desc:
                            labels.append(desc)
                # textDetection
                if "textDetection" in it:
                    td = it["textDetection"]
                    if isinstance(td, dict):
                        s = td.get("text", "")
                        if s:
                            texts.append(s)
                # objects / localizedObjects
                if "objects" in it:
                    for obj in it["objects"]:
                        name = obj.get("name") or obj.get("entityId")
                        if name:
                            objects.append(name)
                # legacy: 'annotations'
                if "annotations" in it:
                    for ann in it["annotations"]:
                        if ann.get("type") == "LABEL":
                            labels.append(ann.get("description", ""))
    except Exception:
        # best-effort; simply ignore parse errors
        traceback.print_exc()

    # fallback parsing: look for 'text' fields anywhere
    def walk_for_text(d):
        if isinstance(d, dict):
            for k, v in d.items():
                if k.lower().find("text") >= 0 and isinstance(v, str) and v.strip():
                    texts.append(v.strip())
                else:
                    walk_for_text(v)
        elif isinstance(d, list):
            for it in d:
                walk_for_text(it)

    walk_for_text(resp_json)

    labels = [l for l in labels if l]
    texts = [t for t in texts if t]
    objects = [o for o in objects if o]

    # Heuristics to fill our sheet fields:
    catalog = "UNKNOWN"
    description = ", ".join(labels[:3]) or "UNKNOWN"
    machine_type = objects[0] if objects else "UNKNOWN"
    manufacturer = "UNKNOWN"
    analogs = "UNKNOWN"
    detail_description = (texts[0] if texts else "") or description
    machine_model = "UNKNOWN"

    # Try to find something that looks like a catalog/part number in texts (digits+letters)
    import re
    for t in texts:
        # common patterns: ABC-1234, 1234-ABC, 123456, A12345
        m = re.search(r"[A-Z0-9\-]{4,}", t, re.I)
        if m:
            candidate = m.group(0)
            # filter very short
            if len(candidate) >= 4:
                catalog = candidate
                break

    return {
        "catalog_number": catalog,
        "description": description,
        "machine_type": machine_type,
        "manufacturer": manufacturer,
        "analogs": analogs,
        "detail_description": detail_description,
        "machine_model": machine_model
    }


def ensure_sheet_headers(sheet):
    HEADERS_SHEET = [
        "Catalog Number",
        "Description",
        "Machine Type",
        "Manufacturer",
        "Analogs",
        "Detail Description",
        "Machine Model",
        "File URL",
    ]
    try:
        existing = sheet.row_values(1)
        if not existing:
            sheet.insert_row(HEADERS_SHEET, 1)
        elif existing != HEADERS_SHEET:
            # replace
            try:
                sheet.delete_rows(1)
            except Exception:
                pass
            sheet.insert_row(HEADERS_SHEET, 1)
    except Exception as e:
        append_log(f"[WARN] Не удалось проверить заголовки: {e}")


# ---------- worker ----------
def worker_process():
    with _state_lock:
        state["done"] = False
        state["error"] = None
        state["processed"] = 0
        state["current_file"] = ""
    append_log("[INFO] Worker started")
    try:
        drive, sheet = get_google_services()
        ensure_sheet_headers(sheet)
    except Exception as e:
        append_log(f"[ERROR] Google API init failed: {e}")
        with _state_lock:
            state["error"] = str(e)
            state["done"] = True
        return

    TO_ANALYZE = os.getenv("TO_ANALYZE_FOLDER_ID")
    ANALYZED = os.getenv("ANALYZED_FOLDER_ID")

    try:
        q = f"'{TO_ANALYZE}' in parents and mimeType contains 'image/'"
        res = drive.files().list(q=q, fields="files(id, name, mimeType)", pageSize=1000).execute()
        files = res.get("files", [])
    except Exception as e:
        append_log(f"[ERROR] Google Drive list failed: {e}")
        with _state_lock:
            state["error"] = str(e)
            state["done"] = True
        return

    total = len(files)
    with _state_lock:
        state["total"] = total
        state["processed"] = 0

    if total == 0:
        append_log("[INFO] Нет файлов для анализа.")
        with _state_lock:
            state["done"] = True
        return

    append_log(f"[INFO] Начинаем анализ {total} файлов, batch={BATCH_SIZE}")

    for i in range(0, total, BATCH_SIZE):
        batch = files[i:i + BATCH_SIZE]
        append_log(f"[INFO] Обрабатываем пакет {i//BATCH_SIZE + 1}: {len(batch)} файлов")

        for f in batch:
            file_id = f.get("id")
            file_name = f.get("name")
            with _state_lock:
                state["current_file"] = file_name

            append_log(f"[INFO] Скачиваем {file_name}")
            img_bytes = download_file_bytes(drive, file_id)
            if not img_bytes:
                append_log(f"[ERROR] Не удалось скачать {file_name}, пропускаем")
                with _state_lock:
                    state["processed"] += 1
                continue

            # call Yandex Vision
            append_log(f"[INFO] Отправка в Yandex Vision: {file_name}")
            resp_json = call_yandex_vision(img_bytes)
            if not resp_json:
                append_log(f"[ERROR] Yandex Vision не вернул результат для {file_name}")
            else:
                # parse results
                parsed = parse_yandex_result(resp_json)
                # write to sheet
                try:
                    sheet.append_row([
                        parsed["catalog_number"],
                        parsed["description"],
                        parsed["machine_type"],
                        parsed["manufacturer"],
                        parsed["analogs"],
                        parsed["detail_description"],
                        parsed["machine_model"],
                        f"https://drive.google.com/file/d/{file_id}/view"
                    ])
                    append_log(f"[INFO] Записано в таблицу: {file_name}")
                except Exception as e:
                    append_log(f"[ERROR] Не удалось записать результат в таблицу: {e}")

            # move file to analyzed folder
            try:
                fi = drive.files().get(fileId=file_id, fields="parents").execute()
                prev_parents = fi.get("parents") or []
                prev_parents_str = ",".join(prev_parents)
                drive.files().update(fileId=file_id, addParents=ANALYZED, removeParents=prev_parents_str, fields="id, parents").execute()
                append_log(f"[INFO] Перемещено в analyzed: {file_name}")
            except Exception as e:
                append_log(f"[WARN] Не удалось переместить {file_name}: {e}")

            # update processed count and memory housekeeping
            with _state_lock:
                state["processed"] += 1
            # cleanup
            try:
                del img_bytes
            except Exception:
                pass
            gc.collect()

        # small pause
        time.sleep(SLEEP_BETWEEN_BATCHES)
        # memory check (best-effort)
        try:
            import psutil
            mem = psutil.Process(os.getpid()).memory_info().rss / (1024 ** 2)
            append_log(f"[DEBUG] Memory usage: {mem:.1f} MB")
            if mem > MEMORY_GC_THRESHOLD_MB:
                append_log("[WARN] Memory usage high — running GC and sleeping")
                gc.collect()
                time.sleep(2)
        except Exception:
            pass

    with _state_lock:
        state["done"] = True
        state["current_file"] = ""
    append_log("[INFO] Worker finished")


# ---------- HTTP endpoints ----------
@APP.route("/")
def index():
    return send_from_directory(APP.static_folder or "static", "index.html")


@APP.route("/start", methods=["POST"])
def start():
    global _worker_thread
    with _state_lock:
        if _worker_thread and _worker_thread.is_alive():
            return jsonify({"status": "already_running"}), 409
        # reset
        state.update({"total": 0, "processed": 0, "current_file": "", "logs": [], "done": False, "error": None})
    _worker_thread = threading.Thread(target=worker_process, daemon=True)
    _worker_thread.start()
    return jsonify({"status": "started"})


@APP.route("/status", methods=["GET"])
def status():
    with _state_lock:
        return jsonify({
            "total": state["total"],
            "processed": state["processed"],
            "current_file": state["current_file"],
            "logs": state["logs"][-200:],
            "done": state["done"],
            "error": state["error"],
            "model_info": state["model_info"]
        })


@APP.route("/model_limits", methods=["GET"])
def model_limits_endpoint():
    # just a placeholder: Yandex has its own billing; we return limited info (not implemented)
    return jsonify({"status": "n/a", "note": "Yandex billing not polled in this demo"})

# ---------- run ----------
if __name__ == "__main__":
    check_requirements()
    port = int(os.getenv("PORT", 5000))
    APP.run(host="0.0.0.0", port=port)
