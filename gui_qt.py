# -*- coding: utf-8 -*-
"""CharTunes 精美前端壳（PyQt5）。

UI 骨架与自绘控件 skid 自 YuanyueTTS 的流媒体选项卡（streaming_page.py），
业务后端换成本项目的 chartunes 三平台客户端（osu! / Phira / Malody）。

用法::

    python gui_qt.py

- 布局：顶栏搜索 + 左侧平台栏 + 右侧卡片式结果列表 + 底部试听播放栏；
- 搜索：点击结果行标题 = 试听（下载到 ./cache/ 后经独立子进程播放），
  「+」= 加入下载队列（./downloads/<平台>/）；
  Malody 结果为歌曲行，试听/下载时自动解析为最低难度谱面，
  无谱死条目已在模块层（chartunes.search）过滤；
- 登录：右上角「👤 登录」打开覆盖面板（osu!/Phira 粘贴 Cookie 或 Phira
  浏览器收割 / Malody 账密），凭证缓存在 ~/.chartunes/state.json；
- 播放：底部栏 ⏮ ⏯ ⏭ ⏹ + 进度条（可拖动 seek）+ 音量（长按静音）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import chartunes as ct

from PyQt5.QtCore import QEvent, QObject, QPoint, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QPainter, QPalette, QPen
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QSlider, QSizePolicy, QVBoxLayout, QWidget, QLayout,
)

from player import MusicPlayer
from qt_stubs import (
    configure_independent_surface, configure_material_overlay,
    configure_semantic_surface, configure_theme_card,
    configure_transparent_container, configure_transparent_root,
    set_transparent_scroll_content,
)
from responsive_ui import UniformUiScaler

try:
    from selenium import webdriver
    from selenium.common.exceptions import WebDriverException
    HAVE_SELENIUM = True
except ImportError:            # 未装 selenium 时仅浏览器收割登录不可用
    HAVE_SELENIUM = False

# ------------------------------------------------------------------ 常量
STATE_DIR = Path.home() / ".chartunes"
STATE_FILE = STATE_DIR / "state.json"
LEGACY_STATE_FILE = Path.home() / ".chart2music" / "state.json"   # 旧版目录
DOWNLOAD_DIR = Path("downloads")
CACHE_DIR = Path("cache")                                        # 试听缓存

PLATFORMS = ["phira", "osu", "malody"]
PLATFORM_CN = {"phira": "Phira", "osu": "osu!", "malody": "Malody",
               "agg": "聚合搜索"}
AGG_KEY = "agg"                     # 聚合视图的伪平台键（三源混排）
AGG_PER_SOURCE = 10                 # 聚合时每源最多取多少条
LOGIN_URL = {"phira": "https://phira.moe/"}     # 仅 Phira 走浏览器收割

# 主题（skid 自流媒体选项卡的默认配色）
FONT_FAMILY = "Microsoft YaHei"
CARD_BG = "#F5F8FF"           # 大卡片背景
COMPONENT_BG = "#ffffff"      # 组件背景
ACCENT = "#3E76D1"            # 强调色（加载圈/选中态）
TEXT_COLOR = "#202A35"
WINDOW_BG = "#E9EDF5"         # 窗口底色

QUEUE_PENDING = "排队中"
QUEUE_RUNNING = "下载中"
QUEUE_DONE = "完成"
QUEUE_FAILED = "失败"


def _fmt_ms(ms: int) -> str:
    s = max(0, int(ms)) // 1000
    return f"{s // 60:02d}:{s % 60:02d}"


# ============================================================== 基础自绘控件
# 以下控件均 skid 自 streaming_page.py，仅剥离设置系统依赖。

class ElidedLabel(QLabel):
    """自动在末尾显示省略号(...)的 QLabel。"""

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setMinimumWidth(1)
        self._full_text = text

    def set_elided_text(self, text):
        self._full_text = text
        self.update_elided_text()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_elided_text()

    def update_elided_text(self):
        metrics = self.fontMetrics()
        elided = metrics.elidedText(self._full_text, Qt.ElideRight, self.width())
        super().setText(elided)


class ElidedButton(QPushButton):
    """保留完整命令文本、绘制时截断为省略号的按钮。"""

    def __init__(self, text='', parent=None):
        super().__init__('', parent)
        self._full_text = ''
        self.set_elided_text(text)

    def set_elided_text(self, text):
        self._full_text = str(text or '')
        self.setToolTip(self._full_text)
        self.setAccessibleName(self._full_text)
        self._update_elided_text()

    def _update_elided_text(self):
        available_width = max(1, self.width() - 24)
        elided = self.fontMetrics().elidedText(
            self._full_text, Qt.ElideRight, available_width)
        super().setText(elided)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_elided_text()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.FontChange:
            self._update_elided_text()


class ScrollingLabel(QLabel):
    """文本超宽时自动横向平移滚动（marquee）的 QLabel。"""

    STEP_PX = 2
    GAP_PX = 40
    DEFAULT_SCROLL_INTERVAL_MS = 400

    def __init__(self, parent=None, scroll_interval_ms=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.setContentsMargins(0, 0, 0, 0)
        self.original_text = ""
        self.offset_px = 0          # 当前像素位移（>= 0）
        self._text_end_offset = 0   # 文本尾部刚好进入可视区域时的位移
        self._max_offset = 0        # 一个周期内的最大位移
        self._scroll_step_px = self.STEP_PX
        try:
            interval_ms = int(scroll_interval_ms)
        except (TypeError, ValueError, OverflowError):
            interval_ms = self.DEFAULT_SCROLL_INTERVAL_MS
        self._default_scroll_interval_ms = max(16, interval_ms)
        self.scroll_timer = QTimer(self)
        self.scroll_timer.setInterval(self._default_scroll_interval_ms)
        self.scroll_timer.timeout.connect(self._scroll_text)
        self._paused = False        # 鼠标悬停时停下方便阅读
        self._overflow = False      # 当前文本是否超长

    def set_scrolling_text(self, text: str):
        self.original_text = text
        self.offset_px = 0
        super().setText(text)       # 未超长时 QLabel 默认渲染
        self.update_text_display()

    def _recompute_overflow(self):
        """超长判定：文本像素宽度 > 控件可用宽度。"""
        fm = self.fontMetrics()
        margins = self.contentsMargins()
        widget_width = self.width() - margins.left() - margins.right()
        if widget_width <= 0:       # 尚未布局
            self._overflow = False
            self._text_end_offset = 0
            self._max_offset = 0
            return
        text_width = fm.width(self.original_text)
        self._overflow = text_width > widget_width
        self._text_end_offset = max(0, text_width - widget_width)
        self._max_offset = self._text_end_offset + self.GAP_PX if self._overflow else 0

    def update_text_display(self):
        self._recompute_overflow()
        self._scroll_step_px = self.STEP_PX
        self.scroll_timer.setInterval(self._default_scroll_interval_ms)
        if not self._overflow:
            self.scroll_timer.stop()
            self.offset_px = 0
        elif not self._paused and not self.scroll_timer.isActive():
            self.scroll_timer.start()
        self.update()

    def pause_scroll(self):
        self._paused = True
        if self.scroll_timer.isActive():
            self.scroll_timer.stop()

    def resume_scroll(self):
        self._paused = False
        if self._overflow and not self.scroll_timer.isActive():
            self.scroll_timer.start()

    def _scroll_text(self):
        if not self._overflow:
            self.scroll_timer.stop()
            self.offset_px = 0
            self.update()
            return
        self.offset_px += self._scroll_step_px
        if self.offset_px >= self._max_offset:
            self.offset_px = 0      # 回到初始态，无缝循环
        self.update()

    def paintEvent(self, event):
        if not self._overflow:
            super().paintEvent(event)
            return
        fm = self.fontMetrics()
        margins = self.contentsMargins()
        x_start = margins.left() - self.offset_px
        y = (self.height() + fm.ascent() - fm.descent()) // 2
        painter = QPainter(self)
        painter.setFont(self.font())
        painter.setPen(self.palette().color(QPalette.WindowText))
        painter.drawText(x_start, y, self.original_text)
        painter.end()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_text_display()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.FontChange and hasattr(self, "scroll_timer"):
            self.update_text_display()


class CircularLoadingIndicator(QWidget):
    """纯自绘圆形加载动画，不阻塞 Qt 事件循环。"""

    def __init__(self, accent_color, parent=None):
        super().__init__(parent)
        configure_semantic_surface(self)
        self.setAccessibleName("加载进度")
        self.setFixedSize(42, 42)
        self._angle = 0
        self._accent = QColor(accent_color)
        if not self._accent.isValid():
            self._accent = QColor(ACCENT)
        self._timer = QTimer(self)
        self._timer.setInterval(32)
        self._timer.timeout.connect(self._advance)

    def start(self):
        if not self._timer.isActive():
            self._timer.start()
        self.update()

    def stop(self):
        self._timer.stop()

    def _advance(self):
        self._angle = (self._angle + 11) % 360
        self.update()

    def paintEvent(self, event):
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        stroke = max(3, int(round(min(self.width(), self.height()) * 0.095)))
        bounds = self.rect().adjusted(stroke, stroke, -stroke, -stroke)

        track_color = QColor(self._accent)
        track_color.setAlpha(48)
        track_pen = QPen(track_color, stroke)
        track_pen.setCapStyle(Qt.RoundCap)
        painter.setPen(track_pen)
        painter.drawEllipse(bounds)

        arc_color = QColor(self._accent)
        arc_color.setAlpha(245)
        arc_pen = QPen(arc_color, stroke)
        arc_pen.setCapStyle(Qt.RoundCap)
        painter.setPen(arc_pen)
        painter.drawArc(bounds, (90 - self._angle) * 16, -270 * 16)


class LoadingOverlay(QWidget):
    """列表加载遮罩：半透明黑幕 + 圆角白卡（spinner + 文案）。"""

    def __init__(self, parent, text="正在加载"):
        super().__init__(parent)
        configure_material_overlay(self)
        self.setObjectName("list_loading_overlay")
        self.setFocusPolicy(Qt.NoFocus)
        self.setAutoFillBackground(False)
        self._target = parent
        self._target.installEventFilter(self)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setAlignment(Qt.AlignCenter)

        self.card = QFrame(self)
        self.card.setObjectName("list_loading_card")
        self.card.setFixedSize(184, 118)
        self.card.setStyleSheet("""
            #list_loading_card {
                background-color: rgba(248, 250, 252, 242);
                border: 1px solid rgba(255, 255, 255, 170);
                border-radius: 8px;
            }
        """)
        outer.addWidget(self.card)

        card_layout = QVBoxLayout(self.card)
        card_layout.setContentsMargins(20, 16, 20, 14)
        card_layout.setSpacing(9)
        card_layout.setAlignment(Qt.AlignCenter)

        self.spinner = CircularLoadingIndicator(ACCENT, self.card)
        card_layout.addWidget(self.spinner, 0, Qt.AlignCenter)

        self.label = QLabel(text, self.card)
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setStyleSheet(
            f"#list_loading_card QLabel {{ color: {TEXT_COLOR}; "
            f"font-size: 14px; font-family: '{FONT_FAMILY}'; }}"
        )
        card_layout.addWidget(self.label)

        self.setGeometry(self._target.rect())
        self.hide()

    def show_loading(self, text=""):
        if text:
            self.label.setText(text)
        self.setGeometry(self._target.rect())
        self.show()
        self.raise_()
        self.spinner.start()

    def hide_loading(self):
        self.spinner.stop()
        self.hide()

    def hideEvent(self, event):
        self.spinner.stop()
        super().hideEvent(event)

    def eventFilter(self, watched, event):
        if watched is self._target and event.type() in {
            QEvent.Resize, QEvent.Show, QEvent.LayoutRequest,
        }:
            self.setGeometry(self._target.rect())
            if self.isVisible():
                self.raise_()
        return super().eventFilter(watched, event)

    def paintEvent(self, event):
        del event
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 174))


class VolumePopup(QFrame):
    """垂直音量调节悬浮窗。"""

    volumeChanged = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        configure_independent_surface(self)
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint)
        self.setFixedSize(50, 250)
        self.setStyleSheet(f"""
            QFrame {{
                background-color: #F0F0F0;
                border: 1px solid #CCCCCC;
                border-radius: 8px;
                font-family: "{FONT_FAMILY}";
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 10)
        layout.setSpacing(5)

        self.percent_label = QLabel("100%")
        self.percent_label.setFixedHeight(20)
        self.percent_label.setAlignment(Qt.AlignCenter)
        self.percent_label.setStyleSheet(
            f"border: none; font-size: 11px; color: #333333; "
            f"font-family: '{FONT_FAMILY}';")
        layout.addWidget(self.percent_label)

        self.slider = QSlider(Qt.Vertical)
        self.slider.setRange(0, 100)
        self.slider.setValue(100)
        self.slider.setCursor(Qt.PointingHandCursor)
        self.slider.setStyleSheet("""
            QSlider::groove:vertical {
                background: white;
                width: 6px;
                border-radius: 3px;
            }
            QSlider::handle:vertical {
                background: #555555;
                height: 14px;
                width: 14px;
                margin: 0 -4px;
                border-radius: 7px;
            }
        """)
        self.slider.valueChanged.connect(self._on_value_changed)
        layout.addWidget(self.slider, 1, Qt.AlignHCenter)

    def _on_value_changed(self, val):
        self.percent_label.setText(f"{val}%")
        self.volumeChanged.emit(val)

    def set_value(self, val):
        self.slider.setValue(val)
        self.percent_label.setText(f"{val}%")


class VolumeButton(QPushButton):
    """支持长按信号的按钮（音量按钮长按静音）。"""

    longPressed = pyqtSignal()

    def __init__(self, text, parent=None, interval=800):
        super().__init__(text, parent)
        self.long_press_timer = QTimer(self)
        self.long_press_timer.setSingleShot(True)
        self.long_press_timer.setInterval(interval)
        self.long_press_timer.timeout.connect(self._on_long_press)
        self._is_long_press = False

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._is_long_press = False
            self.long_press_timer.start()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        self.long_press_timer.stop()
        if self._is_long_press:
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _on_long_press(self):
        self._is_long_press = True
        self.longPressed.emit()


# ============================================================== 结果行控件
class ChartItemWidget(QFrame):
    """单个谱面/歌曲/队列条目卡片。

    kind="chart" 与 kind="song"（Malody 歌曲行，试听/下载时自动解析为
    最低难度谱面）行为一致：+ 加入下载队列；点击标题卡 = 试听。
    kind="queue"：- 从队列移除；下载完成后点击标题卡 = 试听产物。
    """
    preview_clicked = pyqtSignal(object)        # 请求试听 (chart 或 song)
    add_queue_requested = pyqtSignal(object)    # 请求加入下载队列
    remove_requested = pyqtSignal(int)          # 请求移除队列项 (index)
    copy_id_requested = pyqtSignal(str)         # 副文本点击复制 ID

    def __init__(self, kind, title, subtitle, tail="", obj=None,
                 index=-1, indent=False, parent=None):
        super().__init__(parent)
        configure_transparent_container(self)
        self.kind = kind
        self.obj = obj
        self.index = index

        self.setFixedHeight(50)
        self.setFrameShape(QFrame.NoFrame)
        self.setStyleSheet(f"""
            QFrame {{
                background-color: transparent;
                margin: 0px;
                font-family: "{FONT_FAMILY}";
            }}
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6 + (40 if indent else 0), 0, 6, 0)
        layout.setSpacing(10)

        BTN_STYLE = """
            QPushButton {
                background-color: rgb(255,255,255);
                border-radius: 8px;
                border: 1px solid gray;
            }
            QPushButton:hover { background-color: #f0f0f0; }
            QPushButton:pressed { background-color: #e0e0e0; }
        """

        # 1. 功能按钮：队列内 - / 其余 +
        if kind == "queue":
            self.action_btn = QPushButton("-")
            self.action_btn.setToolTip("从队列移除")
            extra = ("background-color: rgb(255, 120, 120); color: white; "
                     "border: 1px solid #CC0000;")
            self.action_btn.clicked.connect(lambda: self.remove_requested.emit(self.index))
        else:
            self.action_btn = QPushButton("+")
            self.action_btn.setToolTip("加入下载队列（Malody 自动取最低难度）")
            extra = "background-color: white; color: #333333; border: 1px solid gray;"
            self.action_btn.clicked.connect(lambda: self.add_queue_requested.emit(self.obj))
        configure_semantic_surface(self.action_btn)
        self.action_btn.setFixedSize(45, 45)
        self.action_btn.setCursor(Qt.PointingHandCursor)
        self.action_btn.setStyleSheet(
            BTN_STYLE + f"QPushButton {{ font-size: 22px; font-weight: bold; {extra} }}")
        layout.addWidget(self.action_btn)

        # 2. 标题卡（点击 = 试听 / 展开难度）
        self.title = title
        name_frame = QFrame()
        name_frame.setFixedHeight(50)
        name_frame.setStyleSheet(
            "background-color: white; border-radius: 8px; border: 1px solid gray;")
        name_frame.setCursor(Qt.PointingHandCursor)
        name_layout = QHBoxLayout(name_frame)
        name_layout.setContentsMargins(15, 0, 15, 0)
        self.name_label = ElidedLabel(title)
        self.name_label.setToolTip(title)
        self.name_label.setStyleSheet(
            f"font-weight: bold; font-size: 18px; background: transparent; "
            f"font-family: '{FONT_FAMILY}'; border: none;")
        self.name_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        name_layout.addWidget(self.name_label)
        name_frame.mousePressEvent = lambda e: self._on_title_clicked()
        layout.addWidget(name_frame, 4)

        # 3. 副文本跑马灯 | 固定短文本
        self.sub_frame = QFrame()
        self.sub_frame.setObjectName("sub_frame")
        self.sub_frame.setFixedHeight(50)
        self.sub_frame.setStyleSheet("""
            QFrame#sub_frame {
                background-color: white;
                border-radius: 8px;
                border: 1px solid gray;
            }
        """)
        sub_layout = QHBoxLayout(self.sub_frame)
        sub_layout.setContentsMargins(15, 0, 12, 0)
        sub_layout.setSpacing(6)

        self.sub_label = ScrollingLabel(scroll_interval_ms=60)
        self.sub_label.set_scrolling_text(subtitle)
        self.sub_label.setToolTip(subtitle)
        self.sub_label.setStyleSheet(
            f"font-size: 16px; color: #555555; background: transparent; "
            f"font-family: '{FONT_FAMILY}'; border: none;")
        self.sub_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        sub_layout.addWidget(self.sub_label, 1)

        self.separator_label = QLabel("|")
        self.separator_label.setFixedWidth(10)
        self.separator_label.setAlignment(Qt.AlignCenter)
        self.separator_label.setStyleSheet(
            f"font-size: 14px; color: #777777; background: transparent; "
            f"font-family: '{FONT_FAMILY}'; border: none;")
        sub_layout.addWidget(self.separator_label)

        self.tail_label = QLabel(tail)
        self.tail_label.setFixedWidth(72)
        self.tail_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.tail_label.setStyleSheet(
            f"font-size: 13px; color: #555555; background: transparent; "
            f"font-family: '{FONT_FAMILY}'; border: none;")
        sub_layout.addWidget(self.tail_label)
        layout.addWidget(self.sub_frame, 3)

        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    # 视觉随状态更新（队列行下载完成后刷新 tail 文本）
    def set_tail(self, text: str, color: str = "#555555"):
        self.tail_label.setText(text)
        self.tail_label.setStyleSheet(
            f"font-size: 13px; color: {color}; background: transparent; "
            f"font-family: '{FONT_FAMILY}'; border: none;")

    def _on_title_clicked(self):
        self.preview_clicked.emit(self.obj)

    def _show_context_menu(self, pos):
        menu = QMenu(self)
        act_add = menu.addAction("加入下载队列")
        act_preview = menu.addAction("试听")
        if self.obj is not None:
            obj_id = getattr(self.obj, "chart_id", None) or getattr(self.obj, "song_id", "")
            act_copy = menu.addAction(f"复制 ID（{obj_id}）")
        else:
            act_copy = None
        action = menu.exec_(self.mapToGlobal(pos))
        if action == act_add:
            self.add_queue_requested.emit(self.obj)
        elif action == act_preview:
            self.preview_clicked.emit(self.obj)
        elif act_copy is not None and action == act_copy:
            self.copy_id_requested.emit(str(obj_id))


# ============================================================== 登录覆盖面板
class LoginOverlay(QWidget):
    """全屏覆盖式登录面板（视觉 skid 自 QrCodeLoginDialog）：
    半透明黑幕 + 居中白色圆角卡片；左 3/4 三平台登录区 + 右 1/4 状态区。"""

    closed = pyqtSignal()
    osu_cookie_ready = pyqtSignal(str)              # osu! cookie 已粘贴
    phira_cookie_ready = pyqtSignal(str)            # Phira cookie 已收割
    malody_logged_in = pyqtSignal(object)           # MalodyClient 实例
    selenium_confirm = pyqtSignal(object, object)   # (holder, event) 请求主线程弹确认框
    log = pyqtSignal(str, bool)

    def __init__(self, parent, state: dict):
        super().__init__(parent)
        configure_material_overlay(self)
        self.setWindowFlags(Qt.SubWindow | Qt.FramelessWindowHint)
        self.setObjectName("login_overlay")
        self._state = state

        # —— 自身作为半透明深色遮罩层 ——
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet("background-color: rgba(0, 0, 0, 160);")
        self._apply_target_geometry()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setAlignment(Qt.AlignCenter)

        self._card = QFrame()
        self._card.setObjectName("login_card")
        self._card.setFixedSize(780, 600)
        self._card.setStyleSheet(f"""
            #login_card {{
                background-color: rgba(255, 255, 255, 252);
                border-radius: 20px;
                font-family: '{FONT_FAMILY}';
            }}
        """)
        outer.addWidget(self._card)

        card_layout = QHBoxLayout(self._card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)

        self._build_left(card_layout)
        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setFixedWidth(1)
        sep.setStyleSheet("color: #e0e0e0; background: #e0e0e0;")
        card_layout.addWidget(sep)
        self._build_right(card_layout)

        if parent is not None:
            parent.installEventFilter(self)

    # ---------------------------------------------------------------- 布局
    def _apply_target_geometry(self):
        target = self.parentWidget()
        if target is not None:
            self.setGeometry(target.rect())

    def eventFilter(self, watched, event):
        if watched is self.parent() and event.type() in {
            QEvent.Resize, QEvent.Show, QEvent.LayoutRequest,
        }:
            self._apply_target_geometry()
            if self.isVisible():
                self.raise_()
        return super().eventFilter(watched, event)

    def _build_left(self, card_layout):
        left = QFrame()
        left.setStyleSheet("background: transparent;")
        v = QVBoxLayout(left)
        v.setContentsMargins(40, 32, 24, 32)
        v.setSpacing(14)

        title = QLabel("账号登录")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(
            f"font-size: 26px; font-weight: bold; color: #222; "
            f"background: transparent; font-family: '{FONT_FAMILY}';")
        v.addWidget(title)

        v.addWidget(self._build_osu_block())
        v.addWidget(self._build_phira_block())
        v.addWidget(self._build_malody_block())
        card_layout.addWidget(left, 3)

    def _section_title(self, text):
        label = QLabel(text)
        label.setStyleSheet(
            f"font-size: 18px; font-weight: bold; color: #333; "
            f"background: transparent; font-family: '{FONT_FAMILY}';")
        return label

    def _hint_label(self, text):
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet(
            f"font-size: 13px; color: #777; background: transparent; "
            f"font-family: '{FONT_FAMILY}';")
        return label

    def _build_osu_block(self):
        block = QFrame()
        block.setStyleSheet(
            "background: #FAFBFF; border-radius: 10px; border: 1px solid #E3E8F2;")
        v = QVBoxLayout(block)
        v.setContentsMargins(18, 12, 18, 12)
        v.setSpacing(6)
        v.addWidget(self._section_title("osu!（粘贴 Cookie）"))
        v.addWidget(self._hint_label(
            "登录页有 Turnstile 人机验证，自动化浏览器过不去：请在正常浏览器"
            "登录 osu.ppy.sh，F12 → Network → 任一请求 → Request Headers，"
            "复制整行 Cookie 的值粘贴到下面。"))
        row = QHBoxLayout()
        row.setSpacing(8)
        self.osu_input = QLineEdit()
        self.osu_input.setPlaceholderText("name1=value1; name2=value2 …")
        self.osu_input.setStyleSheet(
            f"QLineEdit {{ border: 1px solid #BBBBBB; border-radius: 6px; "
            f"padding: 6px 8px; font-size: 13px; font-family: '{FONT_FAMILY}'; }}")
        row.addWidget(self.osu_input, 1)
        btn = QPushButton("保存")
        btn.setFixedHeight(34)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(
            f"QPushButton {{ background-color: {ACCENT}; color: white; "
            f"border-radius: 6px; padding: 0 16px; font-size: 14px; "
            f"font-family: '{FONT_FAMILY}'; }}"
            "QPushButton:hover { background-color: #4f84de; }")
        btn.clicked.connect(self._save_osu)
        row.addWidget(btn)
        v.addLayout(row)
        return block

    def _build_phira_block(self):
        block = QFrame()
        block.setStyleSheet(
            "background: #FAFBFF; border-radius: 10px; border: 1px solid #E3E8F2;")
        v = QVBoxLayout(block)
        v.setContentsMargins(18, 12, 18, 12)
        v.setSpacing(6)
        v.addWidget(self._section_title("Phira（可选）"))
        v.addWidget(self._hint_label(
            "不登录也能搜索下载（免登录通道）。遇风控收紧时二选一："))
        # 方式一：手动粘贴 cookie（即时生效，推荐）
        row = QHBoxLayout()
        row.setSpacing(8)
        self.phira_input = QLineEdit()
        self.phira_input.setPlaceholderText("手动粘贴 Cookie：name1=value1; name2=value2 …")
        self.phira_input.setStyleSheet(
            f"QLineEdit {{ border: 1px solid #BBBBBB; border-radius: 6px; "
            f"padding: 6px 8px; font-size: 13px; font-family: '{FONT_FAMILY}'; }}")
        row.addWidget(self.phira_input, 1)
        save_btn = QPushButton("保存")
        save_btn.setFixedHeight(34)
        save_btn.setCursor(Qt.PointingHandCursor)
        save_btn.setStyleSheet(
            f"QPushButton {{ background-color: {ACCENT}; color: white; "
            f"border-radius: 6px; padding: 0 16px; font-size: 14px; "
            f"font-family: '{FONT_FAMILY}'; }}"
            "QPushButton:hover { background-color: #4f84de; }")
        save_btn.clicked.connect(self._save_phira_cookie)
        row.addWidget(save_btn)
        v.addLayout(row)
        # 方式二：selenium 有头浏览器收割（慢但省事）
        self.phira_btn = QPushButton("打开浏览器登录（慢）")
        self.phira_btn.setFixedHeight(34)
        self.phira_btn.setCursor(Qt.PointingHandCursor)
        self.phira_btn.setStyleSheet(
            f"QPushButton {{ background-color: white; color: {ACCENT}; "
            f"border: 1px solid {ACCENT}; border-radius: 6px; "
            f"font-size: 14px; font-family: '{FONT_FAMILY}'; }}"
            "QPushButton:hover { background-color: #f0f5ff; }")
        self.phira_btn.clicked.connect(self._phira_login)
        if not HAVE_SELENIUM:
            self.phira_btn.setEnabled(False)
            self.phira_btn.setToolTip("未安装 selenium：pip install selenium")
        v.addWidget(self.phira_btn)
        return block

    def _save_phira_cookie(self):
        cookie = self.phira_input.text().strip()
        if cookie.lower().startswith("cookie:"):     # 用户连前缀一起抄了
            cookie = cookie[7:].strip()
        if "=" not in cookie:
            QMessageBox.warning(self, "格式不对",
                                "看着不像 Cookie（应形如 name1=value1; name2=value2）")
            return
        self.phira_cookie_ready.emit(cookie)

    def _build_malody_block(self):
        block = QFrame()
        block.setStyleSheet(
            "background: #FAFBFF; border-radius: 10px; border: 1px solid #E3E8F2;")
        v = QVBoxLayout(block)
        v.setContentsMargins(18, 12, 18, 12)
        v.setSpacing(6)
        v.addWidget(self._section_title("Malody（账密）"))
        row = QHBoxLayout()
        row.setSpacing(8)
        self.malody_name = QLineEdit()
        self.malody_name.setPlaceholderText("用户名")
        self.malody_pwd = QLineEdit()
        self.malody_pwd.setPlaceholderText("密码")
        self.malody_pwd.setEchoMode(QLineEdit.Password)
        for w in (self.malody_name, self.malody_pwd):
            w.setStyleSheet(
                f"QLineEdit {{ border: 1px solid #BBBBBB; border-radius: 6px; "
                f"padding: 6px 8px; font-size: 13px; font-family: '{FONT_FAMILY}'; }}")
        btn = QPushButton("登录")
        btn.setFixedHeight(34)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(
            f"QPushButton {{ background-color: {ACCENT}; color: white; "
            f"border-radius: 6px; padding: 0 16px; font-size: 14px; "
            f"font-family: '{FONT_FAMILY}'; }}"
            "QPushButton:hover { background-color: #4f84de; }")
        btn.clicked.connect(self._malody_login)
        self.malody_pwd.returnPressed.connect(self._malody_login)
        row.addWidget(self.malody_name, 2)
        row.addWidget(self.malody_pwd, 2)
        row.addWidget(btn)
        v.addLayout(row)
        return block

    def _build_right(self, card_layout):
        right = QFrame()
        right.setStyleSheet("background: transparent;")
        v = QVBoxLayout(right)
        v.setContentsMargins(28, 40, 28, 28)
        v.setSpacing(12)

        status_title = QLabel("登录状态")
        status_title.setStyleSheet(
            f"font-size: 16px; font-weight: bold; color: #333; "
            f"background: transparent; font-family: '{FONT_FAMILY}';")
        v.addWidget(status_title)

        self.status_labels: dict[str, QLabel] = {}
        for p in PLATFORMS:
            lab = QLabel()
            lab.setWordWrap(True)
            lab.setStyleSheet(
                f"font-size: 13px; color: #555; background: transparent; "
                f"font-family: '{FONT_FAMILY}';")
            self.status_labels[p] = lab
            v.addWidget(lab)
        v.addStretch(1)

        close_btn = QPushButton("完 成")
        close_btn.setFixedHeight(42)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet(
            f"QPushButton {{ background-color: {ACCENT}; color: white; "
            f"border-radius: 8px; font-size: 15px; font-weight: bold; "
            f"font-family: '{FONT_FAMILY}'; }}"
            "QPushButton:hover { background-color: #4f84de; }")
        close_btn.clicked.connect(self.close_panel)
        v.addWidget(close_btn)
        card_layout.addWidget(right, 1)
        self.refresh_status()

    def refresh_status(self):
        bits = {
            "osu": ("● 已登录" if self._state.get("osu_cookie") else "○ 未登录"),
            "phira": ("● 已登录" if self._state.get("phira_cookie") else "○ 免登录"),
            "malody": (f"● 已登录(uid={self._state['malody_uid']})"
                       if self._state.get("malody_key") else "○ 未登录"),
        }
        for p, text in bits.items():
            self.status_labels[p].setText(f"{PLATFORM_CN[p]}\n{text}")

    # ---------------------------------------------------------------- 动作
    def _save_osu(self):
        cookie = self.osu_input.text().strip()
        if cookie.lower().startswith("cookie:"):     # 用户连前缀一起抄了
            cookie = cookie[7:].strip()
        if "=" not in cookie:
            QMessageBox.warning(self, "格式不对",
                                "看着不像 Cookie（应形如 name1=value1; name2=value2）")
            return
        self.osu_cookie_ready.emit(cookie)

    def _phira_login(self):
        if not HAVE_SELENIUM:
            QMessageBox.information(self, "缺少依赖",
                                    "未安装 selenium：pip install selenium（并确保装有 Chrome）")
            return
        self.phira_btn.setEnabled(False)

        def work():
            try:
                try:
                    driver = webdriver.Chrome()
                except WebDriverException as e:
                    self.log.emit(f"启动浏览器失败（需要本机安装 Chrome）：{e}", True)
                    return
                try:
                    driver.get(LOGIN_URL["phira"])
                    self.log.emit("[Phira] 已打开浏览器，请在页面中完成登录…", False)
                    holder: dict = {}
                    ev = threading.Event()
                    self.selenium_confirm.emit(holder, ev)
                    ev.wait()
                    if not holder.get("ok"):
                        self.log.emit("已取消 Phira 登录", False)
                        return
                    cookies = driver.get_cookies()
                    header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                    if not header:
                        self.log.emit("未收割到任何 cookie", True)
                        return
                    self.phira_cookie_ready.emit(header)
                finally:
                    try:
                        driver.quit()
                    except WebDriverException:
                        pass
            finally:
                # 恢复按钮须回主线程
                self._restore_phira_btn()

        threading.Thread(target=work, daemon=True).start()

    def _restore_phira_btn(self):
        QTimer.singleShot(0, lambda: self.phira_btn.setEnabled(True))

    def _malody_login(self):
        name = self.malody_name.text().strip()
        pwd = self.malody_pwd.text()
        if not (name and pwd):
            return
        self.log.emit(f"[Malody] 登录中：{name}", False)

        def work():
            try:
                client = ct.MalodyClient.login(name, pwd)
            except Exception as e:                   # noqa: BLE001
                self.log.emit(f"Malody 登录失败：{e}", True)
                return
            self.malody_logged_in.emit(client)

        threading.Thread(target=work, daemon=True).start()

    def close_panel(self):
        self.hide()
        self.closed.emit()


# ============================================================== 信号桥
class _Signals(QObject):
    """后台线程 → Qt 主线程的安全投递通道。"""

    log = pyqtSignal(str, bool)                 # 消息, 是否错误
    state_changed = pyqtSignal()                # 登录状态变化
    auth_required = pyqtSignal(str)             # 需要登录某平台
    search_done = pyqtSignal(str, object, int)  # platform, rows, total
    search_failed = pyqtSignal(str)             # 错误消息（含遮罩关闭）
    preview_ready = pyqtSignal(object, str, str)      # chart, path, error
    queue_changed = pyqtSignal()                # 下载队列状态变化
    playback_ended = pyqtSignal()               # 试听自然结束
    copy_done = pyqtSignal(str)                 # 已复制到剪贴板


# ============================================================== 主窗口
class ChartunesWindow(QMainWindow):
    SONG_ROW_GAP = 8
    RENDER_BATCH = 4
    TICK_MS = 250

    def __init__(self):
        super().__init__()
        self.setWindowTitle("CharTunes")
        self.resize(960, 720)
        self.setMinimumSize(800, 560)

        self.state = self._load_state()
        self.clients: dict[str, object | None] = {}
        self._drivers: list = []

        # 视图状态
        self._view_platform = "phira"            # 当前平台视图
        self._view_queue = False                 # 是否显示下载队列视图
        self._results: dict[str, list] = {}      # platform -> [(kind, obj)]
        self._rows: list = []                    # 当前渲染的行 spec 队列
        self._render_next = 0
        self._render_token = 0
        self._render_disposals = deque(maxlen=64)
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._render_next_batch)

        # 下载队列
        self._queue: list[dict] = []             # {chart, status, message, path, want_cover}
        self._queue_keys: set[tuple] = set()
        self._queue_wakeup = threading.Event()
        self._closing = False

        # 试听队列
        self._preview_items: list = []           # ChartInfo 列表（本会话点过的）
        self._preview_keys: set[tuple] = set()
        self._preview_idx = -1
        self._preview_paths: dict[tuple, str] = {}   # (platform, id) -> 文件路径
        self._pre_mute_volume = 100
        self._is_muted = False

        # 日志缓冲
        self._log_buffer: deque = deque(maxlen=500)

        # 播放器
        self.player = MusicPlayer()
        self.player.auto_next_callback = lambda: self.sig.playback_ended.emit()

        # 信号桥
        self.sig = _Signals()
        self._connect_signals()

        self._build_ui()

        # 响应式缩放（960x720 设计基准）
        self._ui_scaler = UniformUiScaler(self._page, 960, 720)
        self._apply_responsive_scale()

        # 播放状态轮询
        self._tick_timer = QTimer(self)
        self._tick_timer.setInterval(self.TICK_MS)
        self._tick_timer.timeout.connect(self._on_tick)
        self._tick_timer.start()

        # 下载 worker
        threading.Thread(target=self._queue_worker, daemon=True).start()

        self._refresh_platform_panel()
        self._log("就绪。点曲名试听、点 + 入队下载；Malody 自动取最低难度。", False)

    # ---------------------------------------------------------------- 状态存取
    def _load_state(self) -> dict:
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        try:                                        # 兼容旧版 chart2music 的凭证
            return json.loads(LEGACY_STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self, **updates) -> None:
        self.state.update(updates)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")

    def get_client(self, platform: str):
        c = self.clients.get(platform)
        if c is not None:
            return c
        if platform == "osu":
            cookie = self.state.get("osu_cookie")
            if not cookie:
                raise ct.AuthError("osu! 未登录：请点右上角「登录」粘贴 Cookie")
            c = ct.OsuClient(cookie=cookie)
        elif platform == "phira":
            c = ct.PhiraClient(cookie=self.state.get("phira_cookie") or None)
        else:
            key, uid = self.state.get("malody_key"), self.state.get("malody_uid")
            if not (key and uid):
                raise ct.AuthError("Malody 未登录：请点右上角「登录」填账密")
            c = ct.MalodyClient(key=key, uid=int(uid))
        self.clients[platform] = c
        return c

    # ---------------------------------------------------------------- 信号
    def _connect_signals(self):
        self.sig.log.connect(self._on_log)
        self.sig.state_changed.connect(self._on_state_changed)
        self.sig.auth_required.connect(self._on_auth_required)
        self.sig.search_done.connect(self._on_search_done)
        self.sig.search_failed.connect(self._on_search_failed)
        self.sig.preview_ready.connect(self._on_preview_ready)
        self.sig.queue_changed.connect(self._on_queue_changed)
        self.sig.playback_ended.connect(self._play_next_preview)
        self.sig.copy_done.connect(lambda t: self._log(f"已复制：{t}", False))

    # ---------------------------------------------------------------- UI 构建
    def _build_ui(self):
        self._page = QWidget()
        self._page.setObjectName("chartunes_root")
        self._page.setStyleSheet(
            f"#chartunes_root {{ font-family: '{FONT_FAMILY}'; "
            f"background-color: {WINDOW_BG}; }}")
        self.setCentralWidget(self._page)

        main_layout = QVBoxLayout(self._page)
        main_layout.setSizeConstraint(QLayout.SetNoConstraint)
        main_layout.setContentsMargins(20, 20, 20, 12)
        main_layout.setSpacing(10)

        # --- 顶部搜索栏 ---
        search_layout = QHBoxLayout()
        search_layout.setSpacing(12)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("搜索谱面 / 歌曲…")
        self.search_input.setFixedSize(225, 65)
        self.search_input.setStyleSheet(f"""
            QLineEdit {{
                background-color: #D3D3D3;
                border: 1px solid #AAAAAA;
                border-radius: 8px;
                padding-left: 15px;
                font-size: 24px;
                font-family: '{FONT_FAMILY}';
            }}
        """)
        self.search_input.returnPressed.connect(self.on_search)
        search_layout.addWidget(self.search_input)

        self.search_btn = QPushButton("🔍")
        self.search_btn.setFixedSize(65, 65)
        self.search_btn.setCursor(Qt.PointingHandCursor)
        self.search_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: white;
                border: 1px solid #AAAAAA;
                border-radius: 8px;
                font-size: 30px;
                color: #0078D7;
                font-family: '{FONT_FAMILY}';
            }}
            QPushButton:hover {{ background-color: #f8f8f8; }}
        """)
        self.search_btn.clicked.connect(self.on_search)
        search_layout.addWidget(self.search_btn)

        search_layout.addStretch(1)
        self.login_btn = QPushButton("👤 登录")
        self.login_btn.setFixedHeight(50)
        self.login_btn.setCursor(Qt.PointingHandCursor)
        self.login_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: white;
                border: 1px solid #AAAAAA;
                border-radius: 25px;
                padding: 0 20px;
                font-size: 18px;
                color: #333333;
                font-family: '{FONT_FAMILY}';
            }}
            QPushButton:hover {{ background-color: #f0f0f0; }}
        """)
        self.login_btn.clicked.connect(self.show_login_panel)
        search_layout.addWidget(self.login_btn)

        main_layout.addLayout(search_layout)

        # --- 中间内容区 ---
        content_layout = QHBoxLayout()
        content_layout.setSpacing(10)

        # 左侧平台栏（大圆角容器）
        self.left_outer = QFrame()
        self.left_outer.setFixedWidth(180)
        self.left_outer.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.left_outer.setStyleSheet(
            f"QFrame {{ background-color: {CARD_BG}; border-radius: 12px; }}")
        left_v = QVBoxLayout(self.left_outer)
        left_v.setContentsMargins(10, 10, 10, 10)
        left_v.setSpacing(8)

        self.agg_btn = QPushButton()
        self.agg_btn.setFixedHeight(56)
        self.agg_btn.setCheckable(True)
        self.agg_btn.setCursor(Qt.PointingHandCursor)
        self.agg_btn.setToolTip("同时搜索三个平台并把结果混排在一起")
        self.agg_btn.clicked.connect(lambda: self.switch_platform(AGG_KEY))
        left_v.addWidget(self.agg_btn)

        self.platform_btns: dict[str, QPushButton] = {}
        for p in PLATFORMS:
            btn = QPushButton()
            btn.setFixedHeight(56)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda _=False, plat=p: self.switch_platform(plat))
            self.platform_btns[p] = btn
            left_v.addWidget(btn)

        self.queue_btn = QPushButton()
        self.queue_btn.setFixedHeight(56)
        self.queue_btn.setCheckable(True)
        self.queue_btn.setCursor(Qt.PointingHandCursor)
        self.queue_btn.clicked.connect(self.switch_queue_view)
        left_v.addWidget(self.queue_btn)

        sep_line = QFrame()
        sep_line.setFrameShape(QFrame.HLine)
        sep_line.setStyleSheet("color: #999999;")
        left_v.addWidget(sep_line)

        open_dir_btn = QPushButton("📂 产物目录")
        open_dir_btn.setFixedHeight(60)
        open_dir_btn.setCursor(Qt.PointingHandCursor)
        open_dir_btn.setStyleSheet(f"""
            QPushButton {{
                background-color: white;
                border-radius: 8px;
                border: 1px solid #BBBBBB;
                font-weight: bold;
                font-size: 18px;
                font-family: '{FONT_FAMILY}';
            }}
            QPushButton:hover {{ background-color: #e8e8e8; }}
        """)
        open_dir_btn.clicked.connect(self.open_download_dir)
        left_v.addWidget(open_dir_btn)

        content_layout.addWidget(self.left_outer)

        # 右侧结果区
        self.right_panel = QWidget()
        right_v = QVBoxLayout(self.right_panel)
        right_v.setContentsMargins(0, 0, 0, 0)
        right_v.setSpacing(6)

        title_layout = QHBoxLayout()
        title_layout.setSpacing(10)
        self.list_title_label = QLabel("Phira")
        self.list_title_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.list_title_label.setFixedHeight(36)
        self.list_title_label.setStyleSheet(
            f"font-weight: bold; font-size: 21px; padding-left: 4px; "
            f"font-family: '{FONT_FAMILY}'; color: #333333;")
        title_layout.addWidget(self.list_title_label)
        title_layout.addStretch(1)

        self.cover_checkbox = QCheckBox("同时保存封面/曲绘")
        self.cover_checkbox.setChecked(True)
        self.cover_checkbox.setStyleSheet(
            f"QCheckBox {{ font-size: 14px; color: #444; "
            f"font-family: '{FONT_FAMILY}'; }}")
        title_layout.addWidget(self.cover_checkbox)
        right_v.addLayout(title_layout)

        self.song_scroll = QScrollArea()
        self.song_scroll.setWidgetResizable(True)
        self.song_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.song_scroll.setStyleSheet("""
            QScrollArea { border: none; background-color: transparent; }
            QScrollBar:vertical { width: 8px; background: transparent; border-radius: 4px; }
            QScrollBar::handle:vertical { background: #888888; border-radius: 4px; }
        """)

        self.song_container = QWidget()
        self.song_layout = QVBoxLayout(self.song_container)
        self.song_layout.setAlignment(Qt.AlignTop)
        self.song_layout.setContentsMargins(6, 6, 6, 6)
        self.song_layout.setSpacing(self.SONG_ROW_GAP)
        set_transparent_scroll_content(self.song_scroll, self.song_container)
        self.song_scroll.setWidget(self.song_container)
        right_v.addWidget(self.song_scroll, 1)

        self.loading_overlay = LoadingOverlay(self.right_panel)

        content_layout.addWidget(self.right_panel, 1)
        main_layout.addLayout(content_layout, 1)

        # --- 底部试听播放栏 ---
        self.player_bar = QFrame()
        self.player_bar.setFixedHeight(120)
        self.player_bar.setStyleSheet(f"""
            QFrame {{
                background-color: {CARD_BG};
                border-radius: 10px;
            }}
        """)
        player_outer = QVBoxLayout(self.player_bar)
        player_outer.setContentsMargins(15, 8, 15, 12)
        player_outer.setSpacing(4)

        # 上行：试听标签 | 分隔 | 曲名/作者 | ⏮ ⏯ ⏭ ⏹ | 弹簧
        player_layout = QHBoxLayout()
        player_layout.setSpacing(12)

        self.now_playing_label = ScrollingLabel()
        self.now_playing_label.set_scrolling_text("点击曲名开始试听")
        self.now_playing_label.setFixedWidth(380)
        self.now_playing_label.setStyleSheet(f"""
            color: #333333;
            font-size: 21px;
            font-family: '{FONT_FAMILY}';
            background-color: {COMPONENT_BG};
            border-radius: 8px;
            padding: 8px 15px;
        """)
        player_layout.addWidget(self.now_playing_label)

        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setFixedWidth(2)
        sep.setStyleSheet("color: #999999; margin: 10px 0;")
        player_layout.addWidget(sep)

        info_layout = QVBoxLayout()
        info_layout.setSpacing(4)
        info_layout.setContentsMargins(5, 0, 5, 0)
        self.cur_song_label = ScrollingLabel()
        self.cur_song_label.setFixedHeight(33)
        self.cur_song_label.setFixedWidth(200)
        self.cur_song_label.setStyleSheet(
            f"font-weight: bold; font-size: 20px; "
            f"font-family: '{FONT_FAMILY}'; background: transparent;")
        self.cur_song_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.cur_artist_label = ScrollingLabel()
        self.cur_artist_label.setFixedHeight(27)
        self.cur_artist_label.setFixedWidth(200)
        self.cur_artist_label.setStyleSheet(
            f"color: #555555; font-size: 17px; "
            f"font-family: '{FONT_FAMILY}'; background: transparent;")
        self.cur_artist_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        info_layout.addWidget(self.cur_song_label)
        info_layout.addWidget(self.cur_artist_label)
        player_layout.addLayout(info_layout)

        ctrl_layout = QHBoxLayout()
        ctrl_layout.setSpacing(12)
        self.prev_btn = QPushButton()
        self.play_pause_btn = QPushButton()
        self.next_btn = QPushButton()
        self.stop_btn = QPushButton()
        CTRL_BTN_STYLE = """
            QPushButton {
                background-color: rgb(255,255,255);
                font: 16pt "Segoe UI Symbol", "HarmonyOS Sans";
                color: #0078D7;
                border-radius: 8px;
                border: 1px solid gray;
            }
            QPushButton:hover { background-color: #f0f0f0; }
            QPushButton:pressed { background-color: #e0e0e0; }
        """
        # 纯字符 + \uFE0E 强制文本模式渲染
        self.prev_btn.setText("\u23EE\uFE0E")
        self.play_pause_btn.setText("\u25B6\uFE0E")
        self.next_btn.setText("\u23ED\uFE0E")
        self.stop_btn.setText("\u23F9\uFE0E")
        for btn in (self.prev_btn, self.play_pause_btn, self.next_btn, self.stop_btn):
            btn.setFixedSize(54, 54)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(CTRL_BTN_STYLE)
        self.stop_btn.setToolTip("停止试听")
        self.prev_btn.clicked.connect(self._play_prev_preview)
        self.play_pause_btn.clicked.connect(self._toggle_play_pause)
        self.next_btn.clicked.connect(self._play_next_preview)
        self.stop_btn.clicked.connect(self._stop_preview)
        ctrl_layout.addWidget(self.prev_btn)
        ctrl_layout.addWidget(self.play_pause_btn)
        ctrl_layout.addWidget(self.next_btn)
        ctrl_layout.addWidget(self.stop_btn)
        player_layout.addLayout(ctrl_layout)
        player_layout.addStretch(1)
        player_outer.addLayout(player_layout)

        # 下行：进度条 | 时间 | 🔊
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(8)
        bottom_row.setAlignment(Qt.AlignVCenter)

        self.progress_slider = QSlider(Qt.Horizontal)
        self.progress_slider.setRange(0, 1000)
        self.progress_slider.setValue(0)
        self.progress_slider.setFixedHeight(20)
        self.progress_slider.setEnabled(False)
        self.progress_slider.setCursor(Qt.PointingHandCursor)
        self.progress_slider.setStyleSheet("""
            QSlider::groove:horizontal {
                border: none;
                height: 6px;
                background: white;
                margin: 2px 0;
                border-radius: 3px;
            }
            QSlider::sub-page:horizontal {
                background: #555555;
                border-radius: 3px;
            }
            QSlider::handle:horizontal {
                background: #333333;
                border: none;
                width: 14px;
                height: 14px;
                margin: -4px 0;
                border-radius: 7px;
            }
        """)
        self.progress_slider.sliderMoved.connect(self._on_seek)
        bottom_row.addWidget(self.progress_slider, 1)

        self.time_label = QLabel("00:00")
        self.time_label.setFixedWidth(110)
        self.time_label.setAlignment(Qt.AlignCenter)
        self.time_label.setStyleSheet(
            f"font-size: 13px; color: #555555; "
            f"font-family: '{FONT_FAMILY}'; background: transparent;")
        bottom_row.addWidget(self.time_label)

        self.volume_popup = VolumePopup(self)
        self.volume_popup.volumeChanged.connect(self._on_volume)
        self.volume_btn = VolumeButton("🔊")
        self.volume_btn.setFixedSize(54, 54)
        self.volume_btn.setCursor(Qt.PointingHandCursor)
        self.volume_btn.setStyleSheet(CTRL_BTN_STYLE)
        self.volume_btn.clicked.connect(self._show_volume_popup)
        self.volume_btn.longPressed.connect(self._toggle_mute)
        bottom_row.addWidget(self.volume_btn)
        player_outer.addLayout(bottom_row)

        main_layout.addWidget(self.player_bar)

        # --- 状态栏 ---
        self._status_label = QLabel("")
        self._status_label.setStyleSheet(f"color: {TEXT_COLOR};")
        self.statusBar().addWidget(self._status_label, 1)
        log_btn = QPushButton("日志")
        log_btn.setFlat(True)
        log_btn.setCursor(Qt.PointingHandCursor)
        log_btn.setStyleSheet(
            f"QPushButton {{ color: {ACCENT}; font-family: '{FONT_FAMILY}'; "
            f"border: none; padding: 0 8px; }}")
        log_btn.clicked.connect(self._show_log_dialog)
        self.statusBar().addPermanentWidget(log_btn)

        self._login_overlay: LoginOverlay | None = None

    # ------------------------------------------------------------ 响应式缩放
    def _apply_responsive_scale(self):
        scaler = getattr(self, "_ui_scaler", None)
        if scaler is not None:
            scaler.apply(self._page.width(), self._page.height())
        if hasattr(self, "song_layout"):
            self.song_layout.setSpacing(self.SONG_ROW_GAP)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_responsive_scale()

    def showEvent(self, event):
        """窗口首次显示后挂接屏幕切换信号（windowHandle 此时才存在）。"""
        super().showEvent(event)
        handle = self.windowHandle()
        if handle is not None and not getattr(self, "_screen_hooked", False):
            self._screen_hooked = True
            handle.screenChanged.connect(self._on_screen_changed)

    def _on_screen_changed(self, _screen) -> None:
        """跨屏拖动后 DPR 变化：重算等比缩放并强制重绘全部自绘控件。"""
        self._apply_responsive_scale()
        for w in self.findChildren(QWidget):
            w.update()

    # ------------------------------------------------------------ 线程投递
    def _thread(self, fn) -> None:
        def run():
            try:
                fn()
            except Exception as e:                   # noqa: BLE001 GUI 兜底
                self.sig.log.emit(f"出错：{e}", True)
        threading.Thread(target=run, daemon=True).start()

    def _log(self, msg: str, error: bool = False) -> None:
        self.sig.log.emit(msg, error)

    # ---------------------------------------------------------------- 搜索
    def on_search(self) -> None:
        if self._view_queue:
            self.switch_platform(self._view_platform)
        q = self.search_input.text().strip()
        if not q:
            return
        platform = self._view_platform
        if platform == AGG_KEY:
            self._aggregate_search(q)
            return
        self._log(f"[{PLATFORM_CN[platform]}] 搜索：{q}")
        self.loading_overlay.show_loading(f"正在搜索 {PLATFORM_CN[platform]}…")

        def work():
            try:
                client = self.get_client(platform)    # 可能抛 AuthError
                if platform == "malody":
                    songs = client.search(q)
                    rows = [("song", s) for s in songs]
                    total = len(songs)
                elif platform == "osu":
                    page = client.search(q, include_covers=True)
                    rows = [("chart", c) for c in page.items]
                    total = page.total
                else:
                    page = client.search(q)
                    rows = [("chart", c) for c in page.items]
                    total = page.total
            except ct.AuthError as e:
                self.sig.search_failed.emit(str(e))
                self.sig.auth_required.emit(platform)
                return
            except Exception as e:                   # noqa: BLE001
                self.sig.search_failed.emit(f"搜索失败：{e}")
                return
            self.sig.search_done.emit(platform, rows, total)

        self._thread(work)

    def _aggregate_search(self, q: str) -> None:
        """聚合搜索：三源并发，未登录/失败的源跳过（不打断），结果混排。"""
        self._log(f"[聚合] 三源并发搜索：{q}")
        self.loading_overlay.show_loading("正在聚合搜索三个平台…")

        def search_one(p: str):
            try:
                client = self.get_client(p)
                if p == "malody":
                    songs = client.search(q)        # 死条目已在模块层过滤
                    return p, [("song", s) for s in songs[:AGG_PER_SOURCE]], None
                if p == "osu":
                    page = client.search(q, include_covers=True)
                else:
                    page = client.search(q)
                return p, [("chart", c) for c in page.items[:AGG_PER_SOURCE]], None
            except ct.AuthError as e:
                return p, [], f"[聚合] 跳过 {PLATFORM_CN[p]}：{e}"
            except Exception as e:                   # noqa: BLE001
                return p, [], f"[聚合] {PLATFORM_CN[p]} 失败：{e}"

        def work():
            with ThreadPoolExecutor(max_workers=len(PLATFORMS)) as ex:
                results = list(ex.map(search_one, PLATFORMS))
            rows: list = []
            notes: list = []
            per_source: dict[str, int] = {}
            for p, r, err in results:
                rows.extend(r)
                per_source[p] = len(r)
                if err:
                    notes.append(err)
            if not rows:
                self.sig.search_failed.emit("聚合搜索：三个平台都没有可用结果")
                for n in notes:
                    self.sig.log.emit(n, True)
                return
            hits = "，".join(f"{PLATFORM_CN[p]} {n}" for p, n in per_source.items() if n)
            self.sig.log.emit(f"[聚合] {hits}", False)
            for n in notes:                          # 跳过/失败源放后面提示
                self.sig.log.emit(n, True)
            self.sig.search_done.emit(AGG_KEY, rows, len(rows))

        self._thread(work)

    def _on_search_failed(self, message: str) -> None:
        self.loading_overlay.hide_loading()
        self._on_log(message, True)

    def _on_search_done(self, platform: str, rows, total: int) -> None:
        self.loading_overlay.hide_loading()
        self._results[platform] = rows
        self.switch_platform(platform)               # 渲染新结果
        if platform == AGG_KEY:
            return                                  # 聚合的分源统计已在 worker 里打印
        hits = len(rows)
        if platform == "malody":
            self._log(f"命中 {hits} 首歌（点击曲名展开难度）")
        else:
            self._log(f"命中 {hits} 条（total={total}）")

    # ------------------------------------------------------------ 行渲染
    @staticmethod
    def _chart_row(chart) -> dict:
        # 只留作者与谱面 id——听歌用，难度/定数等元数据没有意义
        return {"kind": "chart", "obj": chart, "title": chart.title,
                "subtitle": chart.artist or "—",
                "tail": str(chart.chart_id)[:10]}

    @staticmethod
    def _song_row(song) -> dict:
        return {"kind": "song", "obj": song, "title": song.title,
                "subtitle": song.artist or "—",
                "tail": str(song.song_id)[:10]}

    def _current_rows(self) -> list:
        if self._view_queue:
            rows = []
            for i, item in enumerate(self._queue):
                chart = item["chart"]
                rows.append({
                    "kind": "queue", "obj": item.get("src") or chart, "index": i,
                    "title": chart.title,
                    "subtitle": f"{PLATFORM_CN[chart.platform]} | {chart.artist or '—'}",
                    "tail": item["status"],
                    "item": item,
                })
            return rows
        agg = self._view_platform == AGG_KEY
        rows = []
        for kind, obj in self._results.get(self._view_platform, []):
            spec = self._song_row(obj) if kind == "song" else self._chart_row(obj)
            if agg and kind != "song":             # 聚合视图用 tail 标记来源平台
                spec["tail"] = PLATFORM_CN.get(obj.platform, obj.platform)
            rows.append(spec)
        return rows

    def _refresh_rows(self) -> None:
        rows = self._current_rows()
        if self._view_queue:
            done = sum(1 for it in self._queue if it["status"] == QUEUE_DONE)
            self.list_title_label.setText(f"下载队列 · {len(self._queue)} 项（完成 {done}）")
        else:
            results = self._results.get(self._view_platform, [])
            total = len(results)
            suffix = f" · {total} 条" if total else ""
            self.list_title_label.setText(PLATFORM_CN[self._view_platform] + suffix)
        self._set_rows(rows)

    def _set_rows(self, rows: list) -> None:
        self._rows = rows
        self._render_token += 1
        self._render_next = 0
        self._render_disposals.clear()
        while self.song_layout.count():
            item = self.song_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
                self._render_disposals.append(w)
        if not rows:
            hint = QLabel("输入关键词回车搜索" if not self._view_queue
                          else "队列为空：在搜索结果里点 + 加入下载")
            hint.setAlignment(Qt.AlignCenter)
            hint.setStyleSheet(
                f"color: #999999; font-size: 15px; font-family: '{FONT_FAMILY}';")
            self.song_layout.addWidget(hint)
            return
        self._render_timer.start(1)

    def _render_next_batch(self) -> None:
        token = self._render_token
        batch = self._rows[self._render_next:self._render_next + self.RENDER_BATCH]
        for spec in batch:
            widget = ChartItemWidget(
                kind=spec["kind"], title=spec["title"],
                subtitle=spec.get("subtitle", ""), tail=spec.get("tail", ""),
                obj=spec.get("obj"), index=spec.get("index", -1),
                indent=spec.get("indent", False))
            widget.preview_clicked.connect(self._on_preview_clicked)
            widget.add_queue_requested.connect(self._enqueue_download)
            widget.remove_requested.connect(self._remove_queue_item)
            widget.copy_id_requested.connect(self._copy_id)
            if spec["kind"] == "queue":
                status = spec.get("item", {}).get("status")
                color = {"完成": "#2e7d32", "失败": "#c0392b",
                         "下载中": ACCENT}.get(status, "#555555")
                widget.set_tail(status or "", color)
            self.song_layout.addWidget(widget)
        self._render_next += len(batch)
        if self._render_next < len(self._rows) and token == self._render_token:
            self._render_timer.start(1)

    # ------------------------------------------------------------ 平台/队列视图
    def switch_platform(self, platform: str) -> None:
        self._view_platform = platform
        self._view_queue = False
        self._sync_left_panel()
        self._refresh_rows()

    def switch_queue_view(self) -> None:
        self._view_queue = True
        self._sync_left_panel()
        self._refresh_rows()

    def _sync_left_panel(self) -> None:
        active_style = f"""
            QPushButton {{
                background-color: {ACCENT};
                color: white;
                border: none;
                border-radius: 8px;
                font-size: 15px;
                font-weight: bold;
                text-align: left;
                padding-left: 12px;
                font-family: '{FONT_FAMILY}';
            }}
        """
        idle_style = f"""
            QPushButton {{
                background-color: white;
                color: #333333;
                border: 1px solid #DDDDDD;
                border-radius: 8px;
                font-size: 15px;
                text-align: left;
                padding-left: 12px;
                font-family: '{FONT_FAMILY}';
            }}
            QPushButton:hover {{ background-color: #eef3fd; }}
        """
        agg_active = (not self._view_queue and self._view_platform == AGG_KEY)
        self.agg_btn.setChecked(agg_active)
        self.agg_btn.setStyleSheet(active_style if agg_active else idle_style)
        for p, btn in self.platform_btns.items():
            active = (not self._view_queue and p == self._view_platform)
            btn.setChecked(active)
            btn.setStyleSheet(active_style if active else idle_style)
        self.queue_btn.setChecked(self._view_queue)
        self.queue_btn.setStyleSheet(active_style if self._view_queue else idle_style)

    def _refresh_platform_panel(self) -> None:
        texts = {
            "osu": ("● 已登录" if self.state.get("osu_cookie") else "○ 未登录"),
            "phira": ("● 已登录" if self.state.get("phira_cookie") else "○ 免登录"),
            "malody": ("● 已登录" if self.state.get("malody_key") else "○ 未登录"),
        }
        self.agg_btn.setText("✦ 聚合搜索   三源合一")
        for p, btn in self.platform_btns.items():
            btn.setText(f"{PLATFORM_CN[p]}   {texts[p]}")
        pending = sum(1 for it in self._queue if it["status"] in (QUEUE_PENDING, QUEUE_RUNNING))
        self.queue_btn.setText(f"⬇ 下载队列   {len(self._queue)}"
                               + (f"（{pending} 待办）" if pending else ""))
        self._sync_left_panel()

    def open_download_dir(self) -> None:
        path = DOWNLOAD_DIR.resolve()
        path.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(str(path))             # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as e:                       # noqa: BLE001
            self._log(f"打开目录失败：{e}（路径 {path}）", True)

    # ---------------------------------------------------------------- 下载队列
    @staticmethod
    def _queue_key_of(obj) -> tuple:
        """队列去重键：SongInfo 用 song 前缀（与解析后的难度不撞）。"""
        if isinstance(obj, ct.SongInfo):
            return ("malody", f"song-{obj.song_id}")
        return (obj.platform, str(obj.chart_id))

    def _enqueue_download(self, obj) -> None:
        key = self._queue_key_of(obj)
        if key in self._queue_keys:
            self._log("已在下载队列中", False)
            return
        self._queue_keys.add(key)
        self._queue.append({
            "chart": obj, "src": obj, "status": QUEUE_PENDING, "message": "",
            "path": None,
            "want_cover": self.cover_checkbox.isChecked(),
        })
        self._queue_wakeup.set()
        self._on_queue_changed()
        self._log(f"已加入队列：{obj.title}")

    def _remove_queue_item(self, index: int) -> None:
        if not (0 <= index < len(self._queue)):
            return
        item = self._queue[index]
        if item["status"] == QUEUE_RUNNING:
            self._log("该条目正在下载，无法移除", True)
            return
        self._queue_keys.discard(self._queue_key_of(item["src"]))
        self._queue.pop(index)
        self._on_queue_changed()

    def _queue_worker(self) -> None:
        """常驻串行下载线程。SongInfo 在此解析为最低难度谱面。"""
        while not self._closing:
            item = next((it for it in self._queue if it["status"] == QUEUE_PENDING), None)
            if item is None:
                self._queue_wakeup.wait()
                self._queue_wakeup.clear()
                continue
            item["status"] = QUEUE_RUNNING
            self.sig.queue_changed.emit()
            try:
                client = self.get_client(item["chart"].platform)
                if isinstance(item["chart"], ct.SongInfo):
                    # Malody 歌曲行：自动取最低难度
                    item["chart"] = client.default_chart(item["chart"])
                chart = item["chart"]
                tag = f"[{PLATFORM_CN[chart.platform]}] {chart.title}"
                out = DOWNLOAD_DIR / chart.platform
                if chart.platform == "malody":
                    self.sig.log.emit(f"{tag} 下载整包（cid={chart.chart_id}）…", False)
                    bundle = client.download_bundle(chart)
                    music_path = bundle.music.save(out)
                    item["path"] = str(music_path)
                    msg = f"{tag} 音乐 -> {music_path}"
                    if item["want_cover"] and bundle.cover:
                        msg += f"\n{tag} 曲绘 -> {bundle.cover.save(out)}"
                else:
                    self.sig.log.emit(f"{tag} 下载中（id={chart.chart_id}）…", False)
                    music = client.download_music(chart)
                    music_path = music.save(out)
                    item["path"] = str(music_path)
                    msg = f"{tag} 音乐 -> {music_path}"
                    if item["want_cover"]:
                        try:
                            cover = client.download_cover(chart)
                            msg += f"\n{tag} 封面 -> {cover.save(out)}"
                        except ct.CharTunesError as e:
                            msg += f"\n{tag} 封面跳过：{e}"
                item["status"] = QUEUE_DONE
                item["message"] = msg
                self.sig.log.emit(msg, False)
            except ct.AuthError as e:
                item["status"] = QUEUE_FAILED
                item["message"] = str(e)
                self.sig.log.emit(f"{item['chart'].title} 失败：{e}", True)
                self.sig.auth_required.emit(item["chart"].platform)
            except Exception as e:                   # noqa: BLE001
                item["status"] = QUEUE_FAILED
                item["message"] = str(e)
                self.sig.log.emit(f"{item['chart'].title} 失败：{e}", True)
            self.sig.queue_changed.emit()

    def _on_queue_changed(self) -> None:
        self._refresh_platform_panel()
        if self._view_queue:
            self._refresh_rows()

    # ---------------------------------------------------------------- 试听
    def _on_preview_clicked(self, obj) -> None:
        # 队列视图里点击已完成的条目：直接试听产物文件
        if self._view_queue:
            for item in self._queue:
                if item.get("src") is obj or item["chart"] is obj:
                    if item["path"]:
                        self._preview_local(item["chart"], item["path"])
                    else:
                        self._log("该条目尚未下载完成，稍后再试", True)
                    return
        self._start_preview(obj)

    def _start_preview(self, obj) -> None:
        """obj 为 ChartInfo 直接准备缓存；为 SongInfo（Malody）先解析最低难度。"""
        fast_key = None if isinstance(obj, ct.SongInfo) \
            else (obj.platform, str(obj.chart_id))
        if fast_key:
            cached = self._preview_paths.get(fast_key)
            if cached and Path(cached).exists():
                self._preview_local(obj, cached)
                return
        tag = f"[{PLATFORM_CN[obj.platform]}] {obj.title}"
        self._log(f"{tag} 准备试听（下载到缓存）…")
        self.loading_overlay.show_loading(f"正在准备试听：{obj.title}")

        def work():
            try:
                if isinstance(obj, ct.SongInfo):
                    client = self.get_client("malody")
                    chart = client.default_chart(obj)     # 自动取最低难度
                else:
                    chart = obj
                key = (chart.platform, str(chart.chart_id))
                cache_dir = CACHE_DIR / chart.platform
                cache_dir.mkdir(parents=True, exist_ok=True)
                existing = list(cache_dir.glob(f"{chart.chart_id}.*"))
                if existing:
                    path = str(existing[0])
                else:
                    client = self.get_client(chart.platform)
                    ef = client.download_music(chart)
                    path = str(ef.save(cache_dir / f"{chart.chart_id}.{ef.format or 'bin'}"))
            except ct.AuthError as e:
                self.sig.preview_ready.emit(obj, "", str(e))
                self.sig.auth_required.emit(obj.platform)
                return
            except Exception as e:                   # noqa: BLE001
                self.sig.preview_ready.emit(obj, "", f"试听准备失败：{e}")
                return
            self.sig.preview_ready.emit(chart, path, "")

        self._thread(work)

    def _preview_local(self, chart, path: str) -> None:
        """把某谱面的音频加入试听队列并立即播放。"""
        key = (chart.platform, str(chart.chart_id))
        if key in self._preview_keys:
            self._preview_idx = next(
                i for i, c in enumerate(self._preview_items)
                if (c.platform, str(c.chart_id)) == key)
        else:
            self._preview_items.append(chart)
            self._preview_keys.add(key)
            self._preview_idx = len(self._preview_items) - 1
        self._preview_paths[key] = path
        self.player.play_path(path)
        self.now_playing_label.set_scrolling_text(f"正在试听：{chart.title}")
        self.cur_song_label.set_scrolling_text(chart.title)
        artist = chart.artist or ""
        self.cur_artist_label.set_scrolling_text(
            f"{artist} · {PLATFORM_CN[chart.platform]}" if artist else PLATFORM_CN[chart.platform])

    def _on_preview_ready(self, chart, path: str, error: str) -> None:
        self.loading_overlay.hide_loading()
        if error:
            self._on_log(error, True)
            return
        self._preview_local(chart, path)

    def _play_preview_at(self, idx: int) -> None:
        if not (0 <= idx < len(self._preview_items)):
            return
        chart = self._preview_items[idx]
        key = (chart.platform, str(chart.chart_id))
        path = self._preview_paths.get(key)
        if path and Path(path).exists():
            self._preview_idx = idx
            self.player.play_path(path)
            self.now_playing_label.set_scrolling_text(f"正在试听：{chart.title}")
            self.cur_song_label.set_scrolling_text(chart.title)
            self.cur_artist_label.set_scrolling_text(
                (chart.artist + " · " if chart.artist else "") + PLATFORM_CN[chart.platform])

    def _play_prev_preview(self) -> None:
        if self._preview_idx > 0:
            self._play_preview_at(self._preview_idx - 1)
        else:
            self._log("已是试听队列第一首", False)

    def _play_next_preview(self) -> None:
        if self._preview_idx + 1 < len(self._preview_items):
            self._play_preview_at(self._preview_idx + 1)
        elif self._preview_items:
            self._log("试听队列已到末尾", False)

    def _toggle_play_pause(self) -> None:
        if self.player.is_paused:
            self.player.resume()
        elif self.player.is_playing:
            self.player.pause()
        elif self._preview_items:
            self._play_preview_at(max(0, self._preview_idx))

    def _stop_preview(self) -> None:
        self.player.stop()
        self.now_playing_label.set_scrolling_text("已停止")
        self.progress_slider.setValue(0)
        self.time_label.setText("00:00")

    def _on_seek(self, value: int) -> None:
        if self.player.duration_ms > 0:
            self.player.set_pos(value)

    def _on_volume(self, percent: int) -> None:
        if self._is_muted and percent > 0:
            self._is_muted = False
        self.player.set_volume(percent / 100.0)

    def _show_volume_popup(self) -> None:
        # Qt.Popup 是顶级窗口，须用全局坐标：在音量按钮正上方弹出
        pos = self.volume_btn.mapToGlobal(QPoint(0, self.volume_btn.height()))
        popup_size = self.volume_popup.sizeHint()
        self.volume_popup.move(
            pos.x() + self.volume_btn.width() // 2 - popup_size.width() // 2,
            pos.y() - popup_size.height() - self.volume_btn.height() - 8)
        self.volume_popup.show()

    def _toggle_mute(self) -> None:
        if self._is_muted:
            self._is_muted = False
            self.player.set_volume(self._pre_mute_volume / 100.0)
            self.volume_popup.set_value(self._pre_mute_volume)
            self.volume_btn.setText("🔊")
        else:
            self._is_muted = True
            self._pre_mute_volume = int(self.player.volume * 100)
            self.player.set_volume(0.0)
            self.volume_btn.setText("🔇")

    def _on_tick(self) -> None:
        # 播放/暂停按钮态：播放中显示 ⏸（点击暂停），暂停/停止显示 ▶（点击播放）
        playing = self.player.is_playing and not self.player.is_paused
        self.play_pause_btn.setText("⏸\uFE0E" if playing else "▶\uFE0E")
        # 进度条（用户拖动中不回写）
        if self.player.duration_ms > 0:
            if self.progress_slider.maximum() != self.player.duration_ms:
                self.progress_slider.setRange(0, self.player.duration_ms)
                self.progress_slider.setEnabled(True)
            if not self.progress_slider.isSliderDown():
                self.progress_slider.setValue(self.player.current_pos)
            self.time_label.setText(
                f"{_fmt_ms(self.player.current_pos)} / {_fmt_ms(self.player.duration_ms)}")
        else:
            if self.progress_slider.maximum() != 0:
                self.progress_slider.setRange(0, 0)
                self.progress_slider.setEnabled(False)
            self.time_label.setText(_fmt_ms(self.player.current_pos))

    # ---------------------------------------------------------------- 登录
    def show_login_panel(self) -> None:
        if self._login_overlay is None:
            overlay = LoginOverlay(self._page, self.state)
            overlay.osu_cookie_ready.connect(self._on_osu_cookie)
            overlay.phira_cookie_ready.connect(self._on_phira_cookie)
            overlay.malody_logged_in.connect(self._on_malody_logged_in)
            overlay.selenium_confirm.connect(self._on_selenium_confirm)
            overlay.log.connect(self._on_log)
            self._login_overlay = overlay
        self._login_overlay.refresh_status()
        self._login_overlay.show()
        self._login_overlay.raise_()

    def _on_osu_cookie(self, cookie: str) -> None:
        self._save_state(osu_cookie=cookie)
        self.clients["osu"] = None
        self._after_login("osu", "Cookie 已保存（缓存于 ~/.chartunes/state.json）")

    def _on_phira_cookie(self, header: str) -> None:
        self._save_state(phira_cookie=header)
        self.clients["phira"] = None
        self._after_login("phira", "cookie 已保存")

    def _on_malody_logged_in(self, client) -> None:
        self._save_state(malody_key=client.key, malody_uid=client.uid)
        self.clients["malody"] = client
        self._after_login("malody", f"登录成功 uid={client.uid}（key 已缓存，不存密码）")

    def _after_login(self, platform: str, message: str) -> None:
        self._log(f"[{PLATFORM_CN[platform]}] {message}")
        self._on_state_changed()

    def _on_state_changed(self) -> None:
        self._refresh_platform_panel()
        if self._login_overlay is not None:
            self._login_overlay.refresh_status()

    def _on_auth_required(self, platform: str) -> None:
        self.show_login_panel()

    def _on_selenium_confirm(self, holder: dict, event: threading.Event) -> None:
        ok = QMessageBox.question(
            self, "浏览器登录",
            f"请在打开的浏览器中登录 {LOGIN_URL['phira']}，\n"
            "完成后点【Yes】收割 cookie；点【No】放弃。") == QMessageBox.Yes
        holder["ok"] = ok
        event.set()

    # ---------------------------------------------------------------- 日志
    def _on_log(self, msg: str, error: bool) -> None:
        self._log_buffer.append((msg, error))
        first_line = msg.splitlines()[0] if msg else ""
        self._status_label.setText(first_line)
        self._status_label.setStyleSheet(
            f"color: {'#c0392b' if error else TEXT_COLOR};")

    def _show_log_dialog(self) -> None:
        dlg = QDialog(self)
        dlg.setWindowTitle("运行日志")
        dlg.resize(680, 420)
        v = QVBoxLayout(dlg)
        text = QPlainTextEdit(dlg)
        text.setReadOnly(True)
        text.setStyleSheet(
            "font-family: Consolas, 'Microsoft YaHei'; font-size: 12px;")
        text.setPlainText("\n".join(
            ("[错误] " if err else "") + m for m, err in self._log_buffer))
        v.addWidget(text)
        close = QPushButton("关闭")
        close.clicked.connect(dlg.accept)
        v.addWidget(close)
        dlg.exec_()

    def _copy_id(self, text: str) -> None:
        QApplication.clipboard().setText(text)
        self.sig.copy_done.emit(text)

    # ---------------------------------------------------------------- 退出
    def closeEvent(self, event):
        self._closing = True
        self._queue_wakeup.set()
        self.player.shutdown()
        for d in list(self._drivers):
            try:
                d.quit()
            except Exception:                       # noqa: BLE001
                pass
        for c in self.clients.values():
            close = getattr(c, "close", None)
            if close:
                try:
                    close()
                except Exception:                   # noqa: BLE001
                    pass
        super().closeEvent(event)


def main() -> None:
    # 高 DPI / 跨屏缩放：这些属性必须在 QApplication 实例化之前设置，
    # 否则在缩放率不同的屏幕间拖动窗口会出现位图拉伸模糊、控件错位。
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setFont(QFont(FONT_FAMILY, 10))
    win = ChartunesWindow()
    win.show()
    app.exec_()


if __name__ == "__main__":
    main()
