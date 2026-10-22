"""Core logic for meetrec.

Device discovery, recording plans, the transient recording service, session
state and finalization. No GUI dependency, so all of it can be driven from a
shell:

    python3 -c "from meetrec import core; print(core.list_microphones())"
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

APP_ID = "io.github.jayyo.MeetRec"
# Overridable so a test run never touches a real recording.
UNIT = os.environ.get("MEETREC_UNIT", "meetrec-recording")
UNIT_SERVICE = f"{UNIT}.service"

HOME = Path.home()
STATE_VERSION = 1

MIN_FREE_START = 5 * 1024**3       # refuse to start below this
METER_FLOOR_DB = -60.0
METER_STALE_S = 2.0                # meter file not updated for this long = no data

# Smallest rectangle the region picker accepts. Encoders have their own
# limits on top (h264_nvenc refused 144x300 and 200x48 on the machine this was
# written on); the encoder probe catches those and falls back to x264.
MIN_REGION_W = 64
MIN_REGION_H = 48

# Raw frames queued between x11grab and the encoder. Each is width*height*4
# bytes, so this is kept small; a sustained backlog can't be fixed by queueing.
VIDEO_QUEUE_BYTES = 256 * 1024**2

DEFAULT_CONFIG = {
    "mic": None,            # fingerprint dict, or None = follow system default
    "sink": None,           # fingerprint dict, or None = follow system default
    "mic_gain": 1.0,
    "record_screen": False,
    "monitor": None,        # xrandr connector name, ALL_SCREENS, or REGION
    "region": None,         # {"x","y","width","height"} when monitor == REGION
    "outdir": None,         # None = ~/Videos/Meetings (or the XDG videos dir)
}

# Handed to the recording unit so ffmpeg talks to the same sound server and
# X display as the app.
PASSTHROUGH_ENV = ("PULSE_SERVER", "PULSE_RUNTIME_PATH", "PULSE_COOKIE",
                   "PULSE_CLIENTCONFIG", "PIPEWIRE_RUNTIME_DIR", "PIPEWIRE_REMOTE",
                   "XAUTHORITY")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _run(argv, timeout=15, umask=-1):
    """Run a command, return (rc, stdout, stderr). Never raises on failure."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           umask=umask)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{argv[0]}: timeout"


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}TB"


def human_duration(sec: float) -> str:
    sec = int(max(0, sec))
    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def _xdg(var: str, fallback: Path) -> Path:
    v = os.environ.get(var)
    return Path(v) if v and os.path.isabs(v) else fallback


def config_path() -> Path:
    return _xdg("XDG_CONFIG_HOME", HOME / ".config") / "meetrec" / "config.json"


def state_path() -> Path:
    # One file per unit name, so a run with MEETREC_UNIT set never touches
    # the session file of a real recording.
    return _xdg("XDG_STATE_HOME", HOME / ".local" / "state") / "meetrec" / f"{UNIT}.json"


@functools.lru_cache(maxsize=1)
def default_outdir() -> Path:
    rc, out, _ = _run(["xdg-user-dir", "VIDEOS"], timeout=5)
    base = Path(out.strip()) if rc == 0 and out.strip() and out.strip() != str(HOME) \
        else HOME / "Videos"
    return base / "Meetings"


def runtime_dir() -> Path:
    """Private scratch space for meter taps and progress files (tmpfs if possible)."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base and os.path.isdir(base):
        d = Path(base) / "meetrec"
        d.mkdir(mode=0o700, exist_ok=True)
        return d
    d = _xdg("XDG_CACHE_HOME", HOME / ".cache") / "meetrec" / "run"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def _atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------
# audio devices
# --------------------------------------------------------------------------

def device_fingerprint(props: dict) -> dict | None:
    """Stable identity for a sound card, immune to PulseAudio name churn.

    PipeWire renames nodes when a headset is re-plugged or switches profile
    (mono-fallback -> mono-fallback.2 -> .3, analog-stereo -> iec958-stereo),
    so the node name is never stored. Card serial / bus path usually survive
    a re-plug into the same port.
    """
    if not props:
        return None
    if props.get("device.serial"):
        return {"kind": "serial", "value": props["device.serial"]}
    if props.get("device.bus_path"):
        return {"kind": "bus", "value": props["device.bus_path"]}
    vid = props.get("device.vendor.id")
    pid = props.get("device.product.id")
    desc = props.get("device.description")
    if vid or pid or desc:
        return {"kind": "vpd", "value": f"{vid}|{pid}|{desc}"}
    return None


@dataclass
class AudioDevice:
    name: str                 # pulse name (volatile, resolved fresh each run)
    description: str
    fingerprint: dict | None
    monitor_source: str | None = None   # sinks only
    state: str = ""


def _pactl_json(what: str) -> list:
    rc, out, _ = _run(["pactl", "--format=json", "list", what])
    if rc != 0 or not out.strip():
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def is_monitor_source(src: dict) -> bool:
    """True for a sink's monitor, on PipeWire (pipewire-pulse) and PulseAudio."""
    props = src.get("properties") or {}
    mon = src.get("monitor_of_sink")
    return (props.get("device.class") == "monitor"
            or props.get("media.class") == "Audio/Sink"
            or (isinstance(mon, str) and mon not in ("", "n/a")))


