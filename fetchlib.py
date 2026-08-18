"""共用的抓取與正規化工具。

設計原則：
  - 任何單一來源失敗都不中斷流程，失敗原因一律回報（日報要列出「本次未取得來源」）
  - 同網域請求序列化並加間隔，避免被限流
  - 時間戳一律轉成 UTC；未來時間夾回現在（部分 feed 時區標記錯誤）
"""

from __future__ import annotations

import hashlib
import html
import re
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

# 聯絡方式用 repo 網址而不是個人信箱：這個字串會送給每一個被抓取的網站，
# 而且 repo 是公開的。站方要反映抓取問題，開 issue 一樣找得到人。
USER_AGENT = "ai-daily-digest/0.4 (+https://github.com/girlmagician/ai-daily-digest)"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    "Accept-Language": "en,ja;q=0.8,zh;q=0.8",
}
DEFAULT_TIMEOUT = 20

# 每個網域的最小請求間隔（秒）
DOMAIN_INTERVAL = defaultdict(
    lambda: 0.4,
    {"www.reddit.com": 3.0, "old.reddit.com": 3.0, "rsshub.app": 2.0},
)
_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)
_last: dict[str, float] = defaultdict(float)

# 追蹤參數，去重時必須先剝掉，否則同一篇文章會被當成兩篇
TRACKING_PARAMS = re.compile(
    r"^(utm_|ref_|mc_|pk_|_hs|hsa_)|^(fbclid|gclid|igshid|ref|source|src|share_id|__twitter_impression)$"
)
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def polite_get(url: str, timeout: int | None = None, retries: int = 1) -> requests.Response:
    """同網域序列化 + 間隔；遇 429 依 Retry-After 重試。"""
    host = urlparse(url).netloc
    timeout = timeout or DEFAULT_TIMEOUT
    resp = None
    for attempt in range(retries + 1):
        with _locks[host]:
            wait = DOMAIN_INTERVAL[host] - (time.monotonic() - _last[host])
            if wait > 0:
                time.sleep(wait)
            resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
            _last[host] = time.monotonic()

        if resp.status_code != 429 or attempt == retries:
            return resp
        try:
            delay = min(float(resp.headers.get("Retry-After", "5")), 30.0)
        except ValueError:
            delay = 5.0
        time.sleep(delay)
    return resp


def normalize_url(url: str) -> str:
    """剝掉追蹤參數、統一大小寫與尾斜線，作為去重主鍵的依據。"""
    try:
        p = urlparse(url.strip())
    except ValueError:
        return url.strip()
    query = urlencode(
        [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not TRACKING_PARAMS.match(k)]
    )
    path = p.path.rstrip("/") or "/"
    return urlunparse((p.scheme.lower(), p.netloc.lower(), path, "", query, ""))


def item_id(url: str) -> str:
    return hashlib.sha1(normalize_url(url).encode("utf-8")).hexdigest()[:16]


def strip_html(text: str | None, limit: int = 800) -> str:
    """把 feed 的 HTML 摘要轉成純文字。翻譯階段只能改寫這段文字，不得自行補充。"""
    if not text:
        return ""
    clean = _WS.sub(" ", html.unescape(_TAG.sub(" ", text))).strip()
    return clean[:limit]


# 抽取器在某些頁面上會抽到樣板文字而不是內文，長度剛好又過門檻，
# 於是垃圾被當成「原文全文」送進翻譯。實測 GitHub 的 release tag 頁面
# 會抽出 237 字的登入提示（"You signed in with another tab or window…"）。
_BOILERPLATE = re.compile(
    r"you signed (in|out) with another tab"
    r"|reload to refresh your session"
    r"|enable javascript (and cookies )?to continue"
    r"|please enable (js|javascript)"
    r"|access denied|are you a robot|checking your browser"
    r"|subscribe to (continue|read)|this content is for subscribers"
    r"|我們使用 cookie|本網站使用 cookie",
    re.IGNORECASE,
)

# 真正的文章不會這麼短。低於這個長度的「內文」多半是樣板或導覽列殘渣。
MIN_ARTICLE_CHARS = 400


def looks_like_content(text: str | None) -> bool:
    """抽出來的文字看起來像不像真的文章內文。"""
    if not text:
        return False
    t = text.strip()
    if len(t) < MIN_ARTICLE_CHARS:
        return False
    # 樣板句出現在開頭代表整段都是樣板；出現在文末通常只是頁尾，不算數
    return not _BOILERPLATE.search(t[:300])


def to_utc(parsed_time, fallback: datetime | None = None) -> tuple[datetime, bool]:
    """time.struct_time → UTC datetime。

    回傳 (時間, 是否被夾回現在)。
    未來時間一律夾回現在——遊戲陀螺、iThome 的 feed 時區標記錯誤，
    不夾的話它們會永遠排在最新。
    """
    now = datetime.now(timezone.utc)
    if parsed_time:
        dt = datetime(*parsed_time[:6], tzinfo=timezone.utc)
    elif fallback:
        dt = fallback
    else:
        return now, False
    if dt > now:
        return now, True
    return dt, False


def entry_time(entry, fallback: datetime | None = None) -> tuple[datetime, bool, bool]:
    """回傳 (時間, 是否夾回現在, 是否為推估時間)。"""
    for key in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, key, None)
        if parsed:
            dt, clamped = to_utc(parsed)
            return dt, clamped, False
    dt, _ = to_utc(None, fallback)
    return dt, False, True  # 沒有時間欄位 → 用抓取時間，標記為推估
