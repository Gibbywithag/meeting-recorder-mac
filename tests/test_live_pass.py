import json
import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

import gemini_pass as gp
import live_pass as lp


def make_recording(path: Path, seconds: int) -> None:
    """Video + mic + call tracks, like a real capture. The call track's pitch rises over time."""
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=5:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=300:duration={seconds}",
                    "-f", "lavfi", "-i", f"aevalsrc='0.3*sin(2*PI*(200+20*t)*t)':d={seconds}",
                    "-map", "0", "-map", "1", "-map", "2", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-movflags", "+frag_keyframe+empty_moov", str(path)], check=True)


@pytest.fixture
def folder(tmp_path):
    (tmp_path / "capture.json").write_text(json.dumps({"microphones": ["default"], "systemAudio": True, "multipleWindows": False}))
    make_recording(tmp_path / "meeting.mov", 33)
    return tmp_path


def answer(n, speaker="Speaker 1"):
    return {"transcript": [{"t": 1.0, "speaker": speaker, "text": f"line in section {n}"}], "summary": f"summary {n}",
            "decisions": [], "proposals": [], "open_questions": [], "action_items": [], "screen_refs": []}


def fakes(calls, fail_once=None):
    state = {"failed": False}
    def analyze(mp4, context):
        n = len(calls)
        calls.append({"seconds": gp.duration_s(mp4), "context": context})
        if fail_once is not None and n == fail_once and not state["failed"]:
            state["failed"] = True
            raise RuntimeError("network dropped")
        return answer(n)
    return analyze, lambda path, secs, out: []


def run(folder, analyze, transcribe, **kw):
    r = lp.LiveRun(folder, analyze=analyze, transcribe=transcribe, segment_s=10, min_tail_s=4, safety_s=0, probe_every_s=0.05, **kw)
    return r


@pytest.mark.parametrize("start,available,finished,expected", [
    (0, 100, False, None),            # not enough yet
    (0, 419, False, None),            # 5 min ready, but keep 2 min back so the tail can't be tiny
    (0, 420, False, (0, 300)),
    (300, 720, False, (300, 300)),
    (0, 100, True, (0, 100)),         # short meeting: the whole thing at Stop
    (300, 700, True, (300, 400)),     # tail up to 7 min goes as one
    (300, 720, True, (300, 300)),     # far behind: keep splitting
    (300, 300.2, True, None),         # nothing left
])
def test_next_segment(start, available, finished, expected):
    assert lp.next_segment(start, available, finished, 300, 120) == expected


def test_sections_cover_the_whole_recording_without_gaps(folder):
    (folder / "live").mkdir(); (folder / "live" / "recording_done").touch()
    calls = []
    r = run(folder, *fakes(calls))
    summary = r.run()
    assert summary.exists()
    assert [s["start"] for s in r.segments] == [0, 10, 20]
    assert abs(sum(c["seconds"] for c in calls) - gp.duration_s(folder / "meeting.mov")) < 0.6
    assert "section 2" in (folder / "transcript.md").read_text()
    # transcript times are shifted by each section's real start
    assert "00:00:21" in (folder / "transcript.md").read_text()
    # large temp media is gone, answers stay
    assert not list((folder / "live").glob("*/meeting.mov")) and not list((folder / "live").glob("*/*.mp4"))
    assert (folder / "live" / "part_000" / "result.json").exists()


def test_processes_sections_while_recording_then_finishes_at_stop(folder):
    calls = []
    r = run(folder, *fakes(calls))
    t = threading.Thread(target=r.run); t.start()
    deadline = time.monotonic() + 30
    while len(calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(calls) == 2, "the first two sections should not wait for Stop"
    time.sleep(0.3)
    assert len(calls) == 2, "the last section must wait for Stop"
    assert (folder / "summary.md").exists(), "notes so far should exist during the meeting"
    (folder / "live" / "recording_done").touch()
    t.join(30)
    assert not t.is_alive() and len(calls) == 3


def test_failed_section_is_retried_in_place(folder):
    (folder / "live").mkdir(); (folder / "live" / "recording_done").touch()
    calls = []
    r = run(folder, *fakes(calls, fail_once=1))
    r.run()
    assert all(s["result"] for s in r.segments) and [s["start"] for s in r.segments] == [0, 10, 20]
    assert len(calls) == 4
    text = (folder / "transcript.md").read_text()
    assert text.index("00:00:01") < text.index("00:00:11") < text.index("00:00:21")


def test_section_that_never_works_fails_the_run_so_the_app_falls_back(folder):
    (folder / "live").mkdir(); (folder / "live" / "recording_done").touch()
    def analyze(mp4, context):
        raise RuntimeError("Gemini down")
    r = run(folder, analyze, lambda *a: [])
    with pytest.raises(RuntimeError, match="not transcribed"):
        r.run()


def test_rewriting_notes_keeps_geminis_original_labels(folder):
    (folder / "live").mkdir(); (folder / "live" / "recording_done").touch()
    r = run(folder, lambda mp4, ctx: answer(0, "Speaker 1"), lambda *a: ["line in section 0"])
    r.run()
    assert all(s["result"]["transcript"][0]["speaker"] == "Speaker 1" for s in r.segments)


def test_app_dying_mid_recording_stops_the_worker(folder):
    r = run(folder, *fakes([]))
    with pytest.raises(RuntimeError, match="app stopped"):
        r.run(parent=os.getppid() + 12345)


def test_empty_recording_fails_instead_of_hanging(tmp_path):
    (tmp_path / "live").mkdir(); (tmp_path / "live" / "recording_done").touch()
    r = run(tmp_path, *fakes([]))
    with pytest.raises(RuntimeError, match="none produced"):
        r.run()
