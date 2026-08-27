"""配置模块:从 TOML 文件读取常用参数默认值(优先级:命令行 > 配置 > 内置默认)。

配置文件形如(键与 CLI 参数 dest 同名,均为小写下划线):

    [text2video]
    voice = "zh-CN-YunxiNeural"
    bg_color = "1B1B2A"
    width = 1920
    height = 1080

查找顺序:显式 --config PATH > 当前目录下 text2video.toml。
"""

from __future__ import annotations

import logging
import os
import tomllib
from typing import Any

LOG = logging.getLogger(__name__)

DEFAULT_FILENAME = "text2video.toml"


def find_config(explicit: str | None) -> str | None:
    """确定配置文件路径:显式指定 > 当前目录默认名。找不到返回 None。"""
    if explicit:
        if not os.path.isfile(explicit):
            raise FileNotFoundError(f"配置文件不存在: {explicit}")
        return explicit
    candidate = os.path.join(os.getcwd(), DEFAULT_FILENAME)
    return candidate if os.path.isfile(candidate) else None


def load_config(path: str | None) -> dict[str, Any]:
    """读取配置文件 → {dest: value} 字典(仅 [text2video] 节,键转小写)。

    文件损坏时抛 ValueError;未见 [text2video] 节返回空 dict。
    """
    if not path:
        return {}
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"配置文件解析失败 {path}: {exc}") from exc
    section = data.get("text2video", {})
    if not isinstance(section, dict):
        raise TypeError(f"配置文件 {path} 中 [text2video] 必须是表")
    return {str(k).lower(): v for k, v in section.items()}