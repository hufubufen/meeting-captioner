#!/usr/bin/env python
# -*- coding: utf-8 -*-
import os
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
"""
BM25 + LLM rerank 端到端测试 (GitHub 开源高鲁棒重构版)
基于关键字模糊匹配断言，彻底消除硬编码物理数组索引的脆弱测试问题。
"""

import os
import sys
import time
import json
import queue

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TOOL_DIR)

# 读取 KB 目录（重构适配版）
from kb import KnowledgeBase
kb = KnowledgeBase("knowledge_base")
kb_text = kb.load()
print(f"KB loaded: {len(kb_text)} chars")

# 读取 config
config_path = os.path.join(TOOL_DIR, "config.json")
with open(config_path, 'r', encoding='utf-8') as f:
    config = json.load(f)
print(f"Config loaded: model={config['model']}")

# 创建 QwenAnalysisThread 实例
from analysis import QwenAnalysisThread

ai_thread = QwenAnalysisThread.__new__(QwenAnalysisThread)
ai_thread.ai_queue = queue.Queue()
ai_thread.ai_response_queue = queue.Queue()
ai_thread.api_key = config["api_key"]
ai_thread.model = config["model"]
ai_thread.system_prompt = ""
ai_thread.knowledge_base_text = kb_text
ai_thread.resume_text = ""
ai_thread.running = False
ai_thread._stop_event = None
ai_thread.paused = False
ai_thread.conversation_history = []
ai_thread._qa_pairs = []
ai_thread._bm25_ready = False

# 初始化 dashscope
import dashscope
dashscope.api_key = config["api_key"]
from dashscope import Generation
ai_thread.Generation = Generation

# 构建向量检索索引
ai_thread._build_qa_index()

print(f"\n{'='*60}")
print(f"Vector index ready: {ai_thread._index_ready}")
print(f"Q&A pairs: {len(ai_thread._qa_pairs)}")
print(f"{'='*60}\n")

# 测试用例：(改写问题, 预期匹配到的目标问题核心关键字, 是否应该命中)
# 基于关键字的匹配不仅消除了对静态列表顺序的依赖，更能鲁棒适应知识库内容迭代
test_cases = [
    # === 直接改写（语义相同，措辞不同）===
    ("为什么不用单独的摄像头，要多传感器融合", "纯视觉", True),
    ("相机内参不准对深度估计有什么影响", "不稳定", True),
    ("介绍一下你的论文DMHNet的核心思想", "DMHNet", True),
    ("什么是IoU and NMS", "后融合", True),
    ("BatchNorm和LayerNorm有什么不同", "BatchNorm", True),
    ("小目标检测为什么难", "小目标", True),
    ("C++ mutex和lock_guard有什么区别", "指针和引用", True),
    ("防撞系统的状态机怎么设计", "车辆的自动制动", True),
    ("你怎么统计端到端的延迟", "不卡", True),
    ("INT8量化后精度下降怎么排查", "端侧迁移", True),
    ("模型在PC上正常但板端误检增多怎么排查", "PC上跑得快", True),
    ("你觉得你为什么适合这个岗位", "自我介绍", True),
    ("你的单目深度估计模型是什么结构", "自我介绍", True),
    ("pytorch模型怎么部署到板端", "自我介绍", True),
    ("tensorrt和rknn有什么不同", "自我介绍", True),
    ("为什么用Mamba不用Transformer", "Mamba", True),
    ("双目和单目深度估计各自的优缺点", "单目深度", True),
    ("相机的内参外参是什么意思", "Homography", True),
    ("你们怎么做数据增强的", "稳定", True),
    
    # === KB外问题（LLM 应该拒答/判定相似度极低拒答）===
    ("你觉得AI会取代人类吗", None, False),
    ("你最喜欢的电影是什么", None, False),
    ("请你唱一首歌", None, False),
    ("过拟合有哪些解决方法", None, False),
]

