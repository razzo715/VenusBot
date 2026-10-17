FROM python:3.11-slim

# Imposta variabili d'ambiente per Python e fuso orario italiano
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Rome

WORKDIR /app

# Installa le dipendenze di sistema necessarie per Discord Voice, FFmpeg, Opus, NodeJS (per yt-dlp-ejs) e SpeechRecognition
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libopus0 \
    libopus-dev \
    libsodium23 \
    libffi-dev \
    flac \
    nodejs \
    git \
    ca-certificates \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Runtime JavaScript usato da yt-dlp per le challenge di YouTube (il nodejs di Debian è troppo vecchio:
# yt-dlp richiede node >= 22). Senza, i download da YouTube sono più lenti o falliscono.
COPY --from=denoland/deno:bin-2.9.6 /deno /usr/local/bin/deno

# Installa prima le dipendenze Python per sfruttare la cache dei layer Docker
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copia il codice sorgente del bot
COPY . .

# Avvia VenusBot
CMD ["python", "venusbot.py"]
