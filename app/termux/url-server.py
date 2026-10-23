#!/data/data/com.termux/files/usr/bin/python3
# -*- coding: utf-8 -*-
"""DSH App <-> Termux 的小接口（只读、只绑 127.0.0.1:3097）。

  GET /health    -> {"ok": true}
  R6-1: /dsh-url is removed. Token delivery uses the protected task channel.
  GET /ensure    -> 触发 boot.sh（幂等），立刻返回 {"ok": true}

另外在 127.0.0.1:3098 上听一条"存活连接"（DSB App 开着的时候一直连着）：
**App 的进程一死，这条连接立刻断** —— 手机侧据此马上执行 shutdown.sh，
把 dsh web / 代理桥 / 本服务 / 看门狗全停掉（用户要的"划掉 App = 彻底安静省电"）。
为什么不靠 App 发命令：实测划掉后系统 **3 毫秒**就把 App 进程 SIGKILL 了，
onTaskRemoved 里发的广播/命令根本送不出去。

R6-1 的 token 和任务结果由 RUN_COMMAND + 受权限保护的 CmdReceiver 传输，
不依赖 PendingIntent 回传；本服务不再向回环 HTTP 交出 token。
"""
import http.server
import json
import os
import subprocess
import threading
import time

# 定位 Termux 家目录（踩过两个坑）：
#   * os.path.expanduser("~") → /data/user/0/com.termux（passwd 里的值，不是家目录）
#   * 从 run-as/adb 拉起时 $HOME 也可能是 /data/user/0/com.termux
# 真正的家目录是 /data/data/com.termux/files/home，所以按候选列表探测。
def _termux_home():
    env = os.environ.get("HOME", "")
    for c in ("/data/data/com.termux/files/home",
              os.path.join(env, "files", "home") if env else "",
              env):
        if c and os.path.isdir(c):
            return c
    return "/data/data/com.termux/files/home"


HOME = _termux_home()
BIN = "/data/data/com.termux/files/usr/bin"
BOOT = os.path.join(HOME, "dsb", "boot.sh")
UP = os.path.join(HOME, "dsb", "dsh-up.sh")
URLFILE = os.path.join(HOME, ".dsb-web-url")

_lock = threading.Lock()
_last_spawn = [0.0]

ALIVE_PORT = 3098          # App 的"存活连接"端口
SHUTDOWN = os.path.join(HOME, "dsb", "shutdown.sh")
GRACE_SECS = 3.0           # 连接断了先等 3 秒，可能是 App 重启进程
_alive_clients = 0
_alive_lock = threading.Lock()
_alive_timer = [None]


def _log(msg):
    try:
        with open(os.path.join(HOME, "dsb", "alive.log"), "a") as fh:
            fh.write("%s %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def _alive_lost():
    if os.path.exists(os.path.join(HOME, '.dsb-ipc', 'maintenance')):
        _log('maintenance: skipping DSB shutdown')
        return
    with _alive_lock:
        if _alive_clients > 0:
            return
        _alive_timer[0] = None
    _log("App 走了 → 执行 shutdown.sh")
    try:
        subprocess.Popen([BIN + "/bash", SHUTDOWN],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:
        _log("shutdown 失败: %s" % e)


def _alive_conn(conn):
    global _alive_clients
    with _alive_lock:
        _alive_clients += 1
        if _alive_timer[0] is not None:
            _alive_timer[0].cancel()
            _alive_timer[0] = None
    _log("App 连上了 (clients=%d)" % _alive_clients)
    try:
        conn.settimeout(None)
        while True:
            if not conn.recv(256):
                break
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
        with _alive_lock:
            _alive_clients -= 1
            left = _alive_clients
            if left <= 0:
                t = threading.Timer(GRACE_SECS, _alive_lost)
                _alive_timer[0] = t
                t.start()
        _log("App 连接断开 (clients=%d)" % left)


def serve_alive():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", ALIVE_PORT))
    s.listen(8)
    while True:
        try:
            c, _ = s.accept()
        except Exception:
            time.sleep(0.5)
            continue
        threading.Thread(target=_alive_conn, args=(c,), daemon=True).start()


def curl_code(url, timeout=5):
    if not url:
        return ""
    try:
        p = subprocess.run([BIN + "/curl", "-s", "-o", "/dev/null", "-m", str(timeout),
                            "-w", "%{http_code}", url],
                           capture_output=True, text=True, timeout=timeout + 4)
        return (p.stdout or "").strip()
    except Exception:
        return ""


def cached_url():
    try:
        with open(URLFILE) as f:
            return f.read().strip()
    except Exception:
        return ""


def spawn(script):
    """后台拉起脚本；5 秒内不重复拉同一个"""
    with _lock:
        now = time.time()
        if now - _last_spawn[0] < 5:
            return
        _last_spawn[0] = now
    try:
        subprocess.Popen([BIN + "/bash", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        pass


def dsh_url():
    u = cached_url()
    if u and curl_code(u) in ("200", "303"):
        return {"ok": True, "url": u}
    spawn(UP)          # 后台修复（可能要十几秒重新拉起 dsh）
    return {"ok": False, "reason": "dsh 未就绪或 token 已失效，已触发修复"}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "dsb/1.0"

    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            if self.path.startswith("/health"):
                self._send({"ok": True, "ts": int(time.time())})
            elif self.path.startswith("/ensure"):
                spawn(BOOT)
                self._send({"ok": True})
            else:
                self._send({"ok": False, "reason": "unknown path"}, 404)
        except Exception as e:
            try:
                self._send({"ok": False, "reason": str(e)}, 500)
            except Exception:
                pass

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    threading.Thread(target=serve_alive, daemon=True).start()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 3097), Handler)
    srv.serve_forever()
