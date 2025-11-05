import os
import io
import time
import json
import base64
import queue
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List

import requests
from flask import Flask, jsonify, request, send_from_directory, Response
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import gspread

# ---------- CONFIG ----------
app = Flask(__name__, static_folder="static", static_url_path="/static")

YANDEX_VISION_URL = "https://vision.api.cloud.yandex.net/vision/v1/batchAnalyze"
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID", "")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
TO_ANALYZE_FOLDER_ID = os.getenv("TO_ANALYZE_FOLDER_ID")
ANALYZED_FOLDER_ID = os.getenv("ANALYZED_FOLDER_ID")

# tuning
BATCH_SIZE = int(os.getenv("BATCH_SIZE", "5"))        # сколько файлов в одной партии
CONCURRENCY = int(os.getenv("CONCURRENCY", "3"))      # сколько одновременных запросов к Yandex внутри партии
SLEEP_BETWEEN_BATCHES = float(os.getenv("SLEEP_BETWEEN_BATCHES", "0.5"))

# optional sample image to check API (for /limits)
SAMPLE_IMAGE_URL = os.getenv("SAMPLE_IMAGE_URL")

# ---------- runtime state ----------
_event_queue = queue.Queue(maxsize=1000)   # messages for SSE
_worker_thread = None
_worker_lock = threading.Lock()
_worker_stop_flag = threading.Event()
_worker_running = threading.Event()

# ---------- helpers ----------
def emit_event(event: str, data) -> None:
    """Put structured event into queue for SSE clients."""
    payload = {"event": event, "data": data}
    try:
        _event_queue.put_nowait(payload)
    except queue.Full:
        # drop oldest if full
        try:
            _event_queue.get_nowait()
            _event_queue.put_nowait(payload)
        except Exception:
            pass

def check_env():
    missing = []
    for v in ["YANDEX_API_KEY", "SPREADSHEET_ID", "TO_ANALYZE_FOLDER_ID", "ANALYZED_FOLDER_ID"]:
        if not os.getenv(v):
            missing.append(v)
    return missing

def get_google_services():
    """Return drive_service, sheets_sheet (gspread sheet)"""
    if not os.path.exists("credentials.json"):
        raise FileNotFoundError("credentials.json not found in working dir")
    creds = Credentials.from_service_account_file(
        "credentials.json",
        scopes=["https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/spreadsheets"],
    )
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    sheet_client = gspread.authorize(creds)
    sheet = sheet_client.open_by_key(SPREADSHEET_ID).sheet1
    return drive, sheet

def list_drive_images(drive) -> List[dict]:
    """List image files in TO_ANALYZE_FOLDER_ID; returns list of dicts with id,name,webContentLink"""
    q = f"'{TO_ANALYZE_FOLDER_ID}' in parents and mimeType contains 'image/'"
    results = drive.files().list(q=q, fields="files(id,name,webContentLink)", pageSize=1000).execute()
    return results.get("files", [])

def download_drive_file_bytes(drive, file_id) -> bytes:
    """Download file content bytes from Drive using media download."""
    request = drive.files().get_media(fileId=file_id)
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        status, done = downloader.next_chunk()
    fh.seek(0)
    return fh.read()

def call_yandex_vision_with_url(image_url: str):
    """Call Yandex Vision using 'source.uri' (preferred)"""
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
    resp = requests.post(YANDEX_VISION_URL, headers=headers, json=payload, timeout=55)
    resp.raise_for_status()
    return resp.json()

def parse_text_from_yandex_response(resp_json):
    """Extract textual lines from response safely."""
    try:
        collected = []
        results = resp_json.get("results", [])
        for r in results:
            inner = r.get("results", [])
            for rr in inner:
                td = rr.get("textDetection")
                if not td:
                    continue
                pages = td.get("pages", [])
                for p in pages:
                    for block in p.get("blocks", []):
                        for line in block.get("lines", []):
                            words = [w.get("text","") for w in line.get("words",[])]
                            if words:
                                collected.append(" ".join(words))
        return "\n".join(collected) if collected else "UNKNOWN"
    except Exception:
        traceback.print_exc()
        return "UNKNOWN"

# ---------- worker ----------
def processing_worker():
    """Background worker: fetch files from Drive and process in batches."""
    global _worker_stop_flag
    with _worker_lock:
        if _worker_running.is_set():
            emit_event("log", "Worker already running")
            return
        _worker_running.set()
        _worker_stop_flag.clear()

    emit_event("log", "Worker started")
    try:
        drive, sheet = get_google_services()
    except Exception as e:
        emit_event("error", f"Google API init error: {e}")
        _worker_running.clear()
        return

    try:
        files = list_drive_images(drive)
    except Exception as e:
        emit_event("error", f"Drive list error: {e}")
        _worker_running.clear()
        return

    total = len(files)
    emit_event("start", {"total": total})
    if total == 0:
        emit_event("log", "No images to process")
        emit_event("done", {"processed": 0})
        _worker_running.clear()
        return

    processed_count = 0

    for i in range(0, total, BATCH_SIZE):
        if _worker_stop_flag.is_set():
            emit_event("log", "Stop requested — finishing")
            break

        batch = files[i:i+BATCH_SIZE]
        emit_event("log", f"Processing batch {i//BATCH_SIZE + 1}: {len(batch)} files")

        # Use ThreadPoolExecutor to limit concurrency inside batch
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
            futures = {}
            for f in batch:
                file_id = f["id"]
                file_name = f["name"]
                # prefer webContentLink if exists (download link)
                url = f.get("webContentLink") or f.get("webViewLink") or None
                # Create task: try using URL first; if not available, download bytes and call with content
                futures[ex.submit(process_single_file, drive, sheet, file_id, file_name, url)] = file_name

            for fut in as_completed(futures):
                fname = futures[fut]
                try:
                    result = fut.result()
                    processed_count += 1
                    emit_event("result", result)
                    emit_event("progress", {"processed": processed_count, "total": total})
                except Exception as e:
                    emit_event("log", f"Error processing {fname}: {e}")
                    traceback.print_exc()

        time.sleep(SLEEP_BETWEEN_BATCHES)

    emit_event("done", {"processed": processed_count})
    emit_event("log", "Worker finished")
    _worker_running.clear()

