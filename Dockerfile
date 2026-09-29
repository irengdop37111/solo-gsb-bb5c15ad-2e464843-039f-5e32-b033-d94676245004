FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    COC_DB_PATH=/data/custody.db

WORKDIR /app

# 先装依赖，利用层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080

# 容器内无 curl/wget，用 python 做健康检查
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.getenv('COC_PORT','8080')+'/health',timeout=3)" || exit 1

CMD ["sh", "-c", "uvicorn app.main:app --host ${COC_HOST:-0.0.0.0} --port ${COC_PORT:-8080}"]
