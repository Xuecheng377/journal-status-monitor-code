# 期刊稿件状态监控

这是一个用于自动监控期刊投稿状态的 Python 项目。它会登录投稿系统，读取稿件标题、稿件 ID、当前状态和投稿系统链接，并在状态变化时发送中文邮件通知。

项目现在使用 Cloudflare Worker 外部定时器触发 GitHub Actions：

- Cloudflare Worker 会在每个检查窗口的主时间点触发一次 GitHub `workflow_dispatch`
- 27 分、37 分是兜底唤醒点，只在同一窗口还没有触发成功时才补触发
- GitHub Actions 自带 `schedule` 已关闭，避免 Cloudflare 和 GitHub schedule 重复检查

## 当前功能

- 支持 IEEE ScholarOne / Manuscript Central 投稿系统
- 支持 Elsevier Editorial Manager 基础登录和表格解析
- 支持同一平台配置多组投稿账号，新增账号不会覆盖旧账号
- 自动识别 ScholarOne `STATUS` 栏中最上方的当前状态
- 能处理同一篇稿件被页面拆成多条状态历史的情况
- 能按规范化论文题目合并重复记录，避免旧状态重复出现在周报中
- 能识别并归档终态稿件，例如 `Accept`、`Accepted`、`Published`、`Rejected`、`Withdrawn`
- 中文 HTML 邮件通知，论文题目、稿件 ID、原始投稿状态保持英文原文
- 支持状态变化通知和每周一状态报告
- 每周一状态报告当天只发送一次，多次兜底触发不会重复发
- 状态数据保存到 `data/manuscripts.json`
- 周报发送记录保存到 `data/manuscripts.meta.json`
- 解析失败时在 `debug/` 落盘脱敏截图和 HTML，供动作日志定位；`debug/` 已被 `.gitignore` 忽略
- 抓取失败不再"绿色通过"：任一账号登录失败、解析为 0 条，都会让这次抓取判为失败
- 失败的一轮不再直接变红：它会在窗口的兜底分钟（:27 / :37）自己排队重跑，那一轮保持绿色，所以"十分钟后自愈"的 Cloudflare 拦截不会给你发任何邮件
- 只有窗口的**最后一次**唤醒仍然失败，运行才变红并发出告警邮件（手动触发、读不到 cron 时立即发，宁可多一封也不静默）
- 登录页是 JS 渲染的：`title='' password_inputs=0` 只是页面还在绘制，程序会等 `LOGIN_WAIT_SECONDS` 秒再判断，不再把它当"登录失败"
- 邮件发送失败不会丢数据：状态先不落盘，下一轮继续重试，直到通知真正发出
- 终态判定改为"覆盖词 + 关键词 - 否决词"：状态里带 `Reject` 就归档（含 `Reject and Resubmit`），`Required Reviews Completed`、`Awaiting Final Decision` 不会被误归档
- 状态解析失败时保留上一次状态并标记 stale，不再发出 `Under Review -> Unknown` 这类假告警
- 稿件以"平台 + 稿件 ID"作为主键，题目小幅修改不会重复建记录
- 登录前强制校验 URL：必须 https 且域名在 `ALLOWED_URL_HOSTS` 白名单内，避免把真实密码输入到任意页面

## 工作流程

1. 定时器触发 GitHub Actions。
2. GitHub Actions 运行 `monitor.py`。
3. 程序登录已配置的投稿系统。
4. 程序读取稿件信息并和历史记录比较。
5. 如果状态变化，发送中文状态变化通知。
6. 如果是每周一状态报告时段，发送当前活跃稿件报告。
7. 如果稿件进入终态，自动归档，后续不再作为活跃稿件持续提醒。

## 当前触发时间

Cloudflare Worker 使用“主触发 + 兜底唤醒”的时间窗口：17 分主触发，27 分、37 分兜底。窗口容差 20 分钟，所以 17–37 分都算同一个窗口；如果 17 分已经触发成功，27 分、37 分只会检查并跳过。

| 北京时间 | 模式 | 说明 |
| --- | --- | --- |
| 00:17 / 04:17 / 08:17 / 12:17 / 16:17 / 20:17 | `normal` | 每 4 小时一次，状态变化才发邮件 |
| 每周一 08:17 | `daily_report` | 周一这个窗口由周报接管，当天仍是 6 个窗口，不是 7 个 |
| 对应的 27、37 分 | 同上 | 兜底唤醒；同一窗口已触发则跳过 |

