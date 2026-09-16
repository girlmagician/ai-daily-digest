"""第一‧五層：補抓原文全文，讓摘要不必靠腦補。

為什麼需要這一層：很多 RSS 只給一段導言，模型手上沒有內容就寫不出有料的摘要。
在沒有這層的情況下要求「摘要要能看懂整篇」，等於逼模型編造——那正是整套設計
要防的事。所以要拿到真正的內文，模型才能在只改寫眼前文字的前提下寫得完整。

只對入選的項目抓（約 30 則），不是全部候選：省時間，也不必對上百個網站發請求。
論文不抓——arXiv 的摘要本來就是完整摘要，正文抓回來反而是雜訊。

抓不到就沿用 feed 摘要，絕不中斷流程。付費牆、403、JS 網站都算正常情況。

用法：
    python extract.py            # 在 collect.py 之後、translate.py 之前
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import trafilatura
from trafilatura.metadata import extract_metadata

from fetchlib import looks_like_content, polite_get

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
CANDIDATES = ROOT / "out" / "candidates.json"

MAX_CHARS = 6000        # 超過這個長度對摘要沒有幫助，只是燒 token
MIN_GAIN = 200          # 抓到的內文至少要比 feed 摘要多這麼多字才值得換掉

# HN 補進來的項目，`published_utc` 是投稿到 HN 的時間，不是原文發表日期，
# 所以 collect.py 的時間窗對那條路徑完全沒有作用（詳見 SETTINGS.md 的 HN 段）。
# 這一層本來就會把原文 HTML 抓回來，順手用 trafilatura 的 metadata 取真正的
# 發表日期，是唯一能拿到事實的地方——標題沒有 (2019) 這種標記時也擋得住。
#
# 只對 HN 來源做剔除：別的來源日期來自 feed，本來就可信，就算 metadata 抓到
# 舊日期也多半是網站自己的 meta 標錯，不該因此丟掉稿子。
# 14 天是刻意放寬的：抓的是「明顯是舊文」，一週內的時間差留給策展階段判斷。
STALE_DAYS = 14


def is_stale(item: dict, article_date: str) -> bool:
    """原文發表日期比「被投稿到 HN 的時間」早太多 = 舊文重貼。

    刻意拿 `published_utc`（投稿時間）當基準而不是「今天」：
    `backfill.py` 回補歷史日期時，整批項目本來就比今天舊好幾週，
    用今天當基準會把回補的每一則都判成舊文。
    """
    if item.get("source") != "Hacker News" or not article_date:
        return False
    try:
        published = datetime.fromisoformat(
            item["published_utc"].replace("Z", "+00:00")).date()
        written = date.fromisoformat(article_date[:10])
    except (KeyError, ValueError):
        return False
    return written < published - timedelta(days=STALE_DAYS)


def extract_one(item: dict) -> dict:
    """回傳 {"id":…, "text":…, "date":…, "error":…}。任何失敗都只記錄。"""
    url = item.get("url_raw") or item.get("url") or ""
    out = {"id": item["id"], "text": "", "date": "", "error": ""}
    if not url:
        out["error"] = "無網址"
        return out

    try:
        resp = polite_get(url, timeout=30)
        if resp.status_code != 200:
            out["error"] = f"HTTP {resp.status_code}"
            return out
        # 日期先取。付費牆頁面抽不到內文卻常常抽得到 meta 日期，
        # 而且日期失敗絕不能連帶讓內文也拿不到，所以自己包一層
        try:
            meta = extract_metadata(resp.text)
            out["date"] = (meta.date or "") if meta else ""
        except Exception:
            out["date"] = ""
        text = trafilatura.extract(
            resp.text,
            favor_precision=True,      # 寧可少抓一段，也不要把導覽列當內文
            include_comments=False,
            include_tables=False,
            no_fallback=False,
        )
    except Exception as e:
        out["error"] = f"{e.__class__.__name__}: {str(e)[:80]}"
        return out

    if not looks_like_content(text):
        # 抽到的多半是登入提示、cookie 告知或導覽列殘渣。當成沒抓到，
        # 沿用 feed 摘要——寧可短，也不要把樣板文字當內文餵給翻譯
        out["error"] = "抽不到內文（付費牆、純 JS 網站，或只抽到頁面樣板）"
        return out

    out["text"] = text.strip()[:MAX_CHARS]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--include-papers", action="store_true", help="連論文也抓正文（預設不抓）")
    ap.add_argument("--keep-stale", action="store_true",
                    help="不剔除 HN 舊文重貼（除錯用；正常執行不要開）")
    args = ap.parse_args()

    if not CANDIDATES.exists():
        sys.exit(f"找不到 {CANDIDATES}，請先執行 python collect.py")
    data = json.loads(CANDIDATES.read_text(encoding="utf-8"))

    targets = data["official"] + data["ranked"]
    if args.include_papers:
        targets += data.get("papers", [])
    # 爬蟲來源（scrape.py）拿到的本來就是全文，不必再抓一次
    already = sum(1 for i in targets if i.get("text_source") == "article")
    targets = [i for i in targets if i.get("text_source") != "article"]
    # json.load 會讓各清單拿到不同的 dict 物件，所以要依 id 回填到每一份
    by_id: dict[str, list[dict]] = {}
    for key in ("official", "ranked", "papers", "all_scored"):
        for item in data.get(key, []):
            by_id.setdefault(item["id"], []).append(item)

    skip_note = f"（另有 {already} 則由爬蟲取得，已是全文）" if already else ""
    print(f"補抓原文：{len(targets)} 則{skip_note}…")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(extract_one, targets))

    upgraded, kept, failed = 0, 0, []
    for target, res in zip(targets, results):
        feed_text = target.get("summary_original") or ""
        copies = by_id.get(res["id"], [])

        if res["text"] and len(res["text"]) >= len(feed_text) + MIN_GAIN:
            for c in copies:
                c["summary_original"] = res["text"]
                c["text_source"] = "article"
                c["feed_summary"] = feed_text
            upgraded += 1
        else:
            for c in copies:
                c["text_source"] = "feed"
            kept += 1
            if res["error"]:
                failed.append((target["source"], target["title_original"][:40], res["error"]))

        # 真實發表日期一律帶著走：策展階段的候選行會顯示它，
        # 這是模型判斷舊聞時唯一可信的依據（HN 項目的 published_utc 是投稿時間）
        for c in copies:
            c["article_date"] = res["date"]

    # HN 舊文重貼的最後一道防線。標題沒有 (2019) 這種標記時，
    # collect.py 攔不到，只有抓回原文才知道它有多舊
    stale = []
    if not args.keep_stale:
        for target, res in zip(targets, results):
            if is_stale(target, res["date"]):
                stale.append((res["id"], res["date"], target["title_original"][:50]))
        drop = {i for i, _, _ in stale}
        for key in ("official", "ranked", "papers"):
            if key in data:
                data[key] = [i for i in data[key] if i["id"] not in drop]

    CANDIDATES.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"  取得原文 {upgraded} 則、沿用 feed 摘要 {kept} 則")
    dated = sum(1 for r in results if r["date"])
    print(f"  取得原文發表日期 {dated} 則（其餘網站未提供，交由策展階段判斷）")
    if stale:
        print(f"\n剔除 HN 舊文重貼（{len(stale)} 則，原文比投稿時間早 {STALE_DAYS} 天以上）：")
        for _, day, title in stale:
            print(f"  {day}  {title}")
    if failed:
        print(f"\n抓不到原文（{len(failed)} 則，沿用 feed 摘要，不影響流程）：")
        for source, title, error in failed[:15]:
            print(f"  {source:<18} {error:<40} {title}")

    lengths = [len(i.get("summary_original") or "") for i in targets]
    if lengths:
        lengths.sort()
        print(f"\n可用內文長度：中位數 {lengths[len(lengths) // 2]} 字、"
              f"最短 {lengths[0]}、最長 {lengths[-1]}")


if __name__ == "__main__":
    main()
