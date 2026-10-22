"""GTK4/libadwaita front-end for meetrec.

One window, one big button. Everything slow runs on a worker thread; the UI
only polls while a recording is actually running, so idle cost is nil.
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

from . import core, region  # noqa: E402

IDLE, STARTING, RECORDING, STOPPING = "idle", "starting", "recording", "stopping"

FOLLOW_MIC = "시스템 기본"
FOLLOW_SINK = "기본 출력"


METER_CSS = b"""
levelbar.meter > trough { min-height: 9px; min-width: 96px; border-radius: 5px; }
levelbar.meter > trough > block.filled { background-color: #33d17a; border-radius: 5px; }
levelbar.meter.hot > trough > block.filled { background-color: #e01b24; }
levelbar.meter.idle > trough > block.filled { background-color: #5e5c64; }
label.meter-db { font-family: monospace; font-size: 0.85em; }
"""


class Meter(Gtk.Box):
    """Live level readout for one audio source: bar + dB + verdict.

    Values arrive at 10 Hz from the recording ffmpeg's astats tap. The bar
    falls back gradually so a speaking voice reads as a steady level instead
    of a flicker.
    """

    HOT_DB = -3.0        # close to clipping
    SIGNAL_DB = -50.0    # above this we call it real signal

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
        """db None = not measuring; core.METER_FLOOR_DB = silence."""
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

        silent = db <= core.METER_FLOOR_DB + 0.01
        self.bar.remove_css_class("idle" if not silent else "hot")
        if silent:
            self.bar.add_css_class("idle")
            self.lbl.set_text("무음")
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
        self.set_title("회의 녹음기")
        self.set_default_size(500, -1)
        self.set_resizable(False)

        self.cfg = core.load_config()
        self.state = IDLE
        self.plan: core.RecordingPlan | None = None
        self.tick_id = 0
        self.tick_count = 0
        self.silent_since = None
        self.silence_warned = False
        self._detail_base = ""
        self._loading = True

        self._build_ui()
        self._reload_devices()
        self._loading = False
        self._reattach()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()

        menu = Gio.Menu()
        menu.append("저장 폴더 열기", "win.open-folder")
        menu.append("장치 새로고침", "win.refresh")
        btn_menu = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        header.pack_end(btn_menu)
        toolbar.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                      margin_top=6, margin_bottom=18,
                      margin_start=18, margin_end=18)

        # status ---------------------------------------------------------
        self.lbl_status = Gtk.Label(label="준비됨")
        self.lbl_status.add_css_class("title-2")
        self.lbl_detail = Gtk.Label(label="")
        self.lbl_detail.add_css_class("dim-label")
        self.lbl_detail.set_wrap(True)
        self.lbl_detail.set_justify(Gtk.Justification.CENTER)
        box.append(self.lbl_status)
        box.append(self.lbl_detail)

        # audio ----------------------------------------------------------
        g_audio = Adw.PreferencesGroup(title="소리")

        # Meters live in their own row: as ComboRow suffixes they squeezed the
        # selected-device text into an ellipsis.
        # use_subtitle puts the selected device on its own full-width line;
        # in the value column long device names were cut off.
        # Each device row carries its own meter, so "is this one actually
        # picking anything up?" is answered next to the device name.
        self.row_mic = Adw.ComboRow(title="마이크", use_subtitle=True)
        self.row_mic.set_subtitle_lines(1)
        self.row_mic.set_list_factory(plain_list_factory())
        self.row_mic.connect("notify::selected", self._on_mic_changed)
        g_audio.add(self.row_mic)

        self.row_sys = Adw.ComboRow(title="시스템 소리", use_subtitle=True)
        self.row_sys.set_subtitle_lines(1)
        self.row_sys.set_list_factory(plain_list_factory())
        self.row_sys.connect("notify::selected", self._on_sink_changed)
        g_audio.add(self.row_sys)

        # The meters get their own row on purpose: the device rows are
        # desensitized while recording, which would grey out the very readout
        # you want to watch.
        self.row_level = Adw.ActionRow(title="입력 레벨",
                                       subtitle="녹음 중 실시간 표시")
        self.row_level.set_subtitle_lines(1)
        grid = Gtk.Grid(row_spacing=6, column_spacing=10, valign=Gtk.Align.CENTER,
                        margin_end=6)
        self.meter_mic = Meter()
        self.meter_sys = Meter()
        for r, (name, meter) in enumerate((("마이크", self.meter_mic),
                                           ("시스템", self.meter_sys))):
            lbl = Gtk.Label(label=name, xalign=1.0)
            lbl.add_css_class("dim-label")
            lbl.add_css_class("caption")
            grid.attach(lbl, 0, r, 1, 1)
            grid.attach(meter, 1, r, 1, 1)
        self.row_level.add_suffix(grid)
        self.btn_check = Gtk.Button(label="검사", valign=Gtk.Align.CENTER,
                                    tooltip_text="3초 동안 실제 신호가 들어오는지 검사")
        self.row_level.add_suffix(self.btn_check)
        self.btn_check.connect("clicked", self._on_check)
        g_audio.add(self.row_level)
        box.append(g_audio)

        # screen ---------------------------------------------------------
        g_screen = Adw.PreferencesGroup(title="화면")
        self.row_screen = Adw.SwitchRow(title="화면도 함께 녹화")
        self.row_screen.set_active(bool(self.cfg.get("record_screen")))
        self.row_screen.connect("notify::active", self._on_screen_toggled)
        g_screen.add(self.row_screen)

        self.row_monitor = Adw.ComboRow(title="녹화 대상", use_subtitle=True)
        self.row_monitor.set_subtitle_lines(1)
        self.row_monitor.set_list_factory(plain_list_factory())
        self.row_monitor.set_sensitive(self.row_screen.get_active())
        self.row_monitor.connect("notify::selected", self._on_monitor_changed)
        self.btn_region = Gtk.Button(label="영역 지정", valign=Gtk.Align.CENTER,
                                     tooltip_text="화면에서 녹화할 사각형을 드래그로 지정")
        self.btn_region.connect("clicked", lambda *_: self._pick_region())
        self.row_monitor.add_suffix(self.btn_region)
        g_screen.add(self.row_monitor)
        box.append(g_screen)

        # output ---------------------------------------------------------
        g_out = Adw.PreferencesGroup(title="저장")
        self.row_out = Adw.ActionRow(title="저장 위치")
        btn_folder = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER)
        btn_folder.add_css_class("flat")
        btn_folder.connect("clicked", lambda *_: self._open_folder())
        self.row_out.add_suffix(btn_folder)
        g_out.add(self.row_out)
        box.append(g_out)

        # main button ----------------------------------------------------
        self.btn_main = Gtk.Button(label="녹음 시작")
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
        act.connect("activate", lambda *_: self._open_folder())
        self.add_action(act)
        act = Gio.SimpleAction.new("refresh", None)
        act.connect("activate", lambda *_: self._reload_devices())
        self.add_action(act)

        self.connect("close-request", self._on_close)

    # -------------------------------------------------------- device lists
    def _reload_devices(self):
        prev = self._loading
        self._loading = True
        try:
            self.mics = core.list_microphones()
            self.sinks = core.list_sinks()
            self.monitors = core.capture_targets()

            resolved = core.default_source_name()
            dev = next((d for d in self.mics if d.name == resolved), None)
            model = Gtk.StringList()
            model.append(f"{FOLLOW_MIC} → " +
                         (dev.description if dev else "⚠ 마이크가 아님 — 직접 선택 필요"))
            for d in self.mics:
                model.append(d.description)
            self.row_mic.set_model(model)
            sel = 0
            if self.cfg.get("mic"):
                for i, d in enumerate(self.mics):
                    if d.fingerprint == self.cfg["mic"]:
                        sel = i + 1
                        break
                else:
                    self._toast("저장된 마이크를 못 찾았어 — 다시 선택해줘")
                    self.cfg["mic"] = None
                    core.save_config(self.cfg)
            self.row_mic.set_selected(sel)

            resolved = core.default_sink_name()
            snk = next((s for s in self.sinks if s.name == resolved), None)
            model = Gtk.StringList()
            model.append(f"{FOLLOW_SINK} → " +
                         (snk.description if snk else "⚠ 확인 불가"))
            for d in self.sinks:
                model.append(d.description)
            self.row_sys.set_model(model)
            sel = 0
            if self.cfg.get("sink"):
                for i, d in enumerate(self.sinks):
                    if d.fingerprint == self.cfg["sink"]:
                        sel = i + 1
                        break
                else:
                    self.cfg["sink"] = None
                    core.save_config(self.cfg)
            self.row_sys.set_selected(sel)

            self._refresh_monitor_model()
        finally:
            self._loading = prev
        self._refresh_subtitles()

    def _refresh_monitor_model(self):
        """Rebuild the capture-target list: real monitors + the free region."""
        prev = self._loading
        self._loading = True
        try:
            usable = list(self.monitors)
            # The region entry is always last, whether or not one is stored yet.
            usable.append(core.region_monitor(self.cfg.get("region")) or
                          core.Monitor(connector=core.REGION, width=0, height=0,
                                       x=0, y=0))
            self.usable_monitors = usable

            model = Gtk.StringList()
            for m in usable:
                if m.connector == core.REGION and not m.width:
                    model.append("선택 영역 · 아직 지정 안 됨")
                else:
                    model.append(m.label)
            self.row_monitor.set_model(model)

            sel = 0
            for i, m in enumerate(usable):
                if m.connector == self.cfg.get("monitor"):
                    sel = i
                    break
            else:
                for i, m in enumerate(usable):
                    if m.primary:
                        sel = i
                        break
            self.row_monitor.set_selected(sel)
            self.cfg["monitor"] = usable[sel].connector
            self._sync_region_button()
        finally:
            self._loading = prev

    def _sync_region_button(self):
        on_region = self.cfg.get("monitor") == core.REGION
        self.btn_region.set_label("영역 다시 지정" if self.cfg.get("region")
                                  else "영역 지정")
        self.btn_region.set_sensitive(
            on_region and self.row_monitor.get_sensitive() and self.state == IDLE)

    def _refresh_subtitles(self):
        outdir = Path(self.cfg.get("outdir") or core.DEFAULT_OUTDIR).expanduser()
        try:
            free = shutil.disk_usage(outdir if outdir.exists() else outdir.parent).free
            self.row_out.set_subtitle(f"{outdir}  ·  여유 {core.human_bytes(free)}")
        except OSError:
            self.row_out.set_subtitle(str(outdir))

    # ----------------------------------------------------------- settings
    def _on_mic_changed(self, *_):
        if self._loading:
            return
        idx = self.row_mic.get_selected()
        self.cfg["mic"] = None if idx == 0 else self.mics[idx - 1].fingerprint
        core.save_config(self.cfg)

    def _on_sink_changed(self, *_):
        if self._loading:
            return
        idx = self.row_sys.get_selected()
        self.cfg["sink"] = None if idx == 0 else self.sinks[idx - 1].fingerprint
        core.save_config(self.cfg)

    def _on_screen_toggled(self, *_):
        if self._loading:
            return
        on = self.row_screen.get_active()
        self.row_monitor.set_sensitive(on)
        self.cfg["record_screen"] = on
        core.save_config(self.cfg)
        self._sync_region_button()

    def _on_monitor_changed(self, *_):
        if self._loading or not getattr(self, "usable_monitors", None):
            return
        idx = self.row_monitor.get_selected()
        if 0 <= idx < len(self.usable_monitors):
            self.cfg["monitor"] = self.usable_monitors[idx].connector
            core.save_config(self.cfg)
            self._sync_region_button()
            # Picking "선택 영역" with nothing stored goes straight to the overlay.
            if self.cfg["monitor"] == core.REGION and not self.cfg.get("region"):
                GLib.idle_add(self._pick_region)

    def _pick_region(self):
        """Dim the desktop and let the user drag out a capture rectangle."""
        if self.state != IDLE:
            return False
        if os.environ.get("XDG_SESSION_TYPE") == "wayland":
            self._toast("Wayland 세션에선 영역 지정을 쓸 수 없어 (X11 전용)")
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
                self._toast(f"영역 {reg['width']}×{reg['height']} "
                            f"@ ({reg['x']},{reg['y']}) 지정 완료")
            else:
                if not self.cfg.get("region"):
                    # Nothing usable stored: drop back to a real monitor.
                    self.cfg["monitor"] = None
                    core.save_config(self.cfg)
                    self._refresh_monitor_model()
                self._toast("영역 지정 취소 — 최소 "
                            f"{core.MIN_REGION_W}×{core.MIN_REGION_H}px 필요")
            return False

        # Hand the stored rectangle back so it reappears ready to be nudged.
        if not region.select_region(done, initial=self.cfg.get("region")):
            self.set_visible(True)
            self._toast("영역 선택 창을 띄우지 못했어")
        return False

    def _open_folder(self):
        outdir = Path(self.cfg.get("outdir") or core.DEFAULT_OUTDIR).expanduser()
        outdir.mkdir(parents=True, exist_ok=True)
        Gio.AppInfo.launch_default_for_uri(f"file://{outdir}", None)

    def _toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4))

    # -------------------------------------------------------- input check
    def _on_check(self, *_):
        if self.state != IDLE:
            return
        self.btn_check.set_sensitive(False)
        self.btn_check.set_label("검사 중…")

        def work():
            try:
                res = core.check_inputs(self.cfg)
            except core.PlanError as e:
                res = {"ok": False, "error": str(e)}
            except Exception as e:  # defensive: never lose the button
                res = {"ok": False, "error": repr(e)}
            GLib.idle_add(done, res)

        def done(res):
            self.btn_check.set_sensitive(True)
            self.btn_check.set_label("검사")
            if not res.get("ok"):
                self._set_detail(f"입력 확인 실패 — {res.get('error', '')}")
                return
            def fmt(peak, silent_hint):
                if peak is None:
                    return f"신호 없음 ({silent_hint})"
                return f"{peak:.0f} dB"
            mic = fmt(res["mic_peak"], "마이크 확인 필요")
            sysn = fmt(res["sys_peak"], "재생 중인 소리 없음")
            self.meter_mic.set_db(res["mic_peak"] if res["mic_peak"] is not None
                                  else core.METER_FLOOR_DB)
            self.meter_sys.set_db(res["sys_peak"] if res["sys_peak"] is not None
                                  else core.METER_FLOOR_DB)
            self._set_detail(f"마이크 {mic}   ·   시스템 소리 {sysn}")
            return False

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ record
    def _on_main(self, *_):
        if self.state == IDLE:
            self._start()
        elif self.state == RECORDING:
            self._stop()

    def _set_state(self, state):
        self.state = state
        busy = state in (STARTING, STOPPING)
        self.btn_main.set_sensitive(not busy)
        for w in (self.row_mic, self.row_sys, self.row_screen, self.row_monitor,
                  self.btn_check):
            w.set_sensitive(state == IDLE)
        if state == IDLE:
            self.row_monitor.set_sensitive(self.row_screen.get_active())
        self._sync_region_button()

        if state == IDLE:
            self.lbl_status.set_text("준비됨")
            self.btn_main.set_label("녹음 시작")
            self.btn_main.remove_css_class("destructive-action")
            self.btn_main.add_css_class("suggested-action")
        elif state == STARTING:
            self.lbl_status.set_text("시작하는 중…")
        elif state == RECORDING:
            self.btn_main.set_label("녹음 중지")
            self.btn_main.remove_css_class("suggested-action")
            self.btn_main.add_css_class("destructive-action")
        elif state == STOPPING:
            self.lbl_status.set_text("저장 마무리 중…")
            self.btn_main.set_label("저장 중…")

    def _set_detail(self, text):
        self.lbl_detail.set_text(text or "")

    def _set_detail_base(self, text):
        self._detail_base = text or ""
        self._set_detail(self._detail_base)

    def _start(self):
        self._set_state(STARTING)
        self._set_detail("")

        def work():
            try:
                plan = core.build_plan(self.cfg)
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
            what = "화면 + 소리" if plan.with_video else "소리만"
            self._set_detail_base(f"{what}  ·  {Path(plan.final).name}")
            self._start_ticking()
            return False

        def fail(msg):
            self._set_state(IDLE)
            self._set_detail(msg)
            self._toast("녹음을 시작하지 못했어")
            return False

        threading.Thread(target=work, daemon=True).start()

    def _stop(self):
        self._stop_ticking()
        self._set_state(STOPPING)
        plan = self.plan

        def work():
            core.stop_recording()
            result = core.finalize(plan) if plan else {"ok": False, "message": "세션 정보 없음"}
            core.clear_state()
            GLib.idle_add(done, result)

        def done(result):
            self.plan = None
            self._set_state(IDLE)
            self.meter_mic.reset()
            self.meter_sys.reset()
            if result.get("ok"):
                self._set_detail(
                    f"{Path(result['path']).name}  ·  "
                    f"{core.human_duration(result['duration'])}  ·  "
                    f"{core.human_bytes(result['size'])}"
                )
                self._toast("저장 완료")
            else:
                self._set_detail(result.get("message", "저장 실패"))
                self._toast("저장에 문제가 있었어")
            self._reload_devices()
            self.present()
            return False

        threading.Thread(target=work, daemon=True).start()

    def _reattach(self):
        """Pick a recording back up after a GUI crash or restart."""
        if not core.unit_active():
            core.clear_state()
            return
        plan = core.load_state()
        if plan is None:
            self._set_detail("진행 중인 녹음이 있는데 세션 정보를 못 읽었어.")
            return
        self.plan = plan
        self._set_state(RECORDING)
        self._set_detail_base(f"진행 중인 녹음에 다시 연결됨  ·  {Path(plan.final).name}")
        self._start_ticking()

    # -------------------------------------------------------------- ticks
    def _start_ticking(self):
        self._stop_ticking()
        self.tick_count = 0
        self.silent_since = None
        self.silence_warned = False
        self.tick_id = GLib.timeout_add(100, self._tick)

    def _stop_ticking(self):
        if self.tick_id:
            GLib.source_remove(self.tick_id)
            self.tick_id = 0

    def _tick(self):
        if self.state != RECORDING or not self.plan:
            self.tick_id = 0
            return False
        plan = self.plan
        self.tick_count += 1

        if self.tick_count % 4 == 0:
            secs = core.read_progress_seconds(plan.progress)
            if secs is None:
                secs = max(0.0, time.time() - plan.started_at)
            self.lbl_status.set_text(f"녹음 중  {core.human_duration(secs)}")

        mic_db = core.read_meter_db(plan.meter_mic)
        sys_db = core.read_meter_db(plan.meter_sys)
        self.meter_mic.set_db(mic_db)
        self.meter_sys.set_db(sys_db)
        self._track_silence(mic_db)

        # every 6 s: did the capture die, and is the disk still fine?
        if self.tick_count % 60 == 0:
            if not core.unit_active():
                self._stop_ticking()
                self._set_state(STOPPING)
                threading.Thread(target=self._recover_after_crash, daemon=True).start()
                return False
            try:
                free = shutil.disk_usage(Path(plan.partial).parent).free
                need = max(2 * 1024**3, Path(plan.partial).stat().st_size + 1024**3)
                if free < need:
                    self._set_detail("디스크 공간이 부족해서 자동으로 중지할게.")
                    self._stop()
                    return False
            except OSError:
                pass
        return True

    def _track_silence(self, mic_db):
        """Warn if the mic stays flat while recording - a muted headset looks
        exactly like a working one otherwise."""
        now = time.time()
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
            self._set_detail("⚠ 마이크에 소리가 안 들어오고 있어 (음소거 확인)")

    def _recover_after_crash(self):
        plan = self.plan
        result = core.finalize(plan) if plan else {"ok": False, "message": "세션 정보 없음"}
        log = core.recent_log(6)
        core.clear_state()

        def done():
            self.plan = None
            self._set_state(IDLE)
            self.meter_mic.reset()
            self.meter_sys.reset()
            head = "녹음이 예기치 않게 끝났어. "
            if result.get("ok"):
                self._set_detail(head + f"여기까지는 저장했어: {Path(result['path']).name}")
            else:
                self._set_detail(head + result.get("message", "") + (f"\n{log}" if log else ""))
            self.present()
            return False

        GLib.idle_add(done)

    # -------------------------------------------------------------- close
    def _on_close(self, *_):
        if self.state == IDLE:
            return False
        dlg = Adw.MessageDialog(
            transient_for=self, heading="녹음이 진행 중이야",
            body="지금 창을 닫으면 어떻게 할까?")
        dlg.add_response("cancel", "취소")
        dlg.add_response("keep", "녹음 계속하고 창만 닫기")
        dlg.add_response("stop", "중지하고 저장")
        dlg.set_response_appearance("stop", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")

        def on_resp(_d, resp):
            if resp == "stop":
                self._stop()
            elif resp == "keep":
                self.set_visible(False)
        dlg.connect("response", on_resp)
        dlg.present()
        return True


class MeetRecApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=core.APP_ID,
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)

    def do_startup(self):
        Adw.Application.do_startup(self)
        provider = Gtk.CssProvider()
        provider.load_from_data(METER_CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def do_activate(self):
        win = self.props.active_window
        if win is None:
            win = Window(application=self)
        win.set_visible(True)
        win.present()


def main():
    return MeetRecApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
