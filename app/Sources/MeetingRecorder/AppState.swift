import Foundation
import SwiftUI
import AppKit
import UniformTypeIdentifiers
import ServiceManagement

/// One AI analysis (meeting or client video). Runs in the background; recording is never blocked by it.
struct AIJob: Identifiable, Equatable {
    enum State: Equatable { case running, done, failed }
    let folder: URL
    var status: String
    var state: State = .running
    var id: String { folder.path }
    var name: String { folder.lastPathComponent }
}

@MainActor
final class AppState: ObservableObject {
    enum Phase: Equatable { case idle, starting, recording, processing, done, failed }

    @Published var captureSources: [CaptureSource] = []
    @Published var selectedSourceID = "main" {
        didSet { UserDefaults.standard.set(selectedSourceID, forKey: "captureSourceID"); updatePreview() }
    }
    @Published var selectedWindowIDs: Set<String> = [] {
        didSet { UserDefaults.standard.set(Array(selectedWindowIDs), forKey: "captureWindowIDs"); updatePreview() }
    }
    @Published var panelPinned = false
    @Published var sourceError: String?
    @Published var refreshingSources = false
    @Published var phase: Phase = .idle { didSet { updatePreview(); updateSleepGuard() } }
    @Published var elapsed: TimeInterval = 0
    @Published var audioSources = AudioSource.available()
    @Published var selectedMicIDs: Set<String> = ["default"]
    @Published var includeSystemAudio = true
    @Published var useCoreAudio = true
    @Published var isAudioTesting = false
    @Published var audioTestStarting = false
    @Published var audioTestDetected: Set<String> = []
    @Published var audioTestStatus = "Test your inputs before recording."
    @Published var inputLevels: [String: Float] = [:]
    @Published var micLevel: Float = 0
    @Published var sysLevel: Float = 0
    @Published var micSilentSince: Date? = nil
    @Published var sysSilentSince: Date? = nil
    @Published var preview: CGImage? = nil
    @Published var previewStatus = "Select a screen or windows to preview."
    @Published var status: String = "Ready"
    @Published var folder: URL? = nil
    @Published var opensAtLogin = false
    @Published var jobs: [AIJob] = [] { didSet { updateSleepGuard() } }
    @Published var switchingSource = false
    var jobsRunning: Bool { jobs.contains { $0.state == .running } }

    /// The AI scripts (gemini_pass.py etc.), shipped inside the app at Resources/engine by build_app.sh.
    let engineDir: URL
    /// ~/Library/Application Support/MeetingRecorder: the Python .venv and the .env (API key, names).
    let supportDir: URL
    private let engine = CaptureEngine()
    private let audioMonitor = CaptureEngine()
    private let previewCapture = PreviewCapture()
    private let previewEnabled: Bool
    private var previewTask: Task<Void, Never>?
    private var timer: Timer?
    private var startDate: Date?
    private var logHandle: FileHandle?
    private let fmt = ISO8601DateFormatter()
    private var testSignal: DispatchSourceSignal?

