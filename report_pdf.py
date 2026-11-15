"""Final step of every meeting: one finished, send-ready PDF.

Page 1 says who does what; then the summary and the full transcript. A short text-only Gemini pass fixes
speaker labels from what people say ("Actually John, ...") because the mic also hears anyone sitting next
to you. It can only change labels and name spellings in the notes: the words spoken are never edited, and
any answer that fails the checks here is ignored, leaving the labels as they were.

Settings (.env): REPORT_NAME=Sam   REPORT_FIXES=Acmee=Acme;Jon=John   (misheard=correct; separated by ;)
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import gemini_pass as gp

log = logging.getLogger("report_pdf")

CHROME = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
          "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
          "/Applications/Chromium.app/Contents/MacOS/Chromium"]
# Measured on a 28-minute meeting: LOW 7 s but mislabels nearly everything; HIGH times out; MEDIUM ~3 min.
THINKING = os.environ.get("REPORT_THINKING", "MEDIUM")
LINE = re.compile(r"^\[(\d\d:\d\d:\d\d)\] \*\*(.+?):\*\* (.*)$")
GENERIC = re.compile(r"^(you|unidentified|unknown|client|other caller \d+|speaker ?\d*)$", re.I)

POLISH_PROMPT = """You are checking the speaker labels on a meeting transcript before it is sent out.
The person who recorded it is {me} (also called {nicknames}). Their microphone also picks up anyone in the room,
so lines labeled "You" may really be someone else, and lines labeled "Unidentified" or "Speaker N" need a name.
Lines labeled "Unidentified" or "Speaker N" were NOT heard on {me}'s microphone track, so they are never {me}: give them
to whichever other person fits. Lines labeled "You" were heard on that microphone and may be {me} or someone beside them.

Work out who said each line from evidence in the conversation: people addressing each other by name
("Actually John, ..."), people referring to someone in the third person (a speaker who says "John and I" is not
John), who owns what ("the flow I shared with you"), and who answers a question put to a named person.
Someone never refers to themselves by name, so a line saying "this is for Gil too" is not spoken by Gil.
Before answering, go through the transcript in order and decide who holds the floor in each stretch: one voice
usually keeps talking across many lines, so a clear clue on one line settles its neighbours in that stretch.
Check every line labeled {me}: if its words mention {me} or any of those nicknames as someone else ("Gil and I"),
then {me} did not say it, and neither did the rest of that stretch.
Change a label only when the evidence supports it; if you cannot tell, leave the line out.
Write names the way people say them in the conversation, first name only, never adding a surname, and always
write the recorder's name exactly as {me}.

Return ONLY JSON:
{{"title": "<short topic of the meeting, 3-7 words>",
 "participants": ["<each real person who spoke>"],
 "speakers": [{{"from": <first line number>, "to": <last line number>, "name": "<who holds the floor>"}}],
 "note_edits": [{{"find": "<exact text from the NOTES containing You/Unidentified/Speaker N, just long enough to be unique>", "replace": "<the same text with only that label changed to the person's name>"}}],
 "actions": [{{"owner": "<person>", "what": "<task>", "due": "<date or unspecified>"}}]}}
"speakers" is a SHORT list of stretches, not one entry per line: a stretch may span lines by other people, which
keep their own labels when they already have a real name. Use from = to to correct one line that has a real but wrong name.
"actions" is the action items from the NOTES with the right owner. Merge items that are the same task. Never add a task.

TRANSCRIPT (line number|label|words):
{lines}

