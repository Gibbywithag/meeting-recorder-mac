import re
from pathlib import Path

import pytest

import report_pdf as rp

NOTES = """# Meeting summary — 2026-10-29 10:03

Duration: 00:18:16

_Your lines (You, 40% of words) were identified from your separate mic track._

## Summary

You and Dena review the Acmee app.

## Decisions

- (none captured)

## Action items

| What | Owner | Due |
|---|---|---|
| Fix the date format. | You | unspecified |
| Fix the dates | Unidentified | Friday |

## Screen references

- (none)
"""
TRANSCRIPT = """# Transcript — 2026-10-29 10:03

[00:00:01] **You:** Thanks Sam, I will fix the Acmee dates.
[00:00:05] **Dena:** Actually Riley, is that done?
[00:00:09] **Sam:** Not yet.
"""


@pytest.fixture
def folder(tmp_path, monkeypatch):
    d = tmp_path / "meeting"; d.mkdir()
    (d / "summary.md").write_text(NOTES); (d / "transcript.md").write_text(TRANSCRIPT)
    monkeypatch.setenv("LOCAL_SPEAKER_NAMES", "Sam,Samuel")
    monkeypatch.setenv("REPORT_NAME", "Samuel")
    monkeypatch.setenv("REPORT_FIXES", "Acmee=Acme; Dena=Dana")
    monkeypatch.setattr(rp, "print_pdf", lambda page, out: (out.write_text(page), out)[1])
    return d


def good_answer(lines, body, me, nicknames):
    return {"title": "Acmee app review", "participants": ["Riley", "Dena", "Samuel"],
            "speakers": [{"from": 0, "to": 0, "name": "Riley"}],
            "note_edits": [{"find": "You and", "replace": "Riley and"}],
            "actions": [{"owner": "Riley", "what": "Fix the date format", "due": "Friday"}]}


def test_read_and_parse():
    assert [l["speaker"] for l in rp.read_transcript(TRANSCRIPT)] == ["You", "Dena", "Sam"]
    assert rp.parse_actions(NOTES)[0] == {"what": "Fix the date format", "owner": "You", "due": "unspecified"}
    assert "Your lines" not in rp.split_notes(NOTES)["body"]


def test_finished_document_has_people_first_and_right_names(folder):
    copy = rp.build(folder, polisher=good_answer)
    page = (folder / "Meeting Notes.pdf").read_text()
    assert copy.name == "Meeting Notes - 2026-10-29 1003.pdf" and copy.parent == folder.parent and copy.exists()
    assert page.index("Who does what") < page.index(">Summary<") < page.index("Full transcript")
    assert '<span class="sp">Riley</span> Thanks Samuel, I will fix the Acme dates.' in page
    assert '<span class="sp">Samuel</span> Not yet.' in page     # Gemini's nickname label becomes the real name
    word = lambda w: re.search(rf"\b{w}\b", page)
    assert word("Dana") and not word("Dena") and not word("Sam") and not word("Acmee")
    assert "Your lines" not in page and "unspecified" not in page and "No action items assigned" in page


def test_spoken_words_can_never_change(folder):
    lines = rp.read_transcript(TRANSCRIPT)
    answer = {"speakers": [{"from": 0, "to": 0, "name": "Riley"}, {"from": 99, "to": 120, "name": "Riley"}, {"from": 1, "to": 1, "name": "Speaker 3"}, {"from": 2, "to": 2, "name": "Nobody New"}],
              "participants": ["Riley"], "transcript": ["invented"]}
    new, _, _, info = rp.apply_polish(lines, "x", [], answer)
    assert [l["text"] for l in new] == [l["text"] for l in lines]
    assert [l["speaker"] for l in new] == ["Riley", "Dena", "Sam"] and info["changed"] == 1


def test_unnamed_lines_can_never_be_given_to_the_recorder(monkeypatch):
    monkeypatch.setenv("LOCAL_SPEAKER_NAMES", "Samuel")
    lines = [{"t": "0", "speaker": s, "text": "x"} for s in ["Unidentified", "You", "Speaker 2"]]
    new, _, _, _ = rp.apply_polish(lines, "x", [], {"participants": ["Samuel"], "speakers": [{"from": 0, "to": 2, "name": "Samuel"}]})
    assert [l["speaker"] for l in new] == ["Unidentified", "Samuel", "Speaker 2"]


def test_a_stretch_fills_unnamed_lines_but_keeps_other_peoples_names(monkeypatch):
    monkeypatch.setenv("LOCAL_SPEAKER_NAMES", "Sam")
    lines = [{"t": "0", "speaker": s, "text": "x"} for s in ["Unidentified", "Dana", "You", "Speaker 2", "Sam"]]
    new, _, _, _ = rp.apply_polish(lines, "x", [], {"participants": ["Riley", "Dana"], "speakers": [{"from": 0, "to": 4, "name": "Riley"}]})
    assert [l["speaker"] for l in new] == ["Riley", "Dana", "Riley", "Riley", "Riley"]
    new, _, _, _ = rp.apply_polish(lines, "x", [], {"participants": ["Riley", "Dana"], "speakers": [{"from": 1, "to": 1, "name": "Riley"}]})
    assert new[1]["speaker"] == "Riley"      # one line, stated outright, may overrule


def test_note_edits_can_only_swap_a_label_for_a_name():
    body = "You proposes a fix. Unidentified agrees to pay $500."
    good = {"find": "You proposes", "replace": "Riley proposes"}
    rephrase = {"find": "Unidentified agrees to pay $500", "replace": "Riley refuses to pay anything"}
    no_label = {"find": "a fix", "replace": "no fix"}
    missing = {"find": "You said nothing", "replace": "Riley said nothing"}
    out, applied, skipped = rp.apply_note_edits(body, [good, rephrase, no_label, missing, "junk"])
    assert out == "Riley proposes a fix. Unidentified agrees to pay $500." and applied == 1 and skipped == 4


def test_invented_tasks_are_rejected():
    actions = [{"owner": "A", "what": "one", "due": ""}]
    _, _, a, info = rp.apply_polish([], "x", actions, {"actions": [{"owner": "A", "what": "x", "due": ""}, {"owner": "B", "what": "new", "due": ""}]})
    assert a == actions and len(info["rejected"]) == 1


def test_gemini_failing_still_makes_the_pdf(folder):
    def boom(*a):
        raise RuntimeError("down")
    rp.build(folder, polisher=boom)
    page = (folder / "Meeting Notes.pdf").read_text()
    assert '<span class="sp">Samuel</span> Thanks Samuel' in page and "Who does what" in page


def test_safe_build_never_raises(tmp_path):
    rp.safe_build(tmp_path / "does-not-exist")


def test_real_pdf_prints(tmp_path):
    out = rp.print_pdf("<h1>Hi</h1>", tmp_path / "a b.pdf")
    assert out.read_bytes().startswith(b"%PDF")
