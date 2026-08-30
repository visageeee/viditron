from pathlib import Path
import shlex
import re
import subprocess
import threading
import time
import tempfile
import shutil

import gi

gi.require_version('Gtk', '4.0')

from gi.repository import Gtk, Gdk, Gio, Pango, GLib

from .media import probe_video
from .portal import PortalFileChooser


def format_timecode(seconds):
    seconds = max(0.0, float(seconds or 0.0))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f'{hours:02d}:{minutes:02d}:{secs:06.3f}'


def format_short_timecode(seconds):
    seconds = max(0.0, float(seconds or 0.0))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f'{hours:02d}:{minutes:02d}:{secs:04.1f}'


def parse_timecode(text):
    text = text.strip()
    if not text:
        raise ValueError('empty timecode')
    parts = text.split(':')
    try:
        if len(parts) == 3:
            hours, minutes, seconds = float(parts[0]), float(parts[1]), float(parts[2])
            return hours * 3600 + minutes * 60 + seconds
        if len(parts) == 2:
            minutes, seconds = float(parts[0]), float(parts[1])
            return minutes * 60 + seconds
        return float(parts[0])
    except ValueError as exc:
        raise ValueError(f'Invalid timecode: {text}') from exc


TAB_ICON_SIZE = 64
TAB_BAR_HEIGHT = 64


class CropOverlay(Gtk.DrawingArea):
    HANDLE = 9
    MIN_SIZE = 8

    def __init__(self, changed_callback):
        super().__init__()
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_draw_func(self._draw)
        self.set_can_target(False)

        self.source_width = 0
        self.source_height = 0
        self.crop = [0.0, 0.0, 0.0, 0.0]
        self.changed_callback = changed_callback
        self.drag_mode = None
        self.drag_start_crop = None
        self.aspect_ratio = None

        drag = Gtk.GestureDrag()
        drag.connect('drag-begin', self._drag_begin)
        drag.connect('drag-update', self._drag_update)
        drag.connect('drag-end', self._drag_end)
        self.add_controller(drag)

    def set_source(self, width, height):
        self.source_width = int(width or 0)
        self.source_height = int(height or 0)
        if self.source_width and self.source_height:
            self.crop = [0.0, 0.0, float(self.source_width), float(self.source_height)]
        self.queue_draw()

    def set_active(self, active):
        self.set_visible(active)
        self.set_can_target(active)
        self.queue_draw()

    def set_crop(self, x, y, width, height, notify=True):
        if not self.source_width or not self.source_height:
            return
        width = max(self.MIN_SIZE, min(float(width), self.source_width))
        height = max(self.MIN_SIZE, min(float(height), self.source_height))
        x = max(0.0, min(float(x), self.source_width - width))
        y = max(0.0, min(float(y), self.source_height - height))
        self.crop = [x, y, width, height]
        self.queue_draw()
        if notify:
            self.changed_callback(*self.crop)

    def set_aspect_ratio(self, ratio):
        self.aspect_ratio = float(ratio) if ratio else None

    def _fit_ratio(self, x, y, width, height, mode):
        ratio = self.aspect_ratio
        if not ratio:
            return x, y, width, height

        old_x, old_y, old_w, old_h = self.drag_start_crop or self.crop
        old_right = old_x + old_w
        old_bottom = old_y + old_h

        # Horizontal edge drags drive width; vertical edge drags drive height.
        if mode in ('e', 'w'):
            height = width / ratio
            cy = old_y + old_h / 2.0
            y = cy - height / 2.0
        elif mode in ('n', 's'):
            width = height * ratio
            cx = old_x + old_w / 2.0
            x = cx - width / 2.0
        else:
            # Corner drags choose whichever dimension changed proportionally more.
            dw = abs(width - old_w) / max(1.0, old_w)
            dh = abs(height - old_h) / max(1.0, old_h)
            if dw >= dh:
                height = width / ratio
            else:
                width = height * ratio

        # Keep the opposite corner/edge anchored where practical.
        if 'w' in mode:
            x = old_right - width
        elif 'e' in mode:
            x = old_x
        if 'n' in mode:
            y = old_bottom - height
        elif 's' in mode:
            y = old_y

        # Scale down if the constrained box would exceed the source.
        if width > self.source_width:
            width = float(self.source_width)
            height = width / ratio
        if height > self.source_height:
            height = float(self.source_height)
            width = height * ratio

        x = max(0.0, min(x, self.source_width - width))
        y = max(0.0, min(y, self.source_height - height))
        return x, y, width, height

    def _video_rect(self, widget_w, widget_h):
        if not self.source_width or not self.source_height or widget_w <= 0 or widget_h <= 0:
            return (0.0, 0.0, float(widget_w), float(widget_h))
        source_ratio = self.source_width / self.source_height
        widget_ratio = widget_w / widget_h
        if widget_ratio > source_ratio:
            h = float(widget_h)
            w = h * source_ratio
            return ((widget_w - w) / 2.0, 0.0, w, h)
        w = float(widget_w)
        h = w / source_ratio
        return (0.0, (widget_h - h) / 2.0, w, h)

    def _source_to_widget(self, x, y, w, h):
        vx, vy, vw, vh = self._video_rect(self.get_width(), self.get_height())
        if not self.source_width or not self.source_height:
            return vx, vy, vw, vh
        return (
            vx + x / self.source_width * vw,
            vy + y / self.source_height * vh,
            w / self.source_width * vw,
            h / self.source_height * vh,
        )

    def _widget_delta_to_source(self, dx, dy):
        _, _, vw, vh = self._video_rect(self.get_width(), self.get_height())
        if vw <= 0 or vh <= 0:
            return 0.0, 0.0
        return dx / vw * self.source_width, dy / vh * self.source_height

    def _draw(self, area, cr, width, height):
        if not self.source_width or not self.source_height:
            return
        x, y, w, h = self._source_to_widget(*self.crop)
        vx, vy, vw, vh = self._video_rect(width, height)

        # Shade the region that will be discarded.
        cr.set_source_rgba(0, 0, 0, 0.58)
        cr.rectangle(vx, vy, vw, max(0, y - vy))
        cr.rectangle(vx, y + h, vw, max(0, vy + vh - (y + h)))
        cr.rectangle(vx, y, max(0, x - vx), h)
        cr.rectangle(x + w, y, max(0, vx + vw - (x + w)), h)
        cr.fill()

        cr.set_source_rgba(1, 1, 1, 0.95)
        cr.set_line_width(2)
        cr.rectangle(x, y, w, h)
        cr.stroke()

        points = [
            (x, y), (x + w / 2, y), (x + w, y),
            (x, y + h / 2), (x + w, y + h / 2),
            (x, y + h), (x + w / 2, y + h), (x + w, y + h),
        ]
        r = self.HANDLE / 2
        for px, py in points:
            cr.rectangle(px - r, py - r, self.HANDLE, self.HANDLE)
            cr.fill()

    def _pick_mode(self, px, py):
        x, y, w, h = self._source_to_widget(*self.crop)
        tol = 14
        left = abs(px - x) <= tol
        right = abs(px - (x + w)) <= tol
        top = abs(py - y) <= tol
        bottom = abs(py - (y + h)) <= tol

        if left and top:
            return 'nw'
        if right and top:
            return 'ne'
        if left and bottom:
            return 'sw'
        if right and bottom:
            return 'se'
        if top and x - tol <= px <= x + w + tol:
            return 'n'
        if bottom and x - tol <= px <= x + w + tol:
            return 's'
        if left and y - tol <= py <= y + h + tol:
            return 'w'
        if right and y - tol <= py <= y + h + tol:
            return 'e'
        if x <= px <= x + w and y <= py <= y + h:
            return 'move'
        return None

    def _drag_begin(self, gesture, x, y):
        self.drag_mode = self._pick_mode(x, y)
        self.drag_start_crop = list(self.crop)

    def _drag_update(self, gesture, dx, dy):
        if not self.drag_mode or not self.drag_start_crop:
            return
        sx, sy = self._widget_delta_to_source(dx, dy)
        x, y, w, h = self.drag_start_crop
        right = x + w
        bottom = y + h

        if self.drag_mode == 'move':
            self.set_crop(x + sx, y + sy, w, h)
            return

        if 'w' in self.drag_mode:
            x = max(0.0, min(x + sx, right - self.MIN_SIZE))
        if 'e' in self.drag_mode:
            right = min(float(self.source_width), max(right + sx, x + self.MIN_SIZE))
        if 'n' in self.drag_mode:
            y = max(0.0, min(y + sy, bottom - self.MIN_SIZE))
        if 's' in self.drag_mode:
            bottom = min(float(self.source_height), max(bottom + sy, y + self.MIN_SIZE))

        x, y, width, height = self._fit_ratio(x, y, right - x, bottom - y, self.drag_mode)
        self.set_crop(x, y, width, height)

    def _drag_end(self, gesture, dx, dy):
        self.drag_mode = None
        self.drag_start_crop = None
        self.changed_callback(*self.crop)


CENSOR_PALETTE = (
    (0.95, 0.35, 0.35),
    (0.35, 0.70, 0.95),
    (0.45, 0.82, 0.45),
    (0.95, 0.70, 0.30),
    (0.72, 0.48, 0.92),
    (0.30, 0.82, 0.78),
)


class CensorOverlay(Gtk.DrawingArea):
    """Interactive overlay for timed solid black censorship rectangles."""

    HANDLE = 9
    MIN_SIZE = 8

    def __init__(self, changed_callback, select_callback=None, create_callback=None):
        super().__init__()
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_draw_func(self._draw)
        self.set_can_target(False)

        self.source_width = 0
        self.source_height = 0
        self.bars = []
        self.selected_index = None
        self.current_time = 0.0
        self.edit_mode = False

        self.changed_callback = changed_callback
        self.select_callback = select_callback
        self.create_callback = create_callback

        self.drag_mode = None
        self.drag_index = None
        self.drag_start_rect = None
        self.create_start = None
        self.create_preview = None

        drag = Gtk.GestureDrag()
        drag.connect('drag-begin', self._drag_begin)
        drag.connect('drag-update', self._drag_update)
        drag.connect('drag-end', self._drag_end)
        self.add_controller(drag)

    def set_source(self, width, height):
        self.source_width = int(width or 0)
        self.source_height = int(height or 0)
        self.queue_draw()

    def set_state(self, bars, selected_index=None):
        self.bars = bars
        self.selected_index = selected_index
        self.set_visible(bool(self.source_width and self.source_height and (bars or self.edit_mode)))
        self.queue_draw()

    def set_time(self, seconds):
        self.current_time = max(0.0, float(seconds or 0.0))
        if self.bars:
            self.queue_draw()

    def set_edit_mode(self, active):
        self.edit_mode = bool(active)
        self.set_can_target(self.edit_mode and bool(self.source_width and self.source_height))
        self.set_visible(bool(self.source_width and self.source_height and (self.bars or self.edit_mode)))
        self.queue_draw()

    def set_active(self, active):
        self.set_edit_mode(active)

    def set_rect(self, x, y, width, height, notify=True):
        index = self.selected_index
        if index is None or not (0 <= index < len(self.bars)):
            return
        self._set_bar_rect(index, x, y, width, height, notify=notify)

    def _set_bar_rect(self, index, x, y, width, height, notify=True):
        if not self.source_width or not self.source_height:
            return
        width = max(self.MIN_SIZE, min(float(width), self.source_width))
        height = max(self.MIN_SIZE, min(float(height), self.source_height))
        x = max(0.0, min(float(x), self.source_width - width))
        y = max(0.0, min(float(y), self.source_height - height))
        bar = self.bars[index]
        bar['x'] = int(round(x)); bar['y'] = int(round(y))
        bar['w'] = int(round(width)); bar['h'] = int(round(height))
        self.queue_draw()
        if notify:
            self.changed_callback(index, bar['x'], bar['y'], bar['w'], bar['h'])

    def _video_rect(self, widget_w, widget_h):
        if not self.source_width or not self.source_height or widget_w <= 0 or widget_h <= 0:
            return (0.0, 0.0, float(widget_w), float(widget_h))
        source_ratio = self.source_width / self.source_height
        widget_ratio = widget_w / widget_h
        if widget_ratio > source_ratio:
            h = float(widget_h); w = h * source_ratio
            return ((widget_w - w) / 2.0, 0.0, w, h)
        w = float(widget_w); h = w / source_ratio
        return (0.0, (widget_h - h) / 2.0, w, h)

    def _source_to_widget(self, x, y, w, h):
        vx, vy, vw, vh = self._video_rect(self.get_width(), self.get_height())
        if not self.source_width or not self.source_height:
            return vx, vy, vw, vh
        return (vx + x / self.source_width * vw,
                vy + y / self.source_height * vh,
                w / self.source_width * vw,
                h / self.source_height * vh)

    def _widget_to_source_point(self, x, y):
        vx, vy, vw, vh = self._video_rect(self.get_width(), self.get_height())
        if vw <= 0 or vh <= 0:
            return 0.0, 0.0
        sx = (x - vx) / vw * self.source_width
        sy = (y - vy) / vh * self.source_height
        return (max(0.0, min(float(self.source_width), sx)),
                max(0.0, min(float(self.source_height), sy)))

    def _widget_delta_to_source(self, dx, dy):
        _, _, vw, vh = self._video_rect(self.get_width(), self.get_height())
        if vw <= 0 or vh <= 0:
            return 0.0, 0.0
        return dx / vw * self.source_width, dy / vh * self.source_height

    def _bar_visible_now(self, bar):
        return float(bar['start']) <= self.current_time <= float(bar['end'])

    def _draw_rect(self, cr, rect, selected=False, color=None):
        x, y, w, h = self._source_to_widget(*rect)
        cr.set_source_rgba(0, 0, 0, 1)
        cr.rectangle(x, y, w, h)
        cr.fill()
        if selected and self.edit_mode:
            r_col, g_col, b_col = color or (1.0, 1.0, 1.0)
            cr.set_source_rgba(r_col, g_col, b_col, 1.0)
            cr.set_line_width(2)
            cr.rectangle(x, y, w, h)
            cr.stroke()
            r = self.HANDLE / 2
            for px, py in ((x, y), (x + w / 2, y), (x + w, y),
                           (x, y + h / 2), (x + w, y + h / 2),
                           (x, y + h), (x + w / 2, y + h), (x + w, y + h)):
                cr.rectangle(px - r, py - r, self.HANDLE, self.HANDLE)
                cr.fill()

    def _draw(self, area, cr, width, height):
        if not self.source_width or not self.source_height:
            return
        for index, bar in enumerate(self.bars):
            selected = index == self.selected_index
            if self._bar_visible_now(bar) or (self.edit_mode and selected):
                color = CENSOR_PALETTE[index % len(CENSOR_PALETTE)]
                self._draw_rect(
                    cr, (bar['x'], bar['y'], bar['w'], bar['h']),
                    selected, color,
                )
        if self.create_preview is not None:
            index = len(self.bars)
            color = CENSOR_PALETTE[index % len(CENSOR_PALETTE)]
            self._draw_rect(cr, self.create_preview, True, color)

    def _pick_mode_for_rect(self, rect, px, py):
        x, y, w, h = self._source_to_widget(*rect)
        tol = 14
        left = abs(px - x) <= tol; right = abs(px - (x + w)) <= tol
        top = abs(py - y) <= tol; bottom = abs(py - (y + h)) <= tol
        if left and top: return 'nw'
        if right and top: return 'ne'
        if left and bottom: return 'sw'
        if right and bottom: return 'se'
        if top and x - tol <= px <= x + w + tol: return 'n'
        if bottom and x - tol <= px <= x + w + tol: return 's'
        if left and y - tol <= py <= y + h + tol: return 'w'
        if right and y - tol <= py <= y + h + tol: return 'e'
        if x <= px <= x + w and y <= py <= y + h: return 'move'
        return None

    def _pick_bar(self, px, py):
        order = []
        if self.selected_index is not None and 0 <= self.selected_index < len(self.bars):
            order.append(self.selected_index)
        order.extend(i for i in reversed(range(len(self.bars))) if i not in order)
        for index in order:
            bar = self.bars[index]
            if not self._bar_visible_now(bar) and not (self.edit_mode and index == self.selected_index):
                continue
            mode = self._pick_mode_for_rect((bar['x'], bar['y'], bar['w'], bar['h']), px, py)
            if mode:
                return index, mode
        return None, None

    def _drag_begin(self, gesture, x, y):
        if not self.edit_mode:
            return
        index, mode = self._pick_bar(x, y)
        if index is not None:
            self.drag_index = index
            self.drag_mode = mode
            bar = self.bars[index]
            self.drag_start_rect = [float(bar['x']), float(bar['y']), float(bar['w']), float(bar['h'])]
            if index != self.selected_index:
                self.selected_index = index
                if self.select_callback:
                    self.select_callback(index)
                self.queue_draw()
            return
        sx, sy = self._widget_to_source_point(x, y)
        self.drag_mode = 'create'
        self.create_start = (sx, sy)
        self.create_preview = [sx, sy, 1.0, 1.0]
        self.queue_draw()

    def _drag_update(self, gesture, dx, dy):
        if self.drag_mode == 'create' and self.create_start is not None:
            x0, y0 = self.create_start
            sx, sy = self._widget_delta_to_source(dx, dy)
            x1, y1 = x0 + sx, y0 + sy
            x = max(0.0, min(x0, x1)); y = max(0.0, min(y0, y1))
            right = min(float(self.source_width), max(x0, x1))
            bottom = min(float(self.source_height), max(y0, y1))
            self.create_preview = [x, y, max(1.0, right - x), max(1.0, bottom - y)]
            self.queue_draw()
            return
        if self.drag_index is None or not self.drag_start_rect:
            return
        sx, sy = self._widget_delta_to_source(dx, dy)
        x, y, w, h = self.drag_start_rect
        right, bottom = x + w, y + h
        if self.drag_mode == 'move':
            self._set_bar_rect(self.drag_index, x + sx, y + sy, w, h)
            return
        if 'w' in self.drag_mode: x = max(0.0, min(x + sx, right - self.MIN_SIZE))
        if 'e' in self.drag_mode: right = min(float(self.source_width), max(right + sx, x + self.MIN_SIZE))
        if 'n' in self.drag_mode: y = max(0.0, min(y + sy, bottom - self.MIN_SIZE))
        if 's' in self.drag_mode: bottom = min(float(self.source_height), max(bottom + sy, y + self.MIN_SIZE))
        self._set_bar_rect(self.drag_index, x, y, right - x, bottom - y)

    def _drag_end(self, gesture, dx, dy):
        if self.drag_mode == 'create':
            preview = self.create_preview
            self.create_start = None; self.create_preview = None; self.drag_mode = None
            if preview is not None:
                x, y, w, h = preview
                if w >= self.MIN_SIZE and h >= self.MIN_SIZE and self.create_callback:
                    self.create_callback(int(round(x)), int(round(y)), int(round(w)), int(round(h)))
            self.queue_draw()
            return
        self.drag_mode = None
        self.drag_index = None
        self.drag_start_rect = None


