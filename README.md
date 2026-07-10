<div align="center">
  <br>
  <img src="https://img.shields.io/badge/python-3.11+-blue?style=flat-square&logo=python" alt="Python">
  <img src="https://img.shields.io/badge/vnpy-4.4.0-blue?style=flat-square" alt="vnpy">
  <img src="https://img.shields.io/badge/version-0.1.0-brightgreen?style=flat-square" alt="Version">
  <img src="https://img.shields.io/badge/license-MIT-lightgrey?style=flat-square" alt="License">
  <br><br>

  <h1 style="font-size:3.5rem; margin:0.5rem 0">vnpy-domestic</h1>
  <p style="font-size:1.2rem; margin:0.3rem 0">面向国内期货实盘的 vnpy 增强工具包<br>
  <span style="font-weight:300; font-size:1rem">基于 <a href="https://github.com/vnpy/vnpy">vnpy</a> 构建 · 专为国内期货实盘打磨</span></p>
  <br>
</div>

---

## 介绍

vnpy-domestic 是一个面向国内期货实盘的 vnpy 扩展工具包。它不修改 vnpy 源码，而是通过继承 `CtaEngine` 和 `BarGenerator` 的方式叠加功能，让升级 vnpy 版本时无冲突。

**解决的问题：**

| 痛点 | 方案 |
|:----|:-----|
| 主力合约到期后手动换月，错过行情 | `RolloverCtaEngine` 自动识别主力、持仓归零自动换月 |
| 集合竞价 tick 时间戳错位（20:59 → 21:00） | `MyBarGenerator` 时间戳自动校正 |
| 午休/小节间歇无效 tick 干扰 K 线 | 非交易时段自动过滤 |
| 跨交易段 volume 重复计算 | 段首全量 + 后续增量的增量模式 |
| 实盘无人值守，出问题不知道 | 钉钉/飞书推送 + 策略汇总（`monitor_interval` 可调）|
| 非交易时段空跑浪费资源 | 守护进程按交易时段自动启停 |

**五个模块：**

| 模块 | 文件 | 职责 |
|:----|:-----|:-----|
| `RolloverCtaEngine` | `RolloverCtaEngine.py` | 自动换月 + 通知推送 + P&L 追踪 + CTP 断连监控 |
| `MyBarGenerator` | `newbargenerator.py` | 增强 K 线合成 + 品种级交易时段管理 |
| `NotificationManager` | `notification_manager.py` | 钉钉/飞书统一通知 |
| `AKShare Datafeed` | `vnpy_akshare.py` | 免费 1 分钟 K 线数据源 |
| `TradingTime Updater` | `update_trading_times.py` | 交易时段拉取与 CSV 持久化 |

---

## 安装

**环境要求：** Python ≥ 3.10

### 1. 安装 vnpy 框架

```bash
pip install vnpy
pip install vnpy_ctastrategy vnpy_ctp vnpy_sqlite
```

- `vnpy` — 量化交易框架
- `vnpy_ctastrategy` — CTA 策略引擎（`CtaEngine`、`CtaTemplate`）
- `vnpy_ctp` — 上期技术 CTP 柜台接口
- `vnpy_sqlite` — SQLite 数据库适配（vnpy 默认数据存储）

### 2. 安装依赖

```bash
pip install pyyaml requests akshare psutil pandas
```

| 包 | 用途 |
|:---|:-----|
| `pyyaml` | 读取 `secrets.yaml` 通知配置 |
| `requests` | 新浪换月 API + 钉钉/飞书 HTTP 推送 |
| `akshare` | 免费期货行情数据源 |
| `psutil` | 系统硬件监控（CPU/内存/磁盘） |
| `pandas` | CSV 读写与数据整理 |

### 3. 安装 TA-Lib

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

### 4. 安装本包

```bash
cd vnpy-domestic
pip install -e .
```

`-e` 模式（editable install）确保修改源码后无需重新安装。

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

