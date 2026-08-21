# vnpy-domestic Linux 部署指南

## 环境假设

- 服务器 IP：192.168.1.100（示例）
- 用户名：ubuntu
- 项目路径：/home/ubuntu/vnpy-domestic
- conda 路径：/home/ubuntu/miniconda3
- conda 环境名：vnpy

---

## 1. 装 conda（如果没有）

```bash
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash Miniconda3-latest-Linux-x86_64.sh -b -p ~/miniconda3
~/miniconda3/bin/conda init bash
source ~/.bashrc
```

## 2. 创建项目环境

```bash
conda create -n vnpy python=3.11 -y
conda activate vnpy
```

## 3. 克隆项目

```bash
cd ~
git clone https://github.com/practicemakeperfect666/vnpy-domestic.git
cd vnpy-domestic
```

## 4. 安装依赖

```bash
# ⚠️ 先装编译工具链：vnpy_ctp 是源码包（tar.gz），需要 g++ 编译，缺了会报
#   "Unknown compiler(s): c++, g++, clang++..."
sudo apt update
sudo apt install -y build-essential ninja-build

# vnpy 核心
pip install vnpy vnpy_ctastrategy vnpy_ctp vnpy_sqlite

# 本项目依赖
pip install pyyaml requests akshare psutil pandas lark-oapi fastapi uvicorn

# 安装本包（editable 模式，改代码不用重装）
pip install -e .

# ⚠️ 生成中文 locale：vnpy_ctp 是 C++ 扩展，运行时 CTP 库要创建 zh_CN locale，
#    缺了会报 "locale::facet::_S_create_c_locale name not valid" 直接崩溃
sudo apt install -y locales
sudo locale-gen zh_CN.UTF-8 zh_CN.GBK
```

## 5. 配置文件

### 5.1 CTP 账号（SimNow）

账号从 SimNow 官网注册（https://www.simnow.com.cn），注册时选 **7×24 全天候环境**——`run_cta.py` 硬编码的服务器就是 7×24 的，不是「交易时段」环境。

凭证不写进文件、不落盘。用 `systemctl set-environment` 把账号密码注入 systemd 内存（服务器重启后清空，需重输），磁盘上不存密码：

```bash
sudo systemctl set-environment CTP_USER=你的SimNow账号
sudo systemctl set-environment CTP_PASSWORD=你的SimNow密码
```

> ⚠️ 密码含 `!`、`$`、空格、`#` 等特殊字符时，用单引号包住整个 KEY=value
> （否则 bash 把 `!` 当历史命令，报 `event not found`）：
> `sudo systemctl set-environment 'CTP_PASSWORD=a!abcd'`

**查 / 改 / 清密码：**

```bash
# 查看已注入的凭证（密码明文显示，别截图外传）
sudo systemctl show-environment | grep CTP

# 换密码：直接重设同名变量覆盖
sudo systemctl set-environment CTP_PASSWORD=新密码

# 清空（不再跑时）
sudo systemctl unset-environment CTP_USER CTP_PASSWORD
```

> 变量设到 systemd 管理器（PID 1），机器上所有 systemd 服务都能读到。自己的服务器
> + SimNow 仿真盘风险可控；真要隔离可改 `EnvironmentFile=`，当前没必要。

CTP 服务器地址已硬编码在 `run_cta.py` 第74-75行，用的 SimNow 7×24：
```
交易服务器: 182.254.243.31:30003
行情服务器: 182.254.243.31:30013
```

### 5.2 飞书通知 + 远程控制

```bash
cat > .vntrader/secrets.yaml << 'EOF'
notify_type: "feishu"
dingtalk_webhook: ""
dingtalk_secret: ""
feishu_webhook: "https://open.feishu.cn/open-apis/bot/v2/hook/你的hook"

# ── 飞书控制（可选，群里 @机器人 停止/重启策略）──
feishu_app_id: "cli_xxx"
feishu_app_secret: "xxx"
feishu_encrypt_key: ""        # 事件与回调 → Encrypt Key（没开加密留空）
feishu_verification_token: "你的VerificationToken"  # 必填，后台获取
feishu_bot_open_id: "ou_xxx"
feishu_host: "0.0.0.0"        # Linux 服务器监听 0.0.0.0
EOF
```

### 5.3 策略配置

```bash
# 已有 .vntrader/cta_strategy_setting.json，确认品种和参数正确
cat .vntrader/cta_strategy_setting.json
```

## 6. systemd 服务

把下面整段**复制粘贴到服务器终端，回车执行**。它是 heredoc 命令，会自动生成
`/etc/systemd/system/vnpy-cta.service` 这个 unit 文件（不用手动建文件、不用编辑器）。

凭证不存盘。每次开机后手动 `systemctl set-environment` 输入，存 systemd 内存。

```bash
sudo tee /etc/systemd/system/vnpy-cta.service << 'EOF'
[Unit]
Description=vnpy-domestic CTA 策略
After=network-online.target
Wants=network-online.target
ConditionEnvironment=CTP_USER

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/vnpy-domestic
ExecStart=/home/ubuntu/miniconda3/envs/vnpy/bin/python /home/ubuntu/vnpy-domestic/run_cta.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
```

```bash
# 验证文件生成成功
cat /etc/systemd/system/vnpy-cta.service
```

**unit 文件关键字段：**

| 字段 | 作用 |
|:-----|:-----|
| `ConditionEnvironment=CTP_USER` | 凭证未注入时不启动（配合开机自启，静默跳过不报错） |
| `After=network-online.target` | 等网络就绪再启动（CTP 需要网络） |
| `Restart=always` + `RestartSec=10` | 崩溃后 10 秒自动重启 |
| `User=ubuntu` / `WorkingDirectory` | 运行用户与项目目录 |
| `ExecStart` | 用 conda 环境的 python 跑 run_cta.py |
| `StandardOutput/Error=journal` | 日志进 journald，用 `journalctl -u vnpy-cta` 查看 |

