"""第一層：收集器（純程式，不呼叫任何 LLM）。

流程：
    讀 sources_verified.yaml → 抓取 → 正規化 → 時間窗過濾 → AI 關鍵字過濾
    → 去重與跨來源分群 → 評分 → 輸出候選清單

用法：
    python collect.py                # 預設 30 小時內
    python collect.py --hours 48     # 放寬時間窗（補週末）
    python collect.py --top 30       # 目標則數

輸出：
    out/candidates.json   全部候選（含分數與分群資訊），翻譯階段只能讀這份
    out/run_report.json   每個來源的成敗與筆數（日報「本次未取得來源」的依據）

刻意不做的事：
    - 不呼叫 LLM、不生成任何文字。這一層只搬運事實。
    - 不丟棄失敗來源的紀錄。抓不到就記錄下來，不能讓它看起來像「今天沒新聞」。
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import yaml

import hfpapers
import hn
import scrape
from fetchlib import (entry_time, is_social, item_id, normalize_url, polite_get,
                      strip_html)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
VERIFIED = ROOT / "sources_verified.yaml"
SOURCES = ROOT / "sources.yaml"
OUT = ROOT / "out"
# 已發布清單。時間窗（預設 48 小時）刻意大於執行間隔（24 小時），
# 這樣某天執行失敗也不會漏掉新聞——代價是同一則會重複出現，靠這份清單擋掉。
# 由 render.py 在成功產生網頁後才寫入：翻譯失敗時不該把項目標記成已發布。
SEEN = ROOT / "state" / "seen.json"

# ai_filter=true 的綜合型來源要先過這層關鍵字才進入排序。
# 寧可寬鬆一點讓 LLM 階段再篩，也不要在這裡漏掉重要新聞。
#
# 注意：這裡刻意「不」放裸字 "ai"。比對是子字串比對，裸字 ai 會命中
# Sail、甲斐、Cairo 等任何含 ai 的字串，造成大量誤判（實測時 4Gamer 的
# 遊戲雜訊就是這樣混進來的）。單獨的 AI 一律交給下方 AI_WORD 詞界比對。
AI_KEYWORDS = [
    # 英文
    "a.i.", "artificial intelligence", "machine learning", "deep learning",
    "neural", "llm", "large language model", "generative", "gen ai", "genai",
    "transformer", "diffusion", "chatbot", "copilot", "agentic",
    "openai", "anthropic", "claude", "gemini", "deepmind", "gpt", "llama",
    "mistral", "hugging face", "inference", "fine-tun", "multimodal",
    "text-to-image", "text to image", "machine translation", "computer vision",
    # 模型家族名（2026-08-27 補）。只寫模型名而不寫「AI」的標題原本會被判為不相關，
    # 實測 HN 近 14 天有 11 則因此被擋在門外，且全部沒有從其他管道進來——
    # 包含 802 分的 Qwen 3.8 27B 評測、648 分的 Qwen3.8-Flash-Next、
    # 498 分的 DeepSeek-v4-flash-vision。qwen.ai 這個網域從來沒出現在任何一天的日報裡。
    "qwen", "deepseek", "gemma", "grok", "nemotron", "mixtral", "codestral",
    "midjourney", "hunyuan", "doubao", "zhipu", "modelscope",
    # 日文
    "人工知能", "機械学習", "深層学習", "生成ai", "大規模言語モデル", "ディープラーニング",
    "ニューラル", "チャットボット", "画像生成", "自動生成",
    # 中文（簡繁）
    "人工智慧", "人工智能", "機器學習", "机器学习", "深度學習", "深度学习",
    "大模型", "大語言模型", "大语言模型", "生成式", "生成式ai", "智慧體", "智能体",
    "神經網路", "神经网络", "多模態", "多模态", "微調", "微调",
    "通義千問", "通义千问", "文心一言", "豆包", "混元", "智譜", "智谱",
]
# 刻意排除的歧義詞（實測踩過）：
#   推理     中文＝inference，但日文＝推理／懸疑（「推理ADV」是推理冒險遊戲）
#   エージェント 日文遊戲裡＝特務角色；AI 語境會寫「AIエージェント」，靠 AI_WORD 就能命中
#   agent    英文＝密探；AI 語境保留 agentic
# 2026-08-27 補模型家族名時，逐一用陷阱字串測過而排除的（AI_PATTERN 是無邊界子字串比對）：
#   phi      命中 graphics／philosophy／sophisticated
#   yi       命中 playing／saying／buying，幾乎所有 -ying 結尾的字
#   wan      命中 Taiwan／want／swan
#   kimi     命中日文羅馬拼音的「君」（Kimi no Na wa），而來源含日文遊戲媒體
#   glm      命中 generalized linear model，統計文章會誤觸
#   ernie    命中 Bernie。中文的「文心一言」已涵蓋同一個模型
#   falcon   命中 Falcon 9
#   sora     命中 Kingdom Hearts 的 Sora，且日文常用字
#   whisper／flux／moonshot／veo／imagen 皆為常用字或過短
#   星火／盤古  命中「星火計畫」「盤古開天」等一般中文用語
AI_PATTERN = re.compile("|".join(re.escape(k) for k in AI_KEYWORDS), re.IGNORECASE)
# 單獨的 AI：前後不能是英文字母（排除 Sail / Air / Cairo），
# 但允許緊貼中日文字（生成AI、AI技術、AI活用 都要命中）
AI_WORD = re.compile(r"(?<![A-Za-z])AI(?![A-Za-z])")

TITLE_NOISE = re.compile(r"[\s\-–—_|:：、,，.。!！?？'\"“”‘’()（）\[\]【】]+")
SIMILARITY_THRESHOLD = 0.78  # 標題相似度達此值視為同一則新聞

# 每個來源最多貢獻幾則到候選池。arXiv 單日數百篇，不設上限會淹沒整池
# 並讓後續 LLM 排序成本暴增。
PER_SOURCE_CAP = 12
PAPER_SOURCE_CAP = 8
# 論文在收集階段幾乎不截斷：真正的排名交給 select()，那時 HN 熱度與跨分類
# 重複次數都已經算進 score。實測全部 668 篇進 cluster() 要 21 秒（CI 逾時 75 分鐘，
# 綽綽有餘），而且 668 篇會聚成 502 群——166 篇是跨分類重複，那正是原本
# 拿不到的訊號。留一個很寬的上限只是防 arXiv 某天異常暴衝。
PAPER_SOURCE_CAP_RAW = 600


# ────────────────────────────────────────────────────────────
# AI 相關性判定
# ────────────────────────────────────────────────────────────
def ai_hits(text: str) -> int:
    if not text:
        return 0
    return len(AI_PATTERN.findall(text)) + len(AI_WORD.findall(text))


def ai_relevance(title: str, summary: str) -> tuple[bool, int]:
    """回傳 (是否算 AI 相關, 命中強度)。

    綜合型來源要求：標題命中，或摘要至少命中兩次。
    只在摘要出現一次通常是順帶提及（例如遊戲新聞的側欄推薦），不算。
    """
    t, s = ai_hits(title), ai_hits(summary)
    return (t > 0 or s >= 2), t * 2 + s


# ────────────────────────────────────────────────────────────
# 抓取
# ────────────────────────────────────────────────────────────
# 純版號的標題：v0.1.0、b10481、0.32.14-rc0。這種標題本身不帶任何資訊，
# 若又抓不到內文，整則就是一個版號加一條連結，讀者看了等於沒看。
# 實測 llama.cpp 的 v0.1.0 就是這樣混進日報第 18 則的。
#
# 刻意不靠策展階段的 LLM 處理：這是可以用程式明確判定的條件，
# 而 HN 分數會讓它看起來像是有熱度的項目，反而不容易被剔除。
VERSION_ONLY = re.compile(r"^[a-zA-Z]?v?\d[\w.\-]*$")
MIN_SUBSTANCE = 80
# 社群貼文的熱度加成折扣。半價：夠熱的仍然進得來，一般的推文就擠不過真正的報導
SOCIAL_HN_DISCOUNT = 0.5
# 一般新聞的分數下限。權重 1.0 的來源就算全新也只有 1.5，等於規定
# 「沒有 HN 分數、也沒有跨來源佐證的普通來源不上稿」。
# 官方公告與論文不受此限（見 select()）
MIN_SCORE = 1.6


def is_contentless(title: str, summary: str) -> bool:
    """沒有內文，而且標題本身不帶資訊。"""
    if len((summary or "").strip()) >= MIN_SUBSTANCE:
        return False
    t = (title or "").strip()
    # 標題有實質內容就留著（例如「b10481: CUDA: MMVQ nwarps=8…」），
    # 交給策展階段判斷值不值得登。
    # 長度門檻壓到 6：中日文標題天生就短，「AI 教父辭職」只有 7 個字卻是完整資訊，
    # 門檻訂太高會誤殺整批中日文新聞
    return bool(VERSION_ONLY.match(t)) or len(t) < 6


def load_scrapers() -> list[dict]:
    """sources.yaml 的 no_feed 區裡標了 strategy: scraper 的來源。"""
    if not SOURCES.exists():
        return []
    entries = yaml.safe_load(SOURCES.read_text(encoding="utf-8")).get("no_feed") or []
    return [e for e in entries if e.get("strategy") == "scraper"]


def load_seen() -> dict[str, str]:
    """已發布過的項目 id → 發布日期。檔案不存在或壞掉都不該擋住當天的執行。"""
    if not SEEN.exists():
        return {}
    try:
        return json.loads(SEEN.read_text(encoding="utf-8")).get("ids", {})
    except (json.JSONDecodeError, OSError) as e:
        print(f"警告：{SEEN} 無法讀取（{e}），本次不排除已發布項目")
        return {}


def load_sources() -> list[dict]:
    """以 sources.yaml 為準（網址均已驗證過）。

    刻意不讀 sources_verified.yaml：那是某一次驗證的快照，
    一個偶爾閃斷的來源（例如遊戲陀螺曾逾時一次）不該因此被永久剔除。
    sources_verified.yaml 只當驗證報告用。
    """
    if not SOURCES.exists():
        sys.exit(f"找不到 {SOURCES}")
    sources = yaml.safe_load(SOURCES.read_text(encoding="utf-8"))["sources"]
    for src in sources:
        src["url"] = src["urls"][0]
        src.setdefault("timeout", None)
    return sources


def _fetch_feed(src: dict) -> tuple[object | None, str, str]:
    """依序嘗試 src["urls"]，回傳 (已解析的 feed, 實際成功的網址, 錯誤訊息)。

    候選清單原本只有 verify_feeds.py 在用，每天實際執行的收集只取第一個——
    於是第一個網址失敗就等於整個來源當天消失，即使備援網址是好的。
    實測 GitHub Actions 的資料中心 IP 會被 importai.substack.com 回 403，
    而 Jack Clark 自己網站的同一份內容完全正常。
    """
    errors = []
    for url in src["urls"]:
        try:
            resp = polite_get(url, timeout=src.get("timeout"))
        except Exception as e:
            errors.append(f"{url.split('/')[2]}: {e.__class__.__name__}")
            continue
        if resp.status_code != 200:
            errors.append(f"{url.split('/')[2]}: HTTP {resp.status_code}")
            continue
        parsed = feedparser.parse(resp.content)
        if not parsed.entries:
            errors.append(f"{url.split('/')[2]}: 解析不到項目")
            continue
        return parsed, url, ""
    return None, src["urls"][0], "；".join(errors[:3])


def fetch_source(src: dict, cutoff: datetime, cap: int | None = None) -> dict:
    """抓一個來源。任何失敗都只記錄，不拋出。

    cap 是單一來源的取用上限，預設值是為 48 小時的時間窗調的。
    回補歷史時（backfill.py）時間窗有好幾天，沿用同一個上限會讓每個來源
    只留最新的十二則，較早的日期因此餓死——那種情況要傳入更大的 cap。
    """
    report = {
        "name": src["name"], "url": src["url"], "lang": src["lang"], "cat": src["cat"],
        "status": "ok", "error": "", "entries": 0, "in_window": 0, "kept": 0,
    }
    items: list[dict] = []
    fetched_at = datetime.now(timezone.utc)

    parsed, used_url, error = _fetch_feed(src)
    report["url"] = used_url
    if parsed is None:
        report.update(status="fail", error=error or "無法取得")
        return {"report": report, "items": []}
    if used_url != src["urls"][0]:
        report["fallback"] = True

    report["entries"] = len(parsed.entries)
    if not parsed.entries:
        report.update(status="fail", error="解析不到項目")
        return {"report": report, "items": []}

    for entry in parsed.entries:
        link = (getattr(entry, "link", "") or "").strip()
        title = strip_html(getattr(entry, "title", ""), limit=300)
        if not link or not title:
            continue

        published, clamped, estimated = entry_time(entry, fallback=fetched_at)
        if published < cutoff:
            continue
        report["in_window"] += 1

        # 取較長的那個：summary 常常只是導言，content:encoded 才是全文。
        # 原本寫成 summary or content，等於永遠拿到短的那份。
        candidates = [getattr(entry, "summary", None) or ""]
        if getattr(entry, "content", None):
            candidates += [c.get("value") or "" for c in entry.content]
        summary = strip_html(max(candidates, key=len), limit=6000)

        relevant, strength = ai_relevance(title, summary)
        # 綜合型來源必須通過相關性判定；AI 專門來源直接放行
        if src.get("ai_filter") and not relevant:
            continue

        items.append({
            "ai_strength": strength,
            "id": item_id(link),
            "source": src["name"],
            "lang": src["lang"],
            "cat": src["cat"],
            "weight": float(src["weight"]),
            "ai_filter": bool(src.get("ai_filter")),
            "pinned": bool(src.get("pinned")),
            "title_original": title,
            "summary_original": summary,
            "url": normalize_url(link),
            "url_raw": link,
            "social": is_social(link),
            "published_utc": published.isoformat(),
            "time_clamped": clamped,      # feed 時區錯誤，時間被夾回現在
            "time_estimated": estimated,  # feed 無時間欄位，用抓取時間推估
            "fetched_at_utc": fetched_at.isoformat(),
        })

    # 每個來源設上限。排序鍵的選擇很要緊——2026-08-31 查出原本純用 published_utc
    # 由新到舊排序，對 arXiv 完全失效：當天 302 篇 cs.AI 的時間戳一模一樣
    # （都是 04:00:00，同一次 announce），排序等於沒作用，實際效果是取 feed 的
    # 前 8 筆，也就是隨機挑。綜合型來源（ai_filter）也一樣吃虧：4Gamer 一天
    # 被砍掉 99 則、Nikkei xTECH 砍掉 46 則，全是按新舊而不是按相關性。
    #
    # 現在：綜合型來源先看 AI 相關強度再看新舊；論文改為不在這裡截斷，
    # 留到 merge_hn 併入熱度、score() 算完分數之後再由 select() 排名，
    # 否則「訊號還沒套用就先砍掉九成」——一篇 300 分的論文若排在 feed 第 150 筆，
    # 在 HN 分數有機會貼上去之前就已經不存在了。
    limit = cap or (PAPER_SOURCE_CAP if src["cat"] == "paper" else PER_SOURCE_CAP)
    if src["cat"] == "paper" and cap is None:
        limit = PAPER_SOURCE_CAP_RAW
    items.sort(key=lambda i: i["published_utc"], reverse=True)
    if src.get("ai_filter"):
        # 兩段式排序：Python 的 sort 是穩定的，所以先排新舊、再排相關強度，
        # 同強度的項目就會保留「新的在前」。寫成單一 tuple 鍵會出錯——
        # published_utc 是字串，沒辦法在遞增排序裡表達「由新到舊」。
        items.sort(key=lambda i: -i["ai_strength"])
    if len(items) > limit:
        report["capped"] = len(items) - limit
        items = items[:limit]

    report["kept"] = len(items)
    return {"report": report, "items": items}


# ────────────────────────────────────────────────────────────
# Hacker News 熱度訊號
# ────────────────────────────────────────────────────────────
def merge_hn(items: list[dict], stories: list[dict], cutoff: datetime) -> dict:
    """把 HN 分數併入既有項目，並把 HN 上的 AI 新聞補進候選池。

    補進來的項目正好覆蓋我們沒有 RSS 的長尾（Anthropic、Roblox、Cohere…）。
    """
    index = hn.points_by_url(stories)
    matched = 0
    for item in items:
        record = index.get(item["url"])
        if record:
            item["hn_points"] = record["points"]
            item["hn_comments"] = record["comments"]
            item["hn_url"] = record["hn_url"]
            matched += 1

    known = {i["url"] for i in items}
    added = []
    for story in stories:
        if story["url"] in known:
            continue
        # HN 標題是英文，要求標題本身命中 AI 關鍵字（摘要不可得）
        relevant, strength = ai_relevance(story["title"], "")
        if not relevant:
            continue
        published = datetime.fromisoformat(story["created_at_utc"].replace("Z", "+00:00"))
        if published < cutoff:
            continue
        added.append({
            "ai_strength": strength,
            "id": item_id(story["url"]),
            "source": "Hacker News",
            "lang": "en",
            "cat": "forum",
            "weight": 1.3,
            "ai_filter": True,
            "pinned": False,
            "title_original": story["title"],
            "summary_original": "",  # HN 不提供摘要，翻譯階段只能用標題
            "url": story["url"],
            "url_raw": story["url"],
            "social": is_social(story["url"]),
            "published_utc": published.isoformat(),
            "time_clamped": False,
            "time_estimated": False,
            "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
            "hn_points": story["points"],
            "hn_comments": story["comments"],
            "hn_url": story["hn_url"],
        })

    return {"matched": matched, "added": added}


# ────────────────────────────────────────────────────────────
# Hugging Face Daily Papers：論文的重要性訊號
# ────────────────────────────────────────────────────────────
def merge_hf_papers(items: list[dict], papers: list[dict], cutoff: datetime) -> dict:
    """把 HF 投票數併入既有論文，並補進不在我們分類 feed 裡的論文。

    為什麼非有不可：同一次 arXiv announce 的數百篇論文時間戳與權重都相同，
    沒有外部訊號就排不出名次。原本規劃用 HN，但實測 48 小時內 HN 上的
    arXiv 投稿只有 1 則且對不上當天 feed——HN 討論論文是發表數天後的事。
    """
    index = hfpapers.index_by_url(papers)
    matched = 0
    for item in items:
        rec = index.get(item["url"])
        if rec:
            item["hf_upvotes"] = rec["upvotes"]
            item["hf_url"] = rec["hf_url"]
            matched += 1

    known = {i["url"] for i in items}
    added = []
    for rec in papers:
        if rec["url"] in known:
            continue
        # 我們只訂 cs.AI／cs.CL／cs.LG，HF 會收到 cs.CV、cs.RO 等分類的重要論文。
        # 這些補進來的正好是原本完全看不到的那一塊。
        added.append({
            "ai_strength": ai_relevance(rec["title"], rec["summary"])[1],
            "id": item_id(rec["url"]),
            "source": "Hugging Face Papers",
            "lang": "en",
            "cat": "paper",
            "weight": 1.0,
            "ai_filter": False,
            "pinned": False,
            "title_original": rec["title"],
            "summary_original": rec["summary"],
            "url": rec["url"],
            "url_raw": rec["url"],
            "social": False,
            "published_utc": rec["published_utc"],
            "time_clamped": False,
            "time_estimated": False,
            "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
            "hf_upvotes": rec["upvotes"],
            "hf_url": rec["hf_url"],
        })

    return {"matched": matched, "added": added}


# ────────────────────────────────────────────────────────────
# 去重與跨來源分群
# ────────────────────────────────────────────────────────────
def title_key(title: str) -> str:
    return TITLE_NOISE.sub("", title).lower()


def cluster(items: list[dict]) -> list[dict]:
    """先用網址去重，再用標題相似度把同一則新聞分群。

    已知限制：跨語言的同一則新聞（英文原文 vs 日文報導）標題不相似，
    這一層抓不到，留給 LLM 策展階段合併。
    """
    by_url: dict[str, dict] = {}
    for item in items:
        existing = by_url.get(item["url"])
        # 同網址取權重高的來源版本
        if existing is None or item["weight"] > existing["weight"]:
            by_url[item["url"]] = item
    unique = sorted(by_url.values(), key=lambda i: i["published_utc"])

    clusters: list[list[dict]] = []
    keys: list[str] = []
    for item in unique:
        key = title_key(item["title_original"])
        placed = False
        for idx, existing_key in enumerate(keys):
            # 同語言才比標題；不同語言比不出來
            if clusters[idx][0]["lang"] != item["lang"]:
                continue
            if difflib.SequenceMatcher(None, existing_key, key).ratio() >= SIMILARITY_THRESHOLD:
                clusters[idx].append(item)
                placed = True
                break
        if not placed:
            clusters.append([item])
            keys.append(key)

    result = []
    for group in clusters:
        group.sort(key=lambda i: (-i["weight"], i["published_utc"]))
        lead = dict(group[0])
        lead["cross_source_count"] = len({i["source"] for i in group})
        # HN 分數可能落在群組裡的另一則（例如英文原文），取全組最高
        lead["hn_points"] = max(i.get("hn_points", 0) for i in group)
        lead["hn_comments"] = max(i.get("hn_comments", 0) for i in group)
        lead["hn_url"] = next((i["hn_url"] for i in group if i.get("hn_url")), "")
        lead["also_reported_by"] = [
            {"source": i["source"], "url": i["url"], "title_original": i["title_original"]}
            for i in group[1:]
        ]
        result.append(lead)
    return result


# ────────────────────────────────────────────────────────────
# 評分
# ────────────────────────────────────────────────────────────
def score(item: dict, now: datetime, window_hours: int) -> dict:
    # 綜合型來源（尤其遊戲媒體）的加權必須落在「AI × 該領域」的交集，
    # 而不是整個來源。相關性弱的項目只拿到接近基準的權重，
    # 否則遊戲媒體的一般遊戲新聞會靠 ×2 加權壓過真正的 AI 新聞。
    if item["ai_filter"] and item["weight"] > 1.0:
        factor = min(1.0, item.get("ai_strength", 0) / 4)
        effective_weight = 1.0 + (item["weight"] - 1.0) * factor
    else:
        effective_weight = item["weight"]

    age_h = (now - datetime.fromisoformat(item["published_utc"])).total_seconds() / 3600
    recency = max(0.0, 1 - age_h / window_hours) * 0.5
    cross = 0.8 * (item["cross_source_count"] - 1)  # 多家獨立報導 = 最可靠的熱度訊號
    penalty = -0.3 if item["time_estimated"] else 0.0  # 時間靠推估的降權

    # HN 分數：用平方根壓縮，避免單一爆紅文章分數失控
    # （50 分 → 0.35、200 分 → 0.71、800 分 → 1.41，上限 1.6）
    points = item.get("hn_points", 0)
    hn_bonus = min(1.6, (points / 400) ** 0.5) if points else 0.0
    # 社群貼文抓不到內文，讀者拿到的往往只有一句話。熱度打折而不是排除：
    # 真正重大的宣布（常首發在 X）分數夠高，還是擠得進來
    if item.get("social"):
        hn_bonus *= SOCIAL_HN_DISCOUNT

    item["effective_weight"] = round(effective_weight, 3)
    item["score_breakdown"] = {
        "source_weight": round(item["weight"], 2),
        "effective_weight": round(effective_weight, 2),
        "ai_strength": item.get("ai_strength", 0),
        "cross_source_bonus": round(cross, 2),
        "hn_bonus": round(hn_bonus, 2),
        "hn_points": points,
        "social": bool(item.get("social")),
        "recency_bonus": round(recency, 2),
        "estimated_time_penalty": penalty,
    }
    # HF 投票數的量級跟 HN 分數差很多（個位數到數十 vs 數十到數百），
    # 所以用自己的曲線：10 票 → 0.63、30 票 → 1.09，上限 1.4。
    upvotes = item.get("hf_upvotes", 0)
    hf_bonus = min(1.4, (upvotes / 25) ** 0.5) if upvotes else 0.0
    item["score_breakdown"]["hf_bonus"] = round(hf_bonus, 2)
    item["score_breakdown"]["hf_upvotes"] = upvotes
    item["score"] = round(
        effective_weight + cross + hn_bonus + hf_bonus + recency + penalty, 3)
    return item


def select(
    items: list[dict], target: int, max_pinned: int, max_papers: int,
    max_per_source: int, min_score: float = 0.0
) -> dict:
    """論文獨立成區，不與新聞在同一個池子裡競爭。

    論文的排序訊號很弱（沒有點閱、沒有跨來源提及），塞進主排名只會排擠新聞。
    等 Hacker News 接進來後，論文改成「必須有外部提及才收錄」。
    """
    # 「必須有外部提及才收錄」——這是本函式 docstring 從一開始就寫著的規劃，
    # 但一直沒有實作，而且 candidates.json 的 papers 這個鍵根本沒有人讀
    # （translate.py 只讀 official + ranked）。結果是 arXiv 每天抓數百篇、
    # 一篇都沒有上過稿：2026-08-31 回頭查 13 天 356 則，來源是 arXiv 的有 0 則。
    #
    # 沒有外部訊號的論文無法排名：同一天所有論文的權重相同、時間戳也相同，
    # 硬排出來的前五名就是隨機五篇。寧可當天沒有論文，也不要放五篇隨機的。
    # 有 HN 討論的論文才收——那是唯一拿得到的當日重要性訊號。
    paper_pool = [i for i in items if i["cat"] == "paper"]
    papers = sorted(
        (i for i in paper_pool
         if i.get("hn_points", 0) > 0 or i.get("hf_upvotes", 0) > 0),
        key=lambda i: (-i["score"], i["published_utc"]),
    )[:max_papers]
    news = [i for i in items if i["cat"] != "paper"]

    pinned = sorted(
        (i for i in news if i["pinned"]), key=lambda i: (-i["score"], i["published_utc"])
    )
    pinned_selected = pinned[:max_pinned]
    pinned_ids = {i["id"] for i in pinned_selected}

    # 分數下限只套用在一般新聞。官方公告是日報的骨幹，論文的分數訊號天生就弱
    # （權重 0.8–0.9，加滿新鮮度也構不到 1.6），兩者一併套用會被整批清空。
    # 目的不是湊滿 target，而是寧可當天少幾則，也不要拿沒有任何外部訊號的
    # 一般來源硬湊——來源薄的日子本來就該看起來比較薄
    contenders = [i for i in news if i["id"] not in pinned_ids]
    below_floor = [i for i in contenders if i["score"] < min_score]
    rest = sorted(
        (i for i in contenders if i["score"] >= min_score),
        key=lambda i: (-i["score"], i["published_utc"]),
    )

    # 每個來源在最終名單裡最多佔幾席。沒有這道限制，名單會變成
    # 「最會發文的來源前 30 則」而不是「最熱門 30 則」——
    # 實測時 ITmedia AI+ 一家就佔了前 15 名的 5 席。
    slots = max(0, target - len(pinned_selected))
    ranked, per_source, deferred = [], defaultdict(int), []
    for item in rest:
        if len(ranked) >= slots:
            break
        if per_source[item["source"]] >= max_per_source:
            deferred.append(item)
            continue
        ranked.append(item)
        per_source[item["source"]] += 1
    # 名額沒填滿才回頭補被壓下的項目（寧可同來源多篇，也不要湊不到 30 則）
    for item in deferred:
        if len(ranked) >= slots:
            break
        ranked.append(item)

    return {
        "official": pinned_selected,
        "ranked": ranked,
        "papers": papers,
        # 觀測用：論文池有多少、其中有外部訊號的有多少。
        # 「池子很大但有訊號的是 0」正是 2026-08-31 之前的長期狀態，
        # 這兩個數字讓它下次一眼就看得出來。
        "papers_pool_size": len(paper_pool),
        "papers_with_signal": sum(
            1 for i in paper_pool
            if i.get("hn_points", 0) > 0 or i.get("hf_upvotes", 0) > 0),
        "overflow_pinned": pinned[max_pinned:],
        "below_floor": below_floor,
        "source_distribution": dict(sorted(per_source.items(), key=lambda kv: -kv[1])),
    }


# ────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser()
    # 48 小時：官方部落格週末不發文，30 小時的窗在週一會漏掉整個週末
    ap.add_argument("--hours", type=int, default=48, help="時間窗（小時），預設 48")
    ap.add_argument("--top", type=int, default=30, help="目標則數，預設 30")
    ap.add_argument("--max-pinned", type=int, default=12, help="官方公告最多佔幾則")
    ap.add_argument("--max-papers", type=int, default=5, help="論文區最多幾則")
    ap.add_argument("--no-hf-papers", action="store_true",
                    help="跳過 Hugging Face Daily Papers（除錯用）")
    ap.add_argument("--max-per-source", type=int, default=3, help="單一來源在名單裡最多佔幾席")
    ap.add_argument("--min-score", type=float, default=MIN_SCORE,
                    help=f"一般新聞的分數下限，預設 {MIN_SCORE}（官方公告與論文不受限）；0 表示不設限")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--no-hn", action="store_true", help="跳過 Hacker News（除錯用）")
    ap.add_argument("--ignore-seen", action="store_true", help="不排除前幾天已發布過的項目（重跑用）")
    ap.add_argument("--no-scrape", action="store_true", help="跳過無 RSS 來源的爬蟲（除錯用）")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=args.hours)
    sources = load_sources()
    scrapers = [] if args.no_scrape else load_scrapers()
    print(
        f"來源 {len(sources)} 個（另有 {len(scrapers)} 個無 RSS、改用爬蟲），"
        f"時間窗 {args.hours} 小時（{cutoff.isoformat()} 之後）\n"
    )

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda s: fetch_source(s, cutoff), sources))

    # 爬蟲逐一執行：每個來源內部已經併發抓文章頁，外層再併發會打太兇
    for src in scrapers:
        results.append(scrape.fetch_scraped(src, cutoff))

    reports = [r["report"] for r in results]
    raw_items = [i for r in results for i in r["items"]]

    # Hacker News：熱度訊號 + 補上沒有 RSS 的長尾來源
    hn_info = {"matched": 0, "added": [], "stories": 0, "error": ""}
    if not args.no_hn:
        stories, err = hn.fetch_stories(cutoff)
        hn_info["stories"], hn_info["error"] = len(stories), err
        if err:
            reports.append({
                "name": "Hacker News", "url": hn.API, "lang": "en", "cat": "forum",
                "status": "fail", "error": err, "entries": len(stories),
                "in_window": 0, "kept": 0,
            })
        merged = merge_hn(raw_items, stories, cutoff)
        hn_info.update(matched=merged["matched"], added=merged["added"])
        raw_items.extend(merged["added"])

    # Hugging Face Daily Papers：論文的當日重要性訊號。
    # 位置一定要在 cluster()／score() 之前，跟 merge_hn 同一段——
    # 訊號沒有先貼上去，後面的排名就無從談起。
    hf_info = {"matched": 0, "added": [], "papers": 0, "error": ""}
    if not args.no_hf_papers:
        hf_list, hf_err = hfpapers.fetch_papers(cutoff)
        hf_info["papers"], hf_info["error"] = len(hf_list), hf_err
        if hf_err:
            reports.append({
                "name": "Hugging Face Papers", "url": hfpapers.API, "lang": "en",
                "cat": "paper", "status": "fail", "error": hf_err,
                "entries": len(hf_list), "in_window": 0, "kept": 0,
            })
        merged_hf = merge_hf_papers(raw_items, hf_list, cutoff)
        hf_info.update(matched=merged_hf["matched"], added=merged_hf["added"])
        raw_items.extend(merged_hf["added"])

    contentless = [i for i in raw_items
                   if is_contentless(i["title_original"], i.get("summary_original", ""))]
    if contentless:
        blank = {i["id"] for i in contentless}
        raw_items = [i for i in raw_items if i["id"] not in blank]

    clustered = cluster(raw_items)

    seen = {} if args.ignore_seen else load_seen()
    fresh = [i for i in clustered if i["id"] not in seen]
    repeats = len(clustered) - len(fresh)

    scored = [score(i, now, args.hours) for i in fresh]
    selection = select(
        scored, args.top, args.max_pinned, args.max_papers, args.max_per_source,
        min_score=args.min_score,
    )

    # ── 終端摘要 ──
    failures = [r for r in reports if r["status"] != "ok"]
    ok = [r for r in reports if r["status"] == "ok"]
    empty = [r for r in ok if r["kept"] == 0]

    print("每個來源取得則數（時間窗內 → AI 過濾後 → 上限截斷後）：")
    for r in sorted(ok, key=lambda r: -r["kept"]):
        if r["kept"]:
            capped = f"（截斷 {r['capped']} 則）" if r.get("capped") else ""
            print(f"  {r['name']:<24} {r['in_window']:>3} → {r['kept']:>3} {capped}")
    if empty:
        print(f"\n時間窗內無新項目（{len(empty)} 個）：" + "、".join(r["name"] for r in empty))
    fell_back = [r for r in ok if r.get("fallback")]
    if fell_back:
        print(f"\n改用備援網址（{len(fell_back)} 個，主網址失敗）：")
        for r in fell_back:
            print(f"  {r['name']:<24} {r['url']}")
    if failures:
        print(f"\n抓取失敗（{len(failures)} 個，日報必須標注）：")
        for r in failures:
            print(f"  {r['name']:<24} {r['error']}")

    if not args.no_hn:
        if hn_info["error"]:
            print(f"\nHacker News 抓取失敗：{hn_info['error']}（熱度訊號缺失，日報需標注）")
        else:
            print(
                f"\nHacker News：{hn_info['stories']} 則故事"
                f"（{hn.MIN_POINTS} 分以上）→ 對上既有項目 {hn_info['matched']} 則"
                f"、補入新項目 {len(hn_info['added'])} 則"
            )

    if not args.no_hf_papers:
        if hf_info["error"]:
            print(f"HF Daily Papers 抓取失敗：{hf_info['error']}"
                  f"（論文排不出名次，當天論文區會是空的）")
        else:
            print(f"HF Daily Papers：{hf_info['papers']} 篇 → 對上既有項目 "
                  f"{hf_info['matched']} 篇、補入新項目 {len(hf_info['added'])} 篇")

    clamped = sum(1 for i in raw_items if i["time_clamped"])
    estimated = sum(1 for i in raw_items if i["time_estimated"])
    multi = [i for i in scored if i["cross_source_count"] > 1]

    print(
        f"\n原始項目 {len(raw_items) + len(contentless)}"
        f" → 剔除無內容 {len(contentless)} 則 → 去重分群後 {len(clustered)}"
        f" → 排除前幾天已發布 {repeats} 則 → 候選 {len(scored)}"
        f"（其中 {len(multi)} 則有多家報導）"
    )
    if contentless:
        print("  無內容（只有版號或極短標題，且抓不到內文）："
              + "、".join(f"{i['source']} {i['title_original'][:20]}" for i in contentless[:6]))
    if clamped or estimated:
        print(f"時間修正：夾回現在 {clamped} 則、無時間欄位改用抓取時間 {estimated} 則")
    low = selection["below_floor"]
    if low:
        print(f"低於分數下限 {args.min_score} 而未列入 {len(low)} 則："
              + "、".join(f"{i['source']} {i['score']}" for i in
                          sorted(low, key=lambda i: -i["score"])[:6]))
    social = [i for i in scored if i.get("social")]
    if social:
        print(f"社群貼文 {len(social)} 則（熱度加成已乘 {SOCIAL_HN_DISCOUNT}）")

    print(
        f"\n選入：官方公告 {len(selection['official'])} 則"
        f" + 熱門 {len(selection['ranked'])} 則"
        f" + 論文 {len(selection['papers'])} 則（獨立區）"
    )
    if selection["overflow_pinned"]:
        print(f"（官方公告超出上限，{len(selection['overflow_pinned'])} 則未選入）")
    dist = selection["source_distribution"]
    print("來源分布：" + "、".join(f"{k} {v}" for k, v in list(dist.items())[:12]))

    print("\n── 前 15 名預覽 ──")
    preview = selection["official"] + selection["ranked"]
    for n, i in enumerate(preview[:15], 1):
        tag = "[官方]" if i["pinned"] else f"[{i['cross_source_count']}家]"
        pts = f"HN{i['hn_points']:>4}" if i.get("hn_points") else "      "
        print(
            f"{n:>2}. {tag} {i['score']:>5.2f} {pts} "
            f"{i['source'][:16]:<16} {i['title_original'][:44]}"
        )

    # ── 輸出 ──
    OUT.mkdir(exist_ok=True)
    (OUT / "candidates.json").write_text(
        json.dumps(
            {
                "generated_at_utc": now.isoformat(),
                "window_hours": args.hours,
                "official": selection["official"],
                "ranked": selection["ranked"],
                "papers": selection["papers"],
                "all_scored": scored,
            },
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    (OUT / "run_report.json").write_text(
        json.dumps(
            {
                "generated_at_utc": now.isoformat(),
                "window_hours": args.hours,
                "sources_total": len(sources) + len(scrapers),
                "sources_ok": len(ok),
                "sources_failed": len(failures),
                "raw_items": len(raw_items),
                "contentless_dropped": len(contentless),
                "clustered_items": len(clustered),
                "already_published": repeats,
                "candidate_items": len(scored),
                # 論文管線的觀測值。「池子很大但有訊號的是 0」正是 2026-08-31
                # 之前的長期狀態（13 天 356 則，來源是 arXiv 的 0 則），
                # 這幾個數字進版控的 state/last-report.json，讓它下次一眼看得出來。
                "papers_pool_size": selection["papers_pool_size"],
                "papers_with_signal": selection["papers_with_signal"],
                "papers_selected": len(selection["papers"]),
                "hf_papers": {k: (len(v) if isinstance(v, list) else v)
                              for k, v in hf_info.items()},
                "time_clamped": clamped,
                "time_estimated": estimated,
                "per_source": reports,
                "failures": failures,
            },
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n輸出：{OUT / 'candidates.json'}")
    print(f"      {OUT / 'run_report.json'}")


if __name__ == "__main__":
    main()