class CensorTimeline(Gtk.DrawingArea):
    """Stacked, color-coded time ranges for censorship bars."""

    HANDLE_WIDTH = 10
    ROW_HEIGHT = 16
    TOP_PAD = 4
    PALETTE = CENSOR_PALETTE

    def __init__(self, select_callback, change_callback):
        super().__init__()
        self.set_hexpand(True)
        self.set_content_height(0)
        self.set_draw_func(self._draw)
        self.duration = 0.0
        self.bars = []
        self.selected_index = None
        self.select_callback = select_callback
        self.change_callback = change_callback
        self.drag_index = None
        self.drag_handle = None
        self.drag_start = None

        click = Gtk.GestureClick()
        click.connect('pressed', self._click_pressed)
        self.add_controller(click)
        drag = Gtk.GestureDrag()
        drag.connect('drag-begin', self._drag_begin)
        drag.connect('drag-update', self._drag_update)
        drag.connect('drag-end', self._drag_end)
        self.add_controller(drag)

    def set_state(self, bars, duration, selected_index=None):
        self.bars = bars
        self.duration = max(0.0, float(duration or 0.0))
        self.selected_index = selected_index
        rows = min(max(len(bars), 0), 6)
        self.set_content_height(0 if rows == 0 else self.TOP_PAD * 2 + rows * self.ROW_HEIGHT)
        self.set_visible(bool(rows))
        self.queue_draw()

    def _x_for_time(self, seconds):
        usable = max(1, self.get_width() - 24)
        return 12 + (seconds / self.duration * usable if self.duration else 0)

    def _time_delta(self, dx):
        usable = max(1, self.get_width() - 24)
        return dx / usable * self.duration if self.duration else 0.0

    def _row_y(self, index):
        return self.TOP_PAD + index * self.ROW_HEIGHT

    def _draw(self, area, cr, width, height):
        if self.duration <= 0:
            return
        for index, bar in enumerate(self.bars[:6]):
            y = self._row_y(index)
            x1 = self._x_for_time(bar['start'])
            x2 = self._x_for_time(bar['end'])
            r, g, b = self.PALETTE[index % len(self.PALETTE)]

            # Thin muted guide line across the whole duration.
            cr.set_source_rgba(r, g, b, 0.20)
            cr.rectangle(12, y + 5, max(1, width - 24), 6)
            cr.fill()

            # Active censor range.
            cr.set_source_rgba(r, g, b, 0.90)
            cr.rectangle(x1, y + 3, max(2, x2 - x1), 10)
            cr.fill()

            # Handles.
            cr.set_source_rgba(r, g, b, 1.0)
            for x in (x1, x2):
                cr.rectangle(x - self.HANDLE_WIDTH / 2, y, self.HANDLE_WIDTH, 16)
                cr.fill()

            if index == self.selected_index:
                cr.set_source_rgba(1, 1, 1, 0.95)
                cr.set_line_width(1.5)
                cr.rectangle(x1 - 1, y + 2, max(3, x2 - x1 + 2), 12)
                cr.stroke()

    def _pick(self, x, y):
        if self.duration <= 0:
            return None, None
        index = int((y - self.TOP_PAD) // self.ROW_HEIGHT)
        if not (0 <= index < min(len(self.bars), 6)):
            return None, None
        bar = self.bars[index]
        x1 = self._x_for_time(bar['start'])
        x2 = self._x_for_time(bar['end'])
        if abs(x - x1) <= 10:
            return index, 'start'
        if abs(x - x2) <= 10:
            return index, 'end'
        if x1 <= x <= x2:
            return index, 'range'
        return index, None

    def _click_pressed(self, gesture, n_press, x, y):
        index, _ = self._pick(x, y)
        if index is not None:
            self.selected_index = index
            self.select_callback(index)
            self.queue_draw()

    def _drag_begin(self, gesture, x, y):
        index, handle = self._pick(x, y)
        if index is None or handle not in ('start', 'end'):
            return
        self.drag_index = index
        self.drag_handle = handle
        bar = self.bars[index]
        self.drag_start = (float(bar['start']), float(bar['end']))
        self.selected_index = index
        self.select_callback(index)

    def _drag_update(self, gesture, dx, dy):
        if self.drag_index is None or not self.drag_start:
            return
        start0, end0 = self.drag_start
        delta = self._time_delta(dx)
        minimum = min(0.05, self.duration) if self.duration else 0.0
        if self.drag_handle == 'start':
            start = max(0.0, min(start0 + delta, end0 - minimum))
            end = end0
        else:
            start = start0
            end = min(self.duration, max(end0 + delta, start0 + minimum))
        self.change_callback(self.drag_index, start, end)
        self.queue_draw()

    def _drag_end(self, gesture, dx, dy):
        self.drag_index = None
        self.drag_handle = None
        self.drag_start = None


class UnifiedTimeline(Gtk.DrawingArea):
    HANDLE_WIDTH = 10

    def __init__(self, trim_callback, seek_callback):
        super().__init__()
        self.set_content_height(42)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)
        self.duration = 0.0
        self.start = 0.0
        self.end = 0.0
        self.position = 0.0
        self.trim_callback = trim_callback
        self.seek_callback = seek_callback
        self.drag_handle = None
        self.drag_start = None

        click = Gtk.GestureClick()
        click.connect('pressed', self._click_pressed)
        self.add_controller(click)
        drag = Gtk.GestureDrag()
        drag.connect('drag-begin', self._drag_begin)
        drag.connect('drag-update', self._drag_update)
        drag.connect('drag-end', self._drag_end)
        self.add_controller(drag)

    def set_duration(self, duration):
        self.duration = max(0.0, float(duration or 0.0))
        self.start = 0.0
        self.end = self.duration
        self.position = 0.0
        self.queue_draw()

    def set_range(self, start, end, notify=True):
        if self.duration <= 0:
            return
        self.start = max(0.0, min(float(start), self.duration))
        self.end = max(self.start, min(float(end), self.duration))
        self.queue_draw()
        if notify:
            self.trim_callback(self.start, self.end)

    def set_position(self, position, notify=False):
        if self.duration <= 0:
            self.position = 0.0
        else:
            self.position = max(0.0, min(float(position), self.duration))
        self.queue_draw()
        if notify:
            self.seek_callback(self.position)

    def _x_for_time(self, seconds):
        usable = max(1, self.get_width() - 24)
        return 12 + (seconds / self.duration * usable if self.duration else 0)

    def _time_for_x(self, x):
        usable = max(1, self.get_width() - 24)
        return max(0.0, min(1.0, (x - 12) / usable)) * self.duration

    def _draw(self, area, cr, width, height):
        y = height / 2 - 4
        x1 = self._x_for_time(self.start)
        x2 = self._x_for_time(self.end)
        xp = self._x_for_time(self.position)
        cr.set_source_rgba(0.30, 0.30, 0.30, 1)
        cr.rectangle(12, y, max(1, width - 24), 8)
        cr.fill()
        cr.set_source_rgba(0.72, 0.72, 0.72, 1)
        cr.rectangle(x1, y, max(1, x2 - x1), 8)
        cr.fill()
        cr.set_source_rgba(0.95, 0.95, 0.95, 1)
        for x in (x1, x2):
            cr.rectangle(x - self.HANDLE_WIDTH / 2, y - 9, self.HANDLE_WIDTH, 26)
            cr.fill()
        cr.set_source_rgba(1, 1, 1, 1)
        cr.set_line_width(2)
        cr.move_to(xp, 4)
        cr.line_to(xp, height - 4)
        cr.stroke()
        cr.move_to(xp - 5, 4)
        cr.line_to(xp + 5, 4)
        cr.line_to(xp, 10)
        cr.close_path()
        cr.fill()

    def _pick_target(self, x):
        x1 = self._x_for_time(self.start)
        x2 = self._x_for_time(self.end)
        if abs(x - x1) <= 9 and abs(x - x1) <= abs(x - x2):
            return 'start'
        if abs(x - x2) <= 9:
            return 'end'
        return 'playhead'

    def _click_pressed(self, gesture, n_press, x, y):
        if self.duration > 0 and self._pick_target(x) == 'playhead':
            self.set_position(self._time_for_x(x), notify=True)

    def _drag_begin(self, gesture, x, y):
        if self.duration <= 0:
            return
        self.drag_handle = self._pick_target(x)
        self.drag_start = (self.start, self.end, self.position)
        if self.drag_handle == 'playhead':
            self.set_position(self._time_for_x(x), notify=True)

    def _drag_update(self, gesture, dx, dy):
        if not self.drag_handle or not self.drag_start:
            return
        start0, end0, pos0 = self.drag_start
        delta = dx / max(1, self.get_width() - 24) * self.duration
        if self.drag_handle == 'start':
            value = max(0.0, min(start0 + delta, end0))
            self.set_range(value, end0)
            self.set_position(value, notify=True)
        elif self.drag_handle == 'end':
            value = min(self.duration, max(end0 + delta, start0))
            self.set_range(start0, value)
            self.set_position(value, notify=True)
        else:
            self.set_position(pos0 + delta, notify=True)

    def _drag_end(self, gesture, dx, dy):
        if self.drag_handle in ('start', 'end'):
            self.trim_callback(self.start, self.end)
        self.drag_handle = None
        self.drag_start = None



