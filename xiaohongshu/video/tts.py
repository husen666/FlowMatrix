"""
视频配音模块（兼容层）
实际实现在 shared.media.tts，此文件为向后兼容保留
"""

# 直接从 shared 模块导出
from shared.media.tts import TTSGenerator, FFMPEG_EXE, DEFAULT_VOICE  # noqa: F401
