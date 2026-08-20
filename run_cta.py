import os
import multiprocessing
import sys
import queue
import threading
from time import sleep
import time as time_mod
from datetime import datetime, time as dtime

from vnpy.event import EventEngine
from vnpy.trader.setting import SETTINGS
from vnpy.trader.engine import MainEngine
from vnpy.trader.logger import INFO, logger
from vnpy.trader.utility import get_file_path

from vnpy_ctp import CtpGateway
from vnpy_ctastrategy.base import EVENT_CTA_LOG

from vnpy_domestic.RolloverCtaEngine.RolloverCtaEngine import RolloverCtaEngine
from vnpy_domestic.trader.notification_manager import NotificationManager
from vnpy_domestic.trader.update_trading_times import run_and_save
from vnpy_domestic.trader.feishu_http_control import load_feishu_control, build_control_app, start_control, reply_text


SETTINGS["log.active"] = True
SETTINGS["log.level"] = INFO
SETTINGS["log.console"] = True


# ── Chinese futures market trading period (day/night) ──
DAY_START = dtime(8, 45)
DAY_END = dtime(15, 1)

NIGHT_START = dtime(20, 45)
NIGHT_END = dtime(2, 45)




def check_trading_period() -> bool:
    """检查当前是否在交易时段内（含周末过滤）"""
    now = datetime.now()
    weekday = now.weekday()
    current_time = now.time()

    # 周六：只保留周五夜盘延续（凌晨 00:00~02:45）
    if weekday == 5:
        return current_time <= NIGHT_END

    # 周日：只保留周日夜盘（21:00 起，为周一早盘做准备）
    if weekday == 6:
        return current_time >= NIGHT_START

    # 周一 ~ 周五
    trading = False
    if (
        (current_time >= DAY_START and current_time <= DAY_END)
        or (current_time >= NIGHT_START)
        or (current_time <= NIGHT_END)
    ):
        trading = True

    return trading


def load_ctp_setting() -> dict:
    """加载CTP配置：用户名密码走环境变量，其余硬编码"""
    username = os.getenv("CTP_USER")
    password = os.getenv("CTP_PASSWORD")
    if not username or not password:
        return {}

    return {
        "用户名": username,
        "密码": password,
        "经纪商代码": "9999",
        "交易服务器": "182.254.243.31:30003",
        "行情服务器": "182.254.243.31:30013",
        "产品名称": "simnow_client_test",
        "授权编码": "0000000000000000",
        "产品信息": ""
    }


def run_child(child_conn=None) -> None:
    """子进程：运行 vnpy 全栈"""
    SETTINGS["log.file"] = True

    ctp_setting = load_ctp_setting()
    if not ctp_setting:
        logger.error("CTP 配置加载失败，子进程退出")
        sys.exit(1)

    notify = NotificationManager()
    notify.set_log_callback(lambda msg: logger.info(f"[Notify] {msg}"))

    event_engine: EventEngine = EventEngine()
    main_engine: MainEngine = MainEngine(event_engine)
    main_engine.add_gateway(CtpGateway)
    cta_engine = RolloverCtaEngine(main_engine, event_engine)
    main_engine.engines[cta_engine.engine_name] = cta_engine
    logger.info(f"主引擎创建成功（{type(cta_engine).__name__}）")

    log_engine = main_engine.get_engine("log")
    event_engine.register(EVENT_CTA_LOG, log_engine.process_log_event)
    logger.info("注册日志事件监听")

    main_engine.connect(ctp_setting, "CTP")
    logger.info("连接CTP接口")

    logger.info("等待CTP连接和数据加载...")
    sleep(40)

    # 修复 vnpy 空 JSON 文件导致崩溃的 bug
    p = get_file_path("cta_strategy_data.json")
    if p.exists() and p.stat().st_size == 0:
        p.write_text("{}")
        logger.info("已修复空 JSON 文件: cta_strategy_data.json")

    cta_engine.init_engine()
    logger.info("CTA引擎初始化完成（策略已自动加载）")

    cta_engine.init_all_strategies()
    sleep(60)
    logger.info("CTA策略全部初始化")

    cta_engine.send_rollover_init_summary()
    cta_engine.start_all_strategies()
    logger.info("CTA策略全部启动")

    sleep(60)
    notify.send_startup_notification()

    try:
        # 启动后发一次账户报告（含系统状态）
        sleep(5)
        notify.send_account_report(main_engine, order_stats=cta_engine.get_daily_order_stats())

        last_report_time = time_mod.time()
        REPORT_INTERVAL = 420  # 7分钟

        while True:
            sleep(1)

            # 检查父进程停止指令（正常退出：撤挂单 + 写持仓）
            if child_conn is not None and child_conn.poll():
                msg = child_conn.recv()
                if msg == "stop":
                    logger.info("收到父进程停止指令，正常退出")
                    break

            trading = check_trading_period()
            if not trading:
                logger.info("非交易时段，关闭子进程")
                break

            if time_mod.time() - last_report_time >= REPORT_INTERVAL:
                notify.send_account_report(main_engine, order_stats=cta_engine.get_daily_order_stats())
                last_report_time = time_mod.time()

    except KeyboardInterrupt:
        logger.info("收到中断信号")

    logger.info("停止所有策略...")
    cta_engine.stop_all_strategies()
    notify.send_shutdown_notification()

    logger.info("关闭主引擎...")
    main_engine.close()
    logger.info("子进程正常退出")
    sys.exit(0)


