"""驗證 sources.yaml 裡每個來源的候選網址，找出真正可用的那一個。

用法：
    python verify_feeds.py

輸出：
    1. 終端摘要
    2. feed_report.md          — 完整報告（含每個候選網址的失敗原因）
    3. sources_verified.yaml   — 只含通過驗證的來源與勝出網址，供正式管線讀取

判定標準（三者都要過）：
    1. HTTP 200
    2. feedparser 能解析出 entries
    3. 至少一篇項目

與第一版的差異：
    - 每個來源可有多個候選網址，依序嘗試，第一個通過就採用
    - 同網域請求序列化並加間隔（Reddit 等站台會對併發回 429）
    - 遇 429 會依 Retry-After 重試一次
"""

from __future__ import annotations

import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import feedparser
import requests
import yaml

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
SOURCES = ROOT / "sources.yaml"
REPORT = ROOT / "feed_report.md"
VERIFIED = ROOT / "sources_verified.yaml"

HEADERS = {
    "User-Agent": "ai-daily-digest/0.2 (feed verification; contact: elsawu@igs.com.tw)",
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    "Accept-Language": "en,ja;q=0.8,zh;q=0.8",
}
TIMEOUT = 20
MAX_WORKERS = 8
STALE_DAYS = 30  # 超過這個天數沒更新就視為停更，讓其他候選網址有機會勝出

# 每個網域的最小請求間隔（秒）。Reddit 對併發特別敏感。
DOMAIN_INTERVAL = defaultdict(lambda: 0.4, {"www.reddit.com": 3.0, "rsshub.app": 2.0})
_domain_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_domain_last: dict[str, float] = defaultdict(float)


def polite_get(url: str) -> requests.Response:
    """同網域序列化 + 間隔，遇 429 依 Retry-After 重試一次。"""
    host = urlparse(url).netloc
    for attempt in (1, 2):
        with _domain_locks[host]:
            wait = DOMAIN_INTERVAL[host] - (time.monotonic() - _domain_last[host])
            if wait > 0:
                time.sleep(wait)
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=True)
            _domain_last[host] = time.monotonic()

        if resp.status_code != 429 or attempt == 2:
            return resp

        retry_after = resp.headers.get("Retry-After", "5")
        try:
            delay = min(float(retry_after), 30.0)
        except ValueError:
            delay = 5.0
        time.sleep(delay)
    return resp


def entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, key, None)
        if parsed:
            return datetime(*parsed[:6], tzinfo=timezone.utc)
    return None


def try_url(url: str) -> dict:
    """回傳單一網址的檢查結果。"""
    out = {"url": url, "ok": False, "stale": False, "entries": 0, "latest": None, "detail": ""}
    try:
        resp = polite_get(url)
        if resp.status_code != 200:
            out["detail"] = f"HTTP {resp.status_code}"
            return out

        parsed = feedparser.parse(resp.content)
        out["entries"] = len(parsed.entries)
        if not parsed.entries:
            bozo = getattr(parsed, "bozo_exception", None)
            kind = type(bozo).__name__ if bozo else "空 feed"
            ctype = resp.headers.get("Content-Type", "?").split(";")[0]
            out["detail"] = f"解析不到項目（{kind}, Content-Type={ctype}）"
            return out

        out["ok"] = True
        times = [t for t in (entry_time(e) for e in parsed.entries) if t]
        if times:
            latest = max(times)
            out["latest"] = latest
            days = (datetime.now(timezone.utc) - latest).days
            out["detail"] = f"{days} 天前更新" if days >= 0 else "今日（feed 時區偏移）"
            if days > STALE_DAYS:
                # 停更的 feed 不算真正可用，讓後面的候選網址有機會勝出
                out["ok"] = False
                out["stale"] = True
                out["detail"] = f"最新一篇已 {days} 天前，疑似停更"
        else:
            out["detail"] = "可解析，但項目無時間欄位（需自行補抓取時間）"
        return out

    except requests.exceptions.SSLError:
        out["detail"] = "SSL 憑證錯誤"
    except requests.exceptions.Timeout:
        out["detail"] = f"逾時（>{TIMEOUT}s）"
    except requests.exceptions.ConnectionError as e:
        msg = str(e)
        if "NameResolutionError" in msg or "getaddrinfo" in msg:
            out["detail"] = "DNS 無法解析（網域可能已失效）"
        else:
            out["detail"] = "連線失敗（可能擋境外 IP 或站台離線）"
    except Exception as e:
        out["detail"] = f"{e.__class__.__name__}: {str(e)[:100]}"
    return out


