FROM python:3.12-slim

ENV TZ=America/Chicago \
    PYTHONUNBUFFERED=1

# Chromium draws the kitchen board for the Frame (headless, local pages only).
# It comes from Debian and updates with the image's security patches when the app is rebuilt.
# Fonts beyond Archivo's Latin: Chinese, Japanese and Korean (album titles like "fête：祝祭"),
# and color emoji (the ☠️ on the roads board).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata chromium fonts-noto-cjk fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY house/ /app/

WORKDIR /app
CMD ["python3", "/app/server.py"]
