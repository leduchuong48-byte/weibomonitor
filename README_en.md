# Weibo Monitor

[中文](README.md)

A `FastAPI + React` tool for downloading and monitoring Weibo media content.

## Features

- Modes: full, date range, latest, batch links, polling monitor
- Media types: image, video, live photo
- Web UI for tasks, logs, and history
- Optional Telegram notifications

## Quick Start

```bash
docker compose up -d --build
```

Default URL: `http://localhost:1003`

## Configuration

```bash
cp .env.example .env
```

Optional environment variables:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

## Security & Privacy

- Never commit real Weibo cookie, logs, tokens, or downloaded media
- Keep runtime-sensitive files out of version control

## Disclaimer

By using this project, you acknowledge and agree to the [Disclaimer](DISCLAIMER.md).
