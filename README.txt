会议实时字幕工具 - 使用说明
============================

功能：
捕获 Windows 系统音频（会议中对方的声音），实时转成文字显示在桌面。
适用于一对一面试/会议，兼容腾讯会议、Zoom、Teams 等所有会议软件。

运行环境：
- Python 3.9+
- 需要 conda 环境 dmh_env（已配置 CUDA + GPU）
- 依赖：soundcard, funasr, numpy, torchaudio

安装依赖（如未安装）：
    conda activate dmh_env
    pip install soundcard funasr torchaudio

运行程序：
    conda activate dmh_env
    python meeting_captioner.py

使用步骤：
1. 运行程序，打开会议字幕窗口
2. 点击 [▶ 开始监听] 按钮
3. 打开会议软件（腾讯会议/Zoom/Teams等）
4. 对方说话时，文字会自动显示在窗口中
5. 点击 [💾 保存记录] 可以导出会议记录为 .txt 文件

注意事项：
- 首次运行会自动下载 SenseVoice 模型（约 900MB），需联网
- 之后运行完全离线
- 确保系统默认音频输出设备正在播放会议音频
- 电脑不能静音！工具捕获的是系统播放的声音（loopback）
- 如果对方声音太小，可以适当调大系统音量
- 程序使用 GPU 加速，转录速度很快（8秒音频约0.2秒处理）

技术说明：
- 音频捕获：soundcard（Windows WASAPI loopback）
- 语音活动检测：numpy 能量阈值 VAD
- 语音识别：SenseVoice-Small（阿里开源，非自回归，低延迟）
  - 10秒音频仅需70ms识别，比 Whisper 快 5-15 倍
  - 中文识别准确率高，支持中粤英日韩 5 种语言
- 界面：tkinter

文件说明：
- meeting_captioner.py  - 主程序
- requirements.txt      - 依赖清单
- README.txt            - 使用说明
