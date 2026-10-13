# vnpy-domestic Web 监控 · 使用与部署手册

> 整合《Web 端监控使用手册》与《Web 监控部署指南》两份文档，按实际落地代码校正。
> 分两部分：**Windows 本地开发使用**、**Linux 从 GitHub clone 部署**。
> 最后更新：2026-10

---

# 第一部分 · Windows 本地开发使用

## 1. 项目概述

vnpy-domestic 是面向国内期货实盘的 vnpy 增强工具包，不修改 vnpy 源码，靠继承 `CtaEngine` 和 `BarGenerator` 叠加功能。核心是三个模块、两个库：

| 模块 | 归属 | 形态 | 说明 |
|:--|:--|:--|:--|
| 交易核心 | vnpy-domestic 库 | Python 包 | 自动换月 + 逐日盯市 + 飞书通知/控制 + 写监控库 |
| Web backend（监控后端） | 独立项目 web-backend | Python 包 `web_backend` | 只读 monitor.db，REST + WebSocket |
| Web frontend（监控前端） | 独立项目 web-frontend | Vue3 + Vite 静态产物 | 双账户监控面板 |

**解决的问题**：

| 痛点 | 方案 |
|:----|:-----|
| 主力合约到期后手动换月，错过行情 | `RolloverCtaEngine` 自动识别主力、持仓归零自动换月 |
| 集合竞价 tick 时间戳错位（20:59 → 21:00） | `MyBarGenerator` 时间戳自动校正 |
| 午休/小节间歇无效 tick 干扰 K 线 | 非交易时段自动过滤 |
| 跨交易段 volume 重复计算 | 段首全量 + 后续增量的增量模式 |
| 实盘无人值守，出问题不知道 | 钉钉/飞书推送 + 策略汇总 |
| 非交易时段空跑浪费资源 | 守护进程按交易时段自动启停 |
| 实盘/模拟盘分开跑，看盘要开多个终端 | Web 面板双账户合并展示 |

## 2. 总体架构与进程分配

### 2.1 数据流

```
[交易进程 RolloverCtaEngine 埋点] ──MonitorWriter──> monitor.db ──读──> [web-backend] ──REST/WS──> [web-frontend]
```

- **写**：交易进程各事件回调末尾埋点，`MonitorWriter` 用 queue + 独立线程异步落库（不阻塞交易主循环）
- **读**：web-backend 只读 monitor.db，不接 CTP、不碰交易进程，交易挂了照样查历史
- **展示**：web-frontend REST 填历史全量、WS 推增量

### 2.2 进程拓扑（本地开发态，双账户 + Web）

```
实盘 run_cta.py（CTP_MODE=real）
├── 父进程 run_parent（常驻）
│   ├── 主循环：按交易时段 spawn/管理子进程、崩溃自动重启（最多 5 次）
│   └── daemon 线程：飞书控制 uvicorn :3000        ← 线程，不是进程
└── 子进程 run_child（交易时段内）
    └── 连 CTP + RolloverCtaEngine + 策略（MonitorWriter 写库）

simnow run_cta.py（CTP_MODE=simnow）
├── 父进程 run_parent（常驻，无飞书控制）
└── 子进程 run_child（连 CTP + 策略 + 写库）

web-backend：独立进程 uvicorn :8000（只读 monitor.db）
web-frontend：独立进程 vite :5173（nginx 部署后为静态文件，进程消失）
nginx（部署后）：:443 唯一公网入口，静态 + 反代 8000
```

### 2.3 进程清单

| 进程 | OS 进程数 | 端口 | 是否常驻 | 说明 |
|:--|:--|:--|:--|:--|
| 实盘 run_cta.py | 2（父 + 子） | 3000（父进程内线程） | 父常驻，子按交易时段 | 飞书控制线程寄居父进程 |
| simnow run_cta.py | 2（父 + 子） | - | 父常驻，子按交易时段 | 无飞书控制 |
| web-backend | 1 | 8000 | 常驻（可选） | 只读 monitor.db |
| web-frontend | 1（dev）/ 0（nginx） | 5173（dev） | dev 常驻 | 部署后变静态文件 |
| nginx | 1 | 443（+80 跳转） | 常驻 | 唯一公网入口 |

本地开发态共 6 个 OS 进程（交易相关 4 个 + web 2 个）；nginx 部署后前端进程消失，剩 5 个。

### 2.4 关键结论

1. **飞书控制不是独立进程，是父进程里的 daemon 线程**。依据 `run_cta.py`：

   ```python
   threading.Thread(
       target=start_control, args=(app, feishu["host"], 3000), daemon=True
   ).start()
   ```

   `start_control` 内部 `uvicorn.Server(config).run()` 阻塞监听 3000（`feishu_http_control.py`）。

2. **飞书启停的是交易子进程**（连 CTP 跑策略那个），不是父进程、不是 web。父进程（机器人寄居宿主）一直常驻。

3. **飞书控制只实盘开**：`run_cta.py` 里 `if os.getenv("CTP_MODE", "real") == "real":` 才启动。simnow 进程 `CTP_MODE=simnow` 不满足，不启飞书控制端口，所以机器人天生只绑定实盘，不存在「识别错账户」。

4. **「控制」和「通知」是两回事**：控制（@机器人 停/重启）只实盘有；通知（推消息）走 `feishu_webhook` 一个字段，实盘/模拟盘各账户目录的 secrets.yaml 各填各群的 webhook（靠目录隔离分群）。

5. **端口不冲突**：3000（飞书控制）/ 8000（后端）/ 5173（前端 dev）/ 443（nginx）各占各的。

6. **生命周期解耦**：飞书「停止」→ 交易子进程退出 → monitor.db 停更 → web 面板数据停在停止时刻（历史仍可查）；「重启」→ 新子进程 → 恢复写库 → web 恢复实时。web 前后端全程不受影响。

## 3. 双账户隔离

### 3.1 为什么必须两个进程（合约覆盖坑）

vnpy 的合约字典按 `vt_symbol` 覆盖，单 main_engine 连两个 CTP 会合约冲突。查 `vnpy/trader/engine.py:423`：

```python
def process_contract_event(self, event):
    contract = event.data
    self.contracts[contract.vt_symbol] = contract   # key 不含 gateway_name
```

实盘 CTP 和 simnow 都推 `rb2701.SHFE`，后推的覆盖先推的。实盘策略调 `get_contract("rb2701.SHFE")` 时拿到哪个合约取决于推送顺序，可能把实盘单下到 simnow 上。订单/成交/持仓/资金没这个问题（key 带 gateway_name 前缀），唯独「合约」会覆盖。