NOTES:
{notes}
"""


# ---------- settings ----------

def my_name() -> str:
    names = gp.local_names()
    return os.environ.get("REPORT_NAME", "").strip() or (names[0] if names else "You")


def parse_fixes(raw: str | None = None) -> list[tuple[str, str]]:
    raw = os.environ.get("REPORT_FIXES", "") if raw is None else raw
    return [tuple(p.strip() for p in item.split("=", 1)) for item in raw.split(";") if "=" in item and item.split("=", 1)[0].strip()]


def clean_words(text: str, fixes: list[tuple[str, str]], nicknames: list[str], me: str) -> str:
    for a, b in fixes:
        text = re.sub(rf"\b{re.escape(a)}\b", b, text)
    for n in nicknames:
        if n != me:
            text = re.sub(rf"\b{re.escape(n)}\b", me, text)
    return text


# ---------- reading the meeting ----------

def read_transcript(md: str) -> list[dict]:
    return [{"t": m[1], "speaker": m[2], "text": m[3]} for m in map(LINE.match, md.splitlines()) if m]


def split_notes(md: str) -> dict:
    """{'started', 'duration', 'body' (notes without the title and tool lines)}"""
    started = re.search(r"^# .*? — (.+)$", md, re.M)
    duration = re.search(r"^Duration: (\S+)", md, re.M)
    body = md[md.index("## "):] if "## " in md else md
    return {"started": started[1].strip() if started else "", "duration": duration[1] if duration else "", "body": body}


def parse_actions(body: str) -> list[dict]:
    out, inside = [], False
    for line in body.splitlines():
        if line.startswith("## "):
            inside = line.startswith("## Action items")
        elif inside and line.startswith("| ") and not line.startswith("| What") and "(none)" not in line:
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 3:
                out.append({"what": cells[0].rstrip("."), "owner": cells[1], "due": cells[2]})
    return out


# ---------- accepting Gemini's corrections (only what passes these checks) ----------

def apply_note_edits(body: str, edits) -> tuple[str, int, int]:
    """Apply find/replace edits to the notes, only when they replace a label with a name: the words
    around it must stay identical, so an edit cannot rephrase anything. Returns (body, applied, skipped)."""
    applied = skipped = 0
    label = r"(?:You|Unidentified|Unknown|Speaker \d+)"
    for e in edits if isinstance(edits, list) else []:
        find, rep = (e.get("find"), e.get("replace")) if isinstance(e, dict) else (None, None)
        ok = isinstance(find, str) and isinstance(rep, str) and find in body and re.search(rf"\b{label}\b", find)
        if ok:
            parts = re.split(rf"\b{label}\b", find)
            pattern = r"[\w' .-]{1,40}".join(re.escape(x) for x in parts)
            ok = re.fullmatch(pattern, rep) is not None
        if ok:
            body = body.replace(find, rep); applied += 1
        else:
            skipped += 1
    return body, applied, skipped


def apply_polish(lines: list[dict], body: str, actions: list[dict], answer: dict) -> tuple[list[dict], str, list[dict], dict]:
    """Returns corrected (lines, body, actions, info). Each part is checked on its own and kept as it
    was if it fails, so a bad answer can only leave labels uncorrected, never lose or change words."""
    people = [p.strip() for p in answer.get("participants", []) if isinstance(p, str) and p.strip()]
    info = {"title": str(answer.get("title") or "").strip()[:80], "participants": people, "changed": 0, "rejected": []}
    known = set(people) | {l["speaker"] for l in lines}
    lines = [dict(l) for l in lines]
    mine = {"you", *(n.lower() for n in gp.local_names())}
    for item in answer.get("speakers") or []:
        try:
            first, last, name = int(item["from"]), int(item["to"]), str(item["name"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
        if name not in known or GENERIC.match(name) or last < first:
            continue
        for i in range(max(0, first), min(len(lines), last + 1)):
            current = lines[i]["speaker"]
            # A stretch only fills in unnamed or mic-tagged lines; only a single-line entry may overrule a real name.
            # Unnamed lines had no words on the recorder's mic, so that evidence rules the recorder out.
            if name.lower() in mine and current.lower() != "you":
                continue
            if current != name and (first == last or GENERIC.match(current) or current.lower() in mine):
                lines[i]["speaker"] = name
                info["changed"] += 1
    body, applied, skipped = apply_note_edits(body, answer.get("note_edits"))
    info["note_edits"] = applied
    if skipped:
        info["rejected"].append(f"{skipped} note edit(s) skipped")
    new = answer.get("actions")
    if isinstance(new, list):
        good = [{"what": str(a.get("what", "")).strip().rstrip("."), "owner": str(a.get("owner", "")).strip(), "due": str(a.get("due", "")).strip()}
                for a in new if isinstance(a, dict)]
        if all(a["what"] and a["owner"] for a in good) and len(good) <= len(actions):
            actions = good
        else:
            info["rejected"].append("action list added tasks or lost owners")
    return lines, body, actions, info


def polish(lines: list[dict], body: str, me: str, nicknames: list[str], attempts: int = 2) -> dict:
    from google.genai import types
    client = gp.gemini_client()
    numbered = "\n".join(f"{i}|{l['speaker']}|{l['text']}" for i, l in enumerate(lines))
    prompt = POLISH_PROMPT.format(me=me, nicknames=", ".join(nicknames) or me, lines=numbered, notes=body)
    # Thinking counts against the output limit, so leave plenty of room for the answer.
    cfg = types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1, max_output_tokens=65536,
                                      http_options=types.HttpOptions(timeout=240_000),
                                      thinking_config=types.ThinkingConfig(thinking_level=THINKING) if gp.DEFAULT_MODEL.startswith("gemini-3") else None)
    last = None
    for attempt in range(1, attempts + 1):
        try:
            resp = client.models.generate_content(model=gp.DEFAULT_MODEL, contents=prompt, config=cfg)
            return gp.parse_json(resp.text or "")
        except Exception as e:
            last = e
            log.warning("speaker check attempt %d failed: %s", attempt, e)
    raise RuntimeError(f"speaker check failed after {attempts} attempts: {last}")


# ---------- the document ----------

CSS = """
@page { size: Letter; margin: 0.8in 0.75in; }
body { font-family: -apple-system, 'Helvetica Neue', Arial, sans-serif; font-size: 10.5pt; color: #1d1d1f; line-height: 1.45; }
h1 { font-size: 24pt; margin: 0 0 4pt; border-bottom: 2px solid #0a7d74; padding-bottom: 4pt; }
.meta { color: #555; margin: 2pt 0; }
h2 { font-size: 13.5pt; color: #0a7d74; margin: 16pt 0 6pt; }
h2.newpage { page-break-before: always; margin-top: 0; }
h3 { font-size: 11.5pt; margin: 14pt 0 4pt; }
ul { margin: 4pt 0 8pt 16pt; padding: 0; } li { margin: 3pt 0; }
table { border-collapse: collapse; width: 100%; margin: 6pt 0 10pt; font-size: 9.5pt; }
th, td { border: 1px solid #d0d0d0; padding: 4pt 6pt; text-align: left; vertical-align: top; }
th { background: #eef6f5; } tr { page-break-inside: avoid; }
.who { border: 1px solid #d6e9e7; border-left: 4px solid #0a7d74; border-radius: 4px; padding: 6pt 10pt; margin: 0 0 10pt; page-break-inside: avoid; }
.who h3 { margin: 2pt 0 4pt; } .count { background: #0a7d74; color: #fff; border-radius: 8pt; padding: 0 6pt; font-size: 9pt; margin-left: 4pt; }
.who ul { margin: 2pt 0 4pt 16pt; } .due, .none { color: #777; } .due { font-size: 9pt; } .none { margin: 2pt 0 4pt; }
p.line { margin: 0 0 5pt; page-break-inside: avoid; }
.ts { color: #888; font-variant-numeric: tabular-nums; font-size: 9pt; margin-right: 6pt; } .sp { font-weight: 600; margin-right: 2pt; }
"""


def inline(s: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", html.escape(s))


def notes_html(body: str) -> str:
    out, table, in_list = [], [], False
    def end_list():
        nonlocal in_list
        if in_list:
            out.append("</ul>"); in_list = False
    def end_table():
        if table:
            rows = [[c.strip() for c in r.strip().strip("|").split("|")] for r in table if not re.match(r"^\|[-| ]+\|$", r.strip())]
            out.append("<table><thead><tr>" + "".join(f"<th>{inline(c)}</th>" for c in rows[0]) + "</tr></thead><tbody>")
            out.extend("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in rows[1:])
            out.append("</tbody></table>"); table.clear()
    for line in body.splitlines():
        if line.startswith("|"):
            end_list(); table.append(line); continue
        end_table()
        if not line.strip() or line.strip() == "## Summary":
            end_list(); continue
        if line.startswith("## "):
            end_list(); out.append(f"<h3>{inline(line[3:])}</h3>")
        elif line.startswith("- "):
            if not in_list:
                out.append("<ul>"); in_list = True
            out.append(f"<li>{inline(line[2:])}</li>")
        else:
            end_list(); out.append(f"<p>{inline(line)}</p>")
    end_table(); end_list()
    return "\n".join(out)


def with_action_table(body: str, actions: list[dict]) -> str:
    """Rebuild the notes' action table from the corrected list, so it can't disagree with "Who does what"."""
    rows = "\n".join(f"| {x['what']} | {x['owner']} | {'—' if x['due'].lower() in ('', 'unspecified') else x['due']} |" for x in actions) or "| (none) | | |"
    table = f"## Action items\n\n| What | Owner | Due |\n|---|---|---|\n{rows}\n"
    return re.sub(r"## Action items\n.*?(?=\n## |\Z)", lambda _: table, body, flags=re.S)


def people_cards(actions: list[dict], people: list[str], me: str) -> str:
    owners: dict[str, list[dict]] = {}
    for a in actions:
        owners.setdefault(a["owner"], []).append(a)
    named = [p for p in people if p != me]
    order = sorted({*named, *owners} - {me, "unspecified", "Unspecified"}, key=lambda p: -len(owners.get(p, [])))
    order += [me] + [o for o in ("unspecified", "Unspecified") if o in owners]
    cards = []
    for who in order:
        items = owners.get(who, [])
        rows = "".join(f"<li>{html.escape(a['what'])}" + (f" <span class='due'>· due {html.escape(a['due'])}</span>" if a["due"].lower() not in ("", "unspecified", "—") else "") + "</li>" for a in items)
        body = f"<ul>{rows}</ul>" if items else "<p class='none'>No action items assigned in this meeting.</p>"
        label = "No owner yet" if who.lower() == "unspecified" else who
        cards.append(f"<div class='who'><h3>{html.escape(label)} <span class='count'>{len(items)}</span></h3>{body}</div>")
    return "".join(cards)


def document(lines: list[dict], body: str, actions: list[dict], info: dict, started: str, duration: str, me: str) -> str:
    try:
        when = datetime.strptime(started, "%Y-%m-%d %H:%M").strftime("%A, %B %-d, %Y · %-I:%M %p")
    except ValueError:
        when = started
    spoke = list(dict.fromkeys(l["speaker"] for l in lines))
    people = info.get("participants") or [p for p in spoke if not GENERIC.match(p)]
    if me not in people and me in spoke:
        people = [*people, me]
    title = info.get("title")
    top = (f"<h1>Meeting Notes</h1><p class='meta'>{html.escape(title) + ' · ' if title else ''}{html.escape(when)} · {html.escape(duration)}</p>"
           + (f"<p class='meta'>Participants: {html.escape(', '.join(people))}</p>" if people else ""))
    transcript = "".join(f'<p class="line"><span class="ts">{l["t"]}</span><span class="sp">{html.escape(l["speaker"])}</span> {inline(l["text"])}</p>' for l in lines)
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>Meeting Notes</title><style>{CSS}</style></head><body>{top}"
            f"<h2>Who does what</h2>{people_cards(actions, people, me)}"
            f"<h2 class='newpage'>Summary</h2>{notes_html(body)}"
            f"<h2 class='newpage'>Full transcript</h2>{transcript}</body></html>")


def print_pdf(page: str, out: Path) -> Path:
    chrome = next((c for c in CHROME if Path(c).exists()), None)
    if chrome is None:
        raise RuntimeError("no Chrome, Edge or Chromium found to print the PDF")
    with tempfile.TemporaryDirectory(prefix="mr_pdf_") as tmp:     # own profile: never touches the user's browser
        src = Path(tmp) / "notes.html"
        src.write_text(page)
        p = subprocess.run([chrome, "--headless=new", "--disable-gpu", "--no-pdf-header-footer", f"--user-data-dir={tmp}/profile",
                            f"--print-to-pdf={out}", f"file://{src}"], capture_output=True, text=True, timeout=180)
    if not out.exists() or out.stat().st_size < 1000:
        raise RuntimeError(f"printing the PDF failed ({p.returncode}): {p.stderr[-300:]}")
    return out


def build(folder: Path, library: Path | None = None, polisher=polish) -> Path:
    """Writes <folder>/Meeting Notes.pdf and a copy in the Meetings folder (where it's easy to find)."""
    folder = Path(folder)
    notes = split_notes((folder / "summary.md").read_text())
    lines = read_transcript((folder / "transcript.md").read_text())
    actions, body = parse_actions(notes["body"]), notes["body"]
    me, nicknames = my_name(), gp.local_names()
    info: dict = {}
    try:
        lines, body, actions, info = apply_polish(lines, body, actions, polisher(lines, body, me, nicknames))
        log.info("speaker check: %d line(s) relabeled, title %r, rejected %s", info["changed"], info["title"], info["rejected"])
        (folder / "speaker_check.json").write_text(json.dumps(info, indent=1))
    except Exception as e:     # the PDF still gets made with the labels the tool had
        log.exception("speaker check skipped: %s", e)
    fixes = parse_fixes()
    mine = {"you", *(n.lower() for n in nicknames)}
    for l in lines:
        if l["speaker"].lower() in mine:
            l["speaker"] = me
        l["speaker"] = clean_words(l["speaker"], fixes, nicknames, me)
        l["text"] = clean_words(l["text"], fixes, nicknames, me)
    for a in actions:
        if a["owner"].lower() in mine:
            a["owner"] = me
        a["owner"], a["what"] = clean_words(a["owner"], fixes, nicknames, me), clean_words(a["what"], fixes, nicknames, me)
    body = clean_words(re.sub(r"\bYou (?=(?:and|proposes?|suggests?|navigates|clicks|scrolls|views|explains|shares)\b)", f"{me} ", body), fixes, nicknames, me)
    if "participants" in info:
        info["participants"] = list(dict.fromkeys(me if p.lower() in mine else clean_words(p, fixes, nicknames, me) for p in info["participants"]))
    if "title" in info:
        info["title"] = clean_words(info["title"], fixes, nicknames, me)
    body = with_action_table(body, actions)
    pdf = print_pdf(document(lines, body, actions, info, notes["started"], notes["duration"], me), folder / "Meeting Notes.pdf")
    library = folder.parent if library is None else library
    stamp = notes["started"].replace(":", "") or folder.name
    copy = Path(library) / f"Meeting Notes - {stamp}.pdf"
    shutil.copy(pdf, copy)
    log.info("wrote %s", copy)
    return copy


def safe_build(folder: Path, status=lambda m: None) -> None:
    """The PDF is the last step: if it fails, the meeting's transcript and summary still stand."""
    try:
        status("Making the PDF")
        build(folder)
    except Exception as e:
        log.exception("could not make the PDF for %s: %s", folder, e)
