# app.py — улучшенная версия: надёжный парсинг, ретраители, логи
import os
import json
import time
import base64
import re
import traceback
from io import BytesIO
from flask import Flask, jsonify
from PIL import Image
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import gspread
import requests

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

# Настройки
REQUIRED_ENV_VARS = [
    "OPENAI_API_KEY",
    "SPREADSHEET_ID",
    "TO_ANALYZE_FOLDER_ID",
    "ANALYZED_FOLDER_ID"
]

BATCH_SIZE = 5
PAUSE_SECONDS_BETWEEN_BATCHES = 5
OPENAI_API_URL = "https://api.openai.com/v1/responses"
MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds


def check_requirements():
    missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
    if missing:
        print(f"[ERROR] Отсутствуют переменные окружения: {missing}")
    if not os.path.exists("credentials.json"):
        print("[ERROR] credentials.json не найден!")
    print("[INFO] Проверка завершена.")


def get_google_services():
    creds = Credentials.from_service_account_file(
        "credentials.json",
        scopes=[
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/spreadsheets",
        ],
    )
    drive_service = build("drive", "v3", credentials=creds)
    gclient = gspread.authorize(creds)
    sheet = gclient.open_by_key(os.getenv("SPREADSHEET_ID")).sheet1
    return drive_service, sheet


def download_drive_file_bytes(drive_service, file_id):
    try:
        fh = BytesIO()
        request = drive_service.files().get_media(fileId=file_id)
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
        fh.seek(0)
        data = fh.read()
        print(f"[DEBUG] Скачано {len(data)} байт для file_id={file_id}")
        return data
    except Exception as e:
        print(f"[ERROR] download_drive_file_bytes failed for {file_id}: {e}")
        traceback.print_exc()
        return None


def convert_bytes_to_base64_png(image_bytes):
    try:
        img = Image.open(BytesIO(image_bytes))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        buf = BytesIO()
        img.save(buf, format="PNG")
        data = buf.getvalue()
        print(f"[DEBUG] Конвертировано в PNG, size={len(data)}")
        return "data:image/png;base64," + base64.b64encode(data).decode("utf-8")
    except Exception as e:
        print(f"[ERROR] convert_bytes_to_base64_png failed: {e}")
        traceback.print_exc()
        return None


def call_openai_via_rest(image_data_uri, system_prompt, user_prompt):
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY не задан")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # Для Responses API мы передаём image в виде base64 в input
    body = {
        "model": "gpt-4o-mini",
        "input": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": user_prompt},
                    # передаём очищенный base64 (без префикса data:image/..., оставляем только base64)
                    {"type": "input_image", "image_base64": image_data_uri.split(",", 1)[1]},
                ],
            },
        ],
        "max_output_tokens": 700,
    }

    try:
        resp = requests.post(OPENAI_API_URL, headers=headers, json=body, timeout=60)
        print(f"[DEBUG] OpenAI status {resp.status_code}")
        text = resp.text
        # логируем кратко (не перегружая)
        print(f"[DEBUG] OpenAI raw response (truncated): {text[:2000]}")
        if resp.status_code >= 400:
            print(f"[ERROR] OpenAI error {resp.status_code}: {text}")
            return None, resp.status_code, text
        try:
            j = resp.json()
        except Exception:
            print("[WARN] Ответ OpenAI не JSON, возвращаем сырой текст")
            return text, resp.status_code, text
        return j, resp.status_code, text
    except Exception as e:
        print(f"[ERROR] OpenAI REST request failed: {e}")
        traceback.print_exc()
        return None, None, str(e)


