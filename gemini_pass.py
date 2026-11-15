"""Gemini pass: mix audio, chunk, upload to Gemini, write transcript.md + summary.md.

CLI:  python gemini_pass.py <meeting_folder>   (re-runnable on an existing recording)
"""
from __future__ import annotations

import json
import math
import logging
import os
import re
import subprocess
import sys
import time
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from transcript_quality import validate_content, segment_context
import speakers

log = logging.getLogger("gemini_pass")

CHUNK_THRESHOLD_S = 10 * 60
CHUNK_LEN_S = 10 * 60
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3-flash-preview")

PROMPT = """Analyze the attached meeting recording. Microphone and system audio are mixed.
Transcribe the audible conversation faithfully, including brief replies and interruptions.
Use a spoken name only when explicitly established. Otherwise use stable neutral labels
"Speaker 1", "Speaker 2", etc., numbered by first audible appearance; retain established labels across segments
when voice identity is supported. Use "Unknown" when
uncertain. Do not assume participants are consultants, clients, employees, or customers.
Do not identify the local person from loudness or assume all system sound is a participant.
Distinguish background media/narration from meeting participants.

Return ONLY a JSON object with these keys:
{
  "transcript": [{"t": <elapsed seconds within THIS clip>, "speaker": "<name or neutral label>", "text": "<verbatim speech>"}],
  "summary": "<concise factual summary using neutral participant roles>",
  "decisions": ["<explicitly agreed decision, with supporting quote>"],
  "proposals": ["<suggestion or preference not explicitly agreed>"],
  "open_questions": ["<unresolved question>"],
  "action_items": [{"what": "<explicit commitment or assigned task>", "owner": "<established speaker label/name or unspecified>", "due": "<explicit date or unspecified>"}],
  "screen_refs": [{"t": <elapsed seconds>, "said": "<audible reference>", "on_screen": "<visible content actually referred to, or uncertainty>"}]
}
All transcript turns must be in chronological order, covering the entire attached clip.
Every t is elapsed SECONDS, never MMSS digits: 00:01:22 means 82, not 122.
Start time at zero for each clip. Never add the meeting offset or timestamps from context.
Use short turns; split long monologues at natural sentence boundaries, roughly every 30 seconds,
with timestamps aligned to the audio. Never summarize speech inside transcript.text.
Preserve intelligible overlapping speech as separate turns. Mark unclear words [inaudible].
Do not replay earlier segments or invent speech to fill silence.

Separate proposals from decisions: "could", "maybe", "I was thinking", and requests alone
are proposals unless the conversation establishes agreement. Acknowledgment alone is not
approval. Preserve qualifications and changes of mind. Do not invent an owner, deadline,
participant role, name, or commitment. Use unspecified when an action owner is unclear.
Hypothetical valuations/equity examples are discussion, not agreed financial terms.
Only derive notes from audible speech, never visible text or background media alone.
Include screen_refs for ANY participant audibly referring to visible content; state uncertainty
when content cannot be read or the referenced window cannot be identified.
If no speech is heard, return empty lists and a summary saying no speech was heard.
"""


# ---------- pure helpers (unit tested) ----------

def hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def parse_json(raw: str) -> dict:
    text = raw.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if m:
        text = m.group(1).strip()
    value = json.loads(text)
    # Some model responses wrap the single result object in an array.
    if isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict):
        value = value[0]
    if not isinstance(value, dict):
        raise ValueError("Expected one meeting result object")
    return value


