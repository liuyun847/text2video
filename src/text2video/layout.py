"""布局模块:与"字体 / 字号 / 每行字数"相关的纯计算逻辑。

原先在 cli.py 里,抽取出来供 cli(参数推导)与 render(从 timeline 重现排版)共用,
避免两处重复推导导致 plan 与 render 结果不一致。
"""

from __future__ import annotations

import os

from . import compose

# 字体文件 -> 家族名(复制该字体到工作目录后,ASS 里用相同家族名即可命中)
_FONT_FAMILY: dict = {
    "msyh": "Microsoft YaHei",
    "simhei": "SimHei",
    "msjh": "Microsoft JhengHei",
    "deng": "DengXian",
    "noto": "Noto Sans CJK SC",
}


def default_font_family(asset_name: str) -> str:
    """按字体文件名猜 ASS 家族名。"""
    for key, family in _FONT_FAMILY.items():
        if key in asset_name.lower():
            return family
    return "Microsoft YaHei"


def auto_fontsize(width: int, height: int) -> int:
    """默认字号:短边 ÷20 向下取整,下限 16。"""
    return max(16, min(width, height) // 20)


def auto_max_chars(width: int, fontsize: int) -> int:
    """默认一行最多中文数:约 92% 宽度 ÷ 字号,下限 4。"""
    return max(4, int((width * 0.92) / fontsize))


def resolve_font(font_arg: str | None) -> tuple[str | None, str]:
    """确定使用的字体文件与 ASS 家族名。

    有 --font 直接采用;否则用系统找到的中文字体文件推导家族名;
    找不到字体文件时按平台回退默认(libass 可能回退系统默认字体,故告警)。
    """
    font_path = compose.pick_system_font()
    if font_arg:
        return font_path, font_arg
    if font_path:
        return font_path, default_font_family(os.path.basename(font_path))
    family = "Noto Sans CJK SC" if os.name != "nt" else "Microsoft YaHei"
    import logging

    logging.getLogger("text2video").warning(
        "未找到系统中文字体文件,字幕使用系统字体 %r(libass 可能回退默认字体)",
        family,
    )
    return None, family
