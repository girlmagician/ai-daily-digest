"""沒有 RSS 的來源：直接爬索引頁。

目前有兩個非爬不可的：
    Anthropic News   官方公告，四個 feed 候選全部 404
    The Batch        Andrew Ng 的電子報，2026-08 複查仍無 RSS

做法刻意保守：索引頁只用來取得文章網址清單，標題、日期、內文一律從
文章頁本身用 trafilatura 抽取。索引頁的排版最常改，把解析責任放在
那裡最容易壞；文章頁有標準的 meta 標籤，穩定得多。

抓不到日期的文章直接跳過，不用「現在」當預設值——那會讓舊文章每天
都被當成新聞重新刊登一次。

機器之心不在這裡：首頁只回 3,251 字的 JS 空殼，沒有瀏覽器抓不到內容。
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urljoin

import trafilatura

from fetchlib import item_id, looks_like_content, normalize_url, polite_get, strip_html

MAX_LINKS = 15          # 索引頁最多追幾條連結，避免一次打太多請求
MAX_CHARS = 6000


# 分類頁、作者頁、搜尋頁長得跟文章很像，但抓回來只有一句導言。
# 實測 The Batch 的索引頁上，/the-batch/tag/… 比真正的文章還多。
NOT_ARTICLE = re.compile(r"/(tag|tags|category|categories|author|search|about|page)(/|$)")


def _links(html: str, base: str, pattern: str, limit: int) -> list[str]:
    """索引頁上符合路徑樣式的文章連結，保持原順序（通常就是由新到舊）。"""
    seen, out = set(), []
    for href in re.findall(r'href="([^"#?]+)"', html):
        if pattern not in href:
            continue
        url = urljoin(base, href)
        # 索引頁自己、分頁、分類頁都不要
        if url.rstrip("/") == base.rstrip("/") or NOT_ARTICLE.search(url):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= limit:
            break
    return out


def _article(url: str) -> dict | None:
    """抓單篇文章。回傳 None 代表不可用（含抓不到日期的情況）。"""
    try:
        resp = polite_get(url, timeout=30)
        if resp.status_code != 200:
            return None
        meta = trafilatura.extract_metadata(resp.text)
        text = trafilatura.extract(
            resp.text, favor_precision=True, include_comments=False, include_tables=False
        )
    except Exception:
        return None

    if meta is None or not meta.title or not meta.date:
        return None
    try:
        published = datetime.strptime(meta.date[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None

    # 抽到樣板就退回用頁面自己的 description，別把登入提示當內文
    body = text if looks_like_content(text) else (meta.description or "")
    return {
        "url": url,
        "title": strip_html(meta.title, limit=300),
        "text": (body or "").strip()[:MAX_CHARS],
        "published": published,
    }


def fetch_scraped(src: dict, cutoff: datetime, workers: int = 4) -> dict:
    """介面與 collect.fetch_source 相同：{"report":…, "items":[…]}。"""
    report = {
        "name": src["name"], "url": src["target"], "lang": src["lang"],
        "cat": src.get("cat", "vendor"), "status": "ok", "error": "",
        "entries": 0, "in_window": 0, "kept": 0,
    }
    fetched_at = datetime.now(timezone.utc)

    try:
        resp = polite_get(src["target"], timeout=src.get("timeout") or 30)
        if resp.status_code != 200:
            report.update(status="fail", error=f"索引頁 HTTP {resp.status_code}")
            return {"report": report, "items": []}
    except Exception as e:
        report.update(status="fail", error=f"{e.__class__.__name__}: {str(e)[:100]}")
        return {"report": report, "items": []}

    urls = _links(resp.text, src["target"], src["link_pattern"],
                  int(src.get("max_links") or MAX_LINKS))
    report["entries"] = len(urls)
    if not urls:
        report.update(status="fail", error="索引頁找不到文章連結（版型可能改了）")
        return {"report": report, "items": []}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        articles = [a for a in pool.map(_article, urls) if a]

    items = []
    for a in articles:
        if a["published"] < cutoff:
            continue
        report["in_window"] += 1
        items.append({
            "ai_strength": 4,             # 這些來源本身就是 AI 專門，不需再過濾
            "id": item_id(a["url"]),
            "source": src["name"],
            "lang": src["lang"],
            "cat": src.get("cat", "vendor"),
            "weight": float(src.get("weight", 1.5)),
            "ai_filter": False,
            "pinned": bool(src.get("pinned")),
            "title_original": a["title"],
            "summary_original": a["text"],
            "text_source": "article",     # 已經是全文，extract.py 不必再抓
            "url": normalize_url(a["url"]),
            "url_raw": a["url"],
            "published_utc": a["published"].isoformat(),
            "time_clamped": False,
            # 文章頁多半只給到「日」，時間一律當成當天 00:00，
            # 排序上會略微吃虧，但總比假裝知道確切時間好
            "time_estimated": True,
            "fetched_at_utc": fetched_at.isoformat(),
        })

    report["kept"] = len(items)
    if not items and articles:
        report["error"] = "有文章但都不在時間窗內"
    return {"report": report, "items": items}