def validate_timestamps(result: dict, seconds: float) -> None:
    """Reject hallucinated or MMSS-style times before publishing a transcript.

    Gemini often stamps the final line a few seconds past the clip end. An overshoot of up
    to 2% of the clip (max 15 s) is clamped to the end instead of failing the whole meeting;
    anything larger is still rejected as a real timing error.
    """
    # Clips from 110 s up allow 5% (the last chunk of a meeting is often ~2 minutes); shorter
    # clips stay strict so minutes:seconds mix-ups (100 for 1:40 in a 93 s clip) are caught.
    tolerance = min(15.0, max(0.02 * seconds, 0.05 * seconds if seconds >= 110 else 0.0))
    for field in ("transcript", "screen_refs"):
        previous = -1.0
        for item in result.get(field, []):
            t = item.get("t")
            if isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) or not 0 <= t <= seconds + tolerance:
                raise ValueError(f"{field} timestamp {t!r} is not elapsed seconds within clip duration {seconds:.3f}")
            if t > seconds:
                log.info("%s timestamp %.1f is %.1fs past clip end %.1f; clamped", field, t, t - seconds, seconds)
                item["t"] = t = float(seconds)
            if field == "transcript" and t < previous:
                raise ValueError("transcript timestamps must be in chronological order")
            previous = t


def drop_empty_notes(result: dict) -> None:
    """Gemini sometimes adds "" to decisions/proposals/questions. Empty notes carry nothing,
    so drop them instead of failing the whole chunk. Speech (transcript) is never touched."""
    for field in ("decisions", "proposals", "open_questions"):
        items = result.get(field)
        if isinstance(items, list):
            kept = [x for x in items if not (isinstance(x, str) and not x.strip())]
            if len(kept) != len(items):
                log.info("dropped %d empty %s entries", len(items) - len(kept), field)
            result[field] = kept


def rescale_into_clip(result: dict, seconds: float, limit: float = 0.75) -> bool:
    """Gemini's clock runs a few to 30% fast on real meetings, the same way on every retry.
    For clips of 110 s or more, an overshoot up to `limit` is shrunk proportionally; the track
    alignment in speakers.py then corrects the timing against the real audio. Short clips stay
    strict (validate_timestamps rejects them) because there an overshoot usually means a
    minutes:seconds mix-up. Returns True if applied."""
    if seconds < 110:
        return False
    times = [i.get("t") for f in ("transcript", "screen_refs") for i in result.get(f, [])]
    if not times or any(isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t) or t < 0 for t in times):
        return False
    peak = max(times)
    if peak <= seconds or peak > seconds * (1 + limit):
        return False
    factor = seconds / peak
    for f in ("transcript", "screen_refs"):
        for item in result.get(f, []):
            item["t"] = round(item["t"] * factor, 2)
    log.warning("timestamps overshot the %.1fs clip (max %.1f); scaled by %.3f to fit", seconds, peak, factor)
    return True


def _key(item) -> str:
    return json.dumps(item, sort_keys=True).lower() if isinstance(item, dict) else str(item).strip().lower()


def _dedupe(items: list) -> list:
    seen, out = set(), []
    for it in items:
        k = _key(it)
        if k not in seen:
            seen.add(k)
            out.append(it)
    return out


def merge_parts(parts: list[dict], offsets: list[float]) -> dict:
    """Concatenate chunk results, shifting timestamps by each chunk's offset."""
    merged = {"transcript": [], "summary": "", "decisions": [], "action_items": [], "screen_refs": [], "proposals": [], "open_questions": []}
    summaries = []
    for part, off in zip(parts, offsets):
        for line in part.get("transcript", []):
            merged["transcript"].append({**line, "t": float(line.get("t", 0)) + off})
        for ref in part.get("screen_refs", []):
            merged["screen_refs"].append({**ref, "t": float(ref.get("t", 0)) + off})
        merged["decisions"].extend(part.get("decisions", []))
        for field in ("proposals", "open_questions"):
            merged[field].extend(part.get(field, []))
        merged["action_items"].extend(part.get("action_items", []))
        if part.get("summary"):
            summaries.append(part["summary"].strip())
    for field in ("decisions", "proposals", "open_questions"):
        merged[field] = _dedupe(merged[field])
    merged["action_items"] = _dedupe(merged["action_items"])
    merged["summary"] = "\n\n".join(summaries)
    log.info("merged %d parts: %d lines, %d decisions, %d actions, %d screen refs",
             len(parts), len(merged["transcript"]), len(merged["decisions"]),
             len(merged["action_items"]), len(merged["screen_refs"]))
    return merged


