import os
import traceback
import json
import smtplib
from email.mime.text import MIMEText
from flask import Flask, jsonify, render_template_string
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import gspread
from openai import OpenAI
import time

app = Flask(__name__)

REQUIRED_ENV_VARS = [
    "OPENAI_API_KEY", "SPREADSHEET_ID", 
    "TO_ANALYZE_FOLDER_ID", "ANALYZED_FOLDER_ID", 
    "REPORT_EMAIL"
]

HEADERS = [
    "Catalog Number", "Description", "Machine Type", 
    "Manufacturer", "Analogs", "Detail Description", 
    "Machine Model", "File URL"
]

progress_log = []  # Для отображения на UI

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
    return drive_service, sheet

def get_openai_client(model="gpt-4o-mini"):
    return OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def send_email_report(subject, body):
    to_email = os.getenv("REPORT_EMAIL")
    if not to_email:
        print("[WARNING] REPORT_EMAIL не задан, email не отправляется")
        return
    msg = MIMEText(body, "plain", "utf-8")
    msg['Subject'] = subject
    msg['From'] = to_email
    msg['To'] = to_email
    try:
        with smtplib.SMTP("localhost") as server:
            server.send_message(msg)
        print(f"[INFO] Отчет отправлен на {to_email}")
    except Exception as e:
        print(f"[ERROR] Не удалось отправить email: {e}")

@app.route("/")
def index():
    # UI с кнопками запуска анализа и повторного анализа UNKNOWN
    return render_template_string("""
    <h1>Анализ запчастей спецтехники</h1>
    <button onclick="fetch('/analyze', {method:'POST'}).then(r=>r.json()).then(console.log)">Запустить анализ</button>
    <button onclick="fetch('/reanalyze_unknown', {method:'POST'}).then(r=>r.json()).then(console.log)">Повтор анализа UNKNOWN</button>
    <h3>Прогресс:</h3>
    <pre id="log">{{ log }}</pre>
    """, log="\n".join(progress_log))

def analyze_files(files):
    drive, sheet = get_google_services()
    client = get_openai_client()
    processed = []
    batch_size = 5
    total = len(files)

    for i in range(0, total, batch_size):
        batch = files[i:i+batch_size]
        for f in batch:
            file_id, file_name = f["id"], f["name"]
            file_url = f.get("webContentLink") or f"https://drive.google.com/uc?export=download&id={file_id}"

            catalog_number = description = machine_type = manufacturer = analogs = detail_description = machine_model = "UNKNOWN"
            try:
                progress_log.append(f"[INFO] Анализ {file_name} ...")
                resp = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": "Ты эксперт по запчастям строительной техники."},
                        {"role": "user", "content":[
                            {"type":"text","text":"Проанализируй деталь и верни строго в формате: Catalog Number, Description, Machine Type, Manufacturer, Analogs, Detail Description, Machine Model"},
                            {"type":"image_url","image_url":{"url":file_url}}
                        ]}
                    ],
                    max_tokens=400
                )
                answer = resp.choices[0].message.content.strip()
                for line in answer.splitlines():
                    line_lower = line.lower()
                    if line_lower.startswith("catalog number"):
                        catalog_number = line.split(":",1)[1].strip()
                    elif line_lower.startswith("description"):
                        description = line.split(":",1)[1].strip()
                    elif line_lower.startswith("machine type"):
                        machine_type = line.split(":",1)[1].strip()
                    elif line_lower.startswith("manufacturer"):
                        manufacturer = line.split(":",1)[1].strip()
                    elif line_lower.startswith("analogs"):
                        analogs = line.split(":",1)[1].strip()
                    elif line_lower.startswith("detail description"):
                        detail_description = line.split(":",1)[1].strip()
                    elif line_lower.startswith("machine model"):
                        machine_model = line.split(":",1)[1].strip()
            except Exception as e:
                progress_log.append(f"[ERROR] {file_name} - {e}")
            
            processed.append({
                "file": file_name, "catalog_number": catalog_number,
                "description": description, "machine_type": machine_type,
                "manufacturer": manufacturer, "analogs": analogs,
                "detail_description": detail_description, "machine_model": machine_model,
                "file_url": file_url
            })
    return processed

@app.route("/analyze", methods=["POST"])
def analyze():
    drive, sheet = get_google_services()
    TO_ANALYZE = os.getenv("TO_ANALYZE_FOLDER_ID")
    try:
        results = drive.files().list(
            q=f"'{TO_ANALYZE}' in parents and mimeType contains 'image/'",
            fields="files(id,name,webContentLink)"
        ).execute()
        files = results.get("files",[])
        processed = analyze_files(files)
        send_email_report("Отчет анализа запчастей", json.dumps(processed, indent=2, ensure_ascii=False))
        return jsonify({"status":"done","processed_count":len(processed),"processed":processed})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"status":"error","message":str(e)}),500

@app.route("/reanalyze_unknown", methods=["POST"])
def reanalyze_unknown():
    # Чтение всех строк с UNKNOWN из Google Sheets
    drive, sheet = get_google_services()
    all_values = sheet.get_all_records()
    files_to_reanalyze = []
    for i, row in enumerate(all_values, start=2):
        if "UNKNOWN" in [row["Catalog Number"], row["Description"], row["Machine Type"]]:
            # Получаем ID файла из URL
            file_id = row["File URL"].split("id=")[-1]
            files_to_reanalyze.append({"id": file_id, "name": row["File URL"].split("/")[-1]})
    processed = analyze_files(files_to_reanalyze)
    send_email_report("Повторный анализ UNKNOWN", json.dumps(processed, indent=2, ensure_ascii=False))
    return jsonify({"status":"done","processed_count":len(processed),"processed":processed})

if __name__=="__main__":
    check_requirements()
    port = int(os.getenv("PORT",5000))
    app.run(host="0.0.0.0",port=port,debug=True)
