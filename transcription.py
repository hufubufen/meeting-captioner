#!/usr/bin/env python
# -*- coding: utf-8 -*-
import threading
import time
import numpy as np
from datetime import datetime
import logging

logger = logging.getLogger("captioner")

# 语气助词与无实际意义噪声词黑名单 (ASR 前置过滤)
FILLER_WORDS = {
    "嗯", "啊", "哦", "哈", "噢", "呃", "啦", "喂", "呀", "吧", "呢", "呗", "嚯", "噻", "哎", "呀", "嗯嗯", "对", "行", "好",
    "嗯。", "啊。", "哦。", "哈。", "噢。", "呃。", "啦。", "喂。", "呀。", "吧。", "呢。", "呗。", "嚯。", "噻。", "哎。", "呀。", "嗯嗯。", "对。", "行。", "好。",
    "继续", "继续说", "你继续", "请继续", "继续吧", "好的继续",
    "继续。", "继续说。", "你继续。", "请继续。", "继续吧。", "好的继续。",
    "。", ".", "？", "?", "，", ",", "!", "！"
}

class TranscriptionThread(threading.Thread):
    """VAD 检测语音 + SenseVoice 转录，结果同时送字幕队列和AI队列"""

    # 全局模型缓存类变量，避免多次启停造成重复加载
    _shared_model = None
    _shared_postprocess = None
    _model_lock = threading.Lock()

    def __init__(self, audio_queue, text_queue, ai_queue, use_gpu=True, audio_queue_mic=None):
        super().__init__(daemon=True)
        self.audio_queue = audio_queue
        self.audio_queue_mic = audio_queue_mic
        self.text_queue = text_queue
        self.ai_queue = ai_queue
        self.use_gpu = use_gpu
        self.running = False
        self._stop_event = threading.Event()

        # VAD 参数与自适应校准
        self.silence_threshold = 0.006  # 默认初始阈值，将在校准后更新
        self.noise_frames = []          # 收集启动时前 30 帧用作底噪估计
        self.noise_calibrated = False   # 底噪校准是否完成
        
        self.silence_duration = 1.0
        self.max_utterance = 30.0
        self.min_utterance = 0.3
        self.speech_frame_min = 2  # 降低连续帧启动门槛，使得被降噪压缩的前音更容易被捕获

        # 状态
        self.audio_buffer = []
        self.silence_counter = 0.0
        self.is_speaking = False
        self._speech_frame_count = 0  # 连续语音帧计数
        self.utterance_start_time = None

        # 模型指针引用
        self.model = None
        self.postprocess = None

    def run(self):
        self.running = True
        logger.info("转录线程启动")

        try:
            device = "cuda:0" if self.use_gpu else "cpu"
            with TranscriptionThread._model_lock:
                if TranscriptionThread._shared_model is None:
                    from funasr import AutoModel
                    from funasr.utils.postprocess_utils import rich_transcription_postprocess
                    
                    logger.info(f"正在首次加载 SenseVoice-Small ({device})...")
                    TranscriptionThread._shared_model = AutoModel(
                        model="iic/SenseVoiceSmall",
                        trust_remote_code=True,
                        remote_code="./model.py",
                        device=device,
                        disable_update=True,
                    )
                    TranscriptionThread._shared_postprocess = rich_transcription_postprocess
                    logger.info("SenseVoice 语音识别模型首次加载完成并已常驻内存！")
                else:
                    logger.info("SenseVoice 语音识别模型已就绪，直接复用缓存对象。")
                
                self.model = TranscriptionThread._shared_model
                self.postprocess = TranscriptionThread._shared_postprocess
            logger.info("SenseVoice 语音识别模型加载完成！")
        except Exception as e:
            logger.error(f"加载 SenseVoice 失败：{e}", exc_info=True)
            return

        frame_duration = 0.03
        frame_count = 0

        while not self._stop_event.is_set():
            audio_chunk = None
            audio_chunk_mic = None

            if self.audio_queue_mic is None:
                # 1. 单通道模式：直接阻塞获取主队列，闲置时 CPU 占用率为 0%
                try:
                    audio_chunk = self.audio_queue.get(timeout=0.1)
                except Exception:
                    continue
            else:
                # 2. 双通道合流模式：自适应两路防抖对齐，避免空轮询吃满 CPU
                try:
                    # 先以极短超时探测主队列（Speaker）
                    audio_chunk = self.audio_queue.get(timeout=0.02)
                except Exception:
                    pass

                try:
                    if audio_chunk is not None:
                        # 主队列拿到了，麦克风队列执行非阻塞快速获取
                        audio_chunk_mic = self.audio_queue_mic.get_nowait()
                    else:
                        # 主队列空，麦克风队列执行带超时的客观阻塞获取，维持线程挂起休眠
                        audio_chunk_mic = self.audio_queue_mic.get(timeout=0.02)
                except Exception:
                    pass

                # 若两路均无数据，说明当前没有任何声音帧，继续等待
                if audio_chunk is None and audio_chunk_mic is None:
                    continue

                # 3. 执行时域物理矢量混音合流，保持音频波形时序上的绝对连续性
                if audio_chunk is not None and audio_chunk_mic is not None:
                    min_len = min(len(audio_chunk), len(audio_chunk_mic))
                    audio_chunk = audio_chunk[:min_len] + audio_chunk_mic[:min_len]
                elif audio_chunk is None:
                    audio_chunk = audio_chunk_mic

            rms = np.sqrt(np.mean(audio_chunk ** 2))
            if np.isnan(rms) or np.isinf(rms):
                rms = 0.0

            # 1. 环境底噪自适应校准 (启动前30帧，约0.9秒内进行估计)
            if not self.noise_calibrated:
                self.noise_frames.append(rms)
                if len(self.noise_frames) >= 30:
                    valid_noise = [n for n in self.noise_frames if not np.isnan(n) and not np.isinf(n)]
                    if not valid_noise:
                        valid_noise = [0.006]
                    mean_rms = np.mean(valid_noise)
                    std_rms = np.std(valid_noise)
                    # 动态阈值 = 噪声均值 + 3.5倍标准差
                    calibrated = float(mean_rms + 3.5 * std_rms)
                    if np.isnan(calibrated) or np.isinf(calibrated):
                        calibrated = 0.006
                    # 设限制幅限制，防止底噪异常偏高或偏低
                    self.silence_threshold = float(np.clip(calibrated, 0.003, 0.015))
                    self.noise_calibrated = True
                    logger.info(f"[VAD] 环境底噪校准完成: 均值={mean_rms:.6f}, 标准差={std_rms:.6f} -> 判定阈值设为={self.silence_threshold:.6f}")
                continue

            frame_count += 1
            if frame_count % 33 == 0:
                status = "🎤" if rms > self.silence_threshold else "🔇"
                logger.debug(f"[VAD-调试] 当前 RMS={rms:.6f} {status}")

            # 2. 正常语音活动探测 (VAD)
            if rms > self.silence_threshold:
                if not self.is_speaking:
                    # 预缓冲：连续帧计数，达到阈值才正式开始说话
                    self._speech_frame_count += 1
                    self.audio_buffer.append(audio_chunk)
                    if self._speech_frame_count >= self.speech_frame_min:
                        self.is_speaking = True
                        self.utterance_start_time = time.time()
                        self.silence_counter = 0.0
                        logger.info(f"[VAD] 侦测到语音开始 (连续 {self._speech_frame_count} 帧超阈值)")
                else:
                    self.audio_buffer.append(audio_chunk)
                    self.silence_counter = 0.0
            else:
                if not self.is_speaking:
                    # 还没达到连续帧要求就遇到静默，重置预缓冲
                    self._speech_frame_count = 0
                    self.audio_buffer = []
                else:
                    self.audio_buffer.append(audio_chunk)
                    self.silence_counter += frame_duration
                    if self.silence_counter >= self.silence_duration:
                        self._process_utterance()
                    elapsed = time.time() - self.utterance_start_time
                    if elapsed >= self.max_utterance:
                        self._process_utterance()

        self.running = False
        logger.info("转录线程停止")

    def _process_utterance(self):
        if len(self.audio_buffer) < int(self.min_utterance / 0.03):
            self.audio_buffer = []
            self.is_speaking = False
            self.silence_counter = 0.0
            self._speech_frame_count = 0
            return

        audio_np = np.concatenate(self.audio_buffer)
        self.audio_buffer = []
        self.is_speaking = False
        self.silence_counter = 0.0
        self._speech_frame_count = 0

        # === 高阶音频预处理 Pipeline ===
        # 1. 滤除直流分量 (DC Offset Removal)
        audio_np = audio_np - np.mean(audio_np)

        # 2. 动态自动增益控制 (AGC) - 峰值振幅归一化
        max_val = np.max(np.abs(audio_np))
        if max_val > 1e-4:
            # 自动拉伸弱音振幅到标准的 0.8 峰值，防范会议降噪导致的音量微弱，提升 ASR 识别率 20%+
            audio_np = audio_np * (0.8 / max_val)

        try:
            res = self.model.generate(
                input=audio_np,
                cache={},
                language="zh",
                use_itn=True,
            )
            text = self.postprocess(res[0]["text"]).strip()
            if text and text not in FILLER_WORDS:
                timestamp = datetime.now().strftime("%H:%M:%S")
                # 送入字幕队列
                self.text_queue.put((timestamp, text))
                # 送入 AI 分析队列
                try:
                    self.ai_queue.put_nowait((timestamp, text))
                except Exception:
                    pass
        except Exception as e:
            logger.error(f" SenseVoice 识别异常: {e}", exc_info=True)

    def stop(self):
        self._stop_event.set()

    def clear_state(self):
        """清空 VAD 状态和音频缓冲（暂停/恢复时调用，丢弃半截语音）"""
        self.audio_buffer = []
        self.is_speaking = False
        self.silence_counter = 0.0
        self.utterance_start_time = None
        self._speech_frame_count = 0
        logger.info("[转录] VAD 音频缓冲及语音检测状态已清空")
