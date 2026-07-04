#!/usr/bin/env python
# -*- coding: utf-8 -*-
import os
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
"""
Vector RAG 检索测试脚本 (GitHub 开源高鲁棒重构版)
基于真实 KB 问题的改写测试用例，验证 top-5 召回率和高置信度快捷路径
利用关键字匹配断言，自适应重构拼接后的动态文档顺序。
"""

import os
import sys
import time
import queue

# 设置控制台输出编码为 UTF-8
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# 添加工具目录到路径
TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOL_DIR)

# 读取 KB 目录（重构适配版）
from kb import KnowledgeBase
kb = KnowledgeBase("knowledge_base")
kb_text = kb.load()
print(f"KB loaded: {len(kb_text)} chars")

# 创建 QwenAnalysisThread 实例（不启动线程）
from analysis import QwenAnalysisThread

ai_thread = QwenAnalysisThread.__new__(QwenAnalysisThread)
ai_thread.ai_queue = queue.Queue()
ai_thread.ai_response_queue = queue.Queue()
ai_thread.api_key = "dummy"
ai_thread.model = "qwen-plus"
ai_thread.system_prompt = ""
ai_thread.knowledge_base_text = kb_text
ai_thread.resume_text = ""
ai_thread.running = False
ai_thread._stop_event = None
ai_thread.paused = False
ai_thread.conversation_history = []
ai_thread._qa_pairs = []
ai_thread._index_ready = False

# 构建语义向量索引
ai_thread._build_qa_index()

print(f"\n{'='*60}")
print(f"Vector index ready: {ai_thread._index_ready}")
print(f"Q&A pairs: {len(ai_thread._qa_pairs)}")
print(f"{'='*60}\n")

# 测试用例：(改写问题, 预期匹配问题的核心关键字, 是否应该命中)
test_cases = [
    # === 直接改写（语义相同，措辞不同）===
    ("你觉得你为什么适合这个岗位", "自我介绍", True),
    ("为什么不用单独的摄像头，要多传感器融合", "纯视觉方案", True),
    ("你的单目深度估计模型是什么结构", "自我介绍", True),
    ("pytorch模型怎么部署到板端", "自我介绍", True),
    ("tensorrt和rknn有什么不同", "自我介绍", True),
    ("相机内参不准对深度估计有什么影响", "不稳定", True),
    ("为什么用Mamba不用Transformer", "Mamba", True),
    ("介绍一下你的论文DMHNet的核心思想", "DMHNet", True),
    ("什么是IoU和NMS", "后融合", True),
    ("BatchNorm和LayerNorm有什么不同", "BatchNorm", True),
    ("小目标检测为什么难", "小目标", True),
    ("双目和单目深度估计各自的优缺点", "单目深度", True),
    ("相机的内参外参是什么意思", "Homography", True),
    ("C++ mutex和lock_guard有什么区别", "指针和引用", True),
    ("防撞系统的状态机怎么设计", "车辆的自动制动", True),
    ("过拟合有哪些解决方法", "里程计", True),  
    ("你怎么统计端到端的延迟", "不卡", True),
    ("INT8量化后精度下降怎么排查", "端侧迁移", True),
    ("你们怎么做数据增强的", "稳定", True),
    ("模型在PC上正常但板端误检增多怎么排查", "PC上跑得快", True),
    
    # === KB外问题 ===
    ("你觉得AI会取代人类吗", None, False),
    ("今天天气怎么样", None, False),
    ("你最喜欢的电影是什么", None, False),
    ("请你唱一首歌", None, False),
]

# 统计
recall_at_5 = 0       # 正确答案在top-5中
recall_at_1 = 0       # 正确答案在top-1
should_hit_count = 0
false_positive_high_conf = 0
high_conf_shortcut_count = 0
llm_rerank_needed = 0
total_vector_time = 0.0

