#!/usr/bin/env python
# -*- coding: utf-8 -*-
import os
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"  # 修复 conda+pip OpenMP 冲突
os.environ['HF_ENDPOINT'] = 'https://hf-mirror.com'

import tkinter as tk
from tkinter import scrolledtext, messagebox, filedialog
from tkinter import ttk
import threading
import time
import json
import queue
from datetime import datetime
import logging

# 导入重构后的子模块
from knowledge_base import KnowledgeBase
from audio_capture import AudioCaptureThread
from transcription import TranscriptionThread
from web_server import SuggestionWebServer
from settings_dialog import SettingsDialog
from analysis import AIAnalysisThread

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
logger = logging.getLogger("captioner")

# ============================================================================
# 高端暗黑科技配色常数
# ============================================================================
BG_MAIN = "#0f0f1a"         # 深邃星空暗紫蓝
BG_CONTAINER = "#181829"    # 面板容器卡片背景
BG_TEXT_BOX = "#1e1e33"     # 文本框与下拉框背景
FG_TEXT = "#e2e8f0"         # 柔和乳白前景色
FG_MUTED = "#8a9ab0"        # 浅蓝灰辅助色
FG_CAPTION = "#38bdf8"      # 字幕文本色 (明亮天蓝)
FG_AI = "#34d399"           # AI 建议文本色 (明荷绿)

# 按钮控制组配色映射 (普通状态背景色, 悬停高亮背景色)
BTN_COLOR_MAP = {
    "primary": ("#2563eb", "#3b82f6"),      # 科技蓝 (开始/发送)
    "danger": ("#dc2626", "#ef4444"),       # 警告红 (停止)
    "secondary": ("#374151", "#4b5563"),    # 石墨灰 (普通控制)
    "warning": ("#d97706", "#f59e0b"),      # 琥珀黄 (暂停)
    "stealth": ("#4b5563", "#6b7280"),      # 伪装控制按钮
}

