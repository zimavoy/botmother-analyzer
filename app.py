import os
import json
import time
import base64
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

REQUIRED_ENV_VARS = [
    "OPENAI_API_KEY",
    "SPREADSHEET_ID",
    "TO_ANALYZE_FOLDER_ID",
    "ANALYZED_FOLDER_ID"
]

BATCH_SIZE = 5
PAUSE_SECONDS_BETWEEN_BATCHES = 5
OPENAI_API_URL = "https://api.openai.com/v1/responses"  # используем REST напрямую


def check_requirements():
    missing = [v for v in REQUIRED_ENV_VARS if not os.getenv(v)]
    if missing:
        print(f"[ERROR] Отсутствуют переменные окружения: {missing}")
    if not os.path.exists("credentials.json"):
        print("[ERROR] credentials.json не найден!")
    print("[INFO] Проверка завершена.")


def get_google_services():
    """Возвращает (drive_service, sheet). Бросает исключение при ошибке."""
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
    """Скачивает содержимое файла из Google Drive через API и возвращает bytes."""
    try:
        fh = BytesIO()
        request = drive_service.files().get_media(fileId=file_id)
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
        fh.seek(0)
        return fh.read()
    except Exception as e:
        print(f"[ERROR] download_drive_file_bytes failed for {file_id}: {e}")
        traceback.print_exc()
        return None


def convert_bytes_to_base64_png(image_bytes):
    """Конвертирует байты изображения в base64 PNG (data URI)."""
    try:
        img = Image.open(BytesIO(image_bytes))
        # приводим к RGB если нужно (например, RGBA -> RGB)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        buf = BytesIO()
        # Если формат поддерживаемый, можно оставить, но безопасно сохранять как PNG
        img.save(buf, format="PNG")
        data = buf.getvalue()
        return "data:image/png;base64," + base64.b64encode(data).decode("utf-8")
    except Exception as e:
        print(f"[ERROR] convert_bytes_to_base64_png failed: {e}")
        traceback.print_exc()
        return None