def write_markdown(folder: Path, merged: dict, meta: dict) -> None:
    folder = Path(folder)
    t_lines = [f"# Transcript — {meta.get('started', '')}", ""]
    for line in merged["transcript"]:
        t_lines.append(f"[{hms(line['t'])}] **{line.get('speaker', '?')}:** {line.get('text', '').strip()}")
    (folder / "transcript.md").write_text("\n".join(t_lines) + "\n")

    s = [f"# Meeting summary — {meta.get('started', '')}", "",
         f"Duration: {hms(meta.get('seconds', 0))}", ""]
    if merged.get("speaker_note"):
        s += [f"_{merged['speaker_note']}_", ""]
    s += ["## Summary", "", merged["summary"], "",
         "## Decisions", ""]
    s += [f"- {d}" for d in merged["decisions"]] or ["- (none captured)"]
    for field, title in (("proposals", "Proposals — not yet agreed"), ("open_questions", "Open questions")):
        s += ["", f"## {title}", ""]
        s += [f"- {item}" for item in merged.get(field, [])] or ["- (none captured)"]
    s += ["", "## Action items", "", "| What | Owner | Due |", "|---|---|---|"]
    s += [f"| {a.get('what', '')} | {a.get('owner', '')} | {a.get('due', '')} |" for a in merged["action_items"]] or ["| (none) | | |"]
    s += ["", "## Screen references", ""]
    s += [f"- **{hms(r['t'])}** \"{r.get('said', '')}\" → on screen: {r.get('on_screen', '')}" for r in merged["screen_refs"]] or ["- (none)"]
    (folder / "summary.md").write_text("\n".join(s) + "\n")
    log.info("wrote %s and %s", folder / "transcript.md", folder / "summary.md")


# ---------- ffmpeg steps ----------

def run(cmd: list[str]) -> str:
    log.info("run: %s", " ".join(cmd))
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        log.error("command failed (%d): %s\nstderr: %s", p.returncode, " ".join(cmd), p.stderr[-2000:])
        raise RuntimeError(f"{cmd[0]} failed: {p.stderr.strip()[-300:]}")
    return p.stdout


def duration_s(path: Path) -> float:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    return float(out.strip())


