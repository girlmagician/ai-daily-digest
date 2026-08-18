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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import trafilatura

from fetchlib import looks_like_content, polite_get

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
CANDIDATES = ROOT / "out" / "candidates.json"

MAX_CHARS = 6000        # 超過這個長度對摘要沒有幫助，只是燒 token
MIN_GAIN = 200          # 抓到的內文至少要比 feed 摘要多這麼多字才值得換掉


def extract_one(item: dict) -> dict:
    """回傳 {"id":…, "text":…, "error":…}。任何失敗都只記錄。"""
    url = item.get("url_raw") or item.get("url") or ""
    out = {"id": item["id"], "text": "", "error": ""}
    if not url:
        out["error"] = "無網址"
        return out

    try:
        resp = polite_get(url, timeout=30)
        if resp.status_code != 200:
            out["error"] = f"HTTP {resp.status_code}"
            return out
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

    CANDIDATES.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"  取得原文 {upgraded} 則、沿用 feed 摘要 {kept} 則")
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