def check(src: dict) -> dict:
    """依序嘗試候選網址，第一個通過就採用。"""
    attempts = [try_url(u) for u in src["urls"]]

    # 優先取真正可用的；全部停更時退而採用停更的那個（標記 STALE，不直接丟掉）
    winner = next((a for a in attempts if a["ok"]), None)
    status = "OK"
    if winner is None:
        winner = next((a for a in attempts if a["stale"]), None)
        status = "STALE" if winner else "FAIL"

    return {
        "name": src["name"],
        "lang": src["lang"],
        "cat": src["cat"],
        "weight": src["weight"],
        "pinned": src.get("pinned", False),
        "ai_filter": src.get("ai_filter", False),
        "attempts": attempts,
        "winner": winner,
        "status": status,
    }


def main() -> None:
    config = yaml.safe_load(SOURCES.read_text(encoding="utf-8"))
    sources = config["sources"]
    total_urls = sum(len(s["urls"]) for s in sources)
    print(f"驗證 {len(sources)} 個來源、共 {total_urls} 個候選網址…\n")

    started = time.time()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        results = list(pool.map(check, sources))

    order = {"OK": 0, "STALE": 1, "FAIL": 2}
    results.sort(key=lambda r: (order[r["status"]], r["cat"], r["name"]))
    counts = {k: sum(1 for r in results if r["status"] == k) for k in order}

    for r in results:
        mark = {"OK": "[OK  ]", "STALE": "[停更]", "FAIL": "[失效]"}[r["status"]]
        if r["winner"]:
            tail = r["winner"]["detail"]
            if len(r["attempts"]) > 1:
                tail += f"（第 {r['attempts'].index(r['winner']) + 1} 個候選命中）"
        else:
            tail = " / ".join(a["detail"] for a in r["attempts"])
        print(f"{mark} {r['name']:<26} {tail}")

    print(
        f"\n可用 {counts['OK']}／停更 {counts['STALE']}／失效 {counts['FAIL']}"
        f"　（{time.time() - started:.1f}s）"
    )

    # ── feed_report.md ────────────────────────────────────────
    lines = [
        "# Feed 驗證報告（第二輪）",
        "",
        f"產生時間：{datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')}",
        f"可用 {counts['OK']}／停更 {counts['STALE']}／失效 {counts['FAIL']}（共 {len(results)} 個來源）",
        "",
        "## 通過驗證",
        "",
        "| 來源 | 語言 | 分類 | 權重 | 官方公告 | 項目數 | 最新 | 說明 | 採用網址 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        if r["status"] == "FAIL":
            continue
        w = r["winner"]
        latest = w["latest"].date().isoformat() if w["latest"] else "-"
        lines.append(
            f"| {r['name']} | {r['lang']} | {r['cat']} | {r['weight']} | "
            f"{'是' if r['pinned'] else ''} | {w['entries']} | {latest} | "
            f"{w['detail']} | {w['url']} |"
        )

    lines += ["", "## 全部候選失敗（需改用擷取器／API／替代來源）", ""]
    for r in results:
        if r["status"] != "FAIL":
            continue
        lines.append(f"### {r['name']}（{r['lang']} / {r['cat']}）")
        for a in r["attempts"]:
            lines.append(f"- `{a['url']}` → {a['detail']}")
        lines.append("")

    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ── sources_verified.yaml ─────────────────────────────────
    verified = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": [
            {
                "name": r["name"],
                "lang": r["lang"],
                "cat": r["cat"],
                "weight": r["weight"],
                "pinned": r["pinned"],
                "ai_filter": r["ai_filter"],
                "url": r["winner"]["url"],
                "note": r["winner"]["detail"],
                "stale": r["status"] == "STALE",
            }
            for r in results
            if r["status"] in ("OK", "STALE")
        ],
    }
    VERIFIED.write_text(
        yaml.safe_dump(verified, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    print(f"報告：{REPORT}")
    print(f"正式清單：{VERIFIED}（{len(verified['sources'])} 個來源）")


if __name__ == "__main__":
    main()
