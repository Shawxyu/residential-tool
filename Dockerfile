# 深圳住区形态分析工具 —— Hugging Face Spaces 部署
FROM python:3.11-slim

WORKDIR /app

# 系统依赖：geopandas / osmnx / pyogrio 需要 GDAL、GEOS、spatialindex
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgdal-dev \
    libgeos-dev \
    libspatialindex-dev \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制整个项目（含 server/、web/、data/）
COPY . .

EXPOSE 7860

# Hugging Face 会把端口号写入环境变量 PORT（默认 7860）
CMD ["sh", "-c", "cd /app/server && uvicorn app:app --host 0.0.0.0 --port ${PORT:-7860}"]
