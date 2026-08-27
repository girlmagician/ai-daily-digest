# 設定一覽

最後核對：2026-08-19（對應 commit `c042ebf`）

這份文件記錄**現在實際生效的參數值、它在哪個檔案、以及為什麼是這個值**，
用來在調整或優化前先確認現況。三份文件的分工：

| 文件 | 回答的問題 |
|---|---|
| `README.md` | 這個系統是什麼、怎麼跑、設計上為什麼這樣做 |
| **`SETTINGS.md`（本檔）** | **現在的設定值是多少、要改去哪裡改** |
| `HANDOVER.md` | 還有什麼沒做完、待決定、已知限制 |

**之後要調整或優化，把這一份（`ai-daily-digest/SETTINGS.md`）給我就夠了。**
每個參數的現值、所在檔案、以及改動的副作用都在這裡；需要設計背景或待辦事項時，
我會自己去翻上表那兩份。

改動參數時請一併更新本檔的對應列與最下方的變更紀錄。

---

## 一、排程與執行環境

| 項目 | 值 | 位置 |
|---|---|---|
| 排程 | `cron: "23 23 * * *"`（UTC 23:23 = 台北 **07:23**） | `.github/workflows/daily.yml` |
| 手動觸發 | 有（`workflow_dispatch`） | 同上 |
| 執行環境 | `ubuntu-latest` | 同上 |
| 逾時 | `timeout-minutes: 75` | 同上 |
| 併發 | group `daily-digest`，`cancel-in-progress: false` | 同上 |
| 權限 | `contents: write`（要把 `docs/` 與 `state/` 推回 repo） | 同上 |
| 認證 | GitHub Secret `CLAUDE_CODE_OAUTH_TOKEN` | repo Settings → Secrets |
| 執行報告 | `out/` 上傳為 artifact，保留 14 天（失敗時也上傳） | 同上 |
| 看門狗 | `cron` 三次：UTC 01:17 / 03:17 / 05:17（台北 09:17 / 11:17 / 13:17） | `.github/workflows/watchdog.yml` |

**為什麼是 23:23 而不是整點**：整點是全球最熱門的 cron 時間，GitHub 排程佇列會延遲更久；
且 Anthropic 端同樣是尖峰（實測 UTC 00:00 觸發時策展呼叫收到 529 Overloaded）。
提早到 07:23 也留一小時可以重試，仍趕得上 08:30。

**為什麼要有看門狗**：GitHub 的 `schedule` 是 best-effort，尖峰時會**靜默跳過**——那次執行
根本沒有被建立，所以也不會有失敗通知信。2026-08-26 23:23 UTC 那一次就這樣消失了，
連續八天的自動更新斷在那裡，直到隔天早上人工發現才補跑。

看門狗每天早上檢查 `docs/<台北日期>.html` 在不在 `main` 上，然後：

| 現況 | 動作 |
|---|---|
| 檔案已存在 | 安靜退場 |
| `daily.yml` 還在跑 | 不動作，交給下一個時段 |
| 今天還沒補觸發過 | 觸發 `daily.yml` 重跑 |
| 今天補觸發過了、仍然沒有檔案 | 開 issue + 讓自己失敗（兩條獨立的通知管道） |

判斷依據是**現況**而不是「這是第幾個時段」，所以三個時段裡任何一個被 GitHub 跳過，
剩下的都會自己接上正確的動作。排三次而不是一次，就是為了讓看門狗自己也有備援。
補跑是安全的：`collect.py` 的時間窗 48 小時、`state/seen.json` 會擋掉已發布過的項目，
既不會漏新聞也不會重複刊登。

**看門狗擋不住的情況**：三個時段全部被 GitHub 跳過。機率很低但不是零，而且沒有辦法
在 GitHub 內部解決——要真正保證，得有一個 GitHub 以外的東西來戳它。目前判斷不值得。

---

## 二、管線五步與參數

工作流程實際執行的指令，依序：

```
python preflight.py                       # 檢查 CLI 與認證
python collect.py --hours 48 --top 45     # 收集、去重、評分、選候選
python extract.py                         # 補抓原文全文
python translate.py --target 30           # 策展與翻譯（呼叫模型）
python render.py                          # 產生網頁
```

