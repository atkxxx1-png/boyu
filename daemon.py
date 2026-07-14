"""
服务守护进程 - 确保 AI Agent 稳定运行
用 pythonw.exe 运行此脚本可实现完全无窗口的后台守护
每隔 20 秒检查服务器是否存活，挂了就重启
单一守护实例，防止多守护互杀
"""

import subprocess
import time
import os
import sys
import json
import urllib.request
import urllib.error
import logging
from datetime import datetime

PORT = 8081
CHECK_URL = f"http://localhost:{PORT}/api/health"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
APP_SCRIPT = os.path.join(APP_DIR, "app.py")
PYTHON = r"C:\Users\86176\AppData\Local\Programs\Python\Python313\python.exe"
PYTHONW = r"C:\Users\86176\AppData\Local\Programs\Python\Python313\pythonw.exe"
LOG_FILE = os.path.join(APP_DIR, "daemon.log")
CHECK_INTERVAL = 20  # 秒（放宽到20秒，减少误判）
STARTUP_WAIT = 15    # 启动后等待时间
MAX_CRASH_COUNT = 5  # 连续崩溃上限
CRASH_RESET_TIME = 600  # 崩溃计数重置时间(秒)，提高到10分钟

# 配置日志
log_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
log_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
logging.basicConfig(level=logging.INFO, handlers=[log_handler])
log = logging.getLogger('daemon')

# 锁文件
LOCK_FILE = os.path.join(APP_DIR, "daemon.lock")


def acquire_lock():
    """
    确保只有一个守护进程运行。
    增强版：不仅检查 PID 文件，还验证该 PID 是否为 daemon.py 进程。
    """
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, 'r') as f:
                lock_data = json.loads(f.read())
            old_pid = str(lock_data.get('pid', ''))
        except Exception:
            old_pid = ""

        if old_pid:
            # 用 wmic 精确检查：该 PID 是否是 python 进程且命令行包含 daemon.py
            try:
                check = subprocess.run(
                    ['wmic', 'process', 'where', f'ProcessId={old_pid}',
                     'get', 'CommandLine', '/format:csv'],
                    capture_output=True, text=True, timeout=8,
                    encoding='gbk', errors='replace'
                )
                if 'daemon.py' in check.stdout and str(old_pid) in check.stdout:
                    log.warning(f"守护进程已在运行 (PID={old_pid})，退出")
                    print(f"守护进程已在运行 (PID={old_pid})，退出")
                    return False
            except Exception:
                pass

            # 旧进程不在运行或无 daemon.py，清理锁文件
            try:
                os.remove(LOCK_FILE)
                log.info("清理过期锁文件")
            except Exception:
                pass

    # 写入当前进程信息
    with open(LOCK_FILE, 'w') as f:
        json.dump({
            'pid': os.getpid(),
            'started': datetime.now().isoformat(),
            'port': PORT,
        }, f)
    return True


def release_lock():
    """释放锁文件"""
    try:
        os.remove(LOCK_FILE)
    except Exception:
        pass


def is_server_alive():
    """
    检查服务器是否存活。
    连续检查两次（间隔 3 秒），两次都失败才算真的挂了
    """
    def _check():
        try:
            req = urllib.request.Request(CHECK_URL, method='GET')
            with urllib.request.urlopen(req, timeout=8) as resp:
                return resp.status == 200
        except Exception:
            return False

    # 第一次检查
    if _check():
        return True

    # 等 3 秒再试一次，避免瞬时抖动误判
    time.sleep(3)
    return _check()


def is_port_in_use():
    """检查 8081 端口是否被占用"""
    try:
        result = subprocess.run(
            ['netstat', '-ano'],
            capture_output=True, text=True, timeout=5,
            encoding='gbk', errors='replace'
        )
        for line in result.stdout.split('\n'):
            if f':{PORT}' in line and 'LISTENING' in line:
                return True
        return False
    except Exception:
        return False


