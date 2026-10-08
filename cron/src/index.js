// membership-cron
// 毎日決まった時刻に、GitHub Actions の daily-update を実行するよう GitHub に依頼する Worker。
// (GitHub Actions の定期実行は混雑時に遅れたり実行されなかったりするため、
//  Cloudflare の Cron Trigger から「Run workflow」と同じ操作 (workflow_dispatch) を行う)
//
// 必要な設定 (wrangler.jsonc と、Cloudflare に登録するシークレット):
//   GITHUB_REPO    … "ユーザー名/リポジトリ名" (wrangler.jsonc の vars)
//   WORKFLOW_FILE  … "daily.yml" (wrangler.jsonc の vars)
//   GITHUB_TOKEN   … GitHub のトークン (シークレット。リポジトリには書かない)

async function dispatch(env) {
  const url = `https://api.github.com/repos/${env.GITHUB_REPO}/actions/workflows/${env.WORKFLOW_FILE}/dispatches`;
  const res = await fetch(url, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "membership-cron",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ ref: "main", inputs: { limit: "0" } }),   // limit 0 = 全チャンネル
  });
  if (res.status !== 204) {
    // 失敗は Cloudflare のログ (Workers → membership-cron → ログ) に残る
    throw new Error(`GitHub への実行依頼に失敗しました: HTTP ${res.status} ${await res.text()}`);
  }
  console.log("GitHub Actions (daily-update) の実行を依頼しました");
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatch(env));
  },
};
