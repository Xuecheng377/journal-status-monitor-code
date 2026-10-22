# Cloudflare 外部定时器

这个 Worker 用 Cloudflare Cron Triggers 调用 GitHub `workflow_dispatch`，用于替代或兜底 GitHub Actions 自带的 `schedule`。

## 触发时间

Cloudflare cron 使用 UTC 时间。本项目 Worker 会换算成北京时间，并只在以下时间触发 GitHub workflow：

| 北京时间 | 触发模式 |
| --- | --- |
| 00:17、04:17、08:17、12:17、16:17、20:17 | `normal` 主触发（每 4 小时一次） |
| 每周一 08:17 | `daily_report`，同一个窗口由周报接管 |
| 对应的 27、37 分 | 兜底唤醒，若同一时间窗已触发则跳过 |

窗口容差 20 分钟：17 分主触发，27、37 分兜底都在窗口内；47 分及以后算下一个窗口。

Worker 会查询最近的 GitHub workflow_dispatch 运行记录，同一北京时间小时窗口最多触发一次。周报不需要单独的 cron：08 点本来就落在普通检查窗口里，Worker 用 `DAILY_REPORT_HOUR` 和 `WEEKLY_REPORT_DAY` 判断这一天这一刻是不是周报，所以周一 08:17 由周报接管，当天仍然是 6 个窗口。只有把周报小时改到普通检查窗口之外时，配置后台才会额外生成一条带星期名的 cron。**注意 Cloudflare 的星期是 1=周日…7=周六**，写 `* * 1` 会落在周日。

## GitHub Token 权限

建议新建一个专用 fine-grained GitHub token，只给这个仓库授权：

- Repository access: `Xuecheng377/journal-status-monitor`
- Permissions: `Actions: Read and write`
- 如果 GitHub 页面要求 Metadata 权限，保持默认只读即可

不要使用已经公开到聊天记录里的旧 token。

## 部署步骤

安装 Wrangler：

```bash
npm install -g wrangler
```

登录 Cloudflare：

```bash
wrangler login
```

进入本目录：

```bash
cd cloudflare-scheduler
```

设置 GitHub token 为 Cloudflare secret：

```bash
wrangler secret put GITHUB_TOKEN
```

部署：

```bash
wrangler deploy
```

部署后，Cloudflare 会按照 `wrangler.toml` 中的 cron 自动触发 Worker。

## 验证

部署完成后，可以打开 Worker 的访问地址。它会返回当前北京时间、如果此刻是定时窗口会触发的模式，以及目标仓库信息。

也可以在 Cloudflare Dashboard 的 Worker 日志里查看：

- `triggered GitHub workflow_dispatch with mode=daily_report, window=...`
- `skipped GitHub workflow_dispatch with mode=normal, window=...`

GitHub Actions 页面中，对应运行的 `event` 会显示为 `workflow_dispatch`。
