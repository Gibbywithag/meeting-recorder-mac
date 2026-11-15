"""Speaker attribution from the recording's separate audio tracks.

The app writes the local microphone(s) and the call/system audio to separate tracks.
Gemini only hears a mix, so it can tell voices apart but not who is local.

1. Measure, every FRAME_S seconds, whether the call track is active (remote speech) or only
   the mic is (you). Mic bleed is expected, so call activity always wins.
2. Build a clean "you only" track: the mic, boosted to call loudness, muted while the call is
   active. It is mixed into what Gemini hears, and transcribed on its own.
3. Lines of the main transcript whose words appear (in order) in the you-only transcript are
   yours; Gemini's other voices are the far side. No timestamps are used for this, because
   Gemini's timestamps run up to 1.6x fast.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
import subprocess
from pathlib import Path

log = logging.getLogger("speakers")

FRAME_S = 0.5
LOCAL_LABEL = "You"
REMOTE_LABEL = "Client"


# ---------- track roles ----------

def track_roles(folder: Path, n_audio: int) -> list[str]:
    """Return 'mic' or 'system' per audio track, in file order (video, mics..., system)."""
    meta_path = Path(folder) / "capture.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
            mics = len(meta.get("microphones") or [])
            roles = ["mic"] * mics + (["system"] if meta.get("systemAudio") else [])
            if len(roles) == n_audio:
                return roles
            log.warning("capture.json describes %d tracks but file has %d; using default order", len(roles), n_audio)
        except (ValueError, OSError) as e:
            log.warning("unreadable capture.json (%s); using default order", e)
    # Original capture CLI and early app builds: mic first, system second.
    return ["mic"] * (n_audio - 1) + ["system"] if n_audio >= 2 else ["mic"] * n_audio


# ---------- loudness per frame ----------

_RMS = re.compile(r"lavfi\.astats\.Overall\.RMS_level=(-?inf|-?[\d.]+)")


def track_levels(media: Path, track: int) -> list[float]:
    """RMS level in dBFS for each FRAME_S window of one audio track (silence = -120)."""
    samples = int(8000 * FRAME_S)
    cmd = ["ffmpeg", "-v", "error", "-i", str(media), "-map", f"0:a:{track}",
           "-af", f"aresample=8000,asetnsamples=n={samples}:p=0,astats=metadata=1:reset=1,"
                  "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
           "-f", "null", "-"]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg level scan failed for track {track}: {p.stderr.strip()[-300:]}")
    levels = [(-120.0 if v.endswith("inf") else max(-120.0, float(v))) for v in _RMS.findall(p.stdout)]
    log.info("track %d: %d frames measured", track, len(levels))
    return levels


def _percentile(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, max(0, int(q * (len(s) - 1))))] if s else -120.0


def classify_frames(mic: list[float], system: list[float]) -> list[str]:
    """Label each frame 'remote', 'local' or 'quiet' using per-track adaptive thresholds.

    Remote whenever the call track is active (mic bleed is ignored then). Local when the
    call track is quiet and the mic rises above its own noise floor; with no remote sound
    playing, any mic activity can only come from the room. A faint mic (lid closed,
    far from the laptop) still counts, because the floor is measured on quiet-call frames.
    """
    n = min(len(mic), len(system))
    mic, system = mic[:n], system[:n]
    sys_floor, sys_peak = _percentile(system, 0.10), _percentile(system, 0.95)
    # Call audio is digital (no room noise), and speech in it sits around -20..-35 dBFS. Capping the
    # threshold at -50 dB keeps "call active" correct even when the remote side talks nonstop and
    # the adaptive floor would otherwise be measured on speech.
    sys_on = min(sys_floor + max(6.0, 0.35 * (sys_peak - sys_floor)), -50.0)
    mic_when_call_quiet = [m for m, s in zip(mic, system) if s < sys_on] or mic
    mic_floor = _percentile(mic_when_call_quiet, 0.25)
    mic_on = mic_floor + 3.0
    log.info("thresholds: call on>%.1f dB (floor %.1f, peak %.1f); mic on>%.1f dB (floor while call quiet %.1f)",
             sys_on, sys_floor, sys_peak, mic_on, mic_floor)
    out = []
    for m, s in zip(mic, system):
        if s >= sys_on:
            out.append("remote")
        elif m >= mic_on:
            out.append("local")
        else:
            out.append("quiet")
    return out


def _replace_labels(text: str, mapping: dict[str, str]) -> str:
    for old in sorted(mapping, key=len, reverse=True):
        text = re.sub(rf"(?<![\w]){re.escape(old)}(?![\w])", mapping[old], text)
    return text


def apply_mapping(merged: dict, mapping: dict[str, str]) -> dict:
    """Rename speakers everywhere they appear: transcript, owners, summary and lists."""
    if not mapping:
        return merged
    for turn in merged.get("transcript", []):
        turn["speaker"] = mapping.get(turn.get("speaker"), turn.get("speaker"))
    generic = {k: v for k, v in mapping.items() if re.fullmatch(r"Speaker \d+", k)}
    for item in merged.get("action_items", []):
        item["owner"] = _replace_labels(str(item.get("owner", "")), mapping)
        # Inside the task text only rename generic labels; a real name there may mean the person.
        item["what"] = _replace_labels(str(item.get("what", "")), generic)
    merged["summary"] = _replace_labels(merged.get("summary", ""), mapping)
    for field in ("decisions", "proposals", "open_questions"):
        merged[field] = [_replace_labels(x, mapping) for x in merged.get(field, [])]
    for ref in merged.get("screen_refs", []):
        for key in ("said", "on_screen"):
            ref[key] = _replace_labels(str(ref.get(key, "")), mapping)
    return merged


def _median(values: list[float]) -> float:
    return _percentile(values, 0.5) if values else -120.0


def analyze_tracks(folder: Path, media: Path) -> dict:
    """Measure every audio track once. Returns roles, per-frame classes, and the mic boost
    needed so the local voice is as loud as the call in the AI mix. Never raises."""
    folder = Path(folder)
    info: dict = {"roles": [], "frames": [], "mic_gain_db": 0.0, "note": ""}
    try:
        n_audio = len(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                                      "stream=index", "-of", "csv=p=0", str(media)],
                                     capture_output=True, text=True, check=True).stdout.split())
        roles = track_roles(folder, n_audio)
        info["roles"] = roles
        mic_tracks = [i for i, r in enumerate(roles) if r == "mic"]
        sys_tracks = [i for i, r in enumerate(roles) if r == "system"]
        if not mic_tracks or not sys_tracks:
            info["note"] = f"needs a mic and a call-audio track (found {roles})"
            log.warning(info["note"])
            return info
        mic_levels = [track_levels(media, i) for i in mic_tracks]
        mic = [max(vals) for vals in zip(*mic_levels)]
        system = track_levels(media, sys_tracks[0])
        frames = classify_frames(mic, system)
        info["frames"] = frames
        info["counts"] = {k: frames.count(k) for k in ("local", "remote", "quiet")}
        local_mic = [m for m, f in zip(mic, frames) if f == "local"]
        remote_sys = [s for s, f in zip(system, frames) if f == "remote"]
        if local_mic and remote_sys:
            info["mic_gain_db"] = round(max(0.0, min(36.0, _median(remote_sys) - _median(local_mic))), 1)
        log.info("track analysis: roles=%s frames=%s mic boost=%.1f dB", roles, info["counts"], info["mic_gain_db"])
    except Exception as e:  # analysis is an enhancement; the meeting must still be processed
        log.exception("track analysis failed: %s", e)
        info["note"] = f"track analysis failed: {e}"
    return info


# ---------- attribution by matching words (timestamp-free) ----------
#
# Gemini's timestamps run up to 1.6x fast even on audio-only input, so speakers are matched
# by WORDS, which Gemini gets in the right order: the clean mic track (only you) is transcribed
# separately, and main-transcript lines whose words line up with it are yours.

def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", str(text).lower().replace("’", "'"))


def matched_words(turns: list[dict], local_texts: list[str]) -> list[int]:
    """For each turn, how many of its words align (in order) with the mic-only transcript.
    Only runs of 2+ words count: a lone "yeah" matches anyone, so one-word replies are decided
    by their Gemini voice label instead."""
    stream, owner = [], []
    for i, t in enumerate(turns):
        for w in words(t.get("text", "")):
            stream.append(w); owner.append(i)
    local = [w for txt in local_texts for w in words(txt)]
    hits = [0] * len(turns)
    if not stream or not local:
        return hits
    for block in difflib.SequenceMatcher(None, stream, local, autojunk=False).get_matching_blocks():
        if block.size >= 2:
            for k in range(block.a, block.a + block.size):
                hits[owner[k]] += 1
    return hits


TURN_SHARE = 0.5        # a line is yours when half its words are on your mic track
VOICE_SHARE = 0.25      # a Gemini voice is yours when a quarter of its words are


def is_generic(label: str) -> bool:
    return bool(re.fullmatch(r"(speaker \d+|unknown|speaker)", label.strip().lower()))


def decide_by_text(turns: list[dict], hits: list[int], names: list[str] | None = None,
                   local_words: int = 0) -> tuple[dict[str, str], dict[str, dict]]:
    """Map Gemini's voice labels to You / Client / Other caller N.

    1. You = the voice whose words clearly match the mic-only transcript.
    2. Otherwise (in-person meetings put both voices on both tracks, so few words match):
       the voice whose amount of speech is closest to what the mic-only track heard. A voice
       with 10x more words than your mic heard cannot be you. Needs at least 5 mic-only words.
    3. Your name (LOCAL_SPEAKER_NAMES) only breaks a near-tie in step 2; Gemini tends to put
       the recorder's name on whoever talks most, so it is never trusted on its own."""
    stats: dict[str, dict] = {}
    for t, h in zip(turns, hits):
        e = stats.setdefault(t.get("speaker", "Unknown"), {"words": 0, "matched": 0, "turns": 0})
        e["words"] += len(words(t.get("text", ""))); e["matched"] += h; e["turns"] += 1
    share = {k: (e["matched"] / e["words"] if e["words"] else 0.0) for k, e in stats.items()}
    ranked = sorted(stats, key=lambda k: share[k], reverse=True)
    mapping: dict[str, str] = {}
    you, how = None, ""
    if ranked and share[ranked[0]] >= VOICE_SHARE and (len(ranked) == 1 or share[ranked[1]] <= share[ranked[0]] / 2):
        you, how = ranked[0], "words matched your mic track"
    elif local_words >= 5 and len(stats) >= 2:
        import math
        gap = {k: abs(math.log((e["words"] + 5) / (local_words + 5))) for k, e in stats.items()}
        order = sorted(stats, key=lambda k: gap[k])
        best, second = order[0], order[1]
        if gap[best] <= math.log(4):                       # within 4x of what your mic heard
            if gap[second] - gap[best] >= math.log(1.5):
                you, how = best, "amount of speech matched your mic track"
            else:
                lowered = {n.lower() for n in (names or [])}
                named = [k for k in (best, second) if k.lower() in lowered]
                if len(named) == 1:
                    you, how = named[0], "near-tie broken by your name"
    if you:
        mapping[you] = LOCAL_LABEL
    # Real names Gemini heard (e.g. a client introducing themselves) are kept: renaming them per chunk scrambled
    # group meetings. "Client" is used only for a 1-on-1, i.e. exactly one other voice.
    lowered = {n.lower() for n in (names or [])}
    others = [k for k in stats if k != you]
    generic = [k for k in others if is_generic(k)]
    for label in others:
        if label.lower() in lowered:
            # Gemini put your name on a voice that isn't you: in a 1-on-1 that is the client.
            mapping[label] = REMOTE_LABEL if len(others) == 1 else "Unidentified"
        elif not is_generic(label):
            mapping[label] = label
    if len(others) == 1 and generic:
        mapping[generic[0]] = REMOTE_LABEL
    for k in stats:
        stats[k]["share"] = round(share[k], 3)
    stats["_you_by"] = how or "not identified"
    return mapping, stats


