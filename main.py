#!/usr/bin/env python3
"""Target & Arrows - a multi-window sync demo.

The idea comes from MultiWindowSync-PyQt: every window is a viewport into one
shared canvas, kept in sync from each window's real position on screen.

Window 1 holds a target dot at its centre. Every window opened after it holds
an arrow that aims at that dot from wherever the window actually sits. Each
glyph is measured against min(width, height), so both the target and the
arrows rescale as you resize their windows.

    Ctrl+N  new arrow window      Ctrl+W / Esc  close this window      Ctrl+Q  quit
"""

from __future__ import annotations

import math
import sys
import time

from PyQt6.QtCore import QObject, QPoint, QPointF, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QKeySequence,
    QLinearGradient,
    QPainter,
    QPen,
    QPolygonF,
    QRadialGradient,
)
from PyQt6.QtWidgets import QApplication, QMenu, QWidget

MINT = QColor(62, 230, 168)
AMBER = QColor(255, 176, 32)
INK = QColor(14, 19, 26)
INK_DARK = QColor(8, 11, 16)
WHITE = QColor(255, 255, 255)
HUD = QColor(255, 255, 255, 130)

GRID_STEP = 44.0  # world units, shared by every window so the grid lines up
MIN_W, MIN_H = 170, 130
LOCK_RADIUS = 90.0  # screen px: closer than this and the arrow reads as locked
FRAME_MS = 33


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def wrap(value: float, step: float) -> float:
    return value - step * math.floor(value / step)


def fade(color: QColor, alpha: int) -> QColor:
    tinted = QColor(color)
    tinted.setAlpha(int(clamp(alpha, 0, 255)))
    return tinted


def mono_font(pixel_size: float, bold: bool = False) -> QFont:
    font = QFont()
    font.setFamilies(["Consolas", "SF Mono", "Menlo", "DejaVu Sans Mono", "Courier New"])
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPixelSize(max(9, int(round(pixel_size))))
    if bold:
        font.setWeight(QFont.Weight.DemiBold)
    return font