def call_openai_via_rest(image_data_uri, system_prompt, user_prompt):
    """
    Отправляет запрос в OpenAI Responses API через requests.
    Возвращает текст ответа или None.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY не задан")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    # Формируем тело: model + input array. Используем строгий игровой prompt.
    body = {
        "model": "gpt-4o-mini",
        "input": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": user_prompt},
                    {"type": "input_image", "image_base64": image_data_uri.split(",", 1)[1]},
                ],
            },
        ],
        "max_output_tokens": 500,
    }

    try:
        resp = requests.post(OPENAI_API_URL, headers=headers, json=body, timeout=60)
        # логируем код и текст кратко
        print(f"[DEBUG] OpenAI REST status: {resp.status_code}")
        text = resp.text
        # Если ошибка — лог и возврат None
        if resp.status_code >= 400:
            print(f"[ERROR] OpenAI REST error: {resp.status_code} {text}")
            return None
        # Пытаемся извлечь текст из ответа
        j = resp.json()
        # Структура Responses API может содержать output[0].content[...]
        # Попробуем несколько мест, чтобы аккуратно достать текст
        # 1) output_text (if available)
        if "output_text" in j:
            return j["output_text"]
        # 2) output -> list of items -> each may have 'content' with 'text' or 'type'
        out = j.get("output") or j.get("outputs") or j.get("choices")
        if isinstance(out, list) and len(out) > 0:
            # пробуем собрать все текстовые части
            parts = []
            first = out[0]
            # case: first has 'content' which is list of dicts
            content = first.get("content") if isinstance(first, dict) else None
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "output_text":
                        parts.append(c.get("text") or c.get("content") or "")
                    elif isinstance(c, dict) and c.get("type") == "output_text":
                        parts.append(c.get("text") or "")
            # fallback: maybe there is 'text' field
            if parts:
                return "\n".join(parts)
        # fallback: try to stringify choices/text fields
        if "choices" in j and isinstance(j["choices"], list) and j["choices"]:
            ch = j["choices"][0]
            # new format: ch.message.content or ch.text
            if isinstance(ch, dict):
                if "message" in ch and isinstance(ch["message"], dict):
                    m = ch["message"]
                    if "content" in m:
                        # content could be array or dict
                        if isinstance(m["content"], dict):
                            return m["content"].get("text") or json.dumps(m["content"])
                        if isinstance(m["content"], list):
                            # join textual elements
                            txts = []
                            for elem in m["content"]:
                                if isinstance(elem, dict) and "text" in elem:
                                    txts.append(elem["text"])
                            if txts:
                                return "\n".join(txts)
                if "text" in ch:
                    return ch["text"]
        # last resort: return whole json as string
        return json.dumps(j)
    except Exception as e:
        print(f"[ERROR] OpenAI REST request failed: {e}")
        traceback.print_exc()
        return None


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
        return jsonify({"status": "error", "message": "Google API init failed: " + str(e)}), 500

    to_id = os.getenv("TO_ANALYZE_FOLDER_ID")
    analyzed_id = os.getenv("ANALYZED_FOLDER_ID")
    processed = []

    try:
        # get list of images in folder
        res = drive.files().list(
            q=f"'{to_id}' in parents and mimeType contains 'image/'",
            fields="files(id,name,mimeType)"
        ).execute()
        files = res.get("files", [])
        print(f"[INFO] Found {len(files)} files")
    except Exception as e:
        print("[ERROR] Drive list failed:", e)
        traceback.print_exc()
        return jsonify({"status": "error", "message": "Drive list failed: " + str(e)}), 500

    system_prompt = "Ты эксперт по запчастям строительной техники. Ответь в JSON."
    user_prompt = (
        "Верни JSON с полями: catalog_number, description, manufacturer, analogs, machine_model. "
        "Если не удалось распознать — значение поля должно быть 'UNKNOWN'."
    )

    for i in range(0, len(files), BATCH_SIZE):
        batch = files[i:i + BATCH_SIZE]
        print(f"[INFO] Processing batch {i // BATCH_SIZE + 1} with {len(batch)} files")
        for f in batch:
            file_id = f["id"]
            file_name = f.get("name", "unnamed")
            print(f"[INFO] Processing file {file_name} ({file_id})")
            # 1) Download bytes via Drive API (authorized) — robust even for private files if service account has access
            image_bytes = download_drive_file_bytes(drive, file_id)
            if not image_bytes:
                print(f"[WARN] Could not download {file_name}, skipping")
                continue
            # 2) Convert to base64 PNG (data URI)
            image_data_uri = convert_bytes_to_base64_png(image_bytes)
            if not image_data_uri:
                print(f"[WARN] Could not convert {file_name} to PNG base64, skipping")
                continue
            # 3) Call OpenAI via REST
            result_text = call_openai_via_rest(image_data_uri, system_prompt, user_prompt)
            if not result_text:
                print(f"[WARN] OpenAI returned no text for {file_name}, writing UNKNOWNs")
                # still move file and write UNKNOWNs
                catalog_number = description = manufacturer = analogs = machine_model = "UNKNOWN"
            else:
                # try parse as JSON
                catalog_number = description = manufacturer = analogs = machine_model = "UNKNOWN"
                try:
                    parsed = json.loads(result_text)
                    catalog_number = parsed.get("catalog_number", "UNKNOWN")
                    description = parsed.get("description", "UNKNOWN")
                    manufacturer = parsed.get("manufacturer", "UNKNOWN")
                    analogs = parsed.get("analogs", "UNKNOWN")
                    machine_model = parsed.get("machine_model", "UNKNOWN")
                except Exception:
                    print(f"[WARN] OpenAI response not JSON. Saving raw text into description for {file_name}")
                    description = result_text[:2000]  # avoid huge text

            # 4) move file to analyzed
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

            # 5) append to sheet
            try:
                sheet.append_row([catalog_number, description, manufacturer, analogs, machine_model, file_name])
                print(f"[INFO] Wrote row for {file_name}")
            except Exception as e:
                print(f"[ERROR] Failed to write sheet for {file_name}: {e}")
                traceback.print_exc()

            processed.append({
                "file": file_name,
                "catalog_number": catalog_number,
                "description": description,
                "manufacturer": manufacturer,
                "analogs": analogs,
                "machine_model": machine_model
            })

        print(f"[INFO] Sleeping {PAUSE_SECONDS_BETWEEN_BATCHES}s before next batch")
        time.sleep(PAUSE_SECONDS_BETWEEN_BATCHES)

    return jsonify({"status": "done", "processed_count": len(processed), "processed": processed})


if __name__ == "__main__":
    check_requirements()
    port = int(os.getenv("PORT", 5000, 5000))
    print(f"[INFO] Starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
