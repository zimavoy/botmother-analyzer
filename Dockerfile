# Базовый образ Python
FROM python:3.11-slim

# Устанавливаем системные зависимости
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Рабочая директория
WORKDIR /app

# Копируем зависимости
COPY requirements.txt .

# Устанавливаем Python-зависимости
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь проект
COPY . .

# Переменные окружения (PORT можно перезаписать в Render)
ENV PORT=5000

# Команда запуска через gunicorn
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]
