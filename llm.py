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
    pass


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
        raise LLMError(f"CLI 逾時（{timeout}s）")

    raw = proc.stdout.decode("utf-8", "replace").strip()
    err = proc.stderr.decode("utf-8", "replace").strip()
    if not raw:
        raise LLMError(f"CLI 無輸出（returncode={proc.returncode}）：{err[:300]}")

    try:
        env = json.loads(raw)
    except json.JSONDecodeError:
        raise LLMError(f"CLI 回應不是 JSON：{raw[:300]}")

    # 注意：is_error 為 true 時 subtype 仍可能是 "success"，只能看 is_error
    if env.get("is_error"):
        raise LLMError(f"CLI 回報錯誤：{env.get('result', '')[:300]}")
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
    """
    tail = user_tail(contract)
    prompt = user + tail
    last = ""
    cost_total = 0.0
    usage_total = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}

    for n in range(1, attempts + 1):
        res = _run(system, prompt, model, timeout)
        cost_total += res["cost"]
        for k in usage_total:
            usage_total[k] += res["usage"][k]
        try:
            data = extract_json(res["text"])
            if validate:
                validate(data)
            return {
                "data": data,
                "cost": cost_total,
                "usage": usage_total,
                "attempts": n,
            }
        except LLMError as e:
            last = str(e)
            print(f"  第 {n} 次輸出不合格：{last[:200]}")
            if n < attempts:
                prompt = (
                    f"{user}\n\n"
                    f"（上一次你的輸出有問題：{last[:300]}。"
                    f"請重新輸出完整且合法的 JSON 物件。）"
                    f"{tail}"
                )

    raise LLMError(f"連續 {attempts} 次無法取得合法輸出：{last}")
