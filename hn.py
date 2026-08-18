"""Hacker News 熱度訊號（Algolia API，無需認證）。

三個用途：
    1. 提供真實熱度分數（points / 留言數）——Reddit 擋機器人，HN 是可靠替代
    2. 補上跨語言去重抓不到的熱度：同一則英文原文被日中媒體轉載時，
       HN 分數會落在原文那則上
    3. 覆蓋我們沒有 RSS 的長尾來源（Anthropic、Roblox、Cohere、Stability 的
       文章一旦上 HN 就會被撈進來）

刻意只取 points >= MIN_POINTS 的故事：分數太低代表社群沒反應，
拿進來只會擴大候選池、增加後續 LLM 成本。
"""

from __future__ import annotations

from datetime import datetime, timezone

from fetchlib import normalize_url, polite_get

API = "https://hn.algolia.com/api/v1/search_by_date"
MIN_POINTS = 20
PAGE_SIZE = 200
MAX_PAGES = 8


def fetch_stories(cutoff: datetime) -> tuple[list[dict], str]:
    """抓取 cutoff 之後、分數達門檻的 HN 故事。

    回傳 (故事清單, 錯誤訊息)。錯誤不拋出——HN 掛掉不該中斷整個流程。
    """
    stories: list[dict] = []
    since = int(cutoff.timestamp())

    for page in range(MAX_PAGES):
        url = (
            f"{API}?tags=story"
            f"&numericFilters=created_at_i>{since},points>={MIN_POINTS}"
            f"&hitsPerPage={PAGE_SIZE}&page={page}"
        )
        try:
            resp = polite_get(url, timeout=25)
            if resp.status_code != 200:
                return stories, f"HTTP {resp.status_code}"
            data = resp.json()
        except Exception as e:
            return stories, f"{e.__class__.__name__}: {str(e)[:100]}"

        hits = data.get("hits", [])
        for hit in hits:
            link = hit.get("url")
            title = (hit.get("title") or "").strip()
            if not title:
                continue
            hn_url = f"https://news.ycombinator.com/item?id={hit['objectID']}"
            created = hit.get("created_at")
            stories.append({
                "title": title,
                # Ask HN / Show HN 沒有外部連結，用討論頁當來源
                "url": normalize_url(link) if link else hn_url,
                "hn_url": hn_url,
                "points": int(hit.get("points") or 0),
                "comments": int(hit.get("num_comments") or 0),
                "created_at_utc": created,
                "external": bool(link),
            })

        if page + 1 >= data.get("nbPages", 0):
            break

    return stories, ""


def points_by_url(stories: list[dict]) -> dict[str, dict]:
    """網址 → 最高分的那筆 HN 紀錄（同一篇可能被投稿多次）。"""
    best: dict[str, dict] = {}
    for s in stories:
        cur = best.get(s["url"])
        if cur is None or s["points"] > cur["points"]:
            best[s["url"]] = s
    return best