所以选「两个独立进程」而非「一个引擎连两个 CTP」——实盘和模拟本质是「两个世界」，进程隔离后模拟盘随便重启、随便改，不影响实盘。

### 3.2 一份代码 + CTP_MODE 切配置

**不复制代码**。`run_cta.py` 一份，靠环境变量 `CTP_MODE` 切配置：

- `CTP_MODE=simnow`：账号密码读 `secrets.yaml`（simnow_user / simnow_password），经纪商 9999、服务器 182.254.243.31:30003/30013、产品名、授权码硬编码
- `CTP_MODE=real`：7 个凭证全走环境变量（用户名/密码/brokerID/交易服务器/行情服务器/产品名/授权编码），代码里不留真实信息

### 3.3 数据隔离（两个工作目录）

vnpy 的 `.vntrader` 数据目录按「当前工作目录」解析（cwd 下有 .vntrader 就用 cwd 的，否则用 home 的）。两个进程从不同 cwd 跑，vnpy 数据天然隔离。

| 文件 | 实盘 | 模拟 | 处理方式 |
|:--|:--|:--|:--|
| 代码 run_cta.py / strategies | 同 | 同 | git 一份，clone 两份 |
| .vntrader/database.db、cta_strategy_data.json | 独立 | 独立 | 两个 WorkingDirectory 隔离 |
| 日志文件 | 独立 | 独立 | log_real / log_sim 前缀 |
| monitor.db（监控） | 共享 | 共享 | 同一个库，表带 account 字段 |
| secrets.yaml | 独立 | 独立 | 各账户目录一份（`_load_secrets()` 按 cwd → 源码 → home 查找，cwd 优先；模拟盘目录放 simnow 密码） |
| 飞书控制 3000 | 开 | 不开 | CTP_MODE 判断 |

**monitor.db 为什么用固定绝对路径**：单账户时 `get_file_path("monitor.db")` 跟着 cwd 解析到同一文件，天然共享；双账户两个进程 cwd 不同，会解析成两个文件、各写各的。所以 monitor.db 是唯一脱离 cwd 的共享点，用环境变量 `MONITOR_DB_PATH` 显式指定同一个绝对路径（如 `/home/ubuntu/vnpy-data/monitor.db`），三个进程（实盘/模拟/后端）都读它。

### 3.4 凭证管理（两层）

- **实盘 CTP 全部登录信息** → 环境变量（systemd 注入），一个都不落盘
- **模拟盘账号密码** → secrets.yaml（gitignore 了，不公开）

⚠️ 模拟密码不能写进源码：vnpy-domestic 是公开仓库，写源码 = 密码公开（删了也没用，git 历史还在）。写 secrets.yaml 和写源码对程序运行没区别，区别只在「会不会公开」。

### 3.5 策略与参数

策略代码共享、策略参数靠目录隔离、监控层才加 account 字段。两个目录各自的 `cta_strategy_setting.json` 参数完全不同，互不影响，**不需要在策略配置里加账户属性**。

### 3.6 飞书（控制 + 通知）

- **控制**（@机器人 停止/重启，自建应用）：只对实盘有意义，只实盘开
- **通知**（群机器人 webhook）：`feishu_webhook` 一个字段，实盘/模拟盘各目录 secrets.yaml 各填各群 webhook（目录隔离分群），文案加 `[实盘]`/`[模拟]` 前缀

## 4. 监控数据库 monitor.db

文件在根目录 `monitor.db`（由 `MONITOR_DB_PATH` 指定），与 vnpy 的 `database.db` 分开。SQLite + WAL + busy_timeout 处理多进程并发写。所有时间字段存北京时间字符串 `YYYY-MM-DD HH:MM:SS`，写库时 `zoneinfo("Asia/Shanghai")` 统一转换，前端零转换直接显示。

### 4.1 十四张表

| 表名 | 用途 | 关键字段 | 写入时机 |
|:--|:--|:--|:--|
| orders | 订单（委托） | vt_orderid, strategy, account, symbol, direction, offset, price, volume, traded, status, submit_time, queue_delay_ms, cancel_delay_ms | 下单/撤单/拒单时 |
| trades | 成交明细 | vt_tradeid, vt_orderid, strategy, account, symbol, price, volume, slippage, trade_time | 成交回报时 |
| positions | 持仓快照 | strategy, account, vt_symbol, direction, volume, avg_price, pnl, trading_day, snapshot_time | 每次成交后 |
| accounts | 账户资金 | account, accountid, balance, available, frozen, margin, pnl, snapshot_time | 资金事件 + 每 3s 快照 |
| daily_pnl | 每日盈亏 | trading_day, strategy, account, realized_pl, cumulative_pl | 每日收盘归零时 |
| strategy_status | 策略状态 | strategy, account, symbol, status, pos, long_pos, short_pos, pnl, parameters, variables, update_time | 每 3s 快照 |
| logs | 日志 | ts, level, strategy, account, message | 每次写日志 |
| system_metrics | 系统指标 | ts, cpu, mem, disk, uptime | monitor_interval 定时 |
| klines | 1min K 线 | symbol, exchange, account, interval, datetime, open, high, low, close, volume | BarGenerator 合成 1min bar |
| trade_rounds | 交易回合 | strategy, symbol, exchange, account, direction, entry_price, exit_price, volume, pnl, holding_seconds, open_time, close_time, trading_day | 平仓 FIFO 配对 |
| contracts | 合约基础信息 | symbol(主键), exchange, name, product, size, pricetick, update_time | monitor_interval 定时 upsert |
| account_positions | 账户实际持仓（CTP） | account, accountid, vt_symbol, direction, volume, price, pnl, snapshot_time | 每 3s 快照 |
| account_intraday_pnl | 账户日内盈亏 | account, accountid, trading_day, realized_pl, floating_pl, balance, snapshot_time | 每 3s 快照 |
| strategy_intraday | 策略日内净值快照 | strategy, account, symbol, realized_pl, floating_pl, equity, snapshot_time | 每 15 分钟 |

> 除 `contracts` 外，所有表都带 `account` 字段区分 `real`（实盘）/`simnow`（模拟盘）。contracts 表是合约规格（乘数、最小变动价位、品种名），实盘/模拟一致，symbol 是主键，换月自动跟新合约。

