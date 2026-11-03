"""text2video:文本 → 音轨 + 字幕(画面交给 Remotion 渲染)。

两阶段:plan 生成多轨对齐的时间线中间产物(审片)→ produce 产出发给消费端的
中间件(audio.mp3 / subs.webm / material/ / render.json)。
"""

from .subs import WordTiming
from .tts import DEFAULT_VOICE

__version__ = "0.1.0"

__all__ = ["DEFAULT_VOICE", "WordTiming", "__version__"]
