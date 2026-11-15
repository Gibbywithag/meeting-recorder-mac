"""Client video -> request list with exact screenshots.

A client sends a walkthrough/screen recording explaining what they want. Unlike a meeting,
it is short and detail-heavy, so Gemini reads it at high detail, gets the most recent meeting
summary as background, and ties every request to a frame WE extracted (Gemini's own timestamps
can run up to 1.6x fast, so its times are never used to pick screenshots).

CLI:  python video_pass.py <video file or folder containing client_video.*>
Output in the folder: summary.md (request list with screenshots), transcript.md, frames/*.jpg
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import gemini_pass as gp

log = logging.getLogger("video_pass")

MEETINGS = Path.home() / "Meetings"
MAX_FRAMES = 60           # images sent alongside the video
MIN_GAP_S = 3.0           # at most one frame every 3 s unless the screen changes
SCENE = 0.08              # ffmpeg scene-change score that counts as "the screen changed"
VIDEO_NAME = "client_video"

PROMPT = """A client sent this video about their app/product to the developer who builds it ("You").
Attachments: first the VIDEO, then numbered still FRAMES taken from it. Each frame is labelled
"FRAME n at mm:ss" with its exact time in the video.
{context}
Return ONLY a JSON object:
{{
  "summary": "<3-6 sentences: what the client wants overall and why>",
  "transcript": [{{"t": <seconds>, "speaker": "<name if established, else Client or Speaker 2>", "text": "<verbatim>"}}],
  "requests": [{{
      "frame": <number of the FRAME that best shows the screen this request is about>,
      "quote": "<the client's exact words for this request, or '' if only shown on screen>",
      "screen": "<which screen / area, as visible in that frame>",
      "current": "<what it looks like or does now>",
      "wanted": "<precisely what they want instead>",
      "priority": "high | normal | low | unspecified"
  }}],
  "conflicts": [{{"earlier": "<what the earlier meeting decided>", "now": "<what this video asks instead>"}}],
  "questions": ["<anything unclear the developer should ask the client>"],
  "action_items": [{{"what": "<task>", "owner": "You | Client | unspecified", "due": "<explicit date or unspecified>"}}]
}}
Rules: one request per distinct change, in the order they appear. Use the client's own words.
Read on-screen text carefully. Only report conflicts that are real contradictions of the background.
Never invent requests; if the video has no speech, derive requests only from clearly shown content.
"""


# ---------- pure helpers (unit tested) ----------

def mmss(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 60:02d}:{s % 60:02d}"


def parse_frame_times(showinfo_stderr: str) -> list[float]:
    """pts_time values from ffmpeg's showinfo filter output, in order."""
    return [float(x) for x in re.findall(r"pts_time:([\d.]+)", showinfo_stderr)]


def thin_times(times: list[float], max_frames: int = MAX_FRAMES) -> list[int]:
    """Indices of at most max_frames frames, spread evenly, always keeping first and last."""
    n = len(times)
    if n <= max_frames:
        return list(range(n))
    step = (n - 1) / (max_frames - 1)
    return sorted({round(i * step) for i in range(max_frames)})


def resolve_frame(request: dict, frame_count: int) -> int | None:
    """Gemini's frame number for a request, validated (1-based in the prompt)."""
    f = request.get("frame")
    if isinstance(f, bool) or not isinstance(f, (int, float)):
        return None
    f = int(f)
    return f if 1 <= f <= frame_count else None


def latest_meeting_summary(meetings: Path = MEETINGS, exclude: Path | None = None) -> Path | None:
    """Most recent real meeting (recorded by the app, so it has capture.json) with a summary."""
    best = None
    for d in meetings.glob("*") if meetings.exists() else []:
        if exclude and d.resolve() == exclude.resolve():
            continue
        s = d / "summary.md"
        if (d / "capture.json").exists() and s.exists() and not (d / f"{VIDEO_NAME}.json").exists():
            if best is None or s.stat().st_mtime > best.stat().st_mtime:
                best = s
    return best


