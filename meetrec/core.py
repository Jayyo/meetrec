"""Core logic for meetrec.

Device discovery, recording plan construction, transient-service control and
finalization. Deliberately free of any GUI dependency so it can be tested from
a shell.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, asdict, field
from pathlib import Path

APP_ID = "io.github.jayyo.MeetRec"
UNIT = "meetrec-recording"
UNIT_SERVICE = f"{UNIT}.service"

HOME = Path.home()
CONFIG_PATH = HOME / ".config" / "meetrec" / "config.json"
STATE_PATH = HOME / ".local" / "state" / "meetrec" / "current.json"
DEFAULT_OUTDIR = HOME / "Videos" / "Meetings"

# Runtime scratch (tmpfs): meter taps and ffmpeg progress.
RUNTIME_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "meetrec"

# Encoder limits / policy.
NVENC_MAX_WIDTH = 4096
MIN_FREE_START = 5 * 1024**3       # refuse to start below this
WARN_FREE_START = 20 * 1024**3     # warn below this
METER_FLOOR_DB = -60.0

DEFAULT_CONFIG = {
    "mic": None,            # fingerprint dict, or None = follow system default
    "sink": None,           # fingerprint dict, or None = follow system default
    "mic_gain": 1.6,
    "record_screen": False,
    "monitor": None,        # xrandr connector name, ALL_SCREENS, or REGION
    "region": None,         # {"x","y","width","height"} when monitor == REGION
    "outdir": str(DEFAULT_OUTDIR),
}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _run(argv, timeout=15):
    """Run a command, return (rc, stdout, stderr). Never raises on failure."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{argv[0]}: timeout"


def human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}TB"


def human_duration(sec: float) -> str:
    sec = int(max(0, sec))
    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


# --------------------------------------------------------------------------
# audio devices
# --------------------------------------------------------------------------

