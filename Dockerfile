FROM python:3.12-slim
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ ./bot/

# Healthcheck : vérifie que le heartbeat file a été touché dans les 90s.
# Le bot le touche toutes les 30s via bot/utils/health.py tant qu'il est ready.
# start-period laisse le temps au bot de boot avant que ça compte.
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD test -f /tmp/respawn_radio_alive \
        && [ $(( $(date +%s) - $(stat -c %Y /tmp/respawn_radio_alive) )) -lt 90 ] \
        || exit 1

CMD ["python", "-m", "bot.main"]
