"""覆蓋率稽核：抓出「feed 還活著，但重要內容被搬到別的分類去了」。

為什麼需要這支程式：
    2026-08-27 發現 Google 把模型發布搬到 /innovation-and-ai/models-and-research/，
    而我們訂的 /technology/ai/ 仍然回 200、仍然每天更新，只是內容剩下消費性行銷
    貼文。結果 Gemini 3.5 Transcribe 完全沒進日報。

    這種失敗躲得過所有現有檢查：
      - 日報的「本次未取得的來源」不會列它（沒有失敗）
      - verify_feeds.py 會通過（HTTP 200、解析得到項目）
      - 「最新一篇距今幾天」的新鮮度檢查也沒用（那個 feed 顯示 1 天前更新，
        更新的是家飾貼文）

    唯一可行的辦法是拿站台的「總表 feed」跟我們訂的分類 feed 做差集：
    總表有、我們的分類 feed 沒有，而且標題帶 AI 訊號的，就是漏掉的東西。

做不到的事（要先說清楚）：
    只有提供總表 feed 的站台適用。沒有總表的站台（多數個人部落格、多數新聞網站）
    這支程式幫不上忙。它縮小盲區，不消除盲區。

用法：
    python audit_coverage.py              # 印出報告
    python audit_coverage.py --days 14    # 只看最近 14 天的項目
    python audit_coverage.py --json       # 輸出 JSON，給排程接手用
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

import feedparser

import collect
from fetchlib import entry_time, normalize_url, polite_get

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# 站台總表 → 我們實際訂閱的分類 feed。
# 只列「確定有總表」的站台；沒有總表的不要硬湊一個進來，那只會產生假訊號。
WATCH = [
    {
        "site": "blog.google",
        "master": "https://blog.google/rss/",
        "ours": [
            "https://blog.google/technology/google-deepmind/rss/",
            "https://blog.google/technology/ai/rss/",
            "https://blog.google/innovation-and-ai/models-and-research/rss/",
            "https://blog.google/technology/developers/rss/",
        ],
        # 總表混了 Pixel、Search、Workspace 等非 AI 內容，必須過關鍵字，
        # 否則每天都會報一堆手機新聞
        "ai_filter": True,
    },
]


def fetch(url: str) -> tuple[dict[str, dict], str]:
    """回傳 ({正規化網址: {title, published}}, 錯誤訊息)。"""
    try:
        r = polite_get(url, timeout=25)
    except Exception as e:
        return {}, f"{e.__class__.__name__}: {str(e)[:80]}"
    if not r or r.status_code != 200:
        return {}, f"HTTP {r.status_code if r else '無回應'}"
    parsed = feedparser.parse(r.text)
    if not parsed.entries:
        return {}, "解析不到項目"
    now = datetime.now(timezone.utc)
    out = {}
    for e in parsed.entries:
        link = (getattr(e, "link", "") or "").strip()
        title = collect.strip_html(getattr(e, "title", ""), limit=300)
        if not link or not title:
            continue
        published, _, _ = entry_time(e, fallback=now)
        out[normalize_url(link)] = {"title": title, "published": published}
    return out, ""


def audit(days: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    reports = []
    for w in WATCH:
        master, err = fetch(w["master"])
        if err:
            reports.append({"site": w["site"], "error": f"總表抓不到：{err}", "missing": []})
            continue

        ours: dict[str, dict] = {}
        feed_errors = []
        for u in w["ours"]:
            got, e = fetch(u)
            if e:
                feed_errors.append(f"{u} → {e}")
            ours.update(got)

        missing = []
        for url, meta in master.items():
            if url in ours:
                continue
            if meta["published"] < cutoff:
                continue
            if w.get("ai_filter"):
                relevant, strength = collect.ai_relevance(meta["title"], "")
                if not relevant:
                    continue
            else:
                strength = 0
            missing.append({
                "url": url,
                "title": meta["title"],
                "published": meta["published"].isoformat(),
                "ai_strength": strength,
            })
        missing.sort(key=lambda m: m["published"], reverse=True)
        reports.append({
            "site": w["site"],
            "master_items": len(master),
            "our_items": len(ours),
            "feed_errors": feed_errors,
            "missing": missing,
        })
    return reports


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="只看最近幾天的項目")
    ap.add_argument("--json", action="store_true", help="輸出 JSON")
    args = ap.parse_args()

    reports = audit(args.days)
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
        return

    total_missing = 0
    for r in reports:
        print(f"\n=== {r['site']} ===")
        if r.get("error"):
            print(f"  {r['error']}")
            continue
        print(f"  總表 {r['master_items']} 則、我們訂的分類 feed 合計 {r['our_items']} 則")
        for e in r["feed_errors"]:
            print(f"  ⚠ {e}")
        if not r["missing"]:
            print(f"  ✓ 最近 {args.days} 天沒有漏掉的 AI 項目")
            continue
        total_missing += len(r["missing"])
        print(f"  ✗ 總表有、我們沒有的 AI 項目：{len(r['missing'])} 則")
        for m in r["missing"]:
            print(f"      [{m['published'][:10]}] {m['title'][:66]}")
            print(f"        {m['url']}")

    print()
    if total_missing:
        print(f"共 {total_missing} 則。每一則都代表某個分類 feed 沒有涵蓋到——")
        print("要嘛把對應的分類 feed 加進 sources.yaml，要嘛確認那則本來就不該收。")
        sys.exit(1)   # 讓排程能靠結束碼判斷
    print("沒有發現漏掉的項目。")


if __name__ == "__main__":
    main()
