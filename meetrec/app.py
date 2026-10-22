"""GTK4/libadwaita front-end for meetrec.

One window, one big button. Device discovery, systemd calls and file work run
on worker threads; the UI only polls while a recording is running.
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from . import __version__, core, region  # noqa: E402
from .i18n import _  # noqa: E402

IDLE, STARTING, RECORDING, STOPPING = "idle", "starting", "recording", "stopping"

METER_CSS = """
levelbar.meter > trough { min-height: 9px; min-width: 96px; border-radius: 5px; }
levelbar.meter > trough > block.filled { background-color: #33d17a; border-radius: 5px; }
levelbar.meter.hot > trough > block.filled { background-color: #e01b24; }
levelbar.meter.idle > trough > block.filled { background-color: #77767b; }
label.meter-db { font-family: monospace; font-size: 0.85em; }
"""


def display_path(path: Path) -> str:
    """~/Videos/Meetings rather than /home/name/Videos/Meetings."""
    try:
        return "~/" + str(Path(path).relative_to(Path.home()))
    except ValueError:
        return str(path)


def monitor_label(m: core.Monitor) -> str:
    if m.connector == core.REGION:
        if not m.width:
            return _("Selected area · not picked yet")
        return _("Selected area · {w}×{h} at ({x},{y})").format(w=m.width, h=m.height, x=m.x, y=m.y)
    if m.full:
        return _("Whole desktop (all monitors) · {w}×{h}").format(w=m.width, h=m.height)
    if not m.width:
        return _("{name} (not connected)").format(name=m.connector)
    star = "★ " if m.primary else ""
    return f"{star}{m.connector} · {m.width}×{m.height}"


class Meter(Gtk.Box):
    """Live level readout for one audio source: bar + dB.

    Values arrive at 10 Hz from the recording ffmpeg's astats tap. The bar
    falls back gradually so a speaking voice reads as a steady level instead
    of a flicker.
    """

    HOT_DB = -3.0        # close to clipping

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                         valign=Gtk.Align.CENTER)
        self.bar = Gtk.LevelBar(min_value=0, max_value=1, valign=Gtk.Align.CENTER)
        self.bar.add_css_class("meter")
        self.bar.add_css_class("idle")
        self.lbl = Gtk.Label(label="—", xalign=1.0, width_chars=7)
        self.lbl.add_css_class("meter-db")
        self.lbl.add_css_class("dim-label")
        self.append(self.bar)
        self.append(self.lbl)
        self._shown = 0.0

    def set_db(self, db: float | None):
        """db None = no data; core.METER_FLOOR_DB = silence."""
        if db is None:
            self._shown = 0.0
            self.bar.set_value(0.0)
            self.bar.add_css_class("idle")
            self.bar.remove_css_class("hot")
            self.lbl.set_text("—")
            return

        target = core.db_to_fraction(db)
        self._shown = target if target >= self._shown else max(target, self._shown - 0.05)
        self.bar.set_value(self._shown)

        if db <= core.METER_FLOOR_DB + 0.01:
            self.bar.add_css_class("idle")
            self.bar.remove_css_class("hot")
            self.lbl.set_text(_("silent"))
            return
        self.bar.remove_css_class("idle")
        if db >= self.HOT_DB:
            self.bar.add_css_class("hot")
        else:
            self.bar.remove_css_class("hot")
        self.lbl.set_text(f"{db:.0f} dB")

    def reset(self):
        self.set_db(None)


def plain_list_factory() -> Gtk.SignalListItemFactory:
    """Popup rows that size to their text.

    The default ComboRow popup inherits the row's narrow value column and
    ellipsizes device names into uselessness.
    """
    factory = Gtk.SignalListItemFactory()

    def setup(_factory, item):
        label = Gtk.Label(xalign=0)
        label.set_ellipsize(Pango.EllipsizeMode.NONE)
        item.set_child(label)

    def bind(_factory, item):
        item.get_child().set_text(item.get_item().get_string())

    factory.connect("setup", setup)
    factory.connect("bind", bind)
    return factory


class Window(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_title(_("Meeting Recorder"))
        self.set_default_size(520, -1)
        self.set_resizable(False)

        self.cfg = core.load_config()
        self.state = IDLE
        self.plan: core.RecordingPlan | None = None
        self.tick_id = 0
        self.tick_count = 0
        self.silent_since = None
        self.silence_warned = False
        self.stale_since = None
        self.stale_warned = False
        self._checking = False
        self._detail_base = ""
        self._loading = True
        self.mics: list[core.AudioDevice] = []
        self.sinks: list[core.AudioDevice] = []
        self._mic_ids: list = []
        self._sink_ids: list = []

        self._build_ui()
        self._reload_devices()
        self._loading = False
        self._reconcile()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()

        menu = Gio.Menu()
        menu.append(_("Open recordings folder"), "win.open-folder")
        menu.append(_("Refresh devices"), "win.refresh")
        menu.append(_("About Meeting Recorder"), "app.about")
        btn_menu = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        header.pack_end(btn_menu)
        toolbar.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                      margin_top=6, margin_bottom=18,
                      margin_start=18, margin_end=18)

        # status ---------------------------------------------------------
        self.lbl_status = Gtk.Label(label=_("Ready"))
        self.lbl_status.add_css_class("title-2")
        self.lbl_detail = Gtk.Label(label="")
        self.lbl_detail.add_css_class("dim-label")
        self.lbl_detail.set_wrap(True)
        self.lbl_detail.set_justify(Gtk.Justification.CENTER)
        box.append(self.lbl_status)
        box.append(self.lbl_detail)

        # audio ----------------------------------------------------------
        g_audio = Adw.PreferencesGroup(title=_("Sound"))

        # use_subtitle puts the selected device on its own full-width line;
        # in the value column long device names were cut off.
        self.row_mic = Adw.ComboRow(title=_("Microphone"), use_subtitle=True)
        self.row_mic.set_subtitle_lines(1)
        self.row_mic.set_list_factory(plain_list_factory())
        self.row_mic.connect("notify::selected", self._on_mic_changed)
        g_audio.add(self.row_mic)

        self.row_sys = Adw.ComboRow(title=_("System sound"), use_subtitle=True)
        self.row_sys.set_subtitle_lines(1)
        self.row_sys.set_list_factory(plain_list_factory())
        self.row_sys.connect("notify::selected", self._on_sink_changed)
        g_audio.add(self.row_sys)

        # The meters get their own row on purpose: the device rows are
        # desensitized while recording, which would grey out the very readout
        # you want to watch.
        self.row_level = Adw.ActionRow(title=_("Input level"),
                                       subtitle=_("Live while recording"))
        self.row_level.set_subtitle_lines(1)
        grid = Gtk.Grid(row_spacing=6, column_spacing=10, valign=Gtk.Align.CENTER,
                        margin_end=6)
        self.meter_mic = Meter()
        self.meter_sys = Meter()
        for r, (name, meter) in enumerate(((_("Mic"), self.meter_mic),
                                           (_("System"), self.meter_sys))):
            lbl = Gtk.Label(label=name, xalign=1.0)
            lbl.add_css_class("dim-label")
            lbl.add_css_class("caption")
            grid.attach(lbl, 0, r, 1, 1)
            grid.attach(meter, 1, r, 1, 1)
        self.row_level.add_suffix(grid)
        self.btn_check = Gtk.Button(label=_("Test"), valign=Gtk.Align.CENTER,
                                    tooltip_text=_("Listen for 3 seconds and check that "
                                                   "both inputs get real signal"))
        self.row_level.add_suffix(self.btn_check)
        self.btn_check.connect("clicked", self._on_check)
        g_audio.add(self.row_level)
        box.append(g_audio)

        # screen ---------------------------------------------------------
        g_screen = Adw.PreferencesGroup(title=_("Screen"))
        self.row_screen = Adw.SwitchRow(title=_("Also record the screen"))
        self.row_screen.set_active(bool(self.cfg.get("record_screen")))
        self.row_screen.connect("notify::active", self._on_screen_toggled)
        g_screen.add(self.row_screen)

        self.row_monitor = Adw.ComboRow(title=_("Capture"), use_subtitle=True)
        self.row_monitor.set_subtitle_lines(1)
        self.row_monitor.set_list_factory(plain_list_factory())
        self.row_monitor.set_sensitive(self.row_screen.get_active())
        self.row_monitor.connect("notify::selected", self._on_monitor_changed)
        self.btn_region = Gtk.Button(label=_("Pick area"), valign=Gtk.Align.CENTER,
                                     tooltip_text=_("Drag out the rectangle to record"))
        self.btn_region.connect("clicked", lambda *a: self._pick_region())
        self.row_monitor.add_suffix(self.btn_region)
        g_screen.add(self.row_monitor)
        box.append(g_screen)

        # output ---------------------------------------------------------
        g_out = Adw.PreferencesGroup(title=_("Saving"))
        self.row_out = Adw.ActionRow(title=_("Save to"))
        self.row_out.set_subtitle_lines(2)
        self.btn_choose = Gtk.Button(label=_("Change…"), valign=Gtk.Align.CENTER)
        self.btn_choose.connect("clicked", lambda *a: self._choose_folder())
        self.row_out.add_suffix(self.btn_choose)
        btn_folder = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER,
                                tooltip_text=_("Open recordings folder"))
        btn_folder.add_css_class("flat")
        btn_folder.connect("clicked", lambda *a: self._open_folder())
        self.row_out.add_suffix(btn_folder)
        g_out.add(self.row_out)
        box.append(g_out)

        # main button ----------------------------------------------------
        self.btn_main = Gtk.Button(label=_("Start recording"))
        self.btn_main.add_css_class("pill")
        self.btn_main.add_css_class("suggested-action")
        self.btn_main.set_size_request(-1, 46)
        self.btn_main.connect("clicked", self._on_main)
        box.append(self.btn_main)

        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(box)
        toolbar.set_content(self.toasts)
        self.set_content(toolbar)

        act = Gio.SimpleAction.new("open-folder", None)
        act.connect("activate", lambda *a: self._open_folder())
        self.add_action(act)
        self.act_refresh = Gio.SimpleAction.new("refresh", None)
        self.act_refresh.connect("activate", lambda *a: self._reload_devices())
        self.add_action(self.act_refresh)

        self.connect("close-request", self._on_close)

    # -------------------------------------------------------- device lists
    def _device_model(self, devices, fp, follow_label, resolved_desc):
        """Labels + ids for a device row. A saved device that is missing stays
        selected as "(not connected)" instead of quietly becoming the default."""
        labels = [f"{follow_label} → {resolved_desc}"]
        ids: list = [None]
        current = core.match_fingerprint(devices, fp) if fp else None
        if fp and current is None:
            labels.append(_("Saved device (not connected)"))
            ids.append("missing")
        for d in devices:
            labels.append(d.description)
            ids.append(d)
        if not fp:
            sel = 0
        elif current is None:
            sel = 1
        else:
            sel = ids.index(current)
        return labels, ids, sel

    def _reload_devices(self):
        """Ask pactl/xrandr on a worker thread, then rebuild the rows."""
        if getattr(self, "_reloading", False):
            return
        self._reloading = True

        def work():
            try:
                found = (core.list_microphones(), core.list_sinks(), core.capture_targets(),
                         core.default_source_name(), core.default_sink_name())
            except Exception:  # noqa: BLE001 - keep the old lists
                found = None
            GLib.idle_add(self._apply_devices, found)

        threading.Thread(target=work, daemon=True).start()

    def _apply_devices(self, found):
        self._reloading = False
        if found is None:
            return False
        mics, sinks, monitors, default_src, default_sink = found
        prev = self._loading
        self._loading = True
        try:
            self.mics, self.sinks, self.monitors = mics, sinks, monitors

            dev = next((d for d in self.mics if d.name == default_src), None)
            labels, self._mic_ids, sel = self._device_model(
                self.mics, self.cfg.get("mic"), _("System default"),
                dev.description if dev else _("⚠ not a microphone, pick one"))
            self.row_mic.set_model(Gtk.StringList.new(labels))
            self.row_mic.set_selected(sel)

            snk = next((s for s in self.sinks if s.name == default_sink), None)
            labels, self._sink_ids, sel = self._device_model(
                self.sinks, self.cfg.get("sink"), _("Default output"),
                snk.description if snk else _("⚠ unknown"))
            self.row_sys.set_model(Gtk.StringList.new(labels))
            self.row_sys.set_selected(sel)

            self._refresh_monitor_model()
        finally:
            self._loading = prev
        self._refresh_subtitles()
        return False

    def _refresh_monitor_model(self):
        """Rebuild the capture-target list: real monitors + the free region."""
        prev = self._loading
        self._loading = True
        try:
            usable = list(self.monitors)
            want = self.cfg.get("monitor")
            if want and want != core.REGION and not any(m.connector == want for m in usable):
                # Saved screen is unplugged: show it, don't switch to another one.
                usable.insert(0, core.Monitor(connector=want, width=0, height=0, x=0, y=0))
            # The region entry is always last, whether or not one is stored yet.
            usable.append(core.region_monitor(self.cfg.get("region")) or
                          core.Monitor(connector=core.REGION, width=0, height=0,
                                       x=0, y=0))
            self.usable_monitors = usable
            self.row_monitor.set_model(Gtk.StringList.new([monitor_label(m) for m in usable]))

            sel = next((i for i, m in enumerate(usable) if m.connector == want), None)
            if sel is None:
                sel = next((i for i, m in enumerate(usable) if m.primary), 0)
                self.cfg["monitor"] = usable[sel].connector
            self.row_monitor.set_selected(sel)
            self._sync_region_button()
        finally:
            self._loading = prev

    def _sync_region_button(self):
        on_region = self.cfg.get("monitor") == core.REGION
        self.btn_region.set_label(_("Pick again") if self.cfg.get("region") else _("Pick area"))
        self.btn_region.set_sensitive(
            on_region and self.row_monitor.get_sensitive() and self.state == IDLE)

    def _refresh_subtitles(self):
        outdir = core.outdir_of(self.cfg)
        try:
            probe = outdir if outdir.exists() else outdir.parent
            free = shutil.disk_usage(probe).free
            self.row_out.set_subtitle(_("{path}  ·  {free} free").format(
                path=display_path(outdir), free=core.human_bytes(free)))
        except OSError:
            self.row_out.set_subtitle(display_path(outdir))

    # ----------------------------------------------------------- settings
    def _on_mic_changed(self, *_a):
        if self._loading:
            return
        i = self.row_mic.get_selected()
        if 0 <= i < len(self._mic_ids) and self._mic_ids[i] != "missing":
            d = self._mic_ids[i]
            self.cfg["mic"] = core.identity(d) if d else None
            core.save_config(self.cfg)

    def _on_sink_changed(self, *_a):
        if self._loading:
            return
        i = self.row_sys.get_selected()
        if 0 <= i < len(self._sink_ids) and self._sink_ids[i] != "missing":
            d = self._sink_ids[i]
            self.cfg["sink"] = core.identity(d) if d else None
            core.save_config(self.cfg)

    def _on_screen_toggled(self, *_a):
        if self._loading:
            return
        on = self.row_screen.get_active()
        self.row_monitor.set_sensitive(on)
        self.cfg["record_screen"] = on
        core.save_config(self.cfg)
        self._sync_region_button()

    def _on_monitor_changed(self, *_a):
        if self._loading or not getattr(self, "usable_monitors", None):
            return
        idx = self.row_monitor.get_selected()
        if 0 <= idx < len(self.usable_monitors):
            self.cfg["monitor"] = self.usable_monitors[idx].connector
            core.save_config(self.cfg)
            self._sync_region_button()
            # Picking "Selected area" with nothing stored goes straight to the overlay.
            if self.cfg["monitor"] == core.REGION and not self.cfg.get("region"):
                GLib.idle_add(self._pick_region)

    def _pick_region(self):
        """Dim the desktop and let the user drag out a capture rectangle."""
        if self.state != IDLE:
            return False
        if os.environ.get("XDG_SESSION_TYPE") == "wayland":
            self._toast(_("Picking an area needs an X11 session"))
            return False

        # Keep meetrec's own window out of the shot while picking.
        self.set_visible(False)

        def done(reg):
            self.set_visible(True)
            self.present()
            if reg:
                self.cfg["monitor"] = core.REGION
                self.cfg["region"] = reg
                core.save_config(self.cfg)
                self._refresh_monitor_model()
                self._toast(_("Area set: {w}×{h} at ({x},{y})").format(
                    w=reg["width"], h=reg["height"], x=reg["x"], y=reg["y"]))
            else:
                if not self.cfg.get("region"):
                    # Nothing usable stored: drop back to a real monitor.
                    self.cfg["monitor"] = None
                    core.save_config(self.cfg)
                    self._refresh_monitor_model()
                self._toast(_("Area not changed"))
            return False

        # Hand the stored rectangle back so it reappears ready to be nudged.
        if not region.select_region(done, initial=self.cfg.get("region")):
            self.set_visible(True)
            self._toast(_("Couldn't open the area picker"))
        return False

    def _choose_folder(self):
        dialog = Gtk.FileDialog(title=_("Save recordings to"), modal=True)
        current = core.outdir_of(self.cfg)
        if current.exists():
            dialog.set_initial_folder(Gio.File.new_for_path(str(current)))

        def done(dlg, result):
            try:
                folder = dlg.select_folder_finish(result)
            except GLib.Error:
                return              # cancelled
            path = folder.get_path() if folder else None
            if path:
                self.cfg["outdir"] = path
                core.save_config(self.cfg)
                self._refresh_subtitles()

        dialog.select_folder(self, None, done)

    def _open_folder(self):
        outdir = core.outdir_of(self.cfg)
        try:
            outdir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self._toast(str(e))
            return
        Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(str(outdir)).get_uri(), None)

    def _toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4))

    # -------------------------------------------------------- input check
    def _on_check(self, *_a):
        if self.state != IDLE or self._checking:
            return
        self._checking = True
        self.btn_check.set_label(_("Testing…"))
        self._set_state(IDLE)
        cfg = dict(self.cfg)

        def work():
            try:
                res = core.check_inputs(cfg)
            except core.PlanError as e:
                res = {"ok": False, "error": str(e)}
            except Exception as e:  # defensive: never lose the button
                res = {"ok": False, "error": repr(e)}
            GLib.idle_add(done, res)

        def done(res):
            self._checking = False
            self.btn_check.set_label(_("Test"))
            self._set_state(IDLE)
            if not res.get("ok"):
                self._set_detail(_("Input test failed: {error}").format(error=res.get("error", "")))
                return False

            def fmt(peak, hint):
                return _("no signal ({hint})").format(hint=hint) if peak is None else f"{peak:.0f} dB"
            mic = fmt(res["mic_peak"], _("check the microphone"))
            sysn = fmt(res["sys_peak"], _("nothing is playing"))
            self.meter_mic.set_db(res["mic_peak"] if res["mic_peak"] is not None
                                  else core.METER_FLOOR_DB)
            self.meter_sys.set_db(res["sys_peak"] if res["sys_peak"] is not None
                                  else core.METER_FLOOR_DB)
            self._set_detail(_("Mic {mic}   ·   System sound {sys}").format(mic=mic, sys=sysn))
            return False

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ record
    def _on_main(self, *_a):
        if self.state == IDLE:
            self._start()
        elif self.state == RECORDING:
            self._stop()

    def _set_state(self, state):
        self.state = state
        busy = state in (STARTING, STOPPING) or self._checking
        self.btn_main.set_sensitive(not busy)
        for w in (self.row_mic, self.row_sys, self.row_screen, self.row_monitor,
                  self.btn_check, self.btn_choose):
            w.set_sensitive(state == IDLE and not self._checking)
        if state == IDLE and not self._checking:
            self.row_monitor.set_sensitive(self.row_screen.get_active())
        self.act_refresh.set_enabled(state == IDLE)
        self._sync_region_button()

        if state == IDLE:
            self.lbl_status.set_text(_("Ready"))
            self.btn_main.set_label(_("Start recording"))
            self.btn_main.remove_css_class("destructive-action")
            self.btn_main.add_css_class("suggested-action")
        elif state == STARTING:
            self.lbl_status.set_text(_("Starting…"))
        elif state == RECORDING:
            self.btn_main.set_label(_("Stop recording"))
            self.btn_main.remove_css_class("suggested-action")
            self.btn_main.add_css_class("destructive-action")
        elif state == STOPPING:
            self.lbl_status.set_text(_("Finishing the file…"))
            self.btn_main.set_label(_("Saving…"))

    def _set_detail(self, text):
        self.lbl_detail.set_text(text or "")

    def _set_detail_base(self, text):
        self._detail_base = text or ""
        self._set_detail(self._detail_base)

    def _describe(self, plan: core.RecordingPlan) -> str:
        if plan.with_video:
            enc = (plan.encoder or {}).get("codec", "")
            label = core.VideoEncoder(enc, 0, ()).label if enc else ""
            what = _("Screen + sound · {codec}").format(codec=label)
        else:
            what = _("Sound only")
        return f"{what}  ·  {Path(plan.final).name}"

    def _start(self):
        self._set_state(STARTING)
        self._set_detail("")
        cfg = dict(self.cfg)

        def work():
            try:
                plan = core.build_plan(cfg)
                core.start_recording(plan)
            except core.PlanError as e:
                GLib.idle_add(fail, str(e))
                return
            except Exception as e:
                GLib.idle_add(fail, repr(e))
                return
            GLib.idle_add(ok, plan)

        def ok(plan):
            self.plan = plan
            self._set_state(RECORDING)
            self._set_detail_base(self._describe(plan))
            self._start_ticking()
            return False

        def fail(msg):
            self._set_state(IDLE)
            self._set_detail(msg)
            self._toast(_("Couldn't start recording"))
            return False

        threading.Thread(target=work, daemon=True).start()

    def _stop(self):
        self._stop_ticking()
        self._set_state(STOPPING)
        plan = self.plan

        def work():
            if not core.stop_recording():
                result = {"ok": False, "stopped": False, "message": _(
                    "The recorder did not stop. Check: systemctl --user status {unit}")
                    .format(unit=core.UNIT_SERVICE)}
            else:
                target = plan
                if target is None:
                    saved = core.load_state()
                    target = saved[0] if saved else None
                if target is None:
                    result = {"ok": False, "message": _("Stopped, but there was no session "
                                                        "information to finish the file.")}
                else:
                    result = self._finish_session(target, "finalizing")
            GLib.idle_add(done, result)

        def done(result):
            if result.get("stopped") is False:
                # Still running: stay in RECORDING so Stop can be pressed again.
                self._set_state(RECORDING)
                self._set_detail(result["message"])
                self._start_ticking()
                return False
            self._finish(result)
            return False

        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _finish_session(plan, phase):
        """Worker thread: finalize a stopped session; the session file goes either way.

        On failure the raw .partial.mkv stays in the save folder and the
        message names it; scratch files are removed.
        """
        try:
            core.save_state(plan, phase)
        except OSError:
            pass
        result = core.safe_finalize(plan)
        core.clear_state()
        if not result.get("ok"):
            core.discard(plan)
        return result

    def _finish(self, result, recovered=False):
        self.plan = None
        self._set_state(IDLE)
        self.meter_mic.reset()
        self.meter_sys.reset()
        if result.get("ok"):
            text = (f"{Path(result['path']).name}  ·  "
                    f"{core.human_duration(result['duration'])}  ·  "
                    f"{core.human_bytes(result['size'])}")
            if recovered:
                text = _("Recovered an unfinished recording: {file}").format(file=text)
            self._set_detail(text)
            self._toast(_("Saved"))
        else:
            self._set_detail(result.get("message", _("Saving failed")))
            self._toast(_("There was a problem saving"))
        self._reload_devices()
        self.present()

    def _reconcile(self):
        """After a GUI crash or restart: re-attach to a running recording, or
        finish one that ended while no window was watching."""
        def work():
            saved = core.load_state()
            running = core.unit_running()
            invocation = core.unit_invocation() if running else ""
            GLib.idle_add(self._apply_reconcile, saved, running, invocation)

        threading.Thread(target=work, daemon=True).start()

    def _apply_reconcile(self, saved, running, invocation):
        if self.state != IDLE:
            return False
        if running is None:
            self._set_detail(_("Couldn't check whether a recording is running "
                               "(systemd didn't answer). Nothing was changed."))
            return False
        if saved is None:
            if running:
                self.plan = None
                self._set_state(RECORDING)
                self._set_detail_base(_("A recording is running, but its session file is "
                                        "missing. Stop it here; the raw file stays in the "
                                        "save folder."))
            return False
        plan, phase, saved_inv = saved
        if running:
            if saved_inv and saved_inv != invocation:
                # Not the recording we know about. Offer Stop; the saved
                # session is handled after that.
                self.plan = None
                self._set_state(RECORDING)
                self._set_detail_base(_("A recording this window didn't start is running. "
                                        "Stop it here if you want."))
                return False
            self.plan = plan
            self._set_state(RECORDING)
            self._set_detail_base(_("Re-attached to the running recording  ·  {file}")
                                  .format(file=Path(plan.final).name))
            self._start_ticking()
            return False
        if not Path(plan.partial).exists():
            core.clear_state()          # it never got as far as writing a file
            core.discard(plan)
            return False
        # The recording ended without us. Finish what is on disk.
        self._set_state(STOPPING)

        def work():
            result = self._finish_session(plan, "finalizing")
            GLib.idle_add(lambda: self._finish(result, recovered=True) or False)

        threading.Thread(target=work, daemon=True).start()
        return False

    # -------------------------------------------------------------- ticks
    def _start_ticking(self):
        self._stop_ticking()
        self.tick_count = 0
        self.silent_since = self.stale_since = None
        self.silence_warned = self.stale_warned = False
        self.tick_id = GLib.timeout_add(100, self._tick)

    def _stop_ticking(self):
        if self.tick_id:
            GLib.source_remove(self.tick_id)
            self.tick_id = 0

    def _tick(self):
        if self.state != RECORDING:
            self.tick_id = 0
            return False
        plan = self.plan
        self.tick_count += 1
        if plan is None:
            return True

        if self.tick_count % 4 == 0:
            secs = core.read_progress_seconds(plan.progress)
            if secs is None:
                secs = max(0.0, time.time() - plan.started_at)
            self.lbl_status.set_text(_("Recording  {time}").format(time=core.human_duration(secs)))

        mic_db = core.read_meter_db(plan.meter_mic)
        sys_db = core.read_meter_db(plan.meter_sys)
        self.meter_mic.set_db(mic_db)
        self.meter_sys.set_db(sys_db)
        self._track_signal(mic_db, sys_db)

        # every 6 s: did the capture die, and is the disk still fine?
        if self.tick_count % 60 == 0:
            threading.Thread(target=self._health_check, args=(plan,), daemon=True).start()
        return True

    def _health_check(self, plan):
        """Worker thread: systemctl and disk checks off the main loop."""
        alive = core.unit_running()
        low = False
        try:
            free = shutil.disk_usage(Path(plan.partial).parent).free
            need = max(2 * 1024**3, Path(plan.partial).stat().st_size + 1024**3)
            low = free < need
        except OSError:
            pass
        GLib.idle_add(self._health_result, plan, alive, low)

    def _health_result(self, plan, alive, low):
        if self.state != RECORDING or self.plan is not plan:
            return False
        if alive is False:
            self._stop_ticking()
            self._set_state(STOPPING)
            threading.Thread(target=self._recover_after_crash, daemon=True).start()
        elif low:
            self._set_detail(_("Running out of disk space, stopping the recording."))
            self._stop()
        return False

    def _track_signal(self, mic_db, sys_db):
        """Warn when the mic stays flat (a muted headset looks exactly like a
        working one) or when no levels arrive at all."""
        now = time.time()
        if mic_db is None and sys_db is None:
            if self.stale_since is None:
                self.stale_since = now
            elif not self.stale_warned and now - self.stale_since > 5:
                self.stale_warned = True
                self._set_detail(_("⚠ No audio is reaching the recorder"))
            return
        if self.stale_warned:
            self._set_detail(self._detail_base)
        self.stale_since, self.stale_warned = None, False

        if mic_db is None or mic_db > core.METER_FLOOR_DB + 0.01:
            if self.silence_warned:
                self._set_detail(self._detail_base)
            self.silent_since = None
            self.silence_warned = False
            return
        if self.silent_since is None:
            self.silent_since = now
        elif not self.silence_warned and now - self.silent_since > 8:
            self.silence_warned = True
            self._set_detail(_("⚠ The microphone is silent (is it muted?)"))

    def _recover_after_crash(self):
        plan = self.plan
        result = (self._finish_session(plan, "finalizing") if plan
                  else {"ok": False, "message": ""})
        log = core.recent_log(6)

        def done():
            head = _("The recording ended unexpectedly.") + " "
            if result.get("ok"):
                self._finish(result)
                self._set_detail(head + _("Saved what was recorded: {file}")
                                 .format(file=Path(result["path"]).name))
            else:
                result["message"] = head + result.get("message", "") + (f"\n{log}" if log else "")
                self._finish(result)
            return False

        GLib.idle_add(done)

    # -------------------------------------------------------------- close
    def _on_close(self, *_a):
        if self.state == IDLE:
            return False
        if self.state != RECORDING:
            self._toast(_("Wait until the file is finished"))
            return True
        heading = _("A recording is running")
        body = _("What should happen when this window closes?")
        if hasattr(Adw, "AlertDialog"):                 # libadwaita 1.5+
            dlg = Adw.AlertDialog(heading=heading, body=body)
        else:
            dlg = Adw.MessageDialog(transient_for=self, heading=heading, body=body)
        dlg.add_response("cancel", _("Cancel"))
        dlg.add_response("keep", _("Keep recording, hide the window"))
        dlg.add_response("stop", _("Stop and save"))
        dlg.set_response_appearance("stop", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")

        def on_resp(_d, resp):
            if self.state != RECORDING:
                return
            if resp == "stop":
                self._stop()
            elif resp == "keep":
                self.set_visible(False)
        dlg.connect("response", on_resp)
        if isinstance(dlg, Adw.Dialog if hasattr(Adw, "Dialog") else ()):
            dlg.present(self)
        else:
            dlg.present()
        return True


class MeetRecApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=core.APP_ID,
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_startup(self):
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        if hasattr(provider, "load_from_string"):      # GTK 4.12+
            provider.load_from_string(METER_CSS)
        else:
            provider.load_from_data(METER_CSS.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", self._on_about)
        self.add_action(about)

    def do_activate(self):
        win = self.props.active_window
        if win is None:
            win = Window(application=self)
        win.set_visible(True)
        win.present()

    def _on_about(self, *_a):
        kw = dict(application_name=_("Meeting Recorder"), application_icon=core.APP_ID,
                  version=__version__, developer_name="Jay Cho",
                  license_type=Gtk.License.MIT_X11,
                  website="https://github.com/Jayyo/meetrec",
                  comments=_("Record microphone, system sound and optionally the "
                             "screen into one file."))
        if hasattr(Adw, "AboutDialog"):
            Adw.AboutDialog(**kw).present(self.props.active_window)
        else:
            Adw.AboutWindow(transient_for=self.props.active_window, **kw).present()


def main():
    return MeetRecApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
