"""Drag-to-pick a capture rectangle, GNOME-screenshot style.

One borderless fullscreen overlay per monitor dims the desktop; the current
selection is punched out of the dim layer and stays on screen with grab handles
until it is confirmed. Reopening the picker restores the last rectangle, so the
user can see and adjust what they picked before instead of starting over.

Interaction, following GNOME 46's own UIAreaSelector:
  * drag on empty space  -> draw a new rectangle
  * drag inside          -> move it
  * drag a corner/edge   -> resize (4 visible corner handles, 8 resize zones)
  * a plain click        -> keeps the current selection (does NOT clear it)
  * Enter/Space or the confirm button -> accept,  Esc or Cancel -> discard

Drag-end never confirms: it only ends that one manipulation, so the rectangle
can be nudged repeatedly before committing.

All state lives in X root-window coordinates, which is exactly what x11grab
takes. Text goes through Pango: cairo's toy font API does no font fallback and
renders Korean as tofu boxes.
"""

from __future__ import annotations

import math

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango, PangoCairo  # noqa: E402

from . import core  # noqa: E402
from .i18n import _  # noqa: E402

OVERLAY_CSS = b"""
window.meetrec-overlay { background-color: transparent; }
box.meetrec-bar {
    background-color: rgba(28,28,30,0.92);
    border-radius: 16px;
    padding: 10px 12px;
    margin: 28px;
}
box.meetrec-bar label { color: #ffffff; margin: 0 8px; }
"""

DIM = (0.0, 0.0, 0.0, 0.45)
LINE = (1.0, 1.0, 1.0, 0.95)
WARN = (1.0, 0.45, 0.35, 1.0)
HANDLE_FILL = (1.0, 1.0, 1.0, 1.0)
HANDLE_EDGE = (0.0, 0.0, 0.0, 0.55)

HANDLE_R = 7          # drawn radius of the four corner handles
CORNER_HIT = 13       # grab radius around a corner, GNOME uses 12
EDGE_HIT = 10         # band around each edge, applied inside and outside
DRAG_START = 4        # movement below this is a click, not a new rectangle



def hint() -> str:
    return _("Drag to select · drag a corner to resize · drag inside to move · "
             "Enter to confirm · Esc to cancel")

CORNERS = [("nw", 0.0, 0.0), ("ne", 1.0, 0.0), ("sw", 0.0, 1.0), ("se", 1.0, 1.0)]
WEST, EAST = {"w", "nw", "sw"}, {"e", "ne", "se"}
NORTH, SOUTH = {"n", "nw", "ne"}, {"s", "sw", "se"}
CURSORS = {
    "new": "crosshair", "inside": "move",
    "n": "n-resize", "s": "s-resize", "e": "e-resize", "w": "w-resize",
    "nw": "nw-resize", "ne": "ne-resize", "sw": "sw-resize", "se": "se-resize",
}


