# Meeting Recorder

Menu-bar Mac app that records a Zoom/FaceTime meeting (screen + the other person's voice + your mic),
then Gemini writes `transcript.md` and `summary.md` into `~/Meetings/<date_time>/`.

## Install on your Mac

You need: a Mac on **macOS 15 or newer**, and about 10 minutes.

1. Install the tools (skip any you already have):
   - Apple's command line tools: `xcode-select --install`
   - [Homebrew](https://brew.sh), then: `brew install ffmpeg python@3.13`
   - Google Chrome (only used to print the finished PDF of each meeting)
2. Get the code and build the app:

       git clone <this repo's URL> meeting-recorder
       cd meeting-recorder
       ./build_app.sh

   This puts **Meeting Recorder** in your Applications folder.
3. Get your own Gemini API key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey).
   No key is included in this repo. Each person uses their own, and Google bills that person for usage.
4. Open Meeting Recorder from Applications and press **Start recording**. The first time, it asks for your
   key: paste it and press Save. It is stored only on your Mac, in
   `~/Library/Application Support/MeetingRecorder/.env` (that file also holds the optional settings
   shown in `.env.example`, like your name for the PDF).
5. macOS asks for **Screen & System Audio Recording** and **Microphone** permission. Allow both, then quit
   and reopen the app.

Without an Apple developer certificate the app is signed "ad-hoc", so macOS may ask for permission
again after each rebuild. If it says "declined": `tccutil reset ScreenCapture com.gibby.meetingrecorder`,
reopen the app and allow again. To update later: `git pull && ./build_app.sh` (quit the app first).

## Using it

1. Click the camera icon in the menu bar. Tell everyone you're recording, press **Start recording**.
   - The panel shows a live view of what's recorded, plus meters for your mic and the call audio.
   - A meter that says **no signal** in red means that source isn't being captured.
   - Switch screen or window any time from **Recording now** (for example when someone shares their screen).
2. Press **Stop recording** when done. You can start the next recording right away; the AI works in the background
   (it has already processed most of the meeting while it was recording).
3. When it's done, **Summary** opens the finished PDF: who does what, the summary, and the full transcript.
   It is also saved as `~/Meetings/Meeting Notes - <date time>.pdf`. Skim the speaker names before sending it:
   when two people share one microphone the app can mix them up.
4. **Open folder** shows the recording (`meeting.mov`), `summary.md`, `transcript.md` and the log.

The app keeps the Mac awake while recording and processing, but closing the lid on battery can still
interrupt the upload; press **Retry AI** if that happens. The recording itself is always safe.

## How it's built

- `./build_app.sh` builds the app into /Applications. The AI scripts ship inside the app; its Python env and
  settings live in `~/Library/Application Support/MeetingRecorder`. It opens at login (checkbox in the panel).
- `./setup.sh` sets up the `.venv` used by the tests.

## Who said what (speaker labels)

The app records your mic and the call audio on separate tracks. After the meeting:

1. A clean "you only" track is built from your mic: boosted, and muted whenever the call audio is active
   (so the other person's voice from your speakers is removed). It is mixed into what Gemini hears, so a
   quiet mic (lid closed, far away) still gets transcribed.
2. Gemini transcribes that you-only track separately. Lines of the main transcript whose words appear in it,
   in order, are **You**. A Gemini voice that matches is You throughout that chunk.
3. If few words match, the voice whose amount of speech is closest to what your mic heard is You. Your name
   (`LOCAL_SPEAKER_NAMES` in `.env`) only breaks near-ties. With one other voice it's **Client**; in group meetings people keep the names Gemini heard.

No timestamps are used for this: Gemini's timestamps can run up to 1.6x fast. The first line of `summary.md`
says how it went, and `speakers.json` has the per-chunk evidence.

Limits: if the other person is in the same room, both voices reach both tracks, so short replies can still be
mislabelled. With nobody on your mic (only call audio), nobody is labelled You.

Re-label an old meeting after an improvement, without redoing the AI step:

    ./.venv/bin/python gemini_pass.py --relabel ~/Meetings/<folder>

## Screen capture

- Every launch starts on a **whole screen** (the last screen you used, or the main screen if it's unplugged).
  Window modes are still there, but call apps often replace their window mid-call.
- If the recorded window or screen disappears, video reconnects to a screen within a second or two and the
  same recording continues. Audio never stops. The status line says when this happened.

## Client videos

When a client sends a video instead of meeting live: click **Analyze a client video…** in the panel, or drag
the video onto the app icon / right-click it and choose **Open With → Meeting Recorder**. From Terminal:

    ./.venv/bin/python video_pass.py ~/Downloads/their-video.mp4

The video is copied into a new `~/Meetings/<date>_video_<name>/` folder. `summary.md` lists every request with
the client's exact words, where on screen, what it is now, what they want, and a screenshot from `frames/`.
It also flags changes compared to your most recent recorded meeting (sent along as background) and lists
questions to ask the client.

- Screenshots are picked by the app at every screen change (and every few seconds), not by Gemini, so each one
  is the exact frame; Gemini only says which numbered frame a request is about.
- Videos with no speech are detected and labelled; nothing in them is presented as a quote.
- **Retry AI** re-runs the analysis on the same video.

## If something fails

- The recording is always safe at `~/Meetings/<stamp>/meeting.mov`.
- Re-run only the AI part: `./.venv/bin/python gemini_pass.py ~/Meetings/<stamp>`
- Everything is logged in `~/Meetings/<stamp>/log.txt`.
- Live transcription: `live_pass.py` runs at low priority while recording, processing each finished 5-minute section (never leaving a last section under 2 minutes), so Stop only has the tail left. `summary.md` and `transcript.md` hold notes so far during the meeting. If it fails, the app runs the full `gemini_pass.py` automatically.
- Model: `gemini-3-flash-preview` by default; set `GEMINI_MODEL=` in `.env` to change.

## Tests

- `./.venv/bin/python -m pytest -q` also runs `tests/test_speakers.py` (track-based speaker labels)
- `tests/recovery_smoke.swift` — records a real TextEdit window, quits TextEdit, requires video to reconnect (build command at top of the file)

- `./.venv/bin/python -m pytest -q` — merge/markdown logic (mocked Gemini)
- `./tests/integration_app.sh` — launches the app, records, stops, waits for summary.md (uses SIGUSR1 = Start/Stop, SIGUSR2 = open panel)

## Native menu bar app

Run `./build_app.sh`, then open Meeting Recorder from Applications. Each recording has a unique
folder. Use the **Record** menu to select a connected screen (including an external
monitor) or a specific window. Click Refresh after connecting a monitor or
opening a new window. Screen capture includes all visible windows on the selected
display. Window capture follows only the selected window. The live preview shows
the selected source. While recording, **Recording now** switches to another screen or
window in the same file. Unavailable/disconnected selections fail explicitly instead of silently
recording another display. Microphone and system audio remain independent of the
selected video source, so recording a browser window still captures FaceTime audio.

AI analysis starts after Stop. Speakers are inferred from audible voices and
introductions; uncertain identity is marked Unknown. Additional remote voices
can be labeled Client 2, Client 3, or a clearly spoken name. Introduce yourselves
at the start of the call and review labels before relying on assigned actions.
Long recordings carry recent topic/speaker context between segments. Original
microphone and system audio remain separate tracks in meeting.mov.

### Capture several windows together

Click the **Multiple windows** tab at the top, check the meeting/design windows,
then press Start. Their live contents are recorded together in a fixed grid, even
if the windows overlap on the desktop. Windows can be selected from different
displays. The AI receives the combined grid at 1080p; microphone and system audio
are both included. Keep the chosen windows open. To include new windows, stop and
start another recording with an updated selection, or choose a whole screen.

### Recording controls

The Screen, One window, and Multiple windows tabs are always visible. Multi-window
mode includes window/app search and remembers your choices across restarts. Closed
or unavailable selected windows must be removed or refreshed before recording.
The pin button keeps the panel open while you work. After Stop, Summary opens the
result directly; Retry AI reruns analysis on the saved recording without recording
another meeting.

Selecting a screen or checking windows now shows a preview before Start. It
refreshes about once a second and is labeled PREVIEW · NOT RECORDING. Multi-window
preview uses the same grid as the saved video. Previewing does not write a recording,
capture microphone audio unless you start an audio test, or send media to Gemini. Use **Test audio before recording** to activate meters without saving anything.

Native selection regression tests: `swift test --package-path app`.

### Check and choose audio inputs

Click **Test audio before recording**, then speak and play audio from FaceTime or
another app. Each selected input has its own meter. Click **Stop audio test** when
done, or press Start recording to switch from testing to recording.

Expand **Audio sources** to choose system audio plus up to three microphones or
available input devices. System audio includes sound from all apps. Choose explicit
inputs to use several together; the automatic Mac microphone option is exclusive
so it does not duplicate a selected physical microphone. iPhone/Continuity microphones
are excluded, and the automatic option explicitly selects the Mac's built-in mic
rather than the system's possibly phone-routed default.

Audio testing creates no recording and uploads nothing. Recording preserves each
selected input as a separate audio track; AI preparation mixes all tracks. An input
can legitimately be silent until sound reaches it (for example a virtual loopback).

### Transcript quality

Meetings longer than ten minutes are analyzed in roughly ten-minute clips, with
clip-relative timestamps and extra output room for verbatim turns. Segment context
contains speaker labels only, so earlier quotes and timestamps are not replayed.
Unknown participant roles use neutral speaker labels; names require spoken evidence.
Summaries separate agreed decisions, unconfirmed proposals, open questions, and
explicit action items. Unclear owners and deadlines stay unspecified.

Malformed results, invalid timestamps, and long exact repeated passages are rejected
and retried rather than silently published or deleted. Short repeated replies remain.
These checks validate structure and obvious repetition, not word accuracy or speaker
identity; review important commitments against the original recording.