def run_parent() -> None:
    """父进程：按交易时段启停子进程，崩溃自动重启"""
    print("=" * 60)
    print("启动CTA策略守护父进程")
    print("策略配置将由CTA引擎自动加载")
    print("=" * 60, flush=True)

    # 启动时确认 CTP 凭证存在（环境变量）
    ctp_user = os.getenv("CTP_USER", "")
    ctp_pass = os.getenv("CTP_PASSWORD", "")
    if not ctp_user or not ctp_pass:
        print("❌ 请设置环境变量 CTP_USER 和 CTP_PASSWORD")
        print("   export CTP_USER=your_account")
        print("   export CTP_PASSWORD=your_password", flush=True)
        sys.exit(1)
    print(f"CTP 凭证已就绪 (用户名: {ctp_user})", flush=True)

    # 更新交易时段数据
    print("\n📅 正在更新交易时段数据...")
    run_and_save()
    print("=" * 60)

    # ── 飞书控制（放父进程，常驻，非交易时段也能用）──
    ctrl_queue = queue.Queue()
    feishu = load_feishu_control()
    if feishu.get("app_id"):
        app = build_control_app(ctrl_queue, feishu["app_id"], feishu["app_secret"],
                                feishu["encrypt_key"], feishu["verification_token"],
                                feishu["bot_open_id"])
        threading.Thread(target=start_control,
                         args=(app, feishu["host"], 3000), daemon=True).start()
        print(f"飞书 HTTP 控制已启动（父进程），监听 {feishu['host']}:3000", flush=True)
        if feishu.get("public_url"):
            print(f"飞书后台「请求地址」填: {feishu['public_url']}/webhook/feishu", flush=True)

    child_process = None
    parent_conn = None
    restart_count = 0
    max_restart = 5
    manual_stop = False  # stop 指令后阻止自动重启
    child_start_time = 0.0  # 子进程启动时间戳（稳定运行过则重置崩溃计数）

    while True:
        try:
            trading = check_trading_period()

            # ── 处理飞书控制指令 ──
            if feishu.get("app_id"):
                try:
                    cmd = ctrl_queue.get_nowait()
                except queue.Empty:
                    cmd = None
                if cmd is not None:
                    action, msg_id = cmd
                    if action == "stop":
                        manual_stop = True
                        if child_process is not None and child_process.is_alive():
                            print("🛑 飞书指令：停止子进程（正常退出，撤挂单+写持仓）", flush=True)
                            if parent_conn is not None:
                                parent_conn.send("stop")
                            child_process.join(timeout=30)
                            if child_process.is_alive():
                                print("⚠️ 子进程未在30秒内退出，强制终止", flush=True)
                                child_process.terminate()
                                child_process.join(timeout=5)
                                if child_process.is_alive():
                                    child_process.kill()
                            child_process = None
                        reply_text(feishu.get("app_id"), feishu.get("app_secret"),
                                   msg_id, "停止已完成")
                    elif action == "restart":
                        manual_stop = False
                        if child_process is not None and child_process.is_alive():
                            print("🔄 飞书指令：重启子进程（正常退出旧子进程）", flush=True)
                            if parent_conn is not None:
                                parent_conn.send("stop")
                            child_process.join(timeout=30)
                            if child_process.is_alive():
                                child_process.terminate()
                                child_process.join(timeout=5)
                                if child_process.is_alive():
                                    child_process.kill()
                            child_process = None
                        if trading:
                            parent_conn, child_conn = multiprocessing.Pipe()
                            child_process = multiprocessing.Process(target=run_child, args=(child_conn,))
                            child_process.start()
                            child_start_time = time_mod.time()
                            reply_text(feishu.get("app_id"), feishu.get("app_secret"),
                                       msg_id, f"重启已完成 (PID {child_process.pid})")
                        else:
                            reply_text(feishu.get("app_id"), feishu.get("app_secret"),
                                       msg_id, "重启已完成（非交易时段，子进程未启动）")

            if trading and child_process is None and not manual_stop:
                print(f"\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                print("🔄 交易时段开始，启动子进程...")
                parent_conn, child_conn = multiprocessing.Pipe()
                child_process = multiprocessing.Process(target=run_child, args=(child_conn,))
                child_process.start()
                print(f"✅ 子进程启动成功 (PID: {child_process.pid})")
                child_start_time = time_mod.time()

            if not trading and child_process is not None:
                if not child_process.is_alive():
                    child_process = None
                    print("✅ 子进程已关闭")
                else:
                    print("⏹ 非交易时段，等待子进程自动关闭...")
                    sleep(10)
                    if child_process and child_process.is_alive():
                        print("⚠️ 子进程未自动退出，强制终止...")
                        child_process.terminate()
                        child_process.join(timeout=5)
                        child_process = None

            if child_process is not None and not child_process.is_alive():
                print("⚠️ 子进程意外退出")
                child_process = None

                if manual_stop:
                    pass  # 手动停止，不自动重启
                elif trading:
                    # 稳定运行过一段时间才重置崩溃计数，否则连续崩溃累加触发熔断
                    if time_mod.time() - child_start_time > 180:
                        restart_count = 0
                    restart_count += 1
                    if restart_count < max_restart:
                        print(f"🔄 尝试重启子进程 ({restart_count}/{max_restart})...")
                        sleep(3)
                    else:
                        print(f"❌ 重启次数过多 ({max_restart})，暂停重启")
                        sleep(60)
                        restart_count = 0

            sleep(1)

        except KeyboardInterrupt:
            print("\n⚠️ 收到中断信号，正在退出...")
            break
        except Exception as e:
            print(f"❌ 父进程异常: {e}")
            sleep(5)

    if child_process is not None and child_process.is_alive():
        print("🔄 正在终止子进程...")
        child_process.terminate()
        child_process.join(timeout=10)
        if child_process.is_alive():
            child_process.kill()
        print("✅ 子进程已终止")

    print("=" * 60)
    print("✅ 程序正常退出")
    print("=" * 60)


if __name__ == "__main__":
    run_parent()
