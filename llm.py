"""透過 Claude Code CLI 呼叫模型——不需要 API key。

為什麼用 CLI 而不用 SDK：使用者的方案無法申請 API key，但已有 Claude 訂閱。
`claude -p` 用既有登入身分執行，CI 上則用 `claude setup-token` 產生的長期權杖。

三個實測得到的關鍵設定（缺一個成本就差幾十倍）：

  --tools ""            不載入任何內建工具定義。實測：加了這個，快取寫入從
                        17,839 token 降到 0，單次成本 $0.195 → $0.0045。
                        注意 --disallowed-tools 沒用——它只是禁止使用，
                        工具定義照樣塞進提示詞裡計費。
  --system-prompt       **取代**而非附加系統提示詞。用 --append-system-prompt
                        會連 Claude Code 那份編碼取向的系統提示詞一起送出
                        （約 29k token），而且會干擾翻譯任務。
  --exclude-dynamic-system-prompt-sections
                        去掉環境資訊、git 狀態等動態段落。

不要加 --bare：那會跳過憑證載入，直接回 "Not logged in"。

與 SDK 版的差異：CLI 無法傳 json_schema 強制結構化輸出，所以改成
「提示詞要求 JSON + 寬容解析 + 結構檢查 + 重試」。真正的防編造保障
不在 schema，而在 translate.py 的 verify()——那些檢查完全保留。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time

CLI = shutil.which("claude") or shutil.which("claude.cmd") or shutil.which("claude.CMD")

# 一定要寫完整 ID：實測 --model opus 會解析成 claude-opus-4-8 而不是 Opus 5
# 策展需要判斷力（合併跨語言重複、排序）用 Opus；翻譯是機械性工作，
# Sonnet 的中文品質已足夠，且每天要跑好幾批，成本差 5 倍
MODEL_CURATE = "claude-opus-5"
MODEL_TRANSLATE = "claude-sonnet-5"

# 實測：只把格式規定放在系統提示詞尾端，模型會忽略它去寫 markdown 文章。
# 必須開頭先定調身分，結尾再壓一次，使用者訊息末尾還要再提醒一次。
JSON_HEAD = """你是一支 JSON 產生程式，不是寫文章的編輯。
你的回應會被程式直接 json.loads()，任何多餘字元都會導致整個流程失敗。
輸出的第一個字元必須是 {，最後一個字元必須是 }。
禁止 markdown 圍欄、禁止標題、禁止條列、禁止開場白與結語。

以下是你這次的任務內容：

