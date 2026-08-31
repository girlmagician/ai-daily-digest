"""Hugging Face Daily Papers（官方 API，無需認證）。

為什麼需要它：
    arXiv 每天 announce 數百篇，三個分類加起來約 500–900 篇，而同一次 announce
    的所有論文時間戳完全相同、權重也相同——沒有外部訊號就**排不出名次**，
    硬排出來的前五名等於隨機五篇。

    原本規劃的外部訊號是 Hacker News，但 2026-08-31 實測發現行不通：
    48 小時內 HN 上的 arXiv 投稿只有 1 則，而且對不上當天 feed 的任何一篇。
    HN 討論論文通常是發表後好幾天甚至數週，跟 48 小時的時間窗幾乎沒有交集。

    HF Daily Papers 補的正是這個空缺——它是**當天**的人工策展清單且帶投票數，
    今天回傳的論文 ID 是 2608.28xxx，就是當天送出的。

兩個用途：
    1. 幫既有的 arXiv 項目補上重要性訊號（投票數）
    2. 把不在我們三個分類 feed 裡的論文補進候選池
       （我們只訂 cs.AI／cs.CL／cs.LG，HF 會收到 cs.CV、cs.RO 等分類的重要論文）
"""

from __future__ import annotations

from datetime import datetime, timezone

from fetchlib import normalize_url, polite_get

API = "https://huggingface.co/api/daily_papers"
PAGE_LIMIT = 100
# 上榜本身就是訊號（要有人送出、有人投票才會在清單上），所以門檻設得很低，
# 只擋掉剛送出還沒有人看的。真正的排名交給 score() 的 hf_bonus。
MIN_UPVOTES = 1


def paper_url(arxiv_id: str) -> str:
    return normalize_url(f"https://arxiv.org/abs/{arxiv_id}")


def fetch_papers(cutoff: datetime) -> tuple[list[dict], str]:
    """抓 HF Daily Papers。回傳 (清單, 錯誤訊息)。

    錯誤不拋出——HF 掛掉不該中斷整個流程，跟 hn.py 的處理方式一致。
    """
    try:
        resp = polite_get(f"{API}?limit={PAGE_LIMIT}", timeout=25)
        if not resp or resp.status_code != 200:
            return [], f"HTTP {resp.status_code if resp else '無回應'}"
        data = resp.json()
    except Exception as e:
        return [], f"{e.__class__.__name__}: {str(e)[:100]}"

    if not isinstance(data, list):
        return [], "回傳格式非預期（不是陣列）"

    out = []
    for row in data:
        paper = row.get("paper") or {}
        pid = (paper.get("id") or "").strip()
        title = (paper.get("title") or "").strip()
        if not pid or not title:
            continue
        upvotes = int(paper.get("upvotes") or 0)
        if upvotes < MIN_UPVOTES:
            continue

        # 時間用「上榜時間」而不是 arXiv 發表時間。日報的語意是「今天有什麼值得
        # 注意的事」，而 HF 常把幾天前的論文送上當日榜（實測今天的榜首發表於 8/28）。
        # 用發表時間會讓 48 小時的窗把整份榜單濾光——實測直接歸零。
        # merge_hn 對 HN 項目也是同樣的慣例：用投稿時間，不是原文發表時間。
        raw = (paper.get("submittedOnDailyAt")
               or row.get("publishedAt")
               or paper.get("publishedAt") or "")
        try:
            published = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if published < cutoff:
            continue

        out.append({
            "arxiv_id": pid,
            "url": paper_url(pid),
            "title": title,
            "summary": (paper.get("summary") or "").strip(),
            "upvotes": upvotes,
            "comments": int(row.get("numComments") or 0),
            "published_utc": published.isoformat(),
            "hf_url": f"https://huggingface.co/papers/{pid}",
        })
    return out, ""


def index_by_url(papers: list[dict]) -> dict[str, dict]:
    return {p["url"]: p for p in papers}
