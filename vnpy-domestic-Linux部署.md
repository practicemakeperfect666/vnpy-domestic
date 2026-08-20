# vnpy-domestic Linux 部署指南

## 环境假设

- 服务器 IP：192.168.1.100（示例）
- 用户名：youruser
- 项目路径：/home/youruser/vnpy-domestic
- conda 路径：/home/youruser/miniconda3
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
git clone https://github.com/你的用户名/vnpy-domestic.git
cd vnpy-domestic
```

## 4. 安装依赖

```bash
# vnpy 核心
pip install vnpy vnpy_ctastrategy vnpy_ctp vnpy_sqlite

# 本项目依赖
pip install pyyaml requests akshare psutil pandas

# 安装本包（editable 模式，改代码不用重装）
pip install -e .
```

## 5. 配置文件

### 5.1 CTP 账号（SimNow）

不存文件。凭证通过 `systemctl set-environment` 注入 systemd 内存（重启后清空，需重新输入），不落盘。

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
User=youruser
WorkingDirectory=/home/youruser/vnpy-domestic
ExecStart=/home/youruser/miniconda3/envs/vnpy/bin/python /home/youruser/vnpy-domestic/run_cta.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
```

## 7. 启动

```bash
sudo systemctl daemon-reload
sudo systemctl enable vnpy-cta                 # 开机自启（凭证未注入时 ConditionEnvironment 会跳过启动，不会报错）

# ⬇ 每次开机后手动执行这两步：
sudo systemctl set-environment CTP_USER=你的SimNow账号
sudo systemctl set-environment CTP_PASSWORD=你的SimNow密码
sudo systemctl start vnpy-cta

# 之后 restart 不需要重输（systemd 内存里还有）
sudo systemctl restart vnpy-cta
```

## 8. 日常操作

```bash
# 看状态
sudo systemctl status vnpy-cta

# 看实时日志（父进程+子进程都在里面）
journalctl -u vnpy-cta -f

# 看最近日志
journalctl -u vnpy-cta -n 200 --no-pager

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
| 权限问题 | chown youruser:youruser ~/vnpy-domestic -R |

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
2. 防火墙放行：`ufw allow 3000/tcp`
3. 飞书后台「事件与回调」→ 订阅 `im.message.receive_v1` → 回调地址填 `http://公网IP:3000/webhook/feishu`

### 说明

- 控制线程随 run_cta 子进程运行，只在交易时段在线（非交易时段子进程退出）
- 不配置 `feishu_app_id` 则控制功能静默关闭，通知不受影响
- `verification_token` 必填（验签），`encrypt_key` 后台没开加密可留空
