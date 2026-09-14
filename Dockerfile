# Karman: бот + API + Mini App одним процессом. Подходит для любого Docker-хостинга.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Хостинги передают порт в PORT; локально в Docker — 8080.
EXPOSE 8080
CMD ["python", "bot.py"]
