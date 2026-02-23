import os
from typing import Optional, Callable

import requests


class TelegramNotifier:
    """
    Minimal Telegram bot wrapper for sending plain text notifications.
    """

    def __init__(
        self,
        bot_token: Optional[str],
        chat_id: Optional[str],
        logger: Callable[[str], None],
    ):
        self.bot_token = (bot_token or "").strip()
        self.chat_id = (chat_id or "").strip()
        self.logger = logger
        self.enabled = bool(self.bot_token and self.chat_id)

    @classmethod
    def from_env(cls, logger: Callable[[str], None]) -> "TelegramNotifier":
        return cls(
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
            chat_id=os.getenv("TELEGRAM_CHAT_ID"),
            logger=logger,
        )

    def send(self, text: str) -> bool:
        if not self.enabled:
            return False
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": text}
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code != 200:
                self.logger(
                    f"[telegram] 发送失败: {resp.status_code} {resp.text[:200]}"
                )
                return False
            self.logger("[telegram] 已发送通知")
            return True
        except Exception as e:
            self.logger(f"[telegram] 请求异常: {e}")
            return False
