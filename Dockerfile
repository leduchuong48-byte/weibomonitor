ARG BASE_IMAGE=python:3.9-slim
ARG PIP_INDEX_URL=
ARG NODE_IMAGE=node:18-alpine
ARG NPM_REGISTRY=
ARG DEBIAN_MIRROR=

FROM ${NODE_IMAGE} AS frontend

WORKDIR /frontend
COPY frontend/package*.json ./

RUN if [ -n "$NPM_REGISTRY" ]; then npm config set registry "$NPM_REGISTRY"; fi \
    && npm install --no-audit --no-fund

COPY frontend/ .
RUN npm run build

FROM ${BASE_IMAGE}
ARG DEBIAN_MIRROR

WORKDIR /app/backend

# 安装 ffmpeg 用于 Live Photo 转 mp4
RUN set -eux; \
    mirror="${DEBIAN_MIRROR%/}"; \
    if [ -n "$mirror" ]; then \
      if [ -f /etc/apt/sources.list ]; then \
        sed -i "s|http://deb.debian.org/debian|$mirror/debian|g" /etc/apt/sources.list; \
        sed -i "s|https://deb.debian.org/debian|$mirror/debian|g" /etc/apt/sources.list; \
        sed -i "s|http://deb.debian.org/debian-security|$mirror/debian-security|g" /etc/apt/sources.list; \
        sed -i "s|https://deb.debian.org/debian-security|$mirror/debian-security|g" /etc/apt/sources.list; \
        sed -i "s|http://security.debian.org/debian-security|$mirror/debian-security|g" /etc/apt/sources.list; \
        sed -i "s|https://security.debian.org/debian-security|$mirror/debian-security|g" /etc/apt/sources.list; \
      fi; \
      if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i "s|http://deb.debian.org/debian|$mirror/debian|g" /etc/apt/sources.list.d/debian.sources; \
        sed -i "s|https://deb.debian.org/debian|$mirror/debian|g" /etc/apt/sources.list.d/debian.sources; \
        sed -i "s|http://deb.debian.org/debian-security|$mirror/debian-security|g" /etc/apt/sources.list.d/debian.sources; \
        sed -i "s|https://deb.debian.org/debian-security|$mirror/debian-security|g" /etc/apt/sources.list.d/debian.sources; \
        sed -i "s|http://security.debian.org/debian-security|$mirror/debian-security|g" /etc/apt/sources.list.d/debian.sources; \
        sed -i "s|https://security.debian.org/debian-security|$mirror/debian-security|g" /etc/apt/sources.list.d/debian.sources; \
      fi; \
    fi; \
    apt-get update \
    && apt-get install -y ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .

RUN pip install --no-cache-dir ${PIP_INDEX_URL:+-i ${PIP_INDEX_URL}} -r requirements.txt

COPY backend/ .

# 拷贝前端构建产物（React）
COPY --from=frontend /frontend/dist /app/backend/static/spa

# 挂载目录：配置 & 媒体文件
VOLUME ["/app/data", "/app/weibo_media"]

EXPOSE 8000

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
