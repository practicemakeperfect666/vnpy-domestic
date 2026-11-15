# monitor.db 数据表 · 接口 · 清理策略清单

monitor.db 是实盘 + 模拟盘双账户 Web 监控的共享数据库：交易进程（vnpy-domestic 的 RolloverCtaEngine）写入，web-backend（web_backend/models.py）只读，web-frontend 展示。除 `contracts` 外，所有表都带 `account` 字段区分 `real`（实盘）/ `simnow`（模拟盘），靠 `MONITOR_DB_PATH` 环境变量三进程共用同一个库文件。

> 最近一次改动（2026-11-09）：
> - **保证金率走 openctp**：`contracts` 新增 `margin_rate`（小数，0.06 = 6%），启动时从 `dict.openctp.cn/instruments` 拉取；账户卡保证金按合约率估算，缺失按品种中位数、再兜底 8%。
> - **账户资金卡改单写**：`_on_account_event` 只缓存 balance/available/frozen 不再写库，Account 统一由 `_snapshot_realtime`（3s）写；margin=Σ|volume|×price×size×保证金率（openctp 合约字典，缺失按品种中位数、再兜底 8%）、pnl=Σ pos.pnl（CTP 真实持仓盈亏）。消除资金事件 margin=0/pnl=0 覆盖快照导致的"保证金闪动"。
> - **账户持仓改读最新批次**：前端/ws 读「每账户最新 snapshot_time 批次」= 当前 CTP 持仓，平仓后旧记录不残留。
> - **系统指标 3s + WS**：system_metrics 从 7min 改成 3s 快照（cpu 非阻塞采样），并加入 WS 推送。
> - **已实现盈亏口径统一为元**：平仓盈亏乘合约乘数 size（点·手 → 元），trade_rounds.pnl / strategy_intraday 全落库为元。
> - **已实现盈亏改从 trade_rounds 当天 SUM 算**：账户已实现盈亏不再依赖 `_today_realized_pl`（内存变量，重启清零），后端 `/account/latest`、`/account-intraday-pnl`、WS 三处都改读 `trade_rounds` 当天 `SUM(pnl)`（永久保留，重启/换日不丢）。trade_rounds 旧点·手记录已一次性乘 size 修正为元。
> - **收盘挂单兜底**：`_mark_stale_orders` 只在日盘收盘后（15:00 后、夜盘开盘前）把未成交挂单（SUBMITTING 且 traded=0）标记为 CANCELLED；夜盘是次日交易日开始，夜盘挂单保留到次日日盘 15:00 收盘才失效，夜盘收盘退出不标记。
> - 早前：`account_daily_pnl` 表改名 `account_intraday_pnl`（类 `AccountIntradayPnl`、接口 `/api/account-intraday-pnl`、前端 `getAccountIntradayPnl`），因为它存的是 3s 快照不是日频。迁移命令：
> ```sql
> ALTER TABLE account_daily_pnl RENAME TO account_intraday_pnl;
> ```

## 一、十四张表全景（写入 → 表 → 接口 → 前端 → 清理）

