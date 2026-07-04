#!/usr/bin/env python
# -*- coding: utf-8 -*-
import tkinter as tk

class SettingsDialog(tk.Toplevel):
    """API 配置对话框：base_url / api_key / model / rerank_model"""

    def __init__(self, parent, config, on_save=None, is_first_run=False):
        super().__init__(parent)
        self.title("API 设置 (首次启动引导)" if is_first_run else "API 设置")
        self.resizable(False, False)
        self.config_data = dict(config)
        self.on_save = on_save
        self.result = None

        # 居中显示
        self.transient(parent)
        self.grab_set()

        frame = tk.Frame(self, bg="#1e1e2e", padx=20, pady=15)
        frame.pack(fill=tk.BOTH, expand=True)

        style = {"bg": "#1e1e2e", "fg": "#c9d1d9", "insertbackground": "#c9d1d9",
                 "font": ("Microsoft YaHei UI", 10)}

        # 如果是首次启动，展示黄金色醒目配置引导语
        if is_first_run:
            first_run_label = tk.Label(
                frame, 
                text="🔔 检测到您是首次运行项目，请配置大模型 API Key 以便启动！",
                bg="#1e1e2e", fg="#f0ad4e", 
                font=("Microsoft YaHei UI", 9, "bold"),
                anchor="w"
            )
            first_run_label.pack(fill=tk.X, pady=(0, 10))

        # Base URL
        tk.Label(frame, text="API 地址 (Base URL):", **{k: v for k, v in style.items() if k in ("bg", "fg", "font")}, anchor="w").pack(fill=tk.X, pady=(0, 2))
        self.base_url_var = tk.StringVar(value=config.get("base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1"))
        tk.Entry(frame, textvariable=self.base_url_var, width=60, **style).pack(fill=tk.X, pady=(0, 10))

        # API Key
        tk.Label(frame, text="API Key:", **{k: v for k, v in style.items() if k in ("bg", "fg", "font")}, anchor="w").pack(fill=tk.X, pady=(0, 2))
        self.api_key_var = tk.StringVar(value=config.get("api_key", ""))
        tk.Entry(frame, textvariable=self.api_key_var, width=60, show="*", **style).pack(fill=tk.X, pady=(0, 10))

        # Model
        tk.Label(frame, text="模型名称:", **{k: v for k, v in style.items() if k in ("bg", "fg", "font")}, anchor="w").pack(fill=tk.X, pady=(0, 2))
        self.model_var = tk.StringVar(value=config.get("model", "qwen-flash"))
        tk.Entry(frame, textvariable=self.model_var, width=60, **style).pack(fill=tk.X, pady=(0, 10))

        # Rerank Model
        tk.Label(frame, text="Rerank 模型 (可选，留空则用同一模型):", **{k: v for k, v in style.items() if k in ("bg", "fg", "font")}, anchor="w").pack(fill=tk.X, pady=(0, 2))
        self.rerank_model_var = tk.StringVar(value=config.get("rerank_model", ""))
        tk.Entry(frame, textvariable=self.rerank_model_var, width=60, **style).pack(fill=tk.X, pady=(0, 10))

        # Web Connection PIN
        tk.Label(frame, text="物理防窥连接密码 (可选，留空则每次冷启动时随机生成):", **{k: v for k, v in style.items() if k in ("bg", "fg", "font")}, anchor="w").pack(fill=tk.X, pady=(0, 2))
        self.web_pin_var = tk.StringVar(value=config.get("web_pin", ""))
        tk.Entry(frame, textvariable=self.web_pin_var, width=60, **style).pack(fill=tk.X, pady=(0, 10))

        # 测试连接按钮
        self.test_status = tk.Label(frame, text="", bg="#1e1e2e", fg="#888888", font=("Microsoft YaHei UI", 9))
        self.test_status.pack(fill=tk.X, pady=(0, 5))

        btn_frame = tk.Frame(frame, bg="#1e1e2e")
        btn_frame.pack(fill=tk.X, pady=(5, 0))

        tk.Button(btn_frame, text="测试连接", command=self._test_connection,
                  bg="#2d7d46", fg="white", font=("Microsoft YaHei UI", 9),
                  relief=tk.FLAT, padx=12).pack(side=tk.LEFT, padx=(0, 5))
        tk.Button(btn_frame, text="取消", command=self.destroy,
                  bg="#555555", fg="white", font=("Microsoft YaHei UI", 9),
                  relief=tk.FLAT, padx=12).pack(side=tk.RIGHT, padx=(5, 0))
        tk.Button(btn_frame, text="保存", command=self._save,
                  bg="#2d7d46", fg="white", font=("Microsoft YaHei UI", 9),
                  relief=tk.FLAT, padx=12).pack(side=tk.RIGHT)

        # 快捷键
        self.bind("<Escape>", lambda e: self.destroy())
        self.bind("<Return>", lambda e: self._save())

        # 居中
        self.update_idletasks()
        x = parent.winfo_x() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")

    def _test_connection(self):
        """测试 API 连接"""
        self.test_status.config(text="正在测试...", fg="#f0ad4e")
        self.update()
        try:
            from openai import OpenAI
            client = OpenAI(
                base_url=self.base_url_var.get().strip(),
                api_key=self.api_key_var.get().strip(),
            )
            models = client.models.list()
            model_count = len(models.data) if hasattr(models, 'data') else 0
            self.test_status.config(text=f"连接成功！可用模型: {model_count} 个", fg="#27ae60")
        except Exception as e:
            self.test_status.config(text=f"连接失败: {e}", fg="#e74c3c")

    def _save(self):
        """保存设置"""
        self.result = {
            "base_url": self.base_url_var.get().strip(),
            "api_key": self.api_key_var.get().strip(),
            "model": self.model_var.get().strip(),
            "rerank_model": self.rerank_model_var.get().strip(),
            "web_pin": self.web_pin_var.get().strip(),
        }
        if self.on_save:
            self.on_save(self.result)
        self.destroy()