    init(previewEnabled: Bool = true) {
        self.previewEnabled = previewEnabled
        supportDir = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("MeetingRecorder")
        if let bundled = Bundle.main.resourceURL?.appendingPathComponent("engine"),
           FileManager.default.fileExists(atPath: bundled.appendingPathComponent("gemini_pass.py").path) {
            engineDir = bundled
        } else {
            // `swift run` during development: no bundle, use the source folder.
            engineDir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("meeting-recorder")
        }
        // Every launch starts on a whole screen: window IDs change when a call app redraws its
        // window, which ended several recordings early. Window modes stay one click away.
        let saved = UserDefaults.standard.string(forKey: "captureSourceID") ?? "main"
        if saved.hasPrefix("windows:"), let ids = try? WindowLayout.ids(from: saved) {
            selectedWindowIDs = Set(ids.map { "window:\($0)" })
        } else {
            selectedWindowIDs = Set(UserDefaults.standard.stringArray(forKey: "captureWindowIDs") ?? [])
        }
        selectedSourceID = (saved == "main" || saved.hasPrefix("display:")) ? saved : "main"
        engine.log = { [weak self] msg in Task { @MainActor in self?.log(msg) } }
        audioMonitor.onLevels = { [weak self] levels in
            guard let self, self.isAudioTesting else { return }
            self.applyLevels(levels)
        }
        audioMonitor.onError = { [weak self] msg in
            guard let self else { return }
            self.audioTestStatus = "Audio test failed: \(msg)"
            Task { _ = await self.audioMonitor.stop(); self.isAudioTesting = false }
        }
        engine.onLevels = { [weak self] l in self?.applyLevels(l) }
        engine.onPreview = { [weak self] img in
            guard let self, self.isRecording || self.phase == .starting else { return }
            self.preview = img
        }
        engine.onWarning = { [weak self] msg in self?.status = msg }
        engine.onError = { [weak self] msg in
            guard let self else { return }
            guard self.phase == .recording || self.phase == .starting else { return }
            self.timer?.invalidate(); self.timer = nil
            self.phase = .processing
            Task {
                _ = await self.engine.stop()
                self.status = "Capture error: \(msg). Recording preserved in the meeting folder."
                self.phase = .failed
                if let dir = self.folder, let live = self.liveRuns.removeValue(forKey: dir) {
                    await self.finishAnalysis(dir, live: live)
                }
            }
        }
        // Load screens/windows at launch so the picker never shows a stale placeholder.
        Task { [weak self] in await self?.refreshSources() }
        // Test hook: `kill -USR1 <pid>` toggles recording, same as pressing the button (used by tests/integration_app.sh).
        signal(SIGUSR1, SIG_IGN)
        let src = DispatchSource.makeSignalSource(signal: SIGUSR1, queue: .main)
        src.setEventHandler { [weak self] in self?.log("SIGUSR1 received: toggle"); self?.toggle() }
        src.resume()
        testSignal = src
    }