def parse_sources(data: list) -> list[AudioDevice]:
    devs = []
    for s in data:
        if not isinstance(s, dict) or is_monitor_source(s):
            continue
        props = s.get("properties") or {}
        devs.append(AudioDevice(
            name=s.get("name", ""),
            description=s.get("description") or s.get("name", ""),
            fingerprint=device_fingerprint(props),
            state=s.get("state", ""),
        ))
    return devs


def parse_sinks(data: list) -> list[AudioDevice]:
    devs = []
    for s in data:
        if not isinstance(s, dict):
            continue
        props = s.get("properties") or {}
        devs.append(AudioDevice(
            name=s.get("name", ""),
            description=s.get("description") or s.get("name", ""),
            fingerprint=device_fingerprint(props),
            monitor_source=s.get("monitor_source"),
            state=s.get("state", ""),
        ))
    return devs


def list_microphones() -> list[AudioDevice]:
    """Capture devices only; sink monitors are excluded."""
    return parse_sources(_pactl_json("sources"))


def list_sinks() -> list[AudioDevice]:
    return parse_sinks(_pactl_json("sinks"))


def default_source_name() -> str:
    rc, out, _ = _run(["pactl", "get-default-source"])
    return out.strip() if rc == 0 else ""


def default_sink_name() -> str:
    rc, out, _ = _run(["pactl", "get-default-sink"])
    return out.strip() if rc == 0 else ""


def identity(dev: AudioDevice) -> dict:
    """What gets saved for a chosen device: its card fingerprint (or, for
    virtual devices without one, the node name) plus the current name, which
    tells apart several inputs or outputs of the same card."""
    fp = dict(dev.fingerprint) if dev.fingerprint else {"kind": "name", "value": dev.name}
    fp["name"] = dev.name
    return fp


def match_fingerprint(devices: list[AudioDevice], fp: dict | None) -> AudioDevice | None:
    if not fp:
        return None
    if fp.get("kind") == "name":
        return next((d for d in devices if d.name == fp.get("value")), None)
    key = {"kind": fp.get("kind"), "value": fp.get("value")}
    hits = [d for d in devices if d.fingerprint == key]
    if len(hits) > 1:
        same = [d for d in hits if d.name == fp.get("name")]
        if same:
            return same[0]
    return hits[0] if hits else None


# --------------------------------------------------------------------------
# monitors
# --------------------------------------------------------------------------

ALL_SCREENS = "__ALL__"
REGION = "__REGION__"


@dataclass
class Monitor:
    connector: str
    width: int
    height: int
    x: int
    y: int
    primary: bool = False
    full: bool = False


_MON_RE = re.compile(
    r"^\s*\d+:\s+\+(?P<pri>\*)?(?P<name>\S+)\s+"
    r"(?P<w>\d+)/\d+x(?P<h>\d+)/\d+"
    r"(?P<x>[-+]\d+)(?P<y>[-+]\d+)"
)
_SCREEN_RE = re.compile(r"current\s+(\d+)\s*x\s*(\d+)")