def parse_openai_response(resp_json_or_text):
    """
    Пытаемся получить JSON-данные с полями:
    catalog_number, description, manufacturer, analogs, machine_model
    Поддерживаем несколько форматов:
     - если OpenAI вернул объект JSON -> берем поля
     - если вернул текст, где есть JSON-объект внутри -> извлекаем и парсим
     - если вернул текст в формате "Key: value" -> парсим по строкам
    """
    fields = {
        "catalog_number": "UNKNOWN",
        "description": "UNKNOWN",
        "manufacturer": "UNKNOWN",
        "analogs": "UNKNOWN",
        "machine_model": "UNKNOWN",
    }

    # Если это уже dict (responses JSON)
    if isinstance(resp_json_or_text, dict):
        # Попробуем найти полезные места
        # 1) Если есть output_text
        if "output_text" in resp_json_or_text and isinstance(resp_json_or_text["output_text"], str):
            text = resp_json_or_text["output_text"]
        else:
            # try common places
            # try j["output"][0]["content"]...
            text = None
            out = resp_json_or_text.get("output") or resp_json_or_text.get("outputs") or resp_json_or_text.get("choices")
            if isinstance(out, list) and len(out) > 0:
                first = out[0]
                # try nested content
                content = first.get("content") if isinstance(first, dict) else None
                if isinstance(content, list):
                    # collect text parts
                    parts = []
                    for c in content:
                        if isinstance(c, dict):
                            if c.get("type") in ("output_text", "text", "output_text"):
                                t = c.get("text") or c.get("content") or ""
                                if isinstance(t, str):
                                    parts.append(t)
                    if parts:
                        text = "\n".join(parts)
                if not text:
                    # try ch.message.content.text etc.
                    if "message" in first and isinstance(first["message"], dict):
                        m = first["message"]
                        cont = m.get("content")
                        if isinstance(cont, list):
                            txts = []
                            for el in cont:
                                if isinstance(el, dict) and "text" in el:
                                    txts.append(el["text"])
                            if txts:
                                text = "\n".join(txts)
                    # fallback to textual fields
                    if "text" in first and isinstance(first["text"], str):
                        text = first["text"]
            if text is None:
                # fallback whole json string
                try:
                    text = json.dumps(resp_json_or_text)
                except Exception:
                    text = str(resp_json_or_text)
    else:
        # it's a text string
        text = str(resp_json_or_text)

    print(f"[DEBUG] Parsed candidate text (truncated): {text[:1500]}")

    # 1) Try parse as pure JSON
    try:
        maybe = json.loads(text)
        if isinstance(maybe, dict):
            for k in fields.keys():
                if k in maybe:
                    fields[k] = maybe.get(k) or "UNKNOWN"
            return fields
    except Exception:
        pass

    # 2) Try extract first JSON object inside text
    m = re.search(r"(\{[\s\S]*\})", text)
    if m:
        js = m.group(1)
        try:
            maybe = json.loads(js)
            for k in fields.keys():
                if k in maybe:
                    fields[k] = maybe.get(k) or "UNKNOWN"
            return fields
        except Exception:
            pass

    # 3) Try parse "Key: value" lines
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # formats: Key: value  OR "Catalog Number - value"
        if ":" in line:
            parts = line.split(":", 1)
            key = parts[0].strip().lower()
            val = parts[1].strip()
            if "catalog" in key and "number" in key:
                fields["catalog_number"] = val or fields["catalog_number"]
            elif key.startswith("description"):
                fields["description"] = val or fields["description"]
            elif "manufacturer" in key or "maker" in key:
                fields["manufacturer"] = val or fields["manufacturer"]
            elif "analog" in key:
                fields["analogs"] = val or fields["analogs"]
            elif "model" in key:
                fields["machine_model"] = val or fields["machine_model"]
    # 4) return whatever we have
    return fields


@app.route("/ping", methods=["GET"])
def ping():
    return jsonify({"status": "ok", "time": int(time.time())})