对应 UTC cron（北京时间减 8 小时），只有一条：

```yaml
- cron: '17,27,37 0,4,8,12,16,20 * * *'  # 北京 00/04/08/12/16/20 点，17 分主触发 + 27/37 兜底
```

小时、星期和窗口分钟都写在 `cloudflare-scheduler/wrangler.toml` 的 `[vars]` 里（`DAILY_REPORT_HOUR`、`WEEKLY_REPORT_DAY`、`NORMAL_HOURS`、`WINDOW_MINUTE`、`WINDOW_TOLERANCE_MINUTES`），Worker 直接读这些变量。修时间时只要用本地配置后台保存一次，cron 和触发逻辑会一起更新，不会再出现"改了点却静默不触发"。

Worker 还接受窗口内的迟到唤醒：`WINDOW_MINUTE` 起 `WINDOW_TOLERANCE_MINUTES`（默认 20）分钟内的触发都算同一窗口，这样 27、37 分两次兜底才真正有效；47 分及以后算下一个窗口。

周报不需要单独的 cron：08 点本来就在普通检查窗口里，Worker 按 `DAILY_REPORT_HOUR`/`WEEKLY_REPORT_DAY` 把周一 08:17 判成 `daily_report`。如果把周报改到普通窗口之外，本地配置后台会自动补一条带名字的 cron（如 `... * * MON`）——**不要写 `* * 1`，Cloudflare 的星期是 1=周日**。

GitHub Actions 的 `schedule` 可能延迟或丢触发，因此当前项目不再依赖 GitHub schedule。Cloudflare Worker 会调用 GitHub `workflow_dispatch`，GitHub Actions 页面中的事件来源会显示为 `workflow_dispatch`。

## GitHub Secrets

进入仓库：

`Settings` -> `Secrets and variables` -> `Actions` -> `New repository secret`

至少配置一个投稿平台账号。

### IEEE ScholarOne

| Secret | 说明 |
| --- | --- |
| `IEEE_EMAIL` | IEEE ScholarOne 登录邮箱 |
| `IEEE_PASSWORD` | IEEE ScholarOne 登录密码 |
| `IEEE_URL` | 期刊对应的 ScholarOne 投稿系统地址 |

如果需要新增第二、第三组 IEEE 账号，使用：

| Secret | 说明 |
| --- | --- |
| `IEEE_2_EMAIL` / `IEEE_2_PASSWORD` / `IEEE_2_URL` | 第二组 IEEE ScholarOne 账号 |
| `IEEE_3_EMAIL` / `IEEE_3_PASSWORD` / `IEEE_3_URL` | 第三组 IEEE ScholarOne 账号 |
| `IEEE_4_EMAIL` / `IEEE_4_PASSWORD` / `IEEE_4_URL` | 第四组 IEEE ScholarOne 账号 |
| `IEEE_5_EMAIL` / `IEEE_5_PASSWORD` / `IEEE_5_URL` | 第五组 IEEE ScholarOne 账号 |

### Elsevier Editorial Manager

| Secret | 说明 |
| --- | --- |
| `ELSEVIER_EMAIL` | Elsevier / Editorial Manager 登录邮箱 |
| `ELSEVIER_PASSWORD` | Elsevier / Editorial Manager 登录密码 |
| `ELSEVIER_URL` | 期刊对应的 Editorial Manager 地址 |

如果需要新增第二、第三组 Elsevier 账号，使用：

| Secret | 说明 |
| --- | --- |
| `ELSEVIER_2_EMAIL` / `ELSEVIER_2_PASSWORD` / `ELSEVIER_2_URL` | 第二组 Elsevier Editorial Manager 账号 |
| `ELSEVIER_3_EMAIL` / `ELSEVIER_3_PASSWORD` / `ELSEVIER_3_URL` | 第三组 Elsevier Editorial Manager 账号 |
| `ELSEVIER_4_EMAIL` / `ELSEVIER_4_PASSWORD` / `ELSEVIER_4_URL` | 第四组 Elsevier Editorial Manager 账号 |
| `ELSEVIER_5_EMAIL` / `ELSEVIER_5_PASSWORD` / `ELSEVIER_5_URL` | 第五组 Elsevier Editorial Manager 账号 |

