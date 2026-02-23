# app.py
import asyncio
import os
import re
from datetime import datetime, date
import json
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Any, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from weibo_crawler import WeiboCrawler, default_logger
from telegram_bot import TelegramNotifier

APP_ROOT = Path(__file__).parent.resolve()
PROJECT_ROOT = APP_ROOT.parent
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

COOKIE_FILE = DATA_DIR / "cookie.txt"
HISTORY_FILE = DATA_DIR / "history.json"
LOG_FILE = DATA_DIR / "weibo.log"
DOWNLOAD_ROOT = PROJECT_ROOT / "weibo_media"
DOWNLOAD_ROOT.mkdir(parents=True, exist_ok=True)

app = FastAPI()
app.mount("/static", StaticFiles(directory=str(APP_ROOT / "static")), name="static")
SPA_INDEX_FILE = APP_ROOT / "static" / "spa" / "index.html"

# 监控任务：uid -> {thread, stop_event, interval}
monitor_tasks: Dict[str, Dict[str, Any]] = {}
# 一次性任务状态（仅用于停止功能）
current_run: Dict[str, Any] = {
    "running": False,
    "stop_event": None,
    "uid": "",
    "mode": "",
    "started_at": "",
}
current_run_lock = threading.Lock()

# Telegram 通知器（可选）
notifier = TelegramNotifier.from_env(logger=default_logger)
if notifier.enabled:
    notifier.send("恭喜发财!")


def append_log(msg: str):
    ts = datetime.now().strftime("[%H:%M:%S]")
    line = f"{ts} {msg}"
    print(line)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_cookie_text() -> str:
    if COOKIE_FILE.exists():
        return COOKIE_FILE.read_text(encoding="utf-8").strip()
    return ""


def save_cookie(cookie_text: str):
    text = (cookie_text or "").strip()
    if WeiboCrawler.is_netscape_cookie_text(text):
        COOKIE_FILE.write_text(text, encoding="utf-8")
    else:
        COOKIE_FILE.write_text(
            WeiboCrawler.normalize_cookie_str(text), encoding="utf-8"
        )


def load_cookie_for_crawler() -> Dict[str, Any]:
    text = load_cookie_text()
    if not text:
        return {"cookie_str": "", "cookie_jar": None}
    if WeiboCrawler.is_netscape_cookie_text(text):
        jar = WeiboCrawler.load_cookie_jar(COOKIE_FILE, logger=append_log)
        cookie_str = WeiboCrawler.cookie_str_from_jar(jar) if jar else ""
        return {"cookie_str": cookie_str, "cookie_jar": jar}
    return {"cookie_str": WeiboCrawler.normalize_cookie_str(text), "cookie_jar": None}


def parse_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def load_history() -> List[Dict[str, Any]]:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def save_history(history: List[Dict[str, Any]]):
    HISTORY_FILE.write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def add_history(uid: str, nickname: str):
    history = load_history()
    # 去重，最近的在前
    history = [h for h in history if h.get("uid") != uid]
    history.insert(
        0,
        {
            "uid": uid,
            "nickname": nickname,
            "last_time": datetime.now().isoformat(timespec="seconds"),
        },
    )
    save_history(history)


def get_last_log_lines(limit: int = 200) -> str:
    if not LOG_FILE.exists():
        return ""
    size = LOG_FILE.stat().st_size
    if size <= 0:
        return ""
    # 避免整文件读入（weibo.log 可能非常大）
    max_bytes = 256 * 1024
    start = max(0, size - max_bytes)
    with LOG_FILE.open("rb") as f:
        f.seek(start)
        data = f.read()
    text = data.decode("utf-8", errors="ignore")
    lines = text.splitlines()
    return "\n".join(lines[-limit:])