def _xrandr(args: list[str], display: str | None) -> str:
    env = dict(os.environ, DISPLAY=display or os.environ.get("DISPLAY") or ":0")
    try:
        p = subprocess.run(["xrandr", *args], capture_output=True, text=True,
                           timeout=10, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return p.stdout


def parse_monitors(text: str) -> list[Monitor]:
    mons = []
    for line in text.splitlines():
        m = _MON_RE.match(line)
        if not m:
            continue
        mons.append(Monitor(
            connector=m.group("name"),
            # yuv420p needs even sizes
            width=int(m.group("w")) // 2 * 2,
            height=int(m.group("h")) // 2 * 2,
            x=int(m.group("x")),
            y=int(m.group("y")),
            primary=bool(m.group("pri")),
        ))
    return mons


def parse_canvas(text: str) -> Monitor | None:
    m = _SCREEN_RE.search(text)
    if not m:
        return None
    w, h = int(m.group(1)) // 2 * 2, int(m.group(2)) // 2 * 2
    if w <= 0 or h <= 0:
        return None
    return Monitor(connector=ALL_SCREENS, width=w, height=h, x=0, y=0, full=True)


def list_monitors(display: str | None = None) -> list[Monitor]:
    return parse_monitors(_xrandr(["--listactivemonitors"], display))


def full_canvas(display: str | None = None) -> Monitor | None:
    """The whole X root window, i.e. every monitor in one frame."""
    return parse_canvas(_xrandr(["--current"], display))


def capture_targets(display: str | None = None) -> list[Monitor]:
    """What the user may record: each monitor, plus the whole desktop."""
    targets = list_monitors(display)
    canvas = full_canvas(display)
    if canvas and len(targets) > 1:
        targets.append(canvas)
    return targets


def clamp_region(x: int, y: int, width: int, height: int,
                 canvas: tuple[int, int] | None) -> dict | None:
    """Clamp a freehand rectangle to something x11grab will accept.

    Returns None when the rectangle is degenerate or smaller than the minimum.
    Coordinates are in X root-window space, the same space x11grab uses.
    """
    x, y, width, height = int(x), int(y), int(width), int(height)
    if width <= 0 or height <= 0:
        return None
    if canvas:
        cw, ch = canvas
        # Inside the root window, otherwise x11grab dies on the first frame.
        x = max(0, min(x, cw - 1))
        y = max(0, min(y, ch - 1))
        width = min(width, cw - x)
        height = min(height, ch - y)
    width -= width % 2
    height -= height % 2
    if width < MIN_REGION_W or height < MIN_REGION_H:
        return None
    return {"x": x, "y": y, "width": width, "height": height}


def normalize_region(x, y, width, height, display: str | None = None) -> dict | None:
    canvas = full_canvas(display)
    return clamp_region(x, y, width, height,
                        (canvas.width, canvas.height) if canvas else None)


def region_monitor(region: dict | None) -> Monitor | None:
    """Turn a stored region dict into a capture target."""
    if not region:
        return None
    try:
        return Monitor(connector=REGION,
                       width=int(region["width"]), height=int(region["height"]),
                       x=int(region["x"]), y=int(region["y"]))
    except (KeyError, TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# video encoders
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class VideoEncoder:
    codec: str            # ffmpeg encoder name
    fps: int
    args: tuple[str, ...]

    @property
    def label(self) -> str:
        return {"h264_nvenc": "H.264 (NVENC)", "hevc_nvenc": "HEVC (NVENC)",
                "libx264": "H.264 (x264)"}.get(self.codec, self.codec)


_PROBE_CACHE: dict[tuple[str, int, int], bool] = {}


def encoder_works(codec: str, width: int, height: int) -> bool:
    """Can this encoder actually open at this size on this machine?

    `ffmpeg -encoders` only says it was compiled in; NVENC also needs a GPU,
    a driver and a supported frame size. A two-frame test encode answers all
    of that. Results are cached per process.
    """
    key = (codec, width, height)
    if key not in _PROBE_CACHE:
        rc, _, _ = _run(["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
                         "-f", "lavfi", "-i", f"color=c=black:s={width}x{height}:r=30",
                         "-frames:v", "2", "-pix_fmt", "yuv420p", "-c:v", codec,
                         "-f", "null", "-"], timeout=30)
        _PROBE_CACHE[key] = rc == 0
    return _PROBE_CACHE[key]


def software_encoder(width: int, height: int) -> VideoEncoder:
    """x264 settings that keep up with a desktop on an ordinary CPU.

    Big canvases drop to 15 fps rather than shrinking: slides and text stay
    readable, and meeting screens rarely need more.
    """
    if width * height <= 2560 * 1440:
        return VideoEncoder("libx264", 30, ("-preset", "veryfast", "-crf", "23"))
    return VideoEncoder("libx264", 15, ("-preset", "superfast", "-crf", "24"))


def pick_encoder(width: int, height: int, probe=None) -> VideoEncoder:
    """NVENC when it works at this size, otherwise x264.

    h264_nvenc stops at 4096 pixels wide, so a multi-monitor canvas needs
    hevc_nvenc (up to 8192). Below that, H.264 is preferred for compatibility,
    and a size NVENC rejects (very small areas) goes to x264.
    """
    probe = probe or encoder_works
    if width <= 4096 and height <= 4096:
        if probe("h264_nvenc", width, height):
            return VideoEncoder("h264_nvenc", 30, ("-preset", "p4", "-rc", "vbr", "-cq", "24"))
    elif probe("hevc_nvenc", width, height):
        return VideoEncoder("hevc_nvenc", 30, ("-preset", "p4", "-rc", "vbr", "-cq", "26"))
    return software_encoder(width, height)


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def _clean_fp(v) -> dict | None:
    if (isinstance(v, dict) and isinstance(v.get("kind"), str)
            and isinstance(v.get("value"), str)):
        out = {"kind": v["kind"], "value": v["value"]}
        if isinstance(v.get("name"), str):
            out["name"] = v["name"]
        return out
    return None


def validate_config(raw) -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if not isinstance(raw, dict):
        return cfg
    cfg["mic"] = _clean_fp(raw.get("mic"))
    cfg["sink"] = _clean_fp(raw.get("sink"))
    gain = raw.get("mic_gain")
    if isinstance(gain, (int, float)) and not isinstance(gain, bool) and 0.1 <= gain <= 8:
        cfg["mic_gain"] = float(gain)
    if isinstance(raw.get("record_screen"), bool):
        cfg["record_screen"] = raw["record_screen"]
    if isinstance(raw.get("monitor"), str):
        cfg["monitor"] = raw["monitor"]
    if region_monitor(raw.get("region")) is not None:
        r = raw["region"]
        cfg["region"] = {k: int(r[k]) for k in ("x", "y", "width", "height")}
    if isinstance(raw.get("outdir"), str) and raw["outdir"].strip():
        cfg["outdir"] = raw["outdir"]
    return cfg


def load_config() -> dict:
    try:
        raw = json.loads(config_path().read_text())
    except (OSError, ValueError):
        raw = None
    return validate_config(raw)


def save_config(cfg: dict) -> None:
    _atomic_write_json(config_path(), cfg)


def outdir_of(cfg: dict) -> Path:
    return Path(cfg["outdir"]).expanduser() if cfg.get("outdir") else default_outdir()


# --------------------------------------------------------------------------
# recording plan
# --------------------------------------------------------------------------

class PlanError(Exception):
    pass


@dataclass
class RecordingPlan:
    """Everything the recording needs, frozen at the moment Start is pressed.

    Changing the UI mid-recording must never affect a running capture.
    """
    session: str
    mic_name: str
    mic_label: str
    sys_name: str
    sys_label: str
    mic_gain: float
    partial: str
    final: str
    with_video: bool
    monitor: dict | None
    encoder: dict | None
    display: str
    scratch: str
    started_at: float = field(default_factory=time.time)

    @property
    def meter_mic(self) -> str:
        return os.path.join(self.scratch, "mic.meter")

    @property
    def meter_sys(self) -> str:
        return os.path.join(self.scratch, "sys.meter")

    @property
    def progress(self) -> str:
        return os.path.join(self.scratch, "progress")

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict) -> "RecordingPlan":
        names = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in names})