class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, application):
        super().__init__(application=application)
        self.set_title('Viditron')
        print('Viditron build: scroll-custom-ratio-fix')
        self.set_default_size(1450, 820)
        self.set_size_request(1180, 480)

        self.current_file = None
        self.actions = []
        self._dark_mode = self._detect_dark_mode()
        self.censor_bars = []
        self._active_tool_tab = 'crop'
        self.file_chooser = PortalFileChooser()
        self.video_width = 0
        self.video_height = 0
        self.duration_seconds = 0.0
        self._updating_crop_entries = False
        self._updating_trim_entries = False
        self._transport_timer_id = None
        self._updating_aspect_buttons = False
        self.crop_overlay = None
        self.icon_dir = Path(__file__).resolve().parent.parent / 'data' / 'icons'
        self.adjustment_icon_dir = self.icon_dir / 'adjustments'
        self._image_preview_timer_id = None
        self._image_preview_generation = 0
        self.adjustment_scales = {}
        self._resetting_adjustments = False

        self._build_ui()

    def _build_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_child(root)

        # Large mode tabs span the whole window, above all three panes.
        # Gtk size requests are minimums, so use a non-scrolling ScrolledWindow
        # with identical min/max content height to hard-limit the header row.
        tab_header = Gtk.ScrolledWindow()
        tab_header.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
        tab_header.set_min_content_height(TAB_BAR_HEIGHT)
        tab_header.set_max_content_height(TAB_BAR_HEIGHT)
        tab_header.set_propagate_natural_height(False)
        tab_header.set_vexpand(False)
        tab_header.set_hexpand(True)
        tab_header.set_valign(Gtk.Align.START)
        tab_header.add_css_class('main-tab-header')

        tab_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        tab_bar.set_size_request(-1, TAB_BAR_HEIGHT)
        tab_bar.set_vexpand(False)
        tab_bar.set_hexpand(True)
        tab_bar.set_valign(Gtk.Align.START)
        tab_bar.add_css_class('main-tab-bar')
        tab_bar.set_margin_top(0)
        tab_bar.set_margin_bottom(0)
        tab_bar.set_margin_start(10)
        tab_bar.set_margin_end(10)

        tab_header.set_child(tab_bar)
        root.append(tab_header)

        self.tool_stack = Gtk.Stack()
        self.tool_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.tool_stack.set_hexpand(True)
        self.tool_stack.set_vexpand(True)
        # Each tab must measure its own height.  The default homogeneous
        # vertical sizing makes the shorter Image/Audio pages inherit the
        # taller Crop/Trim/Rotate page's height, which creates a persistent
        # centering gap inside the scroller.
        self.tool_stack.set_vhomogeneous(False)
        self.tool_stack.set_hhomogeneous(False)
        self.tool_stack.add_named(self._build_crop_trim_rotate_tab(), 'crop')
        self.tool_stack.add_named(self._build_image_tab(), 'image')
        self.tool_stack.add_named(self._build_audio_tab(), 'audio')
        self.tool_stack.add_named(self._build_censor_tab(), 'censor')

        first_tab = None
        for label, name, icon_name in (
            ('Crop / Trim / Rotate', 'crop', 'crop-trim-rotate.svg'),
            ('Adjust Image/Playback', 'image', 'adjust-image.svg'),
            ('Adjust Audio', 'audio', 'adjust-audio.svg'),
            ('Censor Video', 'censor', 'join-clips.svg'),
        ):
            button = Gtk.ToggleButton()
            button.add_css_class('main-tab-button')
            button.set_hexpand(True)
            button.set_vexpand(False)
            button.set_valign(Gtk.Align.CENTER)
            button.set_size_request(-1, TAB_BAR_HEIGHT)
            button.set_overflow(Gtk.Overflow.HIDDEN)

            tab_content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            tab_content.set_halign(Gtk.Align.CENTER)
            tab_content.set_valign(Gtk.Align.CENTER)
            tab_content.set_hexpand(False)
            tab_content.set_vexpand(False)
            icon_path = self.icon_dir / icon_name

            icon_slot = Gtk.Frame()
            icon_slot.set_size_request(TAB_ICON_SIZE, TAB_ICON_SIZE)
            icon_slot.set_hexpand(False)
            icon_slot.set_vexpand(False)
            icon_slot.add_css_class('flat-icon-slot')
            icon_slot.set_halign(Gtk.Align.CENTER)
            icon_slot.set_valign(Gtk.Align.CENTER)
            icon_slot.set_overflow(Gtk.Overflow.HIDDEN)
            icon_slot.add_css_class("tab-icon-slot")

            if icon_path.exists():
                icon = Gtk.Picture.new_for_filename(str(icon_path))
                icon.set_content_fit(Gtk.ContentFit.CONTAIN)
                icon.set_can_shrink(True)
                icon.set_hexpand(True)
                icon.set_vexpand(False)
                icon.set_valign(Gtk.Align.CENTER)
            else:
                icon = Gtk.Image.new_from_icon_name("image-missing-symbolic")
                icon.set_pixel_size(TAB_ICON_SIZE)
                icon.set_tooltip_text(str(icon_path))

            icon_slot.set_child(icon)
            tab_content.append(icon_slot)
            tab_label = Gtk.Label(label=label)
            tab_label.add_css_class('tab-title')
            tab_label.set_halign(Gtk.Align.CENTER)
            tab_label.set_valign(Gtk.Align.CENTER)
            tab_label.set_xalign(0.5)
            tab_content.append(tab_label)

            tab_center = Gtk.CenterBox()
            tab_center.set_hexpand(True)
            tab_center.set_vexpand(False)
            tab_center.set_center_widget(tab_content)
            button.set_child(tab_center)

            if first_tab is not None:
                button.set_group(first_tab)
            else:
                first_tab = button
            button.connect('toggled', self._on_main_tab_toggled, name)
            tab_bar.append(button)
        first_tab.set_active(True)

        outer_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        outer_paned.set_wide_handle(True)
        outer_paned.set_position(390)
        outer_paned.set_vexpand(True)
        outer_paned.set_resize_start_child(False)
        outer_paned.set_resize_end_child(True)
        outer_paned.set_shrink_start_child(True)
        root.append(outer_paned)

        right_split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        right_split.set_wide_handle(True)
        right_split.set_position(720)
        right_split.set_resize_start_child(True)
        right_split.set_resize_end_child(False)
        right_split.set_shrink_end_child(False)
        outer_paned.set_end_child(right_split)

        tools_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=0,
            margin_top=0, margin_bottom=10, margin_start=0, margin_end=0,
        )
        tools_box.add_css_class('left-tool-panel')
        tools_box.set_size_request(300, -1)
        tools_box.set_hexpand(False)
        outer_paned.set_start_child(tools_box)

        tools_scroller = Gtk.ScrolledWindow()
        tools_scroller.add_css_class('left-tool-scroller')
        tools_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        tools_scroller.set_hexpand(True)
        tools_scroller.set_vexpand(True)
        tools_scroller.set_propagate_natural_height(False)
        tools_scroller.set_child(self.tool_stack)
        tools_box.append(tools_scroller)

        preview_column = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=10,
            margin_top=10, margin_bottom=10, margin_start=10, margin_end=10,
        )
        preview_column.set_hexpand(True)
        preview_column.set_vexpand(True)
        right_split.set_start_child(preview_column)

        preview_frame = Gtk.Frame()
        preview_frame.set_hexpand(True)
        preview_frame.set_vexpand(True)
        preview_column.append(preview_frame)

        self.center_stack = Gtk.Stack()
        self.center_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.center_stack.set_hexpand(True)
        self.center_stack.set_vexpand(True)
        preview_frame.set_child(self.center_stack)

        self.preview_stack = Gtk.Stack()
        self.preview_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.preview_stack.set_hexpand(True)
        self.preview_stack.set_vexpand(True)
        self.center_stack.add_named(self.preview_stack, 'editor')

        # Export mode completely replaces the editor surface in the centre pane.
        # Keep the background on this outer box so it fills every available pixel;
        # only the actual status controls are kept to a comfortable reading width.
        progress_pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        progress_pane.set_halign(Gtk.Align.FILL)
        progress_pane.set_valign(Gtk.Align.FILL)
        progress_pane.set_hexpand(True)
        progress_pane.set_vexpand(True)
        progress_pane.add_css_class('export-progress-pane')

        progress_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=18,
            margin_top=48, margin_bottom=48, margin_start=48, margin_end=48,
        )
        progress_box.set_halign(Gtk.Align.FILL)
        progress_box.set_valign(Gtk.Align.CENTER)
        progress_box.set_hexpand(True)
        progress_box.set_vexpand(True)

        self.export_progress_title = Gtk.Label(label='Exporting video...')
        self.export_progress_title.add_css_class('title-2')
        self.export_progress_title.set_halign(Gtk.Align.CENTER)
        progress_box.append(self.export_progress_title)

        self.export_progress_filename = Gtk.Label(label='')
        self.export_progress_filename.add_css_class('dim-label')
        self.export_progress_filename.set_halign(Gtk.Align.CENTER)
        self.export_progress_filename.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        progress_box.append(self.export_progress_filename)

        self.export_progress_bar = Gtk.ProgressBar()
        self.export_progress_bar.set_hexpand(True)
        self.export_progress_bar.set_show_text(True)
        self.export_progress_bar.set_size_request(-1, 28)
        progress_box.append(self.export_progress_bar)

        self.export_progress_status = Gtk.Label(label='Preparing export...')
        self.export_progress_status.add_css_class('dim-label')
        self.export_progress_status.set_halign(Gtk.Align.CENTER)
        progress_box.append(self.export_progress_status)

        progress_pane.append(progress_box)
        self.center_stack.add_named(progress_pane, 'progress')

        # Successful export replaces the progress surface with an in-pane
        # completion view. The editor returns only when Keep editing is pressed.
        complete_pane = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        complete_pane.set_halign(Gtk.Align.FILL)
        complete_pane.set_valign(Gtk.Align.FILL)
        complete_pane.set_hexpand(True)
        complete_pane.set_vexpand(True)
        complete_pane.add_css_class('export-progress-pane')

        complete_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=18,
            margin_top=48, margin_bottom=48, margin_start=48, margin_end=48,
        )
        complete_box.set_halign(Gtk.Align.FILL)
        complete_box.set_valign(Gtk.Align.CENTER)
        complete_box.set_hexpand(True)
        complete_box.set_vexpand(True)

        complete_title = Gtk.Label(label='Export complete')
        complete_title.add_css_class('title-2')
        complete_title.set_halign(Gtk.Align.CENTER)
        complete_box.append(complete_title)

        self.export_complete_filename = Gtk.Label(label='')
        self.export_complete_filename.add_css_class('dim-label')
        self.export_complete_filename.set_halign(Gtk.Align.CENTER)
        self.export_complete_filename.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.export_complete_filename.set_selectable(True)
        complete_box.append(self.export_complete_filename)

        action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        action_row.set_halign(Gtk.Align.CENTER)

        show_button = Gtk.Button(label='Show file')
        show_button.connect('clicked', self._on_complete_show_file)
        action_row.append(show_button)

        open_button = Gtk.Button(label='Open file')
        open_button.connect('clicked', self._on_complete_open_file)
        action_row.append(open_button)

        copy_button = Gtk.Button(label='Copy file to clipboard')
        copy_button.connect('clicked', self._on_complete_copy_file)
        action_row.append(copy_button)
        complete_box.append(action_row)

        keep_editing = Gtk.Button(label='Keep editing')
        keep_editing.add_css_class('suggested-action')
        keep_editing.set_halign(Gtk.Align.CENTER)
        keep_editing.set_size_request(220, 46)
        keep_editing.connect('clicked', self._on_keep_editing)
        complete_box.append(keep_editing)

        complete_pane.append(complete_box)
        self.center_stack.add_named(complete_pane, 'complete')
        self.center_stack.set_visible_child_name('editor')

        empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        empty.set_halign(Gtk.Align.FILL)
        empty.set_valign(Gtk.Align.FILL)
        empty.set_hexpand(True)
        empty.set_vexpand(True)
        empty.add_css_class('preview-area')

        empty_inner = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=14,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
        )
        empty_inner.set_hexpand(True)
        empty_inner.set_vexpand(True)

        self.preview_label = Gtk.Label(label='No video loaded')
        self.preview_label.set_halign(Gtk.Align.CENTER)
        self.preview_label.add_css_class('title-2')
        empty_inner.append(self.preview_label)

        open_download_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        open_download_row.set_halign(Gtk.Align.CENTER)

        self.center_open_button = Gtk.Button(label='Open Video')
        self.center_open_button.add_css_class('suggested-action')
        self.center_open_button.set_size_request(190, 50)
        self.center_open_button.connect('clicked', self._on_open_video)
        open_download_row.append(self.center_open_button)

        self.download_video_button = Gtk.Button(label='Download Video...')
        self.download_video_button.add_css_class('preview-download-button')
        self.download_video_button.set_size_request(190, 50)
        self.download_video_button.connect('clicked', self._on_download_video)
        open_download_row.append(self.download_video_button)

        empty_inner.append(open_download_row)

        drop_hint = Gtk.Label(label='or drop a video here')
        drop_hint.add_css_class('dim-label')
        drop_hint.set_halign(Gtk.Align.CENTER)
        empty_inner.append(drop_hint)

        empty.append(empty_inner)
        self.preview_stack.add_named(empty, 'empty')

        player = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        player.add_css_class('player-area')

        self.video_overlay = Gtk.Overlay()
        self.video_overlay.set_hexpand(True)
        self.video_overlay.set_vexpand(True)
        player.append(self.video_overlay)

        self.video = Gtk.Video()
        self.video.set_hexpand(True)
        self.video.set_vexpand(True)
        self.video.set_autoplay(False)
        self.video.set_loop(False)
        self.video_overlay.set_child(self.video)

        self.image_preview_picture = Gtk.Picture()
        self.image_preview_picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self.image_preview_picture.set_can_shrink(True)
        self.image_preview_picture.set_hexpand(True)
        self.image_preview_picture.set_vexpand(True)
        self.image_preview_picture.set_visible(False)
        self.image_preview_picture.set_can_target(False)
        self.video_overlay.add_overlay(self.image_preview_picture)

        self.crop_overlay = CropOverlay(self._on_crop_overlay_changed)
        self.video_overlay.add_overlay(self.crop_overlay)

        self.censor_overlay = CensorOverlay(
            self._on_censor_overlay_changed,
            self._on_censor_overlay_selected,
            self._on_censor_overlay_created,
        )
        self.censor_overlay.set_active(False)
        self.video_overlay.add_overlay(self.censor_overlay)

        # Viditron's own compact transport controls.
        transport = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        transport.set_margin_top(6)
        transport.set_margin_bottom(2)
        transport.set_margin_start(8)
        transport.set_margin_end(8)

        self.play_button = Gtk.Button(label='▶')
        self.play_button.set_tooltip_text('Play / Pause')
        self.play_button.connect('clicked', self._on_play_pause)
        transport.append(self.play_button)

        timeline_stack = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        timeline_stack.set_hexpand(True)

        self.censor_timeline = CensorTimeline(
            self._on_censor_timeline_selected,
            self._on_censor_timeline_changed,
        )
        self.censor_timeline.set_visible(False)
        timeline_stack.append(self.censor_timeline)

        self.timeline = UnifiedTimeline(self._on_trim_timeline_changed, self._on_timeline_seek)
        timeline_stack.append(self.timeline)
        transport.append(timeline_stack)

        self.transport_time = Gtk.Label(label='00:00:00.000 / 00:00:00.000')
        self.transport_time.set_width_chars(27)
        self.transport_time.set_xalign(1.0)
        transport.append(self.transport_time)
        player.append(transport)

        self.trim_timeline = self.timeline

        self.preview_stack.add_named(player, 'video')
        self.preview_stack.set_visible_child_name('empty')

        side = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12,
            margin_top=10, margin_bottom=10, margin_start=10, margin_end=10,
        )
        side.set_size_request(320, -1)
        side.set_hexpand(False)
        side.set_vexpand(True)

        side_scroller = Gtk.ScrolledWindow()
        side_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        side_scroller.set_hexpand(False)
        side_scroller.set_vexpand(True)
        side_scroller.set_propagate_natural_height(False)
        side_scroller.set_child(side)
        right_split.set_end_child(side_scroller)

        info_frame = Gtk.Frame()
        info_frame.add_css_class('info-card')
        info_frame.set_hexpand(True)
        side.append(info_frame)

        info_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=7,
            margin_top=10, margin_bottom=10, margin_start=12, margin_end=12,
        )
        info_frame.set_child(info_box)

        info_title_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        self.video_title = Gtk.Label(label='No video loaded', xalign=0)
        self.video_title.set_hexpand(True)
        self.video_title.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.video_title.add_css_class('heading')
        info_title_row.append(self.video_title)

        self.theme_toggle = Gtk.Button()
        self.theme_toggle.add_css_class('flat')
        self.theme_toggle.add_css_class('theme-toggle')
        self.theme_toggle.set_tooltip_text('Switch to light mode' if self._dark_mode else 'Switch to dark mode')
        self.theme_toggle.set_child(
            Gtk.Image.new_from_icon_name(
                'weather-clear-symbolic' if self._dark_mode
                else 'weather-clear-night-symbolic'
            )
        )
        self.theme_toggle.connect('clicked', self._on_theme_toggle)
        info_title_row.append(self.theme_toggle)

        info_box.append(info_title_row)

        self.video_info = Gtk.Label(label='Resolution, duration, FPS and codecs will appear here.', xalign=0)
        self.video_info.set_wrap(True)
        self.video_info.add_css_class('dim-label')
        info_box.append(self.video_info)

        self.audio_info = Gtk.Label(label='', xalign=0)
        self.audio_info.set_wrap(True)
        self.audio_info.add_css_class('dim-label')
        info_box.append(self.audio_info)

        file_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self.open_other_button = Gtk.Button(label='Open other video...')
        self.open_other_button.set_hexpand(True)
        self.open_other_button.connect('clicked', self._on_open_video)
        self.open_other_button.set_visible(False)
        file_buttons.append(self.open_other_button)

        self.download_other_button = Gtk.Button(label='Download Video...')
        self.download_other_button.set_hexpand(True)
        self.download_other_button.connect('clicked', self._on_download_video)
        self.download_other_button.set_visible(False)
        file_buttons.append(self.download_other_button)

        info_box.append(file_buttons)

        actions_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions_title = Gtk.Label(label='Actions to perform', xalign=0)
        actions_title.add_css_class('heading')
        actions_title.set_hexpand(True)
        actions_header.append(actions_title)
        clear_btn = Gtk.Button(label='Clear')
        clear_btn.connect('clicked', self._on_clear_actions)
        actions_header.append(clear_btn)
        side.append(actions_header)

        actions_frame = Gtk.Frame()
        actions_frame.set_hexpand(True)
        actions_frame.set_vexpand(True)
        side.append(actions_frame)

        actions_scroller = Gtk.ScrolledWindow()
        actions_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        actions_scroller.set_hexpand(True)
        actions_scroller.set_vexpand(True)
        actions_frame.set_child(actions_scroller)

        self.actions_list = Gtk.ListBox()
        self.actions_list.set_selection_mode(Gtk.SelectionMode.NONE)
        actions_scroller.set_child(self.actions_list)
        self._refresh_actions_list()

        self.export_settings_expander = Gtk.Expander(label='Export settings')
        self.export_settings_expander.set_expanded(False)
        self.export_settings_expander.set_hexpand(True)
        side.append(self.export_settings_expander)

        export_settings = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=10,
            margin_top=8, margin_bottom=8, margin_start=8, margin_end=8,
        )
        self.export_settings_expander.set_child(export_settings)

        quality_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        qlabel = Gtk.Label(label='Quality', xalign=0)
        qlabel.set_size_request(90, -1)
        quality_row.append(qlabel)

        self.export_quality_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0, 100, 1
        )
        self.export_quality_scale.set_value(75)
        self.export_quality_scale.set_draw_value(True)
        self.export_quality_scale.set_hexpand(True)
        quality_row.append(self.export_quality_scale)
        export_settings.append(quality_row)

        quality_hint = Gtk.Label(
            label='Higher quality produces larger files. 75 is a good general-purpose default.',
            xalign=0,
        )
        quality_hint.set_wrap(True)
        quality_hint.add_css_class('dim-label')
        export_settings.append(quality_hint)

        compression_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        clabel = Gtk.Label(label='Compression', xalign=0)
        clabel.set_size_request(90, -1)
        compression_row.append(clabel)

        self.export_preset_combo = Gtk.DropDown.new_from_strings([
            'Fast', 'Balanced', 'Smaller file'
        ])
        self.export_preset_combo.set_selected(1)
        self.export_preset_combo.set_hexpand(True)
        compression_row.append(self.export_preset_combo)
        export_settings.append(compression_row)

        compression_hint = Gtk.Label(
            label='More compression takes longer to encode but can reduce file size at the same quality.',
            xalign=0,
        )
        compression_hint.set_wrap(True)
        compression_hint.add_css_class('dim-label')
        export_settings.append(compression_hint)

        target_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.target_size_check = Gtk.CheckButton(label='Target file size')
        self.target_size_check.connect('toggled', self._on_export_setting_changed)
        target_row.append(self.target_size_check)

        self.target_size_spin = Gtk.SpinButton.new_with_range(1, 100000, 1)
        self.target_size_spin.set_value(25)
        self.target_size_spin.set_sensitive(False)
        self.target_size_spin.set_width_chars(7)
        target_row.append(self.target_size_spin)
        target_row.append(Gtk.Label(label='MB'))
        export_settings.append(target_row)

        target_hint = Gtk.Label(
            label='Approximate target. The app chooses a bitrate from the final video duration.',
            xalign=0,
        )
        target_hint.set_wrap(True)
        target_hint.add_css_class('dim-label')
        export_settings.append(target_hint)

        self.lossless_check = Gtk.CheckButton(label='Lossless')
        self.lossless_check.connect('toggled', self._on_export_setting_changed)
        export_settings.append(self.lossless_check)

        lossless_hint = Gtk.Label(
            label='No video quality loss. Produces very large files and overrides Quality and Target file size.',
            xalign=0,
        )
        lossless_hint.set_wrap(True)
        lossless_hint.add_css_class('dim-label')
        export_settings.append(lossless_hint)

        self.export_button = Gtk.Button(label='Export Video')
        self.export_button.add_css_class('suggested-action')
        self.export_button.add_css_class('export-button')
        self.export_button.set_hexpand(True)
        self.export_button.set_size_request(-1, 58)
        self.export_button.connect('clicked', self._on_export)
        side.append(self.export_button)

        self._install_css()
        self._install_drop_target()

    def _theme_names_for_switch(self):
        settings = Gtk.Settings.get_default()
        current = ''
        if settings is not None:
            try:
                current = str(settings.get_property('gtk-theme-name') or '')
            except Exception:
                current = ''

        # Preserve the user's theme family where possible. Common examples:
        # Yaru <-> Yaru-dark, Adwaita <-> Adwaita-dark.
        lower = current.lower()
        if lower.endswith('-dark'):
            light = current[:-5]
            dark = current
        elif lower.endswith(':dark'):
            light = current[:-5]
            dark = current
        else:
            light = current or 'Adwaita'
            dark = f'{light}-dark'

        return light, dark

    def _detect_dark_mode(self):
        settings = Gtk.Settings.get_default()
        if settings is None:
            return False
        try:
            theme_name = str(settings.get_property('gtk-theme-name') or '').lower()
            if 'dark' in theme_name:
                return True
        except Exception:
            pass
        try:
            return bool(settings.get_property('gtk-application-prefer-dark-theme'))
        except Exception:
            return False

    def _apply_color_mode(self, dark):
        settings = Gtk.Settings.get_default()
        if settings is None:
            return

        self._dark_mode = bool(dark)
        light_theme, dark_theme = self._theme_names_for_switch()
        wanted = dark_theme if self._dark_mode else light_theme

        # Force the actual GTK theme variant. On some desktops the
        # application-prefer-dark-theme hint is ignored, while gtk-theme-name
        # takes effect immediately.
        try:
            settings.set_property('gtk-theme-name', wanted)
        except Exception:
            pass

        try:
            settings.set_property(
                'gtk-application-prefer-dark-theme',
                self._dark_mode,
            )
        except Exception:
            pass

        if hasattr(self, 'theme_toggle'):
            icon_name = (
                'weather-clear-symbolic'
                if self._dark_mode
                else 'weather-clear-night-symbolic'
            )
            self.theme_toggle.set_child(Gtk.Image.new_from_icon_name(icon_name))
            self.theme_toggle.set_tooltip_text(
                'Switch to light mode'
                if self._dark_mode
                else 'Switch to dark mode'
            )

    def _on_theme_toggle(self, button=None):
        self._apply_color_mode(not self._dark_mode)

    def _on_export_setting_changed(self, button=None):
        lossless = self.lossless_check.get_active()
        target = self.target_size_check.get_active() and not lossless

        self.target_size_spin.set_sensitive(target)
        self.export_quality_scale.set_sensitive(not lossless and not target)
        self.export_preset_combo.set_sensitive(not lossless)

    def _effective_export_duration(self):
        duration = max(0.001, float(self.duration_seconds or 0.001))

        if any(a.startswith('Trim:') for a in self.actions):
            start = max(0.0, float(self.trim_timeline.start))
            end = max(start, float(self.trim_timeline.end))
            duration = max(0.001, end - start)

        speed = 1.0
        for action in self.actions:
            if action.startswith('Playback speed:'):
                speed = max(0.25, min(4.0, float(action.split(':', 1)[1].strip())))
                break

        return max(0.001, duration / speed)

    def _on_main_tab_toggled(self, button, name):
        if button.get_active():
            self._active_tool_tab = name
            self.tool_stack.set_visible_child_name(name)
            if hasattr(self, 'censor_overlay'):
                self.censor_overlay.set_edit_mode(name == 'censor')
                self.censor_overlay.set_state(
                    self.censor_bars,
                    getattr(self, 'selected_censor_index', None),
                )

    def _section_frame(self, title):
        section = Gtk.Overlay()
        section.set_hexpand(True)
        section.set_vexpand(True)
        section.set_margin_start(8)

        # Start the frame a little below the top of the overlay so its top
        # border runs through the middle of the title label.
        frame = Gtk.Frame()
        frame.set_hexpand(True)
        frame.set_vexpand(True)
        frame.set_margin_top(9)
        frame.add_css_class('tool-section-frame')
        section.set_child(frame)

        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=4,
            margin_top=9, margin_bottom=3, margin_start=8, margin_end=8,
        )
        content.set_vexpand(True)
        content.set_valign(Gtk.Align.CENTER)
        frame.set_child(content)

        # Overlay the title on top of the border. Its background masks the
        # line underneath, producing the classic "---- Title ----" effect.
        title_label = Gtk.Label(label=title)
        title_label.set_halign(Gtk.Align.CENTER)
        title_label.set_valign(Gtk.Align.START)
        title_label.add_css_class('tool-section-title')
        section.add_overlay(title_label)

        return section, content

    def _aspect_button(self, label, ratio_w=None, ratio_h=None, group=None, custom=False):
        button = Gtk.ToggleButton(label=label)
        if group is not None:
            button.set_group(group)
        if ratio_w and ratio_h:
            base_h = 34
            width = max(34, int(round(base_h * ratio_w / ratio_h)))
            button.set_size_request(width, base_h)
        else:
            button.set_size_request(58 if custom else 48, 34)
        button.set_halign(Gtk.Align.CENTER)
        button.connect('toggled', self._on_aspect_ratio_toggled, ratio_w, ratio_h, custom)
        return button

    def _build_crop_trim_rotate_tab(self):
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=10,
            margin_top=9, margin_bottom=6, margin_start=0, margin_end=8,
        )
        content.set_homogeneous(True)
        content.set_vexpand(True)

        # Crop -------------------------------------------------------------
        crop_frame, crop_box = self._section_frame('Crop')
        content.append(crop_frame)

        crop_help = Gtk.Label(
            label='Click and drag the white frame around the video to define the cropped area.'
        )
        crop_help.set_wrap(True)
        crop_help.set_justify(Gtk.Justification.CENTER)
        crop_help.add_css_class('dim-label')
        crop_box.append(crop_help)

        # Compact two-column crop controls: labels on the left, controls on the right.
        crop_controls = Gtk.Grid(column_spacing=10, row_spacing=8)
        crop_controls.set_halign(Gtk.Align.CENTER)
        crop_controls.set_valign(Gtk.Align.CENTER)

        preset_label = Gtk.Label(label='Aspect\nratio')
        preset_label.add_css_class('dim-label')
        preset_label.set_halign(Gtk.Align.CENTER)
        preset_label.set_valign(Gtk.Align.CENTER)
        preset_label.set_xalign(0.5)
        preset_label.set_justify(Gtk.Justification.CENTER)
        crop_controls.attach(preset_label, 0, 0, 1, 1)

        self.aspect_buttons = []
        self.custom_aspect_ratio = None

        aspect_block = Gtk.Grid(column_spacing=6, row_spacing=5)
        aspect_block.set_halign(Gtk.Align.CENTER)

        first = None
        presets = (
            ('4:3', 4, 3), ('16:9', 16, 9),
            ('16:10', 16, 10), ('1:1', 1, 1),
        )
        for index, (label, rw, rh) in enumerate(presets):
            btn = self._aspect_button(label, rw, rh, first)
            btn.set_size_request(66, 32)
            if first is None:
                first = btn
            self.aspect_buttons.append(btn)
            # Four fixed-ratio presets occupy the first two columns.
            aspect_block.attach(btn, index % 2, index // 2, 1, 1)

        free_btn = self._aspect_button('Free', group=first)
        free_btn.set_size_request(72, 32)
        self.aspect_buttons.insert(0, free_btn)
        aspect_block.attach(free_btn, 2, 0, 1, 1)

        custom_btn = self._aspect_button('Custom...', group=first, custom=True)
        custom_btn.set_size_request(84, 32)
        self.aspect_buttons.append(custom_btn)
        self.custom_aspect_button = custom_btn
        aspect_block.attach(custom_btn, 2, 1, 1, 1)
        free_btn.set_active(True)

        crop_controls.attach(aspect_block, 1, 0, 1, 1)

        coord_label = Gtk.Label(label='Exact\ncoordinates')
        coord_label.add_css_class('dim-label')
        coord_label.set_halign(Gtk.Align.CENTER)
        coord_label.set_valign(Gtk.Align.CENTER)
        coord_label.set_xalign(0.5)
        coord_label.set_justify(Gtk.Justification.CENTER)
        crop_controls.attach(coord_label, 0, 1, 1, 1)

        grid = Gtk.Grid(column_spacing=6, row_spacing=3)
        grid.set_halign(Gtk.Align.START)
        crop_controls.attach(grid, 1, 1, 1, 1)
        crop_box.append(crop_controls)

        self.crop_entries = {}
        for row, pair in enumerate((('X', 'Y'), ('W', 'H'))):
            for col, key in enumerate(pair):
                label = Gtk.Label(label=key)
                label.set_halign(Gtk.Align.END)
                grid.attach(label, col * 2, row, 1, 1)
                entry = Gtk.Entry()
                entry.set_width_chars(6)
                entry.set_input_purpose(Gtk.InputPurpose.DIGITS)
                entry.connect('changed', self._on_crop_entry_changed, key.lower())
                grid.attach(entry, col * 2 + 1, row, 1, 1)
                self.crop_entries[key.lower()] = entry

        # Trim -------------------------------------------------------------
        trim_frame, trim_box = self._section_frame('Trim')
        content.append(trim_frame)

        trim_help = Gtk.Label(
            label='Drag the handles on the timeline below the video to set the start and end of the trim.'
        )
        trim_help.set_wrap(True)
        trim_help.set_justify(Gtk.Justification.CENTER)
        trim_help.add_css_class('dim-label')
        trim_box.append(trim_help)

        time_label = Gtk.Label(label='Exact timecodes')
        time_label.add_css_class('dim-label')
        trim_box.append(time_label)

        time_grid = Gtk.Grid(column_spacing=6, row_spacing=4)
        time_grid.set_halign(Gtk.Align.CENTER)
        trim_box.append(time_grid)
        time_grid.attach(Gtk.Label(label='Start', xalign=0), 0, 0, 1, 1)
        self.trim_start_entry = Gtk.Entry()
        self.trim_start_entry.set_width_chars(14)
        self.trim_start_entry.set_size_request(-1, 34)
        self.trim_start_entry.set_placeholder_text('00:00:00.000')
        self.trim_start_entry.connect('activate', self._on_trim_entry_activate, 'start')
        time_grid.attach(self.trim_start_entry, 1, 0, 1, 1)
        set_start = Gtk.Button(label='Set')
        set_start.set_size_request(48, 34)
        set_start.connect('clicked', self._on_trim_set_clicked, 'start')
        time_grid.attach(set_start, 2, 0, 1, 1)

        time_grid.attach(Gtk.Label(label='End', xalign=0), 0, 1, 1, 1)
        self.trim_end_entry = Gtk.Entry()
        self.trim_end_entry.set_width_chars(14)
        self.trim_end_entry.set_size_request(-1, 34)
        self.trim_end_entry.set_placeholder_text('00:00:00.000')
        self.trim_end_entry.connect('activate', self._on_trim_entry_activate, 'end')
        time_grid.attach(self.trim_end_entry, 1, 1, 1, 1)
        set_end = Gtk.Button(label='Set')
        set_end.set_size_request(48, 34)
        set_end.connect('clicked', self._on_trim_set_clicked, 'end')
        time_grid.attach(set_end, 2, 1, 1, 1)

        # Rotate -----------------------------------------------------------
        rotate_frame, rotate_box = self._section_frame('Rotate')
        # The three section frames share the panel height equally. The
        # transform grid itself remains compact and centered inside this frame.
        rotate_frame.set_vexpand(True)
        rotate_box.set_vexpand(True)
        rotate_box.set_valign(Gtk.Align.CENTER)
        content.append(rotate_frame)
        rotate_grid = Gtk.Grid(column_spacing=6, row_spacing=4)
        rotate_grid.set_halign(Gtk.Align.CENTER)
        rotate_grid.set_valign(Gtk.Align.CENTER)
        # Keep the grid at its natural size.  If the grid expands, GTK gives
        # its cells the full section width/height and the transform buttons
        # visually become large tiles.
        rotate_grid.set_hexpand(False)
        rotate_grid.set_vexpand(False)
        for col, row, label, icon_name, action in (
            (0, 0, '90\N{DEGREE SIGN} Left', 'rotate-left.svg', 'Rotate: 90\N{DEGREE SIGN} left'),
            (1, 0, '90\N{DEGREE SIGN} Right', 'rotate-right.svg', 'Rotate: 90\N{DEGREE SIGN} right'),
            (0, 1, 'Flip H', 'flip-horizontal.svg', 'Flip: horizontal'),
            (1, 1, 'Flip V', 'flip-vertical.svg', 'Flip: vertical'),
        ):
            button = self._icon_button(
                label,
                self.icon_dir / 'rotate' / icon_name,
                lambda _b, a=action: self._apply_transform_action(a),
                size=40,
                vertical=True,
            )
            button.add_css_class('rotate-button')
            button.set_hexpand(False)
            button.set_vexpand(False)
            button.set_halign(Gtk.Align.CENTER)
            button.set_valign(Gtk.Align.CENTER)
            button.set_size_request(104, 76)

            rotate_grid.attach(button, col, row, 1, 1)
        rotate_box.append(rotate_grid)

        return content

    def _make_section(self, title, widgets):
        # A vertical CenterBox centers the controls when the viewport is tall,
        # but unlike expanding spacer widgets it does not add a persistent
        # minimum-height margin when the viewport becomes short.
        outer = Gtk.CenterBox(orientation=Gtk.Orientation.VERTICAL)
        outer.set_hexpand(True)
        outer.set_vexpand(True)

        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12,
            margin_top=4, margin_bottom=4, margin_start=12, margin_end=12,
        )
        box.set_hexpand(True)
        box.set_vexpand(False)

        if title:
            title_label = Gtk.Label(label=title, xalign=0)
            title_label.add_css_class('heading')
            box.append(title_label)

        for widget in widgets:
            box.append(widget)

        outer.set_center_widget(box)
        return outer

    def _icon_button(self, label, icon_path, callback, size=32, vertical=False):
        button = Gtk.Button()
        button.set_hexpand(True)

        orientation = Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL
        spacing = 2 if vertical else 8
        content = Gtk.Box(orientation=orientation, spacing=spacing)
        content.set_halign(Gtk.Align.CENTER)
        content.set_valign(Gtk.Align.CENTER)

        slot = Gtk.Frame()
        slot.set_size_request(size, size)
        slot.set_hexpand(False)
        slot.set_vexpand(False)
        slot.add_css_class('flat-icon-slot')
        slot.set_halign(Gtk.Align.CENTER)
        slot.set_valign(Gtk.Align.CENTER)
        if size >= 64:
            slot.add_css_class('adjustment-icon-slot')

        if icon_path.exists():
            # Gtk.Picture preserves an SVG's intrinsic natural size, which can
            # make the containing button grow far beyond `size`. Gtk.Image
            # with pixel_size gives scalable SVGs a bounded natural size.
            icon = Gtk.Image.new_from_file(str(icon_path))
            icon.set_pixel_size(size)
            icon.set_size_request(size, size)
            icon.set_hexpand(False)
            icon.set_vexpand(False)
            icon.set_halign(Gtk.Align.CENTER)
            icon.set_valign(Gtk.Align.CENTER)
        else:
            icon = Gtk.Image.new_from_icon_name('image-missing-symbolic')
            icon.set_pixel_size(max(18, size - 8))
            icon.set_size_request(size, size)
            icon.set_hexpand(False)
            icon.set_vexpand(False)
            icon.set_halign(Gtk.Align.CENTER)
            icon.set_valign(Gtk.Align.CENTER)
            icon.set_tooltip_text(str(icon_path))
        slot.set_child(icon)

        content.append(slot)

        text = Gtk.Label(label=label)
        text.set_halign(Gtk.Align.CENTER)
        text.set_valign(Gtk.Align.CENTER)
        text.set_xalign(0.5)
        content.append(text)

        button.set_child(content)
        button.connect('clicked', callback)
        return button

    def _button_row(self, items):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_hexpand(True)
        for label, callback in items:
            button = Gtk.Button(label=label)
            button.set_hexpand(True)
            button.connect('clicked', callback)
            row.append(button)
        return row

    def _slider_row(self, label, minimum, maximum, step, default, callback, icon_name=None):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        if icon_name:
            icon_path = self.adjustment_icon_dir / icon_name
            icon_slot = Gtk.Frame()
            icon_slot.set_size_request(36, 36)
            icon_slot.set_hexpand(False)
            icon_slot.set_vexpand(False)
            icon_slot.add_css_class('flat-icon-slot')
            if icon_path.exists():
                icon = Gtk.Picture.new_for_filename(str(icon_path))
                icon.set_content_fit(Gtk.ContentFit.CONTAIN)
                icon.set_can_shrink(True)
            else:
                icon = Gtk.Image.new_from_icon_name('image-missing-symbolic')
                icon.set_pixel_size(24)
                icon.set_tooltip_text(str(icon_path))
            icon_slot.set_child(icon)
            row.append(icon_slot)
        name = Gtk.Label(label=label, xalign=0)
        name.set_size_request(92, -1)
        row.append(name)
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, minimum, maximum, step)
        scale.set_value(default)
        scale.set_draw_value(True)
        scale.set_hexpand(True)
        scale.connect('value-changed', callback)
        row.append(scale)
        self.adjustment_scales[label] = scale
        return row

    def _adjustment_control(self, label, minimum, maximum, step, default,
                            callback, icon_name, description):
        block = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        block.set_hexpand(True)

        block.append(
            self._slider_row(
                label, minimum, maximum, step, default,
                callback, icon_name,
            )
        )

        detail = Gtk.Label(label=description, xalign=0)
        detail.set_wrap(True)
        detail.set_hexpand(True)
        detail.add_css_class('dim-label')
        block.append(detail)
        return block

    def _build_image_tab(self):
        controls = [
            self._adjustment_control(
                'Brightness', -1.0, 1.0, 0.01, 0.0,
                lambda s: self._slider_action('Brightness', s.get_value(), 2),
                'brightness.svg',
                'Make the image lighter or darker.',
            ),
            self._adjustment_control(
                'Contrast', 0.0, 3.0, 0.01, 1.0,
                lambda s: self._slider_action('Contrast', s.get_value(), 2),
                'contrast.svg',
                'Increase or reduce the difference between light and dark areas.',
            ),
            self._adjustment_control(
                'Saturation', 0.0, 3.0, 0.01, 1.0,
                lambda s: self._slider_action('Saturation', s.get_value(), 2),
                'saturation.svg',
                'Make colours more vivid or more muted.',
            ),
            self._adjustment_control(
                'Gamma', 0.1, 3.0, 0.01, 1.0,
                lambda s: self._slider_action('Gamma', s.get_value(), 2),
                'gamma.svg',
                'Adjust midtone brightness without shifting black and white as much.',
            ),
            self._adjustment_control(
                'Temperature', -1.0, 1.0, 0.01, 0.0,
                lambda s: self._slider_action('Temperature', s.get_value(), 2),
                'temperature.svg',
                'Shift the image toward cooler blue or warmer red tones.',
            ),
            self._adjustment_control(
                'Sharpness', -1.0, 2.0, 0.05, 0.0,
                lambda s: self._slider_action('Sharpness', s.get_value(), 2),
                'sharpness.svg',
                'Soften the image or enhance edges and fine detail.',
            ),
            self._adjustment_control(
                'Playback speed', 0.25, 4.0, 0.05, 1.0,
                lambda s: self._slider_action('Playback speed', s.get_value(), 2),
                'playback-speed.svg',
                'Speed up or slow down both video and audio in the export.',
            ),
        ]

        reset = Gtk.Button(label='Reset adjustments')
        reset.set_hexpand(True)
        reset.connect('clicked', self._on_reset_adjustments)
        controls.append(reset)

        return self._make_section(None, controls)

    def _audio_slider_row(self, label, minimum, maximum, step, default,
                          callback, icon_name, icon_size=64):
        block = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        block.set_hexpand(True)

        title = Gtk.Label(label=label)
        title.set_halign(Gtk.Align.CENTER)
        title.set_xalign(0.5)
        title.add_css_class('heading')
        block.append(title)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.set_hexpand(True)

        icon_path = self.icon_dir / 'audio' / icon_name
        slot = Gtk.Frame()
        slot.set_size_request(icon_size, icon_size)
        slot.set_hexpand(False)
        slot.set_vexpand(False)
        slot.add_css_class('flat-icon-slot')
        slot.set_halign(Gtk.Align.CENTER)
        slot.set_valign(Gtk.Align.CENTER)
        slot.add_css_class('flat-icon-slot')

        if icon_path.exists():
            icon = Gtk.Picture.new_for_filename(str(icon_path))
            icon.set_content_fit(Gtk.ContentFit.CONTAIN)
            icon.set_can_shrink(True)
            icon.set_size_request(icon_size, icon_size)
            icon.set_hexpand(False)
            icon.set_vexpand(False)
        else:
            icon = Gtk.Image.new_from_icon_name('image-missing-symbolic')
            icon.set_pixel_size(max(24, icon_size - 12))
            icon.set_tooltip_text(str(icon_path))
        slot.set_child(icon)
        row.append(slot)

        scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, minimum, maximum, step
        )
        scale.set_value(default)
        scale.set_draw_value(True)
        scale.set_hexpand(True)
        scale.set_valign(Gtk.Align.CENTER)
        scale.connect('value-changed', callback)
        row.append(scale)

        self.adjustment_scales[label] = scale
        block.append(row)
        return block

    def _audio_action_control(self, button_text, description, action, icon_name):
        block = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        block.set_hexpand(True)

        button = self._icon_button(
            button_text,
            self.icon_dir / 'audio' / icon_name,
            lambda *_: self._add_action(action),
            size=64,
        )
        block.append(button)

        detail = Gtk.Label(label=description, xalign=0)
        detail.set_wrap(True)
        detail.set_hexpand(True)
        detail.add_css_class('dim-label')
        block.append(detail)

        return block

    def _build_audio_tab(self):
        def described(control, text):
            block = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            block.set_hexpand(True)
            block.append(control)
            detail = Gtk.Label(label=text, xalign=0)
            detail.set_wrap(True)
            detail.set_hexpand(True)
            detail.add_css_class('dim-label')
            block.append(detail)
            return block

        volume = described(
            self._audio_slider_row(
                'Volume', 0.0, 2.0, 0.01, 1.0,
                lambda s: self._slider_action('Volume', s.get_value(), 2),
                'volume.svg', 64,
            ),
            'Raise or lower the volume of the exported audio.',
        )

        remove_audio = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        remove_audio.set_hexpand(True)

        remove_title = Gtk.Label(label='Remove Audio')
        remove_title.set_halign(Gtk.Align.CENTER)
        remove_title.set_xalign(0.5)
        remove_title.add_css_class('heading')
        remove_audio.append(remove_title)

        remove_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=10,
        )
        remove_row.set_hexpand(True)
        remove_row.set_valign(Gtk.Align.CENTER)

        icon_path = self.icon_dir / 'audio' / 'remove-audio.svg'
        icon_slot = Gtk.Box()
        icon_slot.set_size_request(64, 64)
        icon_slot.set_hexpand(False)
        icon_slot.set_vexpand(False)
        icon_slot.set_halign(Gtk.Align.START)
        icon_slot.set_valign(Gtk.Align.CENTER)

        if icon_path.exists():
            remove_icon = Gtk.Picture.new_for_filename(str(icon_path))
            remove_icon.set_content_fit(Gtk.ContentFit.CONTAIN)
            remove_icon.set_can_shrink(True)
            remove_icon.set_size_request(64, 64)
            remove_icon.set_hexpand(False)
            remove_icon.set_vexpand(False)
        else:
            remove_icon = Gtk.Image.new_from_icon_name('image-missing-symbolic')
            remove_icon.set_pixel_size(52)
            remove_icon.set_tooltip_text(str(icon_path))

        icon_slot.append(remove_icon)
        remove_row.append(icon_slot)

        remove_button = Gtk.Button(label='Remove Audio')
        remove_button.set_hexpand(True)
        remove_button.set_valign(Gtk.Align.CENTER)
        remove_button.add_css_class('remove-audio-button')
        remove_button.connect('clicked', lambda *_: self._add_action('Audio: remove track'))
        remove_row.append(remove_button)

        remove_audio.append(remove_row)

        remove_detail = Gtk.Label(
            label='Remove the audio track completely from the exported video.',
            xalign=0,
        )
        remove_detail.set_wrap(True)
        remove_detail.set_hexpand(True)
        remove_detail.add_css_class('dim-label')
        remove_audio.append(remove_detail)

        pitch = described(
            self._audio_slider_row(
                'Pitch', -12.0, 12.0, 1.0, 0.0,
                lambda s: self._slider_action('Pitch', s.get_value(), 0),
                'pitch.svg', 64,
            ),
            'Shift pitch up or down in semitones without changing playback duration.',
        )

        clarity = described(
            self._audio_slider_row(
                'Boost voice clarity', 0.0, 12.0, 0.5, 0.0,
                lambda s: self._slider_action('Boost voice clarity', s.get_value(), 1),
                'voice-clarity.svg', 64,
            ),
            'Boost the speech/presence range to make voices easier to understand.',
        )

        rumble = described(
            self._audio_slider_row(
                'Rumble reduction', 20.0, 200.0, 5.0, 20.0,
                lambda s: self._slider_action('Rumble reduction', s.get_value(), 0),
                'rumble-reduction.svg', 64,
            ),
            'Cut low-frequency rumble, handling noise and hum below the selected frequency.',
        )

        reset_audio = Gtk.Button(label='Reset audio adjustments')
        reset_audio.set_hexpand(True)
        reset_audio.connect('clicked', self._on_reset_audio_adjustments)

        return self._make_section(None, [
            volume,
            remove_audio,
            pitch,
            clarity,
            rumble,
            reset_audio,
        ])

    def _on_reset_audio_adjustments(self, button=None):
        defaults = {
            'Volume': 1.0,
            'Pitch': 0.0,
            'Boost voice clarity': 0.0,
            'Rumble reduction': 20.0,
        }

        prefixes = tuple(name + ':' for name in defaults)
        self.actions = [
            a for a in self.actions
            if not a.startswith(prefixes)
            and a != 'Audio: remove track'
            and a != 'Audio: normalize'
            and a != 'Audio: mute'
        ]

        self._resetting_adjustments = True
        try:
            for name, value in defaults.items():
                scale = self.adjustment_scales.get(name)
                if scale is not None:
                    scale.set_value(value)
        finally:
            self._resetting_adjustments = False

        self._refresh_actions_list()

    def _build_censor_tab(self):
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=10,
            margin_top=12, margin_bottom=12, margin_start=12, margin_end=12,
        )
        box.set_valign(Gtk.Align.CENTER)

        title = Gtk.Label(label='Censor bars')
        title.add_css_class('heading')
        title.set_halign(Gtk.Align.CENTER)
        box.append(title)

        help_label = Gtk.Label(
            label='Drag on the video to create a black bar. Drag a bar to move it, or use its handles to resize it.'
        )
        help_label.set_wrap(True)
        help_label.set_justify(Gtk.Justification.CENTER)
        help_label.add_css_class('dim-label')
        box.append(help_label)

        add = Gtk.Button(label='Add censor bar')
        add.set_halign(Gtk.Align.CENTER)
        add.set_size_request(180, 40)
        add.connect('clicked', self._on_add_censor_bar)
        box.append(add)

        time_grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        time_grid.set_halign(Gtk.Align.CENTER)

        time_grid.attach(Gtk.Label(label='Start'), 0, 0, 1, 1)
        self.censor_start_entry = Gtk.Entry()
        self.censor_start_entry.set_width_chars(12)
        self.censor_start_entry.set_placeholder_text('00:00:00.0')
        time_grid.attach(self.censor_start_entry, 1, 0, 1, 1)

        set_start = Gtk.Button(label='Current')
        set_start.connect('clicked', self._on_censor_set_current, 'start')
        time_grid.attach(set_start, 2, 0, 1, 1)

        time_grid.attach(Gtk.Label(label='End'), 0, 1, 1, 1)
        self.censor_end_entry = Gtk.Entry()
        self.censor_end_entry.set_width_chars(12)
        self.censor_end_entry.set_placeholder_text('00:00:00.0')
        time_grid.attach(self.censor_end_entry, 1, 1, 1, 1)

        set_end = Gtk.Button(label='Current')
        set_end.connect('clicked', self._on_censor_set_current, 'end')
        time_grid.attach(set_end, 2, 1, 1, 1)
        box.append(time_grid)

        apply_time = Gtk.Button(label='Apply time')
        apply_time.set_halign(Gtk.Align.CENTER)
        apply_time.connect('clicked', self._on_apply_censor_time)
        box.append(apply_time)

        self.censor_list = Gtk.ListBox()
        self.censor_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.censor_list.set_size_request(-1, 120)
        self.censor_list.connect('row-selected', self._on_censor_row_selected)
        box.append(self.censor_list)

        return box

    def _on_add_censor_bar(self, button=None):
        if not self.current_file or not self.video_width or not self.video_height:
            self._show_load_error('Load a video before adding a censor bar.')
            return
        w = max(80, int(self.video_width * 0.35))
        h = max(40, int(self.video_height * 0.12))
        x = max(0, (self.video_width - w) // 2)
        y = max(0, (self.video_height - h) // 2)
        self._on_censor_overlay_created(x, y, w, h)

    def _refresh_censor_list(self, select_index=None):
        if not hasattr(self, 'censor_list'):
            return
        child = self.censor_list.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.censor_list.remove(child)
            child = nxt

        for index, bar in enumerate(self.censor_bars):
            row = Gtk.ListBoxRow()
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            line.set_margin_top(4); line.set_margin_bottom(4)
            line.set_margin_start(6); line.set_margin_end(6)
            label = Gtk.Label(
                label=(
                    f'Bar {index + 1}: {bar["w"]}x{bar["h"]} at {bar["x"]},{bar["y"]}\n'
                    f'{format_short_timecode(bar["start"])} - {format_short_timecode(bar["end"])}'
                ),
                xalign=0,
            )
            label.set_wrap(True)
            label.set_hexpand(True)
            line.append(label)
            remove = Gtk.Button(label='Remove')
            remove.connect('clicked', self._on_remove_censor_bar, index)
            line.append(remove)
            row.set_child(line)
            self.censor_list.append(row)

        if self.censor_bars:
            if select_index is None:
                select_index = min(getattr(self, 'selected_censor_index', 0), len(self.censor_bars) - 1)
            row = self.censor_list.get_row_at_index(select_index)
            if row:
                self.censor_list.select_row(row)
        else:
            self.selected_censor_index = None
        if hasattr(self, 'censor_overlay'):
            self.censor_overlay.set_state(
                self.censor_bars,
                getattr(self, 'selected_censor_index', None),
            )
            self.censor_overlay.set_edit_mode(
                getattr(self, '_active_tool_tab', 'crop') == 'censor'
            )
        if hasattr(self, 'censor_timeline'):
            self.censor_timeline.set_state(
                self.censor_bars,
                self.duration_seconds,
                getattr(self, 'selected_censor_index', None),
            )
        self._sync_censor_actions()

    def _sync_censor_actions(self):
        self.actions = [a for a in self.actions if not a.startswith('Censor:')]
        for index, bar in enumerate(self.censor_bars):
            self.actions.append(
                f'Censor: bar {index + 1} {bar["w"]}x{bar["h"]} '
                f'at x={bar["x"]}, y={bar["y"]} '
                f'from {format_short_timecode(bar["start"])} to {format_short_timecode(bar["end"])}'
            )
        self._refresh_actions_list()

    def _on_censor_row_selected(self, listbox, row):
        if row is None:
            return
        index = row.get_index()
        if not (0 <= index < len(self.censor_bars)):
            return
        self.selected_censor_index = index
        bar = self.censor_bars[index]
        self.censor_start_entry.set_text(format_short_timecode(bar['start']))
        self.censor_end_entry.set_text(format_short_timecode(bar['end']))
        self.censor_overlay.selected_index = index
        self.censor_overlay.set_state(self.censor_bars, index)
        self.censor_overlay.set_edit_mode(getattr(self, '_active_tool_tab', 'crop') == 'censor')
        if hasattr(self, 'censor_timeline'):
            self.censor_timeline.selected_index = index
            self.censor_timeline.queue_draw()

    def _on_censor_timeline_selected(self, index):
        if not (0 <= index < len(self.censor_bars)):
            return
        row = self.censor_list.get_row_at_index(index)
        if row is not None:
            self.censor_list.select_row(row)

    def _on_censor_timeline_changed(self, index, start, end):
        if not (0 <= index < len(self.censor_bars)):
            return
        self.censor_bars[index]['start'] = max(0.0, float(start))
        self.censor_bars[index]['end'] = min(self.duration_seconds, float(end))
        self.selected_censor_index = index
        self.censor_start_entry.set_text(format_short_timecode(start))
        self.censor_end_entry.set_text(format_short_timecode(end))
        self._refresh_censor_list(select_index=index)

    def _on_remove_censor_bar(self, button, index):
        if 0 <= index < len(self.censor_bars):
            self.censor_bars.pop(index)
        self._refresh_censor_list(select_index=max(0, index - 1))

    def _on_censor_overlay_changed(self, index, x, y, w, h):
        if not (0 <= index < len(self.censor_bars)):
            return
        self.selected_censor_index = index
        bar = self.censor_bars[index]
        bar.update(x=int(round(x)), y=int(round(y)), w=int(round(w)), h=int(round(h)))
        self._refresh_censor_list(select_index=index)

    def _on_censor_overlay_selected(self, index):
        if not (0 <= index < len(self.censor_bars)):
            return
        row = self.censor_list.get_row_at_index(index)
        if row is not None:
            self.censor_list.select_row(row)

    def _on_censor_overlay_created(self, x, y, w, h):
        if not self.current_file:
            return
        bar = {
            'x': int(x), 'y': int(y), 'w': int(w), 'h': int(h),
            'start': 0.0, 'end': float(self.duration_seconds),
        }
        self.censor_bars.append(bar)
        self._refresh_censor_list(select_index=len(self.censor_bars) - 1)

    def _current_preview_seconds(self):
        stream = self.video.get_media_stream()
        if stream is not None:
            try:
                return max(0.0, float(stream.get_timestamp()) / 1_000_000.0)
            except Exception:
                pass
        return max(0.0, float(getattr(self.timeline, 'position', 0.0)))

    def _on_censor_set_current(self, button, which):
        value = self._current_preview_seconds()
        entry = self.censor_start_entry if which == 'start' else self.censor_end_entry
        entry.set_text(format_short_timecode(value))

    def _on_apply_censor_time(self, button=None):
        index = getattr(self, 'selected_censor_index', None)
        if index is None or not (0 <= index < len(self.censor_bars)):
            return
        try:
            start = max(0.0, parse_timecode(self.censor_start_entry.get_text()))
            end = min(self.duration_seconds, parse_timecode(self.censor_end_entry.get_text()))
        except ValueError as exc:
            self._show_load_error(str(exc))
            return
        if end <= start:
            self._show_load_error('Censor end time must be after its start time.')
            return
        self.censor_bars[index]['start'] = start
        self.censor_bars[index]['end'] = end
        self._refresh_censor_list(select_index=index)

    def _install_css(self):
        css = b'''
        .preview-area, .player-area { background: #111111; min-height: 360px; }
        .preview-area { padding: 24px; }
        .preview-area label { color: #dddddd; }
        /* Button labels must follow the active GTK theme rather than inherit
         * the light-on-dark preview text colour. */
        .preview-area .preview-download-button label { color: @theme_fg_color; }
        .info-card { padding: 0; }
        .theme-toggle {
            min-width: 30px;
            min-height: 30px;
            padding: 3px;
            border-radius: 999px;
        }
        .theme-toggle image {
            min-width: 18px;
            min-height: 18px;
        }
        frame.flat-icon-slot,
        .flat-icon-slot {
            border: 0;
            border-style: none;
            border-radius: 0;
            box-shadow: none;
            background: transparent;
            padding: 0;
            margin: 0;
        }

        .remove-audio-button,
        .remove-audio-label {
            font-weight: 700;
        }

        .export-button { font-weight: 700; font-size: 1.08em; }
        .tool-section-frame {
            background: alpha(#000000, 0.075);
            border-radius: 8px;
        }
        .tool-section-title {
            font-weight: 700;
            font-size: 1.05em;
            padding: 2px 9px;
            background: #303030;
            color: #f2f2f2;
            border: 1px solid alpha(@theme_fg_color, 0.18);
            border-radius: 7px;
        }
        .main-tab-header { padding: 0; margin: 0; }
        .main-tab-bar { padding: 0; }

        .tab-icon-slot {
            padding: 0;
            margin: 0;
            border: none;
            background: transparent;
            min-width: 64px;
            min-height: 64px;
        }

        /* Treat the tabs as a strip rather than four separate framed buttons.
         * Inactive tabs recede; the selected tab shares the tool-pane surface. */
        .main-tab-button {
            font-size: 1.08em;
            font-weight: 600;
            padding: 0 16px;
            min-height: 0;
            border-radius: 15px 15px 0 0;
            border-left-width: 0;
            border-right-width: 0;
            border-top-width: 0;
            border-bottom: 1px solid alpha(@theme_fg_color, 0.16);
            background: alpha(#000000, 0.16);
            box-shadow: none;
            outline: none;
        }

        .main-tab-button:hover:not(:checked) {
            background: alpha(#000000, 0.08);
        }

        /* Make inactive icons visibly recede without changing the SVG files. */
        .main-tab-button:not(:checked) .tab-icon-slot {
            opacity: 0.48;
        }

        .main-tab-button:not(:checked) .tab-title {
            opacity: 0.72;
        }

        .main-tab-button:checked {
            background: @theme_bg_color;
            border-left-width: 0;
            border-right-width: 0;
            border-top-width: 0;
            border-bottom-color: @theme_bg_color;
            box-shadow: none;
        }

        .main-tab-button:checked .tab-icon-slot {
            opacity: 1.0;
        }

        .main-tab-button:checked .tab-title {
            opacity: 1.0;
            font-weight: 700;
            border-bottom: 2px solid @theme_selected_bg_color;
            padding-bottom: 2px;
        }

        /* The tool pane is the continuation of the active tab.  No vertical
         * frame lines: the paned handle itself is enough to show its edge. */
        .left-tool-panel {
            background: @theme_bg_color;
            border: none;
            border-radius: 0;
            padding: 0;
        }

        .left-tool-scroller {
            background: transparent;
            border: none;
        }

        /* Keep splitters draggable without drawing heavy vertical rails. */
        paned > separator {
            background: transparent;
            border: none;
            box-shadow: none;
        }
        .rotate-button { font-size: 0.88em; font-weight: 600; padding: 1px 3px; min-width: 0; min-height: 0; }
        .export-progress-pane { background: #111111; }
        .export-progress-pane label { color: #dddddd; }
        .export-progress-pane progressbar { min-height: 18px; }
        '''
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

    def _install_drop_target(self):
        file_list_target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        file_list_target.connect('drop', self._on_drop_file_list)
        self.preview_stack.add_controller(file_list_target)
        single_file_target = Gtk.DropTarget.new(Gio.File, Gdk.DragAction.COPY)
        single_file_target.connect('drop', self._on_drop_file)
        self.preview_stack.add_controller(single_file_target)

    def _crop_is_neutral(self):
        if not self.video_width or not self.video_height:
            return True
        x, y, w, h = self.crop_overlay.crop
        return (
            abs(x) < 0.5 and abs(y) < 0.5 and
            abs(w - self.video_width) < 0.5 and
            abs(h - self.video_height) < 0.5
        )

    def _on_aspect_ratio_toggled(self, button, ratio_w, ratio_h, custom=False):
        if self._updating_aspect_buttons or not button.get_active():
            return
        if self.crop_overlay is None:
            return
        if custom:
            self._prompt_custom_aspect_ratio()
            return
        if ratio_w is None or ratio_h is None:
            self.custom_aspect_ratio = None
            self.crop_overlay.set_aspect_ratio(None)
            return
        ratio = float(ratio_w) / float(ratio_h)
        self.custom_aspect_ratio = None
        self.crop_overlay.set_aspect_ratio(ratio)
        if self.current_file:
            self._apply_crop_preset(ratio_w, ratio_h)

    def _prompt_custom_aspect_ratio(self):
        dialog = Gtk.Window(title='Custom aspect ratio', transient_for=self, modal=True)
        dialog.set_resizable(False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      margin_top=16, margin_bottom=16, margin_start=16, margin_end=16)
        dialog.set_child(box)
        box.append(Gtk.Label(label='Enter a custom aspect ratio'))
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_halign(Gtk.Align.CENTER)
        w_entry = Gtk.Entry()
        w_entry.set_width_chars(6)
        w_entry.set_placeholder_text('2.39')
        h_entry = Gtk.Entry()
        h_entry.set_width_chars(6)
        h_entry.set_placeholder_text('1')
        row.append(w_entry)
        row.append(Gtk.Label(label=':'))
        row.append(h_entry)
        box.append(row)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label='Cancel')
        ok = Gtk.Button(label='Use ratio')
        ok.add_css_class('suggested-action')
        buttons.append(cancel)
        buttons.append(ok)
        box.append(buttons)

        custom_accepted = {'value': False}

        def cancel_custom(*_):
            custom_accepted['value'] = False
            dialog.close()

        def apply_custom(*_):
            try:
                rw = float(w_entry.get_text().strip())
                rh = float(h_entry.get_text().strip())
                if rw <= 0 or rh <= 0:
                    raise ValueError
            except ValueError:
                w_entry.add_css_class('error')
                h_entry.add_css_class('error')
                return

            custom_accepted['value'] = True
            self.custom_aspect_ratio = rw / rh
            self.crop_overlay.set_aspect_ratio(self.custom_aspect_ratio)
            self.custom_aspect_button.set_label(f'{rw:g}:{rh:g}')

            if self.current_file:
                self._apply_crop_preset(rw, rh)

            dialog.close()

        def on_custom_close(*_):
            if not custom_accepted['value']:
                self._select_free_aspect()
            return False

        cancel.connect('clicked', cancel_custom)
        ok.connect('clicked', apply_custom)
        w_entry.connect('activate', apply_custom)
        h_entry.connect('activate', apply_custom)
        dialog.connect('close-request', on_custom_close)
        dialog.present()

    def _select_free_aspect(self):
        if not hasattr(self, 'aspect_buttons') or not self.aspect_buttons:
            return
        self._updating_aspect_buttons = True
        self.aspect_buttons[0].set_active(True)
        self._updating_aspect_buttons = False
        self.custom_aspect_ratio = None
        self.crop_overlay.set_aspect_ratio(None)
        if hasattr(self, 'custom_aspect_button'):
            self.custom_aspect_button.set_label('Custom...')

    def _clear_aspect_ratio_selection(self):
        self._select_free_aspect()

    def _apply_crop_preset(self, ratio_w, ratio_h):
        if not self.current_file or not self.video_width or not self.video_height:
            self._show_load_error('Load a video before cropping.')
            return
        target = float(ratio_w) / float(ratio_h)
        source = self.video_width / self.video_height
        if source > target:
            h = self.video_height
            w = int(round(h * target))
        else:
            w = self.video_width
            h = int(round(w / target))
        x = int(round((self.video_width - w) / 2))
        y = int(round((self.video_height - h) / 2))
        self.crop_overlay.set_crop(x, y, w, h)

    def _on_crop_overlay_changed(self, x, y, w, h):
        self._updating_crop_entries = True
        values = {'x': x, 'y': y, 'w': w, 'h': h}
        for key, value in values.items():
            if hasattr(self, 'crop_entries'):
                self.crop_entries[key].set_text(str(int(round(value))))
        self._updating_crop_entries = False
        if self.current_file:
            if self._crop_is_neutral():
                self.actions = [a for a in self.actions if not a.startswith('Crop:')]
                self._refresh_actions_list()
            else:
                self._commit_crop_action()

    def _on_crop_entry_changed(self, entry, changed_key):
        if getattr(self, '_updating_crop_entries', False) or not self.current_file:
            return
        try:
            values = {k: int(self.crop_entries[k].get_text()) for k in ('x', 'y', 'w', 'h')}
        except ValueError:
            return
        x, y, w, h = values['x'], values['y'], values['w'], values['h']
        if w <= 0 or h <= 0:
            return

        ratio = self.crop_overlay.aspect_ratio
        if ratio:
            self._updating_crop_entries = True
            if changed_key == 'w':
                h = max(1, int(round(w / ratio)))
                self.crop_entries['h'].set_text(str(h))
            elif changed_key == 'h':
                w = max(1, int(round(h * ratio)))
                self.crop_entries['w'].set_text(str(w))
            self._updating_crop_entries = False
        self.crop_overlay.set_crop(x, y, w, h)

    def _commit_crop_action(self):
        x, y, w, h = (int(round(v)) for v in self.crop_overlay.crop)
        self._set_action('Crop:', f'Crop: {w}×{h} at x={x}, y={y}')

    def _trim_is_neutral(self):
        if self.duration_seconds <= 0:
            return True
        return (
            abs(self.trim_timeline.start) < 0.0005 and
            abs(self.trim_timeline.end - self.duration_seconds) < 0.0005
        )

    def _seek_preview(self, seconds):
        try:
            stream = self.video.get_media_stream()
            if stream is not None and bool(stream.get_property('seekable')):
                stream.seek(int(max(0.0, seconds) * 1_000_000))
        except Exception as exc:
            print(f'Could not seek preview: {exc}')

    def _on_trim_timeline_changed(self, start, end):
        self._updating_trim_entries = True
        if hasattr(self, 'trim_start_entry'):
            self.trim_start_entry.set_text(format_timecode(start))
            self.trim_end_entry.set_text(format_timecode(end))
        self._updating_trim_entries = False
        if self.current_file:
            if self._trim_is_neutral():
                self.actions = [a for a in self.actions if not a.startswith('Trim:')]
                self._refresh_actions_list()
            else:
                self._commit_trim_action()
            if self.trim_timeline.drag_handle == 'start':
                self._seek_preview(start)
            elif self.trim_timeline.drag_handle == 'end':
                self._seek_preview(end)

    def _commit_trim_entry(self, which):
        if getattr(self, '_updating_trim_entries', False) or not self.current_file or self.duration_seconds <= 0:
            return
        entry = self.trim_start_entry if which == 'start' else self.trim_end_entry
        try:
            value = parse_timecode(entry.get_text())
        except ValueError:
            entry.add_css_class('error')
            return
        entry.remove_css_class('error')
        value = max(0.0, min(value, self.duration_seconds))
        start = self.trim_timeline.start
        end = self.trim_timeline.end
        if which == 'start':
            if value > end:
                entry.add_css_class('error')
                return
            start = value
        else:
            if value < start:
                entry.add_css_class('error')
                return
            end = value
        self.trim_timeline.set_range(start, end)
        self._seek_preview(value)

    def _on_trim_entry_activate(self, entry, which):
        self._commit_trim_entry(which)

    def _on_trim_set_clicked(self, button, which):
        self._commit_trim_entry(which)

    def _commit_trim_action(self):
        self._set_action(
            'Trim:',
            f'Trim: {format_timecode(self.trim_timeline.start)} → {format_timecode(self.trim_timeline.end)}'
        )

    def _on_play_pause(self, button):
        stream = self.video.get_media_stream()
        if stream is None:
            return
        try:
            if stream.get_playing():
                stream.pause()
                self._schedule_image_preview()
            else:
                self._hide_image_preview()
                stream.play()
            self._update_transport()
        except Exception as exc:
            print(f'Could not toggle playback: {exc}')

    def _on_timeline_seek(self, seconds):
        if not self.current_file:
            return
        self._seek_preview(seconds)
        self.transport_time.set_text(
            f'{format_timecode(seconds)} / {format_timecode(self.duration_seconds)}'
        )
        if hasattr(self, 'censor_overlay'):
            self.censor_overlay.set_time(seconds)

    def _start_transport_updates(self):
        if self._transport_timer_id is None:
            self._transport_timer_id = GLib.timeout_add(100, self._update_transport)

    def _update_transport(self):
        stream = self.video.get_media_stream()
        if stream is None or not self.current_file:
            self._transport_timer_id = None
            return GLib.SOURCE_REMOVE
        try:
            timestamp = max(0.0, stream.get_timestamp() / 1_000_000.0)
            self.timeline.set_position(min(timestamp, self.duration_seconds or timestamp), notify=False)
            if hasattr(self, 'censor_overlay'):
                self.censor_overlay.set_time(timestamp)
            self.transport_time.set_text(
                f'{format_timecode(timestamp)} / {format_timecode(self.duration_seconds)}'
            )
            self.play_button.set_label('⏸' if stream.get_playing() else '▶')
        except Exception as exc:
            print(f'Could not update transport: {exc}')
        return GLib.SOURCE_CONTINUE

    def _on_drop_file_list(self, target, value, x, y):
        try:
            files = value.get_files()
            if files:
                return self._load_gio_file(files[0])
        except Exception as exc:
            print(f'Drop failed: {exc}')
        return False

    def _on_drop_file(self, target, value, x, y):
        return self._load_gio_file(value)

    def _load_gio_file(self, file):
        if file is None:
            return False
        path = file.get_path()
        if path:
            self.load_file(path)
            return True
        uri = file.get_uri()
        self._show_load_error(f'Viditron currently needs a local file. Got: {uri}')
        return False

    def load_file(self, path):
        if not path:
            return
        path = Path(path)
        if not path.exists():
            self._show_load_error(f'File does not exist: {path}')
            return
        if not path.is_file():
            self._show_load_error(f'Not a regular file: {path}')
            return

        self.current_file = path
        self._hide_image_preview()
        print(f'Loading video: {self.current_file}')

        try:
            gio_file = Gio.File.new_for_path(str(self.current_file))
            self.video.set_file(gio_file)
            stream = self.video.get_media_stream()
            self.preview_stack.set_visible_child_name('video')
        except Exception as exc:
            self._show_load_error(f'Could not create video preview: {exc}')
            return

        self.video_title.set_text(self.current_file.name)
        self.open_other_button.set_visible(True)
        self.download_other_button.set_visible(True)
        self.video_info.set_text('Reading video information...')
        self.audio_info.set_text('')

        try:
            info = probe_video(self.current_file)
        except Exception as exc:
            self._show_load_error(f'ffprobe could not read the file: {exc}')
            return

        self.video_width = int(info['width'] or 0)
        self.video_height = int(info['height'] or 0)
        self.duration_seconds = float(info['duration_seconds'] or 0.0)
        self.crop_overlay.set_source(self.video_width, self.video_height)
        self.censor_overlay.set_source(self.video_width, self.video_height)
        self.censor_overlay.set_time(0.0)
        self.censor_overlay.set_state(
            self.censor_bars,
            getattr(self, 'selected_censor_index', None),
        )
        self.censor_overlay.set_edit_mode(getattr(self, '_active_tool_tab', 'crop') == 'censor')
        self.censor_timeline.set_state(
            self.censor_bars, self.duration_seconds, getattr(self, 'selected_censor_index', None)
        )
        self.trim_timeline.set_duration(self.duration_seconds)
        self.crop_overlay.set_active(True)
        self._on_crop_overlay_changed(*self.crop_overlay.crop)
        self._on_trim_timeline_changed(self.trim_timeline.start, self.trim_timeline.end)

        self.timeline.set_position(0.0, notify=False)
        self.transport_time.set_text(f'{format_timecode(0)} / {format_timecode(self.duration_seconds)}')
        self.play_button.set_label('▶')
        self._start_transport_updates()

        geometry = 'Unknown resolution'
        if info['width'] and info['height']:
            geometry = f"{info['width']}×{info['height']}"
        fps = f"{info['fps']:.2f} fps" if info['fps'] is not None else 'Unknown fps'
        video_codec = (info['video_codec'] or 'unknown').upper()
        primary = [geometry, info['duration'], fps, video_codec]
        if info['bitrate']:
            primary.append(info['bitrate'])
        primary.append(info['size'])
        self.video_info.set_text('  •  '.join(primary))

        audio_bits = []
        if info['audio_codec']:
            audio_bits.append(info['audio_codec'].upper())
        if info['audio_rate']:
            try:
                audio_bits.append(f"{int(info['audio_rate']) / 1000:g} kHz")
            except ValueError:
                audio_bits.append(f"{info['audio_rate']} Hz")
        if info['audio_layout']:
            audio_bits.append(info['audio_layout'])
        extra = f"Audio: {'  •  '.join(audio_bits)}" if audio_bits else 'Audio: no audio stream detected'
        if info['pixel_format']:
            extra += f"     Video format: {info['pixel_format']}"
        self.audio_info.set_text(extra)

    def _show_load_error(self, message):
        print(message)
        self.video_info.set_text(message)

    def _on_download_video(self, button=None):
        if shutil.which('yt-dlp') is None:
            self._show_export_error(
                'yt-dlp is not installed.',
                'Install it with your package manager, for example:\n\n'
                'sudo apt install yt-dlp\n\n'
                'Then try Download Video again.',
            )
            return

        dialog = Gtk.Dialog(
            title='Download Video',
            transient_for=self,
            modal=True,
        )
        dialog.add_button('Cancel', Gtk.ResponseType.CANCEL)
        dialog.add_button('Download', Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)

        content = dialog.get_content_area()
        content.set_spacing(12)
        content.set_margin_top(16)
        content.set_margin_bottom(16)
        content.set_margin_start(16)
        content.set_margin_end(16)

        label = Gtk.Label(
            label='Paste a video URL. Viditron will download it with yt-dlp and open it automatically.',
            xalign=0,
        )
        label.set_wrap(True)
        content.append(label)

        entry = Gtk.Entry()
        entry.set_placeholder_text('YouTube, Instagram, Vimeo, or other video URL...')
        entry.set_hexpand(True)
        entry.set_activates_default(True)
        content.append(entry)

        location = Gtk.Label(
            label='Downloads are saved in ~/Downloads/Viditron/',
            xalign=0,
        )
        location.add_css_class('dim-label')
        content.append(location)

        def on_response(dlg, response):
            if response == Gtk.ResponseType.OK:
                url = entry.get_text().strip()
                if url:
                    dlg.destroy()
                    self._start_ytdlp_download(url)
                    return
            dlg.destroy()

        dialog.connect('response', on_response)
        dialog.present()

    def _start_ytdlp_download(self, url):
        download_dir = Path.home() / 'Downloads' / 'Viditron'
        download_dir.mkdir(parents=True, exist_ok=True)

        output_template = str(download_dir / '%(title).180B [%(id)s].%(ext)s')

        cmd = [
            'yt-dlp',
            '--newline',
            '--no-playlist',
            '-f', 'bv*+ba/b',
            '--merge-output-format', 'mp4',
            '-o', output_template,
            '--progress-template',
            'download:__QVPROGRESS__%(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s',
            '--print',
            'after_move:__QVFILE__%(filepath)s',
            url,
        ]

        print('yt-dlp command:')
        print(shlex.join(cmd))

        self.export_progress_title.set_text('Downloading video...')
        self.export_progress_filename.set_text(url)
        self.export_progress_bar.set_fraction(0.0)
        self.export_progress_bar.set_text('0%')
        self.export_progress_status.set_text('Connecting with yt-dlp...')
        self.center_stack.set_visible_child_name('progress')

        self._download_process = None
        self._download_final_path = None

        threading.Thread(
            target=self._run_ytdlp_download,
            args=(cmd, url, True),
            daemon=True,
        ).start()

    def _run_ytdlp_download(self, cmd, url=None, allow_instagram_retry=False):
        stderr_lines = []
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            GLib.idle_add(self._on_ytdlp_failed, f'Could not start yt-dlp: {exc}')
            return

        self._download_process = process

        def drain_stderr():
            if process.stderr is None:
                return
            for line in process.stderr:
                stderr_lines.append(line)
                if len(stderr_lines) > 300:
                    del stderr_lines[:100]

        threading.Thread(target=drain_stderr, daemon=True).start()

        final_path = None

        if process.stdout is not None:
            for raw in process.stdout:
                line = raw.strip()
                if not line:
                    continue

                if line.startswith('__QVPROGRESS__'):
                    payload = line[len('__QVPROGRESS__'):]
                    parts = payload.split('|')
                    percent_text = parts[0].strip() if parts else ''
                    speed = parts[1].strip() if len(parts) > 1 else ''
                    eta = parts[2].strip() if len(parts) > 2 else ''

                    match = re.search(r'([0-9]+(?:\.[0-9]+)?)', percent_text)
                    fraction = 0.0
                    if match:
                        try:
                            fraction = max(0.0, min(1.0, float(match.group(1)) / 100.0))
                        except ValueError:
                            pass

                    GLib.idle_add(
                        self._update_ytdlp_progress,
                        fraction,
                        percent_text,
                        speed,
                        eta,
                    )

                elif line.startswith('__QVFILE__'):
                    final_path = line[len('__QVFILE__'):].strip()

        return_code = process.wait()

        if return_code != 0:
            detail = ''.join(stderr_lines).strip()
            if len(detail) > 4000:
                detail = detail[-4000:]

            # Instagram increasingly requires an authenticated browser session,
            # even for some otherwise-public posts/reels. Try anonymously first
            # so normal downloads remain privacy-friendly; only if Instagram
            # rejects that attempt do we retry using the user's Firefox cookies.
            is_instagram = bool(url and 'instagram.com' in url.lower())
            used_browser_cookies = '--cookies-from-browser' in cmd
            if is_instagram and allow_instagram_retry and not used_browser_cookies:
                GLib.idle_add(
                    self.export_progress_status.set_text,
                    'Instagram blocked the anonymous request; retrying with Firefox login cookies...',
                )
                retry_cmd = list(cmd)
                # URL is the final argument in the command constructed above.
                retry_cmd[-1:-1] = ['--cookies-from-browser', 'firefox']
                print('Instagram retry with Firefox cookies:')
                print(shlex.join(retry_cmd))
                self._run_ytdlp_download(
                    retry_cmd,
                    url=url,
                    allow_instagram_retry=False,
                )
                return

            if is_instagram and used_browser_cookies:
                extra = (
                    '\n\nInstagram may require you to be logged in in Firefox. '
                    'Open instagram.com in Firefox, sign in, and try again.'
                )
                detail = (detail or f'yt-dlp exited with code {return_code}.') + extra

            GLib.idle_add(
                self._on_ytdlp_failed,
                detail or f'yt-dlp exited with code {return_code}.',
            )
            return

        if final_path and Path(final_path).exists():
            GLib.idle_add(self._on_ytdlp_finished, final_path)
        else:
            GLib.idle_add(
                self._on_ytdlp_failed,
                'yt-dlp finished, but Viditron could not determine the downloaded file path.',
            )

    def _update_ytdlp_progress(self, fraction, percent_text, speed, eta):
        self.export_progress_bar.set_fraction(fraction)
        self.export_progress_bar.set_text(percent_text or f'{fraction * 100:.0f}%')

        bits = []
        if speed and speed != 'NA':
            bits.append(speed)
        if eta and eta != 'NA':
            bits.append(f'ETA {eta}')

        self.export_progress_status.set_text('  •  '.join(bits) if bits else 'Downloading...')
        return False

    def _on_ytdlp_finished(self, path):
        print(f'yt-dlp download complete: {path}')
        self.export_progress_bar.set_fraction(1.0)
        self.export_progress_bar.set_text('100%')
        self.export_progress_status.set_text('Opening downloaded video...')

        self.center_stack.set_visible_child_name('editor')
        self.load_file(path)
        return False

    def _on_ytdlp_failed(self, detail):
        print('yt-dlp download failed:')
        print(detail)

        self.center_stack.set_visible_child_name('editor')
        self._show_export_error('Could not download the video.', detail)
        return False

    def _on_open_video(self, button):
        self.file_chooser.open_file('Open Video', self._on_video_chosen)

    def _on_video_chosen(self, file, error):
        if error is not None:
            self._show_load_error(f'File chooser error: {error}')
            return
        if file is not None:
            self._load_gio_file(file)

    def _slider_action(self, name, value, digits):
        if self._resetting_adjustments:
            return

        defaults = {
            'Brightness': 0.0,
            'Contrast': 1.0,
            'Saturation': 1.0,
            'Gamma': 1.0,
            'Temperature': 0.0,
            'Sharpness': 0.0,
            'Playback speed': 1.0,
            'Volume': 1.0,
            'Pitch': 0.0,
            'Boost voice clarity': 0.0,
            'Rumble reduction': 20.0,
        }

        prefix = f'{name}:'
        self.actions = [a for a in self.actions if not a.startswith(prefix)]
        default = defaults.get(name)
        if default is None or abs(value - default) > 0.0005:
            self.actions.append(f'{name}: {value:.{digits}f}')
        self._refresh_actions_list()

        if name in {'Brightness', 'Contrast', 'Saturation', 'Gamma', 'Temperature', 'Sharpness'}:
            self._schedule_image_preview()

    def _image_adjustment_filters(self):
        values = {}
        for action in self.actions:
            for name in (
                'Brightness',
                'Contrast',
                'Saturation',
                'Gamma',
                'Temperature',
                'Sharpness',
            ):
                if action.startswith(name + ':'):
                    values[name] = float(action.split(':', 1)[1].strip())
        parts = []
        eq = []
        if 'Brightness' in values: eq.append(f"brightness={values['Brightness']:.6g}")
        if 'Contrast' in values: eq.append(f"contrast={values['Contrast']:.6g}")
        if 'Saturation' in values: eq.append(f"saturation={values['Saturation']:.6g}")
        if 'Gamma' in values: eq.append(f"gamma={values['Gamma']:.6g}")
        if eq:
            parts.append('eq=' + ':'.join(eq))

        if 'Temperature' in values:
            t = max(-1.0, min(1.0, values['Temperature']))
            # Warm: boost red, reduce blue. Cool: reverse.
            red_gain = max(0.50, min(1.50, 1.0 + 0.40 * t))
            blue_gain = max(0.50, min(1.50, 1.0 - 0.40 * t))
            parts.append(
                f'colorchannelmixer=rr={red_gain:.6g}:gg=1:bb={blue_gain:.6g}'
            )

        if 'Sharpness' in values:
            sh = values['Sharpness']
            if abs(sh) > 0.0005:
                # Positive values sharpen; negative values soften.
                # Use a larger kernel and stronger amount so changes are visible.
                amount = max(-1.5, min(3.0, sh * 1.5))
                parts.append(f'unsharp=7:7:{amount:.6g}:7:7:0')

        return parts

    def _schedule_image_preview(self):
        if not self.current_file:
            return
        stream = self.video.get_media_stream()
        if stream is None:
            return
        try:
            if stream.get_playing():
                self.image_preview_picture.set_visible(False)
                return
        except Exception:
            return
        self._image_preview_generation += 1
        generation = self._image_preview_generation
        if self._image_preview_timer_id is not None:
            GLib.source_remove(self._image_preview_timer_id)
        self._image_preview_timer_id = GLib.timeout_add(100, self._start_image_preview_render, generation)

    def _start_image_preview_render(self, generation):
        self._image_preview_timer_id = None
        if generation != self._image_preview_generation or not self.current_file:
            return GLib.SOURCE_REMOVE
        filters = self._image_adjustment_filters()
        if not filters:
            self.image_preview_picture.set_visible(False)
            return GLib.SOURCE_REMOVE
        stream = self.video.get_media_stream()
        try:
            timestamp = max(0.0, stream.get_timestamp() / 1000000.0)
        except Exception:
            timestamp = 0.0
        threading.Thread(target=self._render_image_preview_frame,
                         args=(generation, timestamp, filters), daemon=True).start()
        return GLib.SOURCE_REMOVE

    def _render_image_preview_frame(self, generation, timestamp, filters):
        path = Path(tempfile.gettempdir()) / f'viditron-preview-{generation}.png'
        cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
               '-ss', f'{timestamp:.6f}', '-i', str(self.current_file),
               '-frames:v', '1', '-vf', ','.join(filters), str(path)]
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        if result.returncode:
            print('Adjustment preview failed:', result.stderr.strip())
            return
        GLib.idle_add(self._show_image_preview, generation, str(path))

    def _show_image_preview(self, generation, path):
        if generation != self._image_preview_generation:
            Path(path).unlink(missing_ok=True)
            return GLib.SOURCE_REMOVE
        self.image_preview_picture.set_filename(path)
        self.image_preview_picture.set_visible(True)
        return GLib.SOURCE_REMOVE

    def _hide_image_preview(self):
        self._image_preview_generation += 1
        if self._image_preview_timer_id is not None:
            GLib.source_remove(self._image_preview_timer_id)
            self._image_preview_timer_id = None
        if hasattr(self, 'image_preview_picture'):
            self.image_preview_picture.set_visible(False)

    def _on_reset_adjustments(self, button=None):
        defaults = {
            'Brightness': 0.0,
            'Contrast': 1.0,
            'Saturation': 1.0,
            'Gamma': 1.0,
            'Temperature': 0.0,
            'Sharpness': 0.0,
            'Playback speed': 1.0,
        }

        prefixes = tuple(name + ':' for name in defaults)
        self.actions = [a for a in self.actions if not a.startswith(prefixes)]

        self._resetting_adjustments = True
        try:
            for name, value in defaults.items():
                scale = self.adjustment_scales.get(name)
                if scale is not None:
                    scale.set_value(value)
        finally:
            self._resetting_adjustments = False

        self._hide_image_preview()
        self._refresh_actions_list()

    def _reset_slider_for_action(self, action):
        defaults = {
            'Brightness': 0.0,
            'Contrast': 1.0,
            'Saturation': 1.0,
            'Gamma': 1.0,
            'Temperature': 0.0,
            'Sharpness': 0.0,
            'Playback speed': 1.0,
            'Volume': 1.0,
            'Pitch': 0.0,
            'Boost voice clarity': 0.0,
            'Rumble reduction': 20.0,
        }
        name = action.split(':', 1)[0]
        if name not in defaults:
            return

        scale = self.adjustment_scales.get(name)
        if scale is None:
            return

        self._resetting_adjustments = True
        try:
            scale.set_value(defaults[name])
        finally:
            self._resetting_adjustments = False

        if name in {'Brightness', 'Contrast', 'Saturation', 'Gamma', 'Temperature', 'Sharpness'}:
            self._schedule_image_preview()

    def _set_action(self, prefix, text):
        self.actions = [a for a in self.actions if not a.startswith(prefix)]
        self.actions.append(text)
        self._refresh_actions_list()

    def _current_rotation_quarters(self):
        for action in self.actions:
            if action == 'Rotate: 90\N{DEGREE SIGN} right':
                return 1
            if action == 'Rotate: 180\N{DEGREE SIGN}':
                return 2
            if action == 'Rotate: 90\N{DEGREE SIGN} left':
                return 3
        return 0

    def _set_rotation_quarters(self, quarters):
        quarters %= 4
        self.actions = [a for a in self.actions if not a.startswith('Rotate:')]

        if quarters == 1:
            self.actions.append('Rotate: 90\N{DEGREE SIGN} right')
        elif quarters == 2:
            self.actions.append('Rotate: 180\N{DEGREE SIGN}')
        elif quarters == 3:
            self.actions.append('Rotate: 90\N{DEGREE SIGN} left')

    def _apply_transform_action(self, action):
        if action == 'Rotate: 90\N{DEGREE SIGN} right':
            self._set_rotation_quarters(self._current_rotation_quarters() + 1)
        elif action == 'Rotate: 90\N{DEGREE SIGN} left':
            self._set_rotation_quarters(self._current_rotation_quarters() - 1)
        elif action in {'Flip: horizontal', 'Flip: vertical'}:
            if action in self.actions:
                self.actions.remove(action)
            else:
                self.actions.append(action)

        self._refresh_actions_list()

    def _add_action(self, text):
        self.actions.append(text)
        self._refresh_actions_list()

    def _refresh_actions_list(self):
        child = self.actions_list.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.actions_list.remove(child)
            child = next_child

        if not self.actions:
            row = Gtk.ListBoxRow()
            label = Gtk.Label(
                label='No actions added yet', xalign=0,
                margin_top=10, margin_bottom=10, margin_start=10, margin_end=10,
            )
            row.set_child(label)
            self.actions_list.append(row)
            return

        for index, action in enumerate(self.actions, start=1):
            row = Gtk.ListBoxRow()
            line = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL, spacing=10,
                margin_top=8, margin_bottom=8, margin_start=10, margin_end=10,
            )
            label = Gtk.Label(label=f'{index}. {action}', xalign=0)
            label.set_hexpand(True)
            label.set_wrap(True)
            line.append(label)
            remove = Gtk.Button(label='Remove')
            remove.connect('clicked', self._remove_action, action)
            line.append(remove)
            row.set_child(line)
            self.actions_list.append(row)

    def _reset_crop_edit(self):
        if not self.video_width or not self.video_height:
            return
        self._clear_aspect_ratio_selection()
        self.crop_overlay.set_crop(0, 0, self.video_width, self.video_height, notify=False)
        self._on_crop_overlay_changed(*self.crop_overlay.crop)

    def _reset_trim_edit(self):
        if self.duration_seconds <= 0:
            return
        self.trim_timeline.set_range(0.0, self.duration_seconds, notify=False)
        self._on_trim_timeline_changed(0.0, self.duration_seconds)
        self.timeline.set_position(0.0, notify=False)
        self._seek_preview(0.0)

    def _remove_action(self, button, action):
        try:
            self.actions.remove(action)
        except ValueError:
            return
        if action.startswith('Crop:'):
            self._reset_crop_edit()
        elif action.startswith('Trim:'):
            self._reset_trim_edit()
        elif action.startswith('Censor:'):
            match = re.match(r'Censor: bar (\d+)', action)
            if match:
                index = int(match.group(1)) - 1
                if 0 <= index < len(self.censor_bars):
                    self.censor_bars.pop(index)
                    self._refresh_censor_list(select_index=max(0, index - 1))
                    return
        else:
            self._reset_slider_for_action(action)
        self._refresh_actions_list()

    def _on_clear_actions(self, button):
        had_crop = any(a.startswith('Crop:') for a in self.actions)
        had_trim = any(a.startswith('Trim:') for a in self.actions)
        had_censor = bool(self.censor_bars)
        slider_actions = list(self.actions)
        self.actions.clear()
        if had_censor:
            self.censor_bars.clear()
            self.selected_censor_index = None
            self.censor_overlay.set_active(False)
            self._refresh_censor_list()
        if had_crop:
            self._reset_crop_edit()
        if had_trim:
            self._reset_trim_edit()
        for action in slider_actions:
            self._reset_slider_for_action(action)
        self._refresh_actions_list()

    def _on_reset(self, button):
        stream = self.video.get_media_stream()
        if stream:
            stream.pause()
        self.video.set_file(None)
        self.preview_stack.set_visible_child_name('empty')
        self.current_file = None
        self.actions.clear()
        self.censor_bars.clear()
        self.selected_censor_index = None
        if hasattr(self, 'censor_overlay'):
            self.censor_overlay.set_active(False)
        if hasattr(self, 'censor_list'):
            self._refresh_censor_list()
        self.video_width = 0
        self.video_height = 0
        self.duration_seconds = 0.0
        self.timeline.set_duration(0.0)
        self.transport_time.set_text('00:00:00.000 / 00:00:00.000')
        self.play_button.set_label('▶')
        self.crop_overlay.set_active(False)
        self.video_title.set_text('No video loaded')
        self.open_other_button.set_visible(False)
        self.download_other_button.set_visible(False)
        self.video_info.set_text('Resolution, duration, FPS and codecs will appear here.')
        self.audio_info.set_text('')
        for entry in self.crop_entries.values():
            entry.set_text('')
        self.trim_start_entry.set_text('')
        self.trim_end_entry.set_text('')
        self._refresh_actions_list()

    def _build_ffmpeg_command(self, output_path):
        if not self.current_file:
            raise ValueError('Load a video before exporting.')

        cmd = ['ffmpeg', '-y']

        # Trim is done as an input seek + duration. Because we re-encode video,
        # this remains frame-accurate while avoiding unnecessary decoding.
        if any(a.startswith('Trim:') for a in self.actions):
            start = max(0.0, float(self.trim_timeline.start))
            end = max(start, float(self.trim_timeline.end))
            if start > 0.0005:
                cmd += ['-ss', f'{start:.6f}']
            if end < self.duration_seconds - 0.0005 or start > 0.0005:
                cmd += ['-t', f'{max(0.001, end - start):.6f}']

        cmd += ['-i', str(self.current_file)]

        video_filters = []
        audio_filters = []
        remove_audio = False
        playback_speed = 1.0

        # Preserve action-list order for visual transforms.
        for action in self.actions:
            if action.startswith('Crop:'):
                x, y, w, h = (int(round(v)) for v in self.crop_overlay.crop)
                video_filters.append(f'crop={w}:{h}:{x}:{y}')
            elif action == 'Rotate: 90\N{DEGREE SIGN} left':
                video_filters.append('transpose=2')
            elif action == 'Rotate: 180\N{DEGREE SIGN}':
                video_filters.extend(['hflip', 'vflip'])
            elif action == 'Rotate: 90\N{DEGREE SIGN} right':
                video_filters.append('transpose=1')
            elif action == 'Flip: horizontal':
                video_filters.append('hflip')
            elif action == 'Flip: vertical':
                video_filters.append('vflip')
            elif action.startswith('Brightness:'):
                value = float(action.split(':', 1)[1].strip())
                video_filters.append(f'eq=brightness={value:.6g}')
            elif action.startswith('Contrast:'):
                value = float(action.split(':', 1)[1].strip())
                video_filters.append(f'eq=contrast={value:.6g}')
            elif action.startswith('Saturation:'):
                value = float(action.split(':', 1)[1].strip())
                video_filters.append(f'eq=saturation={value:.6g}')
            elif action.startswith('Gamma:'):
                value = float(action.split(':', 1)[1].strip())
                video_filters.append(f'eq=gamma={value:.6g}')
            elif action.startswith('Temperature:'):
                value = max(-1.0, min(1.0, float(action.split(':', 1)[1].strip())))
                red_gain = max(0.50, min(1.50, 1.0 + 0.40 * value))
                blue_gain = max(0.50, min(1.50, 1.0 - 0.40 * value))
                video_filters.append(
                    f'colorchannelmixer=rr={red_gain:.6g}:gg=1:bb={blue_gain:.6g}'
                )
            elif action.startswith('Sharpness:'):
                raw_value = float(action.split(':', 1)[1].strip())
                value = max(-1.5, min(3.0, raw_value * 1.5))
                if abs(value) > 0.0005:
                    video_filters.append(f'unsharp=7:7:{value:.6g}:7:7:0')
            elif action.startswith('Playback speed:'):
                playback_speed = max(0.25, min(4.0, float(action.split(':', 1)[1].strip())))
            elif action.startswith('Volume:'):
                value = float(action.split(':', 1)[1].strip())
                audio_filters.append(f'volume={value:.6g}')
            elif action.startswith('Pitch:'):
                semitones = float(action.split(':', 1)[1].strip())
                factor = 2.0 ** (semitones / 12.0)
                # Change sample rate to shift pitch, then compensate tempo.
                audio_filters.append(f'asetrate=48000*{factor:.8g}')
                audio_filters.append('aresample=48000')
                remaining = 1.0 / factor
                while remaining > 2.0:
                    audio_filters.append('atempo=2.0')
                    remaining /= 2.0
                while remaining < 0.5:
                    audio_filters.append('atempo=0.5')
                    remaining /= 0.5
                audio_filters.append(f'atempo={remaining:.8g}')
            elif action.startswith('Boost voice clarity:'):
                gain = float(action.split(':', 1)[1].strip())
                if gain > 0.001:
                    audio_filters.append(f'equalizer=f=3000:t=q:w=1.2:g={gain:.6g}')
            elif action.startswith('Rumble reduction:'):
                cutoff = float(action.split(':', 1)[1].strip())
                if cutoff > 20.5:
                    audio_filters.append(f'highpass=f={cutoff:.6g}')
            elif action == 'Audio: mute':
                audio_filters.append('volume=0')
            elif action == 'Audio: normalize':
                audio_filters.append('loudnorm')
            elif action == 'Audio: remove track':
                remove_audio = True

        # Censor bars are kept as structured data so their coordinates and
        # time ranges can be edited interactively without parsing action text.
        for bar in self.censor_bars:
            start = max(0.0, float(bar['start']))
            end = max(start, float(bar['end']))
            enable = f"between(t,{start:.6f},{end:.6f})"
            video_filters.append(
                f"drawbox=x={int(bar['x'])}:y={int(bar['y'])}:"
                f"w={int(bar['w'])}:h={int(bar['h'])}:"
                f"color=black:t=fill:enable='{enable}'"
            )

        if abs(playback_speed - 1.0) > 0.0005:
            video_filters.append(f'setpts=PTS/{playback_speed:.6g}')

            # atempo supports 0.5..2.0 per stage; chain stages when needed.
            remaining = playback_speed
            tempo_parts = []
            while remaining > 2.0:
                tempo_parts.append('atempo=2.0')
                remaining /= 2.0
            while remaining < 0.5:
                tempo_parts.append('atempo=0.5')
                remaining /= 0.5
            tempo_parts.append(f'atempo={remaining:.6g}')
            audio_filters.extend(tempo_parts)

        if video_filters:
            cmd += ['-vf', ','.join(video_filters)]

        lossless = self.lossless_check.get_active()
        target_size = self.target_size_check.get_active() and not lossless

        preset = {
            0: 'fast',
            1: 'medium',
            2: 'slow',
        }.get(int(self.export_preset_combo.get_selected()), 'medium')

        if lossless:
            cmd += ['-c:v', 'libx264', '-preset', preset, '-crf', '0']
        elif target_size:
            target_mb = max(1.0, float(self.target_size_spin.get_value()))
            output_duration = self._effective_export_duration()

            audio_kbps = 0.0 if remove_audio else 192.0
            total_kbps = (target_mb * 8.0 * 1000.0) / output_duration
            video_kbps = max(100.0, total_kbps - audio_kbps - 16.0)

            cmd += [
                '-c:v', 'libx264',
                '-preset', preset,
                '-b:v', f'{video_kbps:.0f}k',
            ]
        else:
            quality = max(0.0, min(100.0, float(self.export_quality_scale.get_value())))
            crf = 35.0 - (quality / 100.0) * 21.0
            cmd += [
                '-c:v', 'libx264',
                '-preset', preset,
                '-crf', f'{crf:.1f}',
            ]

        if remove_audio:
            cmd += ['-an']
        else:
            if audio_filters:
                cmd += ['-af', ','.join(audio_filters)]

            if lossless:
                if Path(output_path).suffix.lower() == '.mkv':
                    cmd += ['-c:a', 'flac']
                else:
                    cmd += ['-c:a', 'alac']
            else:
                cmd += ['-c:a', 'aac', '-b:a', '192k']

        # MP4/MOV playback starts faster when metadata is at the front.
        if Path(output_path).suffix.lower() in ('.mp4', '.m4v', '.mov'):
            cmd += ['-movflags', '+faststart']

        # Machine-readable progress is written to stdout and parsed by the UI.
        cmd += ['-progress', 'pipe:1', '-nostats']
        cmd.append(str(output_path))
        return cmd

    def _on_export(self, button):
        if not self.current_file:
            self._show_export_error('Load a video before exporting.')
            return

        stem = self.current_file.stem
        suggested = f'{stem}-edited.mp4'
        self.file_chooser.save_file('Export Video', self._on_export_path_chosen, suggested)

    def _on_export_path_chosen(self, file, error):
        if error is not None:
            self._show_export_error(f'Save dialog error: {error}')
            return
        if file is None:
            return
        output_path = file.get_path()
        if not output_path:
            self._show_export_error('Viditron currently needs a local output path.')
            return

        try:
            cmd = self._build_ffmpeg_command(output_path)
        except Exception as exc:
            self._show_export_error(str(exc))
            return

        print('FFmpeg command:')
        print(shlex.join(cmd))

        if any(a.startswith('Trim:') for a in self.actions):
            expected_duration = max(0.001, float(self.trim_timeline.end) - float(self.trim_timeline.start))
        else:
            expected_duration = max(0.001, float(self.duration_seconds or 0.001))

        self._export_output_path = output_path
        self._export_expected_duration = expected_duration
        self._export_started_at = time.monotonic()
        self.export_button.set_sensitive(False)
        self.export_button.set_label('Exporting...')
        self.export_progress_title.set_text('Exporting video...')
        self.export_progress_filename.set_text(Path(output_path).name)
        self.export_progress_bar.set_fraction(0.0)
        self.export_progress_bar.set_text('0%')
        self.export_progress_status.set_text('Starting FFmpeg...')
        self.center_stack.set_visible_child_name('progress')
        self.video_info.set_text(f'Exporting to {Path(output_path).name}...')

        worker = threading.Thread(
            target=self._run_ffmpeg_export,
            args=(cmd, output_path, expected_duration),
            daemon=True,
        )
        worker.start()

    def _run_ffmpeg_export(self, cmd, output_path, expected_duration):
        stderr_lines = []
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            GLib.idle_add(self._on_export_start_failed, str(exc))
            return

        self._export_process = process

        def drain_stderr():
            if process.stderr is None:
                return
            for line in process.stderr:
                stderr_lines.append(line)
                if len(stderr_lines) > 400:
                    del stderr_lines[:100]

        threading.Thread(target=drain_stderr, daemon=True).start()

        try:
            if process.stdout is not None:
                for raw in process.stdout:
                    line = raw.strip()
                    if not line or '=' not in line:
                        continue
                    key, value = line.split('=', 1)
                    if key == 'out_time':
                        try:
                            elapsed_media = parse_timecode(value)
                        except ValueError:
                            continue
                        fraction = max(0.0, min(1.0, elapsed_media / expected_duration))
                        GLib.idle_add(self._update_export_progress, fraction, elapsed_media)
                    elif key == 'progress' and value == 'end':
                        GLib.idle_add(self._update_export_progress, 1.0, expected_duration)

            return_code = process.wait()
        except Exception as exc:
            try:
                process.kill()
            except Exception:
                pass
            GLib.idle_add(self._on_export_worker_failed, str(exc))
            return

        detail = ''.join(stderr_lines).strip()
        GLib.idle_add(self._on_ffmpeg_process_finished, return_code, output_path, detail)

    def _update_export_progress(self, fraction, media_seconds):
        self.export_progress_bar.set_fraction(fraction)
        self.export_progress_bar.set_text(f'{fraction * 100:.0f}%')

        wall_elapsed = max(0.001, time.monotonic() - getattr(self, '_export_started_at', time.monotonic()))
        if fraction > 0.01 and fraction < 0.999:
            total_estimate = wall_elapsed / fraction
            remaining = max(0.0, total_estimate - wall_elapsed)
            self.export_progress_status.set_text(
                f'{format_timecode(media_seconds)} processed  •  about {self._format_short_duration(remaining)} remaining'
            )
        elif fraction >= 0.999:
            self.export_progress_status.set_text('Finishing file...')
        else:
            self.export_progress_status.set_text('Encoding...')
        return False

    def _format_short_duration(self, seconds):
        seconds = max(0, int(round(seconds)))
        if seconds < 60:
            return f'{seconds}s'
        minutes, secs = divmod(seconds, 60)
        if minutes < 60:
            return f'{minutes}m {secs:02d}s'
        hours, minutes = divmod(minutes, 60)
        return f'{hours}h {minutes:02d}m'

    def _on_export_start_failed(self, detail):
        self._finish_export_ui()
        self._show_export_error(f'Could not start FFmpeg: {detail}')
        return False

    def _on_export_worker_failed(self, detail):
        self._finish_export_ui()
        self._show_export_error(f'FFmpeg failed: {detail}')
        return False

    def _on_ffmpeg_process_finished(self, return_code, output_path, detail):
        if return_code != 0:
            self._finish_export_ui()
            if len(detail) > 4000:
                detail = detail[-4000:]
            print('FFmpeg export failed:')
            print(detail)
            self._show_export_error('FFmpeg could not export the video.', detail)
            return False

        print(f'Export complete: {output_path}')
        self._show_export_complete_pane(output_path)
        return False

    def _show_export_complete_pane(self, output_path):
        self._completed_export_path = str(output_path)
        self.export_button.set_sensitive(True)
        self.export_button.set_label('Export Video')
        self.export_complete_filename.set_text(str(output_path))
        self.center_stack.set_visible_child_name('complete')
        self.video_info.set_text(f'Export complete: {Path(output_path).name}')

    def _run_completed_file_action(self, action):
        output_path = getattr(self, '_completed_export_path', None)
        if not output_path:
            return
        try:
            action(output_path)
        except Exception as exc:
            self._show_export_error('Could not perform that file action.', str(exc))

    def _on_complete_show_file(self, button):
        self._run_completed_file_action(self._show_file_in_manager)

    def _on_complete_open_file(self, button):
        self._run_completed_file_action(self._open_exported_file)

    def _on_complete_copy_file(self, button):
        self._run_completed_file_action(self._copy_file_to_clipboard)

    def _on_keep_editing(self, button):
        self.center_stack.set_visible_child_name('editor')
        if self.current_file:
            self.video_info.set_text(getattr(self, '_last_video_info_text', self.video_info.get_text()))

    def _open_exported_file(self, output_path):
        uri = Gio.File.new_for_path(str(output_path)).get_uri()
        Gio.AppInfo.launch_default_for_uri(uri, None)

    def _show_file_in_manager(self, output_path):
        file = Gio.File.new_for_path(str(output_path))
        uri = file.get_uri()

        # FileManager1 is implemented by many Linux file managers and can
        # reveal/select the actual file instead of merely opening its folder.
        try:
            proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION,
                Gio.DBusProxyFlags.NONE,
                None,
                'org.freedesktop.FileManager1',
                '/org/freedesktop/FileManager1',
                'org.freedesktop.FileManager1',
                None,
            )
            proxy.call_sync(
                'ShowItems',
                GLib.Variant('(ass)', ([uri], '')),
                Gio.DBusCallFlags.NONE,
                3000,
                None,
            )
            return
        except Exception as exc:
            print(f'FileManager1 ShowItems unavailable, opening containing folder instead: {exc}')

        parent = file.get_parent()
        if parent is not None:
            Gio.AppInfo.launch_default_for_uri(parent.get_uri(), None)

    def _copy_file_to_clipboard(self, output_path):
        file = Gio.File.new_for_path(str(output_path))
        uri = file.get_uri()

        # Linux file managers commonly use x-special/gnome-copied-files for
        # copy/paste semantics. text/uri-list is included for other apps and
        # upload/share targets that accept URI lists.
        copied_files = GLib.Bytes.new(f'copy\n{uri}\n'.encode('utf-8'))
        uri_list = GLib.Bytes.new(f'{uri}\r\n'.encode('utf-8'))
        plain = GLib.Bytes.new(str(output_path).encode('utf-8'))
        kde_copy = GLib.Bytes.new(b'0')

        providers = [
            Gdk.ContentProvider.new_for_bytes('x-special/gnome-copied-files', copied_files),
            Gdk.ContentProvider.new_for_bytes('text/uri-list', uri_list),
            Gdk.ContentProvider.new_for_bytes('application/x-kde-cutselection', kde_copy),
            Gdk.ContentProvider.new_for_bytes('text/plain;charset=utf-8', plain),
        ]
        provider = Gdk.ContentProvider.new_union(providers)
        clipboard = Gdk.Display.get_default().get_clipboard()
        clipboard.set_content(provider)
        print(f'Copied file to clipboard: {output_path}')

    def _finish_export_ui(self):
        self.export_button.set_sensitive(True)
        self.export_button.set_label('Export Video')
        self.center_stack.set_visible_child_name('editor')
        if self.current_file:
            self.video_info.set_text(getattr(self, '_last_video_info_text', self.video_info.get_text()))

    def _show_export_error(self, message, detail=None):
        print(message)
        if detail:
            print(detail)
        dialog = Gtk.AlertDialog()
        dialog.set_message(message)
        if detail:
            dialog.set_detail(detail)
        dialog.show(self)