## 7. 启动（完整流程）

```bash
# ① 让 systemd 重新读取刚写的 unit 文件（每次改 unit 后都要做）
sudo systemctl daemon-reload

# ② 开机自启（凭证没注入时 ConditionEnvironment 让它静默跳过，不报错）
sudo systemctl enable vnpy-cta

# ③ 注入账号密码（见 5.1 节）+ 中文 locale（vnpy_ctp C++ 层需要）
sudo systemctl set-environment CTP_USER=你的SimNow账号
sudo systemctl set-environment CTP_PASSWORD=你的SimNow密码
sudo systemctl set-environment LC_ALL=zh_CN.UTF-8
sudo systemctl set-environment LANG=zh_CN.UTF-8

# ④ 启动
sudo systemctl start vnpy-cta

# ⑤ 验证：凭证读到了 + 服务在跑
sudo systemctl show vnpy-cta -p Environment     # 应显示 CTP_USER=xxx CTP_PASSWORD=yyy
sudo systemctl status vnpy-cta                  # 应显示 active (running)
journalctl -u vnpy-cta -n 50 --no-pager         # 看有没有报错
```

**之后的操作：**

```bash
# 重启服务（改代码后）——凭证还在 systemd 内存里，不用重输
sudo systemctl restart vnpy-cta

# 换密码后重启
sudo systemctl set-environment CTP_PASSWORD=新密码
sudo systemctl restart vnpy-cta
```

**服务器重启后：** systemd 内存清空、凭证没了，服务被 `ConditionEnvironment` 静默跳过（status 显示 inactive，不报错）。重新执行 ③④ 两步即可：

```bash
sudo systemctl set-environment CTP_USER=你的账号
sudo systemctl set-environment CTP_PASSWORD=你的密码
sudo systemctl start vnpy-cta
```

## 8. 日常操作

```bash
# 看状态
sudo systemctl status vnpy-cta

# 看实时日志（父进程+子进程都在里面）
journalctl -u vnpy-cta -f

# 看最近日志
journalctl -u vnpy-cta -n 200 --no-pager

# 看当天全部日志
journalctl -u vnpy-cta --since today

# 重启（改完策略代码后）
sudo systemctl restart vnpy-cta

# 停止
sudo systemctl stop vnpy-cta
```

## 9. 更新代码后

```bash
cd ~/vnpy-domestic
git pull
sudo systemctl restart vnpy-cta
```

## 10. 验证

```bash
# 确认跑起来了
ps aux | grep run_cta

# 飞书应该收到启动通知：
# 🚀 CTA策略系统启动
# 📋 启动换月检查
# 📊 账户报告

# 确认 CTP 连接正常
journalctl -u vnpy-cta -n 50 | grep -E "(连接|订阅|换月|init)"
```

## 排错

| 现象 | 检查 |
|------|------|
| 启动后马上退出 | `journalctl -u vnpy-cta -n 50` 看报错 |
| CTP 连不上 | ping 182.254.243.31，检查防火墙 |
| 环境变量没读到 | `sudo systemctl show vnpy-cta -p Environment` |
| conda python 找不到 | `ls ~/miniconda3/envs/vnpy/bin/python` |
| 权限问题 | chown ubuntu:ubuntu ~/vnpy-domestic -R |
| 改 unit 文件后 start 报错/不生效 | 先 `sudo systemctl daemon-reload` |
| status 显示 inactive 且无报错 | 凭证没注入（ConditionEnvironment 静默跳过），重跑 set-environment 再 start |

---

## 11. 飞书远程控制（可选）

通过飞书群 @机器人 发送指令，远程控制策略启停。**重启/停止都是策略级操作（进程内 stop/restart），不重启进程。**

### 架构（HTTP 回调模式）

```
飞书服务器（事件订阅 → 将事件发送至开发者服务器）
   ↓ HTTP POST /webhook/feishu
公网 IP:3000（ufw 放行）→ run_cta.py 内嵌 FastAPI(uvicorn 线程)
   ↓ lark-oapi 验签解密 → ctrl_queue
run_cta 主循环（1s 轮询）→ stop_all_strategies / init+start
```

### 指令

| 指令 | 动作 |
|------|------|
| `@机器人 停止` | `stop_all_strategies()`（进程不退出，可随时重启）|
| `@机器人 重启` | 停止 → `init_all_strategies()` → `start_all_strategies()` |

### 配置

1. secrets.yaml 填 6 个飞书控制 key（见 5.2 节），`feishu_host` 改 `0.0.0.0`
2. 防火墙放行（两层都要，腾讯云安全组最容易漏）：
   - 服务器本机：`sudo ufw allow 3000/tcp`
   - 腾讯云控制台 → 该实例 → 安全组 → 入站规则 → 放行 TCP 3000 端口
3. 飞书后台「事件与回调」→ 订阅 `im.message.receive_v1` → 回调地址填 **`http://`**（不是 https）`http://公网IP:3000/webhook/feishu`——uvicorn 起的是裸 HTTP 没配 TLS，填 https 会 TLS 握手失败、3 秒超时

### 说明

- 控制线程随 run_cta 子进程运行，只在交易时段在线（非交易时段子进程退出）
- 不配置 `feishu_app_id` 则控制功能静默关闭，通知不受影响
- `verification_token` 必填（验签），`encrypt_key` 后台没开加密可留空