| 表 | 用途 | 写入方（时机） | 后端接口 | 前端函数（组件） | 清理策略 |
|---|---|---|---|---|---|
| orders | 订单委托 | `on_order`（下单/撤单/拒单）+ `_mark_stale_orders`（收盘退出前标记挂单失效） | /orders /orders/stats /orders/daily_freq | getOrders / getOrderStats / getOrderDailyFreq（Orders、StrategyDetail、KlineChart） | 永久保留 |
| trades | 成交明细 | `on_trade`（成交回报） | /trades | getTrades（StrategyDetail） | 永久保留 |
| positions | 策略理论持仓快照 | `_write_position_snapshot`（每次成交后） | /positions | getPositions（StrategyDetail） | 永久保留 |
| accounts | 账户资金/保证金（margin 按 openctp 保证金率估算） | `_snapshot_realtime`（3s 快照，经 `_write_account_positions`） | /account/latest /stats/summary /equity /account/margin_series /ws | getAccountLatest / getStatsSummary / getEquity / getMarginSeries / ws（Dashboard 等） | 最近 10 天 + 每 account 最新 |
| daily_pnl | 策略每日盈亏（日频） | `reset_daily_pl`（收盘归零） | /daily_pnl /equity | getDailyPnl（StrategyDetail） | 永久保留 |
| strategy_status | 策略状态 | `_snapshot_realtime`（3s 快照） | /strategies /ws | getStrategyList / ws（Strategies、StrategyDetail） | 最近 10 天 + 每策略最新 |
| logs | 运行日志 | `write_log`（各处） | /logs | getLogs（Logs） | 永久保留 |
| system_metrics | 系统指标 | `_snapshot_realtime`（3s 快照，cpu 非阻塞采样） | /system/latest /ws | getSystemLatest / ws（Dashboard） | 最近 10 天 + 全局最新 |
| klines | 1min K 线 | `_write_kline`（BarGenerator 合成） | /klines | getKlines（KlineChart） | 永久保留 |
| trade_rounds | 平仓回合（pnl 已乘 size，单位元；账户已实现盈亏数据源） | 平仓 FIFO 配对 | /strategies /stats/summary /account/latest /account-intraday-pnl /ws | getStrategyList / getStatsSummary / getAccountLatest / getAccountIntradayPnl / ws | 永久保留 |
| contracts | 合约规格 + 保证金率（唯一不带 account） | `_write_contract`（upsert，margin_rate 来自 openctp） | /contract | getContract（StrategyDetail） | 永久保留 |
| account_positions | 账户实际持仓（CTP；前端读最新批次） | `_write_account_positions`（3s 快照） | /account-positions /ws | getAccountPositions / ws（Dashboard） | 最近 10 天 + 每 key 最新 |
| account_intraday_pnl | 账户日内盈亏快照（realized_pl 字段现为引擎内存快照，后端已改读 trade_rounds SUM；floating_pl/balance 仍用） | `_write_account_positions`（3s 快照） | /account-intraday-pnl | getAccountIntradayPnl（Dashboard） | 最近 10 天 + 每 account 最新 |
| strategy_intraday | 策略日内净值（平仓+浮动，单位元） | `_snapshot_realtime`（15min） | /equity | getEquity（StrategyDetail、Dashboard） | 永久保留 |

写入方均指交易进程（RolloverCtaEngine）。`seed_demo.py` 另造一套 demo 数据（real + simnow，14 张表全覆盖）用于本地测试。

## 二、后端接口清单（18 REST + 1 WebSocket）

| 接口 | 读表 | 前端函数 |
|---|---|---|
| GET /account/latest | Account + TradeRound（realized_pl 从 trade_rounds 当天 SUM） | getAccountLatest |
| GET /positions | Position | getPositions |
| GET /account-positions | AccountPosition（每账户最新批次） | getAccountPositions |
| GET /account-intraday-pnl | AccountIntradayPnl + TradeRound（realized_pl 覆盖为 trade_rounds 当天 SUM） | getAccountIntradayPnl |
| GET /strategies | StrategyStatus + TradeRound | getStrategyList |
| GET /orders | Order | getOrders |
| GET /orders/stats | Order | getOrderStats |
| GET /orders/daily_freq | Order | getOrderDailyFreq |
| GET /trades | Trade | getTrades |
| GET /daily_pnl | DailyPnl | getDailyPnl |
| GET /logs | Log | getLogs |
| GET /system/latest | SystemMetric | getSystemLatest |
| GET /time | 无（服务器时间） | getServerTime |
| GET /klines | Kline | getKlines |
| GET /stats/summary | TradeRound + Account | getStatsSummary |
| GET /equity | StrategyIntraday + DailyPnl + Account | getEquity |
| GET /account/margin_series | Account | getMarginSeries |
| GET /contract | Contract | getContract |
| WS /ws | Account + AccountPosition + StrategyStatus + TradeRound + SystemMetric | connectWS |

WS 链路：ws.py 推 `accounts / positions / strategies / system` 四个字段 → App.vue 存进 `store.snapshot` → Dashboard / Strategies / StrategyDetail 三个组件 `watch(store.snapshot)` 消费。其中 accounts 补 `realized_pl`（读 trade_rounds 当天 SUM）、positions 取每账户最新 snapshot_time 批次（当前持仓）、system 取 SystemMetric 最新一条。

## 三、清理策略

三类：

