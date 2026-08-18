"""第三層：把 digest.json 產生成可分享的靜態網頁。

設計原則是「可查證」：每一則都把原文標題、來源、發布時間、原始連結攤開，
讀者不必相信譯文，點下去就能對照。頁尾誠實列出本次沒抓到的來源——
少了什麼比多了什麼更難察覺，所以要主動講。

產出：
    docs/index.html                今日日報（GitHub Pages 首頁）
    docs/YYYY-MM-DD.html           當日存檔
    docs/archive.html              歷史索引
    docs/feed.xml                  RSS，方便用閱讀器訂閱

沒有任何外部資源（CSS/字型/JS 全部內嵌），因此離線可讀、載入無追蹤。

用法：
    python render.py              # 讀 out/digest.json（translate.py 的產出）
    python render.py --replay     # 讀 state/last-digest.json，重畫最後一次發布的內容

--replay 是給調版面用的：譯好的內容會留一份在 state/ 並進版控，
所以改 CSS 或排版後可以立刻看到真實內容的效果，不必為了看版面
重跑一次要價十一分鐘、約 US$0.8 的完整流程。
"""

from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent
OUT = ROOT / "out"
DOCS = ROOT / "docs"
DIGEST = OUT / "digest.json"
REPORT = OUT / "run_report.json"
SEEN = ROOT / "state" / "seen.json"
SEEN_DAYS = 21          # 保留三週；時間窗只有兩天，這已經非常寬裕
# 最後一次發布的內容與執行報告，進版控供 --replay 重畫版面用
LAST_DIGEST = ROOT / "state" / "last-digest.json"
LAST_REPORT = ROOT / "state" / "last-report.json"

TPE = timezone(timedelta(hours=8))
SITE_TITLE = "AI 情報日報"
SITE_DESC = "每日自動彙整海外 AI 情報，翻譯成繁體中文並附上原始連結"

GROUP_ORDER = ["模型與產品發布", "遊戲與娛樂", "產業與資本", "研究與論文", "工程與工具", "政策與法規", "其他"]
LANG_LABEL = {"en": "英", "ja": "日", "zh-CN": "簡中", "zh-TW": "繁中", "ko": "韓", "fr": "法", "de": "德"}