def _rounded_rect(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


class _Overlay(Gtk.Window):
    """Fullscreen dim layer for exactly one monitor."""

    def __init__(self, selector: "RegionSelector", monitor: Gdk.Monitor,
                 origin: tuple[int, int], with_bar: bool):
        super().__init__()
        self._sel = selector
        # Root origin comes from xrandr (device pixels) when available; GDK's
        # geometry is in application pixels and the two only agree at scale 1.
        self.origin_x, self.origin_y = origin
        self.scale = monitor.get_scale_factor() or 1

        self.add_css_class("meetrec-overlay")
        self.set_decorated(False)
        self.set_deletable(False)
        self.fullscreen_on_monitor(monitor)

        area = Gtk.DrawingArea()
        area.set_draw_func(self._draw)
        self._area = area

        overlay = Gtk.Overlay()
        overlay.set_child(area)
        self.bar = self._build_bar() if with_bar else None
        if self.bar:
            overlay.add_overlay(self.bar)
        self.set_child(overlay)

        drag = Gtk.GestureDrag()
        drag.set_button(Gdk.BUTTON_PRIMARY)
        drag.connect("drag-begin", self._on_begin)
        drag.connect("drag-update", self._on_update)
        drag.connect("drag-end", self._on_end)
        drag.connect("cancel", lambda *a: self._sel.abort())
        area.add_controller(drag)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        area.add_controller(motion)

        keys = Gtk.EventControllerKey()
        # Capture phase: otherwise Enter goes to whichever button has focus
        # (Cancel, as it comes first) and throws the selection away.
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._on_key)
        self.add_controller(keys)

        self._cursor_name = None
        self._set_cursor("new")

    def _build_bar(self):
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                      halign=Gtk.Align.CENTER, valign=Gtk.Align.END)
        bar.add_css_class("meetrec-bar")
        self.lbl_bar = Gtk.Label(label=hint())
        bar.append(self.lbl_bar)

        cancel = Gtk.Button(label=_("Cancel"))
        cancel.connect("clicked", lambda *a: self._sel.cancel())
        bar.append(cancel)

        ok = Gtk.Button(label=_("Use this area"))
        ok.add_css_class("suggested-action")
        ok.connect("clicked", lambda *a: self._sel.finish())
        bar.append(ok)
        self.btn_ok = ok
        return bar

    def sync_bar(self):
        """Reflect the current selection's validity in the control bar."""
        if not self.bar:
            return
        rect = self._sel.rect_root()
        ok = rect is not None and self._sel.valid(rect)
        self.btn_ok.set_sensitive(ok)
        if rect and not ok:
            self.lbl_bar.set_text(_("Too small, the minimum is {w}×{h} px").format(
                w=core.MIN_REGION_W, h=core.MIN_REGION_H))
        else:
            self.lbl_bar.set_text(hint())

    # ------------------------------------------------------- coordinates
    def _to_root(self, x: float, y: float) -> tuple[int, int]:
        return (self.origin_x + round(x * self.scale),
                self.origin_y + round(y * self.scale))

    def _to_local(self, rx: float, ry: float) -> tuple[float, float]:
        return ((rx - self.origin_x) / self.scale,
                (ry - self.origin_y) / self.scale)

    def _set_cursor(self, mode):
        name = CURSORS.get(mode, "crosshair")
        if name != self._cursor_name:
            self._cursor_name = name
            self.set_cursor(Gdk.Cursor.new_from_name(name, None))

    # ------------------------------------------------------------- events
    def _on_begin(self, gesture, x, y):
        self._sel.press(*self._to_root(x, y), hit=self._hit_test(x, y),
                        owner=self)

    def _on_update(self, gesture, dx, dy):
        ok, sx, sy = gesture.get_start_point()
        if ok:
            self._sel.motion(*self._to_root(sx + dx, sy + dy), owner=self)

    def _on_end(self, gesture, dx, dy):
        ok, sx, sy = gesture.get_start_point()
        if ok:
            self._sel.motion(*self._to_root(sx + dx, sy + dy), owner=self)
        self._sel.release(owner=self)

    def _on_motion(self, controller, x, y):
        if self._sel.dragging:
            self._set_cursor(self._sel.mode)
        else:
            self._set_cursor(self._hit_test(x, y) or "new")

    def _on_key(self, controller, keyval, keycode, state):
        if keyval == Gdk.KEY_Escape:
            self._sel.cancel()
            return True
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space):
            self._sel.finish()
            return True
        return False

    def _hit_test(self, x, y) -> str | None:
        """Which part of the selection is under this widget-local point."""
        rect = self._sel.rect_root()
        if not rect:
            return None
        rx, ry, rw, rh = rect
        lx, ly = self._to_local(rx, ry)
        lw, lh = rw / self.scale, rh / self.scale

        for hid, fx, fy in CORNERS:
            hx, hy = lx + lw * fx, ly + lh * fy
            if math.hypot(x - hx, y - hy) <= CORNER_HIT:
                return hid

        near_x = lx - EDGE_HIT <= x <= lx + lw + EDGE_HIT
        near_y = ly - EDGE_HIT <= y <= ly + lh + EDGE_HIT
        if near_x and abs(y - ly) <= EDGE_HIT:
            return "n"
        if near_x and abs(y - (ly + lh)) <= EDGE_HIT:
            return "s"
        if near_y and abs(x - lx) <= EDGE_HIT:
            return "w"
        if near_y and abs(x - (lx + lw)) <= EDGE_HIT:
            return "e"
        if lx <= x <= lx + lw and ly <= y <= ly + lh:
            return "inside"
        return None

    # -------------------------------------------------------------- paint
    def _draw(self, area, cr, width, height, *_args):
        rect = self._sel.rect_root()
        if not rect:
            cr.set_source_rgba(*DIM)
            cr.paint()
            return

        rx, ry, rw, rh = rect
        lx, ly = self._to_local(rx, ry)
        lw, lh = rw / self.scale, rh / self.scale

        # Dim everything except the selection, drawn as four bands. This avoids
        # OPERATOR_CLEAR, which needs a save/restore dance to stay predictable.
        cr.set_source_rgba(*DIM)
        for bx, by, bw, bh in ((0, 0, width, ly),
                               (0, ly + lh, width, height - ly - lh),
                               (0, ly, lx, lh),
                               (lx + lw, ly, width - lx - lw, lh)):
            if bw > 0 and bh > 0:
                cr.rectangle(bx, by, bw, bh)
        cr.fill()

        valid = self._sel.valid(rect)
        cr.set_source_rgba(*(LINE if valid else WARN))
        cr.set_line_width(1.0)
        cr.rectangle(lx + 0.5, ly + 0.5, max(lw - 1, 0), max(lh - 1, 0))
        cr.stroke()

        for _hid, fx, fy in CORNERS:
            hx, hy = lx + lw * fx, ly + lh * fy
            cr.set_source_rgba(*HANDLE_FILL)
            cr.arc(hx, hy, HANDLE_R, 0, 2 * math.pi)
            cr.fill_preserve()
            cr.set_source_rgba(*HANDLE_EDGE)
            cr.set_line_width(1.0)
            cr.stroke()

        text = f"{rw} × {rh}"
        if not valid:
            text += "  · " + _("min {w}×{h}").format(w=core.MIN_REGION_W, h=core.MIN_REGION_H)
        self._chip(cr, lx, ly, lw, text, warn=not valid)

    def _chip(self, cr, lx, ly, lw, text, warn=False):
        layout = self._area.create_pango_layout(text)
        desc = self._area.get_pango_context().get_font_description()
        desc = desc.copy() if desc else Pango.FontDescription.from_string("Sans")
        desc.set_absolute_size(15 * Pango.SCALE)
        desc.set_weight(Pango.Weight.SEMIBOLD)
        layout.set_font_description(desc)

        tw, th = layout.get_pixel_size()
        pad_x, pad_y = 11, 6
        w, h = tw + 2 * pad_x, th + 2 * pad_y
        x = lx + max(0, (lw - w) / 2)
        y = ly - h - 9 if ly > h + 12 else ly + 9

        cr.set_source_rgba(0, 0, 0, 0.8)
        _rounded_rect(cr, x, y, w, h, 9)
        cr.fill()
        cr.set_source_rgba(*(WARN if warn else (1, 1, 1, 0.97)))
        cr.move_to(x + pad_x, y + pad_y)
        PangoCairo.show_layout(cr, layout)


