#!/usr/bin/env python
# -*- coding: utf-8 -*-
# ============================================================================
# Meeting Captioner & AI Interview Assistant
# GitHub: https://github.com/hufubufen/meeting-captioner
# 💡 觉得好用欢迎给作者点个 Star ⭐️ 支持一下！
# ============================================================================
import os
import threading
import time
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import random
import socket
import subprocess
import re
import logging

logger = logging.getLogger("captioner")

class SuggestionWebServer(threading.Thread):
    """轻量 HTTP 服务，将 AI 建议实时推送到手机浏览器。
    面试时：电脑共享屏幕，手机打开 http://电脑IP:8765 查看建议。"""

    PORT = 8765

    def __init__(self, pin=None):
        super().__init__(daemon=True)
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._latest = {
            "question": "",
            "answer": "",
            "timestamp": "",
        }
        # 伪装模式：开启后桌面端显示纯白记事本界面，AI内容只在手机可见
        self._stealth_mode = False
        self.on_stealth_toggle = None  # 回调: callback(mode: bool) -> None
        self.on_manual_ask = None  # 手机端手动微调问题回调: callback(text: str) -> None
        self._latest_caption = {
            "text": "等待说话...",
            "timestamp": "00:00:00"
        }
        self._ai_paused = False
        
        if pin:
            self._pin = pin
        else:
            # 升级为高强度 6 位数字与小写字母混合 PIN 码
            chars = "abcdefghijklmnopqrstuvwxyz0123456789"
            self._pin = "".join(random.choice(chars) for _ in range(6))
        
        # 安全加固：IP限流与爆破拦截
        self._ip_fail_count = {}   # {ip: 连续错误次数}
        self._ip_blocklist = {}    # {ip: 解封时间戳}

        # SSE 多并发连接数防护限制
        self._active_connections = 0
        self._conn_lock = threading.Lock()
        self._current_session_id = 0.0

    def update(self, question, answer, timestamp):
        """AI 线程回调：更新最新建议"""
        with self._lock:
            self._latest = {
                "question": question,
                "answer": answer,
                "timestamp": timestamp,
            }

    def update_caption(self, text, timestamp):
        """转录线程回调：更新最新原始转写字幕"""
        with self._lock:
            self._latest_caption = {
                "text": text,
                "timestamp": timestamp,
            }

    def set_paused(self, paused):
        """AI 控制线程状态更新回调：同步暂停收音状态"""
        with self._lock:
            self._ai_paused = paused

    @staticmethod
    def _get_local_ip():
        """获取本机真实局域网 IP（排除虚拟网卡）"""
        VIRTUAL_KEYWORDS = [
            'hyper-v', 'vethernet', 'docker', 'wsl',
            'vmware', 'virtualbox', 'virtual', 'mihomo',
            'loopback', 'bluetooth', '回环',
        ]

        try:
            output = subprocess.check_output(
                ['ipconfig'], encoding='gbk', errors='ignore',
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0
            )

            sections = re.split(
                r'\n(?=[^\n]*(?:适配器|adapter)\s+[^\n]*:\s*\n)',
                output, flags=re.IGNORECASE
            )

            wifi_ip = None       # WLAN / 无线
            ethernet_ip = None   # 以太网 / Ethernet (非虚拟)
            fallback_ip = None   # 其他非虚拟私有 IP

            for section in sections:
                header_match = re.match(r'([^\n]*?)[:：]\s*\n', section)
                adapter_name = header_match.group(1).strip() if header_match else ''

                name_lower = adapter_name.lower()
                if any(kw in name_lower for kw in VIRTUAL_KEYWORDS):
                    continue

                if '媒体已断开' in section or 'Media disconnected' in section:
                    continue

                ipv4_match = re.search(r'IPv4[^:]*:\s*(\d+\.\d+\.\d+\.\d+)', section)
                if not ipv4_match:
                    continue
                ip = ipv4_match.group(1)

                if ip.startswith('127.') or ip.startswith('169.254.'):
                    continue
                if ip.startswith('198.18.') or ip.startswith('198.19.'):
                    continue

                is_private = (
                    ip.startswith('192.168.') or
                    ip.startswith('10.') or
                    (ip.startswith('172.') and 16 <= int(ip.split('.')[1]) <= 31)
                )
                if not is_private:
                    continue

                if 'wlan' in name_lower or '无线' in name_lower:
                    wifi_ip = ip
                elif ('以太网' in name_lower or 'ethernet' in name_lower) and 'vether' not in name_lower:
                    ethernet_ip = ip
                elif fallback_ip is None:
                    fallback_ip = ip

            for ip in (wifi_ip, ethernet_ip, fallback_ip):
                if ip:
                    return ip

        except Exception:
            pass

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("114.114.114.114", 80))
            ip = s.getsockname()[0]
            s.close()
            if not ip.startswith('198.18.'):
                return ip
        except Exception:
            pass

        return "127.0.0.1"

    def _make_handler(self):
        """创建请求处理器（闭包捕获 self）"""
        server = self
        
        # 1. 尝试读取独立的静态 HTML 文件以达到解耦目的
        # 兼容 PyInstaller/Nuitka 打包后的临时解压资源路径
        import sys
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            html_path = os.path.join(sys._MEIPASS, "index.html")
        else:
            html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
        try:
            with open(html_path, 'r', encoding='utf-8') as f:
                HTML_TEMPLATE = f.read()
        except Exception as e:
            logger.error(f"加载 index.html 失败: {e}，将采用内存极简模板进行降级兜底。")
            HTML_TEMPLATE = r"""<!DOCTYPE html>
            <html lang="zh">
            <head><meta charset="utf-8"><title>面试辅助(降级版)</title></head>
            <body><h1 style='text-align:center;'>面试辅助 (内置降级模版)</h1></body>
            </html>"""

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlparse(self.path)
                path = parsed.path
                params = parse_qs(parsed.query)
                client_ip = self.client_address[0]

                # 已移除局域网 PIN 码与 IP 鉴权限制，放行所有局域网内请求以提供最佳免密秒连体验
                pass

                # 4. 路由逻辑
                if path == "/api/stream":
                    # 抢占式单活跃连接 Session 顶号机制：一旦有新连接建立，分配最新的 session_id 并写入全局，使旧连接自动安全退出自毁
                    session_id = time.time()
                    with server._lock:
                        server._current_session_id = session_id

                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    try:
                        # 当服务没有被停止，且当前会话没有被新的连接顶替时，持续发送
                        while not server._stop_event.is_set() and server._current_session_id == session_id:
                            with server._lock:
                                payload = {
                                    "question": server._latest["question"],
                                    "answer": server._latest["answer"],
                                    "timestamp": server._latest["timestamp"],
                                    "caption_text": server._latest_caption["text"],
                                    "caption_timestamp": server._latest_caption["timestamp"],
                                    "stealth_mode": server._stealth_mode,
                                    "ai_paused": server._ai_paused,
                                }
                                data = json.dumps(payload, ensure_ascii=False)
                            self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
                            self.wfile.flush()
                            time.sleep(0.3)
                    except Exception:
                        pass
                elif path == "/api/latest":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    with server._lock:
                        payload = {
                            "question": server._latest["question"],
                            "answer": server._latest["answer"],
                            "timestamp": server._latest["timestamp"],
                            "caption_text": server._latest_caption["text"],
                            "caption_timestamp": server._latest_caption["timestamp"],
                            "stealth_mode": server._stealth_mode,
                            "ai_paused": server._ai_paused,
                        }
                        data = json.dumps(payload, ensure_ascii=False)
                    self.wfile.write(data.encode("utf-8"))
                elif path == "/api/stealth":
                    params = parse_qs(parsed.query)
                    mode = params.get("mode", [None])[0]
                    if mode is not None:
                        new_state = (mode == "on")
                        with server._lock:
                            server._stealth_mode = new_state
                        if server.on_stealth_toggle:
                            try:
                                server.on_stealth_toggle(new_state)
                            except Exception:
                                pass
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    with server._lock:
                        resp = json.dumps({"stealth_mode": server._stealth_mode}, ensure_ascii=False)
                    self.wfile.write(resp.encode("utf-8"))
                elif path == "/api/ask":
                    params = parse_qs(parsed.query)
                    text = params.get("text", [None])[0]
                    if text:
                        if server.on_manual_ask:
                            try:
                                server.on_manual_ask(text)
                            except Exception:
                                pass
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "ok"}).encode("utf-8"))
                elif path == "/" or path == "/index.html":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    self.wfile.write(HTML_TEMPLATE.encode("utf-8"))
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, format, *args):
                pass  # 静默控制台请求垃圾日志

        return Handler

    def run(self):
        from http.server import ThreadingHTTPServer
        ThreadingHTTPServer.allow_reuse_address = True
        handler = self._make_handler()
        self._httpd = ThreadingHTTPServer(("0.0.0.0", self.PORT), handler)
        ip = self._get_local_ip()
        logger.info(f"[Web] 手机端面试物理防窥服务端启动成功 (PORT: {self.PORT}, PIN: {self._pin})")
        logger.info(f"[Web] 请在外部浏览器访问: http://{ip}:{self.PORT}/?key={self._pin}")
        self._httpd.serve_forever()

    def stop(self):
        self._stop_event.set()
        if hasattr(self, '_httpd') and self._httpd:
            try:
                self._httpd.shutdown()
            except Exception:
                pass
            try:
                self._httpd.server_close() # 必须显式释放 TCP 端口套接字绑定，防范二次启动OSError冲突
            except Exception:
                pass
        self.join(timeout=2)
