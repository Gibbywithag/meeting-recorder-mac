"""Rolling transcription: analyze a meeting WHILE it records, so Stop only has the last minutes left.

Started by the app when recording begins:  live_pass.py <meeting_folder>
It cuts each finished section out of the growing meeting.mov (AVAssetWriter writes it in fragments, so
it is readable mid-recording) and runs the same steps as gemini_pass on that section. After each section
transcript.md and summary.md are rewritten, so "so far" notes can be opened during the meeting.
The app creates live/recording_done after the file is finalized; the worker then does the rest and exits.

It only ever READS meeting.mov and runs at low priority in its own process: if it fails or dies the
recording is unaffected, and a non-zero exit makes the app run the full gemini_pass instead.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import gemini_pass as gp
import report_pdf
import speakers
from transcript_quality import segment_context

log = logging.getLogger("live_pass")

SEGMENT_S = float(os.environ.get("LIVE_SEGMENT_S", 5 * 60))
MIN_TAIL_S = 2 * 60     # never leave a tiny last section: Gemini's timestamps drift worst on those
SAFETY_S = 10           # the newest fragment (written every 5 s) may be incomplete
POLL_S = 1.0
PROBE_EVERY_S = 15.0


def next_segment(start: float, available: float, finished: bool,
                 seg: float = SEGMENT_S, min_tail: float = MIN_TAIL_S) -> tuple[float, float] | None:
    """The next section to process as (start, length), or None to keep waiting / nothing left."""
    left = available - start
    if left >= seg + min_tail:
        return start, seg
    if finished and left > 0.5:
        return start, left
    return None


def cut_segment(mov: Path, start: float, seconds: float, out: Path) -> Path:
    """Frame-accurate cut, decoding rather than stream-copying: a copy would snap to the previous
    video keyframe (up to 5 s) and sections would overlap or leave gaps. PCM keeps the audio lossless."""
    gp.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{seconds:.3f}", "-i", str(mov),
            "-map", "0:v:0", "-map", "0:a", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-r", "5",
            "-c:a", "pcm_s16le", str(out)])
    return out


def recording_ended_without_app(parent: int) -> bool:
    """The app died mid-recording: stop quietly; Retry AI can process whatever was saved."""
    return os.getppid() != parent


class LiveRun:
    def __init__(self, folder: Path, analyze=None, transcribe=None, model: str = gp.DEFAULT_MODEL,
                 segment_s: float = SEGMENT_S, min_tail_s: float = MIN_TAIL_S, safety_s: float = SAFETY_S,
                 probe_every_s: float = PROBE_EVERY_S):
        self.folder = Path(folder)
        self.mov = self.folder / "meeting.mov"
        self.dir = self.folder / "live"
        self.dir.mkdir(exist_ok=True)
        self.done_flag = self.dir / "recording_done"
        self.model = model
        self.segment_s, self.min_tail_s, self.safety_s, self.probe_every_s = segment_s, min_tail_s, safety_s, probe_every_s
        self.client = None
        self._analyze = analyze
        self._transcribe = transcribe
        self.segments: list[dict] = []   # in order: {start, seconds, result, local} (result None = failed)
        self.mic_gain = 0.0

    # ----- one section -----
    def process(self, index: int, start: float, seconds: float, context: str) -> dict:
        d = self.dir / f"part_{index:03d}"
        d.mkdir(exist_ok=True)
        meta = self.folder / "capture.json"
        if meta.exists():                # track roles and window layout are read from here
            shutil.copy(meta, d / "capture.json")
        seg = cut_segment(self.mov, start, seconds, d / "meeting.mov")
        analysis = speakers.analyze_tracks(d, seg)
        # A section where only one side talked can't measure the boost: keep the last good one.
        if analysis.get("mic_gain_db"):
            self.mic_gain = analysis["mic_gain_db"]
        else:
            analysis["mic_gain_db"] = self.mic_gain
        local = gp.build_local_voice(d, seg, analysis)
        has_tracks = bool(local)
        mp4 = gp.prep_for_ai(seg, analysis, local)
        clip_seconds = gp.duration_s(mp4)
        if self._analyze is None and self.client is None:
            self.client = gp.gemini_client()
        analyze = self._analyze or (lambda p, ctx: gp.analyze_part(self.client, p, self.model, context=ctx))
        result = analyze(mp4, context)
        local_texts = None
        if has_tracks:
            transcribe = self._transcribe or (lambda path, secs, out: gp.transcribe_local(self.client, path, 0.0, secs, self.model, out))
            local_texts = transcribe(local, clip_seconds, d / "gemini_local.json")
        (d / "result.json").write_text(json.dumps({"start": start, "seconds": seconds, "result": result,
                                                   "local": local_texts, "note": analysis.get("note", "")}, indent=1))
        for leftover in ("meeting.mov", "meeting_for_ai.mp4", "meeting_for_ai_audio.m4a", ".local_voice.wav"):
            (d / leftover).unlink(missing_ok=True)   # regenerable and large; the answers are what we keep
        return {"start": start, "seconds": seconds, "result": result, "local": local_texts, "note": analysis.get("note", "")}

    def run_segment(self, index: int, start: float, seconds: float) -> dict:
        previous = [s for s in self.segments[:index] if s.get("result")]
        context = segment_context(previous[-1]["result"]) if previous else ""
        try:
            return self.process(index, start, seconds, context)
        except Exception as e:     # a bad section or a network drop must not end the rolling run
            log.exception("section %d (%.0f-%.0fs) failed, will retry at the end: %s", index + 1, start, start + seconds, e)
            return {"start": start, "seconds": seconds, "result": None, "local": None, "note": ""}

    # ----- output -----
    def write_outputs(self) -> int:
        """Rewrite transcript.md / summary.md from every section so far. Copies the answers because
        attribute_parts renames speakers in place and later passes need Gemini's original labels."""
        good = [s for s in self.segments if s.get("result")]
        if not good:
            return 0
        results = [copy.deepcopy(s["result"]) for s in good]
        any_tracks = any(s.get("local") is not None for s in good)
        local_by_part = {i: s["local"] for i, s in enumerate(good) if s.get("local") is not None} if any_tracks else None
        note = next((s["note"] for s in good if s.get("note")), "")
        speaker_note = speakers.attribute_parts(self.folder, results, local_by_part, note, names=gp.local_names())
        merged = gp.merge_parts(results, [s["start"] for s in good])
        merged["speaker_note"] = speaker_note
        total = good[-1]["start"] + good[-1]["seconds"]
        started = datetime.fromtimestamp(self.mov.stat().st_mtime - total).strftime("%Y-%m-%d %H:%M")
        gp.write_markdown(self.folder, merged, {"started": started, "seconds": total})
        return len(good)

    # ----- the loop -----
    def run(self, status=lambda m: None, parent: int | None = None) -> Path:
        start, last_probe, available = 0.0, 0.0, 0.0
        status("Transcribing live")
        while True:
            finished = self.done_flag.exists()
            if parent is not None and not finished and recording_ended_without_app(parent):
                raise RuntimeError("the app stopped while recording; use Retry AI")
            now = time.monotonic()
            if finished or now - last_probe >= self.probe_every_s:
                last_probe = now
                if self.mov.exists():
                    try:
                        available = gp.duration_s(self.mov)
                    except Exception as e:      # first seconds of a recording aren't readable yet
                        log.info("recording not readable yet: %s", e)
                        available = 0.0
                    if not finished:
                        available -= self.safety_s
            step = next_segment(start, available, finished, self.segment_s, self.min_tail_s)
            if step is None:
                if finished:
                    break
                time.sleep(POLL_S)
                continue
            index = len(self.segments)
            status(f"{'Finishing' if finished else 'Transcribing live'}: section {index + 1}")
            self.segments.append(self.run_segment(index, *step))
            start = step[0] + step[1]
            try:
                self.write_outputs()
            except Exception as e:      # the "so far" notes are a bonus; the final write below still decides
                log.exception("could not write notes so far: %s", e)
        for i, s in enumerate(self.segments):      # second chance for anything that failed mid-meeting
            if not s.get("result"):
                status(f"Retrying section {i + 1}")
                self.segments[i] = self.run_segment(i, s["start"], s["seconds"])
        missing = [i + 1 for i, s in enumerate(self.segments) if not s.get("result")]
        if missing or not self.segments:
            raise RuntimeError(f"sections not transcribed: {missing or 'none produced'}")
        status("Writing files")
        self.write_outputs()
        return self.folder / "summary.md"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) != 2:
        print("usage: live_pass.py <meeting_folder>")
        sys.exit(2)
    os.nice(10)     # ffmpeg children inherit this: the call and the recording always come first
    gp.load_env()
    out = LiveRun(Path(sys.argv[1])).run(status=lambda m: log.info("STATUS %s", m), parent=os.getppid())
    report_pdf.safe_build(Path(sys.argv[1]), status=lambda m: log.info("STATUS %s", m))
    print(f"done: {out}")
