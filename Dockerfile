FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Runs unprivileged. Create ./data on the host owned by this uid, or
# the first write will fail:  mkdir -p data && sudo chown 10001 data
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin adaptive \
 && mkdir -p /data && chown adaptive:adaptive /data
USER adaptive

VOLUME ["/data"]
EXPOSE 8099

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8099"]
