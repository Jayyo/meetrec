"""Pure logic: devices, monitors, regions, encoders, command lines, state, meters."""
import json
import os
import time

import pytest

from meetrec import core

PIPEWIRE_SOURCES = [
    {"name": "alsa_output.pci-0000_0a_00.4.analog-stereo.monitor", "description": "Monitor of Speakers",
     "properties": {"media.class": "Audio/Sink", "device.class": "monitor"}},
    {"name": "alsa_input.usb-Example_Headset-00.mono-fallback", "description": "Headset Mono",
     "properties": {"media.class": "Audio/Source", "device.serial": "Example_Headset",
                    "device.bus_path": "pci-0000:05:00.3-usb-0:1:1.0"}},
    {"name": "noise_suppressed", "description": "Noise-suppressed mic",
     "properties": {"media.class": "Audio/Source/Virtual"}},
]
PULSEAUDIO_SOURCES = [
    {"name": "alsa_output.pci.analog-stereo.monitor", "description": "Monitor of Built-in",
     "monitor_of_sink": "alsa_output.pci.analog-stereo", "properties": {"device.class": "monitor"}},
    {"name": "alsa_input.pci.analog-stereo", "description": "Built-in Microphone",
     "monitor_of_sink": "n/a", "properties": {"device.bus_path": "pci-0000:00:1f.3"}},
]


def test_sources_skip_monitors_on_pipewire_and_pulseaudio():
    assert [d.name for d in core.parse_sources(PIPEWIRE_SOURCES)] == [
        "alsa_input.usb-Example_Headset-00.mono-fallback", "noise_suppressed"]
    assert [d.name for d in core.parse_sources(PULSEAUDIO_SOURCES)] == ["alsa_input.pci.analog-stereo"]


def test_fingerprints_prefer_serial_then_bus_path():
    devs = core.parse_sources(PIPEWIRE_SOURCES)
    assert devs[0].fingerprint == {"kind": "serial", "value": "Example_Headset"}
    assert devs[1].fingerprint is None
    assert core.device_fingerprint({"device.bus_path": "x"}) == {"kind": "bus", "value": "x"}
    assert core.match_fingerprint(devs, {"kind": "serial", "value": "Example_Headset"}) is devs[0]
    assert core.match_fingerprint(devs, None) is None


XRANDR_MONITORS = """Monitors: 2
 0: +*DP-2 2560/597x1440/336+1366+0  DP-2
 1: +HDMI-0 1365/509x767/286+0+673  HDMI-0
"""
XRANDR_CURRENT = "Screen 0: minimum 8 x 8, current 3926 x 1440, maximum 32767 x 32767\n"


def test_monitor_parsing_rounds_to_even_sizes():
    mons = core.parse_monitors(XRANDR_MONITORS)
    assert [(m.connector, m.width, m.height, m.x, m.y, m.primary) for m in mons] == [
        ("DP-2", 2560, 1440, 1366, 0, True), ("HDMI-0", 1364, 766, 0, 673, False)]
    canvas = core.parse_canvas(XRANDR_CURRENT)
    assert (canvas.width, canvas.height, canvas.full) == (3926, 1440, True)
    assert core.parse_canvas("garbage") is None


@pytest.mark.parametrize("rect,canvas,expected", [
    ((10, 20, 301, 201), (1920, 1080), {"x": 10, "y": 20, "width": 300, "height": 200}),
    ((1800, 1000, 400, 400), (1920, 1080), {"x": 1800, "y": 1000, "width": 120, "height": 80}),
    ((-50, -50, 300, 300), (1920, 1080), {"x": 0, "y": 0, "width": 300, "height": 300}),
    ((0, 0, 63, 400), (1920, 1080), None),
    ((0, 0, 0, 0), None, None),
    ((5, 5, 100, 100), None, {"x": 5, "y": 5, "width": 100, "height": 100}),
])
def test_clamp_region(rect, canvas, expected):
    assert core.clamp_region(*rect, canvas) == expected


