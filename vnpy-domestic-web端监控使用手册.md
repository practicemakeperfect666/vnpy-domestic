# Web 端监控使用手册

> 整合自《双账户Web监控方案》《Web前端开发》《运行指导》三份历史文档，并按实际落地代码（14 张表 / 18 个接口 / 5 个视图）校正，补入进程分配说明。
> 最后更新：2026-10

---

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

---

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

4. **「控制」和「通知」是两回事**：控制（@机器人 停/重启）只实盘有；通知（推消息）分群，实盘群、模拟群各一个 webhook（`feishu_webhook_real` / `feishu_webhook_simnow`）。

5. **端口不冲突**：3000（飞书控制）/ 8000（后端）/ 5173（前端 dev）/ 443（nginx）各占各的。

6. **生命周期解耦**：飞书「停止」→ 交易子进程退出 → monitor.db 停更 → web 面板数据停在停止时刻（历史仍可查）；「重启」→ 新子进程 → 恢复写库 → web 恢复实时。web 前后端全程不受影响。

---

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

- `CTP_MODE=simnow`：账号密码走环境变量 `CTP_USER`/`CTP_PASSWORD`，经纪商 9999、服务器 182.254.243.31:30003/30013、产品名、授权码硬编码
- `CTP_MODE=real`：7 个凭证全走环境变量（用户名/密码/brokerID/交易服务器/行情服务器/产品名/授权编码），代码里不留真实信息

### 3.3 数据隔离（两个工作目录）

vnpy 的 `.vntrader` 数据目录按「当前工作目录」解析（cwd 下有 .vntrader 就用 cwd 的，否则用 home 的）。两个进程从不同 cwd 跑，vnpy 数据天然隔离。

| 文件 | 实盘 | 模拟 | 处理方式 |
|:--|:--|:--|:--|
| 代码 run_cta.py / strategies | 同 | 同 | git 一份，clone 两份 |
| .vntrader/database.db、cta_strategy_data.json | 独立 | 独立 | 两个 WorkingDirectory 隔离 |
| 日志文件 | 独立 | 独立 | log_real / log_sim 前缀 |
| monitor.db（监控） | 共享 | 共享 | 同一个库，表带 account 字段 |
| secrets.yaml | 同 | 同 | 共享（含 simnow 密码） |
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
- **通知**（群机器人 webhook）：分两个群，进程启动按 CTP_MODE 选对应 webhook，文案加 `[实盘]`/`[模拟]` 前缀

---

## 4. 监控数据库 monitor.db

文件 `.vntrader/monitor.db`，与 vnpy 的 `database.db` 分开。SQLite + WAL + busy_timeout 处理双进程并发写。所有时间字段存北京时间字符串 `YYYY-MM-DD HH:MM:SS`，写库时 `zoneinfo("Asia/Shanghai")` 统一转换，前端零转换直接显示。

### 4.1 十四张表

| 表名 | 用途 | 关键字段 | 写入时机 |
|:--|:--|:--|:--|
| orders | 订单（委托） | vt_orderid, strategy, account, symbol, direction, offset, price, volume, traded, status, submit_time, delay_ms | 下单/撤单/拒单时 |
| trades | 成交明细 | vt_tradeid, vt_orderid, strategy, account, symbol, price, volume, slippage, delay_ms, trade_time | 成交回报时 |
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
| account_daily_pnl | 账户日内盈亏 | account, accountid, trading_day, realized_pl, floating_pl, balance, snapshot_time | 每 3s 快照 |
| strategy_intraday | 策略日内净值快照 | strategy, account, symbol, realized_pl, floating_pl, equity, snapshot_time | 每 15 分钟 |

> 除 `contracts` 外，所有表都带 `account` 字段区分 `real`（实盘）/`simnow`（模拟盘）。contracts 表是合约规格（乘数、最小变动价位、品种名），实盘/模拟一致，symbol 是主键，换月自动跟新合约。

### 4.2 三处数据冗余（一份数据分两处记录）

