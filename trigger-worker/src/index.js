// AI 情報日報的準時觸發器（2026-11-25）
//
// 為什麼需要：GitHub Actions 的 schedule 是 best-effort，延遲從 8 月的 +2～4 小時
// 一路拉長到 11 月的 +10 小時（11/24 台北中午才發布）。手動 workflow_dispatch
// 則幾乎立刻開始，所以改由這支 Worker 每天準時呼叫 dispatch。
//
// 送出 inputs.trigger = "cloudflare"，daily.yml 的 guard 會把它當排程看待：
// 今天的日報已經存在就跳過。所以第二個時段（備援）與 GitHub 自己遲到的排程
// 都不會重複出稿。
//
// 權杖：GITHUB_TOKEN 是 fine-grained personal access token，只授權
// girlmagician/ai-daily-digest 一個 repo、只有 Actions: Read and write。
// 用 `npx.cmd wrangler secret put GITHUB_TOKEN` 設定，不寫進任何檔案。
//
// 刻意沒有 fetch handler：不提供任何公開網址可以觸發，只有排程能跑。

const REPO = "girlmagician/ai-daily-digest";
const WORKFLOW = "daily.yml";

export default {
  async scheduled(controller, env, ctx) {
    const res = await fetch(
      `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GITHUB_TOKEN}`,
          Accept: "application/vnd.github+json",
          "X-GitHub-Api-Version": "2022-11-28",
          "User-Agent": "ai-daily-digest-trigger",
        },
        body: JSON.stringify({ ref: "main", inputs: { trigger: "cloudflare" } }),
      },
    );
    // 成功是 204。失敗時丟出例外，Cloudflare 的 Cron 事件紀錄會標成失敗，
    // 在 dashboard → Workers → ai-digest-trigger → Logs 看得到原因
    // （權杖過期是 401）。
    if (res.status !== 204) {
      throw new Error(`dispatch 失敗：HTTP ${res.status} ${await res.text()}`);
    }
    console.log(`dispatch 成功（cron ${controller.cron}）`);
  },
};