feishu_webhook: "https://open.feishu.cn/open-apis/bot/v2/hook/你的hook"
```

- `notify_type`：`dingtalk`（仅钉钉）、`feishu`（仅飞书）、`both`（同时推送）
- 钉钉使用 HMAC-SHA256 签名认证，需配置 `webhook` 和 `secret`
- 飞书使用简单的 Webhook URL

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
| `vt_symbol` | 合约代码，格式 `品种.交易所`，如 `rb2610.SHFE`、`i2609.DCE`、`sc2608.INE` |
| `setting` | 策略参数，需与策略类的 `parameters` 列表对应 |

支持的交易所后缀：`SHFE`（上期所）、`DCE`（大商所）、`CZCE`（郑商所）、`INE`（能源中心）、`CFFEX`（中金所）。

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

---

## 模块

### RolloverCtaEngine

继承 `CtaEngine`，叠加自动换月、成交通知、P&L 追踪和 CTP 断连监控。

使用新浪财经 API 按持仓量匹配主力——请求 `nf_XXX0` 连续合约和 24 个月合约，持仓量一致的即为主力。换月执行前先 `get_contract` 验证新合约存在，不存在则跳过不动状态。换月检查分三个时机：初始化时静默记录（`_check_rollover_for_init`）、初始化后汇总推送（`send_rollover_init_summary`）、平仓 pos=0 时即时检查（`_check_and_notify_rollover`）。

通知设计：REJECTED 状态才推手机，开平仓按 position flip（flat↔持仓）语义推送，含本次盈亏与累计盈亏。成交延迟 > 2s 自动暂停开仓。定时器每秒检测 CTP 断连（`last_activity_time` > 120s 告警），按 `monitor_interval` 间隔汇总策略状态。`write_log` 含"失败"或"错误"自动推手机。


### MyBarGenerator

继承 `BarGenerator`，解决集合竞价时间戳校正（20:59 → 21:00）、非交易时段过滤、跨段 volume 增量计算（段首全量 + 后续增量，gap > 2h 重置）。

交易时段从 `trading_times.csv` 加载为模块级全局缓存 `_TRADING_SESSIONS`，按品种名称前缀匹配。竞价判断只对每个交易会话的首段生效（10:30 小节恢复、13:30 午盘无竞价）。

### NotificationManager

钉钉（HMAC-SHA256）和飞书（Webhook）统一通知。配置优先级：显式传入 > `secrets.yaml` > 类默认值，YAML 按当前目录、包目录、用户家目录依次搜索。速率限制当前关闭（设 `_min_send_interval` 可启用）。提供启动/关闭通知、系统硬件信息（CPU/内存/磁盘/运行时间）、账户报告（动态权益/可用资金/持仓）、策略状态分批汇总等预置方法。

### AKShare 数据源

通过 `sys.modules` 别名注册为 vnpy 原生数据源。调用 `akshare.futures_zh_minute_sina()` 获取 1 分钟 K 线，仅支持 `Interval.MINUTE`，其他周期返回空列表。

### 交易时段更新

`run_cta.py` 启动时调用 `run_and_save()`，读取 `cta_strategy_setting.json` 提取品种，从 `dict.openctp.cn/times` 拉取交易时间，按 18–6 点分离夜盘，保存为 `trading_times.csv`。API 失败不阻塞启动。

---

## 守护进程

`run_cta.py` 采用父子双进程。父进程按交易时段启停子进程，子进程崩溃自动重启（最多 5 次）。交易时段判断：周一~周五 8:45–15:01 或 20:45–次日 2:45，周六凌晨延续夜盘，周日晚 20:45 后准备开盘。

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

## 项目结构

```
vnpy-domestic/
├── run_cta.py                          ← 实盘入口（守护进程）
├── pyproject.toml                      ← 包配置
├── .gitignore                          ← 保护 .vntrader/
│
├── .vntrader/                          ← 运行时配置（gitignore 保护）
│   ├── secrets.yaml                    ← 通知 Webhook
│   ├── cta_strategy_setting.json       ← 策略参数
│   ├── cta_strategy_data.json          ← 策略变量持久化（有成交后自动填充）
│   ├── trading_times.csv               ← 交易时段（自动更新）
│   └── bar_data/                       ← K 线数据
│
├── vnpy_domestic/
│   ├── __init__.py                     ← 导出 MyBarGenerator，注册 akshare 别名
│   ├── trader/
│   │   ├── newbargenerator.py          ← MyBarGenerator + 交易时段模块
│   │   ├── notification_manager.py     ← 钉钉/飞书通知
│   │   ├── update_trading_times.py     ← 交易时段拉取
│   │   └── vnpy_akshare.py             ← AKShare 数据源
│   └── RolloverCtaEngine/
│       └── RolloverCtaEngine.py        ← 自动换月 + 通知 + P&L + 断连监控
│
└── strategies/
    ├── save_bar.py                     ← 示例策略（K 线落盘）
    └── dual_ma.py                      ← 双均线策略
```

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

