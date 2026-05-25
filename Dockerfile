# Image Python avec FFmpeg préinstallé
FROM python:3.11-slim

# Installation de FFmpeg
RUN apt-get update && \
    apt-get install -y ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Dossier de travail
WORKDIR /app

# Installation des dépendances Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copie du code
COPY app.py .

# Lancement avec Gunicorn (serveur de production)
CMD gunicorn --bind 0.0.0.0:$PORT --timeout 300 app:app
