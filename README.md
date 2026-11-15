<div align="center">
  <br>
  <img src="https://img.shields.io/badge/python-3.11+-blue?style=flat-square&logo=python" alt="Python">
  <img src="https://img.shields.io/badge/vnpy-4.4.0-blue?style=flat-square" alt="vnpy">
  <img src="https://img.shields.io/badge/vnpy__ctastrategy-1.4.1-blue?style=flat-square" alt="vnpy_ctastrategy">
  <img src="https://img.shields.io/badge/vnpy__ctp-6.7.11.4-blue?style=flat-square" alt="vnpy_ctp">
  <img src="https://img.shields.io/badge/vnpy__sqlite-1.1.3-blue?style=flat-square" alt="vnpy_sqlite">
  <img src="https://img.shields.io/badge/version-0.2.2-brightgreen?style=flat-square" alt="Version">
  <img src="https://img.shields.io/badge/license-MIT-lightgrey?style=flat-square" alt="License">
  <br><br>

  <h1 style="font-size:3.5rem; margin:0.5rem 0">vnpy-domestic</h1>
  <p style="font-size:1.2rem; margin:0.3rem 0">面向国内期货实盘的 vnpy 增强工具包<br>
  <span style="font-weight:300; font-size:1rem">基于 <a href="https://github.com/vnpy/vnpy">vnpy</a> 构建 · 专为国内期货实盘打磨</span></p>
  <br>
</div>

---

## v0.2.2 更新（2026-11）

- **结算价入库（settlements）**：新增按账户区分的 `settlements` 表；启动时用交易所日线（akshare `get_futures_daily`）取上一交易日结算价，写库并推送手机，失败自动回退 CTP 持仓成本价
- **盈亏双口径**：策略累计盈亏 = Σ trade_rounds（开仓价）+ 浮动（开仓价）；日内权益曲线 = 当日已实现 + 浮动（都按昨结算逐日盯市）。两套数字各司其职，不再混用
- **飞书控制按账户（可分别启停）**：实盘/模拟盘各一个自建应用、各占一个端口（`feishu_port`，默认 3000/3001），@谁控谁；两个账户是两个独立父进程，**停实盘不影响模拟盘，反之亦然**；指令里写错账户只提示不执行
- **保证金率走 openctp**：启动时从 `dict.openctp.cn/instruments` 拉全市场合约保证金率（多/空分别取，合约缺失按品种中位数），账户卡不再写死 8%；拉取失败自动兜底并在日志里提示；镜像地址可用环境变量 `OPENCTP_INSTRUMENTS_URL` 覆盖
- **手动停止持久化**：`@机器人 停止` 写 `.vntrader/.manual_stop`，systemd 拉起后仍保持停止，需显式 `重启`
- **稳定性**：通知改异步队列（交易线程零阻塞）、事件线程看门狗（300 秒无 CTP 事件自动重启）、三个事件 handler 异常兜底
- **清理**：`scripts/cleanup_monitor.py` 增加 `settlements` 清理（保留每账户每合约最新一条）
- **换月表自更新闭环**：新增 `scripts/rollover_check.py`（约 20 秒确认主力有没有变）+ `scripts/rollover_daily.py`（只取最近 12 天日线、只更新在交易的品种，约 3 秒）+ `rollover_daily.bat`；`run_cta.py` 内置守护线程每交易日 17:30 先 check、确认要换月才 update，成功/失败都推飞书。不再依赖回测仓 `daily.py`，也不再依赖 git 分发（回测仓生成仍可作基线）
- **策略级持仓归属**：CTP 持仓没有策略标签，移仓手数改以本策略 `strategy_lots` 批次为准（`_strategy_position`），账户总量只做 `>=` 校验；同一合约跑多个**同向**策略不再互相吃仓。**锁仓（同合约同时持多空）不支持自动移仓**——会被判定为双向持仓跳过并告警，需手工处理；启动时检测到同合约多策略也会推告警
- **平仓通知带开仓批次**：平仓消息里逐条列出「手数 / 开仓价 / 开仓时间 / 持有时间 / 该批盈亏」
- **换月表提醒口径修正**：提醒时刻 15:10 → **18:00**（放到 17:30 自动更新之后），判据从「未覆盖当天」改成「落后超过 3 个自然日」——之前每个交易日、每个周一、每个节后首日都会误报
- **代码规范**：新增 `[tool.ruff]` 配置（line-length 100，select E/F/I/UP/B/SIM/RUF/PIE/ISC），`ruff check` 从 162 条收到 27 条；补 `tests/test_multi_strategy.py`（同合约多策略移仓）

## v0.2.1 更新（2026-11）

- **换月改读表**：主力合约不再查新浪，改读 `data/rollover.json`（由回测仓 `scripts/daily.py` 生成），回测与实盘同源；按文件 mtime 热加载、`data_through` 超 10 天拒绝判主力、品种缺失不静默退回新浪
- **带仓移仓**：有持仓也能换月——冻结 → 撤单 → 开新（对手价超 3 跳，夹涨跌停）→ 平旧（SHFE/INE 拆平昨/平今）→ 切合约 → 解冻，每一步推送通知
- **失败分两阶段**：老仓未动 → 平掉新仓回滚；老仓已动 → 继续补平，绝不回滚（避免制造反向敞口）；补不平则停策略 + 报出"策略外账"手数，等人工接管
- **移仓跑独立线程 + 事件线程执行切换**：vnpy 的 EventEngine 单线程，回调里阻塞等成交会收不到成交回报；改为移仓线程只下单与投递 `_pending_switch`，由定时器执行 `stop/切合约/start`
- **换月表更新提醒**：收盘后 15:10 若表还没覆盖当天，推一条 `📅 换月表待更新`（每天一次，节假日靠"当天有市场活动"判断不误报）
- **双尺子（可选）**：`ROLLOVER_ADJUST_PRICES=1` 时策略的 tick / bar / 成交 / 委托全部用复权价（换月指标不断层），下单价格再除回真实价；引擎侧的账户、成交、滑点、监控仍用真实价
- **修复**：移仓在事件线程阻塞、提前返回不解锁导致策略永久冻结、`_contract_month` 未剥后缀使月份断档恒为 0、交割过滤对带后缀代码静默失效

