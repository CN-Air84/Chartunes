# -*- coding: utf-8 -*-
"""gui_qt / player / player_backend 的离线导入冒烟测试。

PyQt5 未安装时整文件跳过（gui-qt extra 可选）。
"""
from __future__ import annotations

import pytest

pytest.importorskip("PyQt5")

import player          # noqa: E402
import player_backend  # noqa: E402


def test_music_player_api_surface():
    """MusicPlayer 公共 API 签名稳定（不含子进程启动的副作用检查）。"""
    for name in ("play_path", "pause", "resume", "stop", "set_volume",
                 "set_pos", "get_pos", "shutdown"):
        assert callable(getattr(player.MusicPlayer, name)), name


def test_backend_command_contract():
    """播放子进程的命令分发骨架可导入且含全部协议动作。"""
    backend_cls = player_backend.PlaybackBackend
    for attr in ("handle_command", "_play", "_stop",
                 "REPORT_INTERVAL_SECONDS", "END_CONFIRM_SAMPLES"):
        assert hasattr(backend_cls, attr), attr
    assert callable(player_backend._probe_duration_ms)


def test_gui_qt_module_importable():
    """gui_qt 模块级导入不创建 QApplication、不启动网络。"""
    gui_qt = pytest.importorskip("gui_qt")
    for attr in ("ChartunesWindow", "ChartItemWidget", "LoginOverlay",
                 "ScrollingLabel", "ElidedLabel", "VolumePopup", "main"):
        assert hasattr(gui_qt, attr), attr
