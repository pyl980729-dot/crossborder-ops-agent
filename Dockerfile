FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先装依赖，利用层缓存：改代码不会重装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 数据目录要可写（SQLite、日志、上传文件都落在这里）
RUN mkdir -p data/logs data/uploads

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=3)"

# 种子数据只在缺失时生成，避免覆盖用户已上传的数据
CMD ["sh", "-c", "[ -f data/seed/ads.csv ] || python scripts/gen_seed.py; exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1"]