def device_fingerprint(props: dict) -> dict | None:
    """Stable identity for a sound card, immune to PulseAudio name churn.

    PipeWire renames nodes when a headset is re-plugged or switches profile
    (mono-fallback -> mono-fallback.2 -> .3, analog-stereo -> iec958-stereo),
    so the node name must never be persisted. Card serial / bus path do not
    change across re-plugs of the same physical port.
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
    name: str                 # pulse node name (volatile, resolved fresh each run)
    description: str
    fingerprint: dict | None
    monitor_source: str | None = None   # sinks only
    state: str = ""


def _pactl_json(what: str) -> list:
    rc, out, _ = _run(["pactl", "--format=json", "list", what])
    if rc != 0 or not out.strip():
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return []


def list_microphones() -> list[AudioDevice]:
    """Real capture devices only - sink monitors are excluded."""
    devs = []
    for s in _pactl_json("sources"):
        props = s.get("properties", {}) or {}
        if props.get("media.class") != "Audio/Source":
            continue
        devs.append(AudioDevice(
            name=s.get("name", ""),
            description=s.get("description") or s.get("name", ""),
            fingerprint=device_fingerprint(props),
            state=s.get("state", ""),
        ))
    return devs


def list_sinks() -> list[AudioDevice]:
    devs = []
    for s in _pactl_json("sinks"):
        props = s.get("properties", {}) or {}
        devs.append(AudioDevice(
            name=s.get("name", ""),
            description=s.get("description") or s.get("name", ""),
            fingerprint=device_fingerprint(props),
            monitor_source=s.get("monitor_source"),
            state=s.get("state", ""),
        ))
    return devs


def default_source_name() -> str:
    rc, out, _ = _run(["pactl", "get-default-source"])
    return out.strip() if rc == 0 else ""


def default_sink_name() -> str:
    rc, out, _ = _run(["pactl", "get-default-sink"])
    return out.strip() if rc == 0 else ""


def match_fingerprint(devices: list[AudioDevice], fp: dict | None) -> AudioDevice | None:
    if not fp:
        return None
    for d in devices:
        if d.fingerprint == fp:
            return d
    return None


# --------------------------------------------------------------------------
# monitors
# --------------------------------------------------------------------------

ALL_SCREENS = "__ALL__"
REGION = "__REGION__"

# x11grab + yuv420p need even dimensions, and h264_nvenc refuses small frames:
# measured on this box, 144x300 and 200x48 fail with "Frame Dimension less than
# the minimum supported value" while 146x146 and 200x50 encode fine.
MIN_REGION_W = 146
MIN_REGION_H = 50


@dataclass
class Monitor:
    connector: str
    width: int
    height: int
    x: int
    y: int
    primary: bool = False
    full: bool = False

    @property
    def label(self) -> str:
        codec = "HEVC" if self.needs_hevc else "H.264"
        if self.connector == REGION:
            return (f"선택 영역 · {self.width}×{self.height} "
                    f"@ ({self.x},{self.y}) · {codec}")
        if self.full:
            return f"전체 화면 (모든 모니터) · {self.width}×{self.height} · {codec}"
        tag = "★ " if self.primary else ""
        return f"{tag}{self.connector} · {self.width}×{self.height} · {codec}"

    @property
    def needs_hevc(self) -> bool:
        """h264_nvenc refuses anything wider than 4096; hevc_nvenc goes to 8192."""
        return self.width > NVENC_MAX_WIDTH


_MON_RE = re.compile(
    r"^\s*\d+:\s+\+(?P<pri>\*)?(?P<name>\S+)\s+"
    r"(?P<w>\d+)/\d+x(?P<h>\d+)/\d+"
    r"(?P<x>[-+]\d+)(?P<y>[-+]\d+)"
)


def list_monitors(display: str | None = None) -> list[Monitor]:
    env_display = display or os.environ.get("DISPLAY") or ":0"
    env = dict(os.environ, DISPLAY=env_display)
    try:
        p = subprocess.run(["xrandr", "--listactivemonitors"],
                           capture_output=True, text=True, timeout=10, env=env)
    except Exception:
        return []
    mons = []
    for line in p.stdout.splitlines():
        m = _MON_RE.match(line)
        if not m:
            continue
        mons.append(Monitor(
            connector=m.group("name"),
            width=int(m.group("w")),
            height=int(m.group("h")),
            x=int(m.group("x")),
            y=int(m.group("y")),
            primary=bool(m.group("pri")),
        ))
    return mons


_SCREEN_RE = re.compile(r"current\s+(\d+)\s*x\s*(\d+)")


def full_canvas(display: str | None = None) -> Monitor | None:
    """The whole X root window, i.e. every monitor in one frame.

    Dimensions are rounded down to even numbers because yuv420p requires it.
    """
    env_display = display or os.environ.get("DISPLAY") or ":0"
    env = dict(os.environ, DISPLAY=env_display)
    try:
        p = subprocess.run(["xrandr", "--current"], capture_output=True,
                           text=True, timeout=10, env=env)
    except Exception:
        return None
    m = _SCREEN_RE.search(p.stdout)
    if not m:
        return None
    w, h = int(m.group(1)) // 2 * 2, int(m.group(2)) // 2 * 2
    if w <= 0 or h <= 0:
        return None
    return Monitor(connector=ALL_SCREENS, width=w, height=h, x=0, y=0, full=True)


def capture_targets(display: str | None = None) -> list[Monitor]:
    """What the user may record: each monitor, plus the whole desktop."""
    targets = list_monitors(display)
    canvas = full_canvas(display)
    if canvas and len(targets) > 1:
        targets.append(canvas)
    return targets


def normalize_region(x: int, y: int, width: int, height: int,
                     display: str | None = None) -> dict | None:
    """Clamp a freehand rectangle to something x11grab/NVENC will accept.

    Returns None when the rectangle is degenerate or below the NVENC minimum.
    Coordinates are in X root-window space, the same space x11grab uses.
    """
    x, y = int(x), int(y)
    width, height = int(width), int(height)
    if width <= 0 or height <= 0:
        return None

    canvas = full_canvas(display)
    if canvas:
        # Keep the rectangle inside the root window, otherwise x11grab reads
        # past the screen and ffmpeg dies on the first frame.
        x = max(0, min(x, canvas.width - 1))
        y = max(0, min(y, canvas.height - 1))
        width = min(width, canvas.width - x)
        height = min(height, canvas.height - y)

    width -= width % 2
    height -= height % 2
    if width < MIN_REGION_W or height < MIN_REGION_H:
        return None
    return {"x": x, "y": y, "width": width, "height": height}


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
# config
# --------------------------------------------------------------------------

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    tmp.replace(CONFIG_PATH)


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
    mic_name: str
    mic_label: str
    sys_name: str
    sys_label: str
    mic_gain: float
    partial: str
    final: str
    with_video: bool
    monitor: dict | None
    display: str
    meter_mic: str
    meter_sys: str
    progress: str
    started_at: float = field(default_factory=time.time)

    def to_json(self) -> dict:
        return asdict(self)


def build_plan(cfg: dict, display: str | None = None) -> RecordingPlan:
    """Resolve volatile device names right now, and validate everything."""
    display = display or os.environ.get("DISPLAY") or ":0"

    # --- microphone -------------------------------------------------------
    mics = list_microphones()
    if not mics:
        raise PlanError("사용 가능한 마이크가 없어. 오디오 장치를 확인해줘.")
    if cfg.get("mic"):
        dev = match_fingerprint(mics, cfg["mic"])
        if dev is None:
            raise PlanError(
                "저장된 마이크를 찾지 못했어. 장치를 다시 연결하거나 "
                "마이크를 다시 선택해줘."
            )
    else:
        want = default_source_name()
        dev = next((d for d in mics if d.name == want), None)
        if dev is None:
            raise PlanError(
                "시스템 기본 입력이 실제 마이크가 아니야(모니터 소스일 수 있음). "
                "마이크를 직접 선택해줘."
            )
    mic_name, mic_label = dev.name, dev.description

    # --- system sound (sink monitor) --------------------------------------
    sinks = list_sinks()
    if not sinks:
        raise PlanError("사용 가능한 출력 장치가 없어.")
    if cfg.get("sink"):
        sink = match_fingerprint(sinks, cfg["sink"])
        if sink is None:
            raise PlanError("저장된 출력 장치를 찾지 못했어. 다시 선택해줘.")
    else:
        want = default_sink_name()
        sink = next((s for s in sinks if s.name == want), None)
        if sink is None:
            raise PlanError("기본 출력 장치를 확인하지 못했어.")
    if not sink.monitor_source:
        raise PlanError(
            f"'{sink.description}' 에 모니터 소스가 없어서 시스템 소리를 녹음할 수 없어."
        )
    sys_name, sys_label = sink.monitor_source, sink.description

    # --- screen -----------------------------------------------------------
    mon_dict = None
    with_video = bool(cfg.get("record_screen"))
    if with_video:
        if os.environ.get("XDG_SESSION_TYPE") == "wayland":
            raise PlanError(
                "현재 세션이 Wayland라 화면 캡처를 쓸 수 없어. "
                "오디오만 녹음하거나 X11 세션으로 로그인해줘."
            )
        mons = capture_targets(display)
        if not mons:
            raise PlanError("모니터 정보를 읽지 못했어(xrandr).")
        want = cfg.get("monitor")
        if want == REGION:
            mon = region_monitor(cfg.get("region"))
            if mon is None:
                raise PlanError(
                    "녹화할 영역이 없어 — '녹화 대상'에서 영역을 다시 지정해줘."
                )
        else:
            mon = next((m for m in mons if m.connector == want), None)
            if mon is None:
                mon = next((m for m in mons if m.primary), mons[0])
        mon_dict = asdict(mon)

    # --- output -----------------------------------------------------------
    outdir = Path(cfg.get("outdir") or DEFAULT_OUTDIR).expanduser()
    try:
        outdir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise PlanError(f"저장 폴더를 만들 수 없어: {e}")
    if not os.access(outdir, os.W_OK):
        raise PlanError(f"저장 폴더에 쓸 수 없어: {outdir}")

    free = shutil.disk_usage(outdir).free
    if free < MIN_FREE_START:
        raise PlanError(f"디스크 여유 공간이 부족해({human_bytes(free)}).")

    stamp = time.strftime("%Y%m%d_%H%M%S")
    tag = uuid.uuid4().hex[:4]
    base = outdir / f"meeting_{stamp}_{tag}"
    partial = f"{base}.partial.mkv"
    final = f"{base}.mp4" if with_video else f"{base}.m4a"

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    for stale in RUNTIME_DIR.glob("*.meter"):
        stale.unlink(missing_ok=True)

    return RecordingPlan(
        mic_name=mic_name, mic_label=mic_label,
        sys_name=sys_name, sys_label=sys_label,
        mic_gain=float(cfg.get("mic_gain", 1.6)),
        partial=partial, final=final,
        with_video=with_video, monitor=mon_dict, display=display,
        meter_mic=str(RUNTIME_DIR / "mic.meter"),
        meter_sys=str(RUNTIME_DIR / "sys.meter"),
        progress=str(RUNTIME_DIR / "progress"),
    )


def _meter_chain(label_in: str, out_label: str, meter_file: str, gain: float | None) -> str:
    """One audio branch: normalize -> (gain) -> 5 Hz peak meter tap."""
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


def ffmpeg_argv(plan: RecordingPlan) -> list[str]:
    argv = ["ffmpeg", "-nostdin", "-n", "-hide_banner", "-loglevel", "warning", "-nostats"]

    if plan.with_video:
        m = plan.monitor
        argv += [
            "-f", "x11grab", "-thread_queue_size", "1024",
            "-framerate", "30",
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
        # normalize=0 keeps both sources at full level; the limiter catches the
        # clipping that boosted mic + system sound can otherwise produce.
        "[mic][sys]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95[a]",
    ])
    argv += ["-filter_complex", fc]

    if plan.with_video:
        wide = plan.monitor["width"] > NVENC_MAX_WIDTH
        argv += ["-map", "0:v", "-map", "[a]"]
        if wide:
            # Multi-monitor canvas: only HEVC NVENC accepts width > 4096.
            argv += ["-c:v", "hevc_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "26",
                     "-tag:v", "hvc1"]
        else:
            argv += ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "24"]
        argv += ["-pix_fmt", "yuv420p"]
    else:
        argv += ["-map", "[a]"]

    argv += ["-c:a", "aac", "-b:a", "192k"]
    argv += ["-progress", plan.progress]
    argv += [plan.partial]
    return argv


# --------------------------------------------------------------------------
# transient service control
# --------------------------------------------------------------------------

def unit_active() -> bool:
    rc, out, _ = _run(["systemctl", "--user", "is-active", UNIT_SERVICE], timeout=10)
    return out.strip() == "active"


def start_recording(plan: RecordingPlan) -> None:
    """Launch ffmpeg as a transient user service.

    A service (not a scope) means the capture is owned by systemd: it survives
    a GUI crash, and stopping it is a real SIGINT + bounded wait, so the file is
    always closed properly.
    """
    if unit_active():
        raise PlanError("이미 녹음이 진행 중이야.")
    _run(["systemctl", "--user", "reset-failed", UNIT_SERVICE], timeout=10)

    for f in (plan.meter_mic, plan.meter_sys, plan.progress):
        Path(f).unlink(missing_ok=True)

    argv = [
        "systemd-run", "--user",
        f"--unit={UNIT}",
        "--collect",
        "--service-type=exec",
        "--property=KillSignal=SIGINT",
        "--property=KillMode=mixed",
        "--property=TimeoutStopSec=90",
        "--property=SuccessExitStatus=255",
        f"--setenv=DISPLAY={plan.display}",
    ]
    xauth = os.environ.get("XAUTHORITY")
    if xauth:
        argv.append(f"--setenv=XAUTHORITY={xauth}")
    argv += ffmpeg_argv(plan)

    rc, out, err = _run(argv, timeout=25)
    if rc != 0:
        raise PlanError(f"녹음을 시작하지 못했어: {(err or out).strip()[:300]}")

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(plan.to_json(), ensure_ascii=False, indent=2))

    # Give ffmpeg a moment to fail loudly (bad device, busy encoder, ...).
    for _ in range(20):
        time.sleep(0.15)
        if not unit_active():
            raise PlanError(
                "녹음이 시작 직후 종료됐어. 장치 상태를 확인해줘:\n"
                + recent_log(8)
            )
        if Path(plan.partial).exists():
            break


def stop_recording(timeout: int = 120) -> None:
    """Ask systemd to stop; it delivers SIGINT and waits for a clean exit."""
    _run(["systemctl", "--user", "stop", UNIT_SERVICE], timeout=timeout)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not unit_active():
            return
        time.sleep(0.3)


def recent_log(lines: int = 20) -> str:
    rc, out, _ = _run(
        ["journalctl", "--user", "-u", UNIT_SERVICE, "-n", str(lines), "-o", "cat", "--no-pager"],
        timeout=10,
    )
    return out.strip() if rc == 0 else ""


def load_state() -> RecordingPlan | None:
    try:
        data = json.loads(STATE_PATH.read_text())
        return RecordingPlan(**data)
    except Exception:
        return None


def clear_state() -> None:
    STATE_PATH.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# meters / progress
# --------------------------------------------------------------------------

_PEAK_RE = re.compile(rb"Peak_level=(-?[\d.]+|-?inf)")


def read_meter_db(path: str) -> float | None:
    """Last peak level in dBFS from an ametadata tap. None if nothing yet."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > 4096:
                f.seek(size - 4096)
            blob = f.read()
    except OSError:
        return None
    hits = _PEAK_RE.findall(blob)
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
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > 4096:
                f.seek(size - 4096)
            blob = f.read().decode(errors="ignore")
    except OSError:
        return None
    vals = re.findall(r"out_time_us=(\d+)", blob)
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