| 冗余 | 风险 | 说明 |
|:--|:--|:--|
| orders.traded ↔ trades.volume | 低 | 标准的明细/汇总设计，靠 vt_orderid 关联，不会错 |
| positions ↔ strategy_status.pos | 中 | 数据源相同，写入时机不同（成交后立即 vs 每 3s），漂移 ≤3s |
| trade_rounds.pnl ↔ daily_pnl.realized_pl | 高 | 数据源不同（引擎 FIFO 配对 vs 策略层累计），算法不一致会对不上，待验证 |

---

## 5. 后端（web-backend）

独立 Python 包 `web_backend`，FastAPI + SQLAlchemy 2.0。交易进程埋点 `import web_backend`，后端也 `import web_backend`，models 只有一份，是「交易写库」和「后端读库」的共同契约。

### 5.1 文件结构

```
web-backend/
├── pyproject.toml            # fastapi / sqlalchemy / uvicorn 依赖
└── web_backend/
    ├── models.py             # monitor.db 14 张表（SQLAlchemy 2.0）
    ├── writer.py             # MonitorWriter 写库（交易进程 import）
    ├── api.py                # 18 个 REST 只读接口
    ├── ws.py                 # WebSocket /ws 每 3s 全量快照广播
    ├── db.py                 # SQLite 连接 + 自动建表
    ├── main.py               # FastAPI 入口（uvicorn :8000）
    └── seed_demo.py          # demo 造数（本地联调验证）
```

### 5.2 数据写入层（MonitorWriter）

设计：`queue + 独立线程落库`，交易线程只入队、立即返回，不阻塞交易主循环。写库失败只记日志、不抛回交易线程。

埋点位置（RolloverCtaEngine 现有方法末尾追加，不动核心逻辑）：

| 现有方法 | 追加调用 | 写入 |
|:--|:--|:--|
| `process_order_event` | `write(Order(...))` | 委托 |
| `process_trade_event` | `write(Trade(...))` | 成交（滑点/延迟已算好） |
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
| `/api/account-daily-pnl` | `account?` | 账户日内盈亏 |
| `/api/strategies` | `account?` | 策略状态列表（含 pos/params/vars/pnl） |
| `/api/orders` | `account?` `strategy?` `date?` `limit?` | 委托列表（倒序，按 vt_orderid 去重） |
| `/api/orders/stats` | `account?` | 订单状态统计（活跃挂单/今日委托，按 vt_orderid 去重） |
| `/api/orders/daily_freq` | `account?` | 每日下单频次 |
| `/api/trades` | `account?` `strategy?` `date?` `limit?` | 成交列表（倒序，含滑点/延迟） |
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

---

## 6. 前端（web-frontend）

### 6.1 技术栈

Vue3 + Vite + Vue Router + Element Plus + ECharts + lightweight-charts + Axios，纯 JS 不上 TypeScript。K线用 lightweight-charts（TradingView 同源，约 45KB），其余图表用 ECharts。

### 6.2 目录结构

```
web-frontend/
├── index.html
├── vite.config.js            # /api、/ws 代理到 8000
├── .env.development          # VITE_USE_MOCK=true（本地 mock）
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

---

## 7. 本地运行

### 7.1 目录结构

```
Desktop/vnpy/
├── vnpy-domestic/      交易核心（run_cta.py + RolloverCtaEngine + strategies）
├── web-backend/        监控后端（FastAPI，读 monitor.db）
├── web-frontend/       监控前端（Vue3 + Vite）
├── 实盘/               实盘账户目录（只放 .vntrader + strategies）
├── 模拟盘simnow/       simnow 账户目录（只放 .vntrader + strategies）
└── monitor.db          监控库（三进程共用）
```

### 7.2 依赖与 import

web-backend 是共享 Python 包，靠两种方式让 import 找到它：

- 交易进程：`run_cta.py` 顶部 `sys.path.insert(0, "../web-backend")`，相对 cwd 解析
- 后端 / seed：cd 到 web-backend 目录再跑（cwd 进 sys.path）

前端 node_modules 已装好，不用重装。

### 7.3 环境变量

CTP 凭证不写文件，走环境变量。simnow 只需 3 个（broker 9999 / 服务器已写死）：

```bash
export CTP_MODE=simnow
export CTP_USER=<simnow账号>
export CTP_PASSWORD=<simnow密码>
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