def attribute_parts(folder: Path, results: list[dict], local_by_part: dict[int, list[str]] | None,
                    note: str = "", names: list[str] | None = None) -> str:
    """Rename speakers to You / Client in each chunk's Gemini result, BEFORE the chunks are merged.

    Gemini numbers voices differently per chunk (it put your name on the client in some chunks), so
    every chunk's transcript AND notes (summary, decisions, action-item owners) are renamed with
    that chunk's own mapping. Each line: yours if its words are on your mic track; otherwise it
    follows its Gemini voice. Returns the line for summary.md. Never raises."""
    report = {"method": "mic-only transcript: word match, then amount of speech", "parts": [], "note": note}
    try:
        if local_by_part is None and not names:
            report["note"] = report["note"] or "no separate mic track; labels left as Gemini gave them"
        else:
            local_by_part = local_by_part or {}
            changed = 0
            for index, result in enumerate(results):
                turns = result.get("transcript", [])
                local_texts = local_by_part.get(index, [])
                hits = matched_words(turns, local_texts)
                mapping, stats = decide_by_text(turns, hits, names, sum(len(words(x)) for x in local_texts))
                by_line = 0
                for t, h in zip(turns, hits):
                    n = len(words(t.get("text", "")))
                    voice = mapping.get(t.get("speaker"))
                    if n and h / n >= TURN_SHARE:
                        target = LOCAL_LABEL
                        by_line += voice != LOCAL_LABEL
                    elif voice:
                        target = voice
                    else:
                        continue
                    t["speaker"] = target
                    changed += 1
                notes = {k: v for k, v in result.items() if k != "transcript"}
                apply_mapping(notes, mapping)
                result.update(notes)
                log.info("part %d: You by %s; voices %s -> %s, %d line(s) moved to You by their own words",
                         index, stats.get("_you_by"), {k: v for k, v in stats.items() if k != "_you_by"}, mapping, by_line)
                report["parts"].append({"part": index, "voices": stats, "mapping": mapping, "lines_by_words": by_line})
            if not changed:
                report["note"] = "could not match any lines to the mic track; labels left as Gemini gave them"
    except Exception as e:
        log.exception("speaker attribution failed: %s", e)
        report["note"] = f"speaker attribution failed: {e}"
    (Path(folder) / "speakers.json").write_text(json.dumps(report, indent=2))
    all_turns = [t for r in results for t in r.get("transcript", [])]
    return describe(all_turns, report)


