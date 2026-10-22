"""finalize() on real files made with ffmpeg (skipped without ffmpeg)."""
import os
import shutil
import signal
import subprocess
import time

import pytest

from meetrec import core

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                                reason="needs ffmpeg")

AUDIO = ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
VIDEO = ["-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30"]


def spool(path, seconds=2.0, video=None, kill_after=None):
    """Write a .partial.mkv the way the recorder does (same muxer flags)."""
    argv = ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error"]
    if video:
        argv += ["-re", *VIDEO]
    argv += (["-re"] if kill_after else []) + AUDIO
    if video:
        argv += ["-map", "0:v", "-map", "1:a", "-c:v", video, "-pix_fmt", "yuv420p"]
    argv += ["-c:a", "aac", "-cluster_time_limit", "1000", "-flush_packets", "1"]
    if kill_after:
        p = subprocess.Popen(argv + [str(path)])
        time.sleep(kill_after)
        p.send_signal(signal.SIGKILL)
        p.wait()
    else:
        subprocess.run(argv + ["-t", str(seconds), str(path)], check=True)


def plan_for(tmp_path, video=False):
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    return core.RecordingPlan(
        session="s1", mic_name="m", mic_label="M", sys_name="s", sys_label="S", mic_gain=1.0,
        partial=str(tmp_path / "rec.partial.mkv"),
        final=str(tmp_path / ("rec.mp4" if video else "rec.m4a")),
        with_video=video, monitor=None, encoder=None, display=":0", scratch=str(scratch))


def test_audio_spool_becomes_m4a(tmp_path):
    plan = plan_for(tmp_path)
    spool(plan.partial)
    res = core.finalize(plan)
    assert res["ok"], res
    assert res["path"] == plan.final and abs(res["duration"] - 2.0) < 0.2
    assert not os.path.exists(plan.partial) and not os.path.exists(plan.scratch)
    assert os.stat(plan.final).st_mode & 0o777 == 0o600


def test_video_spool_becomes_mp4(tmp_path):
    plan = plan_for(tmp_path, video=True)
    spool(plan.partial, video="libx264")
    res = core.finalize(plan)
    assert res["ok"], res
    assert core._streams(core.probe(res["path"])) == {"video": "h264", "audio": "aac"}


def test_killed_recording_is_recovered(tmp_path):
    """SIGKILL leaves no duration in the header; what reached the disk is kept."""
    plan = plan_for(tmp_path)
    spool(plan.partial, kill_after=3.0)
    info = core.probe(plan.partial)
    assert core._duration(info) == 0       # nothing in the header
    res = core.finalize(plan)
    assert res["ok"], res
    assert res["duration"] > 1.5


def test_existing_file_is_not_overwritten(tmp_path):
    plan = plan_for(tmp_path)
    spool(plan.partial)
    with open(plan.final, "w") as f:
        f.write("someone else's file")
    res = core.finalize(plan)
    assert res["ok"] and res["path"].endswith("rec-2.m4a")
    assert open(plan.final).read() == "someone else's file"


def test_missing_video_keeps_the_spool(tmp_path):
    plan = plan_for(tmp_path, video=True)
    spool(plan.partial)                      # audio only, but the plan wants video
    res = core.finalize(plan)
    assert not res["ok"] and os.path.exists(plan.partial)


def test_empty_spool(tmp_path):
    plan = plan_for(tmp_path)
    open(plan.partial, "w").close()
    assert not core.finalize(plan)["ok"]


def has_encoder(name):
    out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True,
                         text=True).stdout
    return f" {name} " in out


def test_hevc_gets_the_hvc1_tag(tmp_path):
    if not has_encoder("libx265"):
        pytest.skip("needs libx265")
    plan = plan_for(tmp_path, video=True)
    spool(plan.partial, video="libx265")
    res = core.finalize(plan)
    assert res["ok"], res
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v",
                          "-show_entries", "stream=codec_tag_string", "-of", "csv=p=0",
                          res["path"]], capture_output=True, text=True).stdout.strip()
    assert out == "hvc1"


def test_remux_never_makes_a_world_readable_file(tmp_path):
    old = os.umask(0o022)
    try:
        rc, _, _ = core._run(["touch", str(tmp_path / "f")], umask=0o077)
    finally:
        os.umask(old)
    assert rc == 0 and os.stat(tmp_path / "f").st_mode & 0o077 == 0


def test_short_result_keeps_the_spool(tmp_path):
    """If the output is clearly shorter than what ffmpeg reported, keep the raw file."""
    plan = plan_for(tmp_path)
    spool(plan.partial, kill_after=2.5)
    with open(plan.progress, "w") as f:
        f.write("out_time_us=30000000\nprogress=continue\n")
    res = core.finalize(plan)
    assert res["ok"] and os.path.exists(res["path"])
    assert os.path.exists(plan.partial) and plan.partial in res["message"]


def test_publish_takes_the_next_free_name(tmp_path):
    tmp = tmp_path / ".x.finalizing.m4a"
    tmp.write_text("new")
    final = tmp_path / "rec.m4a"
    final.write_text("old")
    (tmp_path / "rec-2.m4a").write_text("older")
    out = core._publish(tmp, final)
    assert out.name == "rec-3.m4a" and out.read_text() == "new"
    assert final.read_text() == "old" and not tmp.exists()


def test_safe_finalize_reports_the_raw_file(tmp_path, monkeypatch):
    plan = plan_for(tmp_path)
    spool(plan.partial)
    monkeypatch.setattr(core, "_publish", lambda *a: (_ for _ in ()).throw(PermissionError("nope")))
    res = core.safe_finalize(plan)
    assert not res["ok"] and plan.partial in res["message"] and os.path.exists(plan.partial)
