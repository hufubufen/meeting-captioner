"""
会议字幕工具 - 无终端启动入口
双击此文件即可运行，不会弹出黑色控制台窗口。
"""
import sys
import os

# 确保能导入同目录下的 meeting_captioner
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from meeting_captioner import main

if __name__ == "__main__":
    main()
