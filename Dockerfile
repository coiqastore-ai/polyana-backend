FROM python:3.12-slim

WORKDIR /app

# Deps first for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code (fonts in assets/ are needed by invite.py)
COPY . .

ENV PORT=8000
EXPOSE 8000

# Single worker: aiogram long-polling runs inside the FastAPI process (one bot only).
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