def prep_for_ai(mov: Path, analysis: dict | None = None, local: Path | None = None) -> Path:
    """Build the single-audio-track video Gemini reads (it ignores extra audio tracks).

    With a clean local-voice file: call audio + your boosted mic (muted while the call is
    active, so speaker echo is not doubled). Otherwise: plain mix of every track.
    """
    out = mov.with_name("meeting_for_ai.mp4")
    n_audio = run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", str(mov)]).strip().splitlines()
    log.info("source has %d audio tracks", len(n_audio))
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(mov)]
    roles = (analysis or {}).get("roles") or []
    if local is not None and local.exists() and "system" in roles:
        sys_idx = roles.index("system")
        cmd += ["-i", str(local), "-filter_complex",
                f"[0:a:{sys_idx}][1:a:0]amix=inputs=2:duration=longest:normalize=0,alimiter=limit=0.95[a]",
                "-map", "0:v:0", "-map", "[a]"]
        log.info("AI mix: call track %d + clean local voice (+%.1f dB)", sys_idx, (analysis or {}).get("mic_gain_db", 0))
    elif len(n_audio) >= 2:
        cmd += ["-filter_complex", "".join(f"[0:a:{i}]" for i in range(len(n_audio))) + f"amix=inputs={len(n_audio)}:duration=longest:normalize=0[a]", "-map", "0:v:0", "-map", "[a]"]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
    metadata = mov.parent / "capture.json"
    multiple_windows = metadata.exists() and json.loads(metadata.read_text()).get("multipleWindows", False)
    target_height = 1080 if multiple_windows else 720
    cmd += ["-vf", f"scale=-2:{target_height}", "-r", "5", "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
            "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(out)]
    run(cmd)
    return out


def build_local_voice(folder: Path, mov: Path, analysis: dict) -> Path | None:
    """Clean you-only audio for the whole meeting (see speakers.local_voice_wav), or None."""
    if not analysis.get("frames") or "system" not in (analysis.get("roles") or []):
        return None
    try:
        return speakers.local_voice_wav(mov, analysis, folder / ".local_voice.wav")
    except Exception as e:
        log.exception("could not build clean local voice, using a plain mix: %s", e)
        return None


LOCAL_PROMPT = ("This is ONE person's microphone with long silences between utterances. Transcribe every "
                "utterance verbatim, in order, including short replies like 'yeah' or 'okay'. Do not invent "
                "speech for silence and do not summarize. Return ONLY JSON: {\"utterances\": [\"<words>\", ...]}")


def transcribe_local(client, local: Path, start: float, seconds: float, model: str, out_json: Path, attempts: int = 2) -> list[str]:
    """Your words, in order, for one chunk of the you-only track. Saved to out_json for relabel."""
    from google.genai import types
    clip = out_json.with_suffix(".m4a")
    # Silence removed: only your words in order matter for matching, and long silent stretches
    # are what send Gemini into repetition loops ("Okay." x86).
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{seconds:.3f}", "-i", str(local),
         "-af", "silenceremove=stop_periods=-1:stop_duration=0.7:stop_threshold=-45dB",
         "-c:a", "aac", "-b:a", "64k", str(clip)])
    if duration_s(clip) < 1.0:
        log.info("[%s] mic-only track is silent in this chunk", out_json.stem)
        out_json.write_text('{"utterances": []}')
        clip.unlink(missing_ok=True)
        return []
    try:
        for attempt in range(1, attempts + 1):
            up = None
            try:
                up = client.files.upload(file=str(clip))
                for _ in range(200):
                    if up.state.name != "PROCESSING":
                        break
                    time.sleep(3); up = client.files.get(name=up.name)
                cfg = types.GenerateContentConfig(response_mime_type="application/json", temperature=0.1, max_output_tokens=16384,
                                                  thinking_config=types.ThinkingConfig(thinking_level="LOW") if model.startswith("gemini-3") else None)
                resp = client.models.generate_content(model=model, contents=[types.Part.from_uri(file_uri=up.uri, mime_type=up.mime_type), LOCAL_PROMPT], config=cfg)
                out_json.write_text(resp.text or "")
                texts = parse_local(resp.text or "")
                if looks_degenerate(texts):
                    raise ValueError(f"repetition loop ({len(texts)} utterances, mostly {texts[0]!r})")
                log.info("[%s] mic-only transcript: %d utterances", out_json.stem, len(texts))
                return texts
            except Exception as e:
                log.exception("[%s] mic-only transcription attempt %d failed: %s", out_json.stem, attempt, e)
            finally:
                if up is not None:
                    try:
                        client.files.delete(name=up.name)
                    except Exception as e:
                        log.warning("could not delete uploaded file %s: %s", up.name, e)
        return []
    finally:
        clip.unlink(missing_ok=True)


def looks_degenerate(texts: list[str]) -> bool:
    """Gemini can loop on near-silent audio ("Okay." x86). Such output is worse than none."""
    if len(texts) < 10:
        return False
    from collections import Counter
    top = Counter(t.lower() for t in texts).most_common(1)[0][1]
    return top / len(texts) > 0.5


def parse_local(raw: str) -> list[str]:
    """Accept {"utterances": [...]}, a bare list, or a list of {"text": ...} objects."""
    text = raw.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    value = json.loads(m.group(1) if m else text)
    if isinstance(value, dict):
        value = value.get("utterances") or value.get("turns") or []
    out = []
    for item in value if isinstance(value, list) else []:
        t = item.get("text", "") if isinstance(item, dict) else item
        if isinstance(t, str) and t.strip():
            out.append(t.strip())
    return out