class Scene(QObject):
    """The shared world: owns every window and the coordinate origin."""

    changed = pyqtSignal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.windows: list[SyncWindow] = []
        self.target: TargetWindow | None = None
        self.origin: QPoint | None = None
        self._geometry: dict[SyncWindow, tuple] = {}
        self._clock = QTimer(self)
        self._clock.setInterval(FRAME_MS)
        self._clock.timeout.connect(self._tick)

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self.open_target()
        self.open_arrow()
        self.open_arrow()
        self._clock.start()

    def open_target(self) -> None:
        if self.target is not None:
            self.target.raise_()
            self.target.activateWindow()
            return
        area = QApplication.primaryScreen().availableGeometry()
        window = TargetWindow(self, 1)
        window.resize(
            int(clamp(area.width() * 0.42, MIN_W + 120, 560)),
            int(clamp(area.height() * 0.48, MIN_H + 100, 440)),
        )
        window.move(area.center().x() - window.width() // 2, area.center().y() - window.height() // 2)
        self.target = window
        self._add(window)

    def open_arrow(self) -> None:
        index = max((w.index for w in self.windows), default=0) + 1
        existing = len(self.arrows())
        window = ArrowWindow(self, index)
        window.resize(360, 300)
        x, y = self._arrow_position(existing, window)
        window.move(x, y)

        # A fresh window must not paint with a stale world offset.
        self._geometry.pop(window, None)
        self._add(window)

    def arrows(self) -> list[ArrowWindow]:
        return [w for w in self.windows if isinstance(w, ArrowWindow)]

    def _add(self, window: SyncWindow) -> None:
        window.show()
        if self.origin is None:
            # Any fixed origin works; the first window shown defines the world.
            self.origin = window.mapToGlobal(QPoint(0, 0))
        self.windows.append(window)
        self.changed.emit()

    def remove(self, window: SyncWindow) -> None:
        if window in self.windows:
            self.windows.remove(window)
        self._geometry.pop(window, None)
        if window is self.target:
            self.target = None
            QApplication.quit()
        self.changed.emit()

    def _arrow_position(self, existing: int, window: SyncWindow) -> tuple[int, int]:
        """Place new arrow windows beside / below the target, on screen."""
        screen = QApplication.primaryScreen()
        if self.target is not None:
            screen = QApplication.screenAt(self.target.frameGeometry().center()) or screen
        area = screen.availableGeometry()

        if self.target is None:
            x, y = area.left() + 80, area.top() + 80
        else:
            frame = self.target.frameGeometry()
            if existing == 0:
                x, y = frame.right() + 40, frame.top() + 24
            elif existing == 1:
                x, y = frame.left() + 40, frame.bottom() + 48
            else:
                step = 40 * (existing - 1)
                x, y = frame.right() + 40 + step, frame.top() + 24 + step
        x = clamp(x, area.left(), max(area.left(), area.right() - window.width()))
        y = clamp(y, area.top(), max(area.top(), area.bottom() - window.height()))
        return int(x), int(y)

    # -- shared world ------------------------------------------------------
    def offset(self, window: QWidget) -> QPointF:
        """Where this window's top-left corner sits in the shared world."""
        origin = self.origin or QPoint(0, 0)
        position = window.mapToGlobal(QPoint(0, 0))
        return QPointF(position.x() - origin.x(), position.y() - origin.y())

    def _tick(self) -> None:
        """Catch any move/resize the OS never reported as an event."""
        dirty = False
        for window in list(self.windows):
            key = (window.mapToGlobal(QPoint(0, 0)), window.width(), window.height())
            if self._geometry.get(window) != key:
                self._geometry[window] = key
                dirty = True
        if dirty:
            self.changed.emit()
        if self.target is not None:
            self.target.repaint()  # keeps the target's pulse alive


class SyncWindow(QWidget):
    """Window chrome, shared grid and HUD. Subclasses draw the glyph."""

    def __init__(self, scene: Scene, index: int, kind: str) -> None:
        super().__init__()
        self.scene = scene
        self.index = index
        self.kind = kind
        self.setWindowTitle(f"{kind} {index}")
        self.setMinimumSize(MIN_W, MIN_H)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)
        scene.changed.connect(self.repaint)

    # -- events ------------------------------------------------------------
    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().resizeEvent(event)
        self.scene.changed.emit()

    def moveEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().moveEvent(event)
        self.scene.changed.emit()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().closeEvent(event)
        self.scene.remove(self)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt naming
        sequence = QKeySequence(event.keyCombination())
        if sequence == QKeySequence("Ctrl+N"):
            self.scene.open_arrow()
        elif sequence in (QKeySequence("Ctrl+W"), QKeySequence("Escape")):
            self.close()
        elif sequence == QKeySequence("Ctrl+Q"):
            QApplication.quit()
        else:
            super().keyPressEvent(event)

    def _show_menu(self, position: QPoint) -> None:
        menu = QMenu(self)
        menu.addAction("New arrow window\tCtrl+N", self.scene.open_arrow)
        menu.addAction("Close this window\tCtrl+W", self.close)
        menu.addSeparator()
        menu.addAction("Quit\tCtrl+Q", QApplication.quit)
        menu.exec(self.mapToGlobal(position))

    # -- painting ----------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
        )
        rect = QRectF(self.rect())
        self._paint_background(painter, rect)
        self._paint_grid(painter, rect)
        self.paint_glyph(painter, rect)
        self._paint_hud(painter, rect)
        self._paint_border(painter, rect)
        painter.end()

    def paint_glyph(self, painter: QPainter, rect: QRectF) -> None:
        raise NotImplementedError

    def hud_title(self) -> str:
        return f"{self.kind} {self.index}"

    def hud_detail(self) -> str:
        return ""

    def hud_hint(self) -> str:
        return ""

    def _paint_background(self, painter: QPainter, rect: QRectF) -> None:
        gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
        gradient.setColorAt(0.0, QColor(16, 22, 30))
        gradient.setColorAt(1.0, INK_DARK)
        painter.fillRect(rect, gradient)
        glow = QRadialGradient(rect.center(), max(rect.width(), rect.height()) * 0.8)
        glow.setColorAt(0.0, QColor(90, 160, 255, 22))
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.fillRect(rect, glow)

    def _paint_grid(self, painter: QPainter, rect: QRectF) -> None:
        """World-locked grid: the same lines continue across every window."""
        offset = self.scene.offset(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        for axis, span in (("x", rect.width()), ("y", rect.height())):
            start = -wrap(offset.x() if axis == "x" else offset.y(), GRID_STEP)
            index = math.floor((offset.x() if axis == "x" else offset.y()) / GRID_STEP)
            cursor = start
            while cursor < span:
                painter.setPen(QPen(fade(WHITE, 26 if index % 4 == 0 else 10), 1))
                if axis == "x":
                    painter.drawLine(QPointF(cursor + 0.5, 0), QPointF(cursor + 0.5, span))
                else:
                    painter.drawLine(QPointF(0, cursor + 0.5), QPointF(span, cursor + 0.5))
                cursor += GRID_STEP
                index += 1

        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    def _paint_hud(self, painter: QPainter, rect: QRectF) -> None:
        side = min(rect.width(), rect.height())
        if side < 120:
            return

        pad = clamp(side * 0.05, 10.0, 18.0)
        size = clamp(side * 0.045, 9.0, 14.0)
        top = QRectF(pad, pad, rect.width() - 2 * pad, size * 1.7)

        painter.setFont(mono_font(size, bold=True))
        painter.setPen(HUD)
        painter.drawText(
            top, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), self.hud_title()
        )
        detail = self.hud_detail()
        if detail:
            painter.drawText(
                top, int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), detail
            )

        hint = self.hud_hint()
        if hint and rect.height() > 210:
            painter.setFont(mono_font(size * 0.85))
            painter.setPen(fade(WHITE, 70))
            bottom = QRectF(pad, rect.height() - pad - size * 1.8, rect.width() - 2 * pad, size * 1.7)
            painter.drawText(
                bottom, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), hint
            )

    def _paint_border(self, painter: QPainter, rect: QRectF) -> None:
        radius = clamp(min(rect.width(), rect.height()) * 0.06, 6.0, 18.0)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(fade(WHITE, 34), 1))
        painter.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)


