"""開跑前的自我檢查：確認 Claude Code CLI 真的能用。

為什麼需要：認證問題是最常見的失敗原因，但翻譯是流程的第四步，
前面的收集與補抓要跑四五分鐘。把檢查提前，認證壞掉時三十秒內就知道，
而且錯誤訊息會直接指出是哪一種問題，不必翻日誌猜。

刻意不印出權杖內容，只印長度與前綴——日誌在公開 repo 上任何人都看得到。

用法：
    python preflight.py
離開碼 0 = 可以繼續；1 = 有問題，訊息已指出原因與處理方式。
"""

from __future__ import annotations

import os
import sys

import llm

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    print("── Claude Code CLI 自我檢查 ──")

    if llm.CLI is None:
        print("✗ 找不到 claude 執行檔")
        print("  PATH 上沒有 claude。CI 請確認 `npm install -g @anthropic-ai/claude-code` 成功；")
        print("  Windows 本機請用 claude.cmd（PowerShell 的執行原則會擋 .ps1）")
        return 1
    print(f"✓ 執行檔：{llm.CLI}")

    # 認證來源：CI 用長期權杖，本機用互動登入後留下的憑證
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or ""
    if token:
        print(f"✓ 環境變數 CLAUDE_CODE_OAUTH_TOKEN 已設定（長度 {len(token)}，開頭 {token[:11]}…）")
        if token != token.strip():
            print("  ⚠ 權杖前後有空白或換行，複製時很容易多帶一個換行——請重新存一次 secret")
    elif os.environ.get("CI"):
        print("✗ 環境變數 CLAUDE_CODE_OAUTH_TOKEN 沒有設定")
        print("  請確認 GitHub secret 名稱完全等於 CLAUDE_CODE_OAUTH_TOKEN，")
        print("  且 workflow 的該步驟有把它掛進 env:")
        return 1
    else:
        print("· 未設定權杖，將使用本機既有登入身分")

    print(f"· 測試呼叫（{llm.MODEL_TRANSLATE}）…")
    try:
        res = llm.ask_json(
            "你是測試用的 JSON 產生器。",
            '請輸出 {"ok": true}',
            llm.MODEL_TRANSLATE,
            '{"ok": true}',
            validate=lambda d: None if d.get("ok") is True else (_ for _ in ()).throw(
                llm.LLMError(f'預期 ok=true，實際得到 {d}')
            ),
            attempts=2,
            timeout=180,
        )
    except llm.LLMError as e:
        msg = str(e)
        print(f"✗ 呼叫失敗：{msg[:400]}")
        if "Not logged in" in msg or "login" in msg.lower():
            print("  → 認證問題。權杖無效、過期，或沒被 CLI 讀到。")
            print("    請重新執行 claude setup-token 產生新權杖並更新 secret。")
        elif "credit" in msg.lower() or "quota" in msg.lower() or "usage" in msg.lower():
            print("  → 用量或額度問題，不是設定錯誤。")
        return 1

    u = res["usage"]
    print(f"✓ 呼叫成功　輸入 {u['input']}、輸出 {u['output']} token，成本 US${res['cost']:.4f}")
    if u["cache_write"] > 5000:
        print(f"  ⚠ 快取寫入 {u['cache_write']:,} token 偏高——"
              f"檢查 llm.py 是否還帶著 --tools \"\"（少了它成本會差幾十倍）")
    print("── 檢查通過，可以開始收集 ──")
    return 0


if __name__ == "__main__":
    sys.exit(main())