def test_encoder_choice():
    def probe(ok):
        return lambda codec, w, h: codec in ok
    assert core.pick_encoder(1920, 1080, probe({"h264_nvenc", "hevc_nvenc"})).codec == "h264_nvenc"
    # tiny area NVENC refuses: x264, not HEVC
    assert core.pick_encoder(144, 300, probe({"hevc_nvenc"})).codec == "libx264"
    wide = core.pick_encoder(5526, 2520, probe({"h264_nvenc", "hevc_nvenc"}))
    assert wide.codec == "hevc_nvenc"
    sw = core.pick_encoder(5526, 2520, probe(set()))
    assert (sw.codec, sw.fps) == ("libx264", 15)
    assert core.pick_encoder(1920, 1080, probe(set())).fps == 30


def make_plan(tmp_path, **kw):
    base = dict(session="abcd1234", mic_name="mic", mic_label="Mic", sys_name="spk.monitor",
                sys_label="Speakers", mic_gain=1.0, partial=str(tmp_path / "m.partial.mkv"),
                final=str(tmp_path / "m.m4a"), with_video=False, monitor=None, encoder=None,
                display=":0", scratch=str(tmp_path / "scratch"))
    base.update(kw)
    return core.RecordingPlan(**base)


def test_audio_only_command(tmp_path):
    argv = core.ffmpeg_argv(make_plan(tmp_path, mic_gain=1.6))
    assert "x11grab" not in argv
    fc = argv[argv.index("-filter_complex") + 1]
    assert "volume=1.6" in fc and "alimiter=limit=0.95:level=0" in fc
    assert argv[argv.index("-cluster_time_limit") + 1] == "1000"
    assert argv[argv.index("-flush_packets") + 1] == "1"
    assert argv[-1].endswith(".partial.mkv")


def test_video_command_uses_the_planned_encoder(tmp_path):
    enc = core.software_encoder(5526, 2520)
    plan = make_plan(tmp_path, with_video=True, final=str(tmp_path / "m.mp4"),
                     monitor={"connector": "__ALL__", "width": 5526, "height": 2520, "x": 0, "y": 0},
                     encoder={"codec": enc.codec, "fps": enc.fps, "args": list(enc.args)})
    argv = core.ffmpeg_argv(plan)
    assert argv[argv.index("-framerate") + 1] == "15"
    assert argv[argv.index("-c:v") + 1] == "libx264"
    assert "nvenc" not in " ".join(argv)
    # raw 5526x2520 frames are ~56 MB each, so only a few may queue up
    assert int(argv[argv.index("-thread_queue_size") + 1]) == 4