def read_logs_since(cursor: int = 0) -> Dict[str, Any]:
    if not LOG_FILE.exists():
        return {"lines": [], "cursor": 0}

    try:
        cursor = int(cursor)
    except Exception:
        cursor = 0
    cursor = max(0, cursor)

    size = LOG_FILE.stat().st_size
    if size <= 0:
        return {"lines": [], "cursor": 0}

    # cursor 采用“字节偏移”，避免每次轮询整文件 splitlines。
    # cursor=0 视为首次进入：只返回末尾若干行，并把 cursor 移到文件末尾。
    if cursor == 0:
        tail_text = get_last_log_lines(limit=200)
        return {"lines": tail_text.splitlines(), "cursor": size}

    if cursor >= size:
        return {"lines": [], "cursor": size}

    max_bytes = 256 * 1024
    with LOG_FILE.open("rb") as f:
        f.seek(cursor)
        data = f.read(max_bytes)
        new_cursor = f.tell()

    # 尽量切到换行边界，避免下一次从半行开始导致乱码/断行
    if new_cursor < size:
        last_nl = data.rfind(b"\n")
        if last_nl != -1:
            new_cursor = cursor + last_nl + 1
            data = data[: last_nl + 1]

    text = data.decode("utf-8", errors="ignore")
    return {"lines": text.splitlines(), "cursor": new_cursor}


def parse_uid_list(raw: str) -> List[str]:
    """
    支持逗号、空格、换行分隔的多 UID/链接。
    """
    results: List[str] = []
    for chunk in re.split(r"[,\s]+", raw.strip()):
        if not chunk:
            continue
        try:
            uid_norm = WeiboCrawler.normalize_uid(chunk)
            if uid_norm not in results:
                results.append(uid_norm)
        except Exception as e:
            append_log(f"[错误] 无法解析 UID {chunk!r}: {e}")
    return results


def parse_status_url_list(raw: str) -> List[str]:
    """
    批量下载：一行一个微博链接。
    """
    results: List[str] = []
    for line in (raw or "").splitlines():
        url = line.strip()
        if not url:
            continue
        try:
            WeiboCrawler.normalize_status_id(url)
            results.append(url)
        except Exception as e:
            append_log(f"[错误] 无法解析微博链接 {url!r}: {e}")
    return results


def reset_current_run_state():
    with current_run_lock:
        current_run.update(
            {
                "running": False,
                "stop_event": None,
                "uid": "",
                "mode": "",
                "started_at": "",
            }
        )


def classify_runtime_error_status(err: str) -> int:
    err_lower = err.lower()
    if (
        "cookie" in err_lower
        or "登录" in err
        or "风控" in err
        or "列表接口失败" in err
        or "login.php" in err_lower
        or "ok=-100" in err_lower
    ):
        return 400
    return 500


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if SPA_INDEX_FILE.exists():
        return FileResponse(str(SPA_INDEX_FILE))
    return PlainTextResponse("SPA 构建产物不存在，请先执行前端构建。", status_code=500)


@app.get("/cookie/status")
async def api_cookie_status():
    cookie_text = load_cookie_text()
    return JSONResponse(
        {"has_cookie": bool(cookie_text), "length": len(cookie_text)}
    )


