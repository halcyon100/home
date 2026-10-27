FROM python:3.12-slim

ENV TZ=America/Chicago \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY circadian/ /app/

WORKDIR /app
CMD ["python3", "/app/app.py"]
