"""
自动刷新调度器 - Auto Sync Scheduler
  1. 销量刷新:    每日 09:00   POST /api/sync/sales
  2. 在途+工厂:   每日 09:30   POST /api/sync/in_transit + factory_stock
  3. 库存刷新:    每小时整点   POST /api/sync/inventory（增量）

每日早间刷新完成后自动重启服务器，确保内存状态刷新。

用 pythonw.exe 启动可实现无窗口后台运行
"""

import subprocess
import time
import os
import sys
import json
import urllib.request
import urllib.error
import logging
from datetime import datetime, date

BASE_URL = "http://localhost:8081"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
LOCK_FILE = os.path.join(APP_DIR, "auto_sync.lock")
LOG_FILE = os.path.join(APP_DIR, "auto_sync.log")

# 配置日志
handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
logging.basicConfig(level=logging.INFO, handlers=[handler])
logger = logging.getLogger('auto_sync')


def _now():
    return datetime.now().strftime('%H:%M:%S')


def acquire_lock():
    """确保只有一个调度器实例运行"""
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, 'r') as f:
                old_data = json.loads(f.read())
            old_pid = str(old_data.get('pid', ''))
        except Exception:
            old_pid = ""

        if old_pid:
            import subprocess
            try:
                check = subprocess.run(
                    ['wmic', 'process', 'where', f'ProcessId={old_pid}',
                     'get', 'CommandLine', '/format:csv'],
                    capture_output=True, text=True, timeout=8,
                    encoding='gbk', errors='replace'
                )
                if 'auto_sync.py' in check.stdout and old_pid in check.stdout:
                    logger.warning(f"调度器已在运行 (PID={old_pid})")
                    return False
            except Exception:
                pass
            # 清理过期锁
            try:
                os.remove(LOCK_FILE)
            except Exception:
                pass

    with open(LOCK_FILE, 'w') as f:
        json.dump({'pid': os.getpid(), 'started': datetime.now().isoformat()}, f)
    return True


def release_lock():
    try:
        os.remove(LOCK_FILE)
    except Exception:
        pass


def call_sync(sync_type):
    """调用刷新API，返回 (success, message)"""
    url = f"{BASE_URL}/api/sync/{sync_type}"
    try:
        req = urllib.request.Request(url, method='POST', data=b'')
        req.add_header('Content-Type', 'application/json')
        with urllib.request.urlopen(req, timeout=600) as resp:
            result = resp.read().decode('utf-8')
            logger.info(f"[{sync_type}] 刷新成功: {result[:200]}")
            return True, result[:100]
    except urllib.error.URLError as e:
        msg = f"网络错误: {e.reason}"
        logger.error(f"[{sync_type}] {msg}")
        return False, msg
    except Exception as e:
        msg = f"异常: {e}"
        logger.error(f"[{sync_type}] {msg}")
        return False, msg


PORT = 8081


def is_server_alive():
    """检查服务器是否存活"""
    try:
        req = urllib.request.Request(f"{BASE_URL}/api/health")
        with urllib.request.urlopen(req, timeout=8) as resp:
            return resp.status == 200
    except Exception:
        return False


def restart_server():
    """杀掉 8081 端口的服务器进程，由 daemon.py 自动重启"""
    logger.info(">>> 准备重启服务器...")
    try:
        result = subprocess.run(
            ['netstat', '-ano'],
            capture_output=True, text=True, timeout=5,
            encoding='gbk', errors='replace'
        )
        killed = False
        for line in result.stdout.split('\n'):
            if f':{PORT}' in line and 'LISTENING' in line:
                parts = line.strip().split()
                pid = parts[-1]
                if pid.isdigit() and pid != str(os.getpid()):
                    logger.info(f"  杀掉服务器进程 PID={pid}")
                    subprocess.run(['taskkill', '/F', '/PID', pid],
                                   capture_output=True, timeout=5)
                    killed = True

        if not killed:
            logger.warning("  未找到占用 8081 的服务器进程")
            return True  # 没有进程需要杀，也算"成功"

        # 等待 daemon.py 检测到并重启（最多等 120 秒）
        logger.info("  等待 daemon.py 重启服务器...")
        for i in range(60):
            time.sleep(2)
            if is_server_alive():
                elapsed = (i + 1) * 2
                logger.info(f"<<< 服务器重启成功 (耗时 {elapsed}s)")
                return True

        logger.error("<<< 服务器重启超时（120秒）")
        return False

    except Exception as e:
        logger.error(f"<<< 重启服务器异常: {e}")
        return False