def resolve_audio(cfg: dict) -> tuple[AudioDevice, AudioDevice]:
    """(microphone, sink) for this config right now, or PlanError."""
    mics = list_microphones()
    if not mics:
        raise PlanError(_msg("no-mic"))
    if cfg.get("mic"):
        mic = match_fingerprint(mics, cfg["mic"])
        if mic is None:
            raise PlanError(_msg("mic-missing"))
    else:
        want = default_source_name()
        mic = next((d for d in mics if d.name == want), None)
        if mic is None:
            raise PlanError(_msg("default-mic-not-mic"))

    sinks = list_sinks()
    if not sinks:
        raise PlanError(_msg("no-sink"))
    if cfg.get("sink"):
        sink = match_fingerprint(sinks, cfg["sink"])
        if sink is None:
            raise PlanError(_msg("sink-missing"))
    else:
        want = default_sink_name()
        sink = next((s for s in sinks if s.name == want), None)
        if sink is None:
            raise PlanError(_msg("default-sink-unknown"))
    if not sink.monitor_source:
        raise PlanError(_msg("no-monitor").format(name=sink.description))
    return mic, sink


def resolve_screen(cfg: dict, display: str) -> Monitor:
    if os.environ.get("XDG_SESSION_TYPE") == "wayland":
        raise PlanError(_msg("wayland"))
    canvas = full_canvas(display)
    targets = capture_targets(display)
    if not targets or canvas is None:
        raise PlanError(_msg("no-xrandr"))
    if cfg.get("monitor") == REGION:
        r = cfg.get("region") or {}
        region = clamp_region(r.get("x", 0), r.get("y", 0), r.get("width", 0),
                              r.get("height", 0), (canvas.width, canvas.height))
        # A stored area that no longer fits is not cut down quietly.
        if region is None or region != {k: r.get(k) for k in ("x", "y", "width", "height")}:
            raise PlanError(_msg("no-region"))
        return region_monitor(region)
    want = cfg.get("monitor")
    if want:
        mon = next((m for m in targets if m.connector == want), None)
        if mon is None:
            # Never fall back to another screen: it may show something private.
            raise PlanError(_msg("monitor-missing").format(name=want))
        return mon
    return next((m for m in targets if m.primary), targets[0])


def build_plan(cfg: dict, display: str | None = None) -> RecordingPlan:
    """Resolve volatile device names right now, and validate everything."""
    display = display or os.environ.get("DISPLAY") or ":0"
    mic, sink = resolve_audio(cfg)

    mon = enc = None
    with_video = bool(cfg.get("record_screen"))
    if with_video:
        mon = resolve_screen(cfg, display)
        enc = pick_encoder(mon.width, mon.height)

    outdir = outdir_of(cfg)
    try:
        outdir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise PlanError(_msg("outdir-create").format(error=e))
    if not os.access(outdir, os.W_OK):
        raise PlanError(_msg("outdir-write").format(path=outdir))
    free = shutil.disk_usage(outdir).free
    if free < MIN_FREE_START:
        raise PlanError(_msg("disk-low").format(free=human_bytes(free)))

    session = uuid.uuid4().hex[:8]
    base = outdir / f"meeting_{time.strftime('%Y%m%d_%H%M%S')}_{session[:4]}"
    scratch = runtime_dir() / session
    scratch.mkdir(mode=0o700)
    return RecordingPlan(
        session=session,
        mic_name=mic.name, mic_label=mic.description,
        sys_name=sink.monitor_source, sys_label=sink.description,
        mic_gain=float(cfg.get("mic_gain", 1.0)),
        partial=f"{base}.partial.mkv",
        final=f"{base}.mp4" if with_video else f"{base}.m4a",
        with_video=with_video,
        monitor=asdict(mon) if mon else None,
        encoder=asdict(enc) if enc else None,
        display=display,
        scratch=str(scratch),
    )