def test_unit_command(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSE_SERVER", "unix:/run/test/pulse")
    plan = make_plan(tmp_path)
    argv = core.unit_argv(plan, systemd_version=255)
    assert "--collect" in argv and "--property=UMask=0077" in argv
    assert "--property=KillSignal=SIGINT" in argv
    assert "--expand-environment=no" in argv
    assert "--setenv=PULSE_SERVER=unix:/run/test/pulse" in argv
    assert argv[argv.index("--") + 1] == "ffmpeg"
    assert "--expand-environment=no" not in core.unit_argv(plan, systemd_version=249)


def test_state_round_trip(tmp_path):
    plan = make_plan(tmp_path)
    core.save_state(plan, "recording", "inv-1")
    loaded, phase, inv = core.load_state()
    assert (loaded, phase, inv) == (plan, "recording", "inv-1")
    core.state_path().write_text(json.dumps({"version": 99}))
    assert core.load_state() is None
    core.clear_state()
    assert core.load_state() is None


def test_meter_reading_and_staleness(tmp_path):
    tap = tmp_path / "mic.meter"
    assert core.read_meter_db(str(tap)) is None
    tap.write_text("frame:1\nlavfi.astats.Overall.Peak_level=-12.5\n"
                   "frame:2\nlavfi.astats.Overall.Peak_level=-inf\n")
    assert core.read_meter_db(str(tap)) == core.METER_FLOOR_DB
    with tap.open("a") as f:
        f.write("frame:3\nlavfi.astats.Overall.Peak_level=-9.0\n")
    assert core.read_meter_db(str(tap)) == -9.0
    old = time.time() - 10
    os.utime(tap, (old, old))
    assert core.read_meter_db(str(tap)) is None      # capture stopped updating


def test_progress_reading(tmp_path):
    p = tmp_path / "progress"
    p.write_text("out_time_us=1500000\nprogress=continue\nout_time_us=2500000\n")
    assert core.read_progress_seconds(str(p)) == 2.5
    assert core.read_progress_seconds(str(tmp_path / "none")) is None


@pytest.mark.parametrize("raw", [None, [], {"mic": "x", "mic_gain": True},
                                 {"mic_gain": 100}, {"region": {"x": "a"}}, {"outdir": ""}])
def test_bad_config_falls_back(raw):
    assert core.validate_config(raw) == core.DEFAULT_CONFIG


def test_config_round_trip():
    cfg = dict(core.DEFAULT_CONFIG, mic={"kind": "serial", "value": "s"}, mic_gain=1.6,
               record_screen=True, monitor="__REGION__",
               region={"x": 1, "y": 2, "width": 300, "height": 200}, outdir="/tmp/x")
    core.save_config(cfg)
    assert core.load_config() == cfg


def test_identity_falls_back_to_the_node_name():
    devs = core.parse_sources(PIPEWIRE_SOURCES)
    virt = devs[1]
    ident = core.identity(virt)
    assert ident == {"kind": "name", "value": "noise_suppressed", "name": "noise_suppressed"}
    assert core.match_fingerprint(devs, ident) is virt


def test_identity_tells_apart_two_inputs_of_one_card():
    data = [{"name": f"alsa_input.usb-Card-00.{p}", "description": p,
             "properties": {"media.class": "Audio/Source", "device.serial": "Card"}}
            for p in ("analog-stereo", "iec958-stereo")]
    devs = core.parse_sources(data)
    assert core.match_fingerprint(devs, core.identity(devs[1])) is devs[1]
    # renamed after a profile change: same card, best remaining guess
    assert core.match_fingerprint(devs[:1], core.identity(devs[1])) is devs[0]


def fake_screen(monkeypatch):
    mons = core.parse_monitors(XRANDR_MONITORS)
    monkeypatch.setattr(core, "capture_targets", lambda display=None: mons)
    monkeypatch.setattr(core, "full_canvas", lambda display=None: core.parse_canvas(XRANDR_CURRENT))
    monkeypatch.delenv("XDG_SESSION_TYPE", raising=False)


def test_missing_saved_monitor_is_an_error(monkeypatch):
    fake_screen(monkeypatch)
    with pytest.raises(core.PlanError):
        core.resolve_screen({"monitor": "HDMI-1"}, ":0")
    assert core.resolve_screen({"monitor": "HDMI-0"}, ":0").connector == "HDMI-0"
    assert core.resolve_screen({"monitor": None}, ":0").connector == "DP-2"   # primary


def test_saved_area_is_not_cut_down_quietly(monkeypatch):
    fake_screen(monkeypatch)
    ok = {"monitor": core.REGION, "region": {"x": 100, "y": 100, "width": 400, "height": 300}}
    assert core.resolve_screen(ok, ":0").width == 400
    off = {"monitor": core.REGION, "region": {"x": 3800, "y": 100, "width": 400, "height": 300}}
    with pytest.raises(core.PlanError):
        core.resolve_screen(off, ":0")


def test_unknown_systemd_state_is_not_stopped(monkeypatch):
    monkeypatch.setattr(core, "_run", lambda argv, timeout=15, umask=-1: (1, "", "bus error"))
    assert core.unit_state() == "unknown"
    assert core.unit_running() is None
    with pytest.raises(core.PlanError):
        core.start_recording(make_plan_obj())
    assert core.stop_recording(timeout=0.5) is False


def make_plan_obj():
    return core.RecordingPlan(session="s", mic_name="m", mic_label="M", sys_name="s",
                              sys_label="S", mic_gain=1.0, partial="/nonexistent/p.mkv",
                              final="/nonexistent/p.m4a", with_video=False, monitor=None,
                              encoder=None, display=":0", scratch="/nonexistent/s")


def test_state_file_is_per_unit(monkeypatch):
    assert core.state_path().name == core.UNIT + ".json"