    var isRecording: Bool { phase == .recording }
    var isBusy: Bool { phase == .starting || phase == .processing || audioTestStarting }
    func selectMicrophone(_ id: String, selected: Bool) {
        guard !isRecording, !isBusy, !isAudioTesting else { return }
        if selected {
            if id == "default" { selectedMicIDs = ["default"] }
            else { selectedMicIDs.remove("default"); selectedMicIDs.insert(id) }
        } else { selectedMicIDs.remove(id) }
    }
    func toggleAudioTest() {
        guard !isRecording, !isBusy else { return }
        audioTestStarting = true
        let stopping = isAudioTesting
        let ids = selectedMicIDs.sorted(); let system = includeSystemAudio; let core = useCoreAudio
        Task {
            if stopping {
                _ = await audioMonitor.stop(); isAudioTesting = false
                micLevel = 0; sysLevel = 0; inputLevels = [:]
                audioTestStatus = "Audio test stopped."
            } else {
                audioTestDetected = []; isAudioTesting = true; audioTestStatus = "Audio test only — nothing is saved. Speak and play audio."
                do { try await audioMonitor.startAudioMonitoring(micDeviceIDs: ids, includeSystemAudio: system, useCoreAudio: core) }
                catch { _ = await audioMonitor.stop(); isAudioTesting = false; audioTestStatus = "Audio test failed: \(error.localizedDescription)" }
            }
            audioTestStarting = false
        }
    }
    var captureSourceID: String {
        selectedSourceID == "multiple" ? "windows:" + selectedWindowIDs.sorted().map { String($0.dropFirst(7)) }.joined(separator: ",") : selectedSourceID
    }
    func updatePreview() {
        previewTask?.cancel(); previewTask = nil
        guard previewEnabled, !isRecording, phase != .starting, phase != .processing else { return }
        preview = nil
        if !isAudioTesting { micLevel = 0; sysLevel = 0; inputLevels = [:] }
        if selectedSourceID == "multiple", selectedWindowIDs.isEmpty {
            previewStatus = "Check windows to see their combined preview."; return
        }
        let source = captureSourceID
        previewStatus = "Loading selected source…"
        previewTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                do {
                    let image = try await self.previewCapture.image(sourceID: source)
                    guard !Task.isCancelled, !self.isRecording, self.phase != .starting, self.phase != .processing, self.captureSourceID == source else { return }
                    self.preview = image
                    self.previewStatus = "Preview only — recording has not started."
                } catch {
                    guard !Task.isCancelled else { return }
                    self.preview = nil; self.previewStatus = error.localizedDescription
                }
                do { try await Task.sleep(nanoseconds: 1_000_000_000) } catch { return }
            }
        }
    }
    var captureMode: String {
        selectedSourceID == "multiple" ? "multiple" : (selectedSourceID.hasPrefix("window:") || selectedSourceID == "single" ? "window" : "screen")
    }
    func setCaptureMode(_ mode: String) {
        guard !isRecording, !isBusy else { return }
        switch mode {
        case "multiple": selectedSourceID = "multiple"
        case "window": selectedSourceID = captureSources.first(where: { $0.id.hasPrefix("window:") })?.id ?? "single"
        default: selectedSourceID = "main"
        }
    }
    var unavailableWindowIDs: Set<String> {
        selectedWindowIDs.filter { id in !captureSources.contains(where: { $0.id == id }) }
    }
    func removeUnavailableWindows() {
        guard !isRecording, !isBusy, !refreshingSources else { return }
        selectedWindowIDs.subtract(unavailableWindowIDs)
        phase = .idle; status = "Ready"; elapsed = 0
    }
    var selectionProblem: String? {
        if selectedSourceID == "multiple" {
            if selectedWindowIDs.isEmpty { return "Check the windows you want to record." }
            if selectedWindowIDs.count > WindowLayout.maximumWindows { return "Select at most four windows." }
            if selectedWindowIDs.contains(where: { id in !captureSources.contains(where: { $0.id == id }) }) {
                return "A selected window is unavailable. Refresh or remove it."
            }
        } else if selectedSourceID.hasPrefix("window:"), !captureSources.contains(where: { $0.id == selectedSourceID }) {
            return "Choose an available screen or window."
        }
        // A disconnected screen is not a blocker: capture falls back to the main display.
        return nil
    }
    var summaryAvailable: Bool {
        folder.map { FileManager.default.fileExists(atPath: $0.appendingPathComponent("summary.md").path) } ?? false
    }
    var canRetryAnalysis: Bool {
        !isRecording && !isBusy && !(jobs.contains { $0.folder == folder && $0.state == .running }) && (folder.map { isVideoFolder($0) || FileManager.default.fileExists(atPath: $0.appendingPathComponent("meeting.mov").path) } ?? false)
    }
    private func isVideoFolder(_ url: URL) -> Bool {
        FileManager.default.fileExists(atPath: url.appendingPathComponent("client_video.json").path)
    }

    /// Pick a video the client sent; copy it into a new ~/Meetings folder and analyze it for requests.
    func addVideo() {
        guard !isRecording, !isBusy else { return }
        let panel = NSOpenPanel()
        panel.title = "Choose the client's video"
        panel.allowedContentTypes = [.movie, .video, .mpeg4Movie, .quickTimeMovie]
        panel.allowsMultipleSelection = false
        NSApp.activate(ignoringOtherApps: true)
        guard panel.runModal() == .OK, let source = panel.url else { return }
        analyzeVideo(source)
    }

    func analyzeVideo(_ source: URL) {
        let stamp = { let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd_HHmmss"; return f.string(from: Date()) }()
        let safe = source.deletingPathExtension().lastPathComponent
            .replacingOccurrences(of: "[^A-Za-z0-9_-]+", with: "-", options: .regularExpression).prefix(40)
        let dir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Meetings/\(stamp)_video_\(safe)")
        let dest = dir.appendingPathComponent("client_video." + (source.pathExtension.isEmpty ? "mp4" : source.pathExtension.lowercased()))
        folder = dir
        upsertJob(dir, status: "Copying \(source.lastPathComponent)…")
        Task {
            do {
                try await Task.detached {
                    try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
                    try FileManager.default.copyItem(at: source, to: dest)
                    try (source.path + "\n").write(to: dir.appendingPathComponent("source.txt"), atomically: true, encoding: .utf8)
                }.value
            } catch {
                upsertJob(dir, status: "Could not copy the video: \(error.localizedDescription)", state: .failed); return
            }
            folderLog(dir, "client video: \(source.path) -> \(dest.path)")
            upsertJob(dir, status: "Analyzing the client video…")
            await runGeminiPass(folder: dir)
        }
    }
    func openSummary() { if let folder { openNotes(in: folder) } }
    /// The finished PDF when it exists, otherwise the plain summary (older meetings, or the PDF step failed).
    private func openNotes(in folder: URL) {
        let pdf = folder.appendingPathComponent("Meeting Notes.pdf")
        NSWorkspace.shared.open(FileManager.default.fileExists(atPath: pdf.path) ? pdf : folder.appendingPathComponent("summary.md"))
    }
    func retryAnalysis() {
        guard canRetryAnalysis, let folder else { return }
        retry(folder)
    }
    func retry(_ folder: URL) {
        guard !jobs.contains(where: { $0.folder == folder && $0.state == .running }) else { return }
        upsertJob(folder, status: "Retrying AI analysis…")
        Task { await runGeminiPass(folder: folder) }
    }
    var elapsedText: String {
        let s = Int(elapsed); return String(format: "%02d:%02d:%02d", s / 3600, (s % 3600) / 60, s % 60)
    }
    var micSilent: Bool { silent(micSilentSince) }
    var sysSilent: Bool { silent(sysSilentSince) }
    private func silent(_ since: Date?) -> Bool { isRecording && (since.map { Date().timeIntervalSince($0) > 10 } ?? false) }

    // MARK: - Actions

    func refreshSources() async {
        guard !isBusy, !refreshingSources else { return }
        refreshingSources = true
        defer { refreshingSources = false }
        do {
            captureSources = try await CaptureSource.available()
            updatePreview()
            sourceError = nil
            // Keep stale selections visible until the user explicitly picks a replacement.
        } catch {
            sourceError = "Could not list screens/windows: \(error.localizedDescription)"
        }
    }

    func toggle() { isRecording ? stop() : start() }

    /// Change what's being recorded without stopping: same file, audio untouched.
    func switchSource(to id: String) {
        guard isRecording, !switchingSource, id != engine.currentSourceID else { return }
        switchingSource = true
        status = "Switching the recording…"
        Task {
            do {
                try await engine.switchSource(to: id)
                let name = captureSources.first { $0.id == id }?.name ?? (id == "main" ? "Main screen" : id)
                status = "Recording \(name)"
            } catch {
                log("switch to \(id) failed: \(error.localizedDescription)")
                status = "Could not switch: \(error.localizedDescription) Still recording the previous source."
            }
            switchingSource = false
        }
    }
    var liveSourceID: String { engine.currentSourceID }

    func start() {
        guard !isRecording, !isBusy else { return }
        if let problem = selectionProblem { status = problem; return }
        if selectedMicIDs.isEmpty && !includeSystemAudio { status = "Select at least one audio source."; return }
        if let problem = keyProblem() {
            // First run on a new Mac: ask for the key here instead of sending people to a settings file.
            log("no Gemini key: \(problem)")
            guard askForKey(), keyProblem() == nil else { status = "Add a Gemini API key to record (press Start again)."; return }
        }
        let stamp = { let f = DateFormatter(); f.dateFormat = "yyyy-MM-dd_HHmmss"; return f.string(from: Date()) }()
        let dir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Meetings/\(stamp)_\(UUID().uuidString.prefix(8))")
        do { try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true) }
        catch { status = "Cannot create \(dir.path): \(error.localizedDescription)"; phase = .failed; return }
        folder = dir
        openLog(dir)
        preview = nil; micLevel = 0; sysLevel = 0; elapsed = 0
        micSilentSince = Date(); sysSilentSince = Date()
        phase = .starting
        status = "Starting capture…"
        let sourceID = captureSourceID
        let microphones = selectedMicIDs.sorted(); let systemAudio = includeSystemAudio; let core = useCoreAudio
        Task {
            do {
                _ = await audioMonitor.stop(); isAudioTesting = false
                try await engine.start(sourceID: sourceID, outputURL: dir.appendingPathComponent("meeting.mov"), micDeviceIDs: microphones, includeSystemAudio: systemAudio, useCoreAudio: core)
                guard phase == .starting else { return }
                startDate = Date(); elapsed = 0
                phase = .recording
                status = "Recording"
                startLiveTranscription(dir)
                timer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
                    Task { @MainActor in
                        guard let self, let sd = self.startDate else { return }
                        self.elapsed = Date().timeIntervalSince(sd)
                    }
                }
            } catch {
                _ = await engine.stop()
                log("start failed: \(error.localizedDescription)")
                status = "Could not start: \(error.localizedDescription)"
                phase = .failed
                await refreshSources()
            }
        }
    }

    func stop() {
        guard phase == .recording, let dir = folder else { return }
        timer?.invalidate(); timer = nil
        phase = .processing
        status = "Stopping capture…"
        Task {
            let frames = await engine.stop()
            let mov = dir.appendingPathComponent("meeting.mov")
            let size = (try? FileManager.default.attributesOfItem(atPath: mov.path)[.size] as? Int) ?? 0
            log("recording finished: frames=\(frames) size=\(size) bytes")
            let live = liveRuns.removeValue(forKey: dir)
            guard size > 10_000 else {
                status = "Recording file is empty (\(size) bytes). Nothing to send."; phase = .failed
                if live != nil { scriptProcs[dir]?.terminate() }
                upsertJob(dir, status: "Recording was empty.", state: .failed)
                return
            }
            // The AI runs in the background so a new recording can start right away.
            phase = .done
            status = "Saved. The AI summary is being written below — you can start another recording now."
            if live == nil { upsertJob(dir, status: "Uploading to Gemini…") }
            await finishAnalysis(dir, live: live)
        }
    }

    // MARK: - Stay awake while working

    private var sleepGuard: NSObjectProtocol?
    /// A sleeping Mac cuts uploads mid-way: on 2026-11-06 that failed a 60-minute meeting's AI step after 40 minutes.
    var keepingAwake: Bool { phase == .recording || phase == .starting || phase == .processing || jobsRunning }

    private func updateSleepGuard() {
        if keepingAwake, sleepGuard == nil {
            sleepGuard = ProcessInfo.processInfo.beginActivity(options: [.idleSystemSleepDisabled, .userInitiated], reason: "Recording or transcribing a meeting")
            log("keeping the Mac awake while working")
        } else if !keepingAwake, let active = sleepGuard {
            ProcessInfo.processInfo.endActivity(active)
            sleepGuard = nil
            log("allowing sleep again")
        }
    }

    // MARK: - Open at login

    /// Turned on once, the first time the installed app runs; after that the checkbox in the panel decides.
    func setUpLoginItem() {
        guard Bundle.main.bundleURL.pathExtension == "app" else { return }  // not for `swift run`
        if !UserDefaults.standard.bool(forKey: "loginItemDefaultApplied") {
            UserDefaults.standard.set(true, forKey: "loginItemDefaultApplied")
            setOpensAtLogin(true)
        }
        opensAtLogin = SMAppService.mainApp.status == .enabled
        log("open at login: \(SMAppService.mainApp.status.rawValue) (1=enabled)")
    }

    func setOpensAtLogin(_ on: Bool) {
        do {
            if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
        } catch {
            log("open at login \(on ? "register" : "unregister") failed: \(error.localizedDescription)")
            status = "Couldn't change Open at login: \(error.localizedDescription)"
        }
        opensAtLogin = SMAppService.mainApp.status == .enabled
    }

    func openFolder() { if let f = folder { NSWorkspace.shared.open(f) } }

    // MARK: - Gemini pass (runs the tested Python script)

    private func runGeminiPass(folder: URL) async {
        let script = isVideoFolder(folder) || FileManager.default.fileExists(atPath: folder.appendingPathComponent("source.txt").path) ? "video_pass.py" : "gemini_pass.py"
        guard let rc = await runScript(script, folder: folder) else { return }
        if rc == 0 {
            upsertJob(folder, status: isVideoFolder(folder) ? "Done — requests with screenshots are in the summary." : "Done — summary and transcript are ready.", state: .done)
        } else {
            let detail = jobErrors[folder.path] ?? ""
            upsertJob(folder, status: "AI step failed (exit \(rc)). The recording is safe; press Retry. " + detail, state: .failed)
        }
        jobErrors[folder.path] = nil
    }

    // MARK: - Live transcription (live_pass.py works through the meeting while it records)

    private func startLiveTranscription(_ folder: URL) {
        upsertJob(folder, status: "Transcribing live")
        liveRuns[folder] = Task { [weak self] in await self?.runScript("live_pass.py", folder: folder) ?? nil }
    }

    /// After the recording file is finalized: tell the worker, wait for it, and if it failed for any
    /// reason run the full analysis, so a live problem can never cost the meeting its notes.
    private func finishAnalysis(_ folder: URL, live: Task<Int32?, Never>?) async {
        guard let live else { await runGeminiPass(folder: folder); return }
        upsertJob(folder, status: "Finishing the transcript…")
        let flag = folder.appendingPathComponent("live/recording_done")
        do {
            try FileManager.default.createDirectory(at: flag.deletingLastPathComponent(), withIntermediateDirectories: true)
            try Data().write(to: flag)
        } catch {
            folderLog(folder, "could not signal live worker at \(flag.path): \(error.localizedDescription); stopping it")
            scriptProcs[folder]?.terminate()   // otherwise it would wait for the signal forever
        }
        let rc = await live.value
        if rc == 0 {
            jobErrors[folder.path] = nil
            upsertJob(folder, status: "Done — summary and transcript are ready.", state: .done)
        } else {
            folderLog(folder, "live transcription ended with \(rc.map(String.init) ?? "no process"); running the full analysis")
            upsertJob(folder, status: "Running the full analysis…")
            await runGeminiPass(folder: folder)
        }
    }

    /// Runs one engine script for a meeting folder and returns its exit code; nil if it couldn't start.
    private func runScript(_ name: String, folder: URL) async -> Int32? {
        let python = supportDir.appendingPathComponent(".venv/bin/python")
        let script = engineDir.appendingPathComponent(name)
        for f in [python, script] where !FileManager.default.fileExists(atPath: f.path) {
            folderLog(folder, "missing \(f.path)")
            upsertJob(folder, status: "Missing \(f.path). Reinstall with build_app.sh.", state: .failed); return nil
        }
        let proc = Process()
        proc.executableURL = python
        proc.arguments = [script.path, folder.path]
        proc.currentDirectoryURL = engineDir
        // Opened from Finder or at login, the app gets a bare PATH with no Homebrew, so ffmpeg wouldn't be found.
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + (env["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin")
        env["MR_ENV_FILE"] = supportDir.appendingPathComponent(".env").path
        env["PYTHONDONTWRITEBYTECODE"] = "1"  // the app bundle is signed; don't write __pycache__ into it
        proc.environment = env
        folderLog(folder, "AI step: \(script.lastPathComponent) from \(engineDir.path), settings \(supportDir.path)")
        let pipe = Pipe()
        proc.standardOutput = pipe; proc.standardError = pipe
        let handle = pipe.fileHandleForReading
        let reader = Task.detached { [weak self] in
            var buffer = Data()
            while true {
                let chunk = handle.availableData
                if chunk.isEmpty { break }
                buffer.append(chunk)
                while let nl = buffer.firstIndex(of: 10) {
                    let line = String(decoding: buffer[..<nl], as: UTF8.self)
                    buffer.removeSubrange(...nl)
                    await self?.handleGeminiLine(line, folder: folder)
                }
            }
            if !buffer.isEmpty { await self?.handleGeminiLine(String(decoding: buffer, as: UTF8.self), folder: folder) }
        }
        do { try proc.run() } catch {
            pipe.fileHandleForWriting.closeFile()
            _ = await reader.value
            folderLog(folder, "could not start the AI step: \(error.localizedDescription)")
            upsertJob(folder, status: "Could not run the AI step: \(error.localizedDescription)", state: .failed); return nil
        }
        pipe.fileHandleForWriting.closeFile()
        scriptProcs[folder] = proc
        defer { scriptProcs[folder] = nil }
        folderLog(folder, "\(name) started pid=\(proc.processIdentifier)")
        await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
            DispatchQueue.global().async { proc.waitUntilExit(); c.resume() }
        }
        _ = await reader.value
        let rc = proc.terminationStatus
        folderLog(folder, "\(name) exited \(rc)")
        return rc
    }

    private var liveRuns: [URL: Task<Int32?, Never>] = [:]
    private var scriptProcs: [URL: Process] = [:]
    private var jobErrors: [String: String] = [:]
    private func handleGeminiLine(_ line: String, folder: URL) {
        folderLog(folder, "py: \(line)")
        if let r = line.range(of: "STATUS ") { upsertJob(folder, status: String(line[r.upperBound...])) }
        if line.contains("ERROR") || line.contains("Error") { jobErrors[folder.path] = String(line.suffix(160)) }
    }

    /// Adds or updates the job for `folder`; newest first, keeps the last 6.
    private func upsertJob(_ folder: URL, status: String, state: AIJob.State = .running) {
        if let i = jobs.firstIndex(where: { $0.folder == folder }) {
            jobs[i].status = status; jobs[i].state = state
        } else {
            jobs.insert(AIJob(folder: folder, status: status, state: state), at: 0)
            if jobs.count > 6 { jobs.removeLast(jobs.count - 6) }
        }
    }
    func openSummary(of job: AIJob) { openNotes(in: job.folder) }
    func dismiss(_ job: AIJob) { jobs.removeAll { $0.id == job.id && $0.state != .running } }

    // MARK: - Helpers

    private func applyLevels(_ l: CaptureEngine.Levels) {
        micLevel = l.mic; sysLevel = l.sys; inputLevels = l.inputs
        if isAudioTesting {
            if l.sys > 0.01 { audioTestDetected.insert("System audio") }
            for (id, level) in l.inputs where level > 0.01 {
                audioTestDetected.insert(audioSources.first(where: { $0.id == id })?.name ?? "Microphone")
            }
        }
        let now = Date()
        if l.mic >= 0.01 { micSilentSince = now }
        if l.sys >= 0.01 { sysSilentSince = now }
    }

    /// Reads .env the same way gemini_pass.load_env does, just enough to warn before the meeting instead of after.
    /// Asks for a Gemini API key and saves it in this Mac's settings file only. Returns true when saved.
    private func askForKey() -> Bool {
        let alert = NSAlert()
        alert.messageText = "Add your Gemini API key"
        alert.informativeText = "Meeting Recorder uses Google Gemini to write the transcript and summary. Create a key at aistudio.google.com/apikey and paste it below. It's saved only on this Mac (\(supportDir.path)/.env)."
        let field = NSSecureTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
        field.placeholderString = "AIza…"
        alert.accessoryView = field
        alert.addButton(withTitle: "Save")
        alert.addButton(withTitle: "Cancel")
        NSApp.activate(ignoringOtherApps: true)
        alert.window.initialFirstResponder = field
        guard alert.runModal() == .alertFirstButtonReturn else { return false }
        let key = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !key.isEmpty else { return false }
        let env = supportDir.appendingPathComponent(".env")
        do {
            try FileManager.default.createDirectory(at: supportDir, withIntermediateDirectories: true)
            let current = (try? String(contentsOf: env, encoding: .utf8)) ?? ""
            try Self.settings(current, withKey: key).write(to: env, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: env.path)
            log("Gemini key saved to \(env.path)")
            return true
        } catch {
            log("could not save the Gemini key to \(env.path): \(error.localizedDescription)")
            status = "Could not save the key: \(error.localizedDescription)"
            return false
        }
    }

    /// The settings text with GEMINI_API_KEY set to `key`, every other line kept.
    nonisolated static func settings(_ text: String, withKey key: String) -> String {
        var lines = text.components(separatedBy: "\n").filter { !$0.trimmingCharacters(in: .whitespaces).hasPrefix("GEMINI_API_KEY=") }
        while lines.last == "" { lines.removeLast() }
        return (["GEMINI_API_KEY=\(key)"] + lines).joined(separator: "\n") + "\n"
    }

    private func keyProblem() -> String? {
        let env = supportDir.appendingPathComponent(".env")
        guard let text = try? String(contentsOf: env, encoding: .utf8) else { return "No .env in \(supportDir.path)" }
        func key(in s: String) -> String? {
            for line in s.split(separator: "\n") {
                let t = line.trimmingCharacters(in: .whitespaces)
                if t.hasPrefix("GEMINI_API_KEY=") { let v = String(t.dropFirst("GEMINI_API_KEY=".count)); if !v.isEmpty { return v } }
            }
            return nil
        }
        if key(in: text) != nil { return nil }
        for line in text.split(separator: "\n") where line.hasPrefix("GEMINI_KEY_FALLBACK_FILE=") {
            let p = String(line.dropFirst("GEMINI_KEY_FALLBACK_FILE=".count)).trimmingCharacters(in: .whitespaces)
            if let fb = try? String(contentsOfFile: (p as NSString).expandingTildeInPath, encoding: .utf8), key(in: fb) != nil { return nil }
        }
        return "GEMINI_API_KEY is empty in \(env.path)"
    }

    private func openLog(_ dir: URL) {
        try? logHandle?.close()
        let url = dir.appendingPathComponent("log.txt")
        FileManager.default.createFile(atPath: url.path, contents: nil)
        logHandle = try? FileHandle(forWritingTo: url)
        log("MeetingRecorder.app session start")
    }

    func log(_ msg: String) {
        let line = "\(fmt.string(from: Date())) \(msg)\n"
        logHandle?.seekToEndOfFile(); logHandle?.write(line.data(using: .utf8)!)
        NSLog("%@", msg)
    }

    /// Appends to <folder>/log.txt. AI jobs log here, so they never mix into a recording that started later.
    func folderLog(_ folder: URL, _ msg: String) {
        let url = folder.appendingPathComponent("log.txt")
        let line = Data("\(fmt.string(from: Date())) \(msg)\n".utf8)
        if !FileManager.default.fileExists(atPath: url.path) { FileManager.default.createFile(atPath: url.path, contents: nil) }
        do {
            let h = try FileHandle(forWritingTo: url)
            defer { try? h.close() }
            try h.seekToEnd(); try h.write(contentsOf: line)
        } catch {
            NSLog("could not write %@: %@ (%@)", url.path, error.localizedDescription, msg)
        }
    }
}