def _meter_chain(label_in: str, out_label: str, meter_file: str, gain: float | None) -> str:
    """One audio branch: normalize -> (gain) -> 10 Hz peak meter tap."""
    parts = [
        f"[{label_in}]aformat=sample_rates=48000:channel_layouts=stereo",
        "aresample=async=1:first_pts=0",
    ]
    if gain and abs(gain - 1.0) > 1e-6:
        parts.append(f"volume={gain}")
    parts += [
        # 4800 samples @ 48 kHz = one meter reading every 100 ms
        "asetnsamples=n=4800:p=0",
        "astats=metadata=1:reset=1",
        f"ametadata=mode=print:key=lavfi.astats.Overall.Peak_level:file={meter_file}:direct=1",
    ]
    return ",".join(parts) + f"[{out_label}]"


def video_queue_size(width: int, height: int) -> int:
    return max(4, min(64, VIDEO_QUEUE_BYTES // max(1, width * height * 4)))


def ffmpeg_argv(plan: RecordingPlan) -> list[str]:
    argv = ["ffmpeg", "-nostdin", "-n", "-hide_banner", "-loglevel", "warning", "-nostats"]

    if plan.with_video:
        m, enc = plan.monitor, plan.encoder
        argv += [
            "-f", "x11grab",
            "-thread_queue_size", str(video_queue_size(m["width"], m["height"])),
            "-framerate", str(enc["fps"]),
            "-video_size", f"{m['width']}x{m['height']}",
            "-draw_mouse", "1",
            "-i", f"{plan.display}+{m['x']},{m['y']}",
        ]
    mic_idx = 1 if plan.with_video else 0
    sys_idx = mic_idx + 1

    argv += ["-f", "pulse", "-thread_queue_size", "1024", "-i", plan.mic_name]
    argv += ["-f", "pulse", "-thread_queue_size", "1024", "-i", plan.sys_name]

    fc = ";".join([
        _meter_chain(f"{mic_idx}:a", "mic", plan.meter_mic, plan.mic_gain),
        _meter_chain(f"{sys_idx}:a", "sys", plan.meter_sys, None),
        # normalize=0 keeps both sources at full level; the limiter catches
        # peaks when a boosted mic and system sound add up. level=0 turns off
        # alimiter's auto-level, which would push everything back to 0 dBFS.
        "[mic][sys]amix=inputs=2:duration=longest:normalize=0,"
        "alimiter=limit=0.95:level=0:latency=1[a]",
    ])
    argv += ["-filter_complex", fc]

    if plan.with_video:
        enc = plan.encoder
        argv += ["-map", "0:v", "-map", "[a]", "-c:v", enc["codec"], *enc["args"],
                 "-pix_fmt", "yuv420p"]
    else:
        argv += ["-map", "[a]"]

    argv += ["-c:a", "aac", "-b:a", "192k"]
    # Get data onto disk about once a second. By default an open Matroska
    # cluster and ffmpeg's 256 KiB write buffer hold the most recent seconds
    # (ten or more for audio only), and a killed process loses them.
    argv += ["-cluster_time_limit", "1000", "-flush_packets", "1"]
    argv += ["-progress", plan.progress]
    argv += [plan.partial]
    return argv


# --------------------------------------------------------------------------
# transient service control
# --------------------------------------------------------------------------

def unit_state() -> str:
    """systemd ActiveState of the recording unit, or 'unknown'."""
    rc, out, _ = _run(["systemctl", "--user", "show", "--property=ActiveState",
                       "--value", UNIT_SERVICE], timeout=10)
    return out.strip() if rc == 0 and out.strip() else "unknown"


def unit_running() -> bool | None:
    """True/False, or None when systemd could not be asked. Callers must not
    treat None as stopped: that would finish a file ffmpeg is still writing."""
    state = unit_state()
    if state == "unknown":
        return None
    return state in ("active", "activating", "deactivating", "reloading")


def unit_invocation() -> str:
    rc, out, _ = _run(["systemctl", "--user", "show", "--property=InvocationID",
                       "--value", UNIT_SERVICE], timeout=10)
    return out.strip() if rc == 0 else ""


def _systemd_version() -> int:
    rc, out, _ = _run(["systemd-run", "--version"], timeout=5)
    m = re.match(r"systemd (\d+)", out or "")
    return int(m.group(1)) if rc == 0 and m else 0


def unit_argv(plan: RecordingPlan, systemd_version: int | None = None) -> list[str]:
    argv = [
        "systemd-run", "--user", "--quiet",
        f"--unit={UNIT}",
        "--collect",
        "--service-type=exec",
        "--property=KillSignal=SIGINT",
        "--property=KillMode=mixed",
        "--property=TimeoutStopSec=90",
        "--property=SuccessExitStatus=255",
        "--property=UMask=0077",
        f"--setenv=DISPLAY={plan.display}",
    ]
    version = _systemd_version() if systemd_version is None else systemd_version
    if version >= 254:
        # A file name with '$' in it must reach ffmpeg as written.
        argv.append("--expand-environment=no")
    for key in PASSTHROUGH_ENV:
        if os.environ.get(key):
            argv.append(f"--setenv={key}={os.environ[key]}")
    return argv + ["--", *ffmpeg_argv(plan)]


def save_state(plan: RecordingPlan, phase: str, invocation: str = "") -> None:
    _atomic_write_json(state_path(), {"version": STATE_VERSION, "phase": phase,
                                      "invocation": invocation, "plan": plan.to_json()})


def load_state() -> tuple[RecordingPlan, str, str] | None:
    """(plan, phase, invocation id) of the session on disk, if any."""
    try:
        data = json.loads(state_path().read_text())
        if data.get("version") != STATE_VERSION:
            return None
        return RecordingPlan.from_json(data["plan"]), data["phase"], data.get("invocation", "")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def clear_state() -> None:
    try:
        state_path().unlink(missing_ok=True)
    except OSError:
        pass


def recording_started(plan: RecordingPlan) -> bool:
    """ffmpeg has encoded some media (not just opened the output)."""
    secs = read_progress_seconds(plan.progress)
    return secs is not None and secs > 0


def _spool_size(plan: RecordingPlan) -> int:
    try:
        return os.path.getsize(plan.partial)
    except OSError:
        return 0


def discard(plan: RecordingPlan) -> None:
    """Drop the scratch files of a session, and the spool only if it is empty."""
    if _spool_size(plan) == 0:
        Path(plan.partial).unlink(missing_ok=True)
    shutil.rmtree(plan.scratch, ignore_errors=True)


def start_recording(plan: RecordingPlan, timeout: float = 6.0) -> None:
    """Launch ffmpeg as a transient user service and wait until data flows.

    A service (not a scope) means systemd owns the capture: it survives a GUI
    crash, and stopping it is a real SIGINT + bounded wait, so the file gets
    closed properly. The plan is written to disk before the service exists
    (the invocation id follows once it runs), so a crash in between still
    leaves a trail to recover from.
    """
    running = unit_running()
    if running is None:
        raise PlanError(_msg("systemd-unknown"))
    if running:
        raise PlanError(_msg("already-recording"))
    _run(["systemctl", "--user", "reset-failed", UNIT_SERVICE], timeout=10)
    save_state(plan, "starting")

    rc, out, err = _run(unit_argv(plan), timeout=25)
    if rc != 0 and unit_running() is not True:
        clear_state()
        discard(plan)
        raise PlanError(_msg("start-failed").format(error=(err or out).strip()[:300]))
    try:
        save_state(plan, "recording", unit_invocation())
    except OSError as e:
        # Without a session file nobody could find this recording again.
        stop_recording()
        clear_state()
        raise PlanError(_msg("state-failed").format(error=e, path=plan.partial))

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.15)
        if unit_running() is False:
            log = recent_log(8)
            clear_state()
            kept = _spool_size(plan) > 0
            discard(plan)
            msg = _msg("exited").format(log=log)
            if kept:
                msg += "\n" + _msg("kept").format(path=plan.partial)
            raise PlanError(msg)
        if recording_started(plan):
            return
    if not stop_recording():
        raise PlanError(_msg("no-data-stuck").format(unit=UNIT_SERVICE))
    clear_state()
    discard(plan)
    raise PlanError(_msg("no-data"))


def stop_recording(timeout: float = 120) -> bool:
    """Ask systemd to stop; it delivers SIGINT and waits for ffmpeg to exit.

    True only once systemd confirms the unit is gone.
    """
    _run(["systemctl", "--user", "stop", UNIT_SERVICE], timeout=timeout)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if unit_running() is False:
            return True
        time.sleep(0.3)
    return False


def recent_log(lines: int = 20) -> str:
    rc, out, _ = _run(
        ["journalctl", "--user", "-u", UNIT_SERVICE, "-n", str(lines), "-o", "cat", "--no-pager"],
        timeout=10,
    )
    return out.strip() if rc == 0 else ""


# --------------------------------------------------------------------------
# meters / progress
# --------------------------------------------------------------------------

_PEAK_RE = re.compile(rb"Peak_level=(-?[\d.]+|-?inf)")


def _tail(path: str, size: int = 4096) -> bytes | None:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            f.seek(max(0, end - size))
            return f.read()
    except OSError:
        return None


def read_meter_db(path: str, stale_s: float = METER_STALE_S) -> float | None:
    """Last peak level in dBFS from an ametadata tap.

    None if nothing arrived yet or the tap stopped updating, so a dead capture
    can't keep showing its last level.
    """
    try:
        if time.time() - os.path.getmtime(path) > stale_s:
            return None
    except OSError:
        return None
    blob = _tail(path)
    hits = _PEAK_RE.findall(blob or b"")
    if not hits:
        return None
    raw = hits[-1].decode()
    if "inf" in raw:
        return METER_FLOOR_DB
    try:
        return max(METER_FLOOR_DB, float(raw))
    except ValueError:
        return None


def read_progress_seconds(path: str) -> float | None:
    blob = _tail(path)
    if blob is None:
        return None
    vals = re.findall(r"out_time_us=(\d+)", blob.decode(errors="ignore"))
    if not vals:
        return None
    return int(vals[-1]) / 1_000_000.0


def db_to_fraction(db: float | None) -> float:
    if db is None:
        return 0.0
    return max(0.0, min(1.0, (db - METER_FLOOR_DB) / (0.0 - METER_FLOOR_DB)))


# --------------------------------------------------------------------------
# input check (idle)
# --------------------------------------------------------------------------

def check_inputs(cfg: dict, seconds: float = 3.0) -> dict:
    """Open both sources briefly and report whether real signal arrives."""
    mic, sink = resolve_audio(cfg)
    gain = float(cfg.get("mic_gain", 1.0))
    with tempfile.TemporaryDirectory(dir=runtime_dir(), prefix="check-") as td:
        mic_tap, sys_tap = os.path.join(td, "mic.meter"), os.path.join(td, "sys.meter")
        fc = ";".join([
            _meter_chain("0:a", "mic", mic_tap, gain),
            _meter_chain("1:a", "sys", sys_tap, None),
            "[mic][sys]amix=inputs=2:duration=longest:normalize=0[a]",
        ])
        argv = [
            "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-nostats",
            "-f", "pulse", "-thread_queue_size", "1024", "-i", mic.name,
            "-f", "pulse", "-thread_queue_size", "1024", "-i", sink.monitor_source,
            "-filter_complex", fc,
            "-map", "[a]", "-t", str(seconds), "-f", "null", "-",
        ]
        rc, _, err = _run(argv, timeout=seconds + 20)

        def peak(path):
            try:
                hits = _PEAK_RE.findall(Path(path).read_bytes())
            except OSError:
                return None
            vals = []
            for raw in hits:
                text = raw.decode()
                if "inf" in text:
                    continue
                try:
                    vals.append(float(text))
                except ValueError:
                    pass
            return max(vals) if vals else None

        return {
            "ok": rc == 0,
            "error": (err or "").strip()[:300],
            "mic_label": mic.description,
            "sys_label": sink.description,
            "mic_peak": peak(mic_tap),
            "sys_peak": peak(sys_tap),
        }


# --------------------------------------------------------------------------
# finalize
# --------------------------------------------------------------------------

def probe(path: str) -> dict | None:
    rc, out, _ = _run([
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration,size:stream=codec_type,codec_name",
        "-of", "json", path,
    ], timeout=60)
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def _streams(info: dict | None) -> dict[str, str]:
    """codec_type -> codec_name of the first stream of each type."""
    out: dict[str, str] = {}
    for s in (info or {}).get("streams", []):
        out.setdefault(s.get("codec_type"), s.get("codec_name"))
    return out


def _duration(info: dict | None) -> float:
    try:
        return float((info or {}).get("format", {}).get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0


def finalize(plan: RecordingPlan) -> dict:
    """MKV spool -> validated MP4/M4A, keeping the spool until that worked.

    Writing straight into MP4 risks losing the file if the process dies before
    the moov atom is written; MKV keeps what was recorded so far readable. The
    remux is a stream copy, so it costs no quality. A spool cut off without a
    duration in its header is still remuxed; the output is what gets checked.
    """
    partial = Path(plan.partial)
    if not partial.exists() or partial.stat().st_size == 0:
        return {"ok": False, "message": _msg("no-file"), "path": None}

    in_info = probe(str(partial))
    streams = _streams(in_info)
    if "audio" not in streams:
        return {"ok": False, "message": _msg("no-audio").format(path=partial), "path": str(partial)}
    if plan.with_video and "video" not in streams:
        return {"ok": False, "message": _msg("no-video").format(path=partial), "path": str(partial)}

    final = Path(plan.final)
    free = shutil.disk_usage(final.parent).free
    if free < partial.stat().st_size + 256 * 1024**2:
        return {"ok": False, "message": _msg("remux-space").format(path=partial),
                "path": str(partial)}

    # Unique scratch name next to the target; the real extension stays last
    # because ffmpeg picks the muxer from it. umask 077: the file holds the
    # whole recording before it is published.
    tmp = final.with_name(f".{final.stem}.{uuid.uuid4().hex[:8]}.finalizing{final.suffix}")
    argv = ["ffmpeg", "-nostdin", "-n", "-hide_banner", "-loglevel", "error",
            "-i", str(partial), "-map", "0", "-c", "copy", "-movflags", "+faststart"]
    if streams.get("video") == "hevc":
        argv += ["-tag:v", "hvc1"]      # what QuickTime and Safari expect in MP4
    rc, _out, err = _run(argv + [str(tmp)], timeout=3600, umask=0o077)
    info = probe(str(tmp)) if rc == 0 and tmp.exists() else None
    out_streams = _streams(info)
    duration = _duration(info)
    if (info is None or duration <= 0 or "audio" not in out_streams
            or (plan.with_video and "video" not in out_streams)):
        tmp.unlink(missing_ok=True)
        return {"ok": False,
                "message": _msg("remux-failed").format(path=partial, error=(err or "").strip()[:200]),
                "path": str(partial)}

    # How long should it be? The spool's own duration after a clean stop,
    # otherwise the last position ffmpeg reported. A clearly shorter result
    # is still saved, but the spool is kept next to it.
    expected = _duration(in_info) or (read_progress_seconds(plan.progress) or 0.0)
    short = expected > 0 and duration < expected - 2.0

    final = _publish(tmp, final)
    if not short:
        partial.unlink(missing_ok=True)
        shutil.rmtree(plan.scratch, ignore_errors=True)
    result = {"ok": True, "message": _msg("saved"), "path": str(final),
              "duration": duration, "size": final.stat().st_size}
    if short:
        result["message"] = _msg("saved-short").format(
            got=human_duration(duration), expected=human_duration(expected), path=partial)
    return result


def _publish(tmp: Path, final: Path) -> Path:
    """Move tmp to final without ever replacing an existing file.

    The name is claimed with O_EXCL first, so this also holds on filesystems
    without hard links and when another program creates the same name.
    """
    os.chmod(tmp, 0o600)
    cand = final
    for i in range(1, 1000):
        try:
            fd = os.open(cand, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            cand = final.with_name(f"{final.stem}-{i + 1}{final.suffix}")
            continue
        os.close(fd)
        os.replace(tmp, cand)        # replaces only the placeholder we just made
        return cand
    raise FileExistsError(final)


def safe_finalize(plan: RecordingPlan) -> dict:
    """finalize() that turns any exception into the usual failure result."""
    try:
        return finalize(plan)
    except Exception as e:  # noqa: BLE001 - the raw recording must stay findable
        return {"ok": False, "path": plan.partial,
                "message": _msg("remux-failed").format(path=plan.partial, error=repr(e))}


# --------------------------------------------------------------------------
# user-facing messages
# --------------------------------------------------------------------------

def _msg(key: str) -> str:
    from .i18n import _
    return {
        "no-mic": _("No microphone found. Check your audio devices."),
        "mic-missing": _("The saved microphone isn't connected. Plug it back in or pick another one."),
        "default-mic-not-mic": _("The system's default input isn't a microphone "
                                 "(it may be a monitor). Pick a microphone."),
        "no-sink": _("No output device found."),
        "sink-missing": _("The saved output device isn't connected. Pick it again."),
        "default-sink-unknown": _("Couldn't tell which output is the default."),
        "no-monitor": _("“{name}” has no monitor source, so its sound can't be recorded."),
        "wayland": _("Screen capture needs an X11 session. Record audio only, "
                     "or log in with X11."),
        "no-xrandr": _("Couldn't read the monitor layout (xrandr)."),
        "no-region": _("The saved area doesn't fit the current screen. Pick the area again."),
        "outdir-create": _("Can't create the save folder: {error}"),
        "outdir-write": _("Can't write to the save folder: {path}"),
        "disk-low": _("Not enough free disk space ({free})."),
        "already-recording": _("A recording is already running."),
        "systemd-unknown": _("Couldn't ask systemd about the recording service. "
                             "Is the user session running?"),
        "state-failed": _("Couldn't save the session file ({error}), so the recording "
                          "was stopped. The raw file is here: {path}"),
        "kept": _("The raw file was kept: {path}"),
        "no-data-stuck": _("No data arrived and the recorder did not stop. "
                           "Check: systemctl --user status {unit}"),
        "monitor-missing": _("The saved screen ({name}) isn't connected. Pick what to record."),
        "saved-short": _("Saved {got} of about {expected}. The raw recording was kept "
                         "in case more can be recovered: {path}"),
        "start-failed": _("Couldn't start recording: {error}"),
        "exited": _("The recording stopped right after it started:\n{log}"),
        "no-data": _("The recording started but no data arrived, so it was stopped."),
        "no-file": _("No recording file was created."),
        "no-audio": _("The recording has no audio track. The raw file was kept: {path}"),
        "no-video": _("The recording has no video track. The raw file was kept: {path}"),
        "remux-space": _("Not enough space to finish the file. The raw recording was kept: {path}"),
        "remux-failed": _("Couldn't finish the file. The raw recording was kept: {path}\n{error}"),
        "saved": _("Saved"),
    }[key]
