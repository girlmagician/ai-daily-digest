"""回補歷史日期：為過去每一天各產生一頁日報。

為什麼要單獨寫一支，而不是把 collect.py 的時間窗拉長：
每天的日報是「當天的前 30 名」，不是「整段期間的前 30 名」。時間窗拉長只會
產生一頁跨九天的混合排名，較早的日期完全被淹沒。所以要按日切分後，
逐日獨立評分與挑選。

做法：
    1. 抓一次（涵蓋整段期間），不要每天各抓一次去打同樣的來源八遍
    2. 依台北日期切分
    3. 由舊到新逐日處理：評分 → 挑選 → 補抓原文 → 翻譯 → 產生存檔頁
       由舊到新是為了讓已發布清單逐日累積，後面的日期不會重複前面登過的項目

已知限制（無法解決，只能揭露）：
    · RSS 只保留最近幾則，越早的日期覆蓋越薄，主要靠 Hacker News 的歷史 API 補
    · arXiv 的 RSS 只有當日更新，歷史論文完全補不到（要改用 arXiv API）
    · 單一來源的取用上限要放大，否則每個來源只留最新的十二則，早期日期會餓死

用法：
    python backfill.py --from 2026-08-10 --to 2026-08-17
    python backfill.py --from 2026-08-10 --to 2026-08-17 --dry-run   # 只看每天撈到幾則
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import collect
import hn
import scrape

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
OUT = ROOT / "out"
CANDIDATES = OUT / "candidates.json"
REPORT = OUT / "run_report.json"
TPE = timezone(timedelta(hours=8))

# 一次抓多天，單一來源的上限必須放大：預設的 12 是為 48 小時的窗調的
BACKFILL_SOURCE_CAP = 80


def taipei_day(iso: str) -> str:
    return datetime.fromisoformat(iso).astimezone(TPE).strftime("%Y-%m-%d")


def fetch_range(since: datetime, workers: int) -> tuple[list[dict], list[dict]]:
    """抓一次，涵蓋整段期間。回傳 (項目, 來源報告)。"""
    sources = collect.load_sources()
    scrapers = collect.load_scrapers()
    print(f"抓取 {len(sources)} 個 RSS 來源 + {len(scrapers)} 個爬蟲"
          f"（{since.astimezone(TPE):%Y-%m-%d} 之後）…")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(
            lambda s: collect.fetch_source(s, since, cap=BACKFILL_SOURCE_CAP), sources))
    for src in scrapers:
        results.append(scrape.fetch_scraped(src, since))

    reports = [r["report"] for r in results]
    items = [i for r in results for i in r["items"]]

    stories, err = hn.fetch_stories(since)
    if err:
        print(f"  Hacker News 失敗：{err}")
    else:
        merged = collect.merge_hn(items, stories, since)
        items.extend(merged["added"])
        print(f"  Hacker News：{len(stories)} 則故事 → 對上 {merged['matched']} 則、"
              f"補入 {len(merged['added'])} 則")

    before = len(items)
    items = [i for i in items
             if not collect.is_contentless(i["title_original"], i.get("summary_original", ""))]
    if before != len(items):
        print(f"  剔除無內容 {before - len(items)} 則")

    failures = [r for r in reports if r["status"] != "ok"]
    if failures:
        print(f"  抓取失敗 {len(failures)} 個：" + "、".join(r["name"] for r in failures))
    return items, reports


def run(cmd: list[str]) -> None:
    """跑一個子流程，失敗就中止整個回補——不要留下半套的頁面。"""
    proc = subprocess.run([sys.executable, *cmd], cwd=ROOT)
    if proc.returncode != 0:
        sys.exit(f"步驟失敗（{' '.join(cmd)}），已中止。先前完成的日期都已寫入。")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="since", required=True, help="起始日期 YYYY-MM-DD（台北）")
    ap.add_argument("--to", dest="until", required=True, help="結束日期 YYYY-MM-DD（含）")
    ap.add_argument("--top", type=int, default=30, help="每天保留幾則")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true", help="只顯示每天撈到幾則，不呼叫模型")
    args = ap.parse_args()

    start = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=TPE)
    end = datetime.strptime(args.until, "%Y-%m-%d").replace(tzinfo=TPE)
    if end < start:
        sys.exit("--to 不能早於 --from")

    OUT.mkdir(exist_ok=True)
    items, reports = fetch_range(start, args.workers)
    clustered = collect.cluster(items)
    print(f"\n原始 {len(items)} 則 → 去重分群後 {len(clustered)} 則")

    by_day: dict[str, list[dict]] = defaultdict(list)
    for item in clustered:
        by_day[taipei_day(item["published_utc"])].append(item)

    days = []
    cursor = start
    while cursor <= end:
        days.append(cursor.strftime("%Y-%m-%d"))
        cursor += timedelta(days=1)

    print("\n每天可用則數：")
    for day in days:
        pool = by_day.get(day, [])
        srcs = len({i["source"] for i in pool})
        print(f"  {day}  {len(pool):>3} 則　來源 {srcs:>2} 家")

    if args.dry_run:
        print("\n（--dry-run，未呼叫模型）")
        return

    total_days = 0
    for day in days:
        pool = by_day.get(day, [])
        if not pool:
            print(f"\n=== {day}：沒有資料，跳過 ===")
            continue

        # 已發布清單逐日累積：由舊到新處理，後面的日期不會重複前面登過的
        seen = collect.load_seen()
        fresh = [i for i in pool if i["id"] not in seen]
        if not fresh:
            print(f"\n=== {day}：{len(pool)} 則全部已發布過，跳過 ===")
            continue

        print(f"\n{'=' * 60}\n=== {day}：{len(fresh)} 則候選"
              f"（已排除發布過的 {len(pool) - len(fresh)} 則）===")

        # 評分基準用當天結束時刻，而不是「現在」——否則九天前的項目
        # 會因為時效分數極低而全部被壓平，排序失去意義
        as_of = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=TPE) + timedelta(days=1)
        scored = [collect.score(i, as_of, 24) for i in fresh]
        selection = collect.select(scored, args.top, 12, 5, 3)

        CANDIDATES.write_text(json.dumps({
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "window_hours": 24,
            "digest_date": day,
            "official": selection["official"],
            "ranked": selection["ranked"],
            "papers": selection["papers"],
            "all_scored": scored,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        REPORT.write_text(json.dumps({
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "window_hours": 24,
            "sources_total": len(reports),
            "sources_ok": sum(1 for r in reports if r["status"] == "ok"),
            "sources_failed": sum(1 for r in reports if r["status"] != "ok"),
            "raw_items": len(pool),
            "clustered_items": len(pool),
            "candidate_items": len(fresh),
            "backfilled": True,
            "per_source": reports,
            "failures": [r for r in reports if r["status"] != "ok"],
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        run(["extract.py"])
        run(["translate.py", "--target", str(args.top)])

        # translate.py 不知道這是回補，日期要在這裡補上，render.py 才會寫對檔名
        digest = json.loads((OUT / "digest.json").read_text(encoding="utf-8"))
        digest["digest_date"] = day
        (OUT / "digest.json").write_text(
            json.dumps(digest, ensure_ascii=False, indent=2), encoding="utf-8")

        run(["render.py", "--no-index"])
        total_days += 1

    print(f"\n{'=' * 60}\n回補完成：{total_days} 天")
    print("index.html 未被覆蓋，首頁仍是最新那天。")


if __name__ == "__main__":
    main()
