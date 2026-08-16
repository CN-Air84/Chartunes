# -*- coding: utf-8 -*-
"""YuanyueTTS 主题适配层（theme_page_adapter）的 no-op 桩。

从流媒体选项卡 skid 过来的控件代码里到处调用 theme_page_adapter 的
configure_* 函数（原实现只给控件打动态属性标记，供 theme_manager /
theme_effects 消费）。CharTunes 没有主题引擎，这里提供同名 no-op
实现，让搬来的控件代码零改动可用。
"""
from __future__ import annotations


def configure_transparent_root(widget) -> None:
    """原：把页面标记为透明根。现：no-op。"""


def configure_theme_card(card, preserve_outline: bool = False) -> None:
    """原：把容器标记为主题卡片。现：no-op。"""


def configure_transparent_container(widget) -> None:
    """原：把容器标记为透明层。现：no-op。"""


def configure_semantic_surface(widget) -> None:
    """原：把控件标记为语义表面。现：no-op。"""


def configure_material_overlay(widget) -> None:
    """原：把悬浮层标记为材质遮罩。现：no-op。"""


def configure_independent_surface(widget) -> None:
    """原：把独立窗口标记为自绘表面。现：no-op。"""


def set_transparent_scroll_content(scroll_area, content) -> None:
    """原：把滚动区内容标记为透明。现：no-op。"""
