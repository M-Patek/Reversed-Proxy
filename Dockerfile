使用轻量级的 Python 基础镜像

FROM python:3.11-slim

设置工作目录

WORKDIR /app

安装必要的系统依赖，主要是 curl_cffi 需要的 cffi 库依赖

RUN apt-get update && 

apt-get install -y --no-install-recommends 

build-essential 

libssl-dev 

libffi-dev 

curl 

&& rm -rf /var/lib/apt/lists/*

复制并安装 Python 依赖

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

复制应用代码

COPY app/ app/

暴露 FastAPI 端口

EXPOSE 8000

启动 Uvicorn (使用 gunicorn 或直接 uvicorn 启动，这里使用 uvicorn 方便)

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
