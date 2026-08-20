# MultiLayerStrategy V3 — 双向逐层网格策略

超卖做多逐层补仓，超买做空逐层补仓。每层独立止盈止损，上层必须先止损才开下层。

## 参数

| 参数 | 默认值 | 说明 |
|:---|:---|:---|
| `layers` | 3 | 金字塔层数 |
| `size_1` | 1 | B1/S1 手数 |
| `size_2` | 2 | B2/S2 手数 |
| `size_3` | 3 | B3/S3 手数 |
| `down_2` | 0.005 (0.5%) | B1→B2 补仓间距 |
| `down_3` | 0.008 (0.8%) | B2→B3 补仓间距 |
| `stoploss` | 0.01 (1%) | 逐层止损幅度（每层独立） |
| `tp_pct` | 0.02 (2%) | 每层独立止盈幅度 |
| `rsi_period` | 14 | RSI 计算周期 |
| `rsi_entry` | 30 | 多头入场阈值 / 空头出场阈值 |
| `rsi_exit` | 70 | 空头入场阈值 / 多头出场阈值 |
| `drop_threshold` | 0.05 (5%) | 急变保护幅度 |
| `drop_lookback` | 5 | 急变回看 K 线数 |
| `entry_cmp` | `"less"` | 多头入场比较方式 (less/greater/crossover/crossunder) |
| `exit_cmp` | `"greater"` | 箱体 RSI 出场比较方式 |
| `exit2_cmp` | `"crossunder"` | 箱体 MA20 出场比较方式 |
| `trigger_cmp` | `"greater"` | 入场触发比较方式 |

比较方式说明：

| 值 | 含义 |
|:---|:---|
| `less` | `val < trig` |
| `greater` | `val > trig` |
| `crossover` | `prev ≤ trig 且 val > trig`（上穿） |
| `crossunder` | `prev ≥ trig 且 val < trig`（下穿） |
| `none` | 禁用 |

## 入场方向判定

无持仓时（`pos=0`, `_need_box=True`），每根 K 线判断：

```
RSI < rsi_entry  →  _direction=1  做多
RSI > rsi_exit   →  _direction=-1 做空
```

一旦确定方向，整个箱体内不会改变。只有出箱体后（全平 + `_need_box=True`）才重新判定。

## 每根 K 线执行顺序

出场永远优先于入场，同 bar 不会又平又开：

```
① 逐层止盈      每层独立检查 ±tp_pct → 平该层
② 箱体退出      多头: RSI > rsi_exit 或 close 下穿 MA20(SMA20) → 全平
                空头: RSI < rsi_entry 或 close 上穿 MA20 → 全平
③ 逐层止损      每层独立检查 ±stoploss → 平该层
                全部层平完后 → _need_box=True, _direction=0
④ 急变保护      多头: 5K线跌幅 > drop_threshold → 全平
                空头: 5K线涨幅 > drop_threshold → 全平
⑤ 补仓         _need_box=True  → 等 RSI 入场开 S1
                _need_box=False → 上层已止损且价格达标 → 开 Sn
```

`_need_box` 是箱体状态锁：
- `True`：等待新箱体，只检查入场条件
- `False`：箱体进行中，检查补仓条件
- ①-④ 任一触发全平 → `True`
- ① 或 ③ 触发单层止损但还有其他层 → 保持 `False`

## 多头完整流程

### B1 入场

```
K线1: RSI=18 (<20), close=3000
  pos=0, _need_box=True
  → 方向=做多, _need_box=False
  → 开 B1  1手 @3000
```

### B1 持仓 — 等待补仓或出场

```
K线2: RSI=25, close=3040
  ⑤ 补仓: B1 还在持仓 → B2 条件不检查（直接跳过）
  持仓: B1 @3000

K线3: RSI=22, close=2965
  ③ 止损: 2965 < 3000×0.99=2970 → 平 B1
     _opened=[F,F,F,F], 全部关闭 → _need_box=True
  ⑤ _need_box=True → 等下次 RSI<20 开新 B1
  
K线4: RSI=19, close=3005  (下一轮)
  _need_box=True, pos=0
  → 开新 B1 1手 @3005
```

### B1 止损 → B2 开仓