> 延迟口径：`orders.queue_delay_ms`（发单指令 → 首次 NOTTRADED 入队）、`orders.cancel_delay_ms`（撤单指令 → CANCELLED 终态）。原 `delay_ms` 字段已废弃删除。

### 4.2 三处数据冗余（一份数据分两处记录）

| 冗余 | 风险 | 说明 |
|:--|:--|:--|
| orders.traded ↔ trades.volume | 低 | 标准的明细/汇总设计，靠 vt_orderid 关联，不会错 |
| positions ↔ strategy_status.pos | 中 | 数据源相同，写入时机不同（成交后立即 vs 每 3s），漂移 ≤3s |
| trade_rounds.pnl ↔ daily_pnl.realized_pl | 高 | 数据源不同（引擎 FIFO 配对 vs 策略层累计），算法不一致会对不上，待验证 |

## 5. 后端（web-backend）

独立 Python 包 `web_backend`，FastAPI + SQLAlchemy 2.0，**只读** monitor.db。

> 交易进程和 web-backend 是**两份独立的 models/db 副本**，不是共享包：
> - 交易进程 `run_cta.py` 只 import 自己的 `vnpy_domestic.monitor`（models + MonitorWriter）写库，不依赖 web-backend
> - web-backend 独立 `web_backend` 包（models / db 是交易进程 monitor 的 identical copy），只读 monitor.db
> - **两份 models.py 必须同步改**：`vnpy-domestic/vnpy_domestic/monitor/models.py` 与 `web-backend/web_backend/models.py`

### 5.1 文件结构

```
web-backend/
├── pyproject.toml            # fastapi / sqlalchemy / uvicorn 依赖
└── web_backend/
    ├── models.py             # monitor.db 14 张表（SQLAlchemy 2.0，与交易进程 monitor 同步）
    ├── api.py                # 18 个 REST 只读接口
    ├── ws.py                 # WebSocket /ws 每 3s 全量快照广播
    ├── db.py                 # SQLite 连接 + 自动建表 + 迁移
    ├── main.py               # FastAPI 入口（uvicorn :8000）
    └── seed_demo.py          # demo 造数（本地联调验证）
```

> 交易进程侧的写库模型在 `vnpy-domestic/vnpy_domestic/monitor/`（models.py + writer.py）。

### 5.2 数据写入层（MonitorWriter）

设计：`queue + 独立线程落库`，交易线程只入队、立即返回，不阻塞交易主循环。写库失败只记日志、不抛回交易线程。

埋点位置（RolloverCtaEngine 现有方法末尾追加，不动核心逻辑）：

| 现有方法 | 追加调用 | 写入 |
|:--|:--|:--|
| `process_order_event` | `write(Order(...))` | 委托（入队/撤单延迟已算好） |
| `process_trade_event` | `write(Trade(...))` | 成交（滑点已算好） |
| `process_trade_event`（平仓分支） | `settle_close(with_details=True)` → `write(TradeRound(...))` | 平仓配对 |
| `_write_position_snapshot` | `write(Position(...))` | 持仓快照（多空分记，含浮动盈亏） |
| `_on_account_event` | `write(Account(...))` | 资金 |
| `process_timer_event` | `write(StrategyStatus(...))` + `SystemMetric(...)` | 状态心跳 + 系统指标 |
| `reset_daily_pl` 附近 | `write(DailyPnl(...))` | 每日盈亏 |
| `write_log` | `write(Log(...))` | 日志 |
| MyBarGenerator on_bar | `write(Kline(...))` | 1min K线 |

`db_path` 用显式共享绝对路径（`MONITOR_DB_PATH`），account 值由 CTP_MODE 决定（real / simnow）。

### 5.3 REST 接口（18 个，全部只读）

| 路径 | 参数 | 返回 |
|:--|:--|:--|
| `/api/account/latest` | `account?` | 最新账户快照 |
| `/api/positions` | `account?` `strategy?` | 最新持仓列表 |
| `/api/account-positions` | `account?` | CTP 账户实际持仓 |
| `/api/account-intraday-pnl` | `account?` | 账户日内盈亏 |
| `/api/strategies` | `account?` | 策略状态列表（含 pos/params/vars/pnl） |
| `/api/orders` | `account?` `strategy?` `date?` `limit?` | 委托列表（倒序，按 vt_orderid 去重） |
| `/api/orders/stats` | `account?` | 订单状态统计（活跃挂单/今日委托，按 vt_orderid 去重） |
| `/api/orders/daily_freq` | `account?` | 每日下单频次 |
| `/api/trades` | `account?` `strategy?` `date?` `limit?` | 成交列表（倒序，含滑点） |
| `/api/daily_pnl` | `account?` `strategy?` `days=30` | 盈亏序列 |
| `/api/logs` | `account?` `strategy?` `limit=200` | 日志列表 |
| `/api/system/latest` | - | 最新系统指标 |
| `/api/time` | - | 服务器当前北京时间 |
| `/api/klines` | `account?` `symbol?` `interval=1m` `date?` | K线序列 |
| `/api/stats/summary` | `account?` `strategy?` `days=30` | 胜率/盈亏比/持仓时间/买卖分开 |
| `/api/equity` | `account?` `strategy?` `days=30` | 净值曲线 |
| `/api/account/margin_series` | `account?` `days=30` | 保证金占用序列 |
| `/api/contract` | `symbol?` | 合约规格（乘数/最小变动价位） |

> account 参数不传时返回全部账户，前端按需筛选。

### 5.4 WebSocket

`/ws` 单 broadcaster 每 3 秒查库全量快照，广播给所有连接：账户/持仓/策略状态。数据带 account 字段，前端按账户过滤。

实现要点：
- broadcaster 无连接时跳过查库（`if not active: continue`）
- 死连接清理别边遍历边 remove（用 `for ws in list(active)`）
- 高频数据（账户/持仓）由引擎 3s 快照保证新鲜度，WS 3s 推送与之对齐

### 5.5 绩效统计与成交配对

胜率、盈亏比、持仓时间、买卖分开、净值曲线，底层是同一个算法：**成交配对**——把开仓成交和对应的平仓成交配对成「完整交易」，算单笔盈亏和持仓时间，再聚合。

配对规则与 `position_lots.py` 的 `settle_close` 完全一致（四交易所规则）：DCE/CZCE/GFEX 先开先平、CFFEX 先平今、SHFE/INE 平今平昨。按「策略 + 合约 + 方向」分组，开仓成交排队，平仓成交按规则 FIFO 匹配，写 `trade_rounds` 表。

## 6. 前端（web-frontend）

