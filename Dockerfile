# Используем стабильный образ Python
FROM python:3.11-slim

# Устанавливаем зависимости системы (нужны для google-api-python-client и gspread)
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Устанавливаем рабочую директорию
WORKDIR /app

# Копируем файлы проекта
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Открываем порт
EXPOSE 5000

# Запуск через Gunicorn (рекомендовано Render)
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]