def write_outputs(folder: Path, result: dict, frames: list[dict], meta: dict) -> Path:
    folder = Path(folder)
    lines = [f"# Client video — {meta['name']}", "",
             f"Length: {mmss(meta['seconds'])} · analyzed {meta['when']}", ""]
    if meta.get("speech") is False:
        lines += ["_This video has no speech. Requests are read from the screen only; nothing here is a quote._", ""]
    if meta.get("context"):
        lines += [f"_Background used: summary of the meeting in `{meta['context']}`._", ""]
    lines += ["## What they want", "", result.get("summary", "").strip() or "(no summary)", "",
              "## Requests", ""]
    requests = result.get("requests") or []
    if not requests:
        lines += ["- (none found)", ""]
    for i, r in enumerate(requests, 1):
        idx = resolve_frame(r, len(frames))
        title = (r.get("wanted") or r.get("quote") or "Request").strip().rstrip(".")
        lines += [f"### {i}. {title[:90]}", ""]
        if idx is not None:
            fr = frames[idx - 1]
            lines += [f"![{fr['file']}](frames/{fr['file']})", "", f"- **Screenshot:** at {mmss(fr['t'])} in the video"]
        if r.get("quote"):
            lines.append(f"- **They said:** \"{r['quote'].strip()}\"")
        for key, label in (("screen", "Where"), ("current", "Now"), ("wanted", "Wanted"), ("priority", "Priority")):
            if str(r.get(key, "")).strip():
                lines.append(f"- **{label}:** {str(r[key]).strip()}")
        lines.append("")
    lines += ["## Changes compared to earlier decisions", ""]
    lines += [f"- Earlier: {c.get('earlier', '')} → Now: {c.get('now', '')}" for c in result.get("conflicts") or []] or ["- (none)"]
    lines += ["", "## Questions to ask the client", ""]
    lines += [f"- {q}" for q in result.get("questions") or [] if str(q).strip()] or ["- (none)"]
    lines += ["", "## Action items", "", "| What | Owner | Due |", "|---|---|---|"]
    lines += [f"| {a.get('what', '')} | {a.get('owner', '')} | {a.get('due', '')} |" for a in result.get("action_items") or []] or ["| (none) | | |"]
    (folder / "summary.md").write_text("\n".join(lines) + "\n")

    t = [f"# Transcript — client video {meta['name']}", "",
         "_Times are approximate; screenshots in summary.md use exact frame times._", ""]
    t += [f"[{mmss(x.get('t', 0))}] **{x.get('speaker', 'Client')}:** {str(x.get('text', '')).strip()}"
          for x in result.get("transcript") or []] or ["(no speech)"]
    (folder / "transcript.md").write_text("\n".join(t) + "\n")
    log.info("wrote %s (%d requests) and transcript.md", folder / "summary.md", len(requests))
    return folder / "summary.md"


# ---------- media steps ----------

def prepare_folder(target: Path) -> tuple[Path, Path]:
    """Accept a video file (copied into a new ~/Meetings folder) or an existing folder."""
    target = Path(target).expanduser()
    if target.is_dir():
        videos = sorted(target.glob(f"{VIDEO_NAME}.*"))
        if not videos:
            raise FileNotFoundError(f"no {VIDEO_NAME}.* in {target}")
        return target, videos[0]
    if not target.exists():
        raise FileNotFoundError(target)
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", target.stem)[:40].strip("-") or "video"
    folder = MEETINGS / f"{datetime.now():%Y-%m-%d_%H%M%S}_video_{stem}"
    folder.mkdir(parents=True, exist_ok=False)
    video = folder / f"{VIDEO_NAME}{target.suffix.lower()}"
    shutil.copy2(target, video)
    (folder / "source.txt").write_text(str(target) + "\n")
    log.info("copied %s -> %s", target, video)
    return folder, video