监控写库（三进程共用同一绝对路径，不设则不写库）：

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
# 模拟盘 simnow
cd /c/Users/<你>/Desktop/vnpy/模拟盘simnow
export CTP_MODE=simnow
export CTP_USER=<simnow账号>
export CTP_PASSWORD=<simnow密码>
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
python -c "import sqlite3; c=sqlite3.connect('C:/Users/<你>/Desktop/vnpy/monitor.db'); [print(t, c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]) for t in ['accounts','logs','orders','trades','positions','account_positions','strategy_status','system_metrics','daily_pnl','account_daily_pnl','klines','strategy_intraday','trade_rounds','contracts']]"
```

---

## 8. Linux 部署

### 8.1 环境配置

- **服务器（腾讯云 Ubuntu）**：conda vnpy 环境，`pip install -e .`（镜像 mirrors.tencentyun.com）
- **nginx + certbot**（一次性）：`sudo apt install nginx certbot python3-certbot-nginx`
- **前端 build 在本机做**，服务器全程不装 node

### 8.2 部署步骤

```bash
# 1. 交易 + 后端，clone 两份（实盘 + 模拟各自目录）
git clone ... → /home/ubuntu/vnpy-domestic-real
git clone ... → /home/ubuntu/vnpy-domestic-sim
pip install -e .             # 装项目 + sqlalchemy 依赖

# 2. 前端 dist（本机 build 完传上去）
scp -r web-frontend/dist ubuntu@服务器:/home/ubuntu/vnpy-domestic-real/web-frontend/
```

### 8.3 systemd 进程安排

三个 unit（两个交易 + 一个后端），飞书机器人仍在实盘父进程里跑 3000，不收编也不影响。

**后端 unit（可选）**：

```ini
[Unit]
Description=vnpy-domestic web monitor
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/vnpy-domestic-real
Environment=MONITOR_DB_PATH=/home/ubuntu/vnpy-data/monitor.db
ExecStart=/home/ubuntu/miniconda3/envs/vnpy/bin/python -m uvicorn \
    web_backend.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

**两个交易 unit（vnpy-trade-real / vnpy-trade-sim）**，靠 `WorkingDirectory` 和 `Environment` 区分：

```ini
# vnpy-trade-real.service
[Service]
WorkingDirectory=/home/ubuntu/vnpy-domestic-real
Environment=CTP_MODE=real
Environment=CTP_USER=<实盘账号>
Environment=CTP_PASSWORD=<实盘密码>
Environment=CTP_BROKER=<brokerID>
Environment=CTP_TD_SERVER=<td地址>
Environment=CTP_MD_SERVER=<md地址>
Environment=CTP_APPID=<产品名>
Environment=CTP_AUTHCODE=<授权编码>
Environment=MONITOR_DB_PATH=/home/ubuntu/vnpy-data/monitor.db
ExecStart=... python run_cta.py

# vnpy-trade-sim.service
[Service]
WorkingDirectory=/home/ubuntu/vnpy-domestic-sim
Environment=CTP_MODE=simnow
Environment=MONITOR_DB_PATH=/home/ubuntu/vnpy-data/monitor.db
ExecStart=... python run_cta.py
```

> uvicorn 只监听 127.0.0.1，公网入口只留 nginx。开启/关闭监控后端：
> `sudo systemctl enable --now vnpy-web-backend` / `sudo systemctl disable --now vnpy-web-backend`（交易不受影响）。

### 8.4 nginx（唯一公网入口，443）

| 浏览器访问 | nginx 处理 |
|:--|:--|
| `/`（其他） | 从磁盘读 dist 静态文件返回 |
| `/api/*` | 反代 127.0.0.1:8000 |
| `/ws` | 反代 127.0.0.1:8000（websocket） |
| `/webhook/feishu` | 反代 127.0.0.1:3000（可选，收编飞书） |