### 邮件通知

| Secret | 说明 |
| --- | --- |
| `EMAIL_SENDER` | 发件邮箱 |
| `EMAIL_PASSWORD` | SMTP 授权码或应用专用密码 |
| `EMAIL_RECEIVER` | 收件邮箱，多个收件人用英文逗号分隔 |
| `SMTP_SERVER` 或 `SMTP_HOST` | 可选，SMTP 服务器地址 |
| `SMTP_PORT` | 可选，SMTP 端口，常见为 `465` 或 `587` |

常见邮箱会自动推断 SMTP 服务器，例如 QQ 邮箱、163 邮箱、Gmail、Outlook 等。

## Cloudflare 外部定时器

外部定时器代码位于：

`cloudflare-scheduler/`

已包含：

| 文件 | 说明 |
| --- | --- |
| `cloudflare-scheduler/worker.js` | Cloudflare Worker 代码 |
| `cloudflare-scheduler/wrangler.toml` | Worker 名称、变量和 cron 配置 |
| `cloudflare-scheduler/README.md` | Cloudflare 部署说明 |

Cloudflare Worker 需要一个 GitHub token，保存在 Cloudflare Secret：

```text
GITHUB_TOKEN
```

该 token 至少需要对本仓库具备：

- `Actions: Read and write`

如果要更新仓库文件，还需要 `Contents: Read and write`，但 Worker 触发 workflow 只需要 Actions 权限。

部署命令：

```bash
cd cloudflare-scheduler
wrangler secret put GITHUB_TOKEN
wrangler deploy
```

当前 Worker 名称：

```text
journal-status-monitor-scheduler
```

## 本地可视化配置后台

项目提供一个只在本机运行的配置网页，位置：

```text
local-config-ui/
```

启动：

```powershell
cd local-config-ui
.\start.ps1
```

然后打开：

```text
http://127.0.0.1:8976
```

这个网页可以填写投稿平台、邮件通知、检查时间和归档规则。保存时，投稿系统密码和邮箱授权码会写入 GitHub Secrets，不会写入仓库明文文件。GitHub token 和 Cloudflare token 只用于当前请求，默认不保存到本地文件。

页面分为“已有旧配置”和“新增投稿账号”两块：

- “已有旧配置”对应 `IEEE_EMAIL` / `ELSEVIER_EMAIL` 这一组最早的 Secret，留空会保持不变。
- “新增投稿账号”会写入 `IEEE_2_*`、`IEEE_3_*` 或 `ELSEVIER_2_*`、`ELSEVIER_3_*` 这样的新槽位，不会替换已有账号。
- “账号管理”可以查看已配置槽位、修改指定槽位，或删除某个槽位对应的 `EMAIL`、`PASSWORD`、`URL` 三个 Secret。

页面会自动读取本地 `cloudflare-scheduler/wrangler.toml` 并预填当前检查时间。GitHub Secrets 不能读回明文，但填写 GitHub token 后可以检查哪些 Secret 已存在；已存在的密码或授权码输入框可以留空，保存时会保持旧 Secret 不变。

"配置预览"里的密码和授权码一律显示为 `********`，服务端根本不会把它们回传浏览器。

点击"部署 Worker"时使用 `npx --no-install wrangler deploy`，不会临时联网拉取未固定版本的 wrangler。首次使用前请在该目录执行一次：

```powershell
cd cloudflare-scheduler
npm install --save-dev wrangler
```

页面只监听 `127.0.0.1:8976`，并校验 `Host` 请求头，防止 DNS rebinding 从其他网页访问本地后台。

旧 Secret 的邮箱、网址、密码都不能从 GitHub 读回明文。通过本地后台保存或修改过的投稿账号，会把邮箱和网址作为非敏感元数据保存在本机 `local-config-ui/data/`，用于页面查看；密码不会保存到本地。

## 运行模式

| 模式 | 作用 |
| --- | --- |
| `test` | 只发送测试邮件，用于确认邮箱配置 |
| `normal` | 正常监控，只有状态变化时发邮件 |
| `daily_report` | 发送当前活跃稿件每周一状态报告 |

手动运行：

1. 打开仓库的 `Actions` 页面。
2. 选择 `Journal Status Monitor`。
3. 点击 `Run workflow`。
4. 选择 `test`、`normal` 或 `daily_report`。