## v0.2.0 更新（2026-10）

- **双账户 Web 监控**：实盘 CTP + simnow 模拟盘双账户同时跑，Web 面板合并看，飞书通知分群、每个账户各一个机器人（各占一个端口）。拆分三模块两库——交易核心 vnpy-domestic + 后端独立包 web-backend + 前端独立项目 web-frontend
- **MonitorWriter 写库层**：queue + 独立线程异步落库，交易主循环零阻塞；埋点只在 RolloverCtaEngine 现有方法末尾追加，不动核心交易逻辑；SQLite + WAL + busy_timeout 处理双进程并发写
- **monitor.db 监控库（15 张表）**：orders / trades / positions / accounts / account_positions / account_intraday_pnl / daily_pnl / strategy_status / strategy_intraday / logs / system_metrics / klines / trade_rounds / contracts / settlements，除 contracts 外全带 account 字段区分 real / simnow，时间统一北京时间字符串
- **web-backend 后端**：FastAPI + SQLAlchemy 2.0 只读 monitor.db，18 个 REST 接口 + WebSocket `/ws` 每 3 秒全量快照广播；后端不接 CTP、不碰交易进程，交易挂了照样查历史
- **web-frontend 前端**：Vue3 + Vite + Element Plus + ECharts + lightweight-charts，纯 JS。策略维度首页（策略卡片）→ 策略详情页（净值 / 回撤 / K线 / 合约信息 / 账户持仓），另有 Dashboard / Orders / Logs；白底现代简洁、红涨绿跌（国内期货习惯）
- **双账户隔离**：两个独立进程（一份代码靠 `CTP_MODE` 切配置），`.vntrader` 按 cwd 隔离，monitor.db 靠 `MONITOR_DB_PATH` 固定绝对路径共享；实盘凭证全走环境变量不落盘、simnow 走 secrets.yaml
- **订单统计口径修复**：活跃挂单 / 今日委托按 `vt_orderid` 取最新状态去重（不再按行计数），日期归口按交易日（夜盘 20:00 后归下一交易日）
- **绩效统计与成交配对**：平仓成交按 position_lots FIFO 配对成完整交易写 trade_rounds，胜率 / 盈亏比 / 持仓时间 / 买卖分开 / 净值曲线统一口径


---

## 介绍

vnpy-domestic 是一个面向国内期货实盘的 vnpy 扩展工具包。它不修改 vnpy 源码，而是通过继承 `CtaEngine` 和 `BarGenerator` 的方式叠加功能，让升级 vnpy 版本时无冲突。

**解决的问题：**

| 痛点 | 方案 |
|:----|:-----|
| 主力合约到期后要手动换月，错过行情 | `RolloverCtaEngine` 从**换月复权表**读主力；空仓直接切、有仓自动移仓（冻结→撤单→开新→平旧，失败回滚）|
| 回测与实盘用不同算法判主力，两条线对不上 | 两边读**同一份** `data/rollover.json`；基线由回测仓生成，交易机可自行增量更新（只追加、历史段不动） |
| 集合竞价 tick 时间戳错位（20:59 → 21:00） | `MyBarGenerator` 时间戳自动校正 |
| 午休/小节间歇无效 tick 干扰 K 线 | 非交易时段自动过滤 |
| 跨交易段 volume 重复计算 | 段首全量 + 后续增量的增量模式 |
| 实盘无人值守，出问题不知道 | 钉钉/飞书推送 + 策略汇总（`monitor_interval` 可调）|
| 非交易时段空跑浪费资源 | 守护进程按交易时段自动启停 |
| 实盘/模拟盘分开跑，看盘要开多个终端 | Web 面板双账户合并展示（web-backend + web-frontend） |

**核心模块 + 策略：**

| 模块 | 文件 | 职责 |
|:----|:-----|:-----|
| `RolloverCtaEngine` | `RolloverCtaEngine.py` | 自动换月 + 通知推送 + P&L 追踪 + CTP 断连监控 + 订单状态通知 + openctp 保证金率 |
| `MyBarGenerator` | `newbargenerator.py` | 增强 K 线合成 + 品种级交易时段管理 |
| `NotificationManager` | `notification_manager.py` | 钉钉/飞书统一通知 + 账户报告（含挂单列表） |
| `AKShare Datafeed` | `vnpy_akshare.py` | 免费 1 分钟 K 线数据源 |
| `TradingTime Updater` | `update_trading_times.py` | 交易时段拉取与 CSV 持久化 |
| `FeishuControl` | `feishu_http_control.py` | 飞书 HTTP 回调控制（@机器人 停止/重启，按账户/端口） |
| `PositionLots` | `position_lots.py` | 持仓批次 FIFO + 平今平昨规则 |
| `MonitorWriter` | `vnpy_domestic/monitor/writer.py` | 监控库异步写库（queue + 独立线程，交易主循环零阻塞） |
| `Web Backend` | `web-backend/web_backend/` | FastAPI 只读监控后端（REST + WebSocket） |
| `Web Frontend` | `web-frontend/` | Vue3 监控面板（双账户切换 + 策略详情 + K线） |

---

## 文档