CSS = """
:root{--bg:#fbfbfa;--card:#fff;--fg:#1a1a18;--dim:#6b6b66;--line:#e6e5e0;
--accent:#8a5a2b;--accent-bg:#f3ece3;--hot:#c0442e;--barbg:#fbfbfaf2}
@media(prefers-color-scheme:dark){:root{--bg:#16161a;--card:#1e1e23;--fg:#e8e8e4;
--dim:#9a9a94;--line:#31313a;--accent:#d9a066;--accent-bg:#2a2620;--hot:#e0705a;
--barbg:#16161af2}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font-family:-apple-system,"Segoe UI","Noto Sans TC","Microsoft JhengHei",sans-serif;
line-height:1.75;font-size:16px}
.wrap{max-width:1280px;margin:0 auto;padding:24px 18px 64px}
header{border-bottom:2px solid var(--line);padding-bottom:14px}
h1{font-size:1.6rem;margin:0 0 4px}
h1 a{color:inherit;text-decoration:none}
.sub{color:var(--dim);font-size:.85rem;margin:0}

/* 近 7 日快速切換 */
.days{margin:12px 0 0;font-size:.85rem;display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.days .lbl{color:var(--dim);margin-right:2px}
.days a{color:var(--accent);text-decoration:none;border:1px solid var(--line);
border-radius:6px;padding:2px 9px;font-variant-numeric:tabular-nums}
.days a:hover{background:var(--accent-bg)}
.days .now{background:var(--accent);color:var(--bg);border-color:var(--accent);
padding:2px 9px;border-radius:6px;font-variant-numeric:tabular-nums}
.days .more{border-style:dashed}

/* 分類篩選器：捲動時固定在頂端 */
.groups{position:sticky;top:0;z-index:10;background:var(--barbg);
backdrop-filter:blur(8px);border-bottom:1px solid var(--line);
margin:0 -18px 4px;padding:9px 18px;display:flex;flex-wrap:wrap;gap:7px;
align-items:center;font-size:.85rem}
.groups a{color:var(--fg);text-decoration:none;border:1px solid var(--line);
border-radius:999px;padding:3px 12px;white-space:nowrap}
.groups a:hover{border-color:var(--accent);color:var(--accent);background:var(--accent-bg)}
.groups a b{color:var(--dim);font-weight:400;margin-left:5px;font-size:.78rem}
.groups a:hover b{color:var(--accent)}
.groups .top{margin-left:auto;border-style:dashed;color:var(--dim)}

/* 錨點跳轉時標題不要被固定列遮住 */
h2{font-size:1.05rem;margin:34px 0 4px;padding-top:12px;border-top:1px solid var(--line);
color:var(--accent);letter-spacing:.02em;scroll-margin-top:56px}
/* 兩欄卡片。用 grid 而不是 CSS columns：grid 是先左後右（1,2／3,4），
   閱讀順序與排名一致；columns 會變成整列往下（1,2,3 在左欄），排名就讀不出來了。
   align-items:start 讓卡片保持自然高度，不被同列的長卡片拉長。
   卡片本身就把行寬限制在約 38 個中文字，所以文字不需要再另外設 max-width。 */
.grid{display:grid;grid-template-columns:repeat(2,1fr);gap:14px;align-items:start;
margin:12px 0}
article{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:14px 18px;margin:0}
.t{font-size:1.06rem;font-weight:600;margin:0 0 6px;line-height:1.5}
.t a{color:var(--fg);text-decoration:none}
.t a:hover{color:var(--accent);text-decoration:underline}
.n{color:var(--dim);font-variant-numeric:tabular-nums;margin-right:6px;font-weight:400}
.s{margin:6px 0 10px;font-size:.95rem}
.orig{color:var(--dim);font-size:.82rem;margin:6px 0;
overflow-wrap:anywhere;border-left:2px solid var(--line);padding-left:10px}
.meta{font-size:.78rem;color:var(--dim);display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.tag{background:var(--accent-bg);color:var(--accent);border-radius:4px;padding:1px 7px;font-size:.72rem}
.hot{color:var(--hot);font-weight:600;text-decoration:none}
.meta a{color:var(--dim)}
.src{color:var(--dim);text-decoration:none;overflow-wrap:anywhere}
footer{margin-top:48px;padding-top:16px;border-top:1px solid var(--line);
font-size:.8rem;color:var(--dim)}
footer h3{font-size:.85rem;margin:18px 0 6px;color:var(--fg)}
footer ul{margin:4px 0;padding-left:20px}
.note{background:var(--accent-bg);border-radius:8px;padding:10px 14px;
font-size:.82rem;margin:14px 0;max-width:68em}
ul.arc{list-style:none;padding:0}
ul.arc li{padding:8px 0;border-bottom:1px solid var(--line)}
ul.arc a{color:var(--accent);text-decoration:none;font-weight:600}
/* 視窗不夠寬時收成一欄：兩欄各 450px 以下，卡片會擠得難讀 */
@media(max-width:900px){.grid{grid-template-columns:1fr}}
@media(max-width:640px){
  .groups{margin:0 -14px 4px;padding:8px 14px;gap:5px;font-size:.8rem}
  .wrap{padding:18px 14px 56px}
}
"""


def esc(t: str) -> str:
    return html.escape(t or "", quote=True)


def tpe_str(iso: str, fmt: str = "%m/%d %H:%M") -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TPE).strftime(fmt)
    except ValueError:
        return ""


def domain(url: str) -> str:
    m = re.match(r"https?://(?:www\.)?([^/]+)", url or "")
    return m.group(1) if m else ""


