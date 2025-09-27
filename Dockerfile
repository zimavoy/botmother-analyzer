# Базовый образ Python
FROM python:3.11-slim

# Устанавливаем зависимости для работы с Google API
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Рабочая директория в контейнере
WORKDIR /app

# Копируем зависимости
COPY requirements.txt .

# Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь проект
COPY . .

# Flask будет слушать этот порт
ENV PORT=5000

# Команда запуска (через gunicorn)
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]
