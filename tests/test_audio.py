"""
测试音频捕获和 VAD 检测 - 命令行版本
用于排查会议字幕工具的音频问题
"""
import os
import sys
import time
import numpy as np
import queue
import threading

# 使用 Hugging Face 镜像
os.environ['HF_ENDPOINT'] = 'https://hf-mirror.com'

def test_audio_capture():
    """测试音频捕获"""
    print("=" * 50)
    print("音频捕获测试")
    print("=" * 50)

    try:
        import soundcard as sc
    except ImportError:
        print("错误：未安装 soundcard，请运行 pip install soundcard")
        return False

    # 列出所有扬声器
    print("\n可用的音频输出设备：")
    speakers = sc.all_speakers()
    for i, spk in enumerate(speakers):
        print(f"  [{i}] {spk.name} (ID: {spk.id})")

    default_speaker = sc.default_speaker()
    print(f"\n默认扬声器: {default_speaker.name}")

    print("\n开始捕获音频（10秒）...")
    print("请在这10秒内播放一些声音（比如用手机播放音乐到电脑扬声器）")
    print("-" * 50)

    audio_queue = queue.Queue(maxsize=1000)
    stop_event = threading.Event()

    def capture_audio():
        try:
            with default_speaker.recorder(samplerate=16000, channels=1) as mic:
                frame_count = 0
                while not stop_event.is_set() and frame_count < 333:  # 10秒
                    data = mic.record(numframes=480)  # 30ms
                    if data is not None and len(data) > 0:
                        audio_chunk = data.flatten().astype(np.float32)
                        rms = np.sqrt(np.mean(audio_chunk ** 2))
                        frame_count += 1

                        # 每0.5秒打印一次
                        if frame_count % 17 == 0:
                            bar = "█" * int(rms * 1000)
                            status = "🎤 有声音" if rms > 0.003 else "🔇 静默"
                            print(f"  [{frame_count/33:.1f}s] RMS={rms:.6f} {status} {bar}")

                        audio_queue.put(audio_chunk)
        except Exception as e:
            print(f"音频捕获错误：{e}")

    thread = threading.Thread(target=capture_audio, daemon=True)
    thread.start()
    thread.join(timeout=12)
    stop_event.set()

    print("-" * 50)
    print(f"捕获完成，共 {audio_queue.qsize()} 帧音频")
    return audio_queue.qsize() > 0


def test_whisper():
    """测试 Whisper 模型加载"""
    print("\n" + "=" * 50)
    print("Whisper 模型测试")
    print("=" * 50)

    try:
        from faster_whisper import WhisperModel
        print("加载 Whisper base 模型（GPU float16）...")
        model = WhisperModel("base", device="cuda", compute_type="float16")
        print("✓ 模型加载成功！")

        # 测试一段静音
        print("\n测试转录（静音）...")
        silent_audio = np.zeros(16000, dtype=np.float32)  # 1秒静音
        segments, info = model.transcribe(silent_audio, language="zh")
        segments = list(segments)
        if len(segments) == 0:
            print("✓ 静音正确识别为空")
        else:
            print(f"⚠ 静音被识别为: {[s.text for s in segments]}")

        return True
    except Exception as e:
        print(f" Whisper 测试失败：{e}")
        return False


def test_vad_with_audio():
    """用捕获的音频测试 VAD"""
    print("\n" + "=" * 50)
    print("VAD + 转录测试")
    print("=" * 50)

    audio_queue = queue.Queue(maxsize=1000)
    stop_event = threading.Event()

    # 先捕获5秒音频
    print("请说话...（捕获5秒）")

    import soundcard as sc
    speaker = sc.default_speaker()

    def capture():
        try:
            with speaker.recorder(samplerate=16000, channels=1) as mic:
                for _ in range(166):  # 5秒
                    data = mic.record(numframes=480)
                    if data is not None:
                        audio_queue.put(data.flatten().astype(np.float32))
        except Exception as e:
            print(f"捕获错误: {e}")

    t = threading.Thread(target=capture, daemon=True)
    t.start()
    t.join(timeout=7)

    # 处理音频
    print(f"\n处理 {audio_queue.qsize()} 帧音频...")
    all_audio = []
    while not audio_queue.empty():
        all_audio.append(audio_queue.get())

    if len(all_audio) == 0:
        print("没有捕获到音频")
        return

    # 计算整体 RMS
    all_audio = np.concatenate(all_audio)
    rms = np.sqrt(np.mean(all_audio ** 2))
    print(f"整体 RMS: {rms:.6f}")
    print(f"最大值: {np.max(np.abs(all_audio)):.6f}")
    print(f"最小值: {np.min(np.abs(all_audio)):.6f}")

    if rms < 0.001:
        print("\n⚠ 音频能量非常低，可能原因：")
        print("  1. 电脑静音了 / 音量太小")
        print("  2. 音频输出设备不是默认扬声器")
        print("  3. 没有声音在播放")
    elif rms < 0.003:
        print("\n⚠ 音频能量较低，建议：")
        print("  - 调大系统音量")
        print("  - 靠近麦克风说话")
    else:
        print("\n✓ 音频能量正常！")

    # 尝试转录
    print("\n尝试 Whisper 转录...")
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel("base", device="cuda", compute_type="float16")
        segments, info = model.transcribe(all_audio, language="zh")
        segments = list(segments)
        if len(segments) > 0:
            print("✓ 识别结果：")
            for seg in segments:
                print(f"  [{seg.start:.1f}s - {seg.end:.1f}s] {seg.text}")
        else:
            print("⚠ 没有识别出任何内容（可能是静音或噪声）")
    except Exception as e:
        print(f"✗ 转录失败：{e}")


if __name__ == "__main__":
    print("会议字幕工具 - 诊断测试\n")

    # 1. 测试音频捕获
    has_audio = test_audio_capture()

    # 2. 测试 Whisper
    whisper_ok = test_whisper()

    # 3. 如果有音频，测试 VAD + 转录
    if has_audio and whisper_ok:
        test_vad_with_audio()

    print("\n" + "=" * 50)
    print("测试完成！请将以上输出发给我，我帮你分析问题。")
    print("=" * 50)