def has_speech_audio(video: Path) -> bool:
    """False when the video has no audio stream or only silence. Gemini invents narration for
    silent videos, so this is checked in code, not left to the model."""
    import subprocess
    streams = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                              "-of", "csv=p=0", str(video)], capture_output=True, text=True).stdout.split()
    if not streams:
        log.info("video has no audio track")
        return False
    p = subprocess.run(["ffmpeg", "-v", "info", "-i", str(video), "-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(r"max_volume: (-?[\d.]+) dB", p.stderr)
    peak = float(m.group(1)) if m else -120.0
    log.info("audio peak %.1f dB", peak)
    return peak > -45.0


def silence_result(result: dict) -> dict:
    """Strip anything that claims someone spoke, for videos without speech."""
    result["transcript"] = []
    for r in result.get("requests") or []:
        r["quote"] = ""
    return result


def frame_gap(seconds: float) -> float:
    """Short clips get proportionally more screenshots: ~30 per clip up to one every 3 s."""
    return max(1.0, min(MIN_GAP_S, seconds / 30))


def extract_frames(video: Path, out_dir: Path) -> list[dict]:
    """Frames at every screen change, at most one per gap, plus the first; thinned to MAX_FRAMES."""
    out_dir.mkdir(exist_ok=True)
    for old in out_dir.glob("*.jpg"):
        old.unlink()
    tmp = out_dir / "_all"
    shutil.rmtree(tmp, ignore_errors=True); tmp.mkdir()
    gap = frame_gap(gp.duration_s(video))
    select = (f"select='eq(n\\,0)+gt(scene\\,{SCENE})*gte(t-prev_selected_t\\,{gap})"
              f"+gte(t-prev_selected_t\\,{gap * 2})',showinfo")
    import subprocess
    p = subprocess.run(["ffmpeg", "-v", "info", "-i", str(video), "-vf", select + ",scale='min(1920,iw)':-2",
                        "-vsync", "vfr", "-q:v", "3", str(tmp / "%04d.jpg")], capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg frame extraction failed: {p.stderr[-300:]}")
    times = parse_frame_times(p.stderr)
    files = sorted(tmp.glob("*.jpg"))
    n = min(len(times), len(files))
    frames = []
    for k, i in enumerate(thin_times(times[:n])):
        name = f"frame_{k + 1:03d}_{mmss(times[i]).replace(':', 'm')}s.jpg"
        files[i].rename(out_dir / name)
        frames.append({"n": k + 1, "t": times[i], "file": name})
    shutil.rmtree(tmp, ignore_errors=True)
    log.info("extracted %d frames (from %d candidates)", len(frames), n)
    return frames


def analyze(client, video: Path, frames_dir: Path, frames: list[dict], context: str, model: str, attempts: int = 3,
            speech: bool = True) -> dict:
    from google.genai import types
    seconds = gp.duration_s(video)
    detail = types.MediaResolution.MEDIA_RESOLUTION_HIGH if seconds <= 600 else types.MediaResolution.MEDIA_RESOLUTION_MEDIUM
    last_err = None
    for attempt in range(1, attempts + 1):
        up = None
        try:
            log.info("upload attempt %d (%.1f MB, %s)", attempt, video.stat().st_size / 1e6, detail.name)
            up = client.files.upload(file=str(video))
            deadline = time.monotonic() + 600
            while up.state.name == "PROCESSING":
                if time.monotonic() > deadline:
                    raise TimeoutError("Gemini video processing exceeded 10 minutes")
                time.sleep(4); up = client.files.get(name=up.name)
            if up.state.name != "ACTIVE":
                raise RuntimeError(f"file state {up.state.name}")
            parts = [types.Part.from_uri(file_uri=up.uri, mime_type=up.mime_type)]
            for fr in frames:
                parts.append(f"FRAME {fr['n']} at {mmss(fr['t'])}")
                parts.append(types.Part.from_bytes(data=(frames_dir / fr["file"]).read_bytes(), mime_type="image/jpeg"))
            no_speech = ("" if speech else "\nThis video has NO speech (no audio or only silence). Leave transcript empty, "
                         "set every quote to \"\", and derive requests only from what is clearly shown on screen.")
            parts.append(PROMPT.format(context=context) + f"\nThe video lasts {seconds:.1f} seconds. There are {len(frames)} frames." + no_speech
                         + (f"\nPrevious response failed validation: {last_err}. Fix it." if last_err else ""))
            cfg = types.GenerateContentConfig(
                response_mime_type="application/json", media_resolution=detail, temperature=0.2, max_output_tokens=32768,
                thinking_config=types.ThinkingConfig(thinking_level="LOW") if model.startswith("gemini-3") else None)
            resp = client.models.generate_content(model=model, contents=parts, config=cfg)
            (video.parent / "gemini_raw_video.json").write_text(resp.text or "")
            result = gp.parse_json(resp.text or "")
            if not isinstance(result.get("requests", []), list) or not isinstance(result.get("transcript", []), list):
                raise ValueError("requests and transcript must be lists")
            bad = [r.get("frame") for r in result.get("requests", []) if resolve_frame(r, len(frames)) is None]
            if bad and attempt < attempts:
                raise ValueError(f"requests point at frames that do not exist: {bad}")
            log.info("got %d requests, %d transcript lines", len(result.get("requests", [])), len(result.get("transcript", [])))
            return result
        except Exception as e:
            last_err = e
            log.exception("attempt %d failed: %s", attempt, e)
            time.sleep(4 * attempt)
        finally:
            if up is not None:
                try:
                    client.files.delete(name=up.name)
                except Exception as e:
                    log.warning("could not delete uploaded file %s: %s", up.name, e)
    raise RuntimeError(f"Gemini failed after {attempts} attempts: {last_err}")


def process_video(target: Path, status=lambda m: None, model: str = gp.DEFAULT_MODEL) -> Path:
    status("Preparing video")
    folder, video = prepare_folder(target)
    (folder / f"{VIDEO_NAME}.json").write_text(json.dumps({"kind": "client video", "file": video.name}))
    status("Picking screenshots")
    frames = extract_frames(video, folder / "frames")
    ctx_path = latest_meeting_summary(exclude=folder)
    context = ""
    if ctx_path:
        context = ("\nBACKGROUND — summary of the most recent meeting with this client (for context and to spot "
                   "changed decisions; do not repeat it as requests):\n" + ctx_path.read_text()[:12000] + "\n")
        log.info("context: %s", ctx_path)
    speech = has_speech_audio(video)
    status("Gemini working (1/1)")
    result = analyze(gp.gemini_client(), video, folder / "frames", frames, context, model, speech=speech)
    if not speech:
        result = silence_result(result)
    status("Writing files")
    out = write_outputs(folder, result, frames, {
        "name": (folder / "source.txt").read_text().strip().split("/")[-1] if (folder / "source.txt").exists() else video.name,
        "seconds": gp.duration_s(video), "when": f"{datetime.now():%Y-%m-%d %H:%M}", "speech": speech,
        "context": ctx_path.parent.name if ctx_path else ""})
    print(f"folder: {folder}", flush=True)
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    gp.load_env()
    if len(sys.argv) != 2:
        print("usage: video_pass.py <video file | folder with client_video.*>")
        sys.exit(2)
    print(f"done: {process_video(Path(sys.argv[1]), status=lambda m: log.info('STATUS %s', m))}")