| 文档 | 内容 |
|:--|:--|
| [docs/Linux部署.md](docs/Linux部署.md) | Ubuntu 服务器部署（systemd、CTP 凭证、防火墙） |
| [docs/web监控与部署.md](docs/web监控与部署.md) | Web 面板（web-backend + web-frontend）部署与运维 |
| [docs/飞书控制机器人.md](docs/飞书控制机器人.md) | 飞书自建应用机器人：创建、双账户、指令、排错 |
| [docs/monitor-db数据表.md](docs/monitor-db数据表.md) | monitor.db 各表字段与用途 |
| [docs/盈亏计算与数据流.md](docs/盈亏计算与数据流.md) | 盈亏口径、通知、前后端数据流 |
| [docs/订单统计口径.md](docs/订单统计口径.md) | 订单状态追踪与活跃挂单统计 |

---

## 安装

**环境要求：** Python ≥ 3.10（推荐 3.11 / 3.12）

### 1. 安装本包（含 vnpy 与全部依赖）

```bash
cd vnpy-domestic
pip install -e .
```

`pyproject.toml` 已声明 vnpy 及以下依赖，一条命令装齐；`-e`（editable）模式改源码不用重装。

| 包 | 版本 | 用途 |
|:---|:---|:-----|
| `vnpy` | ≥ 4.3.0 | 量化交易框架 |
| `vnpy_ctastrategy` | ≥ 1.4.1 | CTA 策略引擎（`CtaEngine`、`CtaTemplate`） |
| `vnpy_ctp` | ≥ 6.7.11.4 | 上期技术 CTP 柜台接口 |
| `vnpy_sqlite` | ≥ 1.1.3 | SQLite 数据库适配（vnpy 默认数据存储） |
| `pyyaml` | | 读取 `secrets.yaml` 通知配置 |
| `requests` | | 钉钉/飞书 HTTP 推送 |
| `akshare` | | 免费期货行情数据源 |
| `psutil` | | 系统硬件监控（CPU/内存/磁盘） |
| `pandas` | | CSV 读写与数据整理 |
| `lark-oapi` | | 飞书控制（@机器人 停止/重启） |
| `fastapi` / `uvicorn` | | 飞书 HTTP 回调服务 + Web 监控后端 |
| `sqlalchemy` | | 监控库表定义（交易进程埋点 + 后端读库共用） |

> Web 面板的后端是独立包，需要时再装：
> ```bash
> pip install -e ../web-backend
> ```

### 2. 安装 TA-Lib（可选）

仅当使用 vnpy 原生的技术指标（非 `ArrayManager`）时需要。本项目策略用 `vnpy_ctastrategy` 自带的 `ArrayManager`，不需要 TA-Lib。