def describe(transcript: list[dict], report: dict) -> str:
    """One readable line for summary.md explaining how speakers were identified."""
    counts: dict[str, int] = {}
    for t in transcript:
        counts[t.get("speaker", "?")] = counts.get(t.get("speaker", "?"), 0) + len(words(t.get("text", "")))
    total = sum(counts.values()) or 1
    you_share = counts.get(LOCAL_LABEL, 0) / total
    line = ""
    if counts.get(LOCAL_LABEL):
        line = f"Your lines (You, {you_share:.0%} of words) were identified from your separate mic track."
    elif report.get("parts"):
        line = "Your voice was not found on your mic track, so no line is labelled You."
    others = sorted((k for k in counts if k != LOCAL_LABEL), key=lambda k: -counts[k])
    if len(others) > 2:
        line += f" Group meeting: {len(others)} other voices, named where Gemini heard a name."
    if report.get("note"):
        line = (line + " " if line else "") + "Note: " + report["note"] + "."
    return line


# ---------- clean local voice for the AI mix ----------

def local_voice_wav(media: Path, analysis: dict, out: Path, rate: int = 16000) -> Path | None:
    """Write the mic track(s) boosted to call loudness, muted wherever the call track is
    active (removes speaker bleed/echo). Returns None when there is nothing to build."""
    import array
    import wave
    roles, frames = analysis.get("roles") or [], analysis.get("frames") or []
    mic_tracks = [i for i, r in enumerate(roles) if r == "mic"]
    if not mic_tracks or not frames:
        return None
    gain = analysis.get("mic_gain_db", 0.0)
    inputs = []
    for i in mic_tracks:
        inputs += ["-map", f"0:a:{i}"]
    mix = "".join(f"[0:a:{i}]" for i in mic_tracks)
    chain = (f"{mix}amix=inputs={len(mic_tracks)}:normalize=0," if len(mic_tracks) > 1 else f"{mix}anull,") + \
        f"highpass=f=80,afftdn=nf=-30,volume={gain}dB,alimiter=limit=0.9,aresample={rate}[m]"
    cmd = ["ffmpeg", "-v", "error", "-i", str(media), "-filter_complex", chain, "-map", "[m]",
           "-ac", "1", "-f", "s16le", "-acodec", "pcm_s16le", "-"]
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg local-voice decode failed: {p.stderr.decode(errors='replace')[-300:]}")
    pcm = array.array("h")
    pcm.frombytes(p.stdout[: len(p.stdout) // 2 * 2])
    per_frame = int(rate * FRAME_S)
    pad = per_frame // 2           # also mute a quarter second either side of call speech
    zeros = array.array("h", bytes(2 * (per_frame + 2 * pad)))
    muted = 0
    for idx, f in enumerate(frames):
        if f == "remote":
            a, b = max(0, idx * per_frame - pad), min(len(pcm), (idx + 1) * per_frame + pad)
            if a < b:
                pcm[a:b] = zeros[: b - a]
                muted += 1
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    log.info("local voice: %.0fs, boosted %.1f dB, muted %d call-active frames -> %s",
             len(pcm) / rate, gain, muted, out.name)
    return out