### 6.1 技术栈

Vue3 + Vite + Vue Router + Element Plus + ECharts + lightweight-charts + Axios，纯 JS 不上 TypeScript。K线用 lightweight-charts（TradingView 同源，约 45KB），其余图表用 ECharts。

### 6.2 目录结构

```
web-frontend/
├── index.html
├── vite.config.js            # /api、/ws 代理到 8000
├── .env.development          # VITE_USE_MOCK=false（本地走真实后端）
├── .env.production           # VITE_USE_MOCK=false
├── package.json
└── src/
    ├── main.js               # 挂载 Element Plus + Router
    ├── App.vue               # 左侧导航 + 顶部 header + 内容区
    ├── router/index.js
    ├── store.js              # 全局账户状态（currentAccount / wsConnected）
    ├── api/
    │   ├── http.js           # axios 实例 + 拦截器
    │   ├── index.js          # 接口函数 + mock 开关
    │   ├── mock.js           # mock 数据生成（real / simnow 双账户）
    │   └── ws.js             # WebSocket 封装（自动重连 + mock 降级）
    ├── styles/theme.css      # 主题变量（主色 / 红涨绿跌）
    ├── components/           # AccountCard / AccountSwitch / 图表组件
    └── views/                # 5 个视图
```

### 6.3 视图（实际落地 5 个，策略维度）

| 路由 | 视图 | 内容 |
|:--|:--|:--|
| `/` | Strategies | 首页策略卡片（按账户分组） |
| `/strategy/:account/:strategy` | StrategyDetail | 策略详情：净值/回撤/K线/合约信息/账户持仓 |
| `/dashboard` | Dashboard | 账户资金卡 + 系统指标 |
| `/orders` | Orders | 订单列表（活跃挂单 + 状态统计） |
| `/logs` | Logs | 实时日志流 |

> 早期设计是「八个视图」，实际落地改成了策略维度（首页策略卡片 → 点击进策略详情页），并额外加了回撤图、合约信息卡、账户持仓表、accountid 显示、3s 高频快照联动、服务器时间显示。

### 6.4 数据流

每个视图 `onMounted` 先 REST 拉历史全量（画曲线、填表），再 `connectWS` 订阅增量；WS snapshot 带 account 字段，按当前选中账户筛选；切换账户时 REST 重拉该账户历史全量。

### 6.5 双账户展示

- 顶部「实盘 / 模拟 / 全部」切换；选「全部」时 Dashboard 分两栏并排对比
- 策略状态卡片加环境角标：实盘红 `[实盘]`、模拟蓝 `[模拟]`
- 所有数据按 account 字段标注运行环境

### 6.6 视觉规范（白底红涨绿跌）

| 元素 | 颜色 |
|:--|:--|
| 页面背景 | `#f5f6f8`（浅灰） |
| 卡片背景 | `#ffffff`（纯白） |
| 边框 | `#e5e7eb` |
| 标题/正文/次要 | `#1a1a1a` / `#666` / `#999` |
| 主色 | `#2563eb`（蓝） |
| 涨 | `#e03131`（红） |
| 跌 | `#16a34a`（绿） |

红涨绿跌是国内期货硬习惯，不能照搬国外绿涨红跌。布局：左侧固定导航（约 220px）+ 顶部薄 header + 右侧卡片网格。表格去斑马纹、数字右对齐 + `tabular-nums` 等宽数字。

### 6.7 K线图（lightweight-charts v5）

```js
import { createChart, CandlestickSeries } from 'lightweight-charts'
const chart = createChart(el, { /* 白底 */ })
const series = chart.addSeries(CandlestickSeries, {
  upColor: '#e03131', downColor: '#16a34a',   // 红涨绿跌
  wickUpColor: '#e03131', wickDownColor: '#16a34a'
})
series.setData(klines.map(k => ({ time: k.datetime.slice(0, 10), open: k.open, high: k.high, low: k.low, close: k.close })))
// 进出场 markers：开仓箭头在下、平仓箭头在上
```

进出场点位：拿 `/api/klines`（蜡烛）+ `/api/trades`（标点位），成交时间对齐到 K线，不需要新表。

## 7. 本地运行（Windows 开发）

### 7.1 目录结构（本地 Windows 测试开发）

一个 `vnpy/` 根目录装下三个 git 仓库 + 两个账户目录 + 共享库，全部平级：

```
Desktop/vnpy/
├── vnpy-domestic/      ① 交易核心（git 仓库）——源码统一放这
│   ├── run_cta.py        入口（父子双进程守护）
│   ├── vnpy_domestic/    包（RolloverCtaEngine + monitor + trader）
│   ├── strategies/       策略源码（git 白名单只提交 dual_ma.py，其余策略都不提交）
│   ├── scripts/          cleanup_monitor.py（监控库清理脚本）
│   ├── .vntrader/        本地默认配置+数据（gitignore：只保留 *.example 和 vt_setting.json）
│   │   ├── cta_strategy_setting.json / .example
│   │   ├── secrets.yaml / .example（凭证不提交）
│   │   └── database.db / ctp/ / log/ / trading_times.csv / vt_setting.json
│   └── *.md              部署/使用文档（本手册、Linux部署、飞书机器人等）
├── web-backend/        ② 监控后端（git 仓库）——只读 monitor.db，独立副本（见第 5 章）
│   └── web_backend/      api / ws / db / models / seed_demo / main
├── web-frontend/       ③ 监控前端（git 仓库）——Vue3 + Vite
│   ├── src/  package.json  vite.config.js  index.html  public/
│   ├── node_modules/     npm install 生成（gitignore）
│   └── dist/             npm run build 生成（gitignore，nginx 用）
├── 实盘/               实盘账户目录（非 git）——只放运行时数据
│   ├── .vntrader/        实盘配置+数据
│   └── strategies/       策略副本（short_dual_ma 玉米做空）
├── 模拟盘simnow/       simnow 账户目录（非 git）——同上
│   ├── .vntrader/        模拟配置+数据
│   └── strategies/       策略副本（corn_scalper / dual_ma / save_bar）
└── monitor.db          共享监控库（MONITOR_DB_PATH 指向它，三进程共用）
    ├── monitor.db-shm   SQLite WAL 附属文件（自动生成，别手动删）
    └── monitor.db-wal
```

关键：**三个 git 仓库平级 clone，两个账户目录只是「工作目录」**（跑哪个账户 cd 到哪个），`monitor.db` 脱离 cwd 用绝对路径共享。

