import os
import json
import pytest
from types import SimpleNamespace
from pathlib import Path
import gemini_pass as gp


@pytest.mark.parametrize("bad_time", [100, -1, float("nan"), float("inf"), True, "60"])
def test_rejects_invalid_transcript_timestamps(bad_time):
    with pytest.raises(ValueError):
        gp.validate_timestamps({"transcript": [{"t": bad_time}]}, 93)


def test_validates_screen_times_and_order():
    gp.validate_timestamps({"transcript": [{"t": 0}, {"t": 60}, {"t": 82}]}, 93)
    with pytest.raises(ValueError):
        gp.validate_timestamps({"screen_refs": [{"t": 122}]}, 93)
    with pytest.raises(ValueError):
        gp.validate_timestamps({"transcript": [{"t": 60}, {"t": 20}]}, 93)


def test_analyzer_retries_invalid_times_before_returning(tmp_path, monkeypatch):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"fixture")
    monkeypatch.setattr(gp, "duration_s", lambda _: 93)
    monkeypatch.setattr(gp.time, "sleep", lambda _: None)
    audio = tmp_path / "clip_audio.m4a"
    audio.write_bytes(b"audio fixture")
    monkeypatch.setattr(gp, "audio_only", lambda _: audio)
    calls, deleted = [], []
    uploaded = SimpleNamespace(name="test", uri="test://clip", mime_type="video/mp4", state=SimpleNamespace(name="ACTIVE"))
    def generate(**kwargs):
        calls.append(kwargs)
        data = sample_result("retry")
        data["transcript"][0]["t"] = 100 if len(calls) == 1 else 1
        return SimpleNamespace(text=json.dumps(data))
    client = SimpleNamespace(
        files=SimpleNamespace(upload=lambda **_: uploaded, delete=lambda **kw: deleted.append(kw)),
        models=SimpleNamespace(generate_content=generate),
    )
    result = gp.analyze_part(client, clip, "gemini-3-flash-preview")
    assert result["transcript"][0]["t"] == 1
    assert len(calls) == 2 and len(deleted) == 4          # audio + video uploaded and deleted per attempt
    assert "Previous response failed validation" in calls[1]["contents"][2]
    assert "AUDIO file's timeline" in calls[0]["contents"][2]
    assert not audio.exists()                              # temporary audio copy cleaned up


def sample_result(offset_label):
    return {
        "transcript": [
            {"t": 1.0, "speaker": "You", "text": f"hello {offset_label}"},
            {"t": 4.5, "speaker": "Client", "text": "make the logo bigger"},
        ],
        "summary": f"Summary {offset_label}.",
        "decisions": ["Use blue", "Ship Friday"],
        "action_items": [{"what": "Resize logo", "owner": "Sam", "due": "2026-10-16"}],
        "screen_refs": [{"t": 4.5, "said": "make the logo bigger", "on_screen": "homepage header"}],
    }


def test_merge_single_part_keeps_timestamps():
    merged = gp.merge_parts([sample_result("a")], [0.0])
    assert merged["transcript"][1]["t"] == 4.5
    assert merged["decisions"] == ["Use blue", "Ship Friday"]


def test_merge_two_parts_offsets_and_dedupes():
    merged = gp.merge_parts([sample_result("a"), sample_result("b")], [0.0, 1800.0])
    assert merged["transcript"][3]["t"] == 1804.5
    assert merged["screen_refs"][1]["t"] == 1804.5
    # identical action item in both chunks collapses to one
    assert len(merged["action_items"]) == 1
    assert merged["decisions"] == ["Use blue", "Ship Friday"]


def test_markdown_writers(tmp_path: Path):
    merged = gp.merge_parts([sample_result("a")], [0.0])
    gp.write_markdown(tmp_path, merged, meta={"started": "2026-10-14 14:30", "seconds": 125})
    t = (tmp_path / "transcript.md").read_text()
    s = (tmp_path / "summary.md").read_text()
    assert "[00:00:04] **Client:** make the logo bigger" in t
    assert "| Resize logo | Sam | 2026-10-16 |" in s
    assert "00:00:04" in s and "homepage header" in s


def test_hms():
    assert gp.hms(0) == "00:00:00"
    assert gp.hms(3725.4) == "01:02:05"


