FROM python:3.13-slim

# tzdata: horários do /nowplaying e /stats no fuso de TZ (padrão: Brasília).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=America/Sao_Paulo \
    DATABASE_PATH=/data/games.db

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./

# Monte um volume aqui para o SQLite sobreviver aos redeploys.
VOLUME /data

CMD ["python", "main.py"]