第一次配置完成后，建议先运行 `test`。

## 本地运行

安装依赖：

```bash
pip install -r requirements.txt
```

运行测试邮件：

```bash
python monitor.py --mode test
```

正常监控：

```bash
python monitor.py --mode normal
```

每周一状态报告：

```bash
python monitor.py --mode daily_report
```

## 终态归档和周报防重复

默认情况下，稿件达到终态后会自动归档。归档稿件仍保存在 `data/manuscripts.json`，但不会继续出现在活跃稿件周报中。

终态判定的顺序是"覆盖词 → 否决词 → 关键词"：

- 覆盖词默认是 `reject`，**优先级最高**：状态里只要带 `reject` 就算被拒并归档，包括 `Reject and Resubmit`、`Reject with Possibility for Resubmission`
- 关键词默认包含 `accept`、`accepted`、`published`、`online`、`reject`、`rejected`、`withdrawn`、`transferred`、`declined to publish`
- 否决词默认包含 `required reviews completed`、`reviews completed`、`awaiting`、`in process`、`in progress`、`resubmit`、`resubmission`、`decline to review`、`under review`、`with editor`、`pending`、`decision`

命中否决词的不会归档（例如 `Required Reviews Completed`、`Decision in Process`），但只要命中覆盖词就归档。`Accept (02-Jan-2026)` 这种带日期的状态会先去掉括号内的日期再判断，因此仍能正确归档。

**归档不等于静默**：新稿件首次被记录时一律发邮件（含首见即终态的），之后再按终态规则归档；只有 `draft`、`incomplete`、`unsubmitted` 这类还没投出去的草稿不单独提醒。

如果某篇稿件被误归档，可以撤销：

```bash
python monitor.py --unarchive 26-TIE-0001
python monitor.py --unarchive "part of the title"
```

撤销后该稿件会重新参与监测，并记录 `unarchived_at` 时间。撤销过的记录会带上 `archive_suppressed: true`：后续即使状态里再次出现终态关键词，也不会被自动归档回去，否则撤销就没意义了。想重新开启自动归档，删掉该记录里的 `archive_suppressed` 字段即可。

每周一状态报告发送成功后，会在 `data/manuscripts.meta.json` 记录当天已发送。当天后续兜底触发会跳过周报，避免重复发送。若当前没有活跃稿件，周报不会被标记为"已发送"，避免当天报告被静默吞掉。