### 7.2 依赖与 import

交易进程和 web-backend 是**两份独立的 models/db 副本**，不是共享包：

- 交易进程 `run_cta.py`：只 import 自己的 `vnpy_domestic.monitor`（models + MonitorWriter）写库，不依赖 web-backend
- web-backend：独立 `web_backend` 包（models / db 是交易进程 monitor 的 identical copy），只读 monitor.db
- **两份 models.py 必须同步改**：`vnpy-domestic/vnpy_domestic/monitor/models.py` 与 `web-backend/web_backend/models.py`（文件头注释已注明「change in BOTH files together」）
- 交易进程 import 解析：从账户目录 cd 运行 `python ../vnpy-domestic/run_cta.py`，vnpy_domestic 包随源码目录被找到
- 后端 / seed：cd 到 web-backend 目录再跑（cwd 进 sys.path）

前端 node_modules 已装好，不用重装。

### 7.3 环境变量

> **shell 语法别混**：本地 Windows 可能开 PowerShell（默认）、git-bash、cmd 三种 shell，设环境变量语法各不同——PowerShell 用 `$env:VAR="值"`，git-bash 用 `export VAR=值`，cmd 用 `set VAR=值`。下文示例按 git-bash 的 `export` 写，PowerShell 把 `export VAR=值` 换成 `$env:VAR="值"` 即可。**尤其别在 PowerShell 里用 `set`**：`set` 是 `Set-Variable` 的别名，设的是 PowerShell 内部变量而非环境变量，子进程读不到，会报「请设置 CTP_USER 和 CTP_PASSWORD」。

实盘凭证走环境变量；simnow 凭证写在账户目录 secrets.yaml。simnow 只需 1 个环境变量 CTP_MODE（broker 9999 / 服务器已写死）：

```bash
export CTP_MODE=simnow
# 凭证不在环境变量，写在 模拟盘simnow/.vntrader/secrets.yaml（simnow_user / simnow_password）
```

实盘 7 个全走环境变量：

```bash
export CTP_MODE=real
export CTP_USER=<实盘账号>
export CTP_PASSWORD=<实盘密码>
export CTP_BROKER=<brokerID>
export CTP_TD_SERVER=<td地址>
export CTP_MD_SERVER=<md地址>
export CTP_APPID=<产品名>
export CTP_AUTHCODE=<授权编码>
```

监控写库（三进程共用同一绝对路径；**不设则静默不写库、无任何提示**，前端面板数据为空）：

```bash
export MONITOR_DB_PATH="C:/Users/<你>/Desktop/vnpy/monitor.db"
```

> 注意：这些 export 只对「当前窗口」生效，关窗即失。一个窗口只能前台跑一个进程，跑几个进程开几个窗口。

### 7.4 本地跑前后端（不连 CTP，随时能跑）

```bash
# 1. 造 demo 数据（real + simnow 两账户）
cd /c/Users/<你>/Desktop/vnpy/web-backend
export MONITOR_DB_PATH="C:/Users/<你>/Desktop/vnpy/monitor.db"
python -m web_backend.seed_demo

# 2. 起后端（窗口 A）
cd /c/Users/<你>/Desktop/vnpy/web-backend
export MONITOR_DB_PATH="C:/Users/<你>/Desktop/vnpy/monitor.db"
python -m uvicorn web_backend.main:app --host 127.0.0.1 --port 8000

# 3. 起前端（窗口 B）
cd /c/Users/<你>/Desktop/vnpy/web-frontend
npm run dev          # http://localhost:5173

# 4. 浏览器开 http://localhost:5173，顶部「全部/实盘/模拟」切账户
```

前端 `.env.development` 已是 `VITE_USE_MOCK=false`（走真实后端），vite 已把 `/api` 和 `/ws` 代理到 8000。

### 7.5 本地跑交易进程（连 CTP，要交易时段 + 凭证）

源码统一在 vnpy-domestic，账户目录只放 .vntrader + strategies。跑哪个账户 cd 到哪个账户目录，再指向统一 run_cta.py：

```bash
# 模拟盘 simnow（凭证在 模拟盘simnow/.vntrader/secrets.yaml）
cd /c/Users/<你>/Desktop/vnpy/模拟盘simnow
export CTP_MODE=simnow
export MONITOR_DB_PATH="C:/Users/<你>/Desktop/vnpy/monitor.db"
python ../vnpy-domestic/run_cta.py

# 实盘（7 个凭证全走环境变量）
cd /c/Users/<你>/Desktop/vnpy/实盘
export CTP_MODE=real
# ... 7 个凭证 ...
export MONITOR_DB_PATH="C:/Users/<你>/Desktop/vnpy/monitor.db"
python ../vnpy-domestic/run_cta.py
```

交易时段：周一~周五日盘 8:45–15:01、夜盘 20:45–23:05（非交易时段父进程不 spawn 子进程）。日志看账户目录 `.vntrader/log/vt_YYYYMMDD.log`。

### 7.6 验证命令速查

```bash
# 后端健康检查
curl -s http://127.0.0.1:8000/api/time
curl -s "http://127.0.0.1:8000/api/account/latest"
# 前端（走 vite proxy）
curl -s http://localhost:5173/api/time
# 查 monitor.db 各表行数
python -c "import sqlite3; c=sqlite3.connect('C:/Users/<你>/Desktop/vnpy/monitor.db'); [print(t, c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]) for t in ['accounts','logs','orders','trades','positions','account_positions','strategy_status','system_metrics','daily_pnl','account_intraday_pnl','klines','strategy_intraday','trade_rounds','contracts']]"
```

---

# 第二部分 · Linux 从 GitHub clone 部署

## 8. 整体架构

```
浏览器 ──▶ nginx (80)
              ├── /            → 前端静态文件 dist/
              ├── /api/        → 反代 web-backend (127.0.0.1:8000)
              └── /ws          → 反代 WebSocket (127.0.0.1:8000, 带 Upgrade 头)
                                    │
                                    ▼
                            web-backend（只读 monitor.db）
```

- 对外只有一个 80 端口（上 HTTPS 后是 443）。
- 后端 8000 只绑 `127.0.0.1`，外网直连不到，仅本机 nginx 能转发。
- 进程分工：交易核心（vnpy-domestic 写 monitor.db）、web-backend（只读 monitor.db 出接口）、nginx（静态 + 反代）。

服务器目录布局（腾讯云 Ubuntu，目标机器）：

