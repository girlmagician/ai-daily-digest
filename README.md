# AI 情報日報

每天自動收集海外 AI 情報，翻譯成繁體中文，產生一頁可分享的網頁，每則都附上原始連結。

## 為什麼不會編造內容

翻譯是唯一讓 LLM 碰資料的環節，而且被限制成「只能改寫眼前的文字」。設計上靠結構保證，不靠模型自律：

1. **模型永遠不輸出網址。** 它只輸出項目 id 與譯文；網址、來源、時間全部由程式依 id 從 `out/candidates.json` join 回來。模型在結構上沒有機會捏造連結。
2. **抓不到摘要就標「僅標題」**，不准根據標題想像內文。原文沒摘要卻產出摘要，程式會判定失敗並要求重譯。
3. **輸出的每個 id 都必須存在於輸入**，對不上就整批重跑，不是警告後放行。
4. **譯文出現 `http`／`www` 一律退回**——那代表模型在自己拼連結。
5. **有內文卻交白卷也算失敗**。反向的檢查同樣重要：只有標題加連結的項目，
   讀者等於什麼也沒拿到，而且會被誤標成「原始來源未提供摘要」。
6. 連續三次驗證不過就**中止整個流程**，寧可當天沒有日報，也不要產出不可信的內容。

收集階段還有兩道程式判定，不倚賴模型：

- **樣板文字不算內文**（`fetchlib.looks_like_content`）。抽取器在 GitHub release 頁面上
  會抽出 237 字的登入提示，長度剛好過門檻而被當成正文。低於 400 字、或開頭就是
  登入／cookie／付費牆樣板的，一律視為抓不到。
- **沒有內文又只有版號的項目直接剔除**（`collect.is_contentless`）。像 llama.cpp 的
  `v0.1.0` 這種，標題不帶資訊、內文只有「Release v0.1.0」十四個字，
  但 HN 分數會讓它看起來有熱度，交給 LLM 判斷反而容易漏放。

每則都附上原文標題與原始連結，讀者不必相信譯文，點下去就能對照。頁尾誠實列出本次沒抓到的來源——少了什麼比多了什麼更難察覺。

## 架構

```
sources.yaml ──► collect.py ──► extract.py ──► translate.py ──► render.py ──► docs/
                 純程式，無 LLM   補抓原文全文   Claude，只能改寫   純程式
```

| 檔案 | 職責 |
|---|---|
| `sources.yaml` | 唯一維護的來源清單，含已剔除來源與其理由 |
| `collect.py` | 抓取、去重、AI 相關性過濾、評分、排序 |
| `scrape.py` | 沒有 RSS 的來源（Anthropic News、The Batch）改爬索引頁 |
| `extract.py` | 補抓入選項目的原文全文，摘要才寫得完整 |
| `hn.py` | Hacker News 熱度訊號（Reddit 擋機器人，改用 HN） |
| `fetchlib.py` | 網址正規化、逐網域限速、時間處理 |
| `llm.py` | 透過 Claude Code CLI 呼叫模型（不需要 API key） |
| `translate.py` | 策展（合併跨語言重複、分組排序）與翻譯，含所有驗證 |
| `render.py` | 產生 `docs/` 靜態網頁、存檔與 RSS |
| `verify_feeds.py` | 來源健檢工具，新增來源時才需要跑 |
| `state/seen.json` | 已發布項目，避免時間窗重疊造成重複刊登 |

## 本機執行

```bash
pip install -r requirements.txt
npm install -g @anthropic-ai/claude-code
claude          # 先登入一次

python collect.py --hours 48 --top 30
python extract.py
python translate.py --target 30
python render.py                        # 產出在 docs/index.html
```

試跑省成本：`python translate.py --limit 12 --target 8`

## 自動執行

`.github/workflows/daily.yml` 每天 UTC 00:00（台北 08:00）執行，把 `docs/` 推回 repo，由 GitHub Pages 發布。

需要一個 secret：

```bash
claude setup-token     # 產生長期權杖（需要 Claude 訂閱）
```

把輸出的權杖存成 repo secret **`CLAUDE_CODE_OAUTH_TOKEN`**。權杖等同帳號憑證，只存在 GitHub Secrets，不要寫進任何檔案。

## 成本

實測一天 26 則約 **US$0.8**（策展用 Opus 5、翻譯用 Sonnet 5）。

摘要長度上限（`SUMMARY_ASK` / `SUMMARY_MAX`）對成本影響很大，但關鍵在**兩者要留差距**：
一度把提示詞目標與程式攔截線都訂成同一個數字，結果模型常常只超出十幾個字，
整批因此重譯，成本從 $0.8 跳到 $2.1。攔截線是用來擋「把整篇文章翻完」的失控輸出
（實測有 3,669 字的），不是用來計較那幾個字。

呼叫 CLI 時務必帶 `--tools ""`：實測不帶的話每次多送 17,839 個工具定義 token，單次成本從 $0.0045 變成 $0.195。`--disallowed-tools` 沒有用，它只禁止使用、定義照樣計費。

## 已知限制

- **跨語言重複**只能靠策展階段的 LLM 判斷，程式的標題相似度比對抓不到。
- **Reddit 全面擋機器人**（403／429），已從來源移除，社群熱度改用 Hacker News。
- **Anthropic News、The Batch 沒有 RSS**，改爬公開索引頁（`scrape.py`）。索引頁改版時會壞，
  屆時 `run_report.json` 會回報「索引頁找不到文章連結」。
- **機器之心**首頁只回 3,251 字的 JS 空殼，沒有瀏覽器抓不到，已放棄；簡中由量子位覆蓋。
- **約兩成的文章抓不到全文**（付費牆、純 JS 網站），那幾則只能沿用 feed 導言，摘要會比較短。
- **arXiv RSS 只有當日更新**，補抓歷史需改用 arXiv API。
- 部分來源的 feed 時間戳是未來時間（時區標記錯誤），程式一律夾回現在，並在頁面標注「時間存疑」。