@app.post("/run")
async def run_task(request: Request):
    # 统一解析 body（优先 JSON，其次表单）
    uid_or_url = ""
    mode = "all"
    start_date = ""
    end_date = ""
    interval_minutes: Any = 5
    cookie = ""
    pack: Any = True

    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        body = await request.json()
        uid_or_url = body.get("uid_or_url") or ""
        mode = body.get("mode") or "all"
        start_date = body.get("start_date") or ""
        end_date = body.get("end_date") or ""
        interval_minutes = body.get("interval_minutes") or 5
        cookie = body.get("cookie") or ""
        pack = body.get("pack")
    else:
        form = await request.form()
        uid_or_url = form.get("uid_or_url") or ""
        mode = form.get("mode") or "all"
        start_date = form.get("start_date") or ""
        end_date = form.get("end_date") or ""
        interval_minutes = form.get("interval_minutes") or 5
        cookie = form.get("cookie") or ""
        pack = form.get("pack")

    try:
        interval_minutes = int(interval_minutes)
    except Exception:
        interval_minutes = 5
    pack = parse_bool(pack, default=True)
    uid_list: List[str] = []
    url_list: List[str] = []
    if mode == "batch":
        url_list = parse_status_url_list(uid_or_url)
        if not url_list:
            append_log("[错误] 未找到有效的微博链接，请检查输入")
            return JSONResponse(
                {"message": "未找到有效的微博链接"}, status_code=400
            )
    else:
        uid_list = parse_uid_list(uid_or_url)
        if not uid_list:
            append_log("[错误] 未找到有效的 UID，请检查输入")
            return JSONResponse({"message": "未找到有效的 UID"}, status_code=400)
        if len(uid_list) > 1 and mode != "monitor":
            append_log(
                "[提示] 当前仅取第一位 UID 运行一次性任务，多 UID 适用于监控模式"
            )

    # 保存 / 使用 Cookie
    if cookie.strip():
        save_cookie(cookie)
    cookie_payload = load_cookie_for_crawler()
    cookie_value = cookie_payload.get("cookie_str") or ""
    cookie_jar = cookie_payload.get("cookie_jar")
    if not cookie_value and not cookie_jar:
        append_log("[错误] Cookie 为空，请先在页面底部粘贴 weibo.com 的 Cookie")
        return JSONResponse({"message": "Cookie 为空，请先粘贴 weibo.com 的 Cookie"}, status_code=400)

    # ---------- 监控模式：后台循环调用 latest ----------
    if mode == "monitor":
        started: List[str] = []
        for uid_norm in uid_list:
            # 停掉旧任务
            if uid_norm in monitor_tasks:
                append_log(f"[监控] 已存在 UID {uid_norm} 的监控任务，先停止旧任务")
                monitor_tasks[uid_norm]["stop_event"].set()
                monitor_tasks.pop(uid_norm, None)

            stop_event = threading.Event()

            def monitor_loop(target_uid: str, stop_evt: threading.Event):
                try:
                    crawler = WeiboCrawler(
                        cookie_str=cookie_value,
                        download_root=DOWNLOAD_ROOT,
                        logger=append_log,
                        notifier=notifier,
                        cookie_jar=cookie_jar,
                    )
                except Exception as e:
                    append_log(f"[监控错误] UID {target_uid} 初始化失败: {e}")
                    if notifier.enabled:
                        notifier.send(f"UID {target_uid} 监控初始化失败: {e}")
                    return
                append_log(
                    f"[监控] 启动 UID {target_uid} 的监控，间隔 {interval_minutes} 分钟"
                )
                nickname = target_uid
                try:
                    profile = crawler.fetch_user_profile(target_uid)
                    nickname = profile.get("screen_name") or target_uid
                except Exception as e:
                    append_log(f"[监控提示] 获取用户资料失败，继续使用 UID: {e}")
                if target_uid in monitor_tasks:
                    monitor_tasks[target_uid]["nickname"] = nickname
                while not stop_evt.is_set():
                    try:
                        append_log(f"[监控] 正在扫描用户 {target_uid} ...")
                        crawler.run(
                            uid=target_uid,
                            mode="latest",
                            nickname=nickname,
                            pack=pack,
                        )
                        append_log(f"[监控] 本轮扫描完成 UID {target_uid}")
                        if target_uid in monitor_tasks:
                            monitor_tasks[target_uid][
                                "last_run"
                            ] = datetime.now().isoformat(timespec="seconds")
                    except Exception as e:
                        append_log(f"[监控错误] UID {target_uid}: {e}")
                    # 可提前停止的 sleep
                    total_sleep = max(1, int(interval_minutes) * 60)
                    for _ in range(total_sleep):
                        if stop_evt.is_set():
                            break
                        time.sleep(1)
                append_log(f"[监控] UID {target_uid} 的监控任务已结束")

            t = threading.Thread(
                target=monitor_loop, args=(uid_norm, stop_event), daemon=True
            )
            t.start()
            monitor_tasks[uid_norm] = {
                "thread": t,
                "stop_event": stop_event,
                "interval": interval_minutes,
                "nickname": "",
                "last_run": "",
            }
            started.append(uid_norm)

        if notifier.enabled and started:
            notifier.send(f"恭喜发财! 已开始监控: {', '.join(started)}")
        return JSONResponse({"message": f"监控任务已启动: {', '.join(started)}"})

    # ---------- 一次性任务：all / range / latest ----------
    start_dt: Optional[date] = None
    end_dt: Optional[date] = None

    if mode == "range":
        if start_date.strip():
            try:
                start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
            except Exception:
                append_log(f"[警告] 无法解析开始日期: {start_date}")
        if end_date.strip():
            try:
                end_dt = datetime.strptime(end_date, "%Y-%m-%d").date()
            except Exception:
                append_log(f"[警告] 无法解析结束日期: {end_date}")

    primary_uid = uid_list[0] if uid_list else ""
    run_target = primary_uid or (url_list[0] if url_list else "")
    stop_event = threading.Event()
    with current_run_lock:
        if current_run.get("running"):
            append_log("[提示] 已有一次性任务运行中，请先停止")
            return JSONResponse(
                {"message": "已有任务运行中，请先停止"}, status_code=409
            )
        current_run.update(
            {
                "running": True,
                "stop_event": stop_event,
                "uid": run_target,
                "mode": mode,
                "started_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
    try:
        crawler = WeiboCrawler(
            cookie_str=cookie_value,
            download_root=DOWNLOAD_ROOT,
            logger=append_log,
            notifier=notifier,
            cookie_jar=cookie_jar,
        )
    except Exception as e:
        err = str(e)
        append_log(f"[错误] 初始化失败: {err}")
        reset_current_run_state()
        return JSONResponse({"message": err}, status_code=400)

    try:
        if mode == "batch":
            stats = await asyncio.to_thread(
                crawler.run_status_urls,
                urls=url_list,
                pack=pack,
                stop_event=stop_event,
            )
        else:
            nickname = primary_uid
            try:
                profile = await asyncio.to_thread(crawler.fetch_user_profile, primary_uid)
                nickname = profile.get("screen_name") or primary_uid
            except Exception as e:
                append_log(f"[提示] 获取用户资料失败，继续运行一次性任务: {e}")
            stats = await asyncio.to_thread(
                crawler.run,
                uid=primary_uid,
                mode=mode,
                start_date=start_dt,
                end_date=end_dt,
                nickname=nickname,
                pack=pack,
                stop_event=stop_event,
            )
            add_history(primary_uid, nickname)
        if isinstance(stats, dict):
            if stats.get("stopped"):
                return JSONResponse(
                    {
                        "message": f"任务已停止：遍历 {stats.get('total', 0)} 条，命中 {stats.get('hit', 0)} 条"
                    }
                )
            return JSONResponse(
                {
                    "message": f"任务已完成：遍历 {stats.get('total', 0)} 条，命中 {stats.get('hit', 0)} 条"
                }
            )
        return JSONResponse({"message": "任务已完成"})
    except Exception as e:
        err = str(e)
        append_log(f"[错误] 运行过程中发生异常: {err}")
        if notifier.enabled and ("cookie" in err.lower() or "登录" in err):
            notifier.send("Cookie 可能已过期，请更新。")
        status = classify_runtime_error_status(err)
        return JSONResponse({"message": err}, status_code=status)
    finally:
        reset_current_run_state()


@app.post("/download/single")
async def download_single(request: Request):
    # 统一解析 body（优先 JSON，其次表单）
    status_url = ""
    cookie = ""
    pack: Any = True

    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        body = await request.json()
        status_url = (
            body.get("url")
            or body.get("status_url")
            or body.get("link")
            or body.get("uid_or_url")
            or ""
        )
        cookie = body.get("cookie") or ""
        pack = body.get("pack")
    else:
        form = await request.form()
        status_url = (
            form.get("url")
            or form.get("status_url")
            or form.get("link")
            or form.get("uid_or_url")
            or ""
        )
        cookie = form.get("cookie") or ""
        pack = form.get("pack")

    status_url = (status_url or "").strip()
    pack = parse_bool(pack, default=True)
    if not status_url:
        append_log("[错误] 单链接下载缺少微博链接")
        return JSONResponse({"message": "请提供微博链接"}, status_code=400)
    try:
        WeiboCrawler.normalize_status_id(status_url)
    except Exception as e:
        append_log(f"[错误] 单链接下载无法解析微博链接 {status_url!r}: {e}")
        return JSONResponse({"message": f"微博链接无效: {e}"}, status_code=400)

    # 保存 / 使用 Cookie
    if cookie.strip():
        save_cookie(cookie)
    cookie_payload = load_cookie_for_crawler()
    cookie_value = cookie_payload.get("cookie_str") or ""
    cookie_jar = cookie_payload.get("cookie_jar")
    if not cookie_value and not cookie_jar:
        append_log("[错误] Cookie 为空，请先在页面底部粘贴 weibo.com 的 Cookie")
        return JSONResponse({"message": "Cookie 为空，请先粘贴 weibo.com 的 Cookie"}, status_code=400)

    stop_event = threading.Event()
    with current_run_lock:
        if current_run.get("running"):
            append_log("[提示] 已有一次性任务运行中，请先停止")
            return JSONResponse(
                {"message": "已有任务运行中，请先停止"}, status_code=409
            )
        current_run.update(
            {
                "running": True,
                "stop_event": stop_event,
                "uid": status_url,
                "mode": "single-link",
                "started_at": datetime.now().isoformat(timespec="seconds"),
            }
        )

    try:
        crawler = WeiboCrawler(
            cookie_str=cookie_value,
            download_root=DOWNLOAD_ROOT,
            logger=append_log,
            notifier=notifier,
            cookie_jar=cookie_jar,
        )
    except Exception as e:
        err = str(e)
        append_log(f"[错误] 初始化失败: {err}")
        reset_current_run_state()
        return JSONResponse({"message": err}, status_code=400)

    try:
        stats = await asyncio.to_thread(
            crawler.run_status_urls,
            urls=[status_url],
            pack=pack,
            stop_event=stop_event,
        )
        if isinstance(stats, dict):
            if stats.get("stopped"):
                return JSONResponse(
                    {
                        "message": f"任务已停止：处理 {stats.get('total', 0)} 条，成功 {stats.get('hit', 0)} 条"
                    }
                )
            return JSONResponse(
                {
                    "message": f"任务已完成：处理 {stats.get('total', 0)} 条，成功 {stats.get('hit', 0)} 条"
                }
            )
        return JSONResponse({"message": "任务已完成"})
    except Exception as e:
        err = str(e)
        append_log(f"[错误] 单链接下载发生异常: {err}")
        if notifier.enabled and ("cookie" in err.lower() or "登录" in err):
            notifier.send("Cookie 可能已过期，请更新。")
        status = classify_runtime_error_status(err)
        return JSONResponse({"message": err}, status_code=status)
    finally:
        reset_current_run_state()


@app.post("/stop-monitor")
async def stop_monitor(request: Request):
    uid = ""
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        body = await request.json()
        uid = body.get("uid") or ""
    else:
        form = await request.form()
        uid = form.get("uid") or ""
    uid = uid.strip()
    info = monitor_tasks.get(uid)
    if info:
        info["stop_event"].set()
        monitor_tasks.pop(uid, None)
        append_log(f"[监控] 手动停止 UID {uid} 的监控任务")
        return JSONResponse({"message": f"已停止 {uid}"})
    return JSONResponse({"message": "未找到对应的监控任务"}, status_code=404)


@app.post("/stop-run")
async def stop_run():
    with current_run_lock:
        stop_event = current_run.get("stop_event")
        if not current_run.get("running") or not stop_event:
            return JSONResponse({"message": "当前没有运行中的任务"}, status_code=404)
        stop_event.set()
        target = current_run.get("uid") or ""
        append_log(f"[停止] 已请求停止一次性任务 目标 {target}")
    return JSONResponse({"message": "停止信号已发送"})


@app.get("/logs")
async def api_logs(since: int = 0):
    return JSONResponse(read_logs_since(since))


@app.get("/logs/cursor")
async def api_logs_cursor():
    size = LOG_FILE.stat().st_size if LOG_FILE.exists() else 0
    return JSONResponse({"cursor": size})


@app.get("/history")
async def api_history():
    return JSONResponse(load_history())


@app.get("/monitor/tasks")
async def api_monitor_tasks():
    tasks = []
    for uid, info in monitor_tasks.items():
        tasks.append(
            {
                "uid": uid,
                "interval": info.get("interval", 0),
                "last_run": info.get("last_run", ""),
                "nickname": info.get("nickname", ""),
            }
        )
    return JSONResponse(tasks)
