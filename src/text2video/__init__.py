"""text2video:文本转短视频(语音 + 滚动字幕)。

两阶段:plan 生成多轨对齐的时间线中间产物(审片)→ render 消费时间线渲染 MP4。
"""

from .subs import WordTiming
from .tts import DEFAULT_VOICE

__version__ = "0.1.0"

__all__ = ["DEFAULT_VOICE", "WordTiming", "__version__"]
