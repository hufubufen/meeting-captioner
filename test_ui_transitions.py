#!/usr/bin/env python
# -*- coding: utf-8 -*-
import os
import sys
import unittest
from unittest.mock import MagicMock, patch
import tkinter as tk
from tkinter import messagebox

# 添加项目目录到路径
TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOL_DIR)

# 导入 UI 模块及配色常数
import meeting_captioner
from meeting_captioner import CaptionerUI, BTN_COLOR_MAP, BG_MAIN

class TestUITransitions(unittest.TestCase):
    """会议字幕与 AI 辅助工具 UI 功能转换状态机测试"""

    def setUp(self):
        self.root = tk.Tk()
        # 避免弹出真实图标设置的系统报错
        meeting_captioner.logger = MagicMock()
        
        # 显式启动 Mock，防止真实拉起硬件采集和模型推理
        self.patcher_kb = patch('meeting_captioner.KnowledgeBase')
        self.patcher_audio = patch('meeting_captioner.AudioCaptureThread')
        self.patcher_trans = patch('meeting_captioner.TranscriptionThread')
        self.patcher_ai = patch('meeting_captioner.QwenAnalysisThread')
        self.patcher_web = patch('meeting_captioner.SuggestionWebServer')
        
        self.patcher_showwarning = patch('tkinter.messagebox.showwarning')
        self.patcher_showinfo = patch('tkinter.messagebox.showinfo')
        self.patcher_showerror = patch('tkinter.messagebox.showerror')

        self.mock_kb = self.patcher_kb.start()
        self.mock_audio = self.patcher_audio.start()
        self.mock_trans = self.patcher_trans.start()
        self.mock_ai = self.patcher_ai.start()
        self.mock_web = self.patcher_web.start()
        
        self.mock_showwarning = self.patcher_showwarning.start()
        self.mock_showinfo = self.patcher_showinfo.start()
        self.mock_showerror = self.patcher_showerror.start()
            
        # 设置 Mock 默认行为
        self.mock_kb.return_value.load.return_value = "测试知识库"
        
        # 让 AI 线程的 is_alive 默认返回 True
        self.mock_ai_instance = MagicMock()
        self.mock_ai_instance.is_alive.return_value = True
        self.mock_ai.return_value = self.mock_ai_instance

        # 实例化 UI
        self.app = CaptionerUI(self.root)

    def tearDown(self):
        # 停止所有 Mock patchers
        self.patcher_kb.stop()
        self.patcher_audio.stop()
        self.patcher_trans.stop()
        self.patcher_ai.stop()
        self.patcher_web.stop()
        self.patcher_showwarning.stop()
        self.patcher_showinfo.stop()
        self.patcher_showerror.stop()
        self.root.destroy()

    def test_initial_state(self):
        """测试 1: 验证界面初始化状态"""
        self.assertFalse(self.app.is_running)
        self.assertFalse(self.app.ai_paused)
        self.assertFalse(self.app.mic_mode.get())
        
        # 验证初始按钮状态
        self.assertEqual(self.app.start_btn['state'], tk.NORMAL)
        self.assertEqual(self.app.ai_toggle_btn['state'], tk.DISABLED)
        self.assertEqual(self.app.stop_btn['state'], tk.DISABLED)
        self.assertEqual(self.app.mic_toggle_btn['state'], tk.NORMAL)

    def test_text_mode_start_stop_transitions(self):
        """测试 2: 验证文本测试模式下的状态切换流 (开始 -> 暂停 -> 恢复 -> 停止)"""
        # 1. 勾选仅文本测试
        self.app.text_only_mode.set(True)
        self.app.config = {"api_key": "dummy_key"} # 提供 dummy key 绕过检查
        
        # 2. 点击开始监听
        self.app._start_listening()
        
        self.assertTrue(self.app.is_running)
        self.assertEqual(self.app.status_label['text'], "● 文本测试中...")
        
        # 验证按钮激活状态转变
        self.assertEqual(self.app.start_btn['state'], tk.DISABLED)
        self.assertEqual(self.app.ai_toggle_btn['state'], tk.NORMAL)
        self.assertEqual(self.app.stop_btn['state'], tk.NORMAL)
        
        # 3. 模拟空格暂停
        self.app._toggle_ai()
        self.assertTrue(self.app.ai_paused)
        self.assertEqual(self.app.ai_status_label['text'], "⏸ 已暂停收音")
        self.assertEqual(self.app.ai_toggle_btn['text'], "▶ 恢复收音 (空格)")
        
        # 4. 模拟空格恢复
        self.app._toggle_ai()
        self.assertFalse(self.app.ai_paused)
        self.assertEqual(self.app.ai_status_label['text'], "● 收音中")
        
        # 5. 模拟停止面试
        self.app._stop_listening()
        self.assertFalse(self.app.is_running)
        self.assertEqual(self.app.status_label['text'], "● 已停止")
        
        # 验证状态重置
        self.assertEqual(self.app.start_btn['state'], tk.NORMAL)
        self.assertEqual(self.app.ai_toggle_btn['state'], tk.DISABLED)
        self.assertEqual(self.app.stop_btn['state'], tk.DISABLED)

    def test_mic_mode_interlock_transitions(self):
        """测试 3: 验证麦克风模式切换及其互锁保护限制 (非运行中可切换，运行中拦截修改)"""
        # 1. 点击切换到麦克风模式
        self.app._toggle_mic_mode()
        self.assertTrue(self.app.mic_mode.get())
        self.assertEqual(self.app.mic_toggle_btn['text'], "📱 麦克风模式")
        
        # 2. 模拟启动
        self.app.config = {"api_key": "dummy_key"}
        self.app._start_listening()
        self.assertTrue(self.app.is_running)
        
        # 3. 运行中尝试再次点击切换模式 (应被警告拦截，模式不改变)
        self.app._toggle_mic_mode()
        self.assertTrue(self.app.mic_mode.get()) # 模式应保持不变
        self.mock_showwarning.assert_called_with("提示", "请先停止当前面试再切换音频模式")
        
        # 4. 停止面试，模式应可以被允许切换回系统音频
        self.app._stop_listening()
        self.app._toggle_mic_mode()
        self.assertFalse(self.app.mic_mode.get())
        self.assertEqual(self.app.mic_toggle_btn['text'], "🎤 系统音频")

    def test_clear_text_transitions(self):
        """测试 4: 验证文本清空逻辑与文本域的状态更新"""
        self.app.text_display.config(state=tk.NORMAL)
        self.app.text_display.insert(tk.END, "对方说：Hello")
        self.app.text_display.config(state=tk.DISABLED)
        
        self.app.ai_display.config(state=tk.NORMAL)
        self.app.ai_display.insert(tk.END, "AI建议：Hi")
        self.app.ai_display.config(state=tk.DISABLED)
        
        # 点击清空
        self.app._clear_text()
        
        # 验证文本均已清空
        self.assertEqual(self.app.text_display.get(1.0, tk.END).strip(), "")
        self.assertEqual(self.app.ai_display.get(1.0, tk.END).strip(), "")


if __name__ == "__main__":
    unittest.main()
