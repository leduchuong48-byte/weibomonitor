import React, { useEffect, useMemo, useRef, useState } from "react";

function nowTime() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function normalizeNumber(value, fallback) {
  const n = Number.parseInt(String(value), 10);
  return Number.isFinite(n) ? n : fallback;
}

async function fetchJson(url, options) {
  const res = await fetch(url, options);
  const ct = res.headers.get("content-type") || "";
  let data = null;
  if (ct.includes("application/json")) {
    data = await res.json().catch(() => null);
  } else {
    data = await res.text().catch(() => "");
  }
  return { res, data };
}

export default function App() {
  const [uidOrUrl, setUidOrUrl] = useState("");
  const [cookie, setCookie] = useState("");
  const [mode, setMode] = useState("all");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [intervalMinutes, setIntervalMinutes] = useState(5);
  const [hasSavedCookie, setHasSavedCookie] = useState(null);
  const [packEnabled, setPackEnabled] = useState(true);
  const [isStopping, setIsStopping] = useState(false);

  const [history, setHistory] = useState([]);
  const [monitorTasks, setMonitorTasks] = useState([]);

  const [isSubmitting, setIsSubmitting] = useState(false);

  const [logLines, setLogLines] = useState(["等待任务开始…"]);
  const logCursorRef = useRef(0);
  const [logCursor, setLogCursor] = useState(0);
  const logBoxRef = useRef(null);
  const [autoScroll, setAutoScroll] = useState(true);

  const modeTips = useMemo(
    () => [
      { key: "all", text: "全部媒体：遍历该账号所有微博，下载图片/视频/LivePhoto（耗时最长）。" },
      { key: "range", text: "按时间段：只下载指定日期范围内发布的媒体内容。" },
      { key: "latest", text: "最新一条：仅抓取最新一条微博里的媒体，用于快速备份。" },
      { key: "batch", text: "批量下载：一行一个微博链接，仅下载链接内的媒体内容。" },
      { key: "monitor", text: "监控模式：后台定时轮询最新一条微博，有新内容自动下载。" },
    ],
    []
  );

  function uiLog(line) {
    setLogLines((prev) => {
      const next = prev[0] === "等待任务开始…" ? [] : prev.slice();
      next.push(`[${nowTime()}] [ui] ${line}`);
      return next.slice(-2000);
    });
  }

  async function loadHistory() {
    const { res, data } = await fetchJson("/history");
    if (!res.ok) return;
    setHistory(Array.isArray(data) ? data : []);
  }

  async function loadMonitorTasks() {
    const { res, data } = await fetchJson("/monitor/tasks");
    if (!res.ok) return;
    setMonitorTasks(Array.isArray(data) ? data : []);
  }

  async function loadCookieStatus() {
    const { res, data } = await fetchJson("/cookie/status");
    if (!res.ok || !data || typeof data !== "object") return;
    if (typeof data.has_cookie === "boolean") {
      setHasSavedCookie(data.has_cookie);
    }
  }

  async function initLogsTail() {
    const { res, data } = await fetchJson("/logs?since=0");
    if (!res.ok || !data || typeof data !== "object") return;
    const lines = Array.isArray(data.lines) ? data.lines : [];
    const cursor = typeof data.cursor === "number" ? data.cursor : 0;
    logCursorRef.current = cursor;
    setLogCursor(cursor);
    if (lines.length) {
      setLogLines(lines.slice(-2000));
    }
  }

  async function fetchLogsOnce() {
    const cursor = logCursorRef.current || 0;
    const { res, data } = await fetchJson(`/logs?since=${cursor}`);
    if (!res.ok || !data || typeof data !== "object") return;
    const lines = Array.isArray(data.lines) ? data.lines : [];
    const nextCursor = typeof data.cursor === "number" ? data.cursor : cursor;
    logCursorRef.current = nextCursor;
    setLogCursor(nextCursor);
    if (!lines.length) return;
    setLogLines((prev) => {
      const base = prev[0] === "等待任务开始…" ? [] : prev;
      return base.concat(lines).slice(-2000);
    });
  }

  async function resetLogCursorToEnd() {
    const { res, data } = await fetchJson("/logs/cursor");
    if (!res.ok || !data || typeof data !== "object") return;
    const cursor = typeof data.cursor === "number" ? data.cursor : 0;
    logCursorRef.current = cursor;
    setLogCursor(cursor);
    setLogLines([]);
  }

  async function stopMonitor(uid) {
    const { res, data } = await fetchJson("/stop-monitor", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ uid }),
    });
    if (!res.ok) {
      uiLog((data && data.message) || "停止监控失败");
      return;
    }
    uiLog((data && data.message) || `已停止 ${uid}`);
    await loadMonitorTasks();
  }

  async function stopRun() {
    if (isStopping) return;
    setIsStopping(true);
    try {
      const { res, data } = await fetchJson("/stop-run", { method: "POST" });
      if (!res.ok) {
        uiLog((data && data.message) || "停止任务失败");
        return;
      }
      uiLog((data && data.message) || "已发送停止信号。");
    } catch (e) {
      uiLog(`停止任务失败：${e && e.message ? e.message : String(e)}`);
    } finally {
      setIsStopping(false);
    }
  }

  async function submit() {
    const target = uidOrUrl.trim();
    if (!target) {
      uiLog(mode === "batch" ? "请先填写微博链接（每行一个）。" : "请先填写 UID 或 weibo.com 链接。");
      return;
    }

    setIsSubmitting(true);
    try {
      await resetLogCursorToEnd();
      uiLog("任务已提交，等待后端开始处理…");

      const body = {
        uid_or_url: target,
        mode,
        start_date: startDate || "",
        end_date: endDate || "",
        interval_minutes: normalizeNumber(intervalMinutes, 5),
        cookie: cookie.trim(),
        pack: packEnabled,
      };

      const { res, data } = await fetchJson("/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });

      if (!res.ok) {
        uiLog((data && data.message) || "任务启动失败");
        return;
      }

      uiLog((data && data.message) || "已启动。");
      await Promise.allSettled([loadHistory(), loadMonitorTasks(), loadCookieStatus()]);
    } catch (e) {
      uiLog(`启动任务失败：${e && e.message ? e.message : String(e)}`);
    } finally {
      setIsSubmitting(false);
    }
  }

  useEffect(() => {
    initLogsTail().catch(() => {});
    loadHistory().catch(() => {});
    loadMonitorTasks().catch(() => {});
    loadCookieStatus().catch(() => {});
  }, []);

  useEffect(() => {
    const timer = setInterval(() => {
      fetchLogsOnce().catch(() => {});
    }, 1500);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!autoScroll) return;
    const el = logBoxRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [autoScroll, logLines.length]);

  const cookieHint = (() => {
    if (cookie.trim()) return "将使用你本次粘贴的 Cookie（会覆盖服务器保存的 Cookie）。";
    if (hasSavedCookie === true) return "Cookie 未填写：将使用服务器已保存的 Cookie。";
    if (hasSavedCookie === false) return "Cookie 未填写：服务器未保存 Cookie，运行会失败。";
    return "Cookie 未填写：是否已有保存 Cookie 取决于服务器状态。";
  })();

  return (
    <div className="page">
      <div className="top-bar">
        <div className="title-block">
          <span className="dot red" />
          <span className="dot yellow" />
          <span className="dot green" />
          <div style={{ marginLeft: 10 }}>
            <div className="app-title">Weibo Media Studio</div>
            <div className="app-subtitle">微博媒体下载 & 监控（React 前端）</div>
          </div>
        </div>
        <div className="app-subtitle">
          日志 cursor：<code>{logCursor}</code>
        </div>
      </div>

      <div className="layout">
        <div className="card card-left">
          <div className="section">
            <div className="section-header">
              <div className="section-title">目标</div>
              <div className="section-subtitle">支持 UID / weibo.com 链接，批量模式一行一个链接</div>
            </div>
            <div className="field">
              <label>
                {mode === "batch" ? "微博链接列表（一行一个）" : "UID / 链接（监控支持多行或逗号分隔）"}
              </label>
              <textarea
                className="input textarea"
                rows={4}
                placeholder={
                  mode === "batch"
                    ? "例如：\nhttps://weibo.com/xxx/AbcdefG\nhttps://m.weibo.cn/detail/1234567890"
                    : "例如：5364794518 或 https://weibo.com/u/5364794518"
                }
                value={uidOrUrl}
                onChange={(e) => setUidOrUrl(e.target.value)}
              />
            </div>
          </div>

          <div className="section">
            <div className="section-header">
              <div className="section-title">模式</div>
              <div className="section-subtitle">五种模式</div>
            </div>

            <div className="segmented-control" role="tablist" aria-label="mode">
              {["all", "range", "latest", "batch", "monitor"].map((m) => (
                <label key={m} className="segmented-item">
                  <input
                    type="radio"
                    name="mode"
                    value={m}
                    checked={mode === m}
                    onChange={() => setMode(m)}
                  />
                  <span>
                    {m === "all"
                      ? "全部媒体"
                      : m === "range"
                      ? "按时间段"
                      : m === "latest"
                      ? "最新一条"
                      : m === "batch"
                      ? "批量下载"
                      : "监控模式"}
                  </span>
                </label>
              ))}
            </div>

            {mode === "range" ? (
              <div className="mode-extra" data-mode-show>
                <div className="field inline">
                  <label>开始日期（含）</label>
                  <input
                    className="input"
                    type="date"
                    value={startDate}
                    onChange={(e) => setStartDate(e.target.value)}
                  />
                </div>
                <div className="field inline">
                  <label>结束日期（含）</label>
                  <input
                    className="input"
                    type="date"
                    value={endDate}
                    onChange={(e) => setEndDate(e.target.value)}
                  />
                </div>
              </div>
            ) : null}

            {mode === "monitor" ? (
              <div className="mode-extra" data-mode-show>
                <div className="field inline">
                  <label>监控间隔（分钟）</label>
                  <input
                    className="input"
                    type="number"
                    min={1}
                    max={1440}
                    value={intervalMinutes}
                    onChange={(e) => setIntervalMinutes(e.target.value)}
                  />
                </div>
              </div>
            ) : null}

            <div className="hint">
              {modeTips.find((t) => t.key === mode)?.text || ""}
            </div>
          </div>

          <div className="section">
            <div className="section-header">
              <div className="section-title">Cookie</div>
              <div className="section-subtitle">weibo.com 登录态</div>
            </div>
            <div className="field">
              <label>weibo.com Cookie（可留空使用服务器已保存 Cookie）</label>
              <textarea
                className="input textarea"
                rows={4}
                placeholder="从浏览器复制完整 Cookie 粘贴到这里"
                value={cookie}
                onChange={(e) => setCookie(e.target.value)}
              />
              <div className="hint">{cookieHint}</div>
            </div>
          </div>

          <div className="section">
            <div className="section-header">
              <div className="section-title">是否打包</div>
              <div className="section-subtitle">目录结构</div>
            </div>
            <div className="segmented-control" role="tablist" aria-label="pack-mode">
              <label className="segmented-item">
                <input
                  type="radio"
                  name="pack"
                  value="yes"
                  checked={packEnabled === true}
                  onChange={() => setPackEnabled(true)}
                />
                <span>是（按分类目录）</span>
              </label>
              <label className="segmented-item">
                <input
                  type="radio"
                  name="pack"
                  value="no"
                  checked={packEnabled === false}
                  onChange={() => setPackEnabled(false)}
                />
                <span>否（全部放在昵称目录）</span>
              </label>
            </div>
            <div className="hint">
              否：文件名取贴子文案前 10 个字，若重复会自动加序号。
            </div>
          </div>

          <div className="footer-actions">
            <button
              className="primary-btn"
              type="button"
              onClick={() => submit()}
              disabled={isSubmitting}
            >
              {isSubmitting ? "运行中…" : "开始任务"}
            </button>
            <button
              className="ghost-btn danger-btn"
              type="button"
              onClick={() => stopRun()}
              disabled={isStopping}
            >
              {isStopping ? "停止中…" : "停止任务"}
            </button>
          </div>
        </div>

        <div className="card card-right">
          <div className="section">
            <div className="section-header">
              <div className="section-title">实时日志</div>
              <div className="section-subtitle">轮询 /logs（增量）</div>
            </div>

            <div className="footer-actions" style={{ justifyContent: "space-between" }}>
              <button
                className="ghost-btn"
                type="button"
                onClick={() => {
                  const el = logBoxRef.current;
                  if (el) el.scrollTop = el.scrollHeight;
                }}
              >
                滚动到底部
              </button>
              <label style={{ fontSize: 11, color: "var(--text-muted)" }}>
                <input
                  type="checkbox"
                  checked={autoScroll}
                  onChange={(e) => setAutoScroll(e.target.checked)}
                  style={{ marginRight: 6 }}
                />
                自动滚动
              </label>
              <button
                className="ghost-btn"
                type="button"
                onClick={() => {
                  resetLogCursorToEnd().catch(() => {});
                  uiLog("已清空显示并把 cursor 移到日志末尾。");
                }}
              >
                清空
              </button>
            </div>

            <div className="log-box" ref={logBoxRef}>
              <pre>{logLines.join("\n")}</pre>
            </div>
          </div>

          <div className="section">
            <div className="section-header">
              <div className="section-title">历史用户</div>
              <div className="section-subtitle">点击行填充 UID</div>
            </div>
            <div className="history-table">
              <table>
                <thead>
                  <tr>
                    <th>UID</th>
                    <th>昵称</th>
                    <th>时间</th>
                  </tr>
                </thead>
                <tbody>
                  {history.length ? (
                    history.map((h) => (
                      <tr
                        key={`${h.uid || ""}-${h.last_time || ""}`}
                        onClick={() => setUidOrUrl(h.uid || "")}
                      >
                        <td>{h.uid || ""}</td>
                        <td>{h.nickname || ""}</td>
                        <td>{h.last_time || ""}</td>
                      </tr>
                    ))
                  ) : (
                    <tr>
                      <td colSpan={3} style={{ color: "var(--text-muted)" }}>
                        暂无历史记录
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>

          <div className="section">
            <div className="section-header">
              <div className="section-title">监控任务</div>
              <div className="section-subtitle">后台轮询最新一条</div>
            </div>
            <ul className="monitor-list">
              {monitorTasks.length ? (
                monitorTasks.map((t) => (
                  <li className="monitor-item" key={t.uid}>
                    <div className="monitor-info">
                      <div className="monitor-uid">
                        {(t.nickname && t.nickname.trim()) || t.uid}
                      </div>
                      <div className="monitor-meta">
                        UID：{t.uid} · 间隔：{t.interval} 分钟 · 最近：{t.last_run || "—"}
                      </div>
                    </div>
                    <button
                      className="ghost-btn"
                      type="button"
                      onClick={() => stopMonitor(t.uid)}
                    >
                      停止
                    </button>
                  </li>
                ))
              ) : (
                <li className="monitor-item">
                  <div className="monitor-info">
                    <div className="monitor-uid" style={{ color: "var(--text-muted)" }}>
                      当前无监控任务
                    </div>
                  </div>
                </li>
              )}
            </ul>
          </div>

          <div className="hint" style={{ marginTop: 12 }}>
            仅用于个人备份，请遵守平台协议与法律法规。（来源：项目原页面提示）
          </div>
        </div>
      </div>
    </div>
  );
}
