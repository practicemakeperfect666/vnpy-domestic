# CornScalperStrategy 玉米刷盘口策略

一句话：MA 判方向，盘口一买一卖赚价差；被套开反向锁仓，等盘口回成本价解套。

## 核心逻辑

**MA 模式（正常刷单）：**

- 价 > MA(N) → 买一开多，成交后挂卖一平（赚 1 跳价差）
- 价 < MA(N) → 卖一开空，成交后挂买一平

**被套锁仓：**

- 平仓单价格往不利方向偏离盘口 → 撤单
- 开反向锁仓单（量 = 被困仓位），锁住敞口
- 锁仓单平掉后继续刷反向价差
- 直到盘口回到成本价，才挂平仓单解套（随缘）

**收盘强平：**

- 14:57 / 22:57 撤单 + 多空全平，不留隔夜

**盘口过滤：**

- 入场 / 锁仓单：盘口变薄（< `min_depth`）撤单，等厚重再挂
- 平仓单：价格反向 N 跳才撤（有利方向挂着等成交）

## 状态机

| mode | 含义 |
|:-----|:-----|
| `ma` | 空仓，按 MA 入场 |
| `trapped_long` | 多单被困（被套），开空锁仓 / 等盘口回本解套 |
| `trapped_short` | 空单被困，开多锁仓 / 等盘口回本解套 |

## 持仓追踪

多空分别记录 `long_pos` / `short_pos`（锁仓净持仓 = 0 不误判无仓），多空各自 FIFO 批次，平仓按交易所规则（`settle_close`）核算已实现盈亏。

## 参数

| 参数 | 默认 | 说明 |
|:-----|:-----|:-----|
| `ma_window` | 5 | MA 周期（判方向） |
| `min_depth` | 500 | 盘口最小深度（薄则撤单） |
| `fixed_size` | 1 | 单笔手数 |
| `deviation_ticks` | 2 | 平仓单偏离 N 跳才撤 |

## 配置文件示例

`.vntrader/cta_strategy_setting.json`：

```json
{
    "CornScalper_c": {
        "class_name": "CornScalperStrategy",
        "vt_symbol": "c2611.DCE",
        "setting": {
            "ma_window": 5,
            "min_depth": 500,
            "fixed_size": 1
        }
    }
}
```

> `deviation_ticks` 不写则用默认值 2。