```nginx
server {
    listen 443 ssl;
    # ... ssl 证书 ...

    # 前端静态 + history 路由 fallback
    location / {
        root /home/ubuntu/vnpy-domestic-real/web-frontend/dist;
        try_files $uri $uri/ /index.html;   # history 路由刷新不 404
    }

    # REST API 反代
    location /api/ {
        proxy_pass http://127.0.0.1:8000;
    }

    # WebSocket 反代（必须带升级头）
    location /ws {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 3600s;
    }
}
```

两个必踩坑：`try_files ... /index.html`（history 路由刷新 404）；`/ws` 缺 `Upgrade`/`Connection` 升级头会握手失败、前端一直重连。

### 8.5 公网访问（域名 + DNS + 备案）

1. 买域名（腾讯云，.top/.xyz 几十块）+ 实名认证
2. ICP 备案（国内服务器必须，7-20 个工作日，没备案 80/443 被封；纯 IP 访问不受影响）
3. DNS 解析（DNSPod 加 A 记录）
4. HTTPS 证书：`sudo certbot --nginx -d monitor.域名.com`

备案前过渡：自己看面板用 `http://公网IP:端口`；飞书回调继续用 localtunnel 穿透。不想备案的绕过：Tailscale / ZeroTier 虚拟内网，或 SSH 隧道 `ssh -L 8000:127.0.0.1:8000 ubuntu@服务器`。

---

## 9. 坑清单（提前规避）

1. **合约覆盖坑**：单 main_engine 双 CTP 会合约冲突，必须走「两个进程」。
2. **import 不冲突，数据文件冲突**：两个进程 import vnpy 互不影响（内存隔离），但 .vntrader 数据文件要隔离，靠两个 WorkingDirectory。
3. **SQLite 并发**：两个交易进程写 + 后端读，开 WAL + busy_timeout。
4. **写库别阻塞交易线程**：MonitorWriter 用 queue + 独立线程，交易线程只入队。
5. **时区**：实盘服务器 UTC，写库统一 `zoneinfo("Asia/Shanghai")` 转北京时间；`trade.datetime` 带 tzinfo 要先 `astimezone`。
6. **monitor.db 用显式共享路径**：双账户下 `get_file_path("monitor.db")` 会解析成两个文件，必须用 `MONITOR_DB_PATH` 显式指定同一个绝对路径。
7. **WS 死连接清理**：遍历 `active` 别边删边遍历（跳元素），用 `for ws in list(active)`。
8. **broadcaster 空转**：无连接时跳过查库（`if not active: continue`）。
9. **HTTPS 后 WS 用 wss**：前端按 `location.protocol` 自动切 ws/wss。
10. **监控只读**：REST/WS 全部只读不下单，公网靠 nginx basic_auth 兜底，别把 8000 裸奔。
11. **飞书控制只实盘开**：两个进程都起 3000 会抢端口。
12. **实盘信息不落盘**：实盘 CTP 全走环境变量；模拟密码可进 secrets.yaml，但绝不写源码（公开仓库）。
13. **依赖补 sqlalchemy**：pyproject 缺 `sqlalchemy>=2.0` 不补 import models 直接崩。
14. **gitignore 漏洞先堵**：`.vntrader` 若已被跟踪，先 `git rm -r --cached` 再填真实密码；配置模板用 `.example` 提交、真实文件 gitignore。
15. **vnpy AccountData 字段有限**：只有 balance/frozen/available，没保证金和盈亏字段；账户卡 margin/pnl 改由 3s 快照从持仓 + 最新价计算。
16. **字段名 snake_case 不转 camelCase**：前端直接用后端 Python 返回的字段名，省掉映射层。
17. **lightweight-charts v4/v5 API 不同**：`addSeries(CandlestickSeries)`（v5）vs `addCandlestickSeries()`（v4）。
18. **时间零转换**：后端已存北京时间字符串，前端直接显示，别再 `new Date()` 转一遍（带本地时区错乱）。
19. **订单统计按 vt_orderid 去重**：同一订单有多行状态流转（SUBMITTING → ALLTRADED），活跃挂单/今日委托要按 vt_orderid 取最新状态，不能按行计数；日期归口按交易日（夜盘 20:00 后归下一交易日）。

---

## 10. 与专业团队的差距（后续改进方向）

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