def test_parse_json_tolerates_code_fence():
    raw = "```json\n" + json.dumps(sample_result("x")) + "\n```"
    assert gp.parse_json(raw)["summary"] == "Summary x."


def test_parse_json_tolerates_single_result_array():
    assert gp.parse_json(json.dumps([sample_result("x")]))["summary"] == "Summary x."
    with pytest.raises(ValueError):
        gp.parse_json("[]")


def test_prep_mixes_every_audio_track(tmp_path, monkeypatch):
    commands = []
    def fake_run(cmd):
        commands.append(cmd)
        return "1\n2\n3\n" if cmd[0] == "ffprobe" else ""
    monkeypatch.setattr(gp, "run", fake_run)
    gp.prep_for_ai(tmp_path / "meeting.mov")
    ffmpeg = commands[-1]
    assert ffmpeg[ffmpeg.index("-filter_complex") + 1] == "[0:a:0][0:a:1][0:a:2]amix=inputs=3:duration=longest:normalize=0[a]"


def test_folder_uses_actual_chunk_duration_and_carries_context(tmp_path, monkeypatch):
    (tmp_path / "meeting.mov").write_bytes(b"fixture")
    parts = [tmp_path / "part_000.mp4", tmp_path / "part_001.mp4"]
    mp4 = tmp_path / "meeting_for_ai.mp4"
    monkeypatch.setattr(gp, "prep_for_ai", lambda _mov, _analysis=None, _local=None: mp4)
    monkeypatch.setattr(gp, "duration_s", lambda p: 3605 if p == mp4 else 1802.5)
    monkeypatch.setattr(gp, "chunk", lambda _: parts)
    monkeypatch.setattr(gp, "gemini_client", lambda: object())
    contexts = []
    def analyze(client, path, model, context=""):
        contexts.append(context)
        return sample_result(path.stem)
    monkeypatch.setattr(gp, "analyze_part", analyze)
    gp.process_folder(tmp_path)
    transcript = (tmp_path / "transcript.md").read_text()
    assert "[00:30:07] **Client:**" in transcript
    assert contexts[0] == ""
    assert "Summary part_000" not in contexts[1]
    assert "hello part_000" not in contexts[1]
    assert "You" in contexts[1]
    assert "Timestamps must start at zero" in contexts[1]


def test_proposals_and_questions_survive_merge_and_render(tmp_path):
    data = sample_result("proposal")
    data["proposals"] = ["Consider a calendar; not agreed."]
    data["open_questions"] = ["Who will own the calendar?"]
    merged = gp.merge_parts([data, data], [0, 100])
    assert merged["proposals"] == data["proposals"]
    assert merged["open_questions"] == data["open_questions"]
    gp.write_markdown(tmp_path, merged, {"seconds": 200})
    summary = (tmp_path / "summary.md").read_text()
    assert "## Proposals — not yet agreed" in summary
    assert "Who will own the calendar?" in summary
    assert "## Screen references" in summary


def test_long_meeting_splits_into_shorter_clips(tmp_path, monkeypatch):
    monkeypatch.setattr(gp, "duration_s", lambda _: 1200)
    calls = []
    monkeypatch.setattr(gp, "run", lambda cmd: calls.append(cmd))
    gp.chunk(tmp_path / "meeting_for_ai.mp4")
    assert calls
    assert calls[0][calls[0].index("-segment_time") + 1] == "600"


def test_small_overshoot_at_clip_end_is_clamped_not_fatal():
    result = {"transcript": [{"t": 600.0}, {"t": 630}], "screen_refs": [{"t": 625}]}
    gp.validate_timestamps(result, 620.422)
    assert result["transcript"][1]["t"] == 620.422 and result["screen_refs"][0]["t"] == 620.422


def test_large_overshoot_still_rejected():
    with pytest.raises(ValueError):
        gp.validate_timestamps({"transcript": [{"t": 640}]}, 620.422)   # 3% over = real timing error