相关环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ARCHIVE_TERMINAL` | `true` | 是否自动归档终态稿件 |
| `INCLUDE_ARCHIVED_IN_REPORT` | `false` | 周报是否包含已归档稿件 |
| `TERMINAL_STATUS_KEYWORDS` | 内置列表 | 自定义终态关键词，英文逗号分隔 |
| `TERMINAL_STATUS_OVERRIDES` | `reject` | 覆盖词，命中即归档，优先于否决词 |
| `TERMINAL_STATUS_VETOES` | 内置列表 | 自定义否决词，命中即不归档 |
| `FIRST_SEEN_SILENT_STATUS_KEYWORDS` | `draft,incomplete,unsubmitted` | 首次发现时不单独提醒的状态（草稿类） |
| `ALLOW_EMPTY_ACCOUNTS` | 空 | 允许"登录成功但解析到 0 篇"的账号名，默认视为解析故障并让运行失败 |
| `FAIL_ON_EMPTY` | `true` | 一条稿件都没抓到是否判定为失败 |
| `NOTIFY_ON_FIRST_SEEN` | `true` | 首次记录到稿件时是否发通知 |
| `ALLOWED_URL_HOSTS` | 常见出版社域名 | 允许输入密码的域名后缀，英文逗号分隔 |
| `REQUIRE_HTTPS` | `true` | 是否强制登录地址使用 https |
| `LOG_FULL_TITLES` | `false` | 动作日志是否打印完整论文题目 |
| `STEALTH_MODE` | `true` | 是否传 `--disable-blink-features=AutomationControlled` 并去掉 `navigator.webdriver` 与 HeadlessChrome 标识 |
| `ALERT_COOLDOWN_HOURS` | `6` | 同类失败告警的最小间隔，防止每 3 小时刷一次邮箱 |
| `ALERT_ON_LAST_ATTEMPT_ONLY` | `true` | 只在窗口最后一次唤醒（17/27/37 中的 37）仍失败时才发告警邮件；设为 `false` 回到"第一次失败就发" |
| `WINDOW_CRON_FILE` | `cloudflare-scheduler/wrangler.toml` | 从 Worker 的 cron 里读出唤醒分钟表，避免代码和调度各写一份 |
| `CHALLENGE_WAIT_SECONDS` | `20` | 遇到人机校验页时最多等多久（等自动通过） |
| `ACCOUNT_RETRIES` | `1` | 单个账号失败后重试次数，重试用全新会话 |

## 数据和隐私

**代码仓可以公开，状态不能公开。** `data/manuscripts.json` 里有论文题目、稿件 ID、审稿状态和投稿链接，所以它不在代码仓里，而是放在一个**私有状态仓**（默认 `Xuecheng377/data_storage`）的 `data/manuscripts.json`。

每次运行：

1. `Restore state baseline` 步用 `STATE_REPO_TOKEN` 把私有状态仓 clone 到工作区的 `state/`（`state/` 已进 `.gitignore`，不会被提交回代码仓）；
2. `DATA_FILE=state/data/manuscripts.json`，监控照常读写；
3. 有真实变化时，`Commit state changes` 步把改动提交并推回**状态仓**的 `main`，代码仓里不再出现稿件数据。

需要的两个配置（代码仓 Settings → Secrets and variables → Actions）：

| 名称 | 类型 | 值 |
| --- | --- | --- |
| `STATE_REPO_TOKEN` | Secret | 能读写私有状态仓的 PAT（Contents: Read and write） |
| `STATE_REPO` | Variable | 状态仓全名，如 `Xuecheng377/data_storage`（不填则用默认值） |

`STATE_REPO_TOKEN` 缺失时工作流会**直接失败**，而不是退回把数据写进代码仓。

`REQUIRE_BASELINE=true`（工作流已开）：抓到了稿件但基线文件不存在或损坏时，运行失败并保持状态不变。否则"文件没了"会被当成"所有稿件都是新稿"，发出一封把全部在投稿件报成新稿的邮件。

`REDACT_DEBUG_ARTIFACTS=true`（工作流已开）：公开仓的 Actions artifact 对所有人可见，所以上传的快照只包含哈希后的 ID/题目摘要和状态，不再上传带标题的截图与原始 HTML。排查"新稿有没有出现"仍可用——按哈希对比两次快照即可；需要完整页面时在本地用 `REDACT_DEBUG_ARTIFACTS=false` 跑一次。

本地配置后台同样从状态仓读取稿件列表（`STATE_REPO` 环境变量可覆盖），"恢复监测"也会提交到状态仓。

历史上数据曾提交进公开仓库的 `main` 分支，并产生过 fork：**改走状态仓只是止损，历史里的旧版本、旧日志和 fork 里的副本不会自动消失**，需要的话得单独清理。

动作日志默认只打印稿件 ID 和题目摘要（`title#12345, 26 chars`），不打印完整题目。需要排查时可临时设置 `LOG_FULL_TITLES=true`。

不要把以下内容写进代码或 README：

- 投稿系统密码
- 邮箱授权码
- GitHub Personal Access Token
- Cloudflare API Token
- 任何个人隐私或稿件隐私信息

这些内容应放在 GitHub Secrets 或 Cloudflare Secrets 中。

## 常见问题

### 为什么 GitHub schedule 没按时运行？

GitHub Actions 的 `schedule` 不保证准点，可能延迟，也可能在高负载时丢触发。当前项目已经关闭 GitHub schedule，改由 Cloudflare Worker 外部定时器触发 `workflow_dispatch`。

### 为什么 27 分、37 分没有出现新的 Actions 运行？

这是正常行为。它们是兜底唤醒点：如果同一检查窗口已经由 17 分触发过，Worker 会跳过，不再创建新的 GitHub Actions 运行。

### 为什么稿件接收后不再出现在周报？

`Accept` 属于终态。程序会自动归档终态稿件，默认不再把它列入活跃稿件周报。

### 稿件状态识别不正确怎么办？

投稿系统页面结构可能变化。运行失败时会在 `debug/` 落盘脱敏后的截图和 HTML（密码输入框的值已替换为 `***`），动作日志会列出这些文件名。`debug/` 在 `.gitignore` 里，不会被提交。