### 2-1 收集　`collect.py`

| 參數 | 現值 | 預設 | 說明 |
|---|---|---|---|
| `--hours` | 48 | 48 | 時間窗。大於執行間隔 24 小時，某天失敗也不會漏新聞；重複由 `state/seen.json` 擋 |
| `--top` | **45** | 30 | 候選數。**刻意大於策展目標 30**，策展才有得裁 |
| `--min-score` | **1.6** | 1.6 | 一般新聞的分數下限。官方公告與論文豁免 |
| `--max-pinned` | 12 | 12 | 官方公告最多佔幾則 |
| `--max-papers` | 5 | 5 | 論文區最多幾則（獨立區，不與新聞競爭） |
| `--max-per-source` | 3 | 3 | 單一來源在名單裡最多幾席 |
| `--workers` | 8 | 8 | 抓取併發數 |

模組常數（`collect.py`）：

| 常數 | 值 | 說明 |
|---|---|---|
| `PER_SOURCE_CAP` | 12 | 每個來源進入池子的則數上限 |
| `PAPER_SOURCE_CAP` | 8 | 論文來源的上限（arXiv 單日數百篇會淹沒整池） |
| `SIMILARITY_THRESHOLD` | 0.78 | 標題相似度達此值視為同一則新聞 |
| `MIN_SUBSTANCE` | 80 | 摘要少於此字數才進「無內容」判定 |
| `SOCIAL_HN_DISCOUNT` | **0.5** | 社群貼文的 HN 熱度加成乘數 |
| `MIN_SCORE` | **1.6** | 分數下限的預設值 |

其他旗標：`--no-hn`、`--ignore-seen`、`--no-scrape`（皆為除錯用）。

### 2-2 補抓原文　`extract.py`

| 項目 | 值 | 說明 |
|---|---|---|
| `MAX_CHARS` | 6000 | 超過對摘要沒幫助，只是燒 token |
| `MIN_GAIN` | 200 | 抓到的內文至少要比 feed 摘要多這麼多字才值得換掉 |
| `--workers` | 6 | |
| 論文 | **預設不抓正文**，要加 `--include-papers` | |

### 2-3 策展與翻譯　`translate.py`

| 項目 | 值 | 說明 |
|---|---|---|
| `--target` | **30** | 策展後保留幾則（上限，可以更少） |
| `BATCH_SIZE` | 5 | 翻譯分批大小 |
| `INPUT_CHARS` | 4000 | 送進翻譯的單則字數上限 |
| 策展模型 | `claude-opus-5` | `llm.MODEL_CURATE`。需要判斷力 |
| 翻譯模型 | `claude-sonnet-5` | `llm.MODEL_TRANSLATE` |
| 分組 | 模型與產品發布／產業與資本／研究與論文／遊戲與娛樂／政策與法規／工程與工具／其他 | `GROUPS` |
| 標記種類 | 譯／摘／僅標題 | `KINDS` |

策展編輯只做三件事：合併跨語言重複、剔除無實質內容者、分組排序。
**它只能刪不能加**，而且提示詞明文禁止用自身既有知識判斷重要性——
排序依據只能是輸入提供的訊號（HN 分數、報導家數、來源類型、標題資訊量）。

使用者偏好寫在 `CURATE_SYSTEM` 裡：**遊戲與娛樂產業的 AI 應用**、
各大 AI 公司的最新消息、當下熱門議題。

### 2-4 產生網頁　`render.py`

| 項目 | 值 | 說明 |
|---|---|---|
| `SEEN_DAYS` | 21 | 已發布清單保留三週 |
| `DAYS_SHOWN` | 7 | 「近 7 日」列出幾天 |
| 時區 | UTC+8 | `TPE` |
| 版面寬度 | 1280px | `CSS` |
| 卡片排列 | 兩欄 grid，視窗 <900px 收成一欄 | 同上 |
| 旗標 | `--replay`（重畫最後一次發布，不花錢）、`--no-index`（只寫存檔頁，回補用） | |
| 收藏 | 見 2-5 | |

每次執行的最後會呼叫 `refresh_shell()`，把所有既有存檔頁的「外殼」重刷成最新版：
「近 7 日」導覽的日期清單，以及收藏腳本。只做字串替換，不重畫內容、不花錢。