def check_inputs(cfg: dict, seconds: float = 3.0, display: str | None = None) -> dict:
    """Open both sources briefly and report whether real signal arrives."""
    probe = dict(cfg)
    probe["record_screen"] = False
    plan = build_plan(probe, display=display)

    mic_tap = str(RUNTIME_DIR / "check_mic.meter")
    sys_tap = str(RUNTIME_DIR / "check_sys.meter")
    for f in (mic_tap, sys_tap):
        Path(f).unlink(missing_ok=True)

    fc = ";".join([
        _meter_chain("0:a", "mic", mic_tap, plan.mic_gain),
        _meter_chain("1:a", "sys", sys_tap, None),
        "[mic][sys]amix=inputs=2:duration=longest:normalize=0[a]",
    ])
    argv = [
        "ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error", "-nostats",
        "-f", "pulse", "-thread_queue_size", "1024", "-i", plan.mic_name,
        "-f", "pulse", "-thread_queue_size", "1024", "-i", plan.sys_name,
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

    result = {
        "ok": rc == 0,
        "error": (err or "").strip()[:300],
        "mic_label": plan.mic_label,
        "sys_label": plan.sys_label,
        "mic_peak": peak(mic_tap),
        "sys_peak": peak(sys_tap),
    }
    for f in (mic_tap, sys_tap):
        Path(f).unlink(missing_ok=True)
    return result


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


def finalize(plan: RecordingPlan) -> dict:
    """MKV spool -> validated MP4/M4A.

    Recording straight into MP4 loses the whole file if the process is killed
    before the moov atom is written. MKV survives truncation, and the remux is
    a stream copy, so there is no quality cost.
    """
    partial = Path(plan.partial)
    final = Path(plan.final)

    if not partial.exists() or partial.stat().st_size == 0:
        return {"ok": False, "message": "녹음 파일이 만들어지지 않았어.", "path": None}

    info = probe(str(partial))
    if not info:
        return {"ok": False, "message": f"녹음 파일을 읽을 수 없어: {partial}", "path": str(partial)}

    streams = info.get("streams", [])
    if not any(s.get("codec_type") == "audio" for s in streams):
        return {"ok": False, "message": "오디오 트랙이 없어.", "path": str(partial)}
    if plan.with_video and not any(s.get("codec_type") == "video" for s in streams):
        return {"ok": False, "message": "영상 트랙이 없어.", "path": str(partial)}
    duration = float(info.get("format", {}).get("duration") or 0)
    if duration <= 0:
        return {"ok": False, "message": "녹음 길이가 0이야.", "path": str(partial)}

    free = shutil.disk_usage(final.parent).free
    if free < partial.stat().st_size + 256 * 1024**2:
        return {"ok": False,
                "message": f"변환할 공간이 부족해. 원본은 그대로 뒀어: {partial}",
                "path": str(partial), "duration": duration}

    # Keep the real extension last: ffmpeg picks the muxer from it.
    tmp = final.with_name(f"{final.stem}.finalizing{final.suffix}")
    tmp.unlink(missing_ok=True)
    rc, _, err = _run([
        "ffmpeg", "-nostdin", "-n", "-hide_banner", "-loglevel", "error",
        "-i", str(partial), "-map", "0", "-c", "copy",
        "-movflags", "+faststart", str(tmp),
    ], timeout=1800)
    if rc != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        return {"ok": False,
                "message": f"MP4 변환 실패. 원본은 그대로 뒀어: {partial}\n{err.strip()[:200]}",
                "path": str(partial), "duration": duration}

    if not probe(str(tmp)):
        tmp.unlink(missing_ok=True)
        return {"ok": False,
                "message": f"변환 결과 검증 실패. 원본은 그대로 뒀어: {partial}",
                "path": str(partial), "duration": duration}

    tmp.replace(final)
    partial.unlink(missing_ok=True)
    for f in (plan.meter_mic, plan.meter_sys, plan.progress):
        try:
            Path(f).unlink(missing_ok=True)
        except OSError:
            pass  # scratch cleanup must never mask a successful save

    return {"ok": True, "message": "저장 완료", "path": str(final),
            "duration": duration, "size": final.stat().st_size}