def process_single_file(drive, sheet, file_id, file_name, url):
    """Process one file: call Yandex Vision (by url or by bytes), append to Sheet, move file."""
    emit_event("log", f"Processing file: {file_name}")

    text = "UNKNOWN"
    # 1) try URL approach if available
    try:
        if url:
            resp = call_yandex_vision_with_url(url)
            text = parse_text_from_yandex_response(resp)
        else:
            # download bytes and send base64 in 'content'
            data = download_drive_file_bytes(drive, file_id)
            b64 = base64.b64encode(data).decode("utf-8")
            headers = {"Authorization": f"Api-Key {YANDEX_API_KEY}", "Content-Type": "application/json"}
            payload = {
                "folderId": YANDEX_FOLDER_ID,
                "analyze_specs": [
                    {"features": [{"type": "TEXT_DETECTION"}], "content": {"bytes": b64}}
                ]
            }
            resp = requests.post(YANDEX_VISION_URL, headers=headers, json=payload, timeout=60)
            resp.raise_for_status()
            text = parse_text_from_yandex_response(resp.json())
    except requests.HTTPError as he:
        emit_event("log", f"Yandex API HTTP error for {file_name}: {he}")
        text = "ERROR"
    except Exception as e:
        emit_event("log", f"Yandex call error for {file_name}: {e}")
        text = "ERROR"

    # 2) write to sheet (best-effort)
    try:
        # simple mapping: put recognized text into Catalog Number/Description fields as fallback
        catalog_number = text.splitlines()[0][:40] if text and text != "UNKNOWN" else "UNKNOWN"
        description = text[:120] if text and text != "UNKNOWN" else "UNKNOWN"
        machine_type = manufacturer = analogs = detail_description = machine_model = "UNKNOWN"
        file_url = url or f"https://drive.google.com/uc?export=download&id={file_id}"
        sheet.append_row([catalog_number, description, machine_type, manufacturer, analogs, detail_description, machine_model, file_url])
    except Exception as e:
        emit_event("log", f"Sheet write error for {file_name}: {e}")

    # 3) move file to analyzed folder (best-effort)
    try:
        fi = drive.files().get(fileId=file_id, fields="parents").execute()
        prev_parents = ",".join(fi.get("parents", []))
        drive.files().update(fileId=file_id, addParents=ANALYZED_FOLDER_ID, removeParents=prev_parents, fields="id, parents").execute()
    except Exception as e:
        emit_event("log", f"Drive move warning for {file_name}: {e}")

    return {"file": file_name, "text_preview": text[:300]}

# ---------- control endpoints ----------
@app.route("/start", methods=["POST"])
def start_processing():
    global _worker_thread
    missing = check_env()
    if missing:
        return jsonify({"status": "error", "message": f"Missing env: {missing}"}), 400

    with _worker_lock:
        if _worker_running.is_set():
            return jsonify({"status": "already_running"}), 409
        # launch thread
        _worker_thread = threading.Thread(target=processing_worker, daemon=True)
        _worker_thread.start()
    return jsonify({"status": "started"})

@app.route("/stop", methods=["POST"])
def stop_processing():
    _worker_stop_flag.set()
    emit_event("log", "Stop requested by user")
    return jsonify({"status": "stopping"})

@app.route("/limits", methods=["GET"])
def get_limits():
    """Return API-key presence and optionally test API using SAMPLE_IMAGE_URL."""
    info = {"api_key_present": bool(YANDEX_API_KEY), "checked": False, "ok": None, "note": ""}
    if not YANDEX_API_KEY:
        info["note"] = "YANDEX_API_KEY not set"
        return jsonify(info)

    if SAMPLE_IMAGE_URL:
        try:
            # lightweight test call
            headers = {"Authorization": f"Api-Key {YANDEX_API_KEY}", "Content-Type": "application/json"}
            payload = {
                "analyze_specs": [
                    {"features": [{"type": "TEXT_DETECTION"}], "source": {"uri": SAMPLE_IMAGE_URL}}
                ]
            }
            r = requests.post(YANDEX_VISION_URL, headers=headers, json=payload, timeout=15)
            info["checked"] = True
            info["ok"] = (r.status_code == 200)
            info["status_code"] = r.status_code
            info["note"] = r.text[:800] if r.status_code != 200 else "OK"
        except Exception as e:
            info["checked"] = True
            info["ok"] = False
            info["note"] = str(e)
    else:
        info["note"] = "SAMPLE_IMAGE_URL not set — cannot test API reachability automatically"

    return jsonify(info)

# ---------- SSE stream endpoint ----------
def event_stream():
    """Yield SSE events from the queue."""
    while True:
        try:
            item = _event_queue.get()
            ev = item.get("event")
            data = item.get("data")
            # SSE format: event: <ev>\ndata: <json>\n\n
            payload = f"event: {ev}\ndata: {json.dumps(data)}\n\n"
            yield payload
        except GeneratorExit:
            break
        except Exception:
            # avoid breaking stream on unexpected errors
            continue

@app.route("/stream")
def stream():
    return Response(event_stream(), mimetype="text/event-stream")

# ---------- static/index ----------
@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")

# ---------- main ----------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), threaded=True)
