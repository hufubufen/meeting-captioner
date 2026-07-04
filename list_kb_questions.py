#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""打印 KB 中所有 Q&A 对的问题列表，用于设计测试用例"""

import os
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from docx import Document
from meeting_captioner import QwenAnalysisThread

kb_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "knowledge_base", "技术面试_QA整合版_钟燕鹏.docx")
doc = Document(kb_path)
paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
kb_text = "\n".join(paragraphs)

pairs = QwenAnalysisThread._parse_kb_qa_pairs(kb_text)
print(f"Total Q&A pairs: {len(pairs)}\n")
for i, (q, a) in enumerate(pairs, 1):
    print(f"{i:3d}. {q[:80]}")
    print(f"     A: {a[:60]}...")
    print()