### 2-5 收藏（全站唯一的 JavaScript）

| 項目 | 值 |
|---|---|
| 儲存位置 | 瀏覽器 localStorage，key `ai-digest-favs` |
| 資料格式 | `{原文網址: {t 標題, s 來源, d 日期, g 分類, ts 收藏時間}}` |
| 識別碼 | **原文網址**，不是內部的 id |
| 收藏頁 | `docs/favorites.html`，內容全部由 JS 從 localStorage 畫出來 |
| 進入點 | 每頁導覽列的「收藏 N」連結（`a.fav-link`，筆數由腳本填） |
| 收藏按鈕 | 每張卡片標題右上角的 ☆／★ |
| 匯出／匯入 | 收藏頁上的按鈕，JSON 檔 |
| 舊頁支援 | `refresh_shell()` 每次 render 把腳本注入所有既有存檔頁 |
| 程式位置 | `render.FAV_CSS`、`render._FAV_JS`、`render.fav_js()`、`render.render_favorites()` |

**這是全站唯一用到 JavaScript 的地方。** 其餘功能（含分類篩選器）都刻意用純錨點，
關掉 JS 一樣能用。收藏做不到不用 JS，所以採漸進增強：關掉 JS 頁面完全照舊可讀，
只是不會出現收藏按鈕，既有內容一個字都不依賴腳本。一樣不引任何外部資源。

**識別碼為什麼用網址**：卡片標記裡本來就有原文連結，不必為了收藏改每張卡片的 HTML，
舊存檔頁也能靠注入腳本直接支援。同一篇文章跨兩天出現也只會是一筆收藏。

**資料只存在瀏覽器裡**，不會上傳、不經過本站伺服器，所以：換裝置、換瀏覽器、
清除網站資料都會消失，唯一的退路是「匯出 JSON」。每筆約 200 bytes，
localStorage 上限約 5MB，等於幾千則都放得下。


---

## 三、選稿規則

### 3-1 評分公式（`collect.score()`）

```
分數 = 有效來源權重 + 跨來源加成 + HN 熱度加成 + 新鮮度 − 推估時間罰分
```

| 項 | 算法 | 範圍 |
|---|---|---|
| 有效來源權重 | 綜合型來源（`ai_filter: true` 且權重 >1）：`1 + (權重−1) × min(1, AI強度/4)`；其餘直接用來源權重 | 0.7 ~ 2.2 |
| 跨來源加成 | `0.8 × (報導家數 − 1)` | 0 起 |
| HN 熱度加成 | `min(1.6, √(HN分數/400))`，**社群貼文再乘 0.5** | 0 ~ 1.6 |
| 新鮮度 | `max(0, 1 − 年齡小時/時間窗) × 0.5` | 0 ~ 0.5 |
| 推估時間罰分 | feed 無時間欄位、改用抓取時間者 −0.3 | −0.3 或 0 |

HN 加成用平方根壓縮，避免單一爆紅文章分數失控：50 分 → 0.35、200 分 → 0.71、800 分 → 1.41。

綜合型來源的加權必須落在「AI × 該領域」的交集，否則遊戲媒體的一般遊戲新聞
會靠 ×2 加權壓過真正的 AI 新聞。

### 3-2 名額分配（`collect.select()`）

依序處理：

1. **論文**獨立成區，取分數最高的 `--max-papers`（5）則。**不與新聞競爭、豁免分數下限**
2. **官方公告**（`pinned: true` 的來源）取分數最高的 `--max-pinned`（12）則。**豁免分數下限**
3. **其餘新聞**先濾掉低於 `--min-score`（1.6）的，再依分數排序填滿剩餘名額；
   單一來源最多 `--max-per-source`（3）席，名額沒填滿才回頭補被壓下的項目

**為什麼論文與官方公告要豁免**：論文權重 0.8–0.9，加滿新鮮度也構不到 1.6，
一併套用會整批清空；官方公告是日報的骨幹，超過 40 小時的舊公告也會掉到 1.6 以下。

**為什麼下限只擋一般新聞**：權重 1.0 的來源就算全新也只有 1.5，
等於規定「沒有 HN 分數、也沒有跨來源佐證的普通來源不上稿」。
目的不是湊滿數量，而是寧可當天少幾則——來源薄的日子本來就該看起來比較薄。

