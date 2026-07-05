#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""打印知识库中所有的 Q&A 问答对的问题列表"""

import os
import sys

# 强制启用 UTF-8 编码避免 Windows CMD 打印乱码
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from knowledge_base import KnowledgeBase
from analysis import QwenAnalysisThread

def main():
    kb = KnowledgeBase("knowledge_base")
    kb_text = kb.load()
    if not kb_text:
        print("知识库为空，请先将您的面试题库文件放入 knowledge_base 目录下。")
        return

    pairs = QwenAnalysisThread._parse_kb_qa_pairs(kb_text)
    print(f"检测到知识库总 Q&A 问答对数量: {len(pairs)}\n")
    for i, (q, a) in enumerate(pairs, 1):
        print(f"{i:3d}. {q[:80]}")
        print(f"     A: {a[:60]}...")
        print()

if __name__ == "__main__":
    main()
