"""Speaker attribution: track measurement + word matching against the mic-only transcript."""
import json
from pathlib import Path
import speakers as sp


def test_classify_call_active_is_remote_even_with_mic_bleed():
    mic = [-50.0] * 10 + [-62.0] * 10 + [-75.0] * 10
    call = [-30.0] * 10 + [-74.0] * 10 + [-75.0] * 10
    f = sp.classify_frames(mic, call)
    assert f[:10] == ["remote"] * 10
    assert f[10:20] == ["local"] * 10        # faint mic while the call is quiet = you
    assert f[20:] == ["quiet"] * 10


def test_track_roles_from_capture_json(tmp_path):
    (tmp_path / "capture.json").write_text(json.dumps({"microphones": ["default", "usb"], "systemAudio": True}))
    assert sp.track_roles(tmp_path, 3) == ["mic", "mic", "system"]
    assert sp.track_roles(tmp_path / "missing", 2) == ["mic", "system"]   # old recordings


def test_apply_mapping_renames_everywhere_without_partial_matches():
    merged = {"transcript": [{"t": 0, "speaker": "Speaker 1", "text": "hi"}, {"t": 1, "speaker": "Speaker 10", "text": "yo"}],
              "summary": "Speaker 1 asked Speaker 10 for a logo.", "decisions": ["Speaker 1 approved"],
              "proposals": [], "open_questions": [], "screen_refs": [],
              "action_items": [{"what": "send logo", "owner": "Speaker 1", "due": "Friday"}]}
    sp.apply_mapping(merged, {"Speaker 1": "Client", "Speaker 10": "You"})
    assert [t["speaker"] for t in merged["transcript"]] == ["Client", "You"]
    assert merged["summary"] == "Client asked You for a logo."
    assert merged["action_items"][0]["owner"] == "Client"


def _merged(turns):
    return {"transcript": [dict(t) for t in turns], "summary": "Speaker 2 asked about lives.",
            "decisions": [], "proposals": [], "open_questions": [], "screen_refs": [],
            "action_items": [{"what": "test it", "owner": "Speaker 2", "due": "unspecified"}]}


def attribute(folder, result, local, names=None):
    """One-chunk helper: run attribute_parts on a single result and return it with its note."""
    note = sp.attribute_parts(folder, [result], None if local is None else {0: local[0]} if local else {0: []}, names=names)
    result["speaker_note"] = note
    return result


# Real lines from a recorded test call (chunk 3). Mic-only transcript = what Sam's track heard.
CALL = [
    {"t": 0, "speaker": "Speaker 1", "text": "Yeah, or something within this right here. You see this here Sammy? Because this is what I can see on mobile too."},
    {"t": 9, "speaker": "Speaker 2", "text": "Like a countdown to the live?"},
    {"t": 12, "speaker": "Speaker 1", "text": "No, not a countdown, but something that pops up here. Kind of like how it shows sharing here."},
    {"t": 30, "speaker": "Speaker 2", "text": "Cuz I remember the indicator shows when it's live, like when they're live live."},
    {"t": 36, "speaker": "Speaker 1", "text": "So you see how we have the toggle button here for show my reading list?"},
    {"t": 44, "speaker": "Speaker 2", "text": "Yeah."},
]
MIC_ONLY = ["Well, ain't the live, it shows, right?", "Cause I remember the indicator shows when they're live and that's when they're live live.", "Yeah."]


def test_lines_on_your_mic_track_are_you_and_the_other_voice_is_client():
    out = attribute(Path("/tmp"), _merged(CALL), {0: MIC_ONLY})
    assert [t["speaker"] for t in out["transcript"]] == ["Client", "You", "Client", "You", "Client", "You"]
    # "countdown" was not on the mic track (muted during call audio) but is Sam's voice: follows the voice label
    assert out["action_items"][0]["owner"] == "You" and out["summary"].startswith("You asked")
    assert "Your lines (You" in out["speaker_note"]


def test_mixed_voice_label_is_split_by_words():
    # Gemini filed one of Sam's lines under the client's voice; its words are on the mic track.
    turns = [dict(CALL[0]), {"t": 5, "speaker": "Speaker 1", "text": "Cause I remember the indicator shows when they're live"},
             dict(CALL[2])]
    out = attribute(Path("/tmp"), _merged(turns), {0: MIC_ONLY})
    assert [t["speaker"] for t in out["transcript"]] == ["Client", "You", "Client"]


def test_no_mic_matches_leaves_labels_and_says_so(tmp_path):
    out = attribute(tmp_path, _merged(CALL[:3]), {0: ["completely different words here"]})
    names = {t["speaker"] for t in out["transcript"]}
    assert "You" not in names                         # never guesses who you are
    assert json.loads((tmp_path / "speakers.json").read_text())["parts"][0]["mapping"].get("Speaker 2") != "You"


def test_without_tracks_nothing_is_renamed(tmp_path):
    out = attribute(tmp_path, _merged(CALL[:2]), None)
    assert [t["speaker"] for t in out["transcript"]] == ["Speaker 1", "Speaker 2"]
    assert "no separate mic track" in out["speaker_note"]


