FROM python:3.12-slim

# Time zone fallback; Home Assistant passes its own TZ to apps, which overrides this.
ENV TZ=America/Chicago \
    PYTHONUNBUFFERED=1 \
    FRAME_DATA=/data \
    FRAME_PORT=8800

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY frame_app.py seed_state.json /app/
COPY shared/ /app/shared/

WORKDIR /app
EXPOSE 8800
CMD ["python3", "/app/frame_app.py"]