1. **永久保留（9 张）**：orders、trades、trade_rounds、positions、daily_pnl、strategy_intraday、klines、contracts、logs —— 事件流水 / 日频 / 基础数据，量小且有长期对账价值，不清理。
2. **最近 10 天 + 每 key 最新（5 张快照）**：accounts、strategy_status、account_positions、account_intraday_pnl、system_metrics —— 3s 高频快照，量大（一年约 1GB），只保留最近 10 天历史 + 每 key 最新一条（实时面板不丢当前状态）。

清理脚本 `vnpy-domestic/scripts/cleanup_monitor.py`：

```bash
python cleanup_monitor.py [db_path] [keep_days] [--vacuum]
# 默认 keep_days=10，删 10 天前；--vacuum 回收空间（需交易进程停止）
```

## 四、三处数据冗余（一份数据分两处记录）

### 1. 成交数量：orders.traded ↔ trades.volume（低风险）

- `orders.traded` 是订单累计成交量，`trades.volume` 是逐笔成交明细，靠 `vt_orderid` 关联。标准明细/汇总设计，不会错。

### 2. 持仓：positions ↔ strategy_status.pos（中风险，短暂漂移）

- 数据源都是 `strategy.pos`，但写入时机不同：`positions` 成交后立即写，`strategy_status` 每 3s 快照写。漂移 ≤3s，最终一致。

### 3. 平仓盈亏：trade_rounds.pnl ↔ daily_pnl.realized_pl（高风险，可能记错）

- **数据源不同**：`trade_rounds.pnl` 是平仓 FIFO 配对的 pair_pl × size（引擎层，单位元）；`daily_pnl.realized_pl` 取 `strategy_cumulative_pl`（引擎层累计，已乘 size，单位元）。两者口径一致。待验证：某天某策略 `SUM(trade_rounds.pnl)` 是否等于当天 `daily_pnl.realized_pl`。

## 五、accounts 表写入说明（单写）

- `_on_account_event()`（CTP 资金事件）只把 balance / available / frozen 缓存到内存（`_last_balance` 等），**不写库**。
- `_snapshot_realtime()`（3s 快照，经 `_write_account_positions`）统一写完整一条 Account：balance / available / frozen 来自缓存，margin=Σ|volume|×price×size×保证金率（`_margin_rate_of`：优先 openctp 合约字典的精确合约，缺失按品种中位数，再兜底 8%）、pnl=Σ pos.pnl（CTP 真实持仓盈亏）。

前端取 id 倒序最新一条，3s 快照是唯一写入方，margin/pnl 不会出现资金事件覆盖成 0 的闪动。

## 六、已实现盈亏口径（点·手 → 元 → trade_rounds SUM）

- 引擎平仓盈亏计算在 `vnpy_domestic/trader/position_lots.py` 的 `settle_close`，返回"点·手"（价差 × 手数 × 方向，不含合约乘数）。
- `RolloverCtaEngine.process_trade_event` 平仓累加时乘 size（`pl_amount = pl * size`）转成"元"，写入 `strategy_cumulative_pl`、`_today_realized_pl`、`trade_rounds.pnl`。
- 由此 `strategy_intraday.realized_pl/equity`、`daily_pnl.realized_pl` 全部落库为元，与浮动盈亏（`(last-avg)×pos×size`）和账户权益（CTP balance）单位一致。
- **账户已实现盈亏的数据源 = `account_intraday_pnl.realized_pl`**（引擎 `_today_realized_pl`，逐日盯市口径，与飞书日报一致）。重启时引擎在日切处用 `_restore_today_realized()` 从本表续算，不再"重启归零"。`trade_rounds` 只用于绩效统计（开仓价口径），不再参与账户层展示。

## 七、订单挂单失效兜底

- 期货交易日 = 夜盘（前一晚 21:00 开盘）+ 日盘（当日 15:00 收盘）。挂单在**交易日日盘 15:00 收盘**时才失效；夜盘 23:00 收盘后挂单**保留**到次日日盘 15:00。
- `_mark_stale_orders`（`RolloverCtaEngine`）只在日盘收盘后执行（run_cta.py 退出流程里 `DAY_END(15:01) <= now < NIGHT_START(20:45)` 判断成立时）：`UPDATE orders SET status='CANCELLED' WHERE status='SUBMITTING' AND traded=0`。夜盘收盘退出（23:05）不执行，避免误标记保留到次日的夜盘挂单。