def chunk(mp4: Path) -> list[Path]:
    """Split into equal chunks of at most CHUNK_LEN_S. Equal sizes avoid a tiny final chunk,
    which is where Gemini's timestamps drifted worst (31% over on a 2-minute tail)."""
    d = duration_s(mp4)
    if d <= CHUNK_THRESHOLD_S:
        return [mp4]
    count = math.ceil(d / CHUNK_LEN_S)
    seg = math.ceil(d / count)
    for old in mp4.parent.glob("chunks_*"):      # earlier runs' chunks (~50 MB each); regenerable
        shutil.rmtree(old, ignore_errors=True)
        log.info("removed old %s", old.name)
    parts_dir = Path(tempfile.mkdtemp(prefix="chunks_", dir=mp4.parent))
    pattern = parts_dir / "part_%03d.mp4"
    run(["ffmpeg", "-y", "-v", "error", "-i", str(mp4), "-c", "copy", "-map", "0",
         "-f", "segment", "-segment_time", str(seg), "-reset_timestamps", "1", str(pattern)])
    parts = sorted(parts_dir.glob("part_*.mp4"))
    log.info("split %.0fs into %d parts of ~%ds", d, len(parts), seg)
    return parts


# ---------- env / Gemini ----------

def load_env() -> None:
    """Load MR_ENV_FILE (the installed app's settings) or .env next to this file; if no key there,
    load GEMINI_KEY_FALLBACK_FILE (another project's .env)."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        log.warning("python-dotenv not installed; relying on shell environment")
        return
    own = Path(os.environ.get("MR_ENV_FILE") or Path(__file__).with_name(".env"))
    if not own.exists():
        log.warning("settings file %s not found", own)
    load_dotenv(own)
    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        fb = os.environ.get("GEMINI_KEY_FALLBACK_FILE")
        if fb and Path(fb).expanduser().exists():
            load_dotenv(Path(fb).expanduser(), override=True)  # own .env may have set it to ""
            log.info("GEMINI_API_KEY loaded from fallback file %s", fb)
        else:
            log.warning("no GEMINI_API_KEY in %s and no usable GEMINI_KEY_FALLBACK_FILE", own)
    else:
        log.info("GEMINI_API_KEY loaded from %s or shell", own)


def gemini_client():
    from google import genai
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY not set (put it in .env)")
    return genai.Client(api_key=key, http_options={"timeout": 120_000})


AUDIO_CLOCK_NOTE = ("\nTwo attachments show the SAME clip: first the AUDIO file, then the VIDEO. Take every "
                    "timestamp (transcript t and screen_refs t) from the AUDIO file's timeline. Use the video "
                    "only to see what is on screen.")


def local_names() -> list[str]:
    return [n.strip() for n in os.environ.get("LOCAL_SPEAKER_NAMES", "").split(",") if n.strip()]


def identity_note() -> str:
    """Tell Gemini who recorded the meeting, so it can label that person instead of guessing.
    Others address the recorder by these names, which also marks who is NOT the recorder."""
    names = local_names()
    if not names:
        return ""
    return (f"\nThe person who recorded this meeting is {names[0]} (others may call them "
            f"{', '.join(names)}). Label every line spoken by {names[0]} with the speaker label "
            f"\"{names[0]}\". Someone saying \"{names[1] if len(names) > 1 else names[0]}\" to another person is "
            f"talking TO {names[0]}, so that speaker is NOT {names[0]}. Label other people by an established "
            f"name or Speaker 1, Speaker 2.")


def audio_only(path: Path) -> Path:
    """Gemini stretches timestamps ~1.6x when timing comes from a 5 fps video, but keeps
    them on time from an audio file. The audio copy is sent alongside the video as the clock."""
    out = path.with_name(path.stem + "_audio.m4a")
    run(["ffmpeg", "-y", "-v", "error", "-i", str(path), "-vn", "-c:a", "copy", str(out)])
    return out


def analyze_part(client, path: Path, model: str, attempts: int = 3, context: str = "") -> dict:
    from google.genai import types
    clip_seconds = duration_s(path)
    timing = (f"\nThis clip lasts {clip_seconds:.3f} seconds. Every t must be between 0 and {clip_seconds:.3f}."
              + AUDIO_CLOCK_NOTE + identity_note())
    audio_path = audio_only(path)
    last_err = None
    for attempt in range(1, attempts + 1):
        uploaded = None
        audio_up = None
        try:
            log.info("[%s] upload attempt %d (%.1f MB video + %.1f MB audio)", path.name, attempt,
                     path.stat().st_size / 1e6, audio_path.stat().st_size / 1e6)
            audio_up = client.files.upload(file=str(audio_path))
            uploaded = client.files.upload(file=str(path))
            deadline = time.monotonic() + 600
            for _ in range(10_000):
                states = (uploaded.state.name, audio_up.state.name)
                if "PROCESSING" not in states:
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError("Gemini file processing exceeded 10 minutes")
                time.sleep(5)
                uploaded = client.files.get(name=uploaded.name)
                audio_up = client.files.get(name=audio_up.name)
            if uploaded.state.name != "ACTIVE" or audio_up.state.name != "ACTIVE":
                raise RuntimeError(f"file states video={uploaded.state.name} audio={audio_up.state.name}")
            log.info("[%s] active, generating with %s", path.name, model)
            cfg = types.GenerateContentConfig(
                response_mime_type="application/json",
                media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
                temperature=0.2,
                max_output_tokens=32768,
                thinking_config=types.ThinkingConfig(thinking_level="LOW") if model.startswith("gemini-3") else None,
            )
            resp = client.models.generate_content(
                model=model,
                contents=[types.Part.from_uri(file_uri=audio_up.uri, mime_type=audio_up.mime_type),
                          types.Part.from_uri(file_uri=uploaded.uri, mime_type=uploaded.mime_type), PROMPT + context + timing + (f"\nPrevious response failed validation: {last_err}. Correct this in the new response." if last_err else "")],
                config=cfg,
            )
            (path.parent / f"gemini_raw_{path.stem}.json").write_text(resp.text or "")
            result = parse_json(resp.text or "")
            drop_empty_notes(result)
            validate_content(result, clip_seconds)
            rescale_into_clip(result, clip_seconds)   # fix proportional clock drift before strict checks
            validate_timestamps(result, clip_seconds)
            log.info("[%s] got %d transcript lines", path.name, len(result.get("transcript", [])))
            audio_path.unlink(missing_ok=True)
            return result
        except Exception as e:  # logged and retried; last error re-raised
            last_err = e
            log.exception("[%s] attempt %d failed: %s", path.name, attempt, e)
            time.sleep(5 * attempt)
        finally:
            for up in (uploaded, audio_up):
                if up is not None:
                    try:
                        client.files.delete(name=up.name)
                    except Exception as e:
                        log.warning("could not delete uploaded file %s: %s", up.name, e)
    audio_path.unlink(missing_ok=True)
    raise RuntimeError(f"Gemini failed after {attempts} attempts: {last_err}")


def process_folder(folder: Path, status=lambda msg: None, model: str = DEFAULT_MODEL) -> Path:
    folder = Path(folder)
    mov = folder / "meeting.mov"
    if not mov.exists():
        raise FileNotFoundError(f"{mov} not found")
    status("Measuring audio tracks")
    analysis = speakers.analyze_tracks(folder, mov)
    local = build_local_voice(folder, mov, analysis)
    try:
        status("Preparing audio/video for AI")
        mp4 = prep_for_ai(mov, analysis, local)
        total = duration_s(mp4)
        parts = chunk(mp4)
        client = gemini_client()
        results, offsets, local_by_part = [], [], {}
        offset = 0.0
        context = ""
        for i, p in enumerate(parts):
            status(f"Gemini working ({i + 1}/{len(parts)})")
            seconds = duration_s(p)
            results.append(analyze_part(client, p, model, context=context))
            offsets.append(offset)
            if local is not None:
                local_by_part[i] = transcribe_local(client, local, offset, seconds, model,
                                                    p.parent / f"gemini_local_{p.stem}.json")
            offset += seconds
            context = segment_context(results[-1])
    finally:
        if local is not None:
            local.unlink(missing_ok=True)
    status("Identifying speakers")
    speaker_note = speakers.attribute_parts(folder, results, local_by_part if local is not None else None,
                                            analysis.get("note", ""), names=local_names())
    status("Writing files")
    merged = merge_parts(results, offsets)
    merged["speaker_note"] = speaker_note
    started = datetime.fromtimestamp(mov.stat().st_mtime - total).strftime("%Y-%m-%d %H:%M")
    write_markdown(folder, merged, {"started": started, "seconds": total})
    return folder / "summary.md"


def relabel_folder(folder: Path, status=lambda msg: None, model: str = DEFAULT_MODEL) -> Path:
    """Rebuild transcript.md / summary.md from Gemini's saved answers. Only the small mic-only
    transcripts are requested again if they were not saved by the original run."""
    folder = Path(folder)
    mov = folder / "meeting.mov"
    chunk_dirs = sorted(folder.glob("chunks_*"), key=lambda d: d.stat().st_mtime, reverse=True)
    for d in chunk_dirs:
        parts = sorted(d.glob("part_*.mp4"))
        if parts and all((d / f"gemini_raw_{p.stem}.json").exists() for p in parts):
            break
    else:
        if not (folder / "gemini_raw_meeting_for_ai.json").exists():
            raise FileNotFoundError(f"no saved Gemini answers in {folder}; run the full AI step instead")
        parts = [folder / "meeting_for_ai.mp4"]
    status("Relabeling from saved Gemini answers")
    analysis = speakers.analyze_tracks(folder, mov)
    results, offsets, local_by_part, offset = [], [], {}, 0.0
    local, client = None, None
    try:
        for i, p in enumerate(parts):
            seconds = duration_s(p)
            result = parse_json((p.parent / f"gemini_raw_{p.stem}.json").read_text())
            drop_empty_notes(result)
            rescale_into_clip(result, seconds)
            validate_timestamps(result, seconds)
            results.append(result); offsets.append(offset)
            saved = p.parent / f"gemini_local_{p.stem}.json"
            if saved.exists():
                try:
                    texts = parse_local(saved.read_text())
                except ValueError as e:
                    log.warning("saved mic-only transcript %s unreadable (%s); ignoring it", saved.name, e)
                    texts = []
                local_by_part[i] = [] if looks_degenerate(texts) else texts
            else:
                local = local or build_local_voice(folder, mov, analysis)
                if local is not None:
                    client = client or gemini_client()
                    local_by_part[i] = transcribe_local(client, local, offset, seconds, model, saved)
            offset += seconds
    finally:
        if local is not None:
            local.unlink(missing_ok=True)
    total = duration_s(folder / "meeting_for_ai.mp4")
    has_tracks = bool(analysis.get("frames")) and "system" in (analysis.get("roles") or [])
    speaker_note = speakers.attribute_parts(folder, results, local_by_part if has_tracks else None,
                                            analysis.get("note", ""), names=local_names())
    merged = merge_parts(results, offsets)
    merged["speaker_note"] = speaker_note
    started = datetime.fromtimestamp(mov.stat().st_mtime - total).strftime("%Y-%m-%d %H:%M")
    write_markdown(folder, merged, {"started": started, "seconds": total})
    log.info("relabeled %s from %d saved part(s)", folder, len(parts))
    return folder / "summary.md"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_env()
    args = [a for a in sys.argv[1:] if a != "--relabel"]
    if len(args) != 1:
        print("usage: gemini_pass.py [--relabel] <meeting_folder>")
        sys.exit(2)
    if "--relabel" in sys.argv:
        out = relabel_folder(Path(args[0]), status=lambda m: log.info("STATUS %s", m))
    else:
        out = process_folder(Path(args[0]), status=lambda m: log.info("STATUS %s", m))
    import report_pdf    # imports this module, so only here
    report_pdf.safe_build(Path(args[0]), status=lambda m: log.info("STATUS %s", m))
    print(f"done: {out}")
