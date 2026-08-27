"""doctor 模块单测:注入检查项断言状态与汇总;离线默认项格式。"""

from text2video.doctor import ffmpeg_major_version, run_all_checks


def test_ffmpeg_major_version_parses_suffix_versions():
    """版本串带后缀(如 gyan build)也能解析出主版本号。"""
    assert ffmpeg_major_version(
        "ffmpeg version 9.0-essentials_build-www.gyan.dev Copyright ...") == 9
    assert ffmpeg_major_version("ffmpeg version 7.1-full_build-www.gyan.dev") == 7
    assert ffmpeg_major_version("ffmpeg version n6.1-3-g1234") == 6
    assert ffmpeg_major_version("ffmpeg version 6.1.1") == 6


def test_ffmpeg_major_version_unparsable_returns_zero():
    assert ffmpeg_major_version("totally not ffmpeg") == 0
    assert ffmpeg_major_version("ffmpeg version ?.?.?") == 0


def test_run_all_checks_with_injected_items():
    results = run_all_checks(checks=[
        lambda: ("Python", "PASS", "3.12"),
        lambda: ("ffmpeg", "FAIL", "未找到"),
        lambda: ("字体", "WARN", "回退默认"),
    ])
    assert results[0][1] == "PASS"
    assert results[1] == ("ffmpeg", "FAIL", "未找到")
    assert results[2][1] == "WARN"


def test_run_all_checks_online_appends_network():
    results = run_all_checks(online=True, checks=[lambda: ("x", "PASS", "")])
    assert results[-1][0] == "edge-tts 网络"


def test_run_all_checks_default_shape():
    """默认检查项(不联网)都能跑通并返回三要素。"""
    results = run_all_checks(online=False)
    assert len(results) >= 6
    statuses = {s for _n, s, _d in results}
    assert statuses <= {"PASS", "WARN", "FAIL"}
    for name, status, detail in results:
        assert name and status
        assert isinstance(detail, str)