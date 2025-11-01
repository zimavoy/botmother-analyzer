import os
import time
import base64
import requests
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__, static_folder="static", template_folder="static")

# Переменные окружения
YANDEX_ACCESS_KEY_ID = os.getenv("YANDEX_ACCESS_KEY_ID")
YANDEX_SECRET_ACCESS_KEY = os.getenv("YANDEX_SECRET_ACCESS_KEY")
YANDEX_FOLDER_ID = os.getenv("YANDEX_FOLDER_ID")

# Переменная для хранения IAM токена
IAM_TOKEN = None
TOKEN_EXPIRES_AT = 0


# ==================== ФУНКЦИЯ АВТОМАТИЧЕСКОГО ПОЛУЧЕНИЯ IAM ТОКЕНА ====================
def get_iam_token():
    """
    Получает IAM токен из Yandex Cloud по постоянным ключам доступа.
    Автоматически обновляет, если токен устарел.
    """
    global IAM_TOKEN, TOKEN_EXPIRES_AT

    if IAM_TOKEN and time.time() < TOKEN_EXPIRES_AT - 60:
        return IAM_TOKEN  # токен ещё действителен

    url = "https://iam.api.cloud.yandex.net/iam/v1/tokens"
    data = {
        "yandexPassportOauthToken": None,
        "jwt": None
    }

    response = requests.post(
        url="https://iam.api.cloud.yandex.net/iam/v1/tokens",
        headers={"Content-Type": "application/json"},
        json={
            "yandexPassportOauthToken": None,
            "jwt": None
        },
        auth=(YANDEX_ACCESS_KEY_ID, YANDEX_SECRET_ACCESS_KEY)
    )

    if response.status_code == 200:
        token_data = response.json()
        IAM_TOKEN = token_data["iamToken"]
        TOKEN_EXPIRES_AT = time.time() + 3600 * 12  # 12 часов действия
        print("✅ Новый IAM-токен успешно получен.")
        return IAM_TOKEN
    else:
        print("❌ Ошибка получения IAM-токена:", response.text)
        return None


# ==================== АНАЛИЗ ИЗОБРАЖЕНИЯ ====================
def analyze_image_yandex(image_path):
    """
    Анализирует изображение через Yandex Vision API.
    Возвращает описание содержимого.
    """
    token = get_iam_token()
    if not token:
        return {"status": "error", "message": "Не удалось получить IAM токен"}

    with open(image_path, "rb") as f:
        image_base64 = base64.b64encode(f.read()).decode("utf-8")

    url = "https://vision.api.cloud.yandex.net/vision/v1/batchAnalyze"
    payload = {
        "folderId": YANDEX_FOLDER_ID,
        "analyze_specs": [
            {
                "content": image_base64,
                "features": [{"type": "CLASSIFICATION"}]
            }
        ]
    }

    headers = {"Authorization": f"Bearer {token}"}
    response = requests.post(url, headers=headers, json=payload)

    if response.status_code != 200:
        return {"status": "error", "message": response.text}

    try:
        result = response.json()
        classes = result["results"][0]["results"][0]["classification"]["properties"]
        description = ", ".join([f"{c['name']} ({c['probability']:.2f})" for c in classes])
        return {"status": "ok", "description": description}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ==================== FLASK UI ====================
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    if "images" not in request.files:
        return jsonify({"status": "error", "message": "Файлы не найдены"})

    images = request.files.getlist("images")
    results = []

    for img in images:
        path = os.path.join("static", "uploads", img.filename)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        img.save(path)

        res = analyze_image_yandex(path)
        results.append({"filename": img.filename, **res})

    return jsonify({"status": "ok", "results": results})


@app.route("/limits")
def limits():
    """Просто фейковая демонстрация лимитов для UI (API Яндекса их не даёт напрямую)."""
    token_time_left = int((TOKEN_EXPIRES_AT - time.time()) / 60)
    return jsonify({
        "status": "ok",
        "token_valid_minutes": max(token_time_left, 0),
        "message": "IAM токен обновляется автоматически"
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
