FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY entrypoint.sh /usr/local/bin/entrypoint.sh

RUN useradd -u 10001 -m appuser \
    && mkdir -p /data /image \
    && chown -R appuser:appuser /app /data /image \
    && sed -i 's/\r$//' /usr/local/bin/entrypoint.sh \
    && chmod +x /usr/local/bin/entrypoint.sh

ENV APEX_DATA=/data
ENV APEX_IMAGE=/image

# 一个容器同时监听两个端口：管理后台 8000、用户端 portal 8080。
# 两套路由仍是各自独立的 app，只是跑在同一个进程里，详见 app/serve.py。
EXPOSE 8000 8080

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "app.serve"]