class RegionSelector:
    """Runs the overlays and reports one rectangle (or None) to `on_done`."""

    def __init__(self, on_done, display: str | None = None,
                 initial: dict | None = None):
        self._on_done = on_done
        self.display = display
        self._windows: list[_Overlay] = []
        self._closed = False

        self._rect: list[int] | None = None      # [x0, y0, x1, y1], normalized
        if initial:
            try:
                x, y = int(initial["x"]), int(initial["y"])
                self._rect = [x, y, x + int(initial["width"]),
                              y + int(initial["height"])]
            except (KeyError, TypeError, ValueError):
                self._rect = None

        self.mode: str | None = None
        self.dragging = False
        self._owner = None            # overlay that holds the pointer grab
        self._origin: tuple[int, int] | None = None
        self._before: list[int] | None = None
        self._fixed_x: int | None = None
        self._fixed_y: int | None = None
        self._pending_new = False     # a press that has not moved far enough yet

        # Read the screen size once; drawing must not spawn xrandr per frame.
        canvas = core.full_canvas(display)
        self._canvas = (canvas.width, canvas.height) if canvas else None

    def valid(self, rect) -> bool:
        return core.clamp_region(*rect, self._canvas) is not None

    # -------------------------------------------------------------- state
    def press(self, x, y, hit=None, owner=None):
        if self.dragging:          # another overlay already owns this drag
            return
        self.dragging = True
        self._owner = owner
        self.mode = hit or "new"
        self._origin = (x, y)
        self._before = list(self._rect) if self._rect else None

        if self.mode == "new":
            # Do not throw the current selection away yet: a plain click must
            # leave it alone. It is replaced only once the pointer really moves.
            self._pending_new = True
        elif self.mode != "inside" and self._rect:
            x0, y0, x1, y1 = self._rect
            self._fixed_x = x1 if self.mode in WEST else (
                x0 if self.mode in EAST else None)
            self._fixed_y = y1 if self.mode in NORTH else (
                y0 if self.mode in SOUTH else None)
        self._redraw()

    def motion(self, x, y, owner=None):
        if not self.dragging or (owner is not None and owner is not self._owner):
            return
        if self.mode == "new":
            ox, oy = self._origin
            if self._pending_new:
                if abs(x - ox) < DRAG_START and abs(y - oy) < DRAG_START:
                    return
                self._pending_new = False
            # Always span from the press point; _normalize() reorders corners,
            # so the previous rectangle is no reference once the drag crosses it.
            self._rect = [ox, oy, x, y]
        elif self.mode == "inside":
            self._move_to(x, y)
        else:
            self._resize_to(x, y)
        self._normalize()
        self._redraw()

    def release(self, owner=None):
        if owner is not None and owner is not self._owner:
            return
        self._end_drag()

    def abort(self):
        """The gesture was cancelled: put the rectangle back as it was,
        including no rectangle at all if there was none before."""
        if self.dragging:
            self._rect = list(self._before) if self._before else None
        self._end_drag()

    def _end_drag(self):
        self.dragging = False
        self.mode = None
        self._owner = None
        self._origin = None
        self._before = None
        self._fixed_x = self._fixed_y = None
        self._pending_new = False
        self._redraw()

    def _move_to(self, x, y):
        if not self._before:
            return
        ox, oy = self._origin
        gx0, gy0, gx1, gy1 = self._before
        w, h = gx1 - gx0, gy1 - gy0
        nx, ny = gx0 + (x - ox), gy0 + (y - oy)
        if self._canvas:
            cw, ch = self._canvas
            nx = max(0, min(nx, cw - w)) if w <= cw else 0
            ny = max(0, min(ny, ch - h)) if h <= ch else 0
        self._rect = [nx, ny, nx + w, ny + h]

    def _resize_to(self, x, y):
        if not self._before:
            return
        gx0, gy0, gx1, gy1 = self._before
        # Anchoring on the opposite edge lets the rectangle flip through zero
        # instead of collapsing when the pointer crosses it.
        if self._fixed_x is not None:
            x0, x1 = sorted((self._fixed_x, x))
        else:
            x0, x1 = gx0, gx1
        if self._fixed_y is not None:
            y0, y1 = sorted((self._fixed_y, y))
        else:
            y0, y1 = gy0, gy1
        self._rect = [x0, y0, x1, y1]

        horiz = "" if self._fixed_x is None else ("w" if x < self._fixed_x else "e")
        vert = "" if self._fixed_y is None else ("n" if y < self._fixed_y else "s")
        self.mode = (vert + horiz) or self.mode

    def _normalize(self):
        x0, y0, x1, y1 = self._rect
        self._rect = [min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)]

    def rect_root(self) -> tuple[int, int, int, int] | None:
        if not self._rect:
            return None
        x0, y0, x1, y1 = self._rect
        return (min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))

    def _redraw(self):
        for w in self._windows:
            w._area.queue_draw()
            w.sync_bar()

    # ------------------------------------------------------------ outcome
    def finish(self):
        rect = self.rect_root()
        region = core.clamp_region(*rect, self._canvas) if rect else None
        if region is None:
            # Invalid or empty: stay open so None keeps meaning "cancelled".
            self._redraw()
            return
        self._close(region)

    def cancel(self):
        self._close(None)

    def _close(self, region):
        if self._closed:
            return
        self._closed = True
        for w in self._windows:
            w.destroy()
        self._windows.clear()
        # Let the overlays actually leave the screen before the caller reacts.
        GLib.idle_add(self._on_done, region)

    # ---------------------------------------------------------------- run
    def run(self) -> bool:
        display = Gdk.Display.get_default()
        if display is None:
            self._on_done(None)
            return False

        provider = Gtk.CssProvider()
        provider.load_from_data(OVERLAY_CSS)
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        monitors = display.get_monitors()
        n = monitors.get_n_items()
        if not n:
            self._on_done(None)
            return False

        # Root-space origins from xrandr, matched by connector name.
        xr = {m.connector: (m.x, m.y) for m in core.list_monitors(self.display)}
        bar_idx = self._preferred_monitor(monitors, n, xr)
        for i in range(n):
            mon = monitors.get_item(i)
            geo = mon.get_geometry()
            scale = mon.get_scale_factor() or 1
            origin = xr.get(mon.get_connector(), (geo.x * scale, geo.y * scale))
            self._windows.append(_Overlay(self, mon, origin, with_bar=(i == bar_idx)))
        for w in self._windows:
            w.present()
        self._windows[bar_idx].grab_focus()
        self._redraw()
        return True

    def _preferred_monitor(self, monitors, n, xr) -> int:
        """Put the control bar on the monitor holding the current selection."""
        rect = self.rect_root()
        if rect:
            cx, cy = rect[0] + rect[2] // 2, rect[1] + rect[3] // 2
            for i in range(n):
                mon = monitors.get_item(i)
                geo = mon.get_geometry()
                scale = mon.get_scale_factor() or 1
                ox, oy = xr.get(mon.get_connector(), (geo.x * scale, geo.y * scale))
                if (ox <= cx < ox + geo.width * scale and
                        oy <= cy < oy + geo.height * scale):
                    return i
        return 0


def select_region(on_done, display: str | None = None,
                  initial: dict | None = None) -> bool:
    """Show the overlays. `on_done(region_dict | None)` fires exactly once."""
    return RegionSelector(on_done, display, initial).run()