def start_server():
    """启动服务器进程"""
    log.info("启动服务器...")
    try:
        proc = subprocess.Popen(
            [PYTHONW, APP_SCRIPT, str(PORT)],
            cwd=APP_DIR,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log.info(f"服务器进程已启动 PID={proc.pid}")
        return proc
    except Exception as e:
        log.error(f"启动服务器失败(pythonw): {e}")
        try:
            proc = subprocess.Popen(
                [PYTHON, APP_SCRIPT, str(PORT)],
                cwd=APP_DIR,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            log.info(f"服务器进程已启动(Python回退) PID={proc.pid}")
            return proc
        except Exception as e2:
            log.error(f"启动服务器失败(回退也失败): {e2}")
            return None


def kill_server_process():
    """杀掉 8081 端口的服务器进程（不是守护进程本身）"""
    try:
        result = subprocess.run(
            ['netstat', '-ano'],
            capture_output=True, text=True, timeout=5,
            encoding='gbk', errors='replace'
        )
        killed = []
        for line in result.stdout.split('\n'):
            if f':{PORT}' in line and 'LISTENING' in line:
                parts = line.strip().split()
                pid = parts[-1]
                if pid.isdigit() and pid != str(os.getpid()):
                    log.info(f"杀掉旧服务器进程 PID={pid}")
                    subprocess.run(['taskkill', '/F', '/PID', pid],
                                   capture_output=True, timeout=5)
                    killed.append(pid)
        return bool(killed)
    except Exception as e:
        log.error(f"清理进程出错: {e}")
        return False


def main():
    # 获取锁
    if not acquire_lock():
        return

    log.info("=" * 50)
    log.info("供应链 AI Agent 守护进程启动 v2")
    log.info(f"监控地址: {CHECK_URL}")
    log.info(f"检查间隔: {CHECK_INTERVAL}秒（含防抖）")
    log.info("=" * 50)

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 守护进程启动 (PID={os.getpid()})")
    print(f"  监控: {CHECK_URL}")
    print(f"  日志: {LOG_FILE}")

    # 智能启动：如果已有服务器在运行，不杀
    if is_port_in_use():
        log.info("端口 8081 已被占用，跳过启动，直接进入监控")
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 服务器已在运行，直接监控")
    else:
        start_server()
        time.sleep(STARTUP_WAIT)

        if is_server_alive():
            log.info("服务器初始启动成功")
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ 服务器启动成功")
        else:
            log.warning("服务器初始启动失败，等待自动重试...")
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠ 等待服务器就绪...")

    # 连续崩溃计数
    crash_count = 0
    last_crash_time = 0

    while True:
        try:
            if is_server_alive():
                # 服务器正常
                if crash_count > 0:
                    log.info(f"服务器恢复正常（已连续崩溃 {crash_count} 次）")
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ 服务器已恢复")
                crash_count = 0
                time.sleep(CHECK_INTERVAL)
            else:
                now = time.time()
                # 崩溃计数重置
                if now - last_crash_time > CRASH_RESET_TIME:
                    crash_count = 0
                crash_count += 1
                last_crash_time = now

                log.warning(f"服务器无响应 (连续第{crash_count}次)")

                if crash_count > MAX_CRASH_COUNT:
                    log.error(f"连续崩溃超过{MAX_CRASH_COUNT}次，暂停 10 分钟后重试")
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ 连续崩溃过多，暂停 10 分钟")
                    time.sleep(600)
                    crash_count = 0
                    continue

                print(f"[{datetime.now().strftime('%H:%M:%S')}] ⚠ 重启服务器...")
                kill_server_process()
                time.sleep(3)
                start_server()

                # 等待启动（最多等 30 秒）
                recovered = False
                for i in range(15):
                    time.sleep(2)
                    if is_server_alive():
                        log.info("服务器重启成功")
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ 服务器已恢复")
                        recovered = True
                        break

                if not recovered:
                    log.error("重启失败，将在下次检查周期重试")
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ 重启失败，下次再试")

        except KeyboardInterrupt:
            log.info("守护进程被手动停止")
            print("\n守护进程已停止")
            break
        except Exception as e:
            log.error(f"守护循环异常: {e}")
            time.sleep(30)

    release_lock()


if __name__ == '__main__':
    main()
