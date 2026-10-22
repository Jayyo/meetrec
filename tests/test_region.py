"""Rectangle handling in the area picker (no windows are opened)."""
import pytest

gi = pytest.importorskip("gi")
try:
    gi.require_version("Gtk", "4.0")
    from meetrec import region
except (ValueError, ImportError):
    pytest.skip("needs GTK 4", allow_module_level=True)


def selector(monkeypatch, initial=None):
    monkeypatch.setattr(region.core, "full_canvas",
                        lambda display=None: region.core.Monitor("__ALL__", 1920, 1080, 0, 0, full=True))
    return region.RegionSelector(lambda r: None, initial=initial)


def test_dragging_up_and_left_keeps_the_press_point(monkeypatch):
    s = selector(monkeypatch)
    s.press(500, 500)
    s.motion(400, 400)
    s.motion(300, 300)
    s.release()
    assert s.rect_root() == (300, 300, 200, 200)


def test_plain_click_keeps_the_selection(monkeypatch):
    s = selector(monkeypatch, initial={"x": 10, "y": 10, "width": 200, "height": 100})
    s.press(800, 800)
    s.release()
    assert s.rect_root() == (10, 10, 200, 100)


def test_resize_flips_through_the_opposite_edge(monkeypatch):
    s = selector(monkeypatch, initial={"x": 100, "y": 100, "width": 200, "height": 200})
    s.press(100, 100, hit="nw")
    s.motion(400, 350)
    s.release()
    assert s.rect_root() == (300, 300, 100, 50)


def test_move_stays_on_screen(monkeypatch):
    s = selector(monkeypatch, initial={"x": 100, "y": 100, "width": 200, "height": 200})
    s.press(150, 150, hit="inside")
    s.motion(5000, 5000)
    s.release()
    assert s.rect_root() == (1720, 880, 200, 200)


def test_validity_uses_the_cached_canvas(monkeypatch):
    s = selector(monkeypatch)
    calls = []
    monkeypatch.setattr(region.core, "full_canvas", lambda display=None: calls.append(1))
    assert s.valid((0, 0, 300, 200)) and not s.valid((0, 0, 10, 10))
    assert calls == []


def test_cancelled_first_drag_leaves_nothing(monkeypatch):
    s = selector(monkeypatch)
    s.press(100, 100)
    s.motion(400, 300)
    s.abort()
    assert s.rect_root() is None


def test_cancelled_resize_restores_the_old_rectangle(monkeypatch):
    s = selector(monkeypatch, initial={"x": 100, "y": 100, "width": 200, "height": 200})
    s.press(300, 300, hit="se")
    s.motion(900, 900)
    s.abort()
    assert s.rect_root() == (100, 100, 200, 200)