def page(title: str, body: str, desc: str = SITE_DESC) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(desc)}">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(desc)}">
<meta property="og:type" content="website">
<link rel="alternate" type="application/rss+xml" title="{esc(SITE_TITLE)}" href="feed.xml">
<style>{CSS}</style>
</head>
<body><div class="wrap">{body}</div></body>
</html>
"""


def render_item(it: dict, n: int) -> str:
    url = it["url"]
    parts = [f'<article><p class="t"><span class="n">{n}.</span>'
             f'<a href="{esc(url)}" target="_blank" rel="noopener">{esc(it["title_zh"])}</a></p>']

    if it["summary_zh"].strip():
        parts.append(f'<p class="s">{esc(it["summary_zh"])}</p>')

    # 原文標題一律附上：譯文有疑慮時讀者可以直接對照
    parts.append(f'<p class="orig">原文：{esc(it["title_original"])}</p>')

    meta = [f'<span class="tag">{esc(LANG_LABEL.get(it["lang"], it["lang"]))}</span>',
            f'<a class="src" href="{esc(url)}" target="_blank" rel="noopener">'
            f'{esc(it["source"])}｜{esc(domain(url))}</a>']

    published = tpe_str(it.get("published_utc", ""))
    if published:
        mark = "（時間存疑）" if it.get("time_clamped") else ""
        meta.append(f'<span>{published}{mark}</span>')
    if it.get("hn_points"):
        hn = it.get("hn_url") or url
        meta.append(f'<a class="hot" href="{esc(hn)}" target="_blank" rel="noopener">'
                    f'HN {it["hn_points"]}分</a>')
    if it.get("cross_source_count", 1) > 1:
        # 其他家的連結也附上，跨來源比對是查證最有效的方式
        also = "、".join(
            f'<a href="{esc(a.get("url", ""))}" target="_blank" rel="noopener">'
            f'{esc(a.get("source", ""))}</a>'
            for a in it.get("also_reported_by", [])[:4] if a.get("url")
        )
        meta.append(f'<span>{it["cross_source_count"]}家報導{f"（另見 {also}）" if also else ""}</span>')
    if it.get("kind") == "僅標題":
        meta.append('<span>僅標題（原始來源未提供摘要）</span>')

    parts.append(f'<p class="meta">{"".join(meta)}</p></article>')
    return "".join(parts)


DAYS_SHOWN = 7


def render_days(dates: list[str], current: str) -> str:
    """近 7 日快速切換。更早的日期留給歷史存檔頁，否則列一整年會太長。"""
    parts = ['<nav class="days"><span class="lbl">近 7 日</span>']
    for d in dates[:DAYS_SHOWN]:
        label = f"{d[5:7]}/{d[8:10]}"
        if d == current:
            parts.append(f'<span class="now">{label}</span>')
        else:
            parts.append(f'<a href="{d}.html">{label}</a>')
    if len(dates) > DAYS_SHOWN:
        parts.append(f'<a class="more" href="archive.html">更早（共 {len(dates)} 天）</a>')
    else:
        parts.append('<a class="more" href="archive.html">歷史存檔</a>')
    parts.append('<a class="more" href="feed.xml">RSS</a></nav>')
    return "".join(parts)


def render_groups(groups: list[tuple[str, int]]) -> str:
    """分類篩選器。用純錨點連結而不是 JS 篩選：

    捲動到該段落比隱藏其他內容更符合「跳過去讀」的需求，
    而且不依賴 JavaScript，離線存檔或關閉腳本時一樣能用。
    """
    parts = ['<nav class="groups">']
    for n, (name, count) in enumerate(groups):
        parts.append(f'<a href="#g{n}">{esc(name)}<b>{count}</b></a>')
    parts.append('<a class="top" href="#">回到頂端</a></nav>')
    return "".join(parts)


def render_digest(digest: dict, report: dict, date_key: str, date_label: str,
                  dates: list[str]) -> str:
    items = digest["items"]
    gen = tpe_str(digest["generated_at_utc"], "%Y/%m/%d %H:%M")

    ordered = sorted(items, key=lambda i: i["rank"])
    seen_groups = [g for g in GROUP_ORDER if any(i["group"] == g for i in ordered)]
    seen_groups += sorted({i["group"] for i in ordered} - set(seen_groups))
    counts = [(g, sum(1 for i in ordered if i["group"] == g)) for g in seen_groups]

    head = (f'<header><h1><a href="index.html">{SITE_TITLE}</a></h1>'
            f'<p class="sub">{date_label}　共 {len(items)} 則　'
            f'取材自過去 {digest["source_window_hours"]} 小時　產生於 {gen}（台北時間）</p>'
            f'{render_days(dates, date_key)}</header>')

    note = ('<p class="note">所有內容由程式自動收集、機器翻譯，'
            '每則都附上原始連結與原文標題供查證。'
            '譯文只改寫來源提供的文字，不補充任何外部資訊；'
            '來源未提供摘要者標為「僅標題」。</p>')

    body = [head, render_groups(counts), note]

    n = 0
    for gi, (g, _) in enumerate(counts):
        body.append(f'<h2 id="g{gi}">{esc(g)}</h2><div class="grid">')
        for it in ordered:
            if it["group"] == g:
                n += 1
                body.append(render_item(it, n))
        body.append("</div>")

    body.append(render_footer(digest, report))
    return page(f"{SITE_TITLE}｜{date_label}", "".join(body))


def render_footer(digest: dict, report: dict) -> str:
    parts = ['<footer>']

    failures = report.get("failures", []) if report else []
    if failures:
        parts.append("<h3>本次未取得的來源</h3><ul>")
        for f in failures:
            parts.append(f'<li>{esc(f["name"])}— {esc(f.get("error", "未知錯誤"))}</li>')
        parts.append("</ul>")

    dropped = digest.get("dropped", [])
    if dropped:
        parts.append(f"<h3>策展時剔除 {len(dropped)} 則</h3><ul>")
        for d in dropped[:12]:
            parts.append(f'<li>{esc(d.get("reason", ""))}</li>')
        parts.append("</ul>")

    if report:
        parts.append(
            f'<h3>本次執行</h3><p>來源 {report.get("sources_ok", 0)}／'
            f'{report.get("sources_total", 0)} 成功，'
            f'原始 {report.get("raw_items", 0)} 則，去重後 {report.get("clustered_items", 0)} 則，'
            f'策展後 {len(digest["items"])} 則。</p>'
        )

    models = digest.get("model", {})
    parts.append(
        f'<p>策展：{esc(str(models.get("curate", "")))}　'
        f'翻譯：{esc(str(models.get("translate", "")))}</p>'
        '<p>本頁由程式自動產生。譯文可能有誤，請以原始連結內容為準。</p></footer>'
    )
    return "".join(parts)


def render_archive(dates: list[str]) -> str:
    head = (f'<header><h1><a href="index.html">{SITE_TITLE}</a></h1>'
            f'<p class="sub">歷史存檔　共 {len(dates)} 天</p>'
            f'{render_days(dates, "")}</header>')
    lis = "".join(f'<li><a href="{d}.html">{d}</a></li>' for d in dates)
    return page(f"{SITE_TITLE}｜歷史存檔", f"{head}<ul class=\"arc\">{lis}</ul>")


def render_feed(digest: dict, base: str) -> str:
    """RSS：只放標題與摘要，連結指向原始出處而非本站，讓讀者直接看原文。"""
    now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    entries = []
    for it in sorted(digest["items"], key=lambda i: i["rank"]):
        try:
            pub = datetime.fromisoformat(
                it["published_utc"].replace("Z", "+00:00")
            ).strftime("%a, %d %b %Y %H:%M:%S +0000")
        except (ValueError, KeyError):
            pub = now
        desc = it["summary_zh"] or "（原始來源未提供摘要）"
        entries.append(
            "<item>"
            f"<title>{esc(it['title_zh'])}</title>"
            f"<link>{esc(it['url'])}</link>"
            f"<guid isPermaLink=\"false\">{esc(it['id'])}</guid>"
            f"<pubDate>{pub}</pubDate>"
            f"<category>{esc(it['group'])}</category>"
            f"<description>{esc(desc)}　｜原文：{esc(it['title_original'])}"
            f"　｜來源：{esc(it['source'])}</description>"
            "</item>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0"><channel>'
        f"<title>{esc(SITE_TITLE)}</title>"
        f"<link>{esc(base)}</link>"
        f"<description>{esc(SITE_DESC)}</description>"
        "<language>zh-tw</language>"
        f"<lastBuildDate>{now}</lastBuildDate>"
        + "".join(entries)
        + "</channel></rss>"
    )


def update_seen(digest: dict, date_key: str) -> int:
    """把今天登過的項目記下來，避免明天的時間窗重疊時重複刊登。

    在這裡寫、而不是在 collect 或 translate 寫：只有網頁真的產生出來，
    才算「已發布」。翻譯中途失敗時不該把項目消耗掉。
    """
    ids: dict[str, str] = {}
    if SEEN.exists():
        try:
            ids = json.loads(SEEN.read_text(encoding="utf-8")).get("ids", {})
        except (json.JSONDecodeError, OSError):
            ids = {}

    for it in digest["items"]:
        ids[it["id"]] = date_key
        # 被策展合併掉的跨語言版本也要記，否則明天換另一語言的版本又上一次
        for merged in it.get("merged_ids", []):
            ids[merged] = date_key

    floor = (datetime.now(TPE) - timedelta(days=SEEN_DAYS)).strftime("%Y-%m-%d")
    ids = {k: v for k, v in ids.items() if v >= floor}

    SEEN.parent.mkdir(exist_ok=True)
    SEEN.write_text(
        json.dumps({"ids": ids}, ensure_ascii=False, indent=0, sort_keys=True),
        encoding="utf-8",
    )
    return len(ids)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", action="store_true",
                    help="重畫最後一次發布的內容（調版面用，不呼叫模型也不動已發布清單）")
    ap.add_argument("--no-index", action="store_true",
                    help="只寫當日存檔，不更新 index.html（回補歷史時用）")
    args = ap.parse_args()

    if args.replay:
        if not LAST_DIGEST.exists():
            sys.exit(f"找不到 {LAST_DIGEST}，要先成功執行過一次完整流程")
        digest = json.loads(LAST_DIGEST.read_text(encoding="utf-8"))
        report = json.loads(LAST_REPORT.read_text(encoding="utf-8")) if LAST_REPORT.exists() else {}
        print(f"重播模式：{LAST_DIGEST.name}（{len(digest['items'])} 則）")
    else:
        if not DIGEST.exists():
            sys.exit(f"找不到 {DIGEST}，請先執行 python translate.py")
        digest = json.loads(DIGEST.read_text(encoding="utf-8"))
        report = json.loads(REPORT.read_text(encoding="utf-8")) if REPORT.exists() else {}

    # 回補歷史時，內容的日期不等於產生的日期，由 digest 明確帶 digest_date 指定
    if digest.get("digest_date"):
        day = datetime.strptime(digest["digest_date"], "%Y-%m-%d")
    else:
        day = datetime.fromisoformat(
            digest["generated_at_utc"].replace("Z", "+00:00")).astimezone(TPE)
    date_key = day.strftime("%Y-%m-%d")
    date_label = day.strftime("%Y年%m月%d日")

    DOCS.mkdir(exist_ok=True)
    # 日期清單要先算出來（含今天），首頁與存檔頁的「近 7 日」切換都需要它
    known = {p.stem for p in DOCS.glob("*.html") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem)}
    known.add(date_key)
    dates = sorted(known, reverse=True)

    html_out = render_digest(digest, report, date_key, date_label, dates)
    (DOCS / f"{date_key}.html").write_text(html_out, encoding="utf-8")
    if not args.no_index:
        (DOCS / "index.html").write_text(html_out, encoding="utf-8")
        (DOCS / "feed.xml").write_text(render_feed(digest, ""), encoding="utf-8")

    (DOCS / "archive.html").write_text(render_archive(dates), encoding="utf-8")
    # GitHub Pages 預設會走 Jekyll，底線開頭的檔案會被吃掉；關掉比較保險
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")

    print(f"已產生 {len(digest['items'])} 則 → {DOCS / 'index.html'}")
    print(f"存檔 {date_key}.html　歷史共 {len(dates)} 天")

    if args.replay:
        # 重播只是重畫版面，不算一次新的發布：不能動已發布清單，
        # 否則會把項目重複消耗掉
        print("（重播模式：未更新已發布清單）")
        return

    # last-digest 是「首頁現在長什麼樣」的快照，回補歷史時不該覆蓋它
    if not args.no_index:
        LAST_DIGEST.parent.mkdir(exist_ok=True)
        shutil.copyfile(DIGEST, LAST_DIGEST)
        if REPORT.exists():
            shutil.copyfile(REPORT, LAST_REPORT)

    kept = update_seen(digest, date_key)
    print(f"已發布清單 {kept} 筆（保留 {SEEN_DAYS} 天，明天不會重複刊登）")


if __name__ == "__main__":
    main()
