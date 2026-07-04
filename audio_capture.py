#!/usr/bin/env python
# -*- coding: utf-8 -*-
import threading
import time
import numpy as np
import logging

logger = logging.getLogger("captioner")

class AudioCaptureThread(threading.Thread):
    """持续捕获音频并送入队列

    支持两种模式：
    - "speaker": WASAPI loopback，捕获系统音频输出（默认，适用于电脑端会议）
    - "mic": 麦克风输入（适用于手机外放 + 电脑麦克风收音的电话面试场景）
    """

    def __init__(self, audio_queue, sample_rate=16000, capture_mode="speaker"):
        super().__init__(daemon=True)
        self.audio_queue = audio_queue
        self.sample_rate = sample_rate
        self.capture_mode = capture_mode  # "speaker" 或 "mic"
        self.running = False
        self.error_msg = None  # 记录音频硬件层崩溃的具体错误消息
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()  # set = 暂停收音

    def pause(self):
        """暂停音频捕获（不退出线程，只是停止往队列塞数据）"""
        self._pause_event.set()

    def resume(self):
        """恢复音频捕获"""
        self._pause_event.clear()

    def is_paused(self):
        return self._pause_event.is_set()

    def run(self):
        try:
            import soundcard as sc
        except ImportError:
            self.error_msg = "未安装 soundcard 库，无法采集音频。请运行 pip install soundcard。"
            logger.error("错误：未安装 soundcard，无法采集音频。")
            return

        self.running = True

        try:
            if self.capture_mode == "mic":
                # ====== 麦克风模式：手机外放，电脑麦克风收音 ======
                mic = sc.default_microphone()
                logger.info(f"[音频] 麦克风模式 - 设备: {mic.name}")
                _chunk_count = 0
                with mic.recorder(samplerate=self.sample_rate, channels=1) as recorder:
                    while not self._stop_event.is_set():
                        if self._pause_event.is_set():
                            time.sleep(0.1)
                            continue
                        data = recorder.record(numframes=480)
                        if data is not None and len(data) > 0:
                            audio_chunk = data.flatten().astype(np.float32)
                            try:
                                self.audio_queue.put_nowait(audio_chunk)
                            except Exception:
                                pass
                            _chunk_count += 1
                            if _chunk_count % 33 == 0:
                                rms = float(np.sqrt(np.mean(audio_chunk ** 2)))
                                logger.debug(f"[音频] 麦克风音量 RMS={rms:.4f}")
            else:
                # ====== 扬声器模式（默认）：WASAPI loopback ======
                logger.info("[音频] 扬声器模式启动")
                speaker = sc.default_speaker()
                logger.info(f"默认扬声器: {speaker.name}")

                loopback_mic = None
                for m in sc.all_microphones(include_loopback=True):
                    if m.isloopback and speaker.name in m.name:
                        loopback_mic = m
                        break

                if loopback_mic is None:
                    try:
                        loopback_mic = sc.get_microphone(speaker.id, include_loopback=True)
                    except Exception:
                        for m in sc.all_microphones(include_loopback=True):
                            if m.isloopback:
                                loopback_mic = m
                                break

                if loopback_mic is None:
                    self.error_msg = "未找到 loopback 扬声器音频捕获设备。如果是台式机或外接声卡，请点击切换到麦克风模式试试。"
                    logger.error("错误：未找到 loopback 设备")
                    return

                logger.info(f"使用 loopback: {loopback_mic.name}")

                with loopback_mic.recorder(samplerate=self.sample_rate, channels=1) as mic:
                    while not self._stop_event.is_set():
                        if self._pause_event.is_set():
                            time.sleep(0.1)
                            continue
                        data = mic.record(numframes=480)
                        if data is not None and len(data) > 0:
                            audio_chunk = data.flatten().astype(np.float32)
                            try:
                                self.audio_queue.put_nowait(audio_chunk)
                            except Exception:
                                pass
        except Exception as e:
            self.error_msg = f"音频设备录音被拒绝或独占错误：{e}。请检查您的麦克风或扬声器物理连接及独占属性设置。"
            logger.error(f"[音频] 捕获错误：{e}", exc_info=True)
        finally:
            self.running = False
            logger.info(f"音频捕获线程停止 (mode={self.capture_mode})")

    def stop(self):
        self._stop_event.set()
