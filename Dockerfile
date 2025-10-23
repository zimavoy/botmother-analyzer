# Базовый Python
FROM python:3.11-slim

# Устанавливаем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем приложение
COPY . /app
WORKDIR /app

# Указываем порт
ENV PORT=5000
EXPOSE 5000

# Запуск Flask напрямую
CMD ["python", "app.py"]