# ============================================================================
# UI 界面
# ============================================================================
class CaptionerUI:

    def __init__(self, root):
        self.root = root
        self.root.title("无标题 - 记事本")
        self.root.geometry("900x820")
        self.root.configure(bg=BG_MAIN)

        # 伪装图标为记事本（窗口标题栏 + 任务栏）
        try:
            import os, subprocess, tempfile, ctypes
            notepad = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'notepad.exe')
            if os.path.exists(notepad):
                # 设置 AppUserModelID，让 Windows 任务栏使用正确图标分组
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Windows.Notepad.1')

                icon_png = os.path.join(tempfile.gettempdir(), 'notepad_icon.png')
                if not os.path.exists(icon_png):
                    subprocess.run([
                        'powershell', '-NoProfile', '-Command',
                        f"Add-Type -AssemblyName System.Drawing; "
                        f"[System.Drawing.Icon]::ExtractAssociatedIcon('{notepad}')"
                        f".ToBitmap().Save('{icon_png}')"
                    ], capture_output=True, timeout=5)

                if os.path.exists(icon_png):
                    self._icon_photo = tk.PhotoImage(file=icon_png)
                    self.root.iconphoto(True, self._icon_photo)
                else:
                    self.root.iconbitmap(notepad)
        except Exception as e:
            logger.error(f"[图标] 设置失败: {e}")

        # 队列
        self.audio_queue = queue.Queue(maxsize=1000)
        self.text_queue = queue.Queue(maxsize=100)
        self.ai_queue = queue.Queue(maxsize=50)
        self.ai_response_queue = queue.Queue(maxsize=50)

        # 线程
        self.audio_thread = None
        self.transcription_thread = None
        self.ai_thread = None

        # 状态
        self.is_running = False
        self.ai_paused = False
        self.text_only_mode = tk.BooleanVar(value=False)
        self.mic_mode = tk.BooleanVar(value=False)  # True = 麦克风模式, False = 系统音频
        self.config = {}
        self.kb_text = ""
        self.resume_text = ""

        # Web 建议面板
        self.web_server = None

        # 可选模型列表
        self.available_models = [
            "qwen-turbo", "qwen-plus", "qwen-max", "qwen-long",
            "qwen-turbo-latest", "qwen-plus-latest", "qwen-max-latest",
            "qwen3-235b-a22b", "qwen-coder-plus",
        ]
        self.selected_model = tk.StringVar(value=self.config.get("model", "qwen-plus"))

        # 加载配置和文档
        self._load_config()
        self._load_documents()

        # 手机防窥端生命周期全局唯一连接密钥 (PIN)
        self.web_pin = self.config.get("web_pin", "")
        if not self.web_pin:
            import random
            chars = "abcdefghijklmnopqrstuvwxyz0123456789"
            self.web_pin = "".join(random.choice(chars) for _ in range(6))

        # 配置全局 ttk 控件样式 (消除默认的白底灰边)
        self.style = ttk.Style()
        self.style.theme_use('clam')
        self.style.configure(
            "TCombobox", 
            fieldbackground=BG_TEXT_BOX, 
            background=BTN_COLOR_MAP["secondary"][0],
            foreground=FG_TEXT, 
            arrowcolor=FG_TEXT,
            borderwidth=0
        )
        self.root.option_add("*TCombobox*Listbox.background", BG_TEXT_BOX)
        self.root.option_add("*TCombobox*Listbox.foreground", FG_TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", BTN_COLOR_MAP["primary"][0])
        self.root.option_add("*TCombobox*Listbox.selectForeground", FG_TEXT)

        self._create_ui()

        # 尝试防止窗口截屏/防捕获
        self.root.after(300, self._hide_from_capture)

        # 首次运行自动唤醒配置弹框引导
        if getattr(self, "is_first_run", False):
            self.root.after(600, lambda: self._open_settings(is_first_run=True))

    def _make_flat_button(self, parent, text, command, style_type="secondary", **kwargs):
        """创建一个带鼠标悬停亮化微交互的扁平化现代按钮"""
        bg_normal, bg_hover = BTN_COLOR_MAP.get(style_type, BTN_COLOR_MAP["secondary"])
        
        # 安全弹出默认可能被 kwargs 覆盖的参数，避免 got multiple values 报错
        font_style = kwargs.pop("font", ("Microsoft YaHei UI", 10, "bold"))
        padx_val = kwargs.pop("padx", 12)
        pady_val = kwargs.pop("pady", 6)
        
        btn = tk.Button(
            parent, text=text, command=command,
            bg=bg_normal, fg="#ffffff",
            activebackground=bg_hover, activeforeground="#ffffff",
            font=font_style, padx=padx_val, pady=pady_val,
            relief=tk.FLAT, bd=0, cursor="hand2",
            **kwargs
        )
        
        # 悬停亮化绑定
        btn.bind("<Enter>", lambda e: btn.config(bg=bg_hover))
        btn.bind("<Leave>", lambda e: btn.config(bg=bg_normal))
        return btn

    def _hide_from_capture(self):
        """Windows: 设置窗口对屏幕采集不可见"""
        import ctypes

        hwnd = self.root.winfo_id()
        if not hwnd:
            self.root.after(500, self._hide_from_capture)
            return

        user32 = ctypes.windll.user32
        if not user32.IsWindow(hwnd):
            self.root.after(500, self._hide_from_capture)
            return

        # 优先方案：DWMWA_CLOAK (Windows 11)
        try:
            DWMWA_CLOAK = 14
            cloaked = ctypes.c_int(1)
            dwmapi = ctypes.windll.dwmapi
            result = dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_CLOAK,
                ctypes.byref(cloaked), ctypes.sizeof(cloaked)
            )
            if result == 0:
                logger.info(f"[隐私] DWMWA_CLOAK 成功 -> 屏幕共享不可见 (hwnd={hwnd})")
                return
        except Exception:
            pass

        # 回退方案：WDA_EXCLUDEFROMCAPTURE (Windows 10)
        try:
            WDA_EXCLUDEFROMCAPTURE = 0x00000011
            result = user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
            if result:
                logger.info(f"[隐私] WDA_EXCLUDEFROMCAPTURE 成功 -> 显示黑矩形 (hwnd={hwnd})")
        except Exception as e:
            logger.error(f"[隐私] 设置防截屏异常: {e}")

    def _load_config(self):
        """加载 config.json，若不存在则自适应复制 config.example.json 或新建默认配置"""
        config_path = os.path.join(TOOL_DIR, "config.json")
        self.is_first_run = False
        
        if not os.path.exists(config_path):
            self.is_first_run = True
            example_path = os.path.join(TOOL_DIR, "config.example.json")
            if os.path.exists(example_path):
                try:
                    import shutil
                    shutil.copy2(example_path, config_path)
                    logger.info("首次启动：自动由 config.example.json 生成配置文件 config.json")
                except Exception as copy_err:
                    logger.error(f"首次启动自动拷贝配置失败: {copy_err}")
            
            # 若没拷贝成功或 example 不存在，则写入一个空的默认配置
            if not os.path.exists(config_path):
                default_conf = {
                    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    "api_key": "",
                    "model": "qwen-plus",
                    "rerank_model": "",
                    "system_prompt": "你是一个面试辅助助手..."
                }
                try:
                    with open(config_path, 'w', encoding='utf-8') as f:
                        json.dump(default_conf, f, ensure_ascii=False, indent=4)
                    logger.info("首次启动：成功创建默认 config.json")
                except Exception as write_err:
                    logger.error(f"创建默认 config.json 失败: {write_err}")

        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                self.config = json.load(f)
            # 校验 api_key 是否为空，若为空也视作首次/未配置状态
            if not self.config.get("api_key"):
                self.is_first_run = True
            logger.info("配置加载完成")
        except Exception as e:
            logger.error(f"配置加载失败: {e}")
            self.config = {"api_key": "", "model": "qwen-plus", "system_prompt": ""}
            self.is_first_run = True

        self.selected_model.set(self.config.get("model", "qwen-plus"))

    def _save_config(self):
        """保存配置到 config.json"""
        config_path = os.path.join(TOOL_DIR, "config.json")
        self.config["model"] = self.selected_model.get()
        try:
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, ensure_ascii=False, indent=4)
        except Exception as e:
            logger.error(f"保存配置失败: {e}")

    def _on_model_change(self, event=None):
        new_model = self.selected_model.get()
        logger.info(f"模型切换至: {new_model}")
        self._save_config()

    def _open_settings(self, is_first_run=False):
        """打开 API 设置对话框"""
        def on_save(result):
            self.config["base_url"] = result["base_url"]
            self.config["api_key"] = result["api_key"]
            self.config["model"] = result["model"]
            self.config["rerank_model"] = result["rerank_model"]
            self.config["web_pin"] = result["web_pin"]
            self.web_pin = result["web_pin"]
            self.selected_model.set(result["model"])
            self._save_config()
            logger.info(f"[设置] 界面配置已保存: base_url={result['base_url']}, model={result['model']}, web_pin={result['web_pin']}")
            if self.is_running:
                messagebox.showinfo("提示", "设置已保存，重启后生效。", parent=self.root)

        SettingsDialog(self.root, self.config, on_save=on_save, is_first_run=is_first_run)

    def _load_documents(self):
        """加载知识库和简历"""
        kb = KnowledgeBase("knowledge_base")
        self.kb_text = kb.load()
        if self.kb_text:
            logger.info(f"知识库: {len(self.kb_text)} 字符")
        else:
            logger.warning("知识库为空，请在 knowledge_base/ 目录放入文档")

        resume = KnowledgeBase("resume")
        self.resume_text = resume.load()
        if self.resume_text:
            logger.info(f"简历: {len(self.resume_text)} 字符")
        else:
            logger.warning("简历为空，请在 resume/ 目录放入简历")

    def _create_ui(self):
        """创建 UI"""
        # 顶部状态栏
        status_frame = tk.Frame(self.root, bg=BG_MAIN, height=35)
        status_frame.pack(fill=tk.X, padx=15, pady=(8, 2))
        self.status_frame = status_frame

        self.status_label = tk.Label(
            status_frame, text="● 已停止", fg="#94a3b8", bg=BG_MAIN,
            font=("Microsoft YaHei UI", 10, "bold")
        )
        self.status_label.pack(side=tk.LEFT, padx=(5, 15))

        self.kb_label = tk.Label(
            status_frame, text=f"知识库: {len(self.kb_text)}字", fg=FG_MUTED, bg=BG_MAIN,
            font=("Microsoft YaHei UI", 9)
        )
        self.kb_label.pack(side=tk.LEFT, padx=10)

        self.ai_status_label = tk.Label(
            status_frame, text="AI: 未启动", fg=FG_MUTED, bg=BG_MAIN,
            font=("Microsoft YaHei UI", 9)
        )
        self.ai_status_label.pack(side=tk.LEFT, padx=10)

        self.detail_mode_var = tk.StringVar(value="[深度: 自动]")
        self.detail_mode_label = tk.Label(
            status_frame, textvariable=self.detail_mode_var,
            fg="#56b6c2", bg=BG_MAIN,
            font=("Microsoft YaHei UI", 9)
        )
        self.detail_mode_label.pack(side=tk.LEFT, padx=10)

        self.web_url_label = tk.Label(
            status_frame, text="", fg="#58a6ff", bg=BG_MAIN,
            font=("Microsoft YaHei UI", 9, "underline")
        )
        self.web_url_label.pack(side=tk.LEFT, padx=15)

        model_label = tk.Label(
            status_frame, text="模型:", fg=FG_MUTED, bg=BG_MAIN,
            font=("Microsoft YaHei UI", 9)
        )
        model_label.pack(side=tk.LEFT, padx=(15, 5))

        self.model_combo = ttk.Combobox(
            status_frame, textvariable=self.selected_model,
            values=self.available_models, state="normal",
            width=14, font=("Microsoft YaHei UI", 9)
        )
        self.model_combo.pack(side=tk.LEFT, padx=5)
        self.model_combo.bind("<<ComboboxSelected>>", self._on_model_change)

        self.settings_btn = self._make_flat_button(
            status_frame, "⚙ 设置", self._open_settings, style_type="secondary",
            font=("Microsoft YaHei UI", 9), padx=8, pady=2
        )
        self.settings_btn.pack(side=tk.LEFT, padx=(8, 0))

        self.device_label = tk.Label(
            status_frame, text="[系统音频]", fg=FG_MUTED, bg=BG_MAIN,
            font=("Microsoft YaHei UI", 9)
        )
        self.device_label.pack(side=tk.RIGHT, padx=5)

        # ========== 上半区：字幕显示 ==========
        caption_label = tk.Label(
            self.root, text="📝 会议字幕（对方说话内容）", fg="#38bdf8", bg=BG_MAIN,
            font=("Microsoft YaHei UI", 10, "bold"), anchor="w"
        )
        caption_label.pack(fill=tk.X, padx=15, pady=(10, 2))
        self.caption_label = caption_label

        caption_frame = tk.Frame(self.root, bg=BG_CONTAINER, bd=0)
        caption_frame.pack(fill=tk.BOTH, expand=False, padx=15, pady=2)
        caption_frame.pack_propagate(False)
        caption_frame.configure(height=240)
        self.caption_frame = caption_frame

        self.text_display = scrolledtext.ScrolledText(
            caption_frame, wrap=tk.WORD,
            font=("Microsoft YaHei UI", 12),
            bg=BG_CONTAINER, fg=FG_TEXT,
            insertbackground=FG_TEXT, relief=tk.FLAT,
            padx=18, pady=12
        )
        self.text_display.pack(fill=tk.BOTH, expand=True)
        self.text_display.config(state=tk.DISABLED)

        # ========== 底部按钮区 (必须先pack，固定在底部) ==========
        self._create_buttons()

        # ========== 手动文本输入区 (按钮上方) ==========
        self._create_text_input()

        # ========== 下半区：AI 建议回答 ==========
        ai_label = tk.Label(
            self.root, text="🤖 AI 建议回答", fg="#34d399", bg=BG_MAIN,
            font=("Microsoft YaHei UI", 10, "bold"), anchor="w"
        )
        ai_label.pack(fill=tk.X, padx=15, pady=(12, 2))
        self.ai_label = ai_label

        ai_frame = tk.Frame(self.root, bg=BG_CONTAINER, bd=0)
        ai_frame.pack(fill=tk.BOTH, expand=True, padx=15, pady=2)
        self.ai_frame = ai_frame

        self.ai_display = scrolledtext.ScrolledText(
            ai_frame, wrap=tk.WORD,
            font=("Microsoft YaHei UI", 12),
            bg=BG_CONTAINER, fg=FG_TEXT,
            insertbackground=FG_TEXT, relief=tk.FLAT,
            padx=18, pady=12
        )
        self.ai_display.pack(fill=tk.BOTH, expand=True)
        self.ai_display.config(state=tk.DISABLED)

        # 定时器刷新
        self._update_text_display()
        self._update_ai_display()

        # 键盘快捷键绑定
        self.root.bind_all("<space>", self._toggle_ai)
        self.root.bind_all("0", self._set_detail_mode)
        self.root.bind_all("1", self._set_detail_mode)
        self.root.bind_all("2", self._set_detail_mode)

        self.stealth_mode = False
        self.stealth_frame = tk.Frame(self.root, bg="#ffffff")

    def _create_buttons(self):
        button_frame = tk.Frame(self.root, bg=BG_MAIN, height=60)
        button_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=15, pady=12)
        self.button_frame = button_frame

        self.start_btn = self._make_flat_button(
            button_frame, "▶ 开始面试", self._start_listening, style_type="primary"
        )
        self.start_btn.pack(side=tk.LEFT, padx=(0, 6))

        self.ai_toggle_btn = self._make_flat_button(
            button_frame, "⏸ 暂停收音 (空格)", self._toggle_ai, style_type="primary"
        )
        self.ai_toggle_btn.pack(side=tk.LEFT, padx=6)
        self.ai_toggle_btn.config(state=tk.DISABLED, bg="#1e293b", fg="#475569")  # 初始灰色禁用状态

        self.stop_btn = self._make_flat_button(
            button_frame, "■ 停止面试", self._stop_listening, style_type="danger"
        )
        self.stop_btn.pack(side=tk.LEFT, padx=6)
        self.stop_btn.config(state=tk.DISABLED, bg="#1e293b", fg="#475569")

        self.clear_btn = self._make_flat_button(
            button_frame, "✕ 清空", self._clear_text, style_type="secondary"
        )
        self.clear_btn.pack(side=tk.LEFT, padx=6)

        self.mic_toggle_btn = self._make_flat_button(
            button_frame, "🎤 系统音频", self._toggle_mic_mode, style_type="secondary"
        )
        self.mic_toggle_btn.pack(side=tk.LEFT, padx=6)

        self.stealth_btn = self._make_flat_button(
            button_frame, "🖥 伪装模式", self._toggle_stealth_btn, style_type="stealth"
        )
        self.stealth_btn.pack(side=tk.LEFT, padx=6)

        self.text_only_cb = tk.Checkbutton(
            button_frame, text="仅文本测试", variable=self.text_only_mode,
            bg=BG_MAIN, fg=FG_MUTED, selectcolor=BG_MAIN,
            activebackground=BG_MAIN, activeforeground=FG_TEXT,
            font=("Microsoft YaHei UI", 9), bd=0, highlightthickness=0
        )
        self.text_only_cb.pack(side=tk.RIGHT, padx=10)

        self.save_btn = self._make_flat_button(
            button_frame, "💾 保存记录", self._save_record, style_type="secondary"
        )
        self.save_btn.pack(side=tk.RIGHT, padx=(6, 0))

    def _create_text_input(self):
        input_frame = tk.Frame(self.root, bg=BG_MAIN, height=40)
        input_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=15, pady=(2, 0))
        self.input_frame = input_frame

        tk.Label(
            input_frame, text="✏ 手动输入:", fg=FG_MUTED, bg=BG_MAIN,
            font=("Microsoft YaHei UI", 10, "bold")
        ).pack(side=tk.LEFT, padx=(0, 8))

        self.text_input_entry = tk.Entry(
            input_frame,
            font=("Microsoft YaHei UI", 11),
            bg=BG_TEXT_BOX, fg=FG_TEXT,
            insertbackground=FG_TEXT, relief=tk.FLAT, bd=0
        )
        self.text_input_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 8), ipady=5)
        self.text_input_entry.bind("<Return>", self._send_text_question)

        self.send_btn = self._make_flat_button(
            input_frame, "发送", self._send_text_question, style_type="primary"
        )
        self.send_btn.pack(side=tk.LEFT)

    def _send_text_question(self, event=None):
        if not self.text_input_entry:
            return

        text = self.text_input_entry.get().strip()
        self.text_input_entry.delete(0, tk.END)

        if not text:
            return

        if not self.ai_thread:
            messagebox.showwarning("提示", "请先点击「开始面试」启动 AI 分析")
            return
        elif not self.ai_thread.is_alive():
            messagebox.showwarning(
                "AI 线程异常",
                "检测到 AI 分析线程已异常退出！\n\n"
                "请检查并排查以下原因：\n"
                "1. 您是否直接双击了 run.pyw 或 Python 文件启动？\n"
                "   （这可能使用了不含 openai 依赖的全局 Python。请务必双击运行「启动.bat」，确保在 dmh_env 虚拟环境中运行！）\n"
                "2. 您的 config.json 中的通义千问 API Key 是否填写正确？\n\n"
                "可查看项目根目录下的 captioner_log.txt 文件以了解具体错误日志。"
            )
            return

        timestamp = datetime.now().strftime("%H:%M:%S")
        self._append_caption(timestamp, f"[手动输入] {text}")

        try:
            self.ai_queue.put_nowait((timestamp, text))
        except Exception:
            pass

    def _toggle_mic_mode(self):
        if self.is_running:
            messagebox.showwarning("提示", "请先停止当前面试再切换音频模式")
            return
        current = self.mic_mode.get()
        self.mic_mode.set(not current)
        if self.mic_mode.get():
            self.mic_toggle_btn.config(text="📱 麦克风模式", bg="#d97706", activebackground="#f59e0b")
            # 绑定麦克风模式时的Hover颜色 (变更为琥珀黄警告色系)
            self.mic_toggle_btn.bind("<Enter>", lambda e: self.mic_toggle_btn.config(bg="#f59e0b"))
            self.mic_toggle_btn.bind("<Leave>", lambda e: self.mic_toggle_btn.config(bg="#d97706"))
            self.device_label.config(text="[麦克风输入]", fg="#d97706")
        else:
            bg_normal, bg_hover = BTN_COLOR_MAP["secondary"]
            self.mic_toggle_btn.config(text="🎤 系统音频", bg=bg_normal, activebackground=bg_hover)
            # 恢复普通的石墨灰Hover
            self.mic_toggle_btn.bind("<Enter>", lambda e: self.mic_toggle_btn.config(bg=bg_hover))
            self.mic_toggle_btn.bind("<Leave>", lambda e: self.mic_toggle_btn.config(bg=bg_normal))
            self.device_label.config(text="[系统音频]", fg=FG_MUTED)

    def _start_listening(self):
        """开始监听"""
        if self.is_running:
            return

        api_key = self.config.get("api_key", "")
        if not api_key or "在这里" in api_key:
            messagebox.showwarning("提示", "请先在设置中填入 API Key！")
            return

        base_url = self.config.get("base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        rerank_model = self.config.get("rerank_model", "") or None

        self.is_running = True
        self.ai_paused = False
        text_only = self.text_only_mode.get()

        # 激活控制按钮
        self.start_btn.config(state=tk.DISABLED, bg="#1e293b", fg="#475569")
        self.start_btn.unbind("<Enter>")
        self.start_btn.unbind("<Leave>")
        
        # 激活暂停/停止按钮，并重新绑定Hover
        self.ai_toggle_btn.config(state=tk.NORMAL, bg=BTN_COLOR_MAP["primary"][0], fg="#ffffff")
        self.ai_toggle_btn.bind("<Enter>", lambda e: self.ai_toggle_btn.config(bg=BTN_COLOR_MAP["primary"][1]))
        self.ai_toggle_btn.bind("<Leave>", lambda e: self.ai_toggle_btn.config(bg=BTN_COLOR_MAP["primary"][0]))
        
        self.stop_btn.config(state=tk.NORMAL, bg=BTN_COLOR_MAP["danger"][0], fg="#ffffff")
        self.stop_btn.bind("<Enter>", lambda e: self.stop_btn.config(bg=BTN_COLOR_MAP["danger"][1]))
        self.stop_btn.bind("<Leave>", lambda e: self.stop_btn.config(bg=BTN_COLOR_MAP["danger"][0]))

        self.text_only_cb.config(state=tk.DISABLED)
        self.mic_toggle_btn.config(state=tk.DISABLED, bg="#1e293b", fg="#475569")
        self.mic_toggle_btn.unbind("<Enter>")
        self.mic_toggle_btn.unbind("<Leave>")

        if text_only:
            self.status_label.config(text="● 文本测试中...", fg="#38bdf8")
            self.ai_status_label.config(text="AI: 运行中", fg="#34d399")

            self.ai_thread = AIAnalysisThread(
                self.ai_queue, self.ai_response_queue,
                api_key=api_key,
                model=self.selected_model.get(),
                system_prompt=self.config.get("system_prompt", ""),
                knowledge_base_text=self.kb_text,
                resume_text=self.resume_text,
                max_tokens=self.config.get("max_tokens", 500),
                base_url=base_url,
                rerank_model=rerank_model,
            )
            self.ai_thread.start()

            self._append_caption("系统", "文本测试模式启动（已跳过音频捕获）")
            self._append_ai("系统", "AI 分析已就绪，可在下方输入面试提问...")
            self._start_web_server()
        else:
            self.status_label.config(text="● 正在监听...", fg="#34d399")
            self.ai_status_label.config(text="AI: 运行中", fg="#34d399")

            # 启动音频采集线程
            capture_mode = "mic" if self.mic_mode.get() else "speaker"
            self.audio_thread = AudioCaptureThread(self.audio_queue, capture_mode=capture_mode)
            
            # 启动转录线程
            self.transcription_thread = TranscriptionThread(
                self.audio_queue, self.text_queue, self.ai_queue, use_gpu=True
            )

            self.audio_thread.start()
            self.transcription_thread.start()

            # 启动 AI 分析线程
            self.ai_thread = AIAnalysisThread(
                self.ai_queue, self.ai_response_queue,
                api_key=api_key,
                model=self.selected_model.get(),
                system_prompt=self.config.get("system_prompt", ""),
                knowledge_base_text=self.kb_text,
                resume_text=self.resume_text,
                max_tokens=self.config.get("max_tokens", 500),
                base_url=base_url,
                rerank_model=rerank_model,
            )
            self.ai_thread.start()

            mode_label = "麦克风" if self.mic_mode.get() else "系统音频"
            self._append_caption("系统", f"开始监听 ({mode_label})... AI 辅助已就绪")
            self._append_ai("系统", f"AI 分析已就绪 ({mode_label}模式)，等待面试官提问...")
            self._start_web_server()

    def _stop_listening(self):
        """停止监听"""
        if not self.is_running:
            return

        self.is_running = False
        self.ai_paused = False
        self.status_label.config(text="● 已停止", fg=FG_MUTED)
        self.ai_status_label.config(text="AI: 未启动", fg=FG_MUTED)
        
        # 恢复开始按钮与麦克风按钮
        self.start_btn.config(state=tk.NORMAL, bg=BTN_COLOR_MAP["primary"][0], fg="#ffffff")
        self.start_btn.bind("<Enter>", lambda e: self.start_btn.config(bg=BTN_COLOR_MAP["primary"][1]))
        self.start_btn.bind("<Leave>", lambda e: self.start_btn.config(bg=BTN_COLOR_MAP["primary"][0]))
        
        self.mic_toggle_btn.config(state=tk.NORMAL, bg=BTN_COLOR_MAP["secondary"][0], fg="#ffffff")
        self.mic_toggle_btn.bind("<Enter>", lambda e: self.mic_toggle_btn.config(bg=BTN_COLOR_MAP["secondary"][1]))
        self.mic_toggle_btn.bind("<Leave>", lambda e: self.mic_toggle_btn.config(bg=BTN_COLOR_MAP["secondary"][0]))

        # 禁用停止/暂停按钮，恢复初始灰
        self.ai_toggle_btn.config(state=tk.DISABLED, bg="#1e293b", fg="#475569", text="⏸ 暂停收音 (空格)")
        self.ai_toggle_btn.unbind("<Enter>")
        self.ai_toggle_btn.unbind("<Leave>")
        
        self.stop_btn.config(state=tk.DISABLED, bg="#1e293b", fg="#475569")
        self.stop_btn.unbind("<Enter>")
        self.stop_btn.unbind("<Leave>")

        self.text_only_cb.config(state=tk.NORMAL)

        if self.audio_thread:
            self.audio_thread.stop()
            self.audio_thread.join(timeout=1.0)
            self.audio_thread = None
        if self.transcription_thread:
            self.transcription_thread.stop()
            self.transcription_thread.join(timeout=1.0)
            self.transcription_thread = None
        if self.ai_thread:
            self.ai_thread.stop()
            self.ai_thread.join(timeout=1.0)
            self.ai_thread = None

        self._stop_web_server()

        self._append_caption("系统", "已停止监听")
        self._append_ai("系统", "AI 分析已停止")

    # ------------------------------------------------------------------
    # 伪装模式
    # ------------------------------------------------------------------
    def _enter_stealth_mode(self):
        self.stealth_mode = True
        for w in [self.status_frame, self.caption_label, self.caption_frame,
                  self.ai_label, self.ai_frame, self.button_frame, self.input_frame]:
            w.pack_forget()
        self.stealth_frame.pack(fill=tk.BOTH, expand=True)
        self.root.configure(bg="#ffffff")
        self.root.deiconify()
        if hasattr(self, 'stealth_btn'):
            self.stealth_btn.config(text="✅ 退出伪装", bg="#dc2626")
            self.stealth_btn.bind("<Enter>", lambda e: self.stealth_btn.config(bg="#ef4444"))
            self.stealth_btn.bind("<Leave>", lambda e: self.stealth_btn.config(bg="#dc2626"))
        if self.web_server is not None:
            self.web_server._stealth_mode = True
        logger.info("[Stealth] 进入伪装模式")

    def _exit_stealth_mode(self):
        if not self.stealth_mode:
            return
        self.stealth_mode = False
        self.stealth_frame.pack_forget()
        self.root.configure(bg=BG_MAIN)
        self.status_frame.pack(fill=tk.X, padx=10, pady=(5, 2))
        self.caption_label.pack(fill=tk.X, padx=10, pady=(5, 2))
        self.caption_frame.pack(fill=tk.BOTH, expand=False, padx=10, pady=2)
        self.button_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=8)
        self.input_frame.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=(2, 0))
        self.ai_label.pack(fill=tk.X, padx=10, pady=(8, 2))
        self.ai_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=2)
        if hasattr(self, 'stealth_btn'):
            self.stealth_btn.config(text="🖥 伪装模式", bg=BTN_COLOR_MAP["stealth"][0])
            self.stealth_btn.bind("<Enter>", lambda e: self.stealth_btn.config(bg=BTN_COLOR_MAP["stealth"][1]))
            self.stealth_btn.bind("<Leave>", lambda e: self.stealth_btn.config(bg=BTN_COLOR_MAP["stealth"][0]))
        if self.web_server is not None:
            self.web_server._stealth_mode = False
        logger.info("[Stealth] 退出伪装模式")

    def _toggle_stealth_btn(self):
        if self.stealth_mode:
            self._exit_stealth_mode()
        else:
            self._enter_stealth_mode()

    def _toggle_stealth_mode(self, stealth_on):
        if stealth_on:
            self._enter_stealth_mode()
        else:
            self._exit_stealth_mode()

    def _start_web_server(self):
        """启动手机端 Web 建议面板"""
        if self.web_server is not None and self.web_server.is_alive():
            return
        try:
            self.web_server = SuggestionWebServer(pin=self.web_pin)
            self.web_server.on_stealth_toggle = lambda mode: self.root.after(
                0, lambda: self._toggle_stealth_mode(mode)
            )
            self.web_server.start()
            self.web_url_label.config(text="📱 正在获取地址...", fg="#38bdf8", cursor="hand2")

            def _fetch_ip():
                time.sleep(0.5)
                ip = SuggestionWebServer._get_local_ip()
                url = f"http://{ip}:{SuggestionWebServer.PORT}/?key={self.web_pin}"
                
                # 点击复制到系统剪贴板
                def _copy_url(event=None):
                    try:
                        self.root.clipboard_clear()
                        self.root.clipboard_append(url)
                        self.web_url_label.config(text="✓ 链接已复制！", fg="#34d399")
                        # 1.2 秒后复原显示
                        self.root.after(1200, lambda: self.web_url_label.config(text=f"📱 {url}", fg="#38bdf8"))
                    except Exception as err:
                        logger.error(f"复制链接失败: {err}")
                
                # 悬停超链接亮起反馈
                def _on_hover(event=None):
                    self.web_url_label.config(fg="#60a5fa")
                    
                def _on_leave(event=None):
                    current_txt = self.web_url_label.cget("text")
                    if "已复制" not in current_txt:
                        self.web_url_label.config(fg="#38bdf8")

                self.root.after(0, lambda: self.web_url_label.config(text=f"📱 {url}"))
                self.web_url_label.bind("<Button-1>", _copy_url)
                self.web_url_label.bind("<Enter>", _on_hover)
                self.web_url_label.bind("<Leave>", _on_leave)

            threading.Thread(target=_fetch_ip, daemon=True).start()
        except Exception as e:
            self.web_url_label.config(text=f"⚠ Web 启动失败", fg="#ef4444")
            logger.error(f"[Web] 启动失败: {e}")
            self.web_server = None

    def _stop_web_server(self):
        if self.web_server is not None:
            self.web_server.stop()
            self.web_server = None
        self.web_url_label.config(text="")
        self._exit_stealth_mode()

    def _clear_all_queues(self):
        while not self.audio_queue.empty():
            try:
                self.audio_queue.get_nowait()
            except Exception:
                break
        while not self.text_queue.empty():
            try:
                self.text_queue.get_nowait()
            except Exception:
                break
        while not self.ai_queue.empty():
            try:
                self.ai_queue.get_nowait()
            except Exception:
                break
        while not self.ai_response_queue.empty():
            try:
                self.ai_response_queue.get_nowait()
            except Exception:
                break
        if self.transcription_thread and self.transcription_thread.is_alive():
            self.transcription_thread.clear_state()
        logger.info("[队列] 所有队列已清空")

    def _toggle_ai(self, event=None):
        if event and hasattr(event, 'widget') and isinstance(event.widget, tk.Entry):
            return

        if not self.is_running:
            return

        is_currently_paused = self.ai_paused

        if is_currently_paused:
            self._clear_all_queues()
            if self.ai_thread and self.ai_thread.is_alive():
                self.ai_thread.resume()
            if self.audio_thread and self.audio_thread.is_alive():
                self.audio_thread.resume()
            self.ai_paused = False
            self.ai_status_label.config(text="● 收音中", fg="#34d399")
            self.ai_toggle_btn.config(text="⏸ 暂停收音 (空格)", bg=BTN_COLOR_MAP["primary"][0])
            self.ai_toggle_btn.bind("<Enter>", lambda e: self.ai_toggle_btn.config(bg=BTN_COLOR_MAP["primary"][1]))
            self.ai_toggle_btn.bind("<Leave>", lambda e: self.ai_toggle_btn.config(bg=BTN_COLOR_MAP["primary"][0]))
            self._append_ai("系统", "▶ 已恢复收音")
        else:
            if self.audio_thread and self.audio_thread.is_alive():
                self.audio_thread.pause()
            self._clear_all_queues()
            if self.ai_thread and self.ai_thread.is_alive():
                self.ai_thread.pause()
            self.ai_paused = True
            self.ai_status_label.config(text="⏸ 已暂停收音", fg="#d97706")
            self.ai_toggle_btn.config(text="▶ 恢复收音 (空格)", bg=BTN_COLOR_MAP["warning"][0])
            # 变更Hover至琥珀黄警告色系
            self.ai_toggle_btn.bind("<Enter>", lambda e: self.ai_toggle_btn.config(bg=BTN_COLOR_MAP["warning"][1]))
            self.ai_toggle_btn.bind("<Leave>", lambda e: self.ai_toggle_btn.config(bg=BTN_COLOR_MAP["warning"][0]))
            self._append_ai("系统", "⏸ 已暂停收音，说完后按空格恢复")

    def _set_detail_mode(self, event=None):
        if event and hasattr(event, 'widget') and isinstance(event.widget, tk.Entry):
            return

        key = event.keysym if event else ""
        mapping = {
            "0": ("short", "短"),
            "1": (None, "自动"),
            "2": ("long", "长"),
        }
        if key not in mapping:
            return

        level, label = mapping[key]
        self.detail_mode_var.set(f"[深度: {label}]")
        if self.ai_thread and self.ai_thread.is_alive():
            self.ai_thread.set_detail_override(level)
        else:
            logger.info(f"[AI] 回答深度: {label}（下次启动生效）")
        self._append_ai("系统", f"回答深度: {label}")

    def _clear_text(self):
        self.text_display.config(state=tk.NORMAL)
        self.text_display.delete(1.0, tk.END)
        self.text_display.config(state=tk.DISABLED)

        self.ai_display.config(state=tk.NORMAL)
        self.ai_display.delete(1.0, tk.END)
        self.ai_display.config(state=tk.DISABLED)

        if self.ai_thread and self.ai_thread.is_alive():
            self.ai_thread.clear_history()

    def _save_record(self):
        caption = self.text_display.get(1.0, tk.END).strip()
        ai = self.ai_display.get(1.0, tk.END).strip()
        content = f"=== 会议字幕 ===\n{caption}\n\n=== AI 建议回答 ===\n{ai}"
        if not content.strip():
            messagebox.showinfo("提示", "没有内容可保存")
            return

        filename = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            initialfile=f"面试记录_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        )
        if filename:
            try:
                with open(filename, 'w', encoding='utf-8') as f:
                    f.write(content)
                messagebox.showinfo("成功", f"记录已保存到:\n{filename}")
            except Exception as e:
                messagebox.showerror("错误", f"保存失败:\n{e}")

    def _append_caption(self, timestamp, text):
        self.text_display.config(state=tk.NORMAL)
        self.text_display.insert(tk.END, f"[{timestamp}] {text}\n")
        self.text_display.see(tk.END)
        self.text_display.config(state=tk.DISABLED)

    def _append_ai(self, timestamp, text):
        self.ai_display.config(state=tk.NORMAL)
        self.ai_display.insert(tk.END, f"[{timestamp}] {text}\n")
        self.ai_display.see(tk.END)
        self.ai_display.config(state=tk.DISABLED)

    def _update_text_display(self):
        # 硬件与转录核心健康监控守护
        if self.is_running:
            if self.audio_thread and not self.audio_thread.is_alive():
                err = getattr(self.audio_thread, "error_msg", None)
                if err:
                    self._stop_listening()
                    messagebox.showerror("音频捕获硬件错误", err)
                    return
            if self.transcription_thread and not self.transcription_thread.is_alive():
                self._stop_listening()
                messagebox.showerror("转写引擎异常", "转录线程已意外退出，请检查 CUDA 显存是否充足或设备输入频率！")
                return

        try:
            while True:
                timestamp, text = self.text_queue.get_nowait()
                self._append_caption(timestamp, text)
        except Exception:
            pass
        self.root.after(100, self._update_text_display)

    def _update_ai_display(self):
        try:
            while True:
                item = self.ai_response_queue.get_nowait()
                if len(item) == 4:
                    timestamp, question, answer, status = item
                else:
                    timestamp, question, answer = item
                    status = "complete"

                self.ai_display.config(state=tk.NORMAL)

                if status == "partial":
                    if '_streaming_pos' not in self.ai_display.mark_names():
                        self.ai_display.insert(tk.END, f"[{timestamp}] 面试官: {question}\n")
                        self.ai_display.mark_set('_streaming_pos', 'end-1c linestart')
                        self.ai_display.mark_gravity('_streaming_pos', 'left')

                    self.ai_display.delete('_streaming_pos', 'end-1c')
                    self.ai_display.insert('_streaming_pos', f"         💡 建议: {answer}")
                    self.ai_display.see(tk.END)
                    self.ai_display.config(state=tk.DISABLED)

                    if self.web_server is not None:
                        try:
                            self.web_server.update(question, answer, timestamp)
                        except Exception:
                            pass
                else:
                    if '_streaming_pos' in self.ai_display.mark_names():
                        self.ai_display.delete('_streaming_pos', 'end-1c')
                        self.ai_display.mark_unset('_streaming_pos')
                    else:
                        self.ai_display.insert(tk.END, f"[{timestamp}] 面试官: {question}\n")

                    self.ai_display.insert(tk.END, f"         💡 建议: {answer}\n\n")
                    self.ai_display.see(tk.END)
                    self.ai_display.config(state=tk.DISABLED)

                    if self.web_server is not None:
                        try:
                            self.web_server.update(question, answer, timestamp)
                        except Exception:
                            pass
        except queue.Empty:
            pass
        except Exception as e:
            logger.error(f"[UI] 显示更新异常: {e}")
        self.root.after(200, self._update_ai_display)

    def on_closing(self):
        if self.is_running:
            self._stop_listening()
        self.root.destroy()


# ============================================================================
# 主程序
# ============================================================================
def main():
    # 配置全局 logging 系统
    log_file = os.path.join(TOOL_DIR, "captioner_log.txt")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler()
        ]
    )

    logger.info("=" * 50)
    logger.info("会议字幕 + AI 面试辅助工具 (重构模块化 & 规范日志版)")
    logger.info("=" * 50)

    root = tk.Tk()
    app = CaptionerUI(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)

    logger.info("使用方法:")
    logger.info("1. 在 config.json 填入通义千问 API Key")
    logger.info("2. 在 knowledge_base/ 放入 .docx 知识库文件")
    logger.info("3. 点击 [开始监听]")
    logger.info("4. 面试官说话 → 字幕显示 + AI 建议回答")
    logger.info("=" * 50)

    root.mainloop()


if __name__ == "__main__":
    main()