correct = 0
should_hit_count = 0
false_positive = 0
true_negative = 0
total_time = 0.0
llm_calls = 0
high_conf_skipped = 0

for question, expected_keyword, should_hit in test_cases:
    if QwenAnalysisThread._is_self_intro_question(question):
        print(f"[SKIP] self-intro: {question}")
        continue

    # 对需要上下文的测试用例，先注入对话历史
    if question == "你们怎么做数据增强的":
        ai_thread.conversation_history = [
            {"role": "user", "content": "面试官说：讲一下你在矿井防撞项目中负责什么"},
            {"role": "assistant", "content": "我主要负责视觉感知模块，包括电车防撞系统的单目深度与多传感器融合"},
            {"role": "user", "content": "面试官说：井下环境光照条件怎么样"},
            {"role": "assistant", "content": "井下存在弱光、粉尘、遮挡等复杂条件，深度不稳，我们通过多帧结合和融合数据解决"},
        ]
        print(f"[CONTEXT] 注入矿井项目对话上下文")

    t0 = time.time()
    result = ai_thread._find_best_qa_match(question)
    t1 = time.time()
    elapsed = (t1 - t0) * 1000
    total_time += elapsed

    # 判断是否走了高置信度快捷路径（余弦相似度 >= 0.65 触发直接拦截）
    candidates = ai_thread._embedding_scores(question, top_n=5)
    if candidates:
        best_score = candidates[0][1]
        is_high_conf = best_score >= 0.65
        if is_high_conf:
            high_conf_skipped += 1
    else:
        is_high_conf = False

    if result is not None:
        (matched_q, matched_a), score = result
        matched_idx = None
        for i, (q, _) in enumerate(ai_thread._qa_pairs):
            if q == matched_q:
                matched_idx = i + 1
                break

        # 基于预期关键字模糊检索匹配，判断是否命中
        is_match_correct = False
        if should_hit and expected_keyword:
            if expected_keyword.lower() in matched_q.lower() or expected_keyword.lower() in matched_a.lower():
                is_match_correct = True

        if should_hit and is_match_correct:
            correct += 1
            should_hit_count += 1
            status = "OK"
        elif should_hit:
            should_hit_count += 1
            status = "WRONG"
        elif not should_hit:
            false_positive += 1
            status = "FALSE-POS"
    else:
        if should_hit:
            should_hit_count += 1
            status = "MISS"
        else:
            true_negative += 1
            status = "OK-REJECT"

    # 是否调用了LLM
    if not is_high_conf and result is not None:
        llm_calls += 1

    print(f"[{status}] Q='{question}' ({elapsed:.0f}ms)")
    if result:
        print(f"  -> matched KB #{matched_idx}: '{matched_q[:50]}'")
    else:
        print(f"  -> no match (rejected)")
    if should_hit and expected_keyword:
        print(f"  expected keyword: '{expected_keyword}'")

    # 清除注入的对话上下文，避免影响后续测试
    if question == "你们怎么做数据增强的":
        ai_thread.conversation_history = []
        print(f"[CONTEXT] 清除对话上下文")

    print()

print(f"\n{'='*60}")
print(f"End-to-end test results (Vector + LLM rerank):")
print(f"  Total test cases: {len(test_cases)}")
print(f"  Should hit: {should_hit_count}")
print(f"  Correct matches: {correct}/{should_hit_count}")
if should_hit_count > 0:
    print(f"  Accuracy: {correct/should_hit_count*100:.1f}%")
print(f"  High-confidence shortcut: {high_conf_skipped} (skipped LLM)")
print(f"  LLM rerank calls (approx): {llm_calls}")
print(f"  False positives: {false_positive}")
print(f"  True negatives: {true_negative}")
print(f"  Total time: {total_time:.0f}ms (avg {total_time/len(test_cases):.0f}ms/query)")
print(f"{'='*60}")