@app.route("/analyze", methods=["POST"])
def analyze():
    check_requirements()
    try:
        drive, sheet = get_google_services()
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status": "error", "message": f"Google init failed: {e}"}), 500

    to_id = os.getenv("TO_ANALYZE_FOLDER_ID")
    analyzed_id = os.getenv("ANALYZED_FOLDER_ID")
    processed = []

    try:
        res = drive.files().list(
            q=f"'{to_id}' in parents and mimeType contains 'image/'",
            fields="files(id,name)"
        ).execute()
        files = res.get("files", [])
        print(f"[INFO] Files to analyze: {len(files)}")
    except Exception as e:
        print("[ERROR] Drive list failed:", e)
        traceback.print_exc()
        return jsonify({"status": "error", "message": f"Drive list failed: {e}"}), 500

    system_prompt = "Ты эксперт по запчастям строительной техники. Отвечай строго."
    user_prompt = (
        "Верни JSON с полями: catalog_number, description, manufacturer, analogs, machine_model. "
        "Если не удалось распознать — значение поля 'UNKNOWN'."
    )

    for i in range(0, len(files), BATCH_SIZE):
        batch = files[i:i + BATCH_SIZE]
        print(f"[INFO] Processing batch {i // BATCH_SIZE + 1}, size={len(batch)}")
        for f in batch:
            file_id = f["id"]
            file_name = f.get("name", "unnamed")
            print(f"[INFO] Processing file {file_name} ({file_id})")

            # download bytes from Drive (authorized)
            image_bytes = download_drive_file_bytes(drive, file_id)
            if not image_bytes:
                print(f"[WARN] Could not download {file_name}, skipping")
                continue

            image_data_uri = convert_bytes_to_base64_png(image_bytes)
            if not image_data_uri:
                print(f"[WARN] Could not convert {file_name}, skipping")
                continue

            # debug: length of base64
            print(f"[DEBUG] image_data_uri length: {len(image_data_uri)}")

            # retries for OpenAI call
            parsed_fields = None
            last_raw = None
            for attempt in range(1, MAX_RETRIES + 1):
                print(f"[INFO] OpenAI attempt {attempt} for {file_name}")
                resp_obj, status_code, raw_text = call_openai_via_rest(image_data_uri, system_prompt, user_prompt)
                last_raw = raw_text
                # Try parse
                parsed = None
                if resp_obj is None and raw_text:
                    # resp was error or raw text; try parse from raw
                    parsed = parse_openai_response(raw_text)
                else:
                    parsed = parse_openai_response(resp_obj)
                # check success: at least one field not UNKNOWN or description not UNKNOWN
                non_unknown = [v for v in parsed.values() if v and v != "UNKNOWN"]
                if non_unknown:
                    parsed_fields = parsed
                    print(f"[INFO] Successful parse on attempt {attempt} for {file_name}: {parsed_fields}")
                    break
                else:
                    print(f"[WARN] Parse returned only UNKNOWN on attempt {attempt} for {file_name}")
                    if attempt < MAX_RETRIES:
                        time.sleep(RETRY_DELAY)

            if parsed_fields is None:
                print(f"[ERROR] All attempts failed to get data for {file_name}. Raw OpenAI: {str(last_raw)[:1500]}")
                parsed_fields = {
                    "catalog_number": "UNKNOWN",
                    "description": "UNKNOWN",
                    "manufacturer": "UNKNOWN",
                    "analogs": "UNKNOWN",
                    "machine_model": "UNKNOWN",
                }

            # Move file to analyzed (try best-effort)
            try:
                info = drive.files().get(fileId=file_id, fields="parents").execute()
                prev_parents = info.get("parents", [])
                prev_parents_str = ",".join(prev_parents) if prev_parents else ""
                drive.files().update(
                    fileId=file_id,
                    addParents=analyzed_id,
                    removeParents=prev_parents_str,
                    fields="id, parents"
                ).execute()
                print(f"[INFO] Moved {file_name} to analyzed")
            except Exception as e:
                print(f"[ERROR] Failed to move {file_name}: {e}")
                traceback.print_exc()

            # Append to sheet
            try:
                sheet.append_row([
                    parsed_fields.get("catalog_number"),
                    parsed_fields.get("description"),
                    parsed_fields.get("manufacturer"),
                    parsed_fields.get("analogs"),
                    parsed_fields.get("machine_model"),
                    file_name
                ])
                print(f"[INFO] Wrote sheet row for {file_name}")
            except Exception as e:
                print(f"[ERROR] Failed to write sheet for {file_name}: {e}")
                traceback.print_exc()

            processed.append({
                "file": file_name,
                **parsed_fields
            })

        print(f"[INFO] Sleeping {PAUSE_SECONDS_BETWEEN_BATCHES}s before next batch")
        time.sleep(PAUSE_SECONDS_BETWEEN_BATCHES)

    return jsonify({"status": "done", "processed_count": len(processed), "processed": processed})


if __name__ == "__main__":
    check_requirements()
    port = int(os.getenv("PORT", 5000))
    print(f"[INFO] Starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