```
K线5: close=2970
  B1 @3000, 止损价 2970
  ③ 止损: 2970 = 2970 → 平 B1
  ⑤ _need_box=False (B1刚平，还有空间)
     B2 检查: _opened[1]=False ✓
     ref=3000, close=2970
     (3000-2970)/3000=1.0% > down_2=0.5% ✓
     → 开 B2 2手 @2970
```

### B2 持仓 → B2 止损 → B3 开仓

```
K线6: close=2985
  ② 箱体: RSI=45<70 ✓ 穿越: 不触发
  ⑤ B3 检查: _opened[2]=True → 跳过
  持仓: B2 @2970

K线7: close=2940
  ① 止盈: 2940 < 2970×1.02=3029.4 → 不触发
  ③ 止损: 2940 < 2970×0.99=2940.3 → 平 B2
  ⑤ B3 检查: _opened[2]=False ✓
     ref=B2开仓价=2970, close=2940
     (2970-2940)/2970=1.01% > down_3=1% ✓
     → 开 B3 3手 @2940
```

### B3 止盈出场

```
K线8: close=3005
  ① 止盈: 3005 > 2940×1.02=2998.8 → 平 B3
  全部关闭 → _need_box=True
```

## 空头完整流程

完全镜像多头，所有价格条件反向：

### S1 入场

```
K线1: RSI=75 (>70), close=3100
  → 方向=做空
  → 开 S1 1手 @3100
```

### S1 止损 → S2 开仓

```
K线2: close=3140
  ③ 止损: 3140 > 3100×1.01=3131 → 平 S1
  ⑤ S2 检查: _opened[1]=False ✓
     ref=3100, close=3140
     (3140-3100)/3100=1.29% > down_2=0.5% ✓
     → 开 S2 2手 @3140
```

### S2 止损 → S3 开仓

```
K线3: close=3180
  ③ 止损: 3180 > 3140×1.01=3171.4 → 平 S2
  ⑤ S3 检查: _opened[2]=False ✓
     (3180-3140)/3140=1.27% > down_3=0.8% ✓
     → 开 S3 3手 @3180
```

### S3 止盈出场

```
K线4: close=3110
  ① 止盈: 3110 < 3180×0.98=3116.4 → 平 S3
  全部关闭 → _need_box=True
```

### 空头箱体退出

```
持仓中某 K 线:
  ② 箱体: RSI < rsi_entry(=30) → 全平
     或 close 上穿 MA20 → 全平
```

## 逐层止损 vs 均价止损

V3 使用逐层止损，不再是所有层均价止损：

```
多头，V3 逐层:
  B1 @3000, stoploss=1% → 止损价 2970
  B2 @2980, stoploss=1% → 止损价 2950.2
  各算各的，price < 2970 只平 B1，B2 不受影响

多头，旧版均价:
  B1 @3000 + B2 @2980 → 均价 2990
  stoploss=2% → 均价止损 2930.2
  price < 2930.2 → 全部平
```

逐层止损让每层独立决策：B1 止损了 B2 还可以等，B2 止损了 B3 还可以等，不会因为一层触发就把所有层砍掉。

## 每层不可同时持仓

补仓硬约束（`_try_open_sn` L180）：

```python
if self._opened[n - 1]: return False  # 上层还在持仓 → 不开
```

这意味着：

- B1 和 B2 不会同时持有
- B2 和 B3 不会同时持有
- 每层必须等上层止损后才能开

这是 V2→V3 的核心改动之一。

## 当前运行参数（实盘）

```json
{
  "layers": 3,
  "size_1": 1, "size_2": 2, "size_3": 3,
  "down_2": 0.00, "down_3": 0.01,
  "stoploss": 0.01,
  "tp_pct": 0.02,
  "rsi_period": 14,
  "rsi_entry": 20, "rsi_exit": 70,
  "drop_threshold": 0.03, "drop_lookback": 5,
  "entry_cmp": "less",
  "exit_cmp": "greater",
  "exit2_cmp": "crossunder",
  "trigger_cmp": "greater"
}
```

特点：
- B2 在 B1 价就开（down_2=0），等于是三倍加仓
- B3 在 B2 价 -1% 开
- 止损 1%，止盈 2%，盈亏比 2:1
- RSI<20 做多，RSI>70 做空
- 箱体退出：RSI 回到对方区间 + MA20 穿越