```
/home/ubuntu/
├── vnpy-domestic/          ← git clone 交易核心（共用一份源码）
│   ├── run_cta.py
│   ├── vnpy_domestic/  strategies/  scripts/
├── 实盘/                    ← 实盘账户目录（非 git）——只放 .vntrader + strategies
│   ├── .vntrader/          实盘配置+数据（secrets / database.db / log）
│   └── strategies/         策略副本
├── 模拟盘simnow/           ← simnow 账户目录（非 git）——同上
│   ├── .vntrader/          模拟配置+数据
│   └── strategies/         策略副本
├── web-backend/            ← git clone 监控后端（独立包）
│   └── web_backend/
├── web-frontend/           ← git clone 前端（或只放 dist/）
│   └── dist/               前端构建产物
└── vnpy-data/
    └── monitor.db          ← 共享监控库（三进程共用）
```

> 与本地 Windows 结构一致：vnpy-domestic 一份源码共用，账户目录（实盘/模拟盘simnow）只放 .vntrader + strategies，跑哪个账户 cd 到哪个账户目录、`python ../vnpy-domestic/run_cta.py`。

---

## 9. 部署方式选择（前端）

前端有三种方式拿到 `dist/`（后端无论哪种都要 clone + pip 装依赖）：

| 方式 | 服务器是否需要 Node | 说明 |
| :--- | :--- | :--- |
| A. clone + 服务器 build | 需要 | 服务器装 Node 22，clone 后 `npm install && npm run build` |
| B. 本地 build + scp | 不需要 | 本地构建好 dist，直接传服务器 |
| C. 提交 dist 到 git | 不需要 | clone 下来直接有 dist（见附录 A） |

推荐：个人监控面板用 **C**（最省事），讲究仓库干净用 **A** 或 **B**。下文按 A 展开，B / C 见附录。

## 10. 前端部署

### 10.1 安装 Node（仅方式 A）

vite 8.x 要求 Node ≥ 20.19，装 Node 22 LTS：

```bash
export NVM_NODEJS_ORG_MIRROR=https://npmmirror.com/mirrors/node
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
source ~/.bashrc
nvm install 22
node -v      # 验证：输出 v22.x
```

> 若 `raw.githubusercontent.com` 拉不动，改用 ghproxy 代理或直接下载 npmmirror 的 Node 二进制包（见附录 C）。

### 10.2 clone + 构建

```bash
cd /home/ubuntu
sudo git clone https://github.com/practicemakeperfect666/web-frontend.git
sudo chown -R ubuntu:ubuntu web-frontend      # ubuntu 换成你的用户名
cd web-frontend
npm config set registry https://registry.npmmirror.com
npm install
npm run build
ls dist/      # 验证：能看到 index.html 和 assets/
```

构建产物在 `/home/ubuntu/web-frontend/dist`，这是 nginx 要托管的目录。

### 10.3 方式 B：本地 build + scp（服务器免装 Node）

本地（Windows git-bash）：

```bash
cd /c/Users/29523/Desktop/vnpy/web-frontend
npm run build
ssh ubuntu@你的IP 'mkdir -p /home/ubuntu/web-frontend/dist'
scp -r dist/* ubuntu@你的IP:/home/ubuntu/web-frontend/dist/
```

## 11. 后端部署

后端只依赖三个包（fastapi / sqlalchemy / uvicorn），用独立 venv，不碰实盘 vnpy-domestic 的环境。

### 11.1 clone

```bash
cd /home/ubuntu
sudo git clone https://github.com/practicemakeperfect666/web-backend.git
sudo chown -R ubuntu:ubuntu web-backend
```

### 11.2 venv + 安装

```bash
cd /home/ubuntu/web-backend
python3 -m venv venv
venv/bin/pip install -e . -i https://mirrors.tencentyun.com/pypi/simple
```

### 11.3 MONITOR_DB_PATH 环境变量（关键）

web-backend 只读交易进程写出的 `monitor.db`，路径必须和交易进程完全一致。默认是 `~/vnpy-data/monitor.db`（见 `web_backend/db.py`）。先确认你实盘 vnpy-domestic 进程的 `MONITOR_DB_PATH` 实际指向哪，下面用它。

### 11.4 systemd service

```bash
sudo nano /etc/systemd/system/web-backend.service
```

粘贴（`MONITOR_DB_PATH` 换成交易进程真实路径，`User` 换成你的用户名）：

```ini
[Unit]
Description=vnpy-domestic web monitor backend
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/web-backend
Environment=MONITOR_DB_PATH=/home/ubuntu/vnpy-data/monitor.db
ExecStart=/home/ubuntu/web-backend/venv/bin/python -m uvicorn web_backend.main:app --host 127.0.0.1 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

> `MONITOR_DB_PATH` 是固定路径不是敏感凭证，直接写进 unit 文件的 `Environment=` 即可（持久）。不要用 `systemctl set-environment` 那套内存注入——那会导致重启后丢失。

### 11.5 启动 + 验证

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now web-backend
sudo systemctl status web-backend     # 验证：active (running)

curl http://127.0.0.1:8000/api/time   # 验证：返回服务器时间 JSON
```

## 12. 交易进程部署（vnpy-domestic）

交易核心用 conda vnpy 环境（和本地一致）。vnpy-domestic 源码只 clone 一份共用，账户目录（实盘/模拟盘simnow）各建一个、只放 .vntrader + strategies，跑哪个账户 cd 到哪个账户目录。

### 12.1 环境 + clone

```bash
# 前提：conda vnpy 环境已装好（vnpy + vnpy_ctp + 本项目依赖）
# 1. clone 交易核心（一份共用源码）
git clone https://github.com/practicemakeperfect666/vnpy-domestic.git /home/ubuntu/vnpy-domestic

# 2. 建两个账户目录（只放 .vntrader + strategies，跑哪个 cd 到哪个）
mkdir -p /home/ubuntu/实盘/.vntrader /home/ubuntu/实盘/strategies
mkdir -p /home/ubuntu/模拟盘simnow/.vntrader /home/ubuntu/模拟盘simnow/strategies

# 3. 从源码模板复制配置，改成自己的（cp .example 去后缀，再手写）
for d in /home/ubuntu/实盘 /home/ubuntu/模拟盘simnow; do
    cp /home/ubuntu/vnpy-domestic/.vntrader/secrets.yaml.example              $d/.vntrader/secrets.yaml
    cp /home/ubuntu/vnpy-domestic/.vntrader/cta_strategy_setting.json.example $d/.vntrader/cta_strategy_setting.json
    cp /home/ubuntu/vnpy-domestic/.vntrader/vt_setting.json                   $d/.vntrader/vt_setting.json
done
# 编辑各自 .vntrader/secrets.yaml（simnow 密码 / 飞书 webhook）、cta_strategy_setting.json（策略参数）
# trading_times.csv（交易时段）gitignore 了，从本地 scp 上去

# 4. 交易核心 + 后端包装进 conda vnpy 环境（镜像 mirrors.tencentyun.com）
pip install -e /home/ubuntu/vnpy-domestic   # 交易核心
pip install -e /home/ubuntu/web-backend     # 后端（web_backend 包进环境）

# 5. 建共享库目录
mkdir -p /home/ubuntu/vnpy-data
```

