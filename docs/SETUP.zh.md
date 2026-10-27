# 部署指南（中文）

把 Apple Watch 的健康数据自动送进 Claude：随时在 Claude 里问，每天早上收到一份推送到 Telegram / 微信的日报。Claude 账号不需要登录服务器。

```
Apple Watch → iPhone「健康」
      │  Health Auto Export（Premium）每小时上传：健康指标 CSV + 运动 JSON
      ▼
https://你的域名/ingest            你的服务器（本仓库）
      │
      ├─ https://你的域名/mcp/<令牌>   Claude 自定义连接器：随时问答
      └─ https://你的域名/api/…        Claude 云端定时任务：每天读数据、写日报
                                        → 服务器存档并推送到 Telegram / 微信
```

## 0. 准备

- 一台 Debian 12/13 或 Ubuntu 22.04+ 的服务器（1 核 1 GB 就够），有 root 权限。
- 一个域名，A 记录指向服务器 IP。用 Cloudflare 的话选 **仅 DNS（灰色云朵）**。主域名已经有网站的话，加一个子域名（例如 `health`）专门给它用。先加好记录再运行安装命令。
- iPhone 上的 [Health Auto Export](https://apps.apple.com/app/id1115567069)，需要 Premium（自动上传要用）。
- 推送渠道，任选：
  - **Telegram（推荐，免费不限条数）**：在 Telegram 里找 @BotFather，发 `/newbot`，按提示起名，拿到形如 `123456789:AA...` 的机器人令牌。然后打开你的新机器人，随便发一句话（安装脚本靠这条消息找到你的对话）。
  - **微信**：[Server酱](https://sct.ftqq.com) 的 SendKey，或 [PushPlus](https://www.pushplus.plus) 的 token。

## 1. 服务器：一条命令安装

用 root 登录服务器，粘贴（把域名、时区、机器人令牌换成你的）：

```bash
curl -fsSL https://raw.githubusercontent.com/qingchu9909/apple-health-mcp/main/deploy/install.sh \
  | DOMAIN=health.example.com USER_TZ=Asia/Shanghai TELEGRAM_BOT_TOKEN=123456789:AA... bash
```

脚本会：建一个专用系统用户、装好 Python 3.12 和依赖、生成三个随机令牌、注册两个开机自启的后台服务、配置 HTTPS（443 端口空闲时自动装 Caddy 并申请证书，且不占用 80 端口；已有 Caddy 或 Nginx 时接入现有的。科技lion 脚本那种跑在 Docker 里的 Nginx 也认得：在它的 `/home/web/conf.d` 里加一个独立站点，用 acme.sh 申请证书并自动续期，不动原有站点），然后逐项自检，最后把要填到手机和 Claude 里的配置发到你的 Telegram / 微信。

- 用微信就把 `TELEGRAM_BOT_TOKEN=...` 换成 `SERVERCHAN_SENDKEY=...` 或 `PUSHPLUS_TOKEN=...`，也可以都填。
- 443 被别的程序（比如代理）占用时，脚本不会动它，会提示你；可以加 `HTTPS_PORT=8443` 换一个端口（此时 80 端口要空着用来申请证书）。
- 可以放心重复运行：令牌只在第一次生成，之后保持不变。
- 以后再看配置：`bash /opt/health/app/deploy/show-config.sh`
- 更新代码：`bash /opt/health/app/deploy/update.sh`

## 2. iPhone：Health Auto Export

**先把这个 App 的语言改成英文，并一直保持英文。** App 用中文时，导出的表格连列名也是中文（「日期/時間」「步數」…），服务器只认英文列名，整份都会读不进去。在 iPhone「设置 → App → Health Auto Export → 语言」选 English；没有「语言」这一项的话，先到「设置 → 通用 → 语言与地区」加入 English，主要语言保持中文。手机其他地方不受影响。

然后打开 App → 侧边栏 **Automations** → **New Automation**，建两个自动化。两个都是 **REST API** 类型，URL 和 Header 一样：

| 项目 | 填写 |
| --- | --- |
| URL | `https://你的域名/ingest` |
| HTTP Headers | 名 `Authorization`，值 `Bearer <HEALTH_TOKEN>`（脚本推送给你的那一行，注意 Bearer 后面有一个空格） |

**自动化一：健康指标**

| 项目 | 选择 |
| --- | --- |
| Data Type | Health Metrics |
| Select Health Metrics | 全选 |
| Export Format | CSV |
| Time Grouping | Days（服务器按每天一行来读；旧版 App 另有 Summarize Data 开关，要打开） |
| Date Range | **Default**（前一整天 + 今天到目前为止。每次都会重发完整的昨天，服务器自动去重，漏掉的同步也能补上） |
| Sync Cadence | 每 1 小时 |

**自动化二：运动记录**

| 项目 | 选择 |
| --- | --- |
| Data Type | Workouts |
| Include Workout Metrics | 开 |
| Include Route Data | 关（GPS 轨迹很大，用不上） |
| Time Grouping | Minutes |
| Export Format | JSON |
| Export Version | Version 2 |
| Date Range | Default |
| Sync Cadence | 每 1 小时 |

建好后：

1. 在每个自动化的设置页点 **Manual Export**，选一个较长的时间段（比如近 3 个月）导一次历史数据，这样一开始就有基线可比。
2. 把 Health Auto Export 的 **Automations 小组件**加到主屏幕，并在「设置 → 通用 → 后台 App 刷新」里打开它。iPhone 锁屏时任何 App 都读不到健康数据，所以自动化只在手机解锁期间运行；充电时同步最积极。

## 3. Claude：随时问答（自定义连接器）

claude.ai（网页或手机 App）→ **设置 → 连接器 → 添加自定义连接器**：

| 项目 | 填写 |
| --- | --- |
| 名称 | Apple Health |
| URL | `https://你的域名/mcp/<MCP_PATH_TOKEN>`（脚本推送给你的那一行） |
| 认证 | 无（令牌就在 URL 里，这个 URL 等同密码，别外传） |

然后在对话里打开这个连接器，直接问：「我最近一周睡得怎么样？」「今天适合练强度吗？」「上次游泳的心率曲线」「把今天的建议发到我 Telegram」。

## 4. Claude：每日日报（云端定时任务）

云端定时任务用不了自定义连接器，所以它通过 HTTPS 接口直接读数据、交日报：

1. 新建（或编辑）一个 Claude Code 云端环境：
   - 网络访问里，把你的域名加进允许列表（Allowed domains）。
   - 环境变量：`HEALTH_API_BASE=https://你的域名`，`HEALTH_API_TOKEN=<REPORT_API_TOKEN>`。
2. 用这个环境创建定时任务：每天早上你起床、解锁过手机之后（例如 09:00）运行，提示词用 [`routines/daily-report.zh.prompt.md`](../routines/daily-report.zh.prompt.md)，把最后的「用户背景」换成你自己的情况。
3. 手动触发一次，确认手机收到日报。

## 日常维护

| 想做的事 | 命令（服务器上，root） |
| --- | --- |
| 看服务状态 | `systemctl status health-ingest health-mcp` |
| 看日志 | `journalctl -u health-mcp -n 100` |
| 看手机最近传上来没有、收到了哪些数据 | `bash /opt/health/app/deploy/status.sh`（被拒的上传会写明原因） |
| 看历史日报 | `ls /opt/health/reports` |
| 重新显示配置 | `bash /opt/health/app/deploy/show-config.sh` |
| 更新到最新代码 | `bash /opt/health/app/deploy/update.sh` |
| 换掉某个令牌 | 删掉 `/opt/health/.env` 里对应那一行，重跑 `update.sh`，再到手机/Claude 里换成新值 |

## 常见问题

| 现象 | 原因和办法 |
| --- | --- |
| Health Auto Export 报 401 | Header 值不对。运行 `bash /opt/health/app/deploy/status.sh` 看原因：缺少 Header 是名字没填成 `Authorization`；「got N characters」是令牌复制得不完整；提到 REPORT_API_TOKEN / MCP_PATH_TOKEN 是贴成了另外两组里的令牌 |
| Claude 说没有健康数据，或者只查得到运动记录 | Health Auto Export 用中文时导出的列名也是中文，服务器读不了（`status.sh` 会显示「Claude 读不了」）。把这个 App 的语言改成 English（见第 2 节），再用 Manual Export 把这些天重新导出一次 |
| 一直没有新数据 | 自动化只在手机解锁时运行；检查后台 App 刷新、加小组件，在 App 的 Activity Logs 里看报错 |
| 日报说「昨晚睡眠还没同步」 | 定时任务比你解锁手机还早，把运行时间往后调 |
| 连接器显示 0 个工具 | URL 里的令牌抄错，或服务器上 `health-mcp` 没在运行 |
| 定时任务报 403 | 云端环境的允许列表里没有你的域名 |
| 推送失败 | Telegram：先给机器人发过消息吗？微信：SendKey 是否正确、免费额度是否用完 |
| 证书申请失败 | 域名刚加的话等几分钟再重跑；Cloudflare 必须是灰色云朵（仅 DNS）；80 端口要能从外网访问 |

数据存在你自己的服务器上，三个令牌分别控制「上传数据」「问答读取」「日报接口」，任何一个泄露只需单独更换那一个。