### 运行显示红色是不是说明监控坏了？

现在红色代表"确实有问题"：账号登录失败、页面解析为 0 条、邮件发不出去，都会让这次抓取判为失败。

一轮抓取失败不会马上变红：它会保持运行、睡到本窗口的下一个兜底分钟，自己排队重跑一次；重跑成功，这一窗口就算正常，你什么邮件都收不到。只有窗口的**最后一次**唤醒（:37）仍然失败，运行才变红并发告警邮件。

所以规律是：**偶尔拦截 → Actions 里能看到失败日志但运行是绿的；真的连续三次都失败 → 一个红叉 + 一封邮件**，两种都不会静默。

### 邮件发不出去，状态变化会丢吗？

不会。程序先发通知、成功后才落盘新的状态。发信失败时数据保持旧值，下一轮窗口会再次检出同一次变化并重试。

### 为什么某篇稿件始终不再提醒？

先确认它是否被归档：`data/manuscripts.json` 里该记录的 `archived` 为 `true`。用 `python monitor.py --unarchive <稿件ID>` 可以恢复监测。

### 日志里出现 `Just a moment...` 或 `title='Just a moment...'`

这是出版社前置的 Cloudflare 人机校验页，说明这次没有真正打开登录页。程序会等 `CHALLENGE_WAIT_SECONDS`（默认 40 秒）看它是否自己消失，然后换全新会话重试，每个账号最多试 `ACCOUNT_RETRIES + 1` 次。

- 这一轮抓取失败时会**睡到本窗口的下一个兜底分钟**（:27 或 :37）自己排队重跑，那一轮保持绿色，不给你发邮件
- 只有窗口最后一次唤醒仍然失败，运行才变红并发告警邮件，按 `ALERT_COOLDOWN_HOURS` 冷却
- 登录页是 JS 渲染的，`title='' password_inputs=0` 只是还没画完：程序会等 `LOGIN_WAIT_SECONDS`（默认 30 秒），不再把这当成"登录失败"
- workflow 现在用 `xvfb-run` 以"有窗口"的 Chrome 运行，比 headless 更容易通过校验
- 连续多次登录会提高被挑战的概率；把检查频率降到每天 2-3 次会明显更稳
- GitHub 托管 runner 的 IP 属于数据中心网段，被挑战是常态，这一点无法只靠代码解决

### 为什么 workflow 不再使用 `actions/checkout` 等 action？

为了避免 GitHub 在下载 action 压缩包时偶发失败，当前 workflow 已尽量改为直接使用 runner 自带的 `git`、`python3` 和标准库完成 checkout、运行、提交数据和清理旧运行记录。这样也不会再触发 Node.js 20 action 弃用警告。

## 项目文件说明

| 文件 | 说明 |
| --- | --- |
| `monitor.py` | 登录投稿系统、抓取和解析稿件状态 |
| `window_schedule.py` | 从 Worker 的 cron 读出唤醒分钟，决定"还有没有下一次机会" |
| `retry_window.py` | 失败的一轮睡到下一个兜底分钟、自己排队重跑，让这一轮保持绿色 |
| `notification.py` | 生成并发送中文邮件、失败告警邮件 |
| `storage.py` | 保存状态、比较变化、归档、撤销归档和周报防重复 |
| `config.py` | 读取配置、终态判定、URL 白名单 |
| `.github/workflows/monitor.yml` | GitHub Actions 工作流（含单元测试步骤） |
| `cloudflare-scheduler/` | Cloudflare 外部定时器（`node --test` 覆盖窗口逻辑） |
| `local-config-ui/` | 本机配置后台（`node --test` 覆盖配置生成） |
| `tests/` | Python 单元测试，`python -m unittest discover -s tests -t .` |
| `tools/check_login.py` | 手动排查登录问题的脚本，不属于自动测试 |
| `.env.example` | 本地环境变量示例 |
| `DEPLOYMENT_GUIDE.md` | 早期部署说明 |

## 本地测试

```bash
python -m unittest discover -s tests -t .          # Python
cd local-config-ui && node --test                  # 配置后台
cd cloudflare-scheduler && node --test             # Worker 触发窗口
```

CI 在每次 `normal` / `daily_report` 运行前都会先跑 Python 单元测试，测试失败则不会去登录投稿系统。