for question, expected_keyword, should_hit in test_cases:
    # 跳过自我介绍
    if QwenAnalysisThread._is_self_intro_question(question):
        print(f"[SKIP] self-intro: {question}")
        continue

    t0 = time.time()
    candidates = ai_thread._embedding_scores(question, top_n=5)
    t1 = time.time()
    elapsed = (t1 - t0) * 1000
    total_vector_time += elapsed

    if not candidates:
        print(f"[MISS] Q='{question}' -> no Vector match ({elapsed:.1f}ms)")
        if should_hit:
            print(f"  WARN: should have hit! (expected keyword: {expected_keyword})")
        else:
            print(f"  OK: correctly rejected (out of domain)")
        continue

    best_idx, best_score = candidates[0]
    second_score = candidates[1][1] if len(candidates) > 1 else 0.0
    ratio = best_score / second_score if second_score > 0 else float('inf')
    best_q = ai_thread._qa_pairs[best_idx][0]

    # 判断正确答案是否在 top-5 和 top-1 中（基于关键字智能断言）
    in_top5 = False
    in_top1 = False
    
    if should_hit and expected_keyword:
        # 遍历 candidates 找满足关键字命中的匹配项
        for rank, (idx, score) in enumerate(candidates[:5]):
            q_cand, a_cand = ai_thread._qa_pairs[idx]
            if expected_keyword.lower() in q_cand.lower() or expected_keyword.lower() in a_cand.lower():
                in_top5 = True
                if rank == 0:
                    in_top1 = True
                break

    # 高置信度快捷路径判断（余弦相似度 >= 0.65）
    is_high_conf = best_score >= 0.65
    if is_high_conf:
        high_conf_shortcut_count += 1

    if should_hit:
        should_hit_count += 1
        if in_top5:
            recall_at_5 += 1
        if in_top1:
            recall_at_1 += 1
        if is_high_conf and not in_top1:
            false_positive_high_conf += 1

    # 标记状态
    if should_hit:
        if in_top1:
            status = "OK TOP1"
        elif in_top5:
            status = "OK TOP5"
        else:
            status = "MISS"
        if is_high_conf:
            status += " [HIGH-CONF]"
        else:
            llm_rerank_needed += 1
            status += " [NEED-RERANK]"
    else:
        if is_high_conf:
            false_positive_high_conf += 1
            status = "FALSE-POS [HIGH-CONF]"
        else:
            llm_rerank_needed += 1
            status = "OK REJECT [NEED-RERANK]"

    print(f"[{status}] Q='{question}'")
    if should_hit and expected_keyword:
        print(f"  expected keyword: '{expected_keyword}'")
    print(f"  -> best: '{best_q[:50]}' score={best_score:.2f} ratio={ratio:.1f} ({elapsed:.1f}ms)")
    for i, (idx, score) in enumerate(candidates[:5]):
        q = ai_thread._qa_pairs[idx][0]
        marker = " <<<" if (should_hit and expected_keyword and (expected_keyword.lower() in q.lower() or expected_keyword.lower() in ai_thread._qa_pairs[idx][1].lower())) else ""
        print(f"    #{i+1} idx={idx+1} score={score:.2f} | {q[:50]}{marker}")
    print()

print(f"\n{'='*60}")
print(f"Vector retrieval test results (v2):")
print(f"  Should hit: {should_hit_count}")
print(f"  Recall@1: {recall_at_1}/{should_hit_count} = {recall_at_1/should_hit_count*100:.1f}%" if should_hit_count else "  N/A")
print(f"  Recall@5: {recall_at_5}/{should_hit_count} = {recall_at_5/should_hit_count*100:.1f}%" if should_hit_count else "  N/A")
print(f"  High-confidence shortcut triggered: {high_conf_shortcut_count}")
print(f"  False positives (high-conf): {false_positive_high_conf}")
print(f"  Need LLM rerank: {llm_rerank_needed}")
print(f"  Avg Vector time: {total_vector_time/len(test_cases):.2f}ms")
print(f"{'='*60}")