### 3-3 社群貼文

`fetchlib.SOCIAL_HOSTS`：`twitter.com`、`mobile.twitter.com`、`x.com`、`bsky.app`、
`threads.net`、`threads.com`、`mastodon.social`、`fosstodon.org`、`hachyderm.io`、
`linkedin.com`、`t.me`、`reddit.com`、`old.reddit.com`

**降權而非排除**：各家 AI 公司的重大宣布確實常常首發在 X 上，排除會誤殺。
熱度加成乘 0.5，並在頁面標「社群貼文」讓讀者自己判斷。

實例：一則 HN 151 分的 twitter 個人推文，分數從 2.145 降到 1.838——
仍在下限之上，還是會上稿，只是排名下降。要讓它掉出候選得把折扣壓到 0.11 以下。

### 3-4 「無內容」判定（`collect.is_contentless()`）

摘要少於 80 字**且**標題是純版號或短於 6 個字，才會被剔除。
門檻壓到 6 是因為中日文標題天生就短（「AI 教父辭職」只有 7 個字卻是完整資訊）。
標題有實質內容的一律留給策展階段判斷。

---

## 四、來源

`sources.yaml`，共 **58 個 RSS + 2 個爬蟲**。

| 面向 | 分布 |
|---|---|
| 分類 | media 14、vendor 12、game 12、analysis 5、newsletter 5、eng 5、paper 3、policy 2 |
| 語言 | en 46、ja 8、zh-CN 3、zh-TW 1 |
| 官方公告（`pinned`） | 7 個 |
| 需 AI 關鍵字過濾（`ai_filter`） | 24 個 |
| 權重分布 | 2.2×1、2.0×6、1.8×3、1.7×1、1.6×5、1.5×9、1.4×7、1.3×6、1.2×5、1.1×3、1.0×7、0.9×2、0.8×2、0.7×1 |

爬蟲（`no_feed` 區標 `strategy: scraper`）：**Anthropic News**、**The Batch**。

`dropped` 區記錄 18 個評估過但不採用的來源與原因，`api_sources` 記錄尚未實作的
API 來源（GDELT、arXiv API、Gmail API 等）。**要加來源前先看這兩區，避免重踩。**

### Hacker News（`hn.py`）

熱度訊號主力，兩個作用：對上既有項目補 points，以及補入 RSS 沒有的故事。

| 項目 | 值 |
|---|---|
| API | `https://hn.algolia.com/api/v1/search_by_date`（免認證） |
| `MIN_POINTS` | 20（分數太低代表社群沒反應） |
| `PAGE_SIZE` / `MAX_PAGES` | 200 / 8 |
| 補入的項目權重 | 1.3、`cat: forum`、`ai_filter: true`（標題本身要命中 AI 關鍵字） |

**重要**：HN 補進來的項目，`published_utc` 是**投稿到 HN 的時間**，不是原文發表日期
（Algolia API 拿不到原文日期）。頁面上會顯示成「08/16 投稿 HN」以免誤讀。

### 抓取禮貌（`fetchlib.py`）

| 項目 | 值 |
|---|---|
| `USER_AGENT` | `ai-daily-digest/0.4 (+https://github.com/girlmagician/ai-daily-digest)` |
| `DEFAULT_TIMEOUT` | 20 秒 |
| 每網域最小間隔 | 預設 0.4 秒；reddit 3.0 秒；rsshub.app 2.0 秒 |
| `MIN_ARTICLE_CHARS` | 400 |

---

## 五、模型呼叫與成本

**使用者的方案無法申請 Anthropic API key。** 所有模型呼叫都走 Claude Code CLI
（`llm.py`），CI 用 `claude setup-token` 產生的 OAuth token。
**不要提議改用 anthropic SDK 或 API key。**

| 項目 | 值 |
|---|---|
| CLI 尋找順序 | `claude` → `claude.cmd` → `claude.CMD` |
| 重試等待 | 10、30、60、120、240 秒（總計約 7.5 分鐘後放棄） |
| 每日成本 | 約 **US$0.7**，執行約 11 分鐘 |
| 歷史回補成本 | 每天 US$0.9–2.5，八天合計 US$10.87 |

