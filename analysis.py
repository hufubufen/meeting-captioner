#!/usr/bin/env python
# -*- coding: utf-8 -*-
import os
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
import threading
import time
import json
import queue
from datetime import datetime
import logging

logger = logging.getLogger("captioner")
TOOL_DIR = os.path.dirname(os.path.abspath(__file__))

class AIAnalysisThread(threading.Thread):
    """从队列获取转录文字，调用通义千问 API 生成建议回答"""

    # 全局模型与 Tokenizer 缓存类变量，避免多次启停导致重复加载
    _shared_embed_model = None
    _shared_embed_tokenizer = None
    _shared_embed_device = None
    
    _shared_reranker_model = None
    _shared_reranker_tokenizer = None
    _shared_reranker_device = None
    
    _model_lock = threading.Lock()

    def __init__(self, ai_queue, ai_response_queue, api_key, model, system_prompt, knowledge_base_text, resume_text="", max_tokens=500, rerank_model=None, base_url=None):
        super().__init__(daemon=True)
        self.ai_queue = ai_queue
        self.ai_response_queue = ai_response_queue
        self.api_key = api_key
        self.base_url = base_url or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        self.model = model
        self.rerank_model = rerank_model or model  # 默认复用主模型，可单独指定更快模型
        self.system_prompt = system_prompt
        self.knowledge_base_text = knowledge_base_text
        self.resume_text = resume_text
        self.default_max_tokens = max_tokens
        self.running = False
        self._stop_event = threading.Event()
        self.paused = False
        # 对话历史，保留最近 N 轮，用于理解不完整的追问
        self.conversation_history = []  # [{"role": "user"/"assistant", "content": "..."}]
        # 手动回答深度覆盖: None=自动分类, 'short'/'medium'/'long'=手动指定
        self.detail_override = None
        self._history_lock = threading.Lock()
        self._last_candidates = None

        # === RAG: 语义向量检索（text2vec-base-chinese）+ BGE Reranker ===
        self._qa_pairs = []           # [(question, answer), ...]
        self._qa_intents = {}         # {question_text: intent_tag} (向后兼容的意图字典)
        self._index_ready = False     # 向量索引是否就绪

        # 模型异步加载（在 run() 中执行，不卡 UI）
        self._models_loaded = False
        self._reranker_model = None
        self._reranker_tokenizer = None
        self._reranker_device = None

    # ------------------------------------------------------------------
    # 自定义触发问题生成 (用于语义分流直连)
    # ------------------------------------------------------------------
    @classmethod
    def _get_trigger_questions(cls):
        """返回预置的语义分流触发问题及标签"""
        triggers = []
        
        # 1. 自我介绍
        triggers.extend([
            ("请做一个自我介绍", "self_intro"),
            ("简单介绍下你自己", "self_intro"),
            ("说一下你的个人基本情况", "self_intro"),
            ("讲讲你自己的经历", "self_intro"),
            ("简单介绍一下你的教育背景和工作经历", "self_intro"),
            ("自我介绍一下", "self_intro"),
        ])
        
        # 2. 四大项目介绍
        triggers.extend([
            ("介绍一下第一个项目矿井防撞系统", "project_intro_1"),
            ("讲讲你的矿井防撞项目", "project_intro_1"),
            ("说说那个无轨胶轮车防撞系统的项目", "project_intro_1"),
            ("第一个项目做了什么怎么做的", "project_intro_1"),
            
            ("介绍一下第二个项目煤矸识别", "project_intro_2"),
            ("讲讲你的煤矸识别放煤项目", "project_intro_2"),
            ("说说双模态放煤声音振动识别项目", "project_intro_2"),
            ("第二个项目做了什么怎么做的", "project_intro_2"),
            
            ("介绍一下第三个项目皮带撕裂异物检测", "project_intro_3"),
            ("讲讲你的皮带机跑偏撕裂检测项目", "project_intro_3"),
            ("说说皮带异物撕裂检测那个项目", "project_intro_3"),
            
            ("介绍一下第四个项目农田路径植株识别", "project_intro_4"),
            ("讲讲你做过的农田路径识别项目", "project_intro_4"),
            ("说说第四个农业行间路径识别项目", "project_intro_4"),
        ])

        # 3. 各项目难点
        triggers.extend([
            ("第一个项目遇到了什么困难和挑战", "project_difficulty_1"),
            ("你觉得防撞项目里有什么技术难点", "project_difficulty_1"),
            ("第一个项目最大的痛点是什么", "project_difficulty_1"),
            
            ("第二个项目遇到了什么困难和挑战", "project_difficulty_2"),
            ("煤矸识别项目有什么技术难点", "project_difficulty_2"),
            ("第二个项目最大的痛点是什么", "project_difficulty_2"),
            
            ("第三个项目遇到了什么困难和挑战", "project_difficulty_3"),
            ("皮带机检测项目有什么技术难点", "project_difficulty_3"),
            
            ("第四个项目遇到了什么困难和挑战", "project_difficulty_4"),
            ("农业作物路径项目有什么技术难点", "project_difficulty_4"),
        ])

        # 4. 各项目创新点
        triggers.extend([
            ("第一个项目有什么创新和亮点", "project_innovation_1"),
            ("这个无轨防撞系统有什么新颖的地方吗", "project_innovation_1"),
            
            ("第二个项目有什么创新和亮点", "project_innovation_2"),
            ("煤矸识别系统有什么新颖的地方吗", "project_innovation_2"),
            
            ("第三个项目有什么创新和亮点", "project_innovation_3"),
            
            ("第四个项目有什么创新和亮点", "project_innovation_4"),
        ])

        # 5. 反问面试官
        triggers.extend([
            ("你有什么想问我的吗", "ask_interviewer"),
            ("你有什么问题要问我", "ask_interviewer"),
            ("你有什么要问我的吗", "ask_interviewer"),
            ("反问我几个问题", "ask_interviewer"),
        ])

        return triggers

    # ------------------------------------------------------------------
    # 问题意图分级：根据面试官措辞判断回答深度
    # ------------------------------------------------------------------
    _SHORT_KEYWORDS = ['简单', '简短', '简要', '简答', '简答', '一句话', '概括', '大致']
    _LONG_KEYWORDS = ['详细', '展开', '深入', '具体', '仔细', '好好', '系统', '全面', '透彻']

    @classmethod
    def _classify_detail_level(cls, question):
        import re
        q_clean = question.strip()

        for kw in cls._LONG_KEYWORDS:
            if kw in q_clean:
                if any(sk in q_clean for sk in cls._SHORT_KEYWORDS):
                    continue
                return 'long'

        for kw in cls._SHORT_KEYWORDS:
            if kw in q_clean:
                return 'short'

        if re.search(r'知不知道|(?:听说|听过).*吗|了解.*吗|懂不懂', q_clean):
            return 'short'
        if re.search(r'能不能|可以.*吗|对不对|是不是|是吗', q_clean):
            return 'short'

        if len(q_clean) < 8:
            return 'short'

        return 'medium'

    @classmethod
    def _detail_tokens(cls, level, default_max=500):
        return {'short': 800, 'medium': 1500, 'long': 3000}.get(level, default_max)

    @classmethod
    def _detail_prompt(cls, level):
        prompts = {
            'short': '请用2-3句话简要回答，直击要点，不要展开细节。',
            'medium': '请用5-6句话回答，条理清晰，适度展开。',
            'long': '请详细回答，8句话以上，充分展开细节和原理。',
        }
        return prompts.get(level, prompts['medium'])

    # ------------------------------------------------------------------
    # 免 LLM 自我介绍/项目/难点/创新点/反问提取逻辑 (完全还原)
    # ------------------------------------------------------------------
    _INTRO_PATTERNS = [
        r'自我介绍',
        r'介绍.{0,3}(?:自己|一下.{0,2}你自己|你自己)',
        r'(?:说说|谈谈|讲一下|讲下).{0,3}你.*自己',
    ]
    _DURATION_MAP = {
        '5min': ['五分钟', '5分钟', '5min', '详细', '展开'],
        '1min': ['一分钟', '1分钟', '1min', '简短', '简单', '简要'],
        '3min': ['三分钟', '3分钟', '3min'],
    }
    _PROJECT_KEYWORDS = {
        1: ['防撞', '无轨', '360', '深度估计', '单目深度', 'RK3588', 'Jetson', '车辆', 'UWB', '毫米波'],
        2: ['煤矸', '放煤', '声音', '振动', 'STFT', 'Mamba', '双模态'],
        3: ['皮带', '皮带机', '跑偏', '撕裂', '异物', '卡带'],
        4: ['农田', '农业', '作物', '行间', '滴灌', '植株', '路径识别'],
    }
    _PROJECT_NUM_CN = {1: '一', 2: '二', 3: '三', 4: '四'}

    @classmethod
    def _is_self_intro_question(cls, question):
        import re
        for pat in cls._INTRO_PATTERNS:
            if re.search(pat, question):
                return True
        return False

    @classmethod
    def _intro_duration(cls, question):
        for dur, keywords in cls._DURATION_MAP.items():
            for kw in keywords:
                if kw in question:
                    return dur
        return '3min'

    @classmethod
    def _extract_intro_from_kb(cls, kb_text, duration='3min'):
        import re
        marker_patterns = {
            '1min': [r'1min版本', r'[一二三四五]、\s*1分钟\S*版本', r'1分钟\S*版本'],
            '3min': [r'3min版本', r'[一二三四五]、\s*3分钟\S*版本', r'3分钟\S*版本'],
            '5min': [r'5min版本', r'[一二三四五]、\s*5分钟\S*版本', r'5分钟\S*版本'],
        }
        patterns = marker_patterns.get(duration, marker_patterns['3min'])

        idx = -1
        matched_len = 0
        for pat in patterns:
            m = re.search(pat, kb_text)
            if m:
                idx = m.start()
                matched_len = len(m.group())
                break

        if idx < 0:
            return None

        tail = kb_text[idx + matched_len:]
        m = re.search(r'(?:A：)?老师您好[^\n]*', tail)
        if not m:
            return None

        content = tail[m.start():]
        end_m = re.search(
            r'\n(?:[一二三四五]?min版本|[一二三四五]、\s*\d分钟\S*版本|\d分钟\S*版本|Q[：:]|提示[：：]|总结|[一二三四五六七八九十]+[、，,])',
            content
        )
        if end_m:
            content = content[:end_m.start()]

        lines = []
        for line in content.strip().split('\n'):
            line = line.strip()
            if line.startswith('A：'):
                line = line[2:]
            if line:
                lines.append(line)
        return '\n'.join(lines).strip()

    @classmethod
    def _identify_project(cls, question):
        best_proj = None
        best_score = 0
        for proj_id, keywords in cls._PROJECT_KEYWORDS.items():
            score = sum(len(kw) for kw in keywords if kw in question)
            if score > best_score:
                best_score = score
                best_proj = proj_id
        if best_proj:
            return best_proj
        import re
        m = re.search(r'第([一二三四1234])个?', question)
        if m:
            num = m.group(1)
            num_map = {'一': 1, '二': 2, '三': 3, '四': 4, '1': 1, '2': 2, '3': 3, '4': 4}
            if num in num_map:
                return num_map[num]
        return None

    @classmethod
    def _extract_project_intro_from_kb(cls, kb_text, question, forced_proj_id=None):
        import re
        ask_all = any(kw in question for kw in ['四个', '所有', '哪些', '哪几个'])
        proj_id = forced_proj_id or cls._identify_project(question)

        if ask_all or proj_id is None:
            m = re.search(r'###\s*总体介绍话术', kb_text)
            if m:
                start = m.end()
                tail = kb_text[start:]
                end_m = re.search(r'\n(?:---|\#{1,2}\s)', tail)
                content = tail[:end_m.start()] if end_m else tail
                lines = []
                for line in content.strip().split('\n'):
                    line = line.strip()
                    if line.startswith('> '):
                        line = line[2:]
                    if line:
                        lines.append(line)
                overview = '\n'.join(lines).strip() if lines else None
            else:
                overview = None

            if overview and ('详细' in question or '展开' in question):
                for pid in [1, 2, 3, 4]:
                    proj_cn = cls._PROJECT_NUM_CN.get(pid)
                    if not proj_cn:
                        continue
                    pm = re.search(rf'#\s*项目{proj_cn}[：:]', kb_text)
                    if not pm:
                        continue
                    p_start = pm.start()
                    next_pm = re.search(r'\n#\s*项目[一二三四五六七八九][：:]', kb_text[p_start + 1:])
                    if next_pm:
                        p_end = p_start + 1 + next_pm.start()
                    else:
                        next_sec = re.search(r'\n#\s*(?:四个项目|电话面试)', kb_text[p_start + 1:])
                        p_end = p_start + 1 + next_sec.start() if next_sec else len(kb_text)
                    proj_text = kb_text[p_start:p_end]
                    sm = re.search(r'##\s*2\s*[\.、]?\s*30\s*秒项目介绍', proj_text)
                    if sm:
                        sec_tail = proj_text[sm.end():]
                        sec_end = re.search(r'\n(?:##\s|---|\#\s)', sec_tail)
                        sec_content = sec_tail[:sec_end.start()] if sec_end else sec_tail
                        sec_lines = [l.strip() for l in sec_content.strip().split('\n') if l.strip() and l.strip() != '---']
                        if sec_lines:
                            overview += f"\n\n---\n" + '\n'.join(sec_lines)
            return overview

        proj_cn = cls._PROJECT_NUM_CN.get(proj_id)
        if not proj_cn:
            return None

        proj_pattern = rf'#\s*项目{proj_cn}[：:]'
        m = re.search(proj_pattern, kb_text)
        if not m:
            return None

        proj_start = m.start()
        next_proj_m = re.search(r'\n#\s*项目[一二三四五六七八九][：:]', kb_text[proj_start + 1:])
        if next_proj_m:
            proj_end = proj_start + 1 + next_proj_m.start()
        else:
            next_section_m = re.search(r'\n#\s*(?:四个项目|电话面试)', kb_text[proj_start + 1:])
            proj_end = proj_start + 1 + next_section_m.start() if next_section_m else len(kb_text)

        proj_text = kb_text[proj_start:proj_end]

        if '做了什么' in question or '做了' in question:
            section_title = '我在项目中做了什么'
        elif re.search(r'怎么.*做|如何', question):
            section_title = '我是怎么做的'
        elif '为什么' in question:
            section_title = '为什么这样做'
        elif '结果' in question or '成果' in question:
            section_title = '项目结果'
        elif '关联' in question or '匹配' in question or '岗位' in question:
            section_title = '和大华岗位怎么关联'
        else:
            if '详细' in question or '展开' in question or '深入介绍' in question:
                section_title = '90 秒项目介绍'
            else:
                section_title = '30 秒项目介绍'

        section_pattern = rf'##\s*\d*\s*[\.、]?\s*{re.escape(section_title)}'
        sm = re.search(section_pattern, proj_text)
        if not sm:
            key_word = section_title[:4]
            sm = re.search(rf'##\s*\d*\s*[\.、]?\s*{re.escape(key_word)}', proj_text)
            if not sm:
                return None

        sec_start = sm.end()
        sec_tail = proj_text[sec_start:]
        end_m = re.search(r'\n(?:##\s|---|\#\s)', sec_tail)
        content = sec_tail[:end_m.start()] if end_m else sec_tail

        lines = []
        for line in content.strip().split('\n'):
            line = line.strip()
            if line and line != '---':
                lines.append(line)
        return '\n'.join(lines).strip() if lines else None

    @classmethod
    def _extract_innovation_from_kb(cls, kb_text, question, forced_proj_id=None):
        import re
        ask_compare = any(kw in question for kw in ['四个', '横向', '对比', '所有'])
        proj_id = forced_proj_id or cls._identify_project(question)

        if ask_compare:
            m = re.search(r'#\s*六[、）)]?\s*四个项目横向对比', kb_text)
            if m:
                start = m.start()
                tail = kb_text[start:]
                end_m = re.search(r'\n#(?!\s*#)', tail)
                content = tail[:end_m.start()] if end_m else tail
                return cls._clean_innovation_answer(content)
            return None

        if proj_id:
            proj_cn = cls._PROJECT_NUM_CN.get(proj_id)
            if not proj_cn:
                return None

            pat = rf'#\s*\S*[、）)]?\s*项目{proj_cn}[：:].*创新'
            m = re.search(pat, kb_text)
            if not m:
                return None

            sec_start = m.start()
            tail = kb_text[sec_start + 1:]
            end_m = re.search(r'\n#(?!\s*#)', tail)
            sec_end = sec_start + 1 + end_m.start() if end_m else len(kb_text)
            proj_section = kb_text[sec_start:sec_end]

            recite_m = re.search(r'##\s*\d*\s*[\.、]?\s*.*可背版本', proj_section)
            recite_content = None
            if recite_m:
                recite_start = recite_m.end()
                recite_tail = proj_section[recite_start:]
                recite_end = re.search(r'\n(?:##\s|---|\#\s)', recite_tail)
                recite_text = recite_tail[:recite_end.start()] if recite_end else recite_tail
                recite_content = cls._clean_innovation_answer(recite_text)

            summary_m = re.search(r'##\s*1\s*[\.、]?\s*总体创新点概括', proj_section)
            summary_content = None
            if summary_m:
                sum_start = summary_m.end()
                sum_tail = proj_section[sum_start:]
                sum_end = re.search(r'\n(?:##\s|---|\#\s)', sum_tail)
                sum_text = sum_tail[:sum_end.start()] if sum_end else sum_tail
                summary_content = cls._clean_innovation_answer(sum_text)

            if '详细' in question or '展开' in question:
                parts = []
                if summary_content:
                    parts.append(summary_content)
                if recite_content:
                    parts.append('---\n\n【可背版本】\n' + recite_content)
                return '\n\n'.join(parts) if parts else None
            return recite_content or summary_content

        m = re.search(r'#\s*七[、）)]?\s*创新点速记版', kb_text)
        if m:
            start = m.start()
            tail = kb_text[start:]
            end_m = re.search(r'\n#(?!\s*#)', tail[1:])
            content = tail[:end_m.start() + 1] if end_m else tail
            lines = []
            for line in content.strip().split('\n'):
                line = line.strip()
                if not line or line == '---':
                    continue
                if line.startswith('# ') or line.startswith('#\u3000'):
                    continue
                if '答案' in line and ('**' in line or line.startswith('答案')):
                    continue
                lines.append(line)
            return '\n'.join(lines).strip() if lines else None

        return None

    @classmethod
    def _extract_difficulty_from_kb(cls, kb_text, question, forced_proj_id=None):
        import re
        file_m = re.search(r'--- 四个项目难点.*?---', kb_text)
        if not file_m:
            return None
        diff_start = file_m.end()
        next_file = re.search(r'\n--- .*? ---', kb_text[diff_start:])
        diff_end = diff_start + next_file.start() if next_file else len(kb_text)
        diff_text = kb_text[diff_start:diff_end]

        ask_compare = any(kw in question for kw in ['四个', '横向', '对比', '所有'])
        proj_id = forced_proj_id or cls._identify_project(question)

        if ask_compare:
            m = re.search(r'#\s*六[、）)]?\s*四个项目难点横向对比', diff_text)
            if m:
                start = m.start()
                tail = diff_text[start:]
                end_m = re.search(r'\n#(?!\s*#)', tail)
                content = tail[:end_m.start()] if end_m else tail
                return cls._clean_innovation_answer(content)
            return None

        if proj_id:
            proj_cn = cls._PROJECT_NUM_CN.get(proj_id)
            if not proj_cn:
                return None

            pat = rf'#\s*\S*[、）)]?\s*项目{proj_cn}[：:]'
            m = re.search(pat, diff_text)
            if not m:
                return None

            sec_start = m.start()
            tail = diff_text[sec_start + 1:]
            end_m = re.search(r'\n#(?!\s*#)', tail)
            sec_end = sec_start + 1 + end_m.start() if end_m else len(diff_text)
            proj_section = diff_text[sec_start:sec_end]

            recite_m = re.search(r'##\s*\d*\s*[\.、]?\s*.*可背总结版', proj_section)
            recite_content = None
            if recite_m:
                recite_start = recite_m.end()
                recite_tail = proj_section[recite_start:]
                recite_end = re.search(r'\n(?:---|##\s+\d|#(?!\s*#))', recite_tail)
                recite_text = recite_tail[:recite_end.start()] if recite_end else recite_tail
                recite_content = cls._clean_innovation_answer(recite_text)

            summary_m = re.search(r'##\s*1\s*[\.、]?\s*.*难点总览', proj_section)
            summary_content = None
            if summary_m:
                sum_start = summary_m.end()
                sum_tail = proj_section[sum_start:]
                sum_end = re.search(r'\n(?:---|##\s+\d|#(?!\s*#))', sum_tail)
                sum_text = sum_tail[:sum_end.start()] if sum_end else sum_tail
                summary_content = cls._clean_innovation_answer(sum_text)

            if '详细' in question or '展开' in question:
                parts = []
                if summary_content:
                    parts.append(summary_content)
                if recite_content:
                    parts.append('---\n\n【可背总结版】\n' + recite_content)
                return '\n\n'.join(parts) if parts else None
            return recite_content or summary_content

        m = re.search(r'#\s*七[、）)]?\s*难点速记版', diff_text)
        if m:
            start = m.start()
            tail = diff_text[start:]
            end_m = re.search(r'\n#(?!\s*#)', tail[1:])
            content = tail[:end_m.start() + 1] if end_m else tail
            lines = []
            for line in content.strip().split('\n'):
                line = line.strip()
                if not line or line == '---':
                    continue
                if line.startswith('# ') or line.startswith('#\u3000'):
                    continue
                if '答案' in line and ('**' in line or line.startswith('答案')):
                    continue
                lines.append(line)
            return '\n'.join(lines).strip() if lines else None

        return None

    @classmethod
    def _extract_ask_interviewer_from_kb(cls, kb_text, question):
        import re
        file_m = re.search(r'--- 技术面最后反问面试官.*?---', kb_text)
        if not file_m:
            return None
        file_start = file_m.end()
        next_file = re.search(r'\n--- .*? ---', kb_text[file_start:])
        file_end = file_start + next_file.start() if next_file else len(kb_text)
        file_content = kb_text[file_start:file_end]

        ask_pattern = r'\*\*可以这样问：\*\*\s*(.*?)(?=\*\*为什么适合问|\n---|\Z)'
        asks = re.findall(ask_pattern, file_content, re.DOTALL)
        if not asks:
            return None

        cleaned = []
        for ask in asks:
            lines = ask.strip().split('\n')
            text = ' '.join(l.lstrip('> ').strip() for l in lines if l.strip())
            if text:
                cleaned.append(text)

        if not cleaned:
            return None

        parts = []
        for i, ask in enumerate(cleaned, 1):
            parts.append(f'{i}. {ask}')
        return '\n\n'.join(parts) if parts else None

    @staticmethod
    def _clean_innovation_answer(text):
        lines = []
        for line in text.strip().split('\n'):
            line = line.strip()
            if not line or line == '---':
                continue
            if line.startswith('#'):
                continue
            if '答案' in line and ('**' in line or line.startswith('答案')):
                continue
            lines.append(line)
        return '\n'.join(lines).strip() if lines else None

    # ------------------------------------------------------------------
    # RAG: 语义向量检索 + 本地 BGE-Rerank + LLM reranking 终审
    # ------------------------------------------------------------------
    @classmethod
    def _parse_kb_qa_pairs(cls, kb_text):
        import re
        pairs = []
        lines = kb_text.split('\n')
        n = len(lines)
        i = 0
        while i < n:
            line = lines[i].strip()
            if not line:
                i += 1
                continue

            question = None
            q_match_old = re.match(r'^Q[：:]\s*(.+)', line)
            q_match_md = re.match(r'^#+\s*Q\d*[：:]\s*(.+)', line)
            q_match_md2 = re.match(r'^#+\s*问题\d*[：:]\s*(.+)', line)

            if q_match_old:
                question = q_match_old.group(1).strip()
                i += 1
            elif q_match_md:
                question = q_match_md.group(1).strip()
                i += 1
            elif q_match_md2:
                question = q_match_md2.group(1).strip()
                i += 1
            else:
                i += 1
                continue

            # 过滤掉原本的自我介绍问答对以避免冲突，自我介绍由直连处理器提取
            if '自我介绍' in question:
                continue

            answer_lines = []
            while i < n:
                aline = lines[i].strip()
                if not aline:
                    i += 1
                    continue
                if re.match(r'^Q[：:]', aline) or re.match(r'^#+\s*Q\d*[：:]', aline) or re.match(r'^#+\s*问题\d*[：:]', aline):
                    break
                if re.match(r'^A[：:]', aline):
                    answer_lines.append(re.sub(r'^A[：:]\s*', '', aline))
                    i += 1
                elif re.match(r'^\*{0,2}答案\*{0,2}[：:]', aline):
                    i += 1
                elif aline == '---':
                    i += 1
                    break
                elif aline.startswith('提示：'):
                    i += 1
                elif re.match(r'^[一二三四五六七八九十]+[、，,]', aline):
                    break
                elif 'min版本' in aline or '插入建议' in aline:
                    break
                else:
                    answer_lines.append(aline)
                    i += 1
            if question and answer_lines:
                pairs.append((question, '\n'.join(answer_lines)))
        return pairs

    def _mean_pooling(self, outputs, attention_mask):
        import torch
        embeddings = outputs.last_hidden_state
        mask = attention_mask.unsqueeze(-1).expand(embeddings.size()).float()
        sum_embeddings = torch.sum(embeddings * mask, 1)
        sum_mask = torch.clamp(mask.sum(1), min=1e-9)
        return (sum_embeddings / sum_mask).cpu().numpy()

    def _build_qa_index(self):
        """解析 KB，构造触发问答对并构建语义向量索引"""
        import numpy as np

        if not hasattr(self, '_qa_intents') or self._qa_intents is None:
            self._qa_intents = {}
        if not hasattr(self, '_qa_pairs') or self._qa_pairs is None:
            self._qa_pairs = []

        if not self.knowledge_base_text:
            logger.info("[RAG] 知识库为空，跳过索引构建")
            return

        # 1. 加载文档常规问答
        self._qa_pairs = self._parse_kb_qa_pairs(self.knowledge_base_text)
        logger.info(f"[RAG] 从 KB 解析出 {len(self._qa_pairs)} 个通用 Q&A 对")

        # 2. 注入直连意图的虚拟触发句（保持向后兼容，不改变 QA 元组维度）
        triggers = self._get_trigger_questions()
        for q_trigger, tag in triggers:
            self._qa_pairs.append((q_trigger, ""))
            self._qa_intents[q_trigger] = tag
        logger.info(f"[RAG] 注入 {len(triggers)} 个特化意图触发句，总计 QA: {len(self._qa_pairs)}")

        # 3. 加载 embedding 模型
        import torch
        with AIAnalysisThread._model_lock:
            if AIAnalysisThread._shared_embed_model is None:
                from transformers import AutoModel, AutoTokenizer

                model_path = os.path.join(os.path.expanduser('~'), '.cache', 'models', 'text2vec-base-chinese')
                logger.info(f"[RAG] 正在首次加载嵌入模型: {model_path}")
                AIAnalysisThread._shared_embed_tokenizer = AutoTokenizer.from_pretrained(model_path)
                AIAnalysisThread._shared_embed_model = AutoModel.from_pretrained(
                    model_path, torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32
                )
                AIAnalysisThread._shared_embed_device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
                AIAnalysisThread._shared_embed_model.to(AIAnalysisThread._shared_embed_device)
                AIAnalysisThread._shared_embed_model.eval()
                dtype = "FP16" if torch.cuda.is_available() else "FP32"
                logger.info(f"[RAG] 嵌入模型首次加载完成并已常驻内存！ (device={AIAnalysisThread._shared_embed_device}, {dtype})")
            else:
                logger.info("[RAG] 嵌入模型已就绪，复用缓存对象。")
                
            self._embed_tokenizer = AIAnalysisThread._shared_embed_tokenizer
            self._embed_model = AIAnalysisThread._shared_embed_model
            self._embed_device = AIAnalysisThread._shared_embed_device

        # 4. 批量编码
        questions = [q for q, _ in self._qa_pairs]
        batch_size = 32
        all_embeddings = []

        for i in range(0, len(questions), batch_size):
            batch = questions[i:i + batch_size]
            inputs = self._embed_tokenizer(
                batch, padding=True, truncation=True,
                return_tensors="pt", max_length=512
            ).to(self._embed_device)
            with torch.no_grad():
                outputs = self._embed_model(**inputs)

            batch_embeddings = self._mean_pooling(outputs, inputs["attention_mask"])
            all_embeddings.append(batch_embeddings)

        self._qa_embeddings = np.concatenate(all_embeddings, axis=0)
        norms = np.linalg.norm(self._qa_embeddings, axis=1, keepdims=True)
        self._qa_embeddings_norm = self._qa_embeddings / (norms + 1e-9)

        self._index_ready = True
        logger.info(f"[RAG] 语义向量索引构建完成 (docs={len(questions)}, dim={self._qa_embeddings_norm.shape[1]})")

    def _embedding_scores(self, query, top_n=5):
        import numpy as np
        import torch

        if not self._index_ready:
            return []

        inputs = self._embed_tokenizer(
            [query], padding=True, truncation=True,
            return_tensors="pt", max_length=512
        ).to(self._embed_device)
        with torch.no_grad():
            outputs = self._embed_model(**inputs)

        query_emb = self._mean_pooling(outputs, inputs["attention_mask"])
        query_norm = query_emb / (np.linalg.norm(query_emb, axis=1, keepdims=True) + 1e-9)
        similarities = np.dot(query_norm, self._qa_embeddings_norm.T).flatten()

        ranked = np.argsort(similarities)[::-1][:top_n]
        result = [(int(i), float(similarities[i])) for i in ranked if similarities[i] > 0]
        return result

    def _load_bge_reranker(self):
        try:
            with AIAnalysisThread._model_lock:
                if AIAnalysisThread._shared_reranker_model is None:
                    from transformers import AutoModelForSequenceClassification, AutoTokenizer
                    import torch
                    import os

                    model_paths = [
                        os.path.join(os.path.expanduser('~'), '.cache', 'models', 'BAAI', 'bge-reranker-v2-m3'),
                        os.path.join(os.path.expanduser('~'), '.cache', 'models', 'bge-reranker-v2-m3',
                                     'models--BAAI--bge-reranker-v2-m3', 'snapshots',
                                     '953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e'),
                    ]

                    model_path = None
                    for p in model_paths:
                        if os.path.exists(os.path.join(p, 'config.json')):
                            if os.path.exists(os.path.join(p, 'model.safetensors')) or \
                               os.path.exists(os.path.join(p, 'pytorch_model.bin')):
                                model_path = p
                                break

                    if model_path is None:
                        logger.info("[RAG] BGE-Reranker 模型文件未找到，将退至余弦阈值直接判定")
                        return

                    logger.info(f"[RAG] 正在首次加载 BGE-Reranker: {model_path}")
                    AIAnalysisThread._shared_reranker_tokenizer = AutoTokenizer.from_pretrained(model_path)
                    AIAnalysisThread._shared_reranker_model = AutoModelForSequenceClassification.from_pretrained(
                        model_path, torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32
                    )
                    AIAnalysisThread._shared_reranker_device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
                    AIAnalysisThread._shared_reranker_model.to(AIAnalysisThread._shared_reranker_device)
                    AIAnalysisThread._shared_reranker_model.eval()
                    dtype = "FP16" if torch.cuda.is_available() else "FP32"
                    logger.info(f"[RAG] BGE-Reranker 首次加载完成并已常驻内存！ (device={AIAnalysisThread._shared_reranker_device}, {dtype})")
                else:
                    logger.info("[RAG] BGE-Reranker 已就绪，复用缓存对象。")

                self._reranker_tokenizer = AIAnalysisThread._shared_reranker_tokenizer
                self._reranker_model = AIAnalysisThread._shared_reranker_model
                self._reranker_device = AIAnalysisThread._shared_reranker_device
        except Exception as e:
            logger.error(f"[RAG] BGE-Reranker 加载失败，退至余弦阈值直接判定: {e}")
            self._reranker_model = None

    def _bge_rerank(self, question, candidates, top_n=5):
        if self._reranker_model is None:
            return None

        import torch
        import numpy as np

        cands = candidates[:top_n]
        cands = [(idx, score) for idx, score in cands if score > 0]
        if not cands:
            return None

        pairs = []
        for idx, score in cands:
            kb_q = self._qa_pairs[idx][0]
            pairs.append((question, kb_q))

        try:
            with torch.no_grad():
                inputs = self._reranker_tokenizer(
                    pairs, padding=True, truncation=True,
                    return_tensors="pt", max_length=512
                ).to(self._reranker_device)

                logits = self._reranker_model(**inputs).logits
                scores = torch.sigmoid(logits).squeeze(-1).cpu().numpy()

            if scores.ndim == 0:
                scores = np.array([scores.item()])

            ranked_indices = np.argsort(scores)[::-1]
            reranked = [(cands[i][0], float(scores[i])) for i in ranked_indices]

            best_idx, best_score = reranked[0]
            logger.info(f"[RAG] BGE-Reranker 粗排 #1: Q='{self._qa_pairs[best_idx][0][:40]}...' (bge={best_score:.4f})")
            return reranked

        except Exception as e:
            logger.error(f"[RAG] BGE-Reranker 推理异常: {e}")
            return None

    def _llm_rerank(self, question, candidates, top_n=5):
        """调用大模型对语义候选进行精排终审"""
        if not candidates or not hasattr(self, 'client'):
            return None

        cands = candidates[:top_n]
        cands = [(idx, score) for idx, score in cands if score > 0]
        if not cands:
            return None

        context_text = ""
        if self.conversation_history:
            with self._history_lock:
                recent = self.conversation_history[-6:]
            context_lines = []
            for msg in recent:
                role = "面试官" if msg["role"] == "user" else "候选人"
                content = msg["content"].replace("面试官说：", "")
                if len(content) > 80:
                    content = content[:80] + "..."
                context_lines.append(f"{role}: {content}")
            if context_lines:
                context_text = "最近的对话上下文：\n" + "\n".join(context_lines) + "\n\n"

        candidate_list = []
        for rank, (idx, score) in enumerate(cands, 1):
            kb_q = self._qa_pairs[idx][0]
            kb_a_preview = self._qa_pairs[idx][1][:50].replace("\n", " ")
            candidate_list.append(f"{rank}. {kb_q}\n   （答：{kb_a_preview}...）")
        candidates_text = "\n".join(candidate_list)

        prompt = (
            f"{context_text}"
            f"面试官问题：{question}\n"
            f"候选问题列表：\n{candidates_text}\n"
            f"请结合上下文判断：哪个候选问题与面试官问题最匹配？"
            f"如果都不匹配，回复0。只回复数字。"
        )

        try:
            resp = self.client.chat.completions.create(
                model=self.rerank_model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=5,
                temperature=0.1,
            )
            reply = resp.choices[0].message.content.strip()
            import re
            m = re.search(r'\d+', reply)
            if not m:
                logger.warning(f"[RAG] LLM rerank 回复无数字: '{reply}'")
                return None

            choice = int(m.group())
            if choice == 0 or choice > len(cands):
                logger.info(f"[RAG] LLM rerank 判定不匹配 (choice={choice})")
                return False

            idx = cands[choice - 1][0]
            logger.info(f"[RAG] LLM rerank 选择 #{choice}: Q='{self._qa_pairs[idx][0][:40]}...'")
            return self._qa_pairs[idx]

        except Exception as e:
            logger.error(f"[RAG] LLM rerank 异常: {e}")
            return None

    def _find_best_qa_match(self, question):
        """两阶段检索：语义向量粗排 top5 → BGE 重排高低阈值拦截 → (模糊区间) LLM rerank 终审"""
        if not hasattr(self, '_index_ready') or not self._index_ready:
            return None

        if not hasattr(self, '_reranker_model'):
            self._reranker_model = None
        if not hasattr(self, 'conversation_history'):
            self.conversation_history = []
        if not hasattr(self, '_history_lock'):
            self._history_lock = threading.Lock()

        # Stage 1: 语义向量粗排 Top 5
        candidates = self._embedding_scores(question, top_n=5)
        self._last_candidates = candidates
        if not candidates:
            logger.info(f"[RAG] 语义检索无任何匹配: Q='{question[:40]}'")
            return None

        best_idx, best_score = candidates[0]
        
        # Stage 2: 重排与双门槛分流
        if self._reranker_model is not None:
            reranked = self._bge_rerank(question, candidates, top_n=5)
            if reranked is not None:
                best_idx, best_bge_score = reranked[0]
                
                if best_bge_score >= 0.80:
                    logger.info(f"[RAG] BGE 高置信度直接命中 (bge={best_bge_score:.4f} >= 0.80)，输出本地 KB")
                    return self._qa_pairs[best_idx], best_bge_score
                
                if best_bge_score < 0.45:
                    logger.info(f"[RAG] BGE 置信度极低直接拒绝 (bge={best_bge_score:.4f} < 0.45)")
                    return None
                
                logger.info(f"[RAG] BGE 处于模糊区间 (0.45 <= bge={best_bge_score:.4f} < 0.80)，送 LLM 终审")
                cands = reranked
            else:
                cands = candidates
        else:
            if best_score >= 0.65:
                logger.info(f"[RAG] 向量余弦高置信度直接命中 (cosine={best_score:.4f} >= 0.65)，输出本地 KB")
                return self._qa_pairs[best_idx], best_score
            
            if best_score < 0.40:
                logger.info(f"[RAG] 向量余弦置信度极低直接拒绝 (cosine={best_score:.4f} < 0.40)")
                return None
            
            logger.info(f"[RAG] 向量余弦处于模糊区间 (0.40 <= cosine={best_score:.4f} < 0.65)，送 LLM 终审")
            cands = candidates

        # Stage 3: LLM 终审 (模糊匹配情况下启用)
        reranked_result = self._llm_rerank(question, cands, top_n=5)
        if isinstance(reranked_result, tuple):
            return reranked_result, best_score
        elif reranked_result is False:
            logger.info(f"[RAG] LLM rerank 判定不匹配，不回退")
            return None
        else:
            if best_score >= 0.55:
                logger.info(f"[RAG] LLM rerank 失败，余弦分数高 (cosine={best_score:.4f} >= 0.55)，降级回退 top1")
                return self._qa_pairs[best_idx], best_score
            return None

    def run(self):
        self.running = True
        logger.info("AI 分析线程启动")

        try:
            from openai import OpenAI
            self.client = OpenAI(
                base_url=self.base_url,
                api_key=self.api_key,
            )
            self.client.models.list()
            logger.info(f"OpenAI API 就绪 (base_url: {self.base_url}, 模型: {self.model})")
        except ImportError:
            logger.error("错误：未安装 openai，请运行 pip install openai")
            return
        except Exception as e:
            logger.error(f"OpenAI API 初始化失败：{e}")
            return

        logger.info("[AI] 正在加载 RAG 模型...")
        try:
            self._build_qa_index()
            self._load_bge_reranker()
            self._models_loaded = True
            logger.info("[AI] RAG 模型加载完成")
        except Exception as e:
            logger.error(f"[AI] RAG 模型加载失败: {e}")
            self._models_loaded = True

        while not self._stop_event.is_set():
            try:
                # 1. 阻塞获取第一条提问文本作为基准
                timestamp, first_text = self.ai_queue.get(timeout=0.5)
            except Exception:
                continue

            if self.paused:
                continue

            # 2. 开启自适应防抖拼合窗口
            coalesced_texts = [first_text.strip()]
            last_recv_time = time.time()
            debounce_seconds = 1.2  # 1.2 秒内若有新片段涌入则持续聚合并重置防抖

            while not self._stop_event.is_set() and not self.paused:
                elapsed = time.time() - last_recv_time
                remaining = debounce_seconds - elapsed
                if remaining <= 0:
                    break

                try:
                    # 极短超时轮询后续分句
                    next_item = self.ai_queue.get(timeout=min(remaining, 0.1))
                    _, next_text = next_item
                    next_text_strip = next_text.strip()
                    if next_text_strip and next_text_strip not in coalesced_texts:
                        coalesced_texts.append(next_text_strip)
                        last_recv_time = time.time()  # 重置时钟
                except queue.Empty:
                    pass
                except Exception:
                    break

            # 3. 将破碎分句用逗号串联为语义完整的长问句
            full_text = "，".join(coalesced_texts).strip()
            full_text = full_text.replace("，，", "，")
            
            if not full_text:
                continue

            # 过滤语气词、标点后的纯字数有效字符长度检验，若 < 3 字符直接本地拦截，防止语气词被当作提问触发 AI 检索
            import re
            clean_full_text = re.sub(r'[^\w]', '', full_text)
            if len(clean_full_text) < 3:
                logger.info(f"[AI语义过滤] 拦截极短或无实际提问意义的语气词/单字片段: '{full_text}' (有效字数={len(clean_full_text)} < 3)")
                continue

            logger.info(f"[AI防抖拼合] 聚合成句 (count={len(coalesced_texts)}) -> '{full_text}'")

            try:
                answer = self._call_qwen(full_text)
            except Exception as e:
                logger.error(f"[AI] 处理异常，跳过此提问: {e}", exc_info=True)
                continue

            if answer:
                ai_timestamp = datetime.now().strftime("%H:%M:%S")
                self.ai_response_queue.put_nowait((ai_timestamp, full_text, answer, "complete"))
                logger.info(f"[AI] 完成回答: {answer[:40]}...")

        self.running = False
        logger.info("AI 分析线程停止")

    def _call_qwen(self, question):
        """调用通义千问 API (优先使用语义直连分流)"""
        if not hasattr(self, '_qa_intents') or self._qa_intents is None:
            self._qa_intents = {}

        # === 1. 语义直连分流拦截机制（向量/BGE 相似度匹配） ===
        intent_tag = None
        best_score = 0.0
        
        if self._index_ready:
            candidates = self._embedding_scores(question, top_n=3)
            if candidates:
                best_idx, best_score = candidates[0]
                matched_q = self._qa_pairs[best_idx][0]
                intent_tag = self._qa_intents.get(matched_q)
                
                # 只有固定背诵的内容（如自我介绍 self_intro）才允许不经大模型直接快速输出本地原文
                if intent_tag == "self_intro" and best_score >= 0.50:
                    extracted = self._execute_intent_handler(intent_tag, question)
                    if extracted:
                        logger.info(f"[意图分流] 语义命中自我介绍直连 (score={best_score:.4f}) -> 直接输出本地原文")
                        with self._history_lock:
                            self.conversation_history.append({"role": "user", "content": f"面试官说：{question}"})
                            self.conversation_history.append({"role": "assistant", "content": extracted})
                        return extracted
                
                # 防御安全锁：如果是反问面试官意图，必须在问题中检测到邀请反问的强置信度关键词，否则拦截降级回退
                if intent_tag == "ask_interviewer":
                    import re
                    if not re.search(r'想问|要问|问题|反问|任何疑问|补充|想要了解', question):
                        intent_tag = None  # 剥夺直连标签，迫使其退回到普通的大模型自适应推理

        # === 2. 正常 RAG 通用知识库检索匹配 ===
        direct_kb_context = ""
        if self._index_ready:
            rag_result = self._find_best_qa_match(question)
            if isinstance(rag_result, tuple) and rag_result[0] is not None:
                (kb_q, kb_a), score = rag_result
                # 强力防御：不再直接返回本地硬编码原文（防止死板答非所问），而是作为高优先级背景注入大模型进行自适应融合生成
                logger.info(f"[RAG] 命中相关知识库问题 (score={score:.4f}) -> 转化为高优先级上下文注入大模型自适应融合生成")
                direct_kb_context = f"【精选直接相关知识参考】\n提问: {kb_q}\n官方答案原文: {kb_a}\n\n"

        # === 3. 大模型基于局部知识库及简历的通用答复生成 ===
        if hasattr(self, '_last_candidates') and self._last_candidates:
            ctx_parts = []
            for idx, score in self._last_candidates[:5]:
                q, a = self._qa_pairs[idx]
                if a:
                    ctx_parts.append(f"Q: {q}\nA: {a[:200]}")
            kb_text = "\n\n".join(ctx_parts) if ctx_parts else "（无相关知识）"
        else:
            kb_text = "（知识库为空）"
            
        # 融合直接命中的高优先级 Q&A 文本
        if direct_kb_context:
            kb_text = direct_kb_context + "【其他相关候选参考知识】\n" + kb_text

        resume_text = self.resume_text if self.resume_text else "（简历为空）"
        system_content = self.system_prompt.replace("{knowledge_base}", kb_text)
        system_content = system_content.replace("{resume}", resume_text)
        
        # 强力注入高情商面试黄金准则（强洗脑），防范大模型在任何非预期状态下误吐出反问句，或者在追问中机械堆砌简历
        high_eq_rules = (
            "=== 核心黄金面试准则（优先级最高，你必须无条件严格执行！） ===\n"
            "1. 严格区分【做了什么（技术细节）】与【学到了什么（收获/成长/方法论）】！\n"
            "   - 如果面试官询问你在这个项目中“学到了什么”（或者“有什么收获”、“获得了什么成长”等），你必须归纳提炼出你获得的工程实战经验、踩坑后的方法论提升等（如：对实际工业现场噪声的敬畏、解决多线程同步时差的感悟等）。\n"
            "   - 绝对禁止在这种问题下干巴巴、原封不动地平铺直叙去念你的简历项目！\n"
            "2. 针对面试官的追问/否定/纠正（例如：'我是说让你说学到了什么，不是你做了什么'、'回答得不对'等），你必须在回答的第一句话礼貌致歉并立刻根据他的最新指正进行精准作答。千万不能在这个时候反问面试官任何问题！\n"
            "3. 严格禁止在面试中途【主动反问面试官】！\n"
            "   - 绝对不允许在面试官提问、追问技术或项目时，去主动反问面试官（例如：'老师，我想了解一下...'这类问题绝对不允许在非反问阶段生成！）。\n"
            "   - 只有当面试官明确邀请你提问时（例如：'你有什么想问我的吗'、'你有什么问题要问我'），你才能给出反问问题清单。\n\n"
        )
        system_content = high_eq_rules + system_content

        if self.detail_override:
            detail_level = self.detail_override
        else:
            detail_level = self._classify_detail_level(question)
        max_tokens = self._detail_tokens(detail_level, self.default_max_tokens)
        detail_prompt = self._detail_prompt(detail_level)
        logger.info(f"[AI] 模型生成模式 - 回答深度: {detail_level}，Q='{question[:40]}...")

        messages = [{"role": "system", "content": system_content}]
        max_history = 10
        with self._history_lock:
            recent_history = self.conversation_history[-max_history:] if len(self.conversation_history) > max_history else self.conversation_history
        messages.extend(recent_history)
        messages.append({"role": "user", "content": f"面试官说：{question}\n\n{detail_prompt}"})

        try:
            partial_text = []
            retry_count = 0
            max_retries = 2
            ai_timestamp = datetime.now().strftime("%H:%M:%S")

            while retry_count <= max_retries:
                try:
                    stream = self.client.chat.completions.create(
                        model=self.model,
                        messages=messages,
                        max_tokens=max_tokens,
                        stream=True,
                        timeout=30,
                    )

                    for chunk in stream:
                        delta = chunk.choices[0].delta.content if chunk.choices else None
                        if delta:
                            partial_text.append(delta)
                            partial_answer = "".join(partial_text)
                            try:
                                self.ai_response_queue.put_nowait((
                                    ai_timestamp, question, partial_answer, "partial"
                                ))
                            except Exception:
                                pass
                    break

                except Exception as call_err:
                    logger.error(f"[AI] 模型请求失败: {call_err}")
                    if retry_count < max_retries:
                        wait = (2 ** retry_count) * 1
                        logger.info(f"[AI] {wait} 秒后进行重试 ({retry_count+1}/{max_retries})...")
                        time.sleep(wait)
                        retry_count += 1
                        partial_text = []
                    else:
                        return f"[调用异常] {call_err}"

            answer = "".join(partial_text).strip()

            if answer:
                with self._history_lock:
                    self.conversation_history.append({"role": "user", "content": f"面试官说：{question}"})
                    self.conversation_history.append({"role": "assistant", "content": answer})
                return answer
            else:
                return "[未收到回复内容]"
        except Exception as e:
            logger.error(f"[AI] 模型生成捕获异常：{e}", exc_info=True)
            return f"[调用异常] {e}"

    def _execute_intent_handler(self, intent_tag, question):
        """执行特化意图的数据抽取"""
        if intent_tag == "self_intro":
            duration = self._intro_duration(question)
            return self._extract_intro_from_kb(self.knowledge_base_text, duration)
        elif intent_tag.startswith("project_intro_"):
            proj_id = int(intent_tag.split("_")[-1])
            return self._extract_project_intro_from_kb(self.knowledge_base_text, question, proj_id)
        elif intent_tag.startswith("project_difficulty_"):
            proj_id = int(intent_tag.split("_")[-1])
            return self._extract_difficulty_from_kb(self.knowledge_base_text, question, proj_id)
        elif intent_tag.startswith("project_innovation_"):
            proj_id = int(intent_tag.split("_")[-1])
            return self._extract_innovation_from_kb(self.knowledge_base_text, question, proj_id)
        elif intent_tag == "ask_interviewer":
            return self._extract_ask_interviewer_from_kb(self.knowledge_base_text, question)
        return None

    def clear_history(self):
        with self._history_lock:
            self.conversation_history.clear()

    def set_detail_override(self, level):
        self.detail_override = level
        if level:
            logger.info(f"[AI] 回答深度覆盖: {level}")
        else:
            logger.info("回答深度覆盖已重置为自动判断")

    def pause(self):
        self.paused = True
        logger.info("分析线程暂停")

    def resume(self):
        self.paused = False
        logger.info("分析线程恢复")

    def toggle_pause(self):
        if self.paused:
            self.resume()
            return "resumed"
        else:
            self.pause()
            return "paused"

    def stop(self):
        self._stop_event.set()
