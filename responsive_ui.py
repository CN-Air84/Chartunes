# coding=utf-8
"""Small helpers for uniformly scaling fixed-size Qt UIs.

Copied verbatim from YuanyueTTS (src/responsive_ui.py); no project-specific
dependencies beyond PyQt5 itself.
"""

from __future__ import annotations

import math
import re
from typing import Optional

from PyQt5.QtWidgets import QLayout, QWidget


class UniformUiScaler:
    """Scale a widget tree uniformly while keeping the layout responsive.

    Qt layouts resize expanding widgets automatically, but fixed dimensions and
    pixel/point values in style sheets do not follow the window size.  This
    helper records those values once and reapplies them using one uniform scale
    factor, so a page grows proportionally instead of only stretching its
    largest panel.
    """

    _NUMBER_WITH_UNIT = re.compile(r"(?P<value>-?\d+(?:\.\d+)?)\s*(?P<unit>px|pt)\b")
    _MAX_WIDGET_SIZE = 16_777_215

    def __init__(
        self,
        root: QWidget,
        base_width: int,
        base_height: int,
        *,
        minimum_scale: float = 1.0,
        maximum_scale: Optional[float] = None,
    ) -> None:
        self.root = root
        self.base_width = max(1, base_width)
        self.base_height = max(1, base_height)
        self.minimum_scale = max(0.1, minimum_scale)
        self.maximum_scale = maximum_scale
        self.scale = self.minimum_scale
        self._applying = False

    def scale_for_size(self, width: int, height: int) -> float:
        """Return a uniform scale based on the limiting page dimension."""
        scale = min(max(1, width) / self.base_width, max(1, height) / self.base_height)
        scale = max(self.minimum_scale, scale)
        if self.maximum_scale is not None:
            scale = min(self.maximum_scale, scale)
        return round(scale, 4)

    def apply(self, width: Optional[int] = None, height: Optional[int] = None) -> float:
        """Apply the current page scale and return it."""
        if self._applying:
            return self.scale

        width = self.root.width() if width is None else width
        height = self.root.height() if height is None else height
        scale = self.scale_for_size(width, height)

        self._applying = True
        try:
            self.scale = scale
            layouts = []
            root_layout = self.root.layout()
            if root_layout is not None:
                layouts.append(root_layout)
            layouts.extend(self.root.findChildren(QLayout))
            # findChildren can include a layout already added above.
            seen_layouts = set()
            for layout in layouts:
                if id(layout) in seen_layouts:
                    continue
                seen_layouts.add(id(layout))
                self._apply_layout(layout, scale)

            widgets = [self.root, *self.root.findChildren(QWidget)]
            for widget in widgets:
                self._apply_widget(widget, scale)
            self.root.updateGeometry()
        finally:
            self._applying = False
        return self.scale

    def apply_subtree(self, widget: QWidget) -> float:
        """Apply the current scale only to a dynamically inserted widget tree."""
        if widget is self.root:
            return self.apply()
        if self._applying:
            return self.scale

        self._applying = True
        try:
            layouts = []
            root_layout = widget.layout()
            if root_layout is not None:
                layouts.append(root_layout)
            layouts.extend(widget.findChildren(QLayout))
            seen_layouts = set()
            for layout in layouts:
                if id(layout) in seen_layouts:
                    continue
                seen_layouts.add(id(layout))
                self._apply_layout(layout, self.scale)

            for child in (widget, *widget.findChildren(QWidget)):
                self._apply_widget(child, self.scale)
            widget.updateGeometry()
        finally:
            self._applying = False
        return self.scale

    def _apply_layout(self, layout: QLayout, scale: float) -> None:
        base = getattr(layout, "_uniform_ui_base", None)
        if base is None:
            margins = layout.contentsMargins()
            base = (
                margins.left(),
                margins.top(),
                margins.right(),
                margins.bottom(),
                layout.spacing(),
            )
            layout._uniform_ui_base = base

        left, top, right, bottom, spacing = base
        layout.setContentsMargins(
            self._scaled_int(left, scale),
            self._scaled_int(top, scale),
            self._scaled_int(right, scale),
            self._scaled_int(bottom, scale),
        )
        if spacing >= 0:
            layout.setSpacing(self._scaled_int(spacing, scale))
        layout._uniform_ui_last = (layout.contentsMargins(), layout.spacing())

    def _apply_widget(self, widget: QWidget, scale: float) -> None:
        self._apply_dimensions(widget, scale)

        current_style = widget.styleSheet()
        base_style = getattr(widget, "_uniform_ui_base_style", None)
        # Never promote an already-scaled style sheet to the new baseline, or
        # fonts accumulate scale and cannot return to their original size.
        if base_style is None:
            base_style = current_style
            widget._uniform_ui_base_style = base_style

        scaled_style = self._scale_style_sheet(base_style, scale)
        if current_style != scaled_style:
            widget.setStyleSheet(scaled_style)
        widget._uniform_ui_last_style = scaled_style

    def set_base_style(self, widget: QWidget, style: str) -> None:
        """Replace a widget's unscaled style and apply the current scale."""
        widget._uniform_ui_base_style = style
        scaled_style = self._scale_style_sheet(style, self.scale)
        widget.setStyleSheet(scaled_style)
        widget._uniform_ui_last_style = scaled_style

    def _apply_dimensions(self, widget: QWidget, scale: float) -> None:
        current = (
            widget.minimumWidth(),
            widget.minimumHeight(),
            widget.maximumWidth(),
            widget.maximumHeight(),
        )
        base = getattr(widget, "_uniform_ui_base_dimensions", None)
        if base is None:
            base = current
            widget._uniform_ui_base_dimensions = base

        min_w, min_h, max_w, max_h = base
        target_min_w = self._scaled_dimension(min_w, scale)
        target_min_h = self._scaled_dimension(min_h, scale)
        target_max_w = self._scaled_dimension(max_w, scale, preserve_unbounded=True)
        target_max_h = self._scaled_dimension(max_h, scale, preserve_unbounded=True)

        # Clear constraints first so changing a fixed size never gets clamped
        # by the old size.  Unbounded dimensions remain unbounded.
        widget.setMinimumSize(0, 0)
        widget.setMaximumSize(self._MAX_WIDGET_SIZE, self._MAX_WIDGET_SIZE)
        widget.setMaximumWidth(target_max_w)
        widget.setMaximumHeight(target_max_h)
        widget.setMinimumWidth(target_min_w)
        widget.setMinimumHeight(target_min_h)
        widget._uniform_ui_last_dimensions = (
            widget.minimumWidth(),
            widget.minimumHeight(),
            widget.maximumWidth(),
            widget.maximumHeight(),
        )

    @classmethod
    def _scale_style_sheet(cls, style: str, scale: float) -> str:
        if not style or abs(scale - 1.0) < 0.0001:
            return style

        def replace(match: re.Match) -> str:
            value = float(match.group("value")) * scale
            # Keep related values valid after quantization. For example,
            # round(14*s)=19 and round(7*s)=10 makes a slider radius larger
            # than half its handle, which Qt renders as a square.
            rendered = str(math.floor(value))
            return f"{rendered}{match.group('unit')}"

        return cls._NUMBER_WITH_UNIT.sub(replace, style)

    @staticmethod
    def _scaled_int(value: int, scale: float) -> int:
        return max(0, int(round(value * scale)))

    @classmethod
    def _scaled_dimension(cls, value: int, scale: float, *, preserve_unbounded: bool = False) -> int:
        if preserve_unbounded and value >= cls._MAX_WIDGET_SIZE:
            return cls._MAX_WIDGET_SIZE
        return cls._scaled_int(value, scale)