### 12.2 systemd 交易 unit

两个 unit（vnpy-trade-real / vnpy-trade-sim）靠各自 unit 里的 `Environment=CTP_MODE` 区分（实盘 real / 模拟 simnow）；敏感凭证不写进 unit 文件：

```ini
# /etc/systemd/system/vnpy-trade-real.service
[Unit]
Description=vnpy-domestic real trading
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/实盘
Environment=CTP_MODE=real
Environment=MONITOR_DB_PATH=/home/ubuntu/vnpy-data/monitor.db
ExecStart=/home/ubuntu/miniconda3/envs/vnpy/bin/python ../vnpy-domestic/run_cta.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/vnpy-trade-sim.service
[Unit]
Description=vnpy-domestic simnow trading
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/模拟盘simnow
Environment=CTP_MODE=simnow
Environment=MONITOR_DB_PATH=/home/ubuntu/vnpy-data/monitor.db
ExecStart=/home/ubuntu/miniconda3/envs/vnpy/bin/python ../vnpy-domestic/run_cta.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
# 实盘凭证注入（内存，不落盘；重启服务器后清空，需重新执行）
sudo systemctl set-environment \
  CTP_USER=<实盘账号> CTP_PASSWORD=<实盘密码> CTP_BROKER=<brokerID> \
  CTP_TD_SERVER=<td地址> CTP_MD_SERVER=<md地址> CTP_APPID=<产品名> CTP_AUTHCODE=<授权编码>
sudo systemctl restart vnpy-trade-real
```

> 区分机制：`CTP_MODE` 写死在各自 unit 的 `Environment=`（unit 变量每进程独立，实盘读 real、模拟盘读 simnow，互不覆盖）；**实盘凭证**走 `systemctl set-environment` 注入内存（敏感，不落盘，重启服务器后清空需重新注入）；**模拟盘凭证**写在账户目录 `.vntrader/secrets.yaml`（simnow_user / simnow_password，虚拟资金不敏感）。三条各管各的：CTP_MODE 走 unit 文件、实盘凭证走 manager 环境、模拟盘凭证走 secrets.yaml，互不串号。飞书机器人仍在实盘父进程里跑 3000，不收编也不影响。
> 开启/关闭监控后端：`sudo systemctl enable --now web-backend` / `sudo systemctl disable --now web-backend`（交易不受影响）。

## 13. nginx 反代

```bash
sudo apt install -y nginx
sudo nano /etc/nginx/sites-available/web-monitor
```

粘贴整段（`server_name` 填域名或服务器公网 IP）：

```nginx
server {
    listen 80;
    server_name 你的域名或IP;

    # 前端静态文件
    root /home/ubuntu/web-frontend/dist;
    index index.html;

    # SPA 回退（history 路由，刷新子页面必须）
    location / {
        try_files $uri $uri/ /index.html;
    }

    # REST 反代到后端
    location /api/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    # WebSocket 反代（实时快照走这里）
    location /ws {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 3600s;
    }
}
```

三个关键点，缺一个都会坏：

1. `try_files ... /index.html` —— 前端用 createWebHistory（HTML5 history 路由），直接刷新 `/dashboard`、`/orders` 会 404，这行把找不到的路径回退给 index.html 让前端路由接管。
2. `location /api/` —— 前端 axios `baseURL` 是空串，请求打同域 `/api/xxx`，靠 nginx 转发到 8000。
3. `location /ws` 的 `Upgrade`/`Connection "upgrade"` + `http_version 1.1` —— 前端生产模式用 `location.host` 连 `/ws`，缺这些头 WebSocket 握手失败，实时快照不刷新。

启用 + 生效：

```bash
sudo ln -s /etc/nginx/sites-available/web-monitor /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t                     # 验证：syntax is ok / test is successful
sudo systemctl reload nginx
```

## 14. 端口与安全组

- 对外只暴露 80（HTTPS 后加 443），腾讯云安全组只放行这两个。
- 8000 端口绑 `127.0.0.1`，外网直连不到，安全组不用开。
- 后端 FastAPI 是 CORS 全开、无鉴权的，绑 127.0.0.1 等于物理隔离外网，只让本机 nginx 转发进来。**不要**把后端改成 `--host 0.0.0.0` 裸暴露。

## 15. 验证清单

1. 浏览器开 `http://你的IP/` → 看到策略卡片首页。
2. 刷新 `http://你的IP/dashboard` → 不 404，说明 try_files 生效。
3. F12 → Console 无 `/api` 报错，说明 REST 反代通。
4. F12 → Network → WS → `/ws` 状态 101，说明 WebSocket 握手成功。
5. 页面数据 3 秒自动刷新 = WS 正常推送。
6. `sudo journalctl -u web-backend -f` 能看到访问记录。

## 16. HTTPS 与公网访问

**不备案不买域名**：直接 `http://公网IP`（80 端口）访问面板即可；飞书控制回调仍用 localtunnel 穿透（见飞书控制机器人文档）。更稳的替代是 SSH 隧道 `ssh -L 8000:127.0.0.1:8000 ubuntu@服务器` 或 Tailscale / ZeroTier 组内网，不把端口暴露公网。