def main():
    if not acquire_lock():
        print("调度器已在运行，退出")
        return

    logger.info("=" * 50)
    logger.info("自动刷新调度器启动")
    logger.info("  销量刷新:     每日 09:00")
    logger.info("  在途+工厂:    每日 09:30")
    logger.info("  库存刷新:     每小时整点（增量）")
    logger.info("  早间完成后:   自动重启服务器")
    logger.info("=" * 50)

    # 等待服务器就绪
    logger.info("等待服务器就绪...")
    for i in range(30):
        if is_server_alive():
            logger.info("服务器已就绪，开始调度")
            break
        time.sleep(10)
    else:
        logger.warning("等待超时，调度器继续运行（服务器就绪后自动开始）")

    last_sales_date = None          # 销量: 防止同一天内重复触发
    last_morning_seq_date = None    # 在途+工厂早间序列: 防止同一天内重复触发
    last_inventory_hour = None      # 库存: 防止同一小时内重复触发

    try:
        while True:
            now = datetime.now()

            # 服务器存活检查（但如果是刚重启完还在恢复期，跳过告警）
            if not is_server_alive():
                logger.warning("服务器无响应，跳过本轮")
                time.sleep(30)
                continue

            # ========== 每日早间刷新序列 ==========

            # --- ① 销量刷新: 每日 09:00 ---
            if now.hour == 9 and last_sales_date != now.date():
                logger.info(f">>> 触发每日销量刷新 (09:00)")
                ok, msg = call_sync('sales')
                if ok:
                    last_sales_date = now.date()
                    logger.info(f"<<< 销量刷新完成")
                else:
                    logger.error(f"<<< 销量刷新失败: {msg}")

            # --- ② 在途数据 + 工厂库存: 每日 09:30 ---
            if now.hour == 9 and 30 <= now.minute <= 35 and last_morning_seq_date != now.date():
                logger.info(f">>> 触发早间序列：在途数据 + 工厂库存 (09:30)")

                # 在途数据
                ok1, msg1 = call_sync('in_transit')
                if ok1:
                    logger.info(f"  [in_transit] 刷新完成")
                else:
                    logger.error(f"  [in_transit] 刷新失败: {msg1}")

                # 工厂库存
                ok2, msg2 = call_sync('factory_stock')
                if ok2:
                    logger.info(f"  [factory_stock] 刷新完成")
                else:
                    logger.error(f"  [factory_stock] 刷新失败: {msg2}")

                last_morning_seq_date = now.date()

                # 早间序列完成后，重启服务器
                logger.info(">>> 早间刷新序列完成，开始重启服务器...")
                restart_server()

            # ========== 每小时库存刷新 ==========

            # 库存刷新: 每小时整点附近（0-5 分窗口），但跳过 9:30-9:35（避免与早间序列冲突）
            if 0 <= now.minute <= 5 and last_inventory_hour != now.hour:
                if now.hour == 9 and 30 <= now.minute <= 35:
                    pass  # 跳过，留给早间序列
                else:
                    logger.info(f">>> 触发每小时库存刷新 ({now.hour}:00)")
                    ok, msg = call_sync('inventory')
                    if ok:
                        last_inventory_hour = now.hour
                        logger.info(f"<<< 库存刷新完成")
                    else:
                        logger.error(f"<<< 库存刷新失败: {msg}")

            # 30 秒检查一次
            time.sleep(30)

    except KeyboardInterrupt:
        logger.info("调度器收到退出信号")
    except Exception as e:
        logger.error(f"调度器异常退出: {e}")
    finally:
        release_lock()
        logger.info("调度器已停止")


if __name__ == '__main__':
    main()