成本主力是**翻譯階段約 4 萬輸出 token**。策展階段的輸入只有約 3,600 token，
所以調 `--top`（候選數）對成本影響很小——每則候選約 120 token。

---

## 六、狀態檔與產出

| 路徑 | 進版控 | 內容 |
|---|---|---|
| `docs/index.html` | 是 | 首頁（等於最新那天的存檔頁） |
| `docs/YYYY-MM-DD.html` | 是 | 每日存檔 |
| `docs/archive.html` | 是 | 歷史索引，每次 render 依 `docs/*.html` 重算 |
| `docs/favorites.html` | 是 | 收藏頁外殼，內容由瀏覽器端的 localStorage 畫 |
| `docs/feed.xml` | 是 | RSS |
| `state/seen.json` | 是 | 已發布清單 `{"ids": {項目雜湊: 日期}}`，保留 21 天 |
| `state/last-digest.json` | 是 | 首頁內容快照，`--replay` 用 |
| `state/last-report.json` | 是 | 上次執行的統計 |
| `out/` | 否 | 中間產物（`candidates.json`、`digest.json`、`run_report.json`） |
| `sources_verified.yaml`、`feed_report.md` | 否 | `verify_feeds.py` 的快照，不是設定檔 |

---

## 七、想調整什麼 → 改哪裡

| 想達成 | 改什麼 | 注意 |
|---|---|---|
| 每天登多／少幾則 | `translate.py --target`（workflow 第 76 行） | 翻譯成本與則數成正比 |
| 策展有更多／更少可裁 | `collect.py --top`（workflow 第 67 行） | 成本影響小，但 `extract.py` 要多抓，CI 時間變長 |
| 內容太雜、想更嚴 | 調高 `--min-score` | 來源薄的日子則數會明顯變少 |
| 某天則數太少 | 調低 `--min-score`（例如 1.5） | 1.6 落在分數分布的密集帶上，動 0.1 影響很大 |
| 某個來源太常出現 | 調低該來源 `weight`，或調低 `--max-per-source` | |
| 想加新來源 | `sources.yaml` 的 `sources`，先確認不在 `dropped` 裡 | 用 `verify_feeds.py` 先驗 |
| 社群貼文太多／太少 | `collect.SOCIAL_HN_DISCOUNT` | 壓到 0.11 以下會誤殺 X 上的官方宣布 |
| 論文太多／太少 | `collect.py --max-papers` | 論文豁免分數下限 |
| 只調版面 | `render.py` 的 `CSS`，然後 `python render.py --replay` | 不呼叫模型、不花錢、三秒完成 |
| 改分組名稱 | `translate.GROUPS` 與 `render.GROUP_ORDER` **兩邊都要改** | 不一致會讓分類篩選器排序錯亂 |
| 改執行時間 | workflow 的 `cron`（UTC） | 避開整點，理由見第一節 |
| 改收藏的外觀 | `render.FAV_CSS` | 改完 `render.py --replay`，所有舊頁會一起更新 |
| 改收藏的行為 | `render._FAV_JS` | 同上。改完建議用 jsdom 實測，不要只看語法 |
| 收藏資料搬家 | 收藏頁的「匯出 JSON」→ 新裝置「匯入」 | localStorage 不跨裝置同步 |

---

## 八、變更紀錄

| 日期 | 變更 | commit |
|---|---|---|
| 2026-08-19 | 新增收藏功能：卡片 ☆ 按鈕、`favorites.html` 收藏頁、匯出／匯入，腳本注入既有存檔頁 | `219e8bc` |
| 2026-08-19 | 選稿調整：`--top` 30→45、社群貼文熱度乘 0.5 並標示、分數下限 1.6、HN 日期標成「投稿 HN」 | `c042ebf` |
| 2026-08-19 | 每次 render 重刷所有存檔頁的「近 7 日」導覽 | `702635f` |
| 2026-08-19 | 回補 8/14–8/17，歷史存檔補齊為 8/10–8/19 連續十天 | `040b029` |
| 2026-08-18 | 版面：1280px、兩欄卡片、分類篩選器、近 7 日切換；新增 The Information、Bloomberg Technology、FT 人工智慧 | `7c7105e`、`703f356` |