**要 HTTPS + 域名**（有域名时）：

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d 你的域名
```

证书自动续期。前端 ws.js 会自动切 `wss`，nginx 的 WebSocket 反代配置不用改。

---

## 附录 A：dist 是否提交的取舍

`.gitignore` 默认忽略 `dist`（构建产物）。两种模式：

| 模式 | 做法 | 代价 |
| :--- | :--- | :--- |
| 提交 dist（省事） | 删掉 `.gitignore` 里的 `dist`，本地 build 后 `git add dist` 提交 | 每次 build 文件名哈希都变，git 历史变脏、仓库变大 |
| 不提交 dist（干净） | 保持现状，服务器 build 或本地 build scp | 服务器得装 Node 或手动传产物 |

个人监控面板推荐提交 dist，部署最省事。

## 附录 B：.env 文件说明

前端 `.gitignore` 规则：`.env`、`.env.*` 都忽略，仅 `!.env.example` 例外上传。三个文件内容都是同一个变量 `VITE_USE_MOCK=false`，非密钥。

- `.env.example` → 上传（模板）
- `.env.development` / `.env.production` → 忽略，不上传

服务器 build 时缺 `.env.production` 无影响：`VITE_USE_MOCK` 为 undefined，自动走真实后端，正是生产想要的行为。

## 附录 C：国内网络加速

腾讯云访问 GitHub / npm 官方源慢或超时，已内置的处理：

- npm 已配 npmmirror：`npm config set registry https://registry.npmmirror.com`
- pip 已配腾讯云镜像：`-i https://mirrors.tencentyun.com/pypi/simple`
- git clone 卡住时：改用 ssh 协议 `git@github.com:...`（配 ssh key），或走 ghproxy 代理前缀
- Node 下载已配 npmmirror 镜像：`NVM_NODEJS_ORG_MIRROR=https://npmmirror.com/mirrors/node`

---

# 坑清单（提前规避）

1. **合约覆盖坑**：单 main_engine 双 CTP 会合约冲突，必须走「两个进程」。
2. **import 不冲突，数据文件冲突**：两个进程 import vnpy 互不影响（内存隔离），但 .vntrader 数据文件要隔离，靠两个 WorkingDirectory。
3. **SQLite 并发**：两个交易进程写 + 后端读，开 WAL + busy_timeout。
4. **写库别阻塞交易线程**：MonitorWriter 用 queue + 独立线程，交易线程只入队。
5. **时区**：实盘服务器 UTC，写库统一 `zoneinfo("Asia/Shanghai")` 转北京时间；`trade.datetime` 带 tzinfo 要先 `astimezone`。
6. **monitor.db 用显式共享路径**：双账户下 `get_file_path("monitor.db")` 会解析成两个文件，必须用 `MONITOR_DB_PATH` 显式指定同一个绝对路径。
7. **WS 死连接清理**：遍历 `active` 别边删边遍历（跳元素），用 `for ws in list(active)`。
8. **broadcaster 空转**：无连接时跳过查库（`if not active: continue`）。
9. **HTTPS 后 WS 用 wss**：前端按 `location.protocol` 自动切 ws/wss。
10. **监控只读**：REST/WS 全部只读不下单；纯 HTTP 暴露公网必须上 nginx basic_auth，别把 8000 裸奔。
11. **飞书控制只实盘开**：两个进程都起 3000 会抢端口。
12. **实盘信息不落盘**：实盘 CTP 全走环境变量；模拟密码可进 secrets.yaml，但绝不写源码（公开仓库）。
13. **依赖补 sqlalchemy**：pyproject 缺 `sqlalchemy>=2.0` 不补 import models 直接崩。
14. **gitignore 漏洞先堵**：`.vntrader` 若已被跟踪，先 `git rm -r --cached` 再填真实密码；配置模板用 `.example` 提交、真实文件 gitignore。
15. **vnpy AccountData 字段有限**：只有 balance/frozen/available，没保证金和盈亏字段；账户卡 margin/pnl 改由 3s 快照从持仓 + 最新价计算。
16. **字段名 snake_case 不转 camelCase**：前端直接用后端 Python 返回的字段名，省掉映射层。
17. **lightweight-charts v4/v5 API 不同**：`addSeries(CandlestickSeries)`（v5）vs `addCandlestickSeries()`（v4）。
18. **时间零转换**：后端已存北京时间字符串，前端直接显示，别再 `new Date()` 转一遍（带本地时区错乱）。
19. **订单统计按 vt_orderid 去重**：同一订单有多行状态流转（SUBMITTING → ALLTRADED），活跃挂单/今日委托要按 vt_orderid 取最新状态，不能按行计数；日期归口按交易日（夜盘 20:00 后归下一交易日）。
20. **删表字段要连带删列**：models 里删字段时，老库对应列若 `NOT NULL`（如旧 `delay_ms`），新代码 INSERT 不写它就会 `NOT NULL constraint failed`、写库静默失败。迁移时用 `ALTER TABLE ... DROP COLUMN` 把废弃列一起删掉。
21. **MONITOR_DB_PATH 未设静默禁写库**：`RolloverCtaEngine` 里 `if db_path:` 才建 MonitorWriter，不设就整段跳过、无任何报错/提示，前端面板数据为空。排查「前端没实盘数据」先查这个环境变量设没设。
22. **账户目录缺 secrets.yaml 会 fallback 到源码占位符**：`_load_secrets()` 按 cwd → 源码 → home 查找，账户目录没配 secrets.yaml 就回退到源码目录的 `.example` 占位符（webhook「你的webhook」、app_id「cli_xxx」），导致飞书通知报 `incoming webhook access token invalid`、飞书控制假启动。每个账户目录必须配自己的 secrets.yaml。
23. **策略状态汇总 window=0 是显示问题**：引擎状态汇总只读 `strategy.bg`（1 分钟 MyBarGenerator），双 BarGenerator 做 N 分钟周期时，`bg_window`（window=N）不被显示，状态里 `window=0` 不代表周期没生效。

---

# 与专业团队的差距（后续改进方向）

按「补上就明显上台阶」排序：

| 优先级 | 方向 | 现状 → 改法 |
|:--|:--|:--|
| 最高 | 自动化测试 | 无 tests/ → 给 settle_close / 换月 / 逐日盯市 / normalize_vt_symbol 补 pytest |
| 高 | CI（GitHub Actions） | push 无自动检查 → 加 ci.yml 跑 pytest + ruff |
| 中 | 密钥管理 | secrets.yaml 明文存飞书 token → 全走环境变量，secrets 只留 simnow 密码 |
| 中 | 代码规范强制 | 无 ruff 门槛 → 加 ruff 配置 + CI 强制 |
| 中 | 可观测性 | 只有日志+飞书 → system_metrics 趋势图 + 阈值告警，后期 Prometheus/Grafana |
| 高 | 风控独立 | 风控散在策略里 → 抽独立风控，下单前四道校验（单日亏损/敞口/频率/额度） |

建议顺序：pytest 补核心测试 → CI + ruff → 密钥走环境变量 → 可观测性 → 风控独立。
