FROM python:3.11-slim-bookworm

# Chromium + its matching chromedriver come from the same Debian repo here,
# so they're guaranteed to be version-compatible (this is what the
# "InvalidSessionIdException" / version-mismatch crashes locally usually
# come down to when webdriver_manager grabs a driver that doesn't match
# whatever Chrome happens to be installed).
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        chromium-driver \
        fonts-liberation \
        libnss3 \
        libatk-bridge2.0-0 \
        libgtk-3-0 \
        libasound2 \
        libgbm1 \
        libxshmfence1 \
    && rm -rf /var/lib/apt/lists/*

ENV CHROME_BIN=/usr/bin/chromium
ENV CHROMEDRIVER_PATH=/usr/bin/chromedriver
ENV DATA_DIR=/data
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY culc.py .

RUN mkdir -p /data

CMD ["python", "culc.py"]
