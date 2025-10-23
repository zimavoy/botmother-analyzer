# Используем официальный Python
FROM python:3.11-slim

# Устанавливаем зависимости системы
RUN apt-get update && apt-get install -y \
    build-essential \
    libjpeg-dev \
    zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Копируем проект
WORKDIR /app
COPY . /app

# Устанавливаем Python-зависимости
RUN pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# Экспортируем порт
EXPOSE 5000

# Запуск приложения через gunicorn
CMD ["gunicorn", "-b", "0.0.0.0:5000", "app:app"]
