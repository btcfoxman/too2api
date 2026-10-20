FROM ghcr.io/btcfoxman/ijm2api:64df29824ccdc6c44823a0a7ce14c44834048d9b

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TOO_CHROME_EXECUTABLE=/usr/bin/chromium \
    TOO_CHROME_USER_DATA_ROOT=/app/data/chrome-profiles \
    TOO_DATA_DIR=/app/data

WORKDIR /app
COPY app ./app
RUN mkdir -p /app/data/chrome-profiles

EXPOSE 8812
CMD ["xvfb-run", "-a", "-s", "-screen 0 1280x960x24 -nolisten tcp", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8812"]