def test_relabel_rebuilds_from_saved_answers_without_gemini(tmp_path, monkeypatch):
    (tmp_path / "meeting.mov").write_bytes(b"mov")
    (tmp_path / "meeting_for_ai.mp4").write_bytes(b"mp4")
    (tmp_path / "gemini_raw_meeting_for_ai.json").write_text(json.dumps(sample_result("saved")))
    monkeypatch.setattr(gp, "duration_s", lambda _: 93)
    monkeypatch.setattr(gp.speakers, "analyze_tracks", lambda folder, media: {"roles": [], "frames": [], "note": "no tracks"})
    monkeypatch.setattr(gp, "gemini_client", lambda: (_ for _ in ()).throw(AssertionError("must not call Gemini")))
    out = gp.relabel_folder(tmp_path)
    assert "make the logo bigger" in (tmp_path / "transcript.md").read_text()
    assert out.name == "summary.md"


def test_relabel_without_saved_answers_says_so(tmp_path):
    (tmp_path / "meeting.mov").write_bytes(b"mov")
    with pytest.raises(FileNotFoundError):
        gp.relabel_folder(tmp_path)


def test_short_final_chunk_tail_overshoot_is_allowed():
    result = {"transcript": [{"t": 0}, {"t": 129}]}
    gp.validate_timestamps(result, 123.423)            # 4.5% over on a 2-minute last chunk
    assert result["transcript"][1]["t"] == 123.423


def test_rescale_last_resort_keeps_order_and_rejects_wild_values():
    result = {"transcript": [{"t": 0}, {"t": 600}, {"t": 690}], "screen_refs": [{"t": 300}]}
    assert gp.rescale_into_clip(result, 620.0)
    assert result["transcript"][-1]["t"] == 620.0 and result["screen_refs"][0]["t"] < 300
    assert not gp.rescale_into_clip({"transcript": [{"t": 1200}]}, 620.0)   # 93% over: real error, not drift


def test_chunks_are_equal_length_without_a_short_tail(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(gp, "duration_s", lambda _: 3156.2)
    monkeypatch.setattr(gp, "run", lambda cmd: calls.append(cmd) or "")
    gp.chunk(tmp_path / "meeting_for_ai.mp4")
    seg = int(calls[0][calls[0].index("-segment_time") + 1])
    assert seg == 527 and 3156.2 - 5 * seg > 400       # 6 parts of ~9 min, last one not a stub


def test_empty_notes_dropped_but_transcript_untouched():
    r = {"decisions": ["Ship it", ""], "proposals": ["  "], "open_questions": [], "transcript": [{"t": 0, "speaker": "A", "text": ""}]}
    gp.drop_empty_notes(r)
    assert r["decisions"] == ["Ship it"] and r["proposals"] == [] and r["transcript"][0]["text"] == ""


def test_rescale_never_applies_to_short_clips():
    assert not gp.rescale_into_clip({"transcript": [{"t": 100}]}, 93)   # keep the MMSS guard
    r = {"transcript": [{"t": 10}, {"t": 162}]}
    assert gp.rescale_into_clip(r, 123.423) and r["transcript"][1]["t"] == 123.42


def test_degenerate_mic_transcript_is_discarded():
    assert gp.looks_degenerate(["Okay."] * 86)
    assert not gp.looks_degenerate(["Okay.", "Yeah.", "That's fire, I like that."] * 4)
    assert not gp.looks_degenerate(["Okay."] * 5)


def test_identity_note_names_the_recorder(monkeypatch):
    monkeypatch.setenv("LOCAL_SPEAKER_NAMES", "Sam, Sammy")
    note = gp.identity_note()
    assert '"Sam"' in note and "NOT Sam" in note
    monkeypatch.setenv("LOCAL_SPEAKER_NAMES", "")
    assert gp.identity_note() == ""


def test_load_env_reads_the_installed_apps_settings_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("GEMINI_API_KEY=from-app-settings\nLOCAL_SPEAKER_NAMES=Sam\n")
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "LOCAL_SPEAKER_NAMES", "GEMINI_KEY_FALLBACK_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MR_ENV_FILE", str(env))
    gp.load_env()
    assert os.environ["GEMINI_API_KEY"] == "from-app-settings"
    assert gp.local_names() == ["Sam"]


def test_load_env_missing_settings_file_does_not_crash(tmp_path, monkeypatch):
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GEMINI_KEY_FALLBACK_FILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("MR_ENV_FILE", str(tmp_path / "nope.env"))
    gp.load_env()
    assert not os.environ.get("GEMINI_API_KEY")