class TargetWindow(SyncWindow):
    """The main window: concentric rings and a dot at its own centre."""

    def __init__(self, scene: Scene, index: int) -> None:
        super().__init__(scene, index, "TARGET")

    def hud_detail(self) -> str:
        count = len(self.scene.arrows())
        return f"{count} ARROW{'S' if count != 1 else ''}"

    def hud_hint(self) -> str:
        return "Ctrl+N opens an aiming window"

    def paint_glyph(self, painter: QPainter, rect: QRectF) -> None:
        centre = rect.center()
        side = min(rect.width(), rect.height())
        radius = clamp(side * 0.10, 12.0, 110.0)
        pulse = 1.0 + 0.06 * math.sin(time.monotonic() * 2.4)
        stroke = max(1.2, side * 0.006)

        # Crosshair through the whole window.
        painter.setPen(QPen(fade(WHITE, 16), 1))
        painter.drawLine(QPointF(rect.left(), centre.y()), QPointF(rect.right(), centre.y()))
        painter.drawLine(QPointF(centre.x(), rect.top()), QPointF(centre.x(), rect.bottom()))

        # Outer breathing ring with four range ticks.
        ring = radius * 2.2 * pulse
        pen = QPen(fade(MINT, 48), max(1.0, side * 0.004))
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setDashPattern([5, 7])
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(centre, ring, ring)

        painter.setPen(QPen(fade(MINT, 120), max(1.0, side * 0.006)))
        tick = radius * 0.3
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            start = QPointF(centre.x() + dx * ring, centre.y() + dy * ring)
            painter.drawLine(start, QPointF(start.x() + dx * tick, start.y() + dy * tick))

        # Middle ring.
        painter.setPen(QPen(fade(MINT, 80), max(1.0, side * 0.005)))
        painter.drawEllipse(centre, radius * 1.5, radius * 1.5)

        # Glow under the core.
        glow = QRadialGradient(centre, radius * 2.0)
        glow.setColorAt(0.0, fade(MINT, 130))
        glow.setColorAt(0.35, fade(MINT, 40))
        glow.setColorAt(1.0, fade(MINT, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(centre, radius * 2.0, radius * 2.0)

        # Core ring and dot.
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(MINT, stroke))
        painter.drawEllipse(centre, radius * 1.05 * pulse, radius * 1.05 * pulse)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(MINT)
        painter.drawEllipse(centre, radius * 0.34, radius * 0.34)
        painter.setBrush(QColor(240, 255, 250))
        painter.drawEllipse(centre, radius * 0.13, radius * 0.13)


class ArrowWindow(SyncWindow):
    """Every later window: an arrow at its centre aimed at the target."""

    def __init__(self, scene: Scene, index: int) -> None:
        super().__init__(scene, index, "ARROW")

    def aim(self) -> tuple[float, float, float] | None:
        """(angle in radians, screen distance, compass bearing) toward the target."""
        target = self.scene.target
        if target is None:
            return None
        here = self.mapToGlobal(self.rect().center())
        there = target.mapToGlobal(target.rect().center())
        dx = float(there.x() - here.x())
        dy = float(there.y() - here.y())
        angle = math.atan2(dy, dx)
        distance = math.hypot(dx, dy)
        bearing = (math.degrees(math.atan2(dx, -dy)) + 360.0) % 360.0
        return angle, distance, bearing

    def hud_detail(self) -> str:
        aim = self.aim()
        if aim is None:
            return "NO TARGET"
        _, distance, bearing = aim
        if distance < LOCK_RADIUS:
            return f"{bearing:03.0f}°  LOCKED"
        return f"{bearing:03.0f}°  {distance:.0f} px"

    def paint_glyph(self, painter: QPainter, rect: QRectF) -> None:
        aim = self.aim()
        if aim is None:
            return
        angle, distance, _ = aim
        centre = rect.center()
        side = min(rect.width(), rect.height())
        color = MINT if distance < LOCK_RADIUS else AMBER

        span = clamp(side * 0.60, 26.0, 300.0)
        dx, dy = math.cos(angle), math.sin(angle)
        nx, ny = -dy, dx
        tail = QPointF(centre.x() - dx * span * 0.45, centre.y() - dy * span * 0.45)
        tip = QPointF(centre.x() + dx * span * 0.55, centre.y() + dy * span * 0.55)

        # Dashed sight line and compass ring.
        reach = math.hypot(rect.width(), rect.height())
        pen = QPen(fade(color, 36), 1)
        pen.setStyle(Qt.PenStyle.DashLine)
        pen.setDashPattern([6, 8])
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawLine(
            QPointF(centre.x() - dx * reach, centre.y() - dy * reach),
            QPointF(centre.x() + dx * reach, centre.y() + dy * reach),
        )
        painter.setPen(QPen(fade(WHITE, 20), 1))
        painter.drawEllipse(centre, span * 0.66, span * 0.66)

        # Glow around the tip.
        glow = QRadialGradient(tip, span * 0.28)
        glow.setColorAt(0.0, fade(color, 95))
        glow.setColorAt(1.0, fade(color, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(tip, span * 0.28, span * 0.28)

        # Shaft, stopping at the base of the head.
        head, half = span * 0.26, span * 0.13
        base = QPointF(tip.x() - dx * head, tip.y() - dy * head)
        pen = QPen(color, clamp(side * 0.018, 2.0, 7.0))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawLine(tail, base)

        # Arrow head.
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawPolygon(
            QPolygonF(
                [
                    tip,
                    QPointF(base.x() + nx * half, base.y() + ny * half),
                    QPointF(base.x() - nx * half, base.y() - ny * half),
                ]
            )
        )

        # Pivot at the window centre.
        pivot = clamp(side * 0.035, 3.0, 12.0)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(fade(color, 95), max(1.0, side * 0.004)))
        painter.drawEllipse(centre, pivot, pivot)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fade(color, 210))
        painter.drawEllipse(centre, pivot * 0.35, pivot * 0.35)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Target & Arrows")
    scene = Scene(app)
    scene.start()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
