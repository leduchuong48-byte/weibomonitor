# Weibo Monitor

基于 `FastAPI + React` 的微博媒体下载与监控工具。支持通过登录态 Cookie 抓取微博图片、视频、Live Photo，并提供 Web 页面管理任务与查看日志。

## 功能

- 下载模式：全量、时间段、最新一条、批量链接、监控轮询
- 媒体类型：图片、视频、Live Photo（mov 可转 mp4）
- Web 管理界面：提交任务、查看日志、查看历史
- 可选 Telegram 通知

## 项目结构

```text
.
├── backend/
├── frontend/
├── data/
├── docker-compose.yml
├── Dockerfile
└── README.md
```

## 运行方式（Docker Compose）

```bash
docker compose up -d --build
```

访问地址：`http://localhost:1003`

## 配置

1. 复制环境变量模板：

```bash
cp .env.example .env
```

2. 可选填写 Telegram：
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

3. 默认数据目录挂载：
- `./data -> /app/data`
- `./weibo_media -> /app/weibo_media`

## 隐私与安全

- 不要提交真实微博 Cookie、日志、下载内容、Token。
- 仓库已忽略：`data/cookie.txt`、`data/weibo.log`、`.env`、`weibo_media/`。
- 发布前建议再次执行敏感信息扫描。

## 免责声明

仅用于个人备份与学习用途，请遵守平台规则与当地法律法规。
