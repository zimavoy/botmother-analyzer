import os
import json
import time
import base64
import requests
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv

# Загружаем переменные окружения
load_dotenv()

app = Flask(__name__, static_folder="static", template_folder="static")

# Настройки Yandex Vision
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")
YANDEX_VISION_URL = "https://vision.api.cloud.yandex.net/vision/v1/batchAnalyze"

# Проверка
if not YANDEX_API_KEY or not YANDEX_FOLDER_ID:
    raise ValueError("❌ Отсутствует YANDEX_API_KEY или YANDEX_FOLDER_ID в .env")

# Хранилище прогресса и логов
progress = {"current": 0, "total": 0, "status": "Ожидание"}
analysis_log = []

def analyze_image_yandex(image_path):
    """Отправляет изображение в Yandex Vision API и возвращает результат анализа."""
    with open(image_path, "rb") as f:
        content = base64.b64encode(f.read()).decode("utf-8")

    payload = {
        "folderId": YANDEX_FOLDER_ID,
        "analyze_specs": [
            {
                "content": content,
                "features": [{"type": "TEXT_DETECTION"}]
            }
        ]
    }

    headers = {
        "Authorization": f"Api-Key {YANDEX_API_KEY}",
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(YANDEX_VISION_URL, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()

        text_blocks = []
        for page in data["results"][0]["results"][0]["textDetection"]["pages"]:
            for block in page.get("blocks", []):
                for line in block.get("lines", []):
                    text_blocks.append(" ".join([word["text"] for word in line["words"]]))

        return "\n".join(text_blocks) if text_blocks else "Текст не обнаружен"
    except Exception as e:
        return f"Ошибка: {str(e)}"

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/analyze", methods=["POST"])
def analyze():
    files = request.files.getlist("images")
    if not files:
        return jsonify({"error": "Файлы не загружены"}), 400

    total = len(files)
    progress["total"] = total
    progress["current"] = 0
    progress["status"] = "Выполняется анализ..."
    analysis_log.clear()

    results = []
    for i, file in enumerate(files, start=1):
        filename = file.filename
        path = os.path.join("uploads", filename)
        os.makedirs("uploads", exist_ok=True)
        file.save(path)

        result_text = analyze_image_yandex(path)
        results.append({"filename": filename, "result": result_text})
        analysis_log.append(f"{filename}: {result_text[:100]}...")

        progress["current"] = i
        time.sleep(1)

    progress["status"] = "Анализ завершён ✅"
    return jsonify(results)

@app.route("/progress")
def get_progress():
    return jsonify(progress)

@app.route("/logs")
def get_logs():
    return jsonify({"logs": analysis_log})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