def test_lone_words_never_count_as_a_match():
    turns = [{"speaker": "A", "text": "yeah so the thing is that we really need the calendar"}, {"speaker": "B", "text": "Yeah."}]
    assert sp.matched_words(turns, ["yeah"]) == [0, 0]
    assert sp.matched_words(turns, ["we really need it"]) == [3, 0]


def test_group_meeting_keeps_real_names_and_never_says_client():
    turns = [{"speaker": "Speaker 1", "text": "hello there friend"}, {"speaker": "Sam", "text": "w " * 40},
             {"speaker": "Pat Rivera", "text": "z " * 90}, {"speaker": "Speaker 3", "text": "y " * 30}]
    mapping, _ = sp.decide_by_text(turns, sp.matched_words(turns, ["hello there friend"]), names=["Sam"])
    assert mapping == {"Speaker 1": "You", "Pat Rivera": "Pat Rivera", "Sam": "Unidentified"}
    assert "Speaker 3" not in mapping          # generic label kept as is in a group


def test_one_on_one_generic_other_voice_is_client():
    turns = [{"speaker": "Speaker 1", "text": "hello there friend"}, {"speaker": "Speaker 2", "text": "w " * 40}]
    mapping, _ = sp.decide_by_text(turns, sp.matched_words(turns, ["hello there friend"]))
    assert mapping == {"Speaker 1": "You", "Speaker 2": "Client"}


def test_in_person_meeting_uses_amount_of_speech_not_gemini_name():
    # A recorded test call: Gemini put "Sam" on the client's 1,118-word monologue;
    # the mic-only track heard ~20 words (short replies). The small voice is you.
    turns = [{"speaker": "Sam", "text": "w " * 1118}, {"speaker": "Speaker 1", "text": "okay yeah mhm sure " * 20}]
    mic_only = ["Okay.", "Mhm.", "Yeah, for sure.", "See how she's working?", "Okay.", "Yes."] * 2
    out = attribute(Path("/tmp"), _merged(turns), {0: mic_only}, names=["Sam", "Sammy"])
    assert [t["speaker"] for t in out["transcript"]] == ["Client", "You"]


def test_remote_only_audio_never_produces_you():
    # Live test: only the client's voice played. Mic-only track silent -> nobody is You.
    turns = [{"speaker": "Sam", "text": "w " * 160}, {"speaker": "Speaker 2", "text": "Oh, yeah."}]
    out = attribute(Path("/tmp"), _merged(turns), {0: []}, names=["Sam"])
    assert "You" not in {t["speaker"] for t in out["transcript"]}
    # Two other voices, and Gemini's "Sam" can't be you (mic silent): it isn't trusted as a name.
    assert out["transcript"][0]["speaker"] == "Unidentified"


def test_call_threshold_holds_when_remote_talks_nonstop():
    call = [-28.0] * 95 + [-80.0] * 5            # remote talks 95% of the time
    mic = [-48.0] * 95 + [-75.0] * 5             # mic hears only bleed
    assert sp.classify_frames(mic, call).count("local") == 0


def test_strong_track_evidence_beats_a_wrong_name_label():
    turns = [{"speaker": "Sam", "text": "You see this here Sammy? Because this is what I can see on mobile too."},
             {"speaker": "Speaker 2", "text": "Cause I remember the indicator shows when they're live live."}]
    out = attribute(Path("/tmp"), _merged(turns), {0: MIC_ONLY}, names=["Sam"])
    assert [t["speaker"] for t in out["transcript"]] == ["Client", "You"]



def test_owners_are_renamed_per_chunk_even_when_gemini_swaps_labels():
    # Chunk 1: Gemini calls the client "Sam". Chunk 2: Gemini calls you "Sam".
    c1 = {"transcript": [{"speaker": "Sam", "text": "w " * 300}, {"speaker": "Speaker 1", "text": "sounds good to me then"}],
          "summary": "", "decisions": [], "proposals": [], "open_questions": [], "screen_refs": [],
          "action_items": [{"what": "open a bank account", "owner": "Sam", "due": "unspecified"}]}
    c2 = {"transcript": [{"speaker": "Speaker 1", "text": "z " * 300}, {"speaker": "Sam", "text": "I will send the mockup tonight"}],
          "summary": "", "decisions": [], "proposals": [], "open_questions": [], "screen_refs": [],
          "action_items": [{"what": "send the mockup", "owner": "Sam", "due": "tonight"}]}
    sp.attribute_parts(Path("/tmp"), [c1, c2], {0: ["sounds good to me then"], 1: ["I will send the mockup tonight"]},
                       names=["Sam"])
    assert c1["action_items"][0]["owner"] == "Client"
    assert c2["action_items"][0]["owner"] == "You"


def test_task_text_renames_generic_labels_but_not_names():
    m = {"transcript": [], "summary": "", "decisions": [], "proposals": [], "open_questions": [], "screen_refs": [],
         "action_items": [{"what": "Speaker 1 to send a mockup to Sam", "owner": "Speaker 1", "due": "x"}]}
    sp.apply_mapping(m, {"Speaker 1": "Client", "Sam": "You"})
    assert m["action_items"][0] == {"what": "Client to send a mockup to Sam", "owner": "Client", "due": "x"}