"""

JSON_TAIL = "\n\n（再次強調：整個回應就是一個 JSON 物件，不要有任何其他文字。）"

# 結構規格放在系統提示詞裡會被忽略——實測模型會自己發明欄位名。
# 必須貼在使用者訊息的最末端，這是模型最服從的位置。
def user_tail(contract: str) -> str:
    return (
        "\n\n────\n**輸出格式（欄位名稱必須完全一致，不得改名、不得新增欄位）：**\n"
        f"{contract}\n\n"
        "現在只輸出這個 JSON 物件，第一個字元必須是 {。"
    )


class LLMError(RuntimeError):
    """輸出不合格——重試同一個請求有機會改善。"""


class LLMTransient(LLMError):
    """伺服器壅塞、限流、逾時——等一下再送同樣的請求就好。

    實測過的情形：GitHub Actions 在 UTC 00:00 觸發（整點是最熱門的排程時間），
    策展呼叫收到 API Error 529 Overloaded。這種錯誤不重試就等於當天沒有日報。
    """


class LLMAuthError(LLMError):
    """認證或權限問題——重試沒有意義，要人去處理。"""


# 從 CLI 的錯誤字串判斷該不該重試。寧可把不確定的當成暫時性（多等一下），
# 也不要把暫時性誤判為永久性（整天空白）。
_TRANSIENT = re.compile(
    r"\b(429|500|502|503|504|529)\b"
    r"|overloaded|rate.?limit|too many requests"
    r"|temporarily|try again|timeout|timed out"
    r"|connection (reset|refused|error)|econnreset|socket hang up",
    re.IGNORECASE,
)
_AUTH = re.compile(
    r"not logged in|invalid api key|authentication|unauthorized"
    r"|\b401\b|\b403\b|please run /login",
    re.IGNORECASE,
)

# 遇到暫時性錯誤的等待秒數。總計約 7.5 分鐘後放棄；
# 工作流程的 timeout 要留得比這個寬裕
BACKOFF = (10, 30, 60, 120, 240)


def available() -> bool:
    return CLI is not None


def extract_json(text: str) -> dict:
    """從模型輸出中取出 JSON 物件。

    沒有 schema 強制輸出，所以要容忍模型偶爾包 markdown 圍欄或加開場白。
    但只容忍「包裝」，不容忍「內容不合法」——解析失敗就拋出，由上層重試。
    """
    t = text.strip()

    fence = re.match(r"^```[a-zA-Z]*\s*(.*?)\s*```$", t, re.DOTALL)
    if fence:
        t = fence.group(1).strip()

    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass

    # 掃描第一個左右平衡的 {...}，字串內的括號與跳脫字元要跳過
    start = t.find("{")
    if start < 0:
        raise LLMError(f"輸出中找不到 JSON 物件：{t[:200]}")
    depth, in_str, esc = 0, False, False
    for pos in range(start, len(t)):
        ch = t[pos]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[start : pos + 1])
                except json.JSONDecodeError as e:
                    raise LLMError(f"JSON 解析失敗：{e}") from e
    raise LLMError(f"JSON 物件未閉合（可能被截斷）：{t[-200:]}")


def _run(system: str, user: str, model: str, timeout: int) -> dict:
    if CLI is None:
        sys.exit("找不到 claude CLI。請確認已安裝並在 PATH 中（npm i -g @anthropic-ai/claude-code）")

    cmd = [
        CLI, "-p",
        "--output-format", "json",
        "--model", model,
        "--system-prompt", JSON_HEAD + system + JSON_TAIL,
        "--exclude-dynamic-system-prompt-sections",
        "--tools", "",
    ]
    try:
        proc = subprocess.run(
            cmd,
            input=user.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise LLMTransient(f"CLI 逾時（{timeout}s）")

    raw = proc.stdout.decode("utf-8", "replace").strip()
    err = proc.stderr.decode("utf-8", "replace").strip()
    if not raw:
        # 沒有任何輸出通常是行程被殺或網路斷掉，值得再試
        raise LLMTransient(f"CLI 無輸出（returncode={proc.returncode}）：{err[:300]}")

    try:
        env = json.loads(raw)
    except json.JSONDecodeError:
        raise LLMError(f"CLI 回應不是 JSON：{raw[:300]}")

    # 注意：is_error 為 true 時 subtype 仍可能是 "success"，只能看 is_error
    if env.get("is_error"):
        detail = str(env.get("result") or "")[:300]
        if _AUTH.search(detail):
            raise LLMAuthError(detail)
        if _TRANSIENT.search(detail):
            raise LLMTransient(detail)
        raise LLMError(f"CLI 回報錯誤：{detail}")
    if env.get("stop_reason") == "max_tokens":
        raise LLMError("輸出被長度上限截斷，請縮小批次")

    usage = env.get("usage", {}) or {}
    return {
        "text": env.get("result", ""),
        "cost": float(env.get("total_cost_usd") or 0),
        "usage": {
            "input": int(usage.get("input_tokens") or 0),
            "output": int(usage.get("output_tokens") or 0),
            "cache_read": int(usage.get("cache_read_input_tokens") or 0),
            "cache_write": int(usage.get("cache_creation_input_tokens") or 0),
        },
        "stderr": err,
    }


def ask_json(
    system: str,
    user: str,
    model: str,
    contract: str,
    validate=None,
    attempts: int = 3,
    timeout: int = 900,
) -> dict:
    """呼叫模型並取回 JSON 物件，失敗自動重試。

    contract 是輸出結構範例，會貼在使用者訊息末端。
    validate(data) 應在結構不符時拋出 LLMError；錯誤訊息會回饋給下一次嘗試，
    讓模型知道上次哪裡錯了。回傳 {"data":…, "cost":…, "usage":…, "attempts":…}。

    兩種失敗分開計數，因為處理方式完全不同：
      內容不合格  改寫提示詞、附上錯誤原因後立刻重送（最多 attempts 次）
      暫時性錯誤  同樣的請求，等一段時間再送（BACKOFF）
    認證問題直接往上拋——重試只是浪費時間，需要人去換權杖。
    """
    tail = user_tail(contract)
    prompt = user + tail
    last = ""
    cost_total = 0.0
    usage_total = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    content_tries = 0
    waits = 0

    while True:
        try:
            res = _run(system, prompt, model, timeout)
        except LLMAuthError as e:
            raise LLMAuthError(
                f"認證失敗，重試無用：{e}。"
                f"請重新執行 claude setup-token 並更新 CLAUDE_CODE_OAUTH_TOKEN"
            ) from e
        except LLMTransient as e:
            if waits >= len(BACKOFF):
                raise LLMTransient(f"暫時性錯誤重試 {waits} 次仍失敗：{e}") from e
            wait = BACKOFF[waits]
            waits += 1
            print(f"  伺服器忙碌（{str(e)[:120]}）；{wait} 秒後重試（第 {waits}/{len(BACKOFF)} 次）")
            time.sleep(wait)
            continue

        cost_total += res["cost"]
        for k in usage_total:
            usage_total[k] += res["usage"][k]

        content_tries += 1
        data = None
        try:
            data = extract_json(res["text"])
            if validate:
                validate(data)
            return {
                "data": data,
                "cost": cost_total,
                "usage": usage_total,
                "attempts": content_tries,
                "waits": waits,
            }
        except (LLMTransient, LLMAuthError):
            raise
        except LLMError as e:
            last = str(e)
            print(f"  第 {content_tries} 次輸出不合格：{last[:200]}")
            if content_tries >= attempts:
                err = LLMError(f"連續 {attempts} 次無法取得合法輸出：{last}")
                # 把最後一次的輸出附在例外上，讓呼叫端有機會分辨
                # 「內容不可信」與「只是格式／字形不合」——後者不值得中止整份日報。
                # data 為 None 代表連 JSON 都解不出來，那種情況沒有東西可放行。
                if data is not None:
                    err.partial = {
                        "data": data,
                        "cost": cost_total,
                        "usage": usage_total,
                        "attempts": content_tries,
                        "waits": waits,
                    }
                raise err from e
            prompt = (
                f"{user}\n\n"
                f"（上一次你的輸出有問題：{last[:300]}。"
                f"請重新輸出完整且合法的 JSON 物件。）"
                f"{tail}"
            )
