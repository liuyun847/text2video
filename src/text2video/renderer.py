"""出片:text2video render —— 调内置 Remotion 渲染端(renderer/render.mjs)渲染整片。

本模块是**薄壳**,只做三件事:找到 node、按仓库内 renderer/ 定位 render.mjs、把 node 的
原始输出回显。契约校验 / 素材复制到 public/ / bundle / 出片全部仍由 render.mjs 负责,
所以出片行为的唯一事实源还是 renderer/README.md。

路径约定:
  * 渲染端入口按**包根**定位(`<repo>/renderer/render.mjs`),不依赖 cwd;
  * 子进程 cwd = renderer/,让 render.mjs 内部的相对约定(public/、work-tmp/、out/)不变;
  * 用户给的 `--manifest` / `--out` / `--stills-dir` 是**相对当前目录**的路径,
    传参前转成绝对路径(否则会被 render.mjs 按 renderer/ 为根解析,落错地方)。

异常与返回值:
  * 找不到 node 或 renderer/render.mjs:抛 RuntimeError(cli 顶层转成可读报错,退出码 1);
  * 渲染失败:不抛,原样回显 node 输出后返回 node 的退出码。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

LOG = logging.getLogger("text2video")

# 渲染端相对包根的位置(src/text2video/renderer.py → 上三级 = 仓库根)
_RENDERER_DIR = "renderer"
_RENDERER_ENTRY = "render.mjs"

# 找不到 node 时的排障提示(与本机实测环境一致)
_NODE_HINT = (
    "未找到 node(出片需要 Node.js,Remotion 渲染端由它跑;本机实测 Node v24.13.0 可用)。\n"
    "  · 安装/升级:winget install OpenJS.NodeJS.LTS,或见 https://nodejs.org\n"
    "  · 已装但本命令找不到:把 node.exe 所在目录加进 PATH,重开终端再试"
)


def renderer_dir() -> Path:
    """返回渲染端目录 `<repo>/renderer` 的绝对路径,不校验是否存在。

    返回值:
        Path: 按本文件位置推出的仓库根下的 renderer/ 目录。
    """
    return Path(__file__).resolve().parents[2] / _RENDERER_DIR


def render_entry() -> Path:
    """返回渲染端入口 `<repo>/renderer/render.mjs` 的绝对路径,不校验是否存在。

    返回值:
        Path: 入口脚本路径。
    """
    return renderer_dir() / _RENDERER_ENTRY


def find_node() -> str:
    """在 PATH 里找 node 可执行文件。

    返回值:
        str: node 的绝对路径(交给 subprocess 用)。

    异常:
        RuntimeError: PATH 里没有 node,报错文案带安装/排障提示。
    """
    exe = shutil.which("node")
    if not exe:
        raise RuntimeError(_NODE_HINT)
    return exe


def _abs(path: str | None) -> str | None:
    """把用户路径按当前目录转绝对路径(空值原样返回 = 交给渲染端默认值)。"""
    if not path:
        return None
    return os.path.abspath(path)


def build_node_argv(
    node: str,
    manifest: str,
    *,
    out: str | None = None,
    stills: str | None = None,
    stills_dir: str | None = None,
) -> list[str]:
    """拼出 `node render.mjs ...` 的参数表(纯函数,便于单测)。

    参数:
        node: node 可执行文件路径(find_node 的返回值)。
        manifest: render.json 路径;相对路径按**当前目录**解析后转绝对。
        out: 成片 mp4 路径;None 时不传,由 render.mjs 用它自己的默认值
            (renderer/out/demo.mp4)。与 stills 同时给出时忽略(静帧模式不出 mp4)。
        stills: 帧号列表原样透传,如 "0,120"(只出 PNG)。
        stills_dir: 静帧目录;None 时不传(= renderer/out/)。

    返回值:
        list[str]: 可直接交给 subprocess.run 的参数表(第一项是 node)。

    异常:
        ValueError: manifest 为空(argparse 已兜住 required,这里再防一层)。
    """
    manifest_abs = _abs(manifest)
    if not manifest_abs:
        raise ValueError("--manifest 不能为空")
    argv = [node, str(render_entry()), "--manifest", manifest_abs]
    if out and not stills:
        argv += ["--out", _abs(out)]
    if stills:
        argv += ["--stills", str(stills)]
        if stills_dir:
            argv += ["--stills-dir", _abs(stills_dir)]
    return argv


def _echo(text: str | None, stream) -> None:
    """把子进程输出原样写到指定流(保留原文,只在缺末尾换行时补一个)。"""
    if not text:
        return
    stream.write(text)
    if not text.endswith("\n"):
        stream.write("\n")
    stream.flush()


def run_render(
    manifest: str,
    *,
    out: str | None = None,
    stills: str | None = None,
    stills_dir: str | None = None,
) -> int:
    """调渲染端出片:同步跑完 node,回显它的 stdout/stderr 原文。

    参数:
        manifest: produce 产出的 render.json 路径(相对当前目录或绝对)。
        out: 成片路径;与 stills 同时给出时忽略。
        stills: 帧号列表,如 "0,120"。
        stills_dir: 静帧输出目录。

    返回值:
        int: 0 = 成功;非 0 = node 的退出码原样上抛,cli 以此作为进程退出码。

    异常:
        RuntimeError: 找不到 node 或 renderer/render.mjs(缺 renderer/ 说明仓库不完整)。
    """
    entry = render_entry()
    if not entry.is_file():
        raise RuntimeError(
            f"找不到渲染端入口: {entry}\n"
            "  · renderer/ 应与 src/ 同级待在 text2video 仓库内"
        )
    node = find_node()
    argv = build_node_argv(node, manifest, out=out, stills=stills, stills_dir=stills_dir)
    cmdline = subprocess.list2cmdline(argv)
    LOG.debug("渲染命令: %s", cmdline)
    print(f"出片: {cmdline}")

    # 显式按 UTF-8 解码 render.mjs 输出(它打的是中文日志),再由本进程的 UTF-8 流回显,
    # 避免 Windows 控制台代码页把中文读成乱码;errors=replace 保证异常文本也能落出来。
    proc = subprocess.run(
        argv,
        cwd=str(entry.parent),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    _echo(proc.stdout, sys.stdout)
    _echo(proc.stderr, sys.stderr)
    if proc.returncode != 0:
        print(
            f"错误: 渲染失败(node 退出码 {proc.returncode}),以上为 render.mjs 的原始输出",
            file=sys.stderr,
        )
    return proc.returncode