**Windows：** 从 [whl 下载页](https://github.com/cgohlke/talib-build/releases) 下载对应 Python 版本的 `.whl` 文件：

```bash
pip install TA_Lib-0.6.0-cp311-cp311-win_amd64.whl
```

**Linux：**

```bash
sudo apt install ta-lib
pip install TA-Lib
```

**macOS：**

```bash
brew install ta-lib
pip install TA-Lib
```

---

## 配置

所有配置文件位于 `.vntrader/` 目录，该目录已加入 `.gitignore`，不会误提交到仓库。

### CTP 账户

用户名密码通过环境变量设置，其余参数硬编码在 `run_cta.py` 中。

**方法一：临时设置（每次新终端都需要）**

```bash
# Linux / macOS
export CTP_USER=your_account
export CTP_PASSWORD=your_password
python run_cta.py
```

```cmd
:: Windows CMD
set CTP_USER=your_account
set CTP_PASSWORD=your_password
python run_cta.py
```

```powershell
# Windows PowerShell
$env:CTP_USER="your_account"
$env:CTP_PASSWORD="your_password"
python run_cta.py
```

**方法二：写入 shell 配置文件（持久化）**

```bash
# Linux / macOS — 追加到 ~/.bashrc 或 ~/.zshrc
echo 'export CTP_USER=your_account' >> ~/.bashrc
echo 'export CTP_PASSWORD=your_password' >> ~/.bashrc
source ~/.bashrc
```

```cmd
:: Windows — 系统环境变量（需要管理员）
:: 搜索"环境变量" → 系统属性 → 环境变量 → 新建
:: 变量名: CTP_USER     变量值: your_account
:: 变量名: CTP_PASSWORD  变量值: your_password
```

| 参数 | 来源 |
|:----|:-----|
| 用户名 | 环境变量 `CTP_USER` |
| 密码 | 环境变量 `CTP_PASSWORD` |
| 经纪商代码、服务器地址、授权码等 | `run_cta.py` 中硬编码（SimNow 7x24 环境） |

### secrets.yaml

通知渠道配置：

```yaml
notify_type: "both"                     # dingtalk | feishu | both

dingtalk_webhook: "https://oapi.dingtalk.com/robot/send?access_token=你的token"
dingtalk_secret: "你的签名密钥"

feishu_webhook: "https://open.feishu.cn/open-apis/bot/v2/hook/你的hook"   # 通知用（群机器人 webhook）

# ── 飞书控制（可选，群里 @机器人 停止/重启策略）──
feishu_app_id: "cli_xxx"             # 自建应用 App ID
feishu_app_secret: "xxx"             # App Secret
feishu_encrypt_key: ""               # 事件与回调 → Encrypt Key（没开加密留空）
feishu_verification_token: "xxx"     # 事件与回调 → Verification Token（验签必填）
feishu_bot_open_id: "ou_xxx"         # 机器人 open_id（@识别）
feishu_host: "127.0.0.1"             # 监听地址，Linux 服务器改 0.0.0.0
feishu_port: 3000                    # 控制端口：实盘 3000 / 模拟盘 3001（不填按账户默认）
```

> 实盘、模拟盘**各自**一份 `.vntrader/secrets.yaml`、各自一个自建应用/机器人。
> 飞书一个应用只能填一个回调地址，一个端口也只能被一个进程占用，所以两个账户不能共用一个应用。

- `notify_type`：`dingtalk`（仅钉钉）、`feishu`（仅飞书）、`both`（同时推送）
- 钉钉使用 HMAC-SHA256 签名认证，需配置 `webhook` 和 `secret`
- 飞书使用简单的 Webhook URL

`feishu_webhook` 是通知用的【群机器人 webhook】，`feishu_app_*` 是控制用的【自建应用】——两套独立、互不影响。

**飞书控制（可选，按账户）**：群里 @机器人 发「停止」/「重启」，控制**该机器人所属账户**的子进程启停。跑在**父进程**（常驻，非交易时段也能 @机器人）：

> 实盘和模拟盘是**两个独立父进程**（各自 cwd / `CTP_MODE` / 端口 / 机器人），所以可以**分别启停**：`@实盘机器人 停止` 只停实盘，模拟盘继续交易；反过来也一样。停一个账户不会影响另一个账户的子进程、策略和持仓。

- 「停止」让子进程正常退出（撤挂单 → 日报 → 归零），并落盘 `<账户>/.vntrader/.manual_stop`；之后**即使 systemd 把父进程拉起，也不会自动启动子进程**；
- 「重启」清掉标记、重新 spawn 子进程（全新 CTP 连接 + 策略初始化）；
- 不配 `feishu_app_id` 不启用，通知不受影响；`feishu_verification_token` 为空时出于安全**不启动控制**；
- 端口由 `feishu_port` 决定，不填按账户默认：实盘 3000、模拟盘 3001。

指令可以只写动作（机器人身份就是账户），也可以带账户词做防呆：

| 群消息 | 结果 |
|------|------|
| `@实盘机器人 停止` / `@实盘机器人 实盘停止` | 停实盘子进程、写 `.manual_stop`，回「停止已完成」 |
| `@实盘机器人 模拟盘停止` | **不执行**，回「你 @ 的是实盘机器人，模拟盘的操作请 @模拟盘机器人」 |
| `@实盘机器人 实盘模拟盘停止` | **不执行**，回「指令里同时出现了实盘和模拟盘，请只写一个」 |
| `@机器人 停一下` | 回用法提示（没识别出停止/重启） |
| `@实盘机器人 重启` | 清标记、重新起子进程，回「重启已完成 (PID xxx)」 |

部署：

- 本地 Windows：`npx localtunnel --port 3000`（模拟盘再开一条 `--port 3001`），回调填 `公网URL/webhook/feishu`
- Linux 服务器：`feishu_host` 改 `0.0.0.0` + `ufw allow 3000`（模拟盘再加 `ufw allow 3001`），回调填 `http://公网IP:<端口>/webhook/feishu`

飞书后台「事件与回调」→「加密策略」区域，两个字段挨在一起：
- `Encrypt Key`（加密密钥）→ 填 `feishu_encrypt_key`，没开加密可留空
- `Verification Token`（验证令牌）→ 填 `feishu_verification_token`，必填，否则回调验签失败、challenge 不返回

配置加载优先级：**显式传入参数 > `secrets.yaml` > 类内部默认值**。YAML 搜索路径：当前目录 `.vntrader/` → 包目录 `.vntrader/` → 用户家目录 `.vntrader/`。

### cta_strategy_setting.json

策略配置，每个策略一个条目：

```json
{
    "策略名称": {
        "class_name": "YourStrategyClass",
        "vt_symbol": "rb2610.SHFE",
        "setting": {
            "参数名": 值
        }
    }
}
```

| 字段 | 说明 |
|:----|:-----|
| `class_name` | 策略类名，需与代码中类名一致 |
| `vt_symbol` | 合约代码，格式 `品种.交易所`，如 `rb2610.SHFE`、`m2701.DCE`、`MA610.CZCE` |
| `setting` | 策略参数，需与策略类的 `parameters` 列表对应 |

支持的交易所后缀：`SHFE`（上期所）、`DCE`（大商所）、`CZCE`（郑商所）、`INE`（能源中心）、`GFEX`（广期所）、`CFFEX`（中金所）。

> **⚠ 合约代码大小写与年份位数（CTP InstrumentID 规范）：**
> CTP 的合约代码大小写按交易所而定，写错会导致 `unable to find contract` 订阅失败、
> 换月时 `找不到新合约` 跳过换月：
>
> | 交易所 | 字母 | 年份位数 | 示例 |
> |:---|:---|:---|:---|
> | SHFE / INE | 小写 | 2 位 | `rb2610.SHFE`、`sc2610.INE` |
> | DCE / GFEX | 小写 | 2 位 | `m2701.DCE`、`si2611.GFEX` |
> | CZCE | 大写 | **1 位** | `MA610.CZCE`、`SA701.CZCE` |
> | CFFEX | 大写 | 2 位 | `IF2609.CFFEX` |
>
> 引擎已统一处理，无需手写正确格式也能跑：
>
> - **配置入口**：`add_strategy` 调用 `normalize_vt_symbol` 规范化，`RB2610.SHFE` → `rb2610.SHFE`、
>   `RM2701.CZCE` → `RM701.CZCE`，修正结果写回 `cta_strategy_setting.json` 并记日志
> - **换月方向**（换月表 → CTP）：表里存**库内格式**（`m2701`、`SR2701`，无后缀），
>   按交易所转小写/大写 + CZCE 截年份 → `rb2610.SHFE`/`MA701.CZCE`
> - **数据加载方向**（CTP → akshare）：CZCE 按当前年份补年份前缀 → `MA610` → `MA2610` 查询 Sina

### vt_setting.json（数据源配置）

vnpy 的数据源配置，需设置：

```json
{
    "datafeed.name": "akshare"
}
```

`__init__.py` 通过 `sys.modules["vnpy_akshare"] = vnpy_akshare` 注册别名，使 vnpy 能自动发现 `akshare` 数据源。

---

## 运行

```bash
python run_cta.py
```

启动后自动执行：

```
父进程 run_parent()
├─ 📅 更新交易时段（run_and_save）
├─ 等待交易时段到来
│
├─ 🟢 交易时段 → 启动子进程 run_child()
│   ├─ 📡 连接 CTP
│   ├─ 🏗️  初始化 MainEngine + RolloverCtaEngine
│   ├─ ⏳ 等待合约数据（sleep 40，CTP 连接）
│   ├─ 📂 加载策略配置（init_engine 内部调用 load_strategy_data）
│   ├─ 🔄 初始化策略 + 换月检查（init_all_strategies → rollover summary）
│   ├─ ▶️  启动策略（start_all_strategies）
│   ├─ 📱 推送启动通知 + 账户报告（含系统硬件信息）
│   └─ 🔄 交易循环（每 10s 检查，账户报告由 REPORT_INTERVAL 控制）
│
├─ 🔴 非交易时段 → 等待子进程自退出
│   └─ 超时 → terminate
│
└─ ⚠️ 子进程崩溃 → 自动重启（最多 5 次）
```

`run_parent()` 先确认环境变量存在，然后 `run_and_save()` 更新交易时段，最后按 `check_trading_period()` 的时段判断启停子进程。子进程内 `load_ctp_setting()` 在函数内读取环境变量，避免 Windows spawn 的模块级重入问题。

Linux 服务器部署（systemd 守护 + CTP 凭证环境变量注入）见 [docs/Linux部署.md](docs/Linux部署.md)。

---

## 模块

### RolloverCtaEngine

继承 `CtaEngine`，叠加自动换月、成交通知、P&L 追踪和 CTP 断连监控。

**换月逻辑（读表，与回测同源）：** 主力合约来自 `data/rollover.json`——由**回测仓**
`vnpy_backtester/scripts/publish.py` 整份生成并分发；日常由**交易机自己增量更新**
（`scripts/rollover_check.py` 确认主力是否变化 → `scripts/rollover_daily.py` 只取最近 12 天
日线、只更新在交易的品种，原子替换），回测仓不再是必需环节。
`_fetch_main_contract(variety)` 取表里该品种的最后一段，即当前主力；表里存库内格式
（`m2701`/`SR2701`），经 `normalize_vt_symbol` 转成 CTP InstrumentID 后使用。

> **表的位置与更新**
> - 默认 `<仓库根>/data/rollover.json`，可用环境变量 `ROLLOVER_TABLE` 覆盖
> - 引擎按文件 mtime **热加载**：`rollover_daily.py`（或回测仓 `daily.py`）覆盖后无需重启
> - `data_through` 超过 **10 天**视为过期，拒绝拿它判主力（记 ERROR 日志），避免用旧表猜
> - 表里没有的品种直接返回 `None`，**不会**静默退回新浪
> - **每日更新提醒**：收盘后 **18:00**（放在 17:30 自动更新之后）若表的 `data_through`
>   落后超过 **3 个自然日**，推一条 `📅 换月表待更新`，每天只推一次；节假日不误报——
>   要求"当天确实有过市场活动"（CTP 有 tick/account 事件）才会提醒
> - 判据不能用「是否覆盖当天」：`data_through` 语义上永远是**上一个交易日**，
>   用 `>= today` 判断会让每个交易日、每个周一、每个节后首日都误报

**换月时机：** 只在**初始化时**判定一次（`_check_rollover_for_init`）：读表 → 比合约 →
空仓立即切；有持仓则登记 `_pending_rollover`，等盘中窗口（09:05–14:45 / 21:05–22:55 /
00:05–02:20）内、新合约有行情时执行一次（`_run_pending_rollovers`，每策略只执行一次，
失败不重试、告警交人工）。启动汇总由 `send_rollover_init_summary` 推送。
运行期不轮询主力，平仓后也不补检查——但**子进程每个交易时段都会重启**（父进程按
`check_trading_period()` 起停：日盘 08:45、夜盘 20:45），所以**每个交易日判定 2 次**，
不会因为连续跑而漏掉换月。

**同一合约跑多个策略（含锁仓）：** CTP 持仓**没有策略标签**——
`_ctp_position(vt_symbol)` 返回的是账户在该合约上的**总量**，所以引擎用 `strategy_lots`
（本策略逐笔开仓批次）当**策略级持仓归属表**：

- **移仓手数以本策略批次为准**（`_strategy_position`）：账户总量只做 `>=` 校验，不够就中止
  移仓并告警。同一合约跑多个**同向**策略时，各自只搬自己的手数，不再互相吃仓。
- **🔴 不支持锁仓（同一合约同时持多空）**：`_ctp_position` 一旦看到 `long > 0 且 short > 0`，
  移仓直接跳过并推一条「多空双向持仓，请手工处理」。两个原因：
  1. 判断是**账户级**的——A 持多、B 持空时，A 单独移仓也会被误判成锁仓；
  2. DCE 等**净持仓交易所** CTP 只回一条 `Direction.NET`，多空已合并，**无从归因**。

  锁仓类策略（趋势 + 反向对冲、网格双边成交）只能**手工移仓**，或拆到两个 CTP 账户。
- **批次缺失时退化为账户口径**：`strategy_lots` 为空（`cta_strategy_data.json` 被清、
  在 CTP 客户端手工建过仓、首次上线尚未成交）时，移仓退回按账户总量执行并写一条日志——
  **这种情况下同合约多策略仍可能吃错仓**。
- **启动告警**：检测到同合约多策略会推一条 🚨，但不阻止启动（有时只是临时对比）。
- **想要完全隔离**：一个策略一个 CTP 账户，或同品种跑不同月份合约。同一账户 + 同一合约 +
  多个策略，从交易所层面就没有"策略"这个概念，代码补不上。

**空仓换月：** `get_contract` 验证新合约存在 → 切 `vt_symbol` + 重新 `on_init` → 恢复变量
（**丢弃价格类变量**：`entry_price`/`intra_trade_high`/`atr`/`ma20` 等旧点位带到新合约会立刻
误触发止损；`cumulative_pl`/`lots`/`pos` 保留）→ 原来在跑就重新 `start`。

**带仓移仓（`_migrate_position`）：** 有持仓时不再跳过，按下面的顺序执行，**每一步都推送通知**：

| 步骤 | 动作 | 失败时 |
|:---|:---|:---|
| 🔒 冻结 | `_migrating` 加锁，策略下单全部丢弃 | — |
| 🧹 撤单 | `cancel_all` 撤掉全部挂单 | 继续 |
| 🟢 开新 | 对手价**超 3 跳**开等量仓（`_send_manual` 绕过 `strategy.vt_symbol` 直发，但登记策略映射，成交按普通开平仓记账） | 回滚（无需平旧） |
| 🔵 平旧 | 按交易所规则拆平昨/平今（SHFE/INE）或 `CLOSE` | 平掉新仓回滚 |
| ✅ 完成 | 切合约 + 对账（盈亏已由成交回报记账，不再手工结算）+ 解冻 + 恢复运行 | — |

**保护机制：**

- **表过期不换月**：`data_through` 超过 10 天 → 拒绝判主力并推送 `🚨`（策略继续跑在老合约上，
  不会换到错合约）

- **超价 3 跳**：买 = 卖一 +3×tick、卖 = 买一 −3×tick，保证吃透盘口；自动夹在涨跌停内
  （超涨停是废单）；跳数用环境变量 `ROLLOVER_SLIPPAGE_TICKS` 调，每笔都记对手价→委托价，
  事后能算移仓成本
- 移仓跑在**独立线程**——vnpy 的 `EventEngine` 是单线程的，在回调里阻塞等成交会让
  成交回报永远进不来，必然超时
- **线程只投递、事件线程执行**：移仓线程下完单后只写 `_pending_switch`，由定时器
  （事件线程）执行 `stop → 切合约 → start → 解冻`，保证 `on_init/on_start` 和 `on_bar`
  永远在同一条线程上串行；成交到切换之间最多等 1 秒，策略保持冻结
- 每腿 3 秒超时，成交数量不足即判定失败
- 冻结用 `try/finally` 解锁，切合约抛异常也不会把策略冻死
- 账户层多空双向持仓（多空各持一手）不自动移仓，直接告警要求人工处理
- 移仓过程记 `write_log`（进 vnpy 日志 + monitor.db，失败自动升 ERROR 并额外告警）

**双尺子：指标用复权价（可选，默认关）**

换月当天合约价格会跳一截（M2609 → M2701 差 30~60 点），指标（MA/ATR/布林）如果直接用
真实价算，会在换月处断层，信号跟着乱。打开开关后：

```
CTP tick（真实价 3000）
   ↓ process_tick_event 复制并 ×1.05（当前段的因子）
策略收到 3150 → BarGenerator 合成 bar → on_bar → 指标在复权空间计算
   ↓ 策略 buy(3148)
send_order：3148 / 1.05 = 2998.1 → 取整 2998 → 下给 CTP
```

开关方式（账户目录的环境变量）：

```bat
set ROLLOVER_ADJUST_PRICES=1
```

| 位置 | 行为 |
|:---|:---|
| `process_tick_event` | 给策略的 tick 价格 × 因子（`copy` 副本，原 tick 不动） |
| `process_trade_event` | 给策略的成交价 × 因子 —— **不换的话** strategy 用真实入场价和复权 bar 算盈亏，会凭空多出 `价格×(因子-1)` 的假盈利 |
| `process_order_event` | 给策略的委托价 × 因子（engine 自己的滑点统计仍用真实价） |
| `_factor_of` | 查表取当前主力段因子；合约不在主力段 / 表读不到 → 1.0 |
| `send_order` | 策略价格 ÷ 因子，再按 `pricetick` 取整 |
| `last_prices` / 移仓取盘口 / 监控快照 | **始终用真实价**（不会被因子污染） |

> ⚠️ 三个已知限制：
> 1. 开关**默认关**；打开前先在 SimNow 验证指标连续、下单价格落在真实 tick 网格
> 2. monitor 的 K 线写入（`_write_kline`）尚未做还原，开启后可能记成复权价
> 3. 策略内部算出的盈亏是**等比例放大**（真实盈亏 × 因子），不再有假盈利，但绝对点数止损
>    （如"止损 20 点"）实际生效为 `20 / 因子` 点，需要按因子调整参数

**盈亏统计：** 平仓成交按交易所规则（`settle_close`）匹配持仓批次，推送本次盈亏与累计盈亏（元）；平仓单挂出时附预估盈亏（挂单价 vs 开仓成本价）。累计盈亏按交易日统计，每日收盘归零（隔夜持仓按逐日盯市已结算，次日从 0 起算）。

**订单通知：**

| 订单状态 | 通知 |
|:---|:---|
| REJECTED | ❌ 订单被拒 |
| NOTTRADED（开仓单/平仓单） | ⚠️ 开仓单未成交 / ⚠️ 平仓单未成交（去重，首次推送） |
| 平仓单挂出（策略层） | 📤 平仓单已挂出 @价格 |
| 未成交追价（策略层） | ⚠️ 平仓单追价 @新价格 |
| 成交 | 🟢 开仓 / 🔴 平仓 |

**延迟计算：** 所有订单统一统计延迟 = `trade.datetime - 发单时间`（含挂单排队等待时间）。

### MyBarGenerator

继承 `BarGenerator`，解决集合竞价时间戳校正（20:59 → 21:00）、非交易时段过滤、跨段 volume 增量计算（段首全量 + 后续增量，gap > 2h 重置）。

交易时段从 `trading_times.csv` 加载为模块级全局缓存 `_TRADING_SESSIONS`，按品种名称前缀匹配。竞价判断只对每个交易会话的首段生效（10:30 小节恢复、13:30 午盘无竞价）。

### NotificationManager

钉钉（HMAC-SHA256）和飞书（Webhook）统一通知。配置优先级：显式传入 > `secrets.yaml` > 类默认值，YAML 按当前目录、包目录、用户家目录依次搜索。速率限制当前关闭（设 `_min_send_interval` 可启用）。提供启动/关闭通知、系统硬件信息（CPU/内存/磁盘/运行时间）、账户报告（动态权益/可用资金/持仓/活跃挂单列表）、策略状态分批汇总等预置方法。

**账户报告：** 新增活跃挂单列表（合约、方向、价格、已成交/总数量、状态），由 `main_engine.get_all_active_orders()` 实时查询。

**每日订单状态统计：** 账户报告自动附带当日订单状态汇总（提交中/未成交/部分成交/全部成交/已撤销/被拒），按自然日自动清零。数据由 `RolloverCtaEngine` 在 `process_order_event` 中实时统计，`get_daily_order_stats()` 供 `run_cta.py` 调用后传入 `send_account_report`。

### AKShare 数据源

通过 `sys.modules` 别名注册为 vnpy 原生数据源。调用 `akshare.futures_zh_minute_sina()` 获取 1 分钟 K 线，仅支持 `Interval.MINUTE`，其他周期返回空列表。CZCE 合约按当前年份补 2 位年份前缀（`MA610` → `MA2610`，跨年份自动适配，不再硬编码 "2"）。

### 交易时段更新

`run_cta.py` 启动时调用 `run_and_save()`，读取 `cta_strategy_setting.json` 提取品种，从 `dict.openctp.cn/times` 拉取交易时间，按 18–6 点分离夜盘，保存为 `trading_times.csv`。API 失败不阻塞启动。

### FeishuControl（飞书控制）

可选功能，配置 `feishu_app_id` 后启用。**每个账户一个机器人**：实盘、模拟盘各一套自建应用，端口各占一个（`feishu_port`，默认 3000 / 3001），群里 @谁就控制谁。跑在父进程（常驻，非交易时段也能 @机器人），回调地址 `/webhook/feishu`。

- `on_message` 把指令解析成动作后入队；指令里可以带「实盘/模拟盘」做防呆，写错机器人、或同时写了两个，都只回提示不执行；
- 回复由父进程主循环发（回调必须 3 秒内返回），`message_id` 去重防重复触发；
- `verification_token` 为空时出于安全不启动控制（防伪造回调）；
- 「停止」会把标记落盘 `.vntrader/.manual_stop`，父进程重启后仍保持停止，需 @机器人 重启 恢复。

详见上文「配置 → secrets.yaml → 飞书控制」；从 0 到 1 的创建/部署教程见 [docs/飞书控制机器人.md](docs/飞书控制机器人.md)。

### PositionLots（持仓批次）

`settle_close(lots, exchange, offset, qty)` 按交易所规则平仓：DCE/CZCE/GFEX 先开先平、CFFEX 先平今、SHFE/INE 平今平昨（涨停先平昨）。`trading_day(dt)` 提供交易日判定。逐日盯市核算中，历史仓按昨结算、当日仓按开仓价。

---

## 守护进程

`run_cta.py` 采用父子双进程。父进程按交易时段启停子进程，子进程崩溃自动重启（最多 5 次）。交易时段判断：周一~周五 8:45–15:01 或 20:45–次日 2:45，周六凌晨延续夜盘，周日晚 20:45 后准备开盘。

实盘、模拟盘各跑一份 `run_cta.py`（不同 cwd / `CTP_MODE` / 端口），是两个互不影响的守护进程，可以分别启停。

```
run_child()
  ├─ EventEngine + MainEngine
  ├─ 连接 CTP（sleep 40 等待合约数据）
  ├─ 初始化 RolloverCtaEngine + 加载策略（init_engine → load_strategy_data）
  ├─ 换月检查（init_all_strategies → check rollover → summary）
  ├─ 启动策略（start_all_strategies）
  ├─ 推送启动通知 + 账户报告
  └─ 主循环（10s）：检查交易时段，账户报告间隔由 REPORT_INTERVAL 控制
     非交易时段 → 停止策略 → 关闭通知 → 退出
```

---

## Web 监控（双账户）

实盘 + simnow 双账户同时跑，Web 面板合并看。链路：交易进程埋点写 monitor.db → web-backend 只读查库 → web-frontend 展示。飞书通知分群、每个账户各一个机器人（各占一个端口）。

### 架构

```
[实盘进程 run_cta.py (CTP_MODE=real)]     [模拟进程 run_cta.py (CTP_MODE=simnow)]
    │ MonitorWriter (account=real)          │ MonitorWriter (account=simnow)
    ▼                                       ▼
        monitor.db（同一个库，表带 account 字段，MONITOR_DB_PATH 固定绝对路径）
                        │
                        ▼
        [web-backend uvicorn :8000]  只读查库，REST + WebSocket
                        │
                        ▼
        [web-frontend Vue3]  按 account 分组展示（实盘 / 模拟 / 全部切换）
```

### 数据流

- **写**：交易进程 RolloverCtaEngine 各事件回调末尾埋点，`MonitorWriter` 用 queue + 独立线程异步落库（不阻塞交易主循环）
- **读**：web-backend 只读 monitor.db，18 个 REST 接口 + WebSocket `/ws` 每 3 秒全量快照广播；后端不接 CTP、不碰交易进程，交易挂了照样查历史
- **展示**：web-frontend 策略维度首页 → 策略详情页；REST 填历史全量、WS 推增量
- **保证金**：启动时拉 `dict.openctp.cn/instruments` 的 `Long/ShortMarginRatioByMoney`，按合约取率（缺失按品种中位数，再兜底 8%）；账户卡按「手数 × 成本价 × 乘数 × 保证金率」估算，`contracts.margin_rate` 同步给前端合约卡

### 本地跑法

```bash
# 后端（窗口 A，cd 到 web-backend 目录，cwd 进 sys.path）
export MONITOR_DB_PATH="/path/to/monitor.db"
python -m uvicorn web_backend.main:app --host 127.0.0.1 --port 8000

# 前端（窗口 B）
cd web-frontend
npm run dev        # http://localhost:5173，/api /ws 已代理到 8000
```

前端已装好 node_modules；`npm run build` 出 dist 交 nginx（服务器不装 node，本机 build 后 scp 上去）。

### 数据清理

monitor.db 快照表（accounts / strategy_status / account_positions / account_intraday_pnl / system_metrics）每 3s 一条会持续膨胀。用 `scripts/cleanup_monitor.py` 定期清理——删 10 天前、保留每 key 最新一条（实时面板不丢状态）；`settlements` 按 `trading_day` 一起清，保留每账户每合约最新一条（盯市只需要最近一条，删了下次启动也会重新拉）。事件表（orders / trades / trade_rounds）、日频表（daily_pnl / strategy_intraday）、基础表（klines / contracts / logs）永久保留；每次清理写一条日志进 logs 表留痕。

```bash
python scripts/cleanup_monitor.py            # 默认删 10 天前
python scripts/cleanup_monitor.py --vacuum   # 删完回收空间（需交易进程停止）
```

完整方案 / 前端开发 / 运行部署说明见 [docs/web监控与部署.md](docs/web监控与部署.md)。

---

## 项目结构

```
vnpy-domestic/
├── run_cta.py                          ← 实盘入口（守护进程）
├── README.md                           ← 本文件
├── pyproject.toml                      ← 包配置
├── docs/                               ← 说明文档（长文都放这里）
│   ├── README.md                       ← 文档索引
│   ├── Linux部署.md
│   ├── web监控与部署.md
│   ├── 飞书控制机器人.md
│   ├── monitor-db数据表.md
│   ├── 盈亏计算与数据流.md
│   └── 订单统计口径.md
├── .gitignore                          ← 保护 .vntrader/
│
├── .vntrader/                          ← 运行时配置（gitignore 保护）
│   ├── secrets.yaml                    ← 通知 Webhook
│   ├── cta_strategy_setting.json       ← 策略参数
│   ├── cta_strategy_data.json          ← 策略变量持久化（有成交后自动填充）
│   └── trading_times.csv               ← 交易时段（自动更新）
│
├── data/
│   └── rollover.json                   ← 换月复权表（回测仓生成基线，交易机可增量更新）
│
├── scripts/
│   ├── cleanup_monitor.py              ← monitor.db 清理（按天删历史快照）
│   ├── rollover_check.py               ← 换月表：确认主力有没有变（约 20 秒，只读）
│   ├── rollover_daily.py               ← 换月表：增量更新 + 飞书通知 + 日志
│   └── rollover_daily.bat              ← 每日更新入口（计划任务/手动）
│
├── vnpy_domestic/
│   ├── __init__.py                     ← 导出 MyBarGenerator，注册 akshare 别名
│   ├── rollover/
│   │   ├── __init__.py                 ← 导出 RolloverTable 等读表接口
│   │   ├── table.py                    ← 换月复权表读取（与回测仓 rollover_table.py 逐字节一致）
│   │   └── core.py                     ← 换月算法纯函数（与回测仓 rollover_core.py 逐字节一致）
│   ├── monitor/
│   │   ├── __init__.py                 ← 导出 models + MonitorWriter
│   │   ├── models.py                   ← monitor.db 15 张表（写侧）
│   │   └── writer.py                   ← MonitorWriter 写库（queue + 独立线程）
│   ├── trader/
│   │   ├── newbargenerator.py          ← MyBarGenerator + 交易时段模块
│   │   ├── notification_manager.py     ← 钉钉/飞书通知
│   │   ├── feishu_http_control.py      ← 飞书控制（@机器人 停止/重启）
│   │   ├── position_lots.py            ← 持仓批次 + 平仓规则（FIFO/平今平昨）
│   │   ├── update_trading_times.py     ← 交易时段拉取
│   │   └── vnpy_akshare.py             ← AKShare 数据源
│   └── RolloverCtaEngine/
│       └── RolloverCtaEngine.py        ← 自动换月 + 通知 + P&L + 断连监控
│
├── strategies/
│   └── dual_ma.py                      ← 双均线策略
│
└── scripts/
    └── cleanup_monitor.py              ← monitor.db 快照清理（删 10 天前保留最新）
```

> **数据准备不在这里。** 换月复权表的生成与数据体检都在回测仓：
>
> ```bat
> cd C:\Users\29523\Desktop\vnpy_backtester
> python scripts\daily.py        :: 诊断 + 生成表 + 写回实盘这份
> ```
>
> 本仓只有读表模块（`vnpy_domestic/rollover/`）和表数据（`data/rollover.json`），
> 不产生任何派生文件。

---

## 致谢

本项目基于 [vnpy](https://github.com/vnpy/vnpy)（MIT License）构建。

```
The MIT License (MIT)
Copyright (c) 2015-present, Xiaoyou Chen

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.
```

