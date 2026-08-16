# -*- coding: utf-8 -*-
"""gui_qt / player / player_backend 的离线导入与离屏冒烟测试。

PyQt5 未安装时整文件跳过（gui-qt extra 可选）。
"""
from __future__ import annotations

import os

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


def _fake_chart():
    return type("C", (), {"platform": "phira", "chart_id": 123, "title": "T",
                          "artist": "A", "difficulty": "HD", "charter": None,
                          "extra": {"level": "Lv.14"}})()


def _fake_song():
    return type("S", (), {"platform": "malody", "song_id": 456, "title": "Song",
                          "artist": "AR", "bpm": 180, "duration": 95,
                          "extra": {"mode_mask": 3}})()


def test_aggregate_view_rendering():
    """聚合视图：结果混排 + 行尾显示来源平台 + 左栏聚合按钮选中。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    gui_qt = pytest.importorskip("gui_qt")
    from PyQt5.QtTest import QTest
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    win = gui_qt.ChartunesWindow()
    try:
        win._results[gui_qt.AGG_KEY] = [("chart", _fake_chart()), ("song", _fake_song())]
        win.switch_platform(gui_qt.AGG_KEY)
        QTest.qWait(80)                    # 等分批渲染的 1ms QTimer 跑完
        assert win.list_title_label.text().startswith("聚合搜索")
        assert win.agg_btn.isChecked()
        rows = [win.song_layout.itemAt(i).widget()
                for i in range(win.song_layout.count())]
        tails = [w.tail_label.text() for w in rows
                 if isinstance(w, gui_qt.ChartItemWidget)]
        assert "Phira" in tails            # 聚合行的 tail 是来源平台名
        # 屏幕切换钩子可直接调用（跨屏 DPR 变化时触发），不崩即可
        win._on_screen_changed(None)
        QTest.qWait(30)
    finally:
        win.close()
        QTest.qWait(30)
