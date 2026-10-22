# 本地可视化配置后台

这个目录提供一个只在本机运行的配置网页，用来管理期刊状态监控项目的投稿平台、邮件通知、检查时间和高级选项；同时可以查看"监控到底有没有在跑"和"现在在盯哪些稿件"。

## 页面结构

左侧是七个页签，前两个是只读视图，后面五个是配置：

| 页签 | 作用 | 需要的权限 |
| --- | --- | --- |
| 运行状态 | 顶部健康条（成功 / 少跑 / 失败）、今天计划与已完成次数、下次自动检查时间、最近 20 次运行；可一键"立即检查一次" | Actions 读 + Contents 读（触发还需 Actions 写） |
| 稿件清单 | 在监控 / 已归档的稿件，含状态、归档时间；已归档的可"恢复监测" | Contents 读写 |
| 投稿账号 | 已有账号槽位、新增账号、各组 Secret 是否存在 | Secrets 读写 |
| 邮件通知 | 发件人、SMTP、收件人 | Secrets 读写 |
| 检查时间 | 每周报告星期/小时、每天检查小时、主触发分钟；会写回 `wrangler.toml` | Contents 读写 + 本地文件写 |
| 高级设置 | 终态归档、周报含归档、新稿必提醒、空抓取判失败、失败告警冷却 | Secrets 读写 |
| 保存与部署 | 写入 Secrets、可选部署 Cloudflare Worker、触发测试邮件 | 同上 + Cloudflare token |

"运行状态"和"稿件清单"都走 `POST /api/status`、`/api/manuscripts`，只用 token 读，不写任何东西。

## 启动

在 PowerShell 中运行：

```powershell
cd local-config-ui
.\start.ps1
```

然后打开：

```text
http://127.0.0.1:8976
```

也可以直接运行：

```powershell
npm install
npm start
```

## 桌面快捷方式

双击桌面上的 **「期刊监控配置」** 就会：先看后台是否已经在跑 → 没跑就在后台静默启动 → 等端口就绪 → 自动打开浏览器。再点一次不会启动第二个服务，只会把页面调出来。

- 第一次运行会自动 `npm install`（只有缺依赖时才装）
- 想关掉后台：双击 **「停止监控后台」**
- 桌面上另有「论文监控」指向 `E:\论文抓取`，是另一个项目，不受影响

快捷方式指向本目录的 `launch.ps1`，所以**移动这个文件夹后**要先删掉旧快捷方式，再运行：

```powershell
powershell -ExecutionPolicy Bypass -File install-shortcut.ps1
```

启动日志写在 `%TEMP%\journal-status-ui.log`，启动失败时弹窗也会提示这个路径。

## 健康状态怎么算

- **成功**：最近一次运行成功，且今天到点的时间窗都跑过
- **少跑**：最近一次成功，但今天有到点却没有任何运行的点（Worker 没唤醒、token 过期等）
- **失败**：最近一次运行失败（含连续失败次数），或运行还在进行中
- **还没看过**：页面还没读取过状态；填入 token 后会自动刷新

## 安全边界

- 服务只监听 `127.0.0.1`，只允许本机访问。
- GitHub token 和 Cloudflare token 只用于当前保存请求，不写入项目文件。
- 投稿系统密码、SMTP 授权码会写入 GitHub Secrets，不会写入仓库明文文件。
- `cloudflare-scheduler/wrangler.toml` 会被更新，用来保存检查时间。

## GitHub Token 权限

保存配置需要 GitHub token 至少具备：

- Repository access: `Xuecheng377/journal-status-monitor`
- Actions: Read and write
- Secrets: Read and write
- Contents: Read and write 如果后续需要自动推送更多文件

当前本地后台主要通过 GitHub API 更新 Actions Secrets，并可选择触发测试 workflow。

## Cloudflare Token 权限

如果勾选“保存后部署 Cloudflare Worker”，需要填写 Cloudflare API Token。该 token 需要能部署当前 Worker：

```text
journal-status-monitor-scheduler
```

如果不勾选部署，网页只会更新本地 `cloudflare-scheduler/wrangler.toml`，不会让线上调度立即生效。

## 页面功能

- 运行状态：健康条、今天计划/已完成、下次检查时间、最近运行列表、立即检查一次
- 稿件清单：在监控与已归档稿件、归档原因，可恢复监测
- 投稿平台：IEEE ScholarOne、Elsevier Editorial Manager；支持新增多组账号
- 账号管理：查看已配置槽位、修改指定槽位、删除指定槽位
- 邮件通知：发件邮箱、SMTP 授权码、收件人、SMTP 服务器和端口
- 检查时间：每周报告时间、普通状态检查时间
- 高级设置：终态归档、周报是否包含归档稿件、新稿必提醒、空抓取判失败、失败告警冷却
- 保存部署：写入 GitHub Secrets、可选部署 Cloudflare、可选触发测试邮件 workflow

## 新增账号规则

页面里的“已有旧配置”区域对应最早的一组 Secret：

- `IEEE_EMAIL` / `IEEE_PASSWORD` / `IEEE_URL`
- `ELSEVIER_EMAIL` / `ELSEVIER_PASSWORD` / `ELSEVIER_URL`

如果你要新增论文或新增投稿账号，请使用“新增投稿账号”区域。新增内容会写入下一个空槽位，例如：

- `IEEE_2_EMAIL` / `IEEE_2_PASSWORD` / `IEEE_2_URL`
- `ELSEVIER_2_EMAIL` / `ELSEVIER_2_PASSWORD` / `ELSEVIER_2_URL`

新增账号不会覆盖 `IEEE_EMAIL` 或 `ELSEVIER_EMAIL` 这一组旧配置。当前每个平台最多支持 5 组账号。

## 旧配置读取

页面会自动读取本地 `cloudflare-scheduler/wrangler.toml`，并预填当前检查时间。

GitHub Secrets 不能被读回明文，这是 GitHub 的安全限制。填写 GitHub token 后，可以点击“检查已配置 Secret”查看哪些 Secret 已存在。对于已经存在的密码、授权码等敏感项，输入框可以留空；保存时会保持旧 Secret 不变。只有你填写新值时，才会覆盖对应 Secret。

## 账号管理

“账号管理”区域会根据 GitHub Secrets 列出当前配置的投稿账号槽位，例如 `IEEE`、`IEEE_2`、`ELSEVIER_2`。

- 查看：显示槽位、平台、Secret 是否完整；密码永远不会显示。
- 修改：点击“修改”会打开一个固定槽位的编辑表单，保存后覆盖该槽位。
- 删除：点击“删除”会删除该槽位对应的 `EMAIL`、`PASSWORD`、`URL` 三个 GitHub Secret。

GitHub Secrets 不能读取明文，所以旧账号的邮箱和网址无法自动显示。通过本地后台保存或修改过的账号，会把邮箱和网址作为非敏感元数据保存在 `local-config-ui/data/`，仅用于本地查看；密码不会保存到本地。

## 测试

```powershell
npm test
```
