"""Client-video mode: frame picking, validation, context lookup, output files."""
import json
from pathlib import Path
import video_pass as vp


def test_frame_times_parsed_from_showinfo():
    err = "[Parsed_showinfo_1] n:   0 pts:      0 pts_time:0  fmt\n[Parsed_showinfo_1] n:   1 pts: 512 pts_time:3.4 fmt\n"
    assert vp.parse_frame_times(err) == [0.0, 3.4]


def test_thinning_keeps_first_last_and_cap():
    times = [float(i) for i in range(200)]
    idx = vp.thin_times(times, 60)
    assert len(idx) == 60 and idx[0] == 0 and idx[-1] == 199
    assert vp.thin_times(times[:10], 60) == list(range(10))


def test_frame_numbers_validated():
    assert vp.resolve_frame({"frame": 3}, 5) == 3
    assert vp.resolve_frame({"frame": 0}, 5) is None
    assert vp.resolve_frame({"frame": 9}, 5) is None
    assert vp.resolve_frame({"frame": True}, 5) is None
    assert vp.resolve_frame({}, 5) is None


def test_context_is_latest_recorded_meeting_not_a_video(tmp_path):
    old, new, video = tmp_path / "a", tmp_path / "b", tmp_path / "c_video"
    for d in (old, new, video):
        d.mkdir(); (d / "summary.md").write_text(d.name)
    (old / "capture.json").write_text("{}"); (new / "capture.json").write_text("{}")
    (video / "capture.json").write_text("{}"); (video / "client_video.json").write_text("{}")
    import os, time
    os.utime(old / "summary.md", (time.time() - 100, time.time() - 100))
    assert vp.latest_meeting_summary(tmp_path) == new / "summary.md"
    assert vp.latest_meeting_summary(tmp_path / "missing") is None


def test_outputs_use_exact_frame_times_and_skip_bad_frames(tmp_path):
    frames = [{"n": 1, "t": 0.0, "file": "frame_001_00m00s.jpg"}, {"n": 2, "t": 37.4, "file": "frame_002_00m37s.jpg"}]
    result = {"summary": "Wants a P&L calendar.", "transcript": [{"t": 999, "speaker": "Client", "text": "add a PnL book"}],
              "requests": [{"frame": 2, "quote": "I want PnL book to be a button", "screen": "Signals page", "current": "toggle only",
                            "wanted": "PnL book and PnL calendar buttons", "priority": "high"},
                           {"frame": 77, "quote": "", "wanted": "Bigger logo"}],
              "conflicts": [{"earlier": "toggle for reading list", "now": "button instead"}], "questions": ["Weekly or monthly view?"],
              "action_items": [{"what": "build PnL calendar", "owner": "You", "due": "unspecified"}]}
    vp.write_outputs(tmp_path, result, frames, {"name": "walkthrough.mp4", "seconds": 61, "when": "now", "context": "2026-10-14_meeting"})
    s = (tmp_path / "summary.md").read_text()
    assert "![frame_002_00m37s.jpg](frames/frame_002_00m37s.jpg)" in s and "at 00:37 in the video" in s
    assert "### 2. Bigger logo" in s and "frame_077" not in s           # invalid frame: request kept, no screenshot
    assert "Weekly or monthly view?" in s and "button instead" in s and "2026-10-14_meeting" in s
    assert "add a PnL book" in (tmp_path / "transcript.md").read_text()


def test_empty_video_result_still_writes_files(tmp_path):
    vp.write_outputs(tmp_path, {"summary": "", "requests": [], "transcript": []}, [], {"name": "x", "seconds": 0, "when": "now"})
    assert "(none found)" in (tmp_path / "summary.md").read_text() and "(no speech)" in (tmp_path / "transcript.md").read_text()


def test_silent_video_has_no_quotes_or_transcript(tmp_path):
    r = vp.silence_result({"transcript": [{"t": 0, "text": "invented narration"}],
                           "requests": [{"frame": 1, "quote": "invented", "wanted": "weekly summary page"}]})
    assert r["transcript"] == [] and r["requests"][0]["quote"] == ""
    vp.write_outputs(tmp_path, r, [{"n": 1, "t": 0.0, "file": "f.jpg"}], {"name": "x", "seconds": 10, "when": "now", "speech": False})
    s = (tmp_path / "summary.md").read_text()
    assert "no speech" in s and "They said" not in s and "invented" not in s


def test_short_clips_get_denser_screenshots():
    assert vp.frame_gap(10) == 1.0 and vp.frame_gap(60) == 2.0 and vp.frame_gap(600) == 3.0
