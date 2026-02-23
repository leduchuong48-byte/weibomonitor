# weibo_crawler.py
import re
import os
import time
import random
import shutil
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime, date
from http.cookiejar import Cookie, CookieJar, MozillaCookieJar
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Dict, Any
from urllib.parse import urlsplit

import requests
import urllib3
from lxml import html as lxml_html
from telegram_bot import TelegramNotifier

# 关闭 weibo.cn https 证书警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0_0) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)


def default_logger(msg: str) -> None:
    print(msg)


def parse_html_safely(html_text: str):
    """
    解决 lxml: Unicode strings with encoding declaration are not supported 问题。

    weibo.cn / weibo.com 页面有时会在最前面加：
        <?xml version="1.0" encoding="utf-8"?>
    直接把 Unicode 字符串丢给 fromstring 会报错。
    做法：
      - 如果是 str，先用 utf-8 编码成 bytes
      - 再用 HTML 解析器去解析
    """
    if isinstance(html_text, str):
        data = html_text.encode("utf-8", errors="ignore")
    else:
        data = html_text
    return lxml_html.fromstring(data)


@dataclass
class WeiboStatus:
    mblogid: str
    created_at: Optional[datetime]
    raw_json: Dict[str, Any]


class WeiboClient:
    def __init__(
        self,
        cookie_str: str,
        logger: Callable[[str], None] = default_logger,
        cookie_jar: Optional[CookieJar] = None,
    ):
        self.cookie_str = (cookie_str or "").strip()
        self.logger = logger
        self.session = requests.Session()
        self.cookie_jar = cookie_jar
        if self.cookie_jar:
            self.session.cookies.update(self.cookie_jar)
        xsrf = self._extract_xsrf_token(self.cookie_str, self.cookie_jar)
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Connection": "keep-alive",
            "Origin": "https://weibo.com",
            "Referer": "https://weibo.com/",
            "X-Requested-With": "XMLHttpRequest",
        }
        if self.cookie_str and not self.cookie_jar:
            headers["Cookie"] = self.cookie_str
        if xsrf:
            headers["X-XSRF-TOKEN"] = xsrf
            headers["XSRF-TOKEN"] = xsrf
        self.session.headers.update(headers)

    @staticmethod
    def _extract_xsrf_token(
        cookie_str: str, cookie_jar: Optional[CookieJar] = None
    ) -> Optional[str]:
        if cookie_jar:
            for c in cookie_jar:
                if c.name == "XSRF-TOKEN":
                    return c.value
        for part in cookie_str.split(";"):
            if "XSRF-TOKEN" in part:
                kv = part.strip().split("=", 1)
                if len(kv) == 2:
                    return kv[1]
        return None

    def set_uid_headers(self, uid: str):
        # 某些接口需要以用户页为 Referer
        self.session.headers["Referer"] = f"https://weibo.com/u/{uid}"

    def _sleep(self):
        time.sleep(random.uniform(0.6, 1.3))

    def get(self, url: str, **kwargs) -> requests.Response:
        self._sleep()
        self.logger(f"[request] GET {url}")
        resp = self.session.get(url, timeout=15, verify=False, **kwargs)
        return resp

    def get_html(self, url: str, **kwargs) -> str:
        resp = self.get(url, **kwargs)
        text = resp.text
        if "登陆 - 新" in text or "登录 - 新浪" in text or "输入密码" in text:
            raise RuntimeError("疑似跳转登录页，Cookie 可能失效")
        return text

    def get_json(
        self, url: str, params: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        self._sleep()
        self.logger(f"[request] JSON {url} params={params}")
        resp = self.session.get(
            url, params=params, timeout=15, verify=False, allow_redirects=False
        )
        text = (resp.text or "").strip()
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location")
            self.logger(f"[json] 接口重定向 {resp.status_code} -> {loc}")
            return None
        if resp.status_code != 200:
            self.logger(
                f"[json] 非 200 status={resp.status_code} ct={resp.headers.get('Content-Type')} 片段: {text[:200]!r}"
            )
        if not text:
            self.logger(
                f"[json] 空响应 status={resp.status_code} ct={resp.headers.get('Content-Type')}"
            )
            return None
        if text.startswith("<!DOCTYPE html") or text.startswith("<html"):
            # 返回了 HTML 错误页
            m = re.search(r"retcode=(\\d+)", text)
            if m:
                self.logger(f"[json] 收到 HTML，retcode={m.group(1)}，可能 Cookie 失效或接口限制")
            else:
                self.logger("[json] 收到 HTML，可能 Cookie 失效或接口限制")
            return None
        try:
            return resp.json()
        except Exception as e:
            self.logger(
                f"[json] 解析失败: {e} status={resp.status_code} ct={resp.headers.get('Content-Type')} 片段: {text[:200]!r}"
            )
            return None

    def download_binary(self, url: str, save_path: Path):
        save_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = save_path.with_suffix(save_path.suffix + ".part")

        self.logger(f"[download] {url} -> {save_path}")
        with self.session.get(url, stream=True, timeout=30, verify=False) as r:
            r.raise_for_status()
            total = int(r.headers.get("Content-Length") or 0)
            downloaded = 0
            with open(tmp_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = downloaded * 100 // total
                        self.logger(f"  .. {pct}% ({downloaded}/{total} bytes)")
        tmp_path.replace(save_path)


class WeiboCrawler:
    def __init__(
        self,
        cookie_str: str,
        download_root: Path,
        logger: Callable[[str], None] = default_logger,
        notifier: Optional[TelegramNotifier] = None,
        cookie_jar: Optional[CookieJar] = None,
    ):
        self.logger = logger
        self.cookie_str = self.normalize_cookie_str(cookie_str)
        self.cookie_jar = cookie_jar
        if not self.cookie_str and self.cookie_jar:
            self.cookie_str = self.cookie_str_from_jar(self.cookie_jar)
        self.allow_weibo_cn_fallback = self._env_flag(
            "WEIBO_CN_FALLBACK", default=False
        )
        self.avoid_wm_url = self._env_flag("WEIBO_AVOID_WM_URL", default=True)
        self._assert_cookie_fields()
        self.client = WeiboClient(
            self.cookie_str, logger=logger, cookie_jar=self.cookie_jar
        )
        self.download_root = download_root
        self.has_ffmpeg = shutil.which("ffmpeg") is not None
        self.notifier = notifier

    # -------------------- 公共入口 -------------------- #

    @staticmethod
    def _env_flag(name: str, default: bool = False) -> bool:
        v = os.getenv(name)
        if v is None:
            return default
        return v.strip().lower() in {"1", "true", "yes", "y", "on"}

    @staticmethod
    def is_netscape_cookie_text(text: str) -> bool:
        lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
        if not lines:
            return False
        if lines[0].lower().startswith("# netscape http cookie file"):
            return True
        for line in lines:
            if line.startswith("#"):
                continue
            if len(line.split("\t")) >= 7:
                return True
        return False

    @staticmethod
    def _parse_netscape_cookie_text(
        text: str, logger: Callable[[str], None] = default_logger
    ) -> CookieJar:
        jar = CookieJar()
        if not text:
            return jar

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            http_only = False
            if line.startswith("#HttpOnly_"):
                http_only = True
                line = line[len("#HttpOnly_") :]
            elif line.startswith("#"):
                continue

            parts = line.split("\t")
            if len(parts) < 7:
                parts = re.split(r"\s+", line)
            if len(parts) < 7:
                continue

            domain, include_subdomains, path, secure, expires, name, value = parts[:7]
            domain = (domain or "").strip()
            name = (name or "").strip()
            if not domain or not name:
                continue

            include_subdomains = include_subdomains.upper() == "TRUE"
            secure = secure.upper() == "TRUE"
            try:
                expires_int = int(expires)
                if expires_int <= 0:
                    expires_int = None
            except Exception:
                expires_int = None

            domain_initial_dot = domain.startswith(".") or include_subdomains
            if include_subdomains and not domain.startswith("."):
                domain = "." + domain
                domain_initial_dot = True

            rest = {"HttpOnly": True} if http_only else {}
            cookie = Cookie(
                version=0,
                name=name,
                value=value,
                port=None,
                port_specified=False,
                domain=domain,
                domain_specified=True,
                domain_initial_dot=domain_initial_dot,
                path=path or "/",
                path_specified=True,
                secure=secure,
                expires=expires_int,
                discard=expires_int is None,
                comment=None,
                comment_url=None,
                rest=rest,
                rfc2109=False,
            )
            jar.set_cookie(cookie)

        if not any(True for _ in jar):
            logger("[cookie] 未解析到任何有效的 Netscape Cookie 行")
        return jar

    @staticmethod
    def load_cookie_jar(
        cookie_file: Path, logger: Callable[[str], None] = default_logger
    ) -> Optional[CookieJar]:
        try:
            text = cookie_file.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            logger(f"[cookie] 读取失败: {e}")
            return None

        if not text.strip():
            return None

        jar = MozillaCookieJar()
        try:
            jar.load(
                str(cookie_file), ignore_discard=True, ignore_expires=True
            )
        except Exception as e:
            logger(f"[cookie] Netscape Cookie 加载失败: {e}")
            jar = None

        if not jar or not any(c.name == "SUB" for c in jar):
            fallback = WeiboCrawler._parse_netscape_cookie_text(text, logger=logger)
            if any(c.name == "SUB" for c in fallback):
                return fallback
            return jar or fallback

        return jar

    @staticmethod
    def cookie_str_from_jar(cookie_jar: CookieJar) -> str:
        parts = []
        for c in cookie_jar:
            if not c.name:
                continue
            parts.append(f"{c.name}={c.value}")
        return "; ".join(parts)

    @staticmethod
    def normalize_cookie_str(raw: str) -> str:
        """
        兼容用户从「复制请求头」里粘贴的 Cookie：
        - 允许带前缀 "Cookie: ..."
        - 允许带换行/多余空格
        最终统一为 "k1=v1; k2=v2" 形式。
        """
        s = (raw or "").strip().strip('"').strip("'").strip()
        if not s:
            return ""

        # 先去掉首行 Cookie: 前缀
        s = re.sub(r"(?i)^cookie\\s*:\\s*", "", s).strip()
        # 再把分隔符统一拆开重组
        parts: List[str] = []
        for seg in re.split(r"[;\\r\\n]+", s):
            seg = seg.strip()
            if not seg:
                continue
            seg = re.sub(r"(?i)^cookie\\s*:\\s*", "", seg).strip()
            if "=" not in seg:
                continue
            parts.append(seg)
        return "; ".join(parts)

    @staticmethod
    def normalize_uid(uid_or_url: str) -> str:
        s = uid_or_url.strip()
        # 纯数字
        if s.isdigit():
            return s
        parts = urlsplit(s)
        host = (parts.hostname or "").lower()
        if parts.scheme or parts.netloc:
            if not (host.endswith("weibo.com") or host.endswith("weibo.cn")):
                raise ValueError("仅支持 weibo.com / weibo.cn 链接")
        path = parts.path or s
        # URL: https://weibo.com/u/xxxx
        m = re.search(r"/u/(\d+)", path)
        if m:
            return m.group(1)
        # URL: https://weibo.com/xxxxxx/abcd
        m = re.search(r"/(\d{5,})", path)
        if m:
            return m.group(1)
        raise ValueError(f"无法从 {uid_or_url!r} 中解析出 UID")

    @staticmethod
    def normalize_status_id(status_url: str) -> str:
        s = (status_url or "").strip()
        if not s:
            raise ValueError("链接为空")
        if re.fullmatch(r"[0-9A-Za-z]+", s):
            return s
        parts = urlsplit(s)
        host = (parts.hostname or "").lower()
        if parts.scheme or parts.netloc:
            if not (host.endswith("weibo.com") or host.endswith("weibo.cn")):
                raise ValueError("仅支持 weibo.com / weibo.cn 链接")
        path = parts.path or ""
        m = re.search(r"/detail/([0-9A-Za-z]+)", path)
        if m:
            return m.group(1)
        m = re.search(r"/status/([0-9A-Za-z]+)", path)
        if m:
            return m.group(1)
        segments = [seg for seg in path.split("/") if seg]
        if len(segments) >= 2:
            if segments[-2] in {"u", "profile"}:
                raise ValueError("检测到用户主页链接")
            last = segments[-1]
            if re.fullmatch(r"[0-9A-Za-z]+", last):
                return last
        raise ValueError(f"无法从链接中解析微博 ID: {status_url!r}")

    def _assert_cookie_fields(self):
        """
        提前检查 Cookie 是否缺少关键字段，避免请求时被重定向到 HTML。
        """
        if self.cookie_jar:
            cookie_names = {c.name for c in self.cookie_jar if c.name}
            if "SUB" not in cookie_names:
                raise RuntimeError(
                    "Cookie 缺少 SUB 字段：请从 weibo.com / weibo.cn 登录态导出完整 Cookie"
                )
            for k in ("SUBP", "XSRF-TOKEN", "WBPSESS"):
                if k not in cookie_names:
                    self.logger(
                        f"[提示] Cookie 未包含 {k}，部分接口可能受限（如遇失败请更新 Cookie）"
                    )
            if self.allow_weibo_cn_fallback and "_T_WM" not in cookie_names:
                self.logger(
                    "[提示] Cookie 未包含 _T_WM（仅在启用 WEIBO_CN_FALLBACK 时可能需要）"
                )
            return

        cookie_parts = {
            p.strip().split("=", 1)[0]: p
            for p in self.cookie_str.split(";")
            if "=" in p
        }
        if "SUB" not in cookie_parts:
            raise RuntimeError(
                "Cookie 缺少 SUB 字段：请从 weibo.com / weibo.cn 登录态复制完整 Cookie"
            )
        # 非致命字段仅提示（不同端的 Cookie 字段不完全一致）
        for k in ("SUBP", "XSRF-TOKEN", "WBPSESS"):
            if k not in cookie_parts:
                self.logger(
                    f"[提示] Cookie 未包含 {k}，部分接口可能受限（如遇失败请更新 Cookie）"
                )
        # _T_WM 主要用于移动端/ weibo.cn，仅在启用回退时提示
        if self.allow_weibo_cn_fallback and "_T_WM" not in cookie_parts:
            self.logger(
                "[提示] Cookie 未包含 _T_WM（仅在启用 WEIBO_CN_FALLBACK 时可能需要）"
            )

    @staticmethod
    def _extract_ok_msg(j: Dict[str, Any]) -> Dict[str, Any]:
        ok = j.get("ok")
        msg = ""
        for k in ("msg", "message", "error", "errmsg", "error_msg"):
            v = j.get(k)
            if isinstance(v, str) and v.strip():
                msg = v.strip()
                break
        data = j.get("data")
        if not msg and isinstance(data, dict):
            for k in ("msg", "message", "error", "errmsg"):
                v = data.get(k)
                if isinstance(v, str) and v.strip():
                    msg = v.strip()
                    break
        return {"ok": ok, "msg": msg}

    @staticmethod
    def _extract_error_url(j: Dict[str, Any]) -> str:
        url = j.get("url")
        if isinstance(url, str) and url.strip():
            return url.strip()
        data = j.get("data")
        if isinstance(data, dict):
            url2 = data.get("url")
            if isinstance(url2, str) and url2.strip():
                return url2.strip()
        return ""

    def _format_api_error(self, name: str, j: Dict[str, Any]) -> str:
        meta = self._extract_ok_msg(j)
        ok = meta.get("ok")
        msg = meta.get("msg") or ""
        url = self._extract_error_url(j)
        parts = [f"ok={ok}"]
        if msg:
            parts.append(f"msg={msg}")
        if url:
            parts.append(f"url={url}")
        suffix = ""
        if "login.php" in url:
            suffix = "（接口返回登录页，说明当前请求未被识别为已登录，请更新 weibo.com Cookie）"
        return f"{name} 接口失败：{' '.join(parts)}{suffix}"

    def fetch_user_profile(self, uid: str) -> Dict[str, Any]:
        self.client.set_uid_headers(uid)
        url = "https://weibo.com/ajax/profile/info"
        j = self.client.get_json(url, params={"uid": uid})
        if not isinstance(j, dict):
            raise RuntimeError("profile/info 返回空：Cookie 可能失效或被风控")

        meta = self._extract_ok_msg(j)
        data = j.get("data") if isinstance(j.get("data"), dict) else {}
        user = j.get("user") or data.get("user") or {}
        if not isinstance(user, dict) or not user:
            raise RuntimeError(self._format_api_error("profile/info", j))

        return {
            "uid": uid,
            "screen_name": user.get("screen_name") or "",
            "statuses_count": user.get("statuses_count") or 0,
            "followers": user.get("followers_count") or 0,
        }

    def run(
        self,
        uid: str,
        mode: str = "all",  # "all" / "range" / "latest"
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
        nickname: Optional[str] = None,
        pack: bool = True,
        stop_event: Optional[threading.Event] = None,
    ):
        uid = self.normalize_uid(uid)
        if not nickname:
            profile = self.fetch_user_profile(uid)
            nickname = profile.get("screen_name") or uid

        self.logger(f"=== 开始爬取用户 [{nickname}] (UID: {uid}) 的媒体 ===")
        self.logger(f"模式: {mode}")
        if mode == "range":
            self.logger(f"时间范围: {start_date or '不限'} ~ {end_date or '不限'}")
        elif mode == "latest":
            self.logger("仅抓取最新一条微博。")

        count_total = 0
        count_hit = 0

        stopped = False
        for status in self.iter_statuses(uid, stop_event=stop_event):
            if stop_event and stop_event.is_set():
                self.logger("  [stop] 收到停止信号，结束任务")
                stopped = True
                break
            # 若列表页信息不完整，则尝试补充详情页 JSON，以提高媒体命中率
            detail = None
            if self._should_fetch_detail(status.raw_json):
                detail = self._get_status_json(status.mblogid)
            if isinstance(detail, dict):
                created2 = self._parse_created_at(detail.get("created_at"))
                status = WeiboStatus(
                    mblogid=status.mblogid,
                    created_at=created2 or status.created_at,
                    raw_json=detail,
                )

            count_total += 1
            created = status.created_at
            created_str = created.strftime("%Y-%m-%d %H:%M") if created else "未知时间"
            self.logger(
                f"------ 微博 {count_total} ------\n"
                f"ID: {status.mblogid}\n"
                f"时间: {created_str}\n"
                f"内容: {self._short_text(status.raw_json.get('text_raw') or status.raw_json.get('text') or '')}"
            )

            # 时间过滤仅对 range 模式生效
            if mode == "range":
                if created is not None:
                    d = created.date()
                    if start_date and d < start_date:
                        self.logger("  [skip] 早于开始日期，跳过")
                        continue
                    if end_date and d > end_date:
                        self.logger("  [skip] 晚于结束日期，跳过")
                        continue

            count_hit += 1
            self._download_media_for_status(uid, status, nickname=nickname, pack=pack)

            # latest 模式：处理完第一条就结束
            if mode == "latest":
                break

        if stop_event and stop_event.is_set():
            stopped = True
        self.logger(
            f"=== 完成: 共遍历微博 {count_total} 条，满足条件的 {count_hit} 条。"
        )
        return {"total": count_total, "hit": count_hit, "stopped": stopped}

    def run_status_urls(
        self,
        urls: List[str],
        pack: bool = True,
        stop_event: Optional[threading.Event] = None,
    ):
        self.logger(f"=== 批量下载：共 {len(urls)} 条链接 ===")
        count_total = 0
        count_hit = 0
        stopped = False

        for raw in urls:
            if stop_event and stop_event.is_set():
                self.logger("  [stop] 收到停止信号，结束任务")
                stopped = True
                break
            target = (raw or "").strip()
            if not target:
                continue
            count_total += 1
            try:
                status_id = self.normalize_status_id(target)
            except Exception as e:
                self.logger(f"[batch] 无法解析链接: {target} ({e})")
                continue
            detail = self._get_status_json(status_id)
            if not isinstance(detail, dict):
                self.logger(f"[batch] 详情获取失败: {status_id}")
                continue

            created = self._parse_created_at(detail.get("created_at"))
            mblogid = (
                detail.get("mblogid")
                or detail.get("mid")
                or detail.get("id")
                or status_id
            )
            status = WeiboStatus(
                mblogid=str(mblogid), created_at=created, raw_json=detail
            )
            user = detail.get("user") or {}
            uid = str(user.get("id") or user.get("idstr") or "").strip()
            if not uid:
                uid = "unknown"
            nickname = (user.get("screen_name") or "").strip() or uid
            created_str = created.strftime("%Y-%m-%d %H:%M") if created else "未知时间"
            self.logger(
                f"------ 链接 {count_total} ------\n"
                f"ID: {status.mblogid}\n"
                f"时间: {created_str}\n"
                f"内容: {self._short_text(detail.get('text_raw') or detail.get('text') or '')}"
            )
            count_hit += 1
            self._download_media_for_status(uid, status, nickname=nickname, pack=pack)

        if stop_event and stop_event.is_set():
            stopped = True
        self.logger(
            f"=== 完成: 共处理链接 {count_total} 条，成功 {count_hit} 条。"
        )
        return {"total": count_total, "hit": count_hit, "stopped": stopped}

    # -------------------- 微博列表 & 详情 -------------------- #

    def iter_statuses(
        self, uid: str, stop_event: Optional[threading.Event] = None
    ) -> Iterable[WeiboStatus]:
        """
        直接使用 weibo.com 的 ajax 接口分页列出微博：
        - https://weibo.com/ajax/statuses/mymblog?uid={uid}&page=1&feature=0
        - 继续按 since_id 翻页
        """
        self.client.set_uid_headers(uid)
        page = 1
        since_id: Optional[str] = None
        seen_ids = set()

        while True:
            if stop_event and stop_event.is_set():
                self.logger("[stop] 收到停止信号，终止列表遍历")
                return
            params = {"uid": uid, "page": page, "feature": 0, "count": 20}
            if since_id:
                params["since_id"] = since_id

            url = "https://weibo.com/ajax/statuses/mymblog"
            j = self.client.get_json(url, params=params)
            if not j or not isinstance(j, dict):
                if page == 1:
                    if self.allow_weibo_cn_fallback:
                        self.logger(
                            "[list] weibo.com 列表接口失败，已启用 WEIBO_CN_FALLBACK，尝试回退到 weibo.cn 列表解析"
                        )
                        for s in self._iter_statuses_weibo_cn(uid):
                            yield s
                        return
                    raise RuntimeError(
                        "weibo.com 列表接口失败：可能 Cookie 失效/被风控/缺少登录态，请更新 weibo.com Cookie 后重试"
                    )
                self.logger("[list] 列表接口返回空，结束")
                break
            meta = self._extract_ok_msg(j)
            ok = meta.get("ok")
            if isinstance(ok, int) and ok != 1:
                if page == 1:
                    raise RuntimeError(self._format_api_error("statuses/mymblog", j))
                self.logger(
                    f"[list] weibo.com 列表接口异常，结束：{self._format_api_error('statuses/mymblog', j)}"
                )
                break

            data = j.get("data") or {}
            statuses = data.get("list") or []
            since_id = data.get("since_id") or None
            if not statuses:
                if page == 1:
                    self.logger(
                        f"[list] page=1 空列表（可能该用户无可见微博或被限制）。ok={meta.get('ok')}"
                    )
                else:
                    self.logger("[list] 未获取到更多微博，结束")
                break

            self.logger(
                f"[list] page={page} since_id={since_id or '-'} 数量={len(statuses)}"
            )

            for s in statuses:
                if stop_event and stop_event.is_set():
                    self.logger("[stop] 收到停止信号，终止列表遍历")
                    return
                mid = s.get("mblogid") or s.get("mid") or s.get("id")
                if not mid or mid in seen_ids:
                    continue
                seen_ids.add(mid)
                created = self._parse_created_at(s.get("created_at"))
                yield WeiboStatus(mblogid=str(mid), created_at=created, raw_json=s)

            # 翻页控制
            page += 1
            if since_id in (None, "", 0):
                break

    def _iter_statuses_weibo_cn(self, uid: str) -> Iterable[WeiboStatus]:
        """
        备用方案：从 weibo.cn 的用户页解析微博 mblogid，再用 weibo.com/ajax/statuses/show 拉详情。
        适用于只有 _T_WM 等移动端 Cookie 的场景。
        """
        base = f"https://weibo.cn/u/{uid}"
        page = 1
        max_page: Optional[int] = None
        seen = set()

        while True:
            url = f"{base}?page={page}"
            self.logger(f"[list] weibo.cn page={page} {url}")
            resp = self.client.get(url)
            doc = parse_html_safely(resp.content)

            if max_page is None:
                max_page = self._parse_weibo_cn_max_page(doc) or 1
                self.logger(f"[list] weibo.cn 估算总页数: {max_page}")

            ids = self._extract_weibo_cn_mblogids(doc)
            if not ids:
                if page == 1:
                    self.logger("[list] weibo.cn 第一页没有找到任何微博，可能 UID 不存在或 Cookie 有问题")
                break

            for mblogid in ids:
                if mblogid in seen:
                    continue
                seen.add(mblogid)
                detail = self._get_status_json(mblogid)
                if not isinstance(detail, dict):
                    self.logger(f"[status] 详情获取失败: {mblogid}")
                    continue
                created = self._parse_created_at(detail.get("created_at"))
                yield WeiboStatus(mblogid=str(mblogid), created_at=created, raw_json=detail)

            page += 1
            if max_page and page > max_page:
                break

    @staticmethod
    def _parse_weibo_cn_max_page(doc) -> Optional[int]:
        # 常见：<input name="mp" value="75" />
        mp = doc.xpath("//input[@name='mp']/@value")
        if mp:
            try:
                return int(mp[0])
            except Exception:
                return None

        text = "".join(doc.xpath("//div[@id='pagelist']//text()")).strip()
        # 形如：1/75页
        m = re.search(r"/\\s*(\\d+)\\s*页", text)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                return None
        return None

    @staticmethod
    def _extract_weibo_cn_mblogids(doc) -> List[str]:
        hrefs = doc.xpath("//a[contains(@href, '/comment/')]/@href")
        ids: List[str] = []
        for href in hrefs:
            m = re.search(r"/comment/([0-9A-Za-z]+)", href)
            if m:
                ids.append(m.group(1))
        # 保持顺序去重
        return list(dict.fromkeys(ids))

    def _get_status_json(self, mblogid: str) -> Optional[Dict[str, Any]]:
        base = "https://weibo.com/ajax/statuses/show"
        # 依次尝试不同参数名
        for key in ("id", "mid", "mblogid"):
            params = {key: mblogid}
            j = self.client.get_json(base, params=params)
            if isinstance(j, dict) and (j.get("id") or j.get("mid") or j.get("mblogid")):
                return j
        self.logger(f"[status] 无法通过 ajax/statuses/show 拿到微博 {mblogid} 详情")
        return None

    @staticmethod
    def _parse_created_at(s: Optional[str]) -> Optional[datetime]:
        if not s:
            return None
        # 典型格式: "Sun Nov 03 19:30:00 +0800 2025"
        for fmt in ("%a %b %d %H:%M:%S %z %Y", "%a %b %d %H:%M:%S %Y"):
            try:
                return datetime.strptime(s, fmt)
            except Exception:
                continue
        return None

    # -------------------- 媒体提取 & 下载 -------------------- #

    def _download_media_for_status(
        self,
        uid: str,
        status: WeiboStatus,
        nickname: Optional[str] = None,
        pack: bool = True,
    ):
        created = status.created_at or datetime.now()
        prefix = f"{created.strftime('%Y%m%d_%H%M%S')}_{status.mblogid}"

        media = self._extract_media_from_json(status.raw_json)
        images = media["images"]
        livephotos = media["livephotos"]
        videos = media["videos"]

        user_dir = self.download_root / self._format_user_dir(uid, nickname)
        img_dir = user_dir / "images"
        video_dir = user_dir / "videos"
        live_dir = video_dir / "livephoto"
        used_names: set[str] = set()
        base_name = ""
        if not pack:
            img_dir = user_dir
            video_dir = user_dir
            live_dir = user_dir
            base_name = self._post_basename(status, limit=10)

        index = 1
        new_files: List[Path] = []

        # 图片（原图）
        if images:
            self.logger(f"  [img] 图片数: {len(images)}")
        for url in images:
            ext = self._guess_ext(url, default=".jpg")
            if pack:
                save_name = f"{prefix}({index}){ext}"
                index += 1
                save_path = img_dir / save_name
            else:
                name = self._reserve_unique_basename(
                    img_dir, base_name, (ext,), used_names
                )
                save_path = img_dir / f"{name}{ext}"
            if save_path.exists():
                self.logger(f"  [skip exist] {save_path.name}")
                continue
            try:
                self.client.download_binary(url, save_path)
                new_files.append(save_path)
            except Exception as e:
                self.logger(f"  [img] 下载失败: {e}")

        # Live Photo（mov -> mp4）
        if livephotos:
            self.logger(f"  [live] LivePhoto 数: {len(livephotos)}")
        for url in livephotos:
            if pack:
                mov_name = f"{prefix}_live_{self._safe_id_from_url(url)}.mov"
            else:
                ext_group = (".mov", ".mp4") if self.has_ffmpeg else (".mov",)
                base = self._reserve_unique_basename(
                    live_dir, base_name, ext_group, used_names
                )
                mov_name = f"{base}.mov"
            mov_path = live_dir / mov_name
            mov_path.parent.mkdir(parents=True, exist_ok=True)
            if mov_path.exists():
                self.logger(f"  [skip exist] {mov_path.name}")
            else:
                try:
                    self.client.download_binary(url, mov_path)
                    self.logger(f"  [live] 已下载 LivePhoto: {mov_path.name}")
                except Exception as e:
                    self.logger(f"  [live] 下载失败: {e}")
                    continue
            # 转 mp4
            if self.has_ffmpeg:
                if pack:
                    mp4_name = mov_name.replace(".mov", ".mp4")
                    mp4_path = live_dir / mp4_name
                else:
                    mp4_path = mov_path.with_suffix(".mp4")
                if mp4_path.exists():
                    self.logger(f"  [skip exist] {mp4_path.name}")
                else:
                    self._convert_mov_to_mp4(mov_path, mp4_path)
                    if mp4_path.exists():
                        new_files.append(mp4_path)
            else:
                self.logger("  [live] 未检测到 ffmpeg，保留 mov 文件")

        # 普通视频
        if videos:
            self.logger(f"  [video] 视频数: {len(videos)}")
        for url in videos:
            ext = self._guess_ext(url, default=".mp4")
            if pack:
                save_name = f"{prefix}_video_{self._safe_id_from_url(url)}{ext}"
                save_path = video_dir / save_name
            else:
                name = self._reserve_unique_basename(
                    video_dir, base_name, (ext,), used_names
                )
                save_path = video_dir / f"{name}{ext}"
            if save_path.exists():
                self.logger(f"  [skip exist] {save_path.name}")
                continue
            try:
                self.client.download_binary(url, save_path)
                new_files.append(save_path)
            except Exception as e:
                self.logger(f"  [video] 下载失败: {e}")

        if new_files and self.notifier and self.notifier.enabled:
            names = ", ".join(p.name for p in new_files[:5])
            more = "" if len(new_files) <= 5 else f" 等 {len(new_files)} 个文件"
            msg = f"UID {uid} 有新的媒体下载: {names}{more}"
            self.notifier.send(msg)

    def _convert_mov_to_mp4(self, mov_path: Path, mp4_path: Path):
        try:
            self.logger(f"  [ffmpeg] 转换 LivePhoto -> MP4: {mov_path.name}")
            cmd = [
                "ffmpeg",
                "-y",
                "-i",
                str(mov_path),
                "-c",
                "copy",
                str(mp4_path),
            ]
            subprocess.run(
                cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            self.logger(f"  [ffmpeg] 转换完成: {mp4_path.name}")
            mov_path.unlink(missing_ok=True)
        except Exception as e:
            self.logger(f"  [ffmpeg] 转换失败: {e}, 保留 mov 文件")

    def _extract_media_from_json(self, j: Dict[str, Any]) -> Dict[str, List[str]]:
        """
        统一从 ajax JSON 中抽取：
        - images: 原图 URL 列表
        - livephotos: LivePhoto 视频 URL 列表
        - videos: 普通视频 MP4 URL 列表
        """
        result = {"images": [], "livephotos": [], "videos": []}

        def _extract_video_url(info: Dict[str, Any]) -> Optional[str]:
            v = info.get("video")
            if isinstance(v, str):
                return v
            if isinstance(v, dict):
                return v.get("url")
            return None

        def handle_status(s: Dict[str, Any]):
            # 混合媒体
            mix = s.get("mix_media_info") or {}
            items = mix.get("items") or []
            for item in items:
                mtype = item.get("type")
                data = item.get("data") or {}
                if mtype == "pic":
                    pic_url = self._pick_best_pic_url(data)
                    if pic_url:
                        result["images"].append(pic_url)
                elif mtype == "video":
                    v_url = self._pick_best_video_url(data.get("media_info") or {})
                    if v_url:
                        result["videos"].append(v_url)

            # 纯图片 / LivePhoto
            pic_ids = s.get("pic_ids") or []
            pic_infos = s.get("pic_infos") or {}
            for pid in pic_ids:
                info = pic_infos.get(pid) or {}
                ptype = info.get("type")
                if ptype == "pic":
                    pic_url = self._pick_best_pic_url(info)
                    if pic_url:
                        result["images"].append(pic_url)
                elif ptype in ("livephoto", "gif"):
                    vurl = _extract_video_url(info)
                    if vurl:
                        result["livephotos"].append(vurl)
                    elif ptype == "gif":
                        pic_url = self._pick_best_pic_url(info)
                        if pic_url:
                            result["images"].append(pic_url)

            # pics 列表兜底（部分接口返回 pics 而非 pic_infos）
            pics = s.get("pics") or []
            if isinstance(pics, list):
                for info in pics:
                    if not isinstance(info, dict):
                        continue
                    ptype = info.get("type")
                    if ptype in ("livephoto", "gif"):
                        vurl = _extract_video_url(info)
                        if vurl:
                            result["livephotos"].append(vurl)
                            continue
                        if ptype == "gif":
                            pic_url = self._pick_best_pic_url(info)
                            if pic_url:
                                result["images"].append(pic_url)
                            continue
                    pic_url = self._pick_best_pic_url(info)
                    if pic_url:
                        result["images"].append(pic_url)

            # 单视频 page_info
            page_info = s.get("page_info") or {}
            media_info = page_info.get("media_info") or {}
            v_url = self._pick_best_video_url(media_info)
            if v_url:
                result["videos"].append(v_url)

        # 当前微博
        handle_status(j)
        # 转发微博也顺手抓一下
        if j.get("retweeted_status"):
            handle_status(j["retweeted_status"])

        # 去重
        result["images"] = list(dict.fromkeys(result["images"]))
        result["livephotos"] = list(dict.fromkeys(result["livephotos"]))
        result["videos"] = list(dict.fromkeys(result["videos"]))

        return result

    @staticmethod
    def _should_fetch_detail(status_json: Dict[str, Any]) -> bool:
        # 常见：列表页只有 pic_ids，没有 pic_infos；或 page_info 不含 media_info
        pic_ids = status_json.get("pic_ids") or []
        pic_infos = status_json.get("pic_infos") or {}
        if pic_ids and not pic_infos:
            return True
        page_info = status_json.get("page_info") or {}
        if page_info and not (page_info.get("media_info") or {}):
            return True
        mix = status_json.get("mix_media_info") or {}
        if mix and not (mix.get("items") or []):
            return True
        return False

    @staticmethod
    def _is_wm_url(url: Optional[str]) -> bool:
        if not url:
            return False
        return "wm" in url.lower()

    @staticmethod
    def _video_label_score(label: Optional[str]) -> int:
        if not label:
            return 0
        val = label.lower()
        if "4k" in val:
            return 4000
        if "2k" in val:
            return 2000
        m = re.search(r"(\\d{3,4})", val)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                return 0
        if "hd" in val:
            return 720
        if "sd" in val:
            return 480
        return 0

    def _pick_best_pic_url(self, info: Dict[str, Any]) -> Optional[str]:
        # 优先 original / large，其次 largest / bmiddle 中的 url（原图 / 大图）
        wm_candidates: List[str] = []
        for key in ("original", "large", "largest", "bmiddle"):
            obj = info.get(key)
            if isinstance(obj, dict) and obj.get("url"):
                url = obj["url"]
                if self.avoid_wm_url and self._is_wm_url(url):
                    wm_candidates.append(url)
                    continue
                return url
        if info.get("original_pic"):
            url = info["original_pic"]
            if self.avoid_wm_url and self._is_wm_url(url):
                wm_candidates.append(url)
            else:
                return url
        if wm_candidates:
            return wm_candidates[0]
        return None

    def _pick_best_video_url(self, media_info: Dict[str, Any]) -> Optional[str]:
        # 新格式 playback_list 优先
        pl = media_info.get("playback_list") or []
        if isinstance(pl, dict):
            pl_items = list(pl.values())
        elif isinstance(pl, list):
            pl_items = pl
        else:
            pl_items = []
        candidates: List[Dict[str, Any]] = []
        order = 0
        for item in pl_items:
            if not isinstance(item, dict):
                continue
            p = item.get("play_info") or {}
            if not isinstance(p, dict):
                p = {}
            url = p.get("url") or p.get("url_https")
            label = p.get("label") or item.get("label") or ""
            if url:
                candidates.append({"url": url, "label": label, "order": order})
                order += 1
        # 旧字段兜底
        for key in ("mp4_720p_mp4", "stream_url_hd", "stream_url", "mp4_hd_url", "mp4_sd_url"):
            url = media_info.get(key)
            if url:
                candidates.append({"url": url, "label": key, "order": order})
                order += 1
        if not candidates:
            return None
        if not self.avoid_wm_url:
            return candidates[0]["url"]
        non_wm = [c for c in candidates if not self._is_wm_url(c["url"])]
        if non_wm:
            candidates = non_wm
        best = max(
            candidates,
            key=lambda c: (self._video_label_score(c["label"]), -c["order"]),
        )
        return best["url"]

    @staticmethod
    def _guess_ext(url: str, default: str = ".bin") -> str:
        m = re.search(r"\.(mp4|mov|jpg|jpeg|png|gif|webp)(?:\?|$)", url, re.I)
        if m:
            return "." + m.group(1).lower()
        return default

    @staticmethod
    def _safe_id_from_url(url: str) -> str:
        tail = url.split("/")[-1]
        tail = tail.split("?")[0]
        return re.sub(r"[^0-9A-Za-z]+", "_", tail)[:40]

    def _reserve_unique_basename(
        self,
        dest_dir: Path,
        base: str,
        exts: Iterable[str],
        used: Optional[set[str]] = None,
    ) -> str:
        used_names = used if used is not None else set()
        safe_base = self._safe_name(base) or "weibo"
        index = 1
        while True:
            name = safe_base if index == 1 else f"{safe_base}_{index}"
            conflict = False
            for ext in exts:
                filename = f"{name}{ext}"
                if filename in used_names or (dest_dir / filename).exists():
                    conflict = True
                    break
            if not conflict:
                for ext in exts:
                    used_names.add(f"{name}{ext}")
                return name
            index += 1

    @staticmethod
    def _safe_name(name: Optional[str]) -> str:
        text = (name or "").strip()
        if not text:
            return ""
        return re.sub(r"[\\\\/:*?\"<>|\\r\\n\\t]+", "_", text).strip()

    def _post_basename(self, status: WeiboStatus, limit: int = 10) -> str:
        text = status.raw_json.get("text_raw") or status.raw_json.get("text") or ""
        text = re.sub(r"<[^>]+>", "", text)
        text = text.replace("\n", " ").strip()
        if not text:
            return status.mblogid
        short = text[:limit].strip()
        safe = self._safe_name(short)
        return safe or status.mblogid

    def _format_user_dir(self, uid: str, nickname: Optional[str]) -> str:
        safe_nickname = self._safe_name(nickname)
        if not safe_nickname:
            safe_nickname = uid
        return f"{safe_nickname}({uid})"

    @staticmethod
    def _short_text(text: str, limit: int = 60) -> str:
        t = text.replace("\n", " ").strip()
        return t[:limit] + ("..." if len(t) > limit else "")
