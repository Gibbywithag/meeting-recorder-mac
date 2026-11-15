import Foundation
import AVFoundation
import ScreenCaptureKit
import CoreMedia
import CoreImage
import CoreVideo
import CoreAudio

/// Records the main display + system audio + microphone to a two-audio-track .mov.
/// Records screen, system audio and microphone(s) to one .mov with separate audio tracks.
final class CaptureEngine: NSObject, SCStreamOutput, SCStreamDelegate {
    struct Levels { var mic: Float = 0; var sys: Float = 0; var inputs: [String: Float] = [:] }

    var onLevels: ((Levels) -> Void)?
    var onPreview: ((CGImage) -> Void)?
    var onError: ((String) -> Void)?
    var onWarning: ((String) -> Void)?
    var log: ((String) -> Void) = { _ in }

    private var writer: AVAssetWriter?
    private var videoInput: AVAssetWriterInput?
    private var micInput: AVAssetWriterInput?
    private var sysInput: AVAssetWriterInput?
    private var stream: SCStream?
    private var audioStream: SCStream?
    private var systemTap: SystemAudioTap?
    private var microphoneStreams: [MicrophoneCapture] = []
    private var microphoneIDs: [ObjectIdentifier: String] = [:]
    private var microphoneInputs: [String: AVAssetWriterInput] = [:]
    private var extraStreams: [SCStream] = []
    private var windowIndices: [ObjectIdentifier: Int] = [:]
    private var latestWindows: [Int: CIImage] = [:]
    private var compositeAdaptor: AVAssetWriterInputPixelBufferAdaptor?
    private var lastCompositeTime: CMTime?
    private var compositeWindowCount = 0
    // Video recovery: if the screen/window stream dies mid-meeting, reconnect to a display
    // instead of ending the recording. Audio streams are independent and keep running.
    private var videoConfig: SCStreamConfiguration?
    private var videoDisplayID: CGDirectDisplayID?
    private var recovering = false
    private(set) var videoRecoveries = 0
    // Call-audio self-heal: the Core Audio tap can stay "running" but deliver pure digital zeros after
    // the output device or its format changes (2026-10-29: 6 min of the client lost). Rebuild it when that happens.
    private var tapWatchdog: DispatchSourceTimer?
    private var lastSysSound = Date()
    private var lastSysCallback = Date()
    private var pendingDeviceRestart: DispatchWorkItem?
    /// End time of the last call-audio sample written. AVAssetWriter packs audio back to back, so a gap
    /// (tap reconnect) would pull everything after it earlier than the mic and video; we fill gaps with silence.
    private var sysNextPTS: CMTime?
    private(set) var sysGapFilled: Double = 0
    private var lastTapRestart = Date.distantPast
    private var tapRestarts = 0
    private var outputListener: AudioObjectPropertyListenerBlock?
    private let tapControl = DispatchQueue(label: "capture.tap.control")
    private(set) var currentSourceID = "main"
    private let queue = DispatchQueue(label: "capture.samples")
    private var sessionStarted = false
    private var stopping = false
    private(set) var frames = 0
    private var levels = Levels()
    private var lastLevelEmit = Date.distantPast
    private var lastPreview = Date.distantPast
    private let ciContext = CIContext()

    func start(sourceID: String = "main", outputURL: URL, micDeviceIDs: [String] = ["default"], includeSystemAudio: Bool = true, useCoreAudio: Bool = true) async throws {
        try AudioSource.validate(ids: micDeviceIDs, system: includeSystemAudio, available: AudioSource.available())
        stopping = false; sessionStarted = false; frames = 0; sysNextPTS = nil; sysGapFilled = 0
        levels = Levels(); lastLevelEmit = .distantPast; lastPreview = .distantPast
        videoDisplayID = nil
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        guard let mainDisplay = (content.displays.first { $0.displayID == CGMainDisplayID() } ?? content.displays.first) else { throw err("No display found. Is Screen Recording permission granted?") }
        var windowFilters: [SCContentFilter] = []
        let filter: SCContentFilter
        let width: Int
        let height: Int
        if sourceID.hasPrefix("windows:") {
            let ids = try WindowLayout.ids(from: sourceID)
            for id in ids {
                guard let window = content.windows.first(where: { $0.windowID == id }) else { throw err("A selected window closed. Refresh and choose the windows again.") }
                windowFilters.append(SCContentFilter(desktopIndependentWindow: window))
            }
            filter = windowFilters[0]
            width = 1920; height = 1080
        } else if sourceID.hasPrefix("window:"), let id = UInt32(sourceID.dropFirst(7)) {
            guard let window = content.windows.first(where: { $0.windowID == id }) else { throw err("Selected window is unavailable. Refresh and choose another window.") }
            filter = SCContentFilter(desktopIndependentWindow: window)
            width = max(2, Int(window.frame.width) / 2 * 2)
            height = max(2, Int(window.frame.height) / 2 * 2)
        } else {
            let display: SCDisplay
            if sourceID == "main" { display = mainDisplay }
            else if sourceID.hasPrefix("display:"), let id = UInt32(sourceID.dropFirst(8)), let selected = content.displays.first(where: { $0.displayID == id }) { display = selected }
            else {
                log("selected screen \(sourceID) is not connected; recording the main display instead")
                display = mainDisplay
            }
            videoDisplayID = display.displayID
            filter = SCContentFilter(display: display, excludingWindows: [])
            width = display.width; height = display.height
        }

        let metadata = try JSONSerialization.data(withJSONObject: ["source": sourceID, "multipleWindows": !windowFilters.isEmpty, "microphones": micDeviceIDs, "systemAudio": includeSystemAudio])
        try metadata.write(to: outputURL.deletingLastPathComponent().appendingPathComponent("capture.json"), options: .atomic)
        let w = try AVAssetWriter(outputURL: outputURL, fileType: .mov)
        w.movieFragmentInterval = CMTime(seconds: 5, preferredTimescale: 600)
        let v = AVAssetWriterInput(mediaType: .video, outputSettings: [
            AVVideoCodecKey: AVVideoCodecType.h264,
            AVVideoWidthKey: width, AVVideoHeightKey: height,
            AVVideoCompressionPropertiesKey: [AVVideoAverageBitRateKey: 1_500_000, AVVideoExpectedSourceFrameRateKey: 5, AVVideoMaxKeyFrameIntervalKey: 25],
        ])
        let audioSettings: [String: Any] = [AVFormatIDKey: kAudioFormatMPEG4AAC, AVSampleRateKey: 48_000, AVNumberOfChannelsKey: 2, AVEncoderBitRateKey: 128_000]
        microphoneInputs = Dictionary(uniqueKeysWithValues: micDeviceIDs.map { ($0, AVAssetWriterInput(mediaType: .audio, outputSettings: audioSettings)) })
        let system = includeSystemAudio ? AVAssetWriterInput(mediaType: .audio, outputSettings: audioSettings) : nil
        for i in [v] + micDeviceIDs.compactMap({ microphoneInputs[$0] }) + [system].compactMap({ $0 }) {
            i.expectsMediaDataInRealTime = true
            guard w.canAdd(i) else { throw err("AVAssetWriter cannot add input") }
            w.add(i)
        }
        writer = w; videoInput = v; micInput = nil; sysInput = system
        windowIndices = [:]; latestWindows = [:]; extraStreams = []; lastCompositeTime = nil
        compositeWindowCount = windowFilters.count
        compositeAdaptor = windowFilters.isEmpty ? nil : AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: v, sourcePixelBufferAttributes: [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
            kCVPixelBufferWidthKey as String: width, kCVPixelBufferHeightKey as String: height,
            kCVPixelBufferIOSurfacePropertiesKey as String: [:],
        ])

        let cfg = SCStreamConfiguration()
        cfg.width = width; cfg.height = height
        cfg.minimumFrameInterval = CMTime(value: 1, timescale: 5)
        cfg.pixelFormat = kCVPixelFormatType_32BGRA
        cfg.showsCursor = true; cfg.queueDepth = 6
        cfg.capturesAudio = false
        cfg.excludesCurrentProcessAudio = true
        cfg.captureMicrophone = false

        videoConfig = cfg; recovering = false; videoRecoveries = 0
        if videoDisplayID == nil { videoDisplayID = mainDisplay.displayID }   // window modes fall back to the main screen
        let st = SCStream(filter: filter, configuration: cfg, delegate: self)
        try st.addStreamOutput(self, type: .screen, sampleHandlerQueue: queue)
        if !windowFilters.isEmpty {
            windowIndices[ObjectIdentifier(st)] = 0
            for (index, windowFilter) in windowFilters.dropFirst().enumerated() {
                let additional = SCStream(filter: windowFilter, configuration: cfg, delegate: self)
                try additional.addStreamOutput(self, type: .screen, sampleHandlerQueue: queue)
                windowIndices[ObjectIdentifier(additional)] = index + 1
                extraStreams.append(additional)
            }
        }
        try await configureAudio(content: content, micDeviceIDs: micDeviceIDs, includeSystemAudio: includeSystemAudio, useCoreAudio: useCoreAudio)
        stream = st
        guard w.startWriting() else { throw w.error ?? err("startWriting failed") }
        try await st.startCapture()
        for additional in extraStreams { try await additional.startCapture() }
        currentSourceID = sourceID
        startTapWatchdog()
        log("capture started \(width)x\(height) source=\(sourceID) -> \(outputURL.path)")
    }

    private func configureAudio(content: SCShareableContent, micDeviceIDs: [String], includeSystemAudio: Bool, useCoreAudio: Bool = true) async throws {
        guard let display = content.displays.first else { throw err("No display available for system audio") }
        let filter = SCContentFilter(display: display, excludingWindows: [])
        func config() -> SCStreamConfiguration {
            let c = SCStreamConfiguration(); c.width = 2; c.height = 2
            c.minimumFrameInterval = CMTime(value: 1, timescale: 1)
            c.sampleRate = 48_000; c.channelCount = 2; c.excludesCurrentProcessAudio = true
            return c
        }
        if includeSystemAudio && useCoreAudio {
            let tap = SystemAudioTap()
            tap.onSample = { [weak self] sample in
                guard let self else { return }
                self.queue.async(execute: DispatchWorkItem { self.processSample(sample, type: .audio, streamID: nil) })
            }
            systemTap = tap; try tap.start()
            log("system audio backend: Core Audio process tap")
        } else if includeSystemAudio {
            log("system audio backend: ScreenCaptureKit")
            let c = config(); c.capturesAudio = true; c.captureMicrophone = false
            let audio = SCStream(filter: filter, configuration: c, delegate: self)
            try audio.addStreamOutput(self, type: .audio, sampleHandlerQueue: queue)
            audioStream = audio; try await audio.startCapture()
        }
        for id in micDeviceIDs {
            let mic = MicrophoneCapture()
            let identifier = ObjectIdentifier(mic)
            microphoneIDs[identifier] = id; microphoneStreams.append(mic)
            mic.log = { [weak self] msg in
                self?.log(msg)
                if msg.hasPrefix("Microphone reconnected") { DispatchQueue.main.async { [weak self] in self?.onWarning?(msg) } }
            }
            mic.onSample = { [weak self] sample in
                guard let self else { return }
                self.queue.async(execute: DispatchWorkItem { self.processSample(sample, type: .microphone, streamID: identifier) })
            }
            // macOS's audio service can hang starting a mic (seen 2026-10-29). Never let that block a
            // recording: after 8 s go on without it; the mic joins the file if it starts later.
            switch await Self.startWithTimeout(seconds: 8, { try await mic.start(deviceID: id) }) {
            case .ok: break
            case .failed(let error): throw error
            case .timedOut:
                let msg = "Microphone is slow to start (macOS audio problem). Recording screen + call audio now; your mic joins when it starts."
                log(msg); DispatchQueue.main.async { [weak self] in self?.onWarning?(msg) }
            }
        }
    }

    enum StartResult { case ok, failed(Error), timedOut }
    private final class Once: @unchecked Sendable {
        private let lock = NSLock(); private var done = false
        func run(_ body: () -> Void) { lock.lock(); defer { lock.unlock() }; if !done { done = true; body() } }
    }
    static func startWithTimeout(seconds: Double, _ op: @escaping () async throws -> Void) async -> StartResult {
        await withCheckedContinuation { (c: CheckedContinuation<StartResult, Never>) in
            let once = Once()
            Task {
                do { try await op(); once.run { c.resume(returning: .ok) } }
                catch { once.run { c.resume(returning: .failed(error)) } }
            }
            DispatchQueue.global().asyncAfter(deadline: .now() + seconds) { once.run { c.resume(returning: .timedOut) } }
        }
    }

    func startAudioMonitoring(micDeviceIDs: [String], includeSystemAudio: Bool, useCoreAudio: Bool = true) async throws {
        try AudioSource.validate(ids: micDeviceIDs, system: includeSystemAudio, available: AudioSource.available())
        stopping = false; levels = Levels(); lastLevelEmit = .distantPast
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        try await configureAudio(content: content, micDeviceIDs: micDeviceIDs, includeSystemAudio: includeSystemAudio, useCoreAudio: useCoreAudio)
    }

    func stop() async -> Int {
        stopTapWatchdog()
        tapControl.sync { systemTap?.stop(); systemTap = nil }
        queue.sync { stopping = true }
        if let st = stream { try? await st.stopCapture() }
        for additional in extraStreams { try? await additional.stopCapture() }
        if let audio = audioStream { try? await audio.stopCapture() }
        for mic in microphoneStreams { mic.stop() }
        queue.sync {
            videoInput?.markAsFinished(); sysInput?.markAsFinished()
            for input in microphoneInputs.values { input.markAsFinished() }
        }
        if let w = writer, w.status == .writing {
            if sessionStarted { await w.finishWriting() } else { w.cancelWriting() }
        }
        log("capture stopped, frames=\(frames), writerStatus=\(writer?.status.rawValue ?? -1)")
        stream = nil; audioStream = nil; microphoneStreams = []; microphoneIDs = [:]; microphoneInputs = [:]; extraStreams = []; writer = nil; compositeAdaptor = nil
        return frames
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sb: CMSampleBuffer, of type: SCStreamOutputType) {
        processSample(sb, type: type, streamID: ObjectIdentifier(stream))
    }

    private func processSample(_ sb: CMSampleBuffer, type: SCStreamOutputType, streamID: ObjectIdentifier?) {
        guard !stopping, sb.isValid else { return }
        if type == .audio {
            levels.sys = rms(sb)
            lastSysCallback = Date()
            if levels.sys > 0 { lastSysSound = Date() }   // exactly 0 = digital silence from a dead tap
        }
        if type == .microphone, let streamID, let id = microphoneIDs[streamID] {
            levels.inputs[id] = rms(sb)
            levels.mic = levels.inputs.values.max() ?? 0
        }
        if Date().timeIntervalSince(lastLevelEmit) >= 0.1 {
            lastLevelEmit = Date(); let l = levels
            DispatchQueue.main.async { [weak self] in self?.onLevels?(l) }
        }
        guard let w = writer, w.status == .writing else { return }
        if type == .screen {
            guard let atts = CMSampleBufferGetSampleAttachmentsArray(sb, createIfNecessary: false) as? [[SCStreamFrameInfo: Any]],
                  let raw = atts.first?[.status] as? Int, SCFrameStatus(rawValue: raw) == .complete else { return }
        }
        let pts = CMSampleBufferGetPresentationTimeStamp(sb)
        if !sessionStarted { w.startSession(atSourceTime: pts); sessionStarted = true }
        switch type {
        case .screen:
            if let streamID, let index = windowIndices[streamID], let pb = CMSampleBufferGetImageBuffer(sb) {
                latestWindows[index] = CIImage(cvPixelBuffer: pb)
                appendComposite(at: pts)
            } else {
                if let v = videoInput, v.isReadyForMoreMediaData, v.append(sb) { frames += 1 }
                previewIfDue(sb)
            }
        case .audio:
            levels.sys = rms(sb)
            if let s = sysInput, s.isReadyForMoreMediaData {
                fillSystemGap(before: sb, into: s)
                if s.append(sb) { sysNextPTS = CMTimeAdd(pts, CMSampleBufferGetDuration(sb)) }
            }
        case .microphone:
            if let streamID, let id = microphoneIDs[streamID], let input = microphoneInputs[id], input.isReadyForMoreMediaData { input.append(sb) }
        @unknown default: break
        }
        if Date().timeIntervalSince(lastLevelEmit) >= 0.1 {
            lastLevelEmit = Date()
            let l = levels
            DispatchQueue.main.async { [weak self] in self?.onLevels?(l) }
        }
        if w.status == .failed {
            let msg = "writer failed: \(w.error?.localizedDescription ?? "unknown")"
            log(msg); DispatchQueue.main.async { [weak self] in self?.onError?(msg) }
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        guard !stopping else { return }
        if let index = windowIndices[ObjectIdentifier(stream)], compositeWindowCount > 1 {
            queue.async(execute: DispatchWorkItem {
                self.latestWindows[index] = CIImage(color: CIColor(red: 0, green: 0, blue: 0)).cropped(to: CGRect(x: 0, y: 0, width: 1920, height: 1080))
            })
            let warning = "A selected window stopped. Remaining windows and audio are still recording."
            log(warning)
            DispatchQueue.main.async { [weak self] in self?.onWarning?(warning) }
            return
        }
        // The main video stream (screen, single window, or last window): keep recording.
        if stream === self.stream || windowIndices[ObjectIdentifier(stream)] != nil {
            recoverVideo(reason: error.localizedDescription)
            return
        }
        let msg = "stream stopped: \(error.localizedDescription)"
        log(msg); DispatchQueue.main.async { [weak self] in self?.onError?(msg) }
    }

    /// Reconnect video to the same display (or the main one) with the same output size, so the
    /// recording continues in the same file. Audio never stops. Retries with backoff.
    private func recoverVideo(reason: String, attempt: Int = 1) {
        guard !stopping, let cfg = videoConfig else { return }
        if attempt == 1 {
            if recovering { return }
            recovering = true
            let warning = "Screen capture was interrupted (\(reason)). Audio is still recording; reconnecting the screen…"
            log(warning); DispatchQueue.main.async { [weak self] in self?.onWarning?(warning) }
        }
        Task { [weak self] in
            guard let self else { return }
            try? await Task.sleep(nanoseconds: UInt64(min(attempt, 5)) * 700_000_000)
            guard !self.stopping else { return }
            do {
                let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
                guard let display = content.displays.first(where: { $0.displayID == self.videoDisplayID })
                        ?? content.displays.first(where: { $0.displayID == CGMainDisplayID() })
                        ?? content.displays.first else { throw self.err("no display available") }
                let st = SCStream(filter: SCContentFilter(display: display, excludingWindows: []), configuration: cfg, delegate: self)
                try st.addStreamOutput(self, type: .screen, sampleHandlerQueue: self.queue)
                try await st.startCapture()
                guard !self.stopping else { try? await st.stopCapture(); return }
                let old = self.stream
                self.stream = st
                self.videoDisplayID = display.displayID
                self.recovering = false
                self.videoRecoveries += 1
                if let old, old !== st { try? await old.stopCapture() }
                let ok = "Screen reconnected (display \(display.displayID), attempt \(attempt)). Recording continues."
                self.log(ok); DispatchQueue.main.async { [weak self] in self?.onWarning?(ok) }
            } catch {
                self.log("screen reconnect attempt \(attempt) failed: \(error.localizedDescription)")
                if attempt < 12 {
                    self.recoverVideo(reason: reason, attempt: attempt + 1)
                } else {
                    self.recovering = false
                    let msg = "Screen could not be reconnected. Audio is still recording; press Stop when the meeting ends."
                    self.log(msg); DispatchQueue.main.async { [weak self] in self?.onWarning?(msg) }
                }
            }
        }
    }

    /// If call audio skipped ahead (reconnect, dropped buffers), write digital silence for the missing time
    /// so the call track stays aligned with the mic and the video.
    private func fillSystemGap(before sb: CMSampleBuffer, into input: AVAssetWriterInput) {
        guard let next = sysNextPTS, let fmt = CMSampleBufferGetFormatDescription(sb),
              let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(fmt)?.pointee,
              asbd.mBytesPerFrame > 0, asbd.mSampleRate > 0 else { return }
        let gap = CMTimeGetSeconds(CMTimeSubtract(CMSampleBufferGetPresentationTimeStamp(sb), next))
        guard gap > 0.02 else { return }
        if gap > 600 { log("call audio gap of \(Int(gap)) s not filled (implausible)"); return }
        let rate = asbd.mSampleRate
        var remaining = Int((gap * rate).rounded())
        var start = next
        while remaining > 0 {
            let n = min(remaining, Int(rate))   // ≤ 1 s per buffer
            // Non-interleaved audio stores one plane per channel, back to back.
            let planes = asbd.mFormatFlags & kAudioFormatFlagIsNonInterleaved != 0 ? Int(asbd.mChannelsPerFrame) : 1
            let bytes = n * Int(asbd.mBytesPerFrame) * planes
            var block: CMBlockBuffer?
            guard CMBlockBufferCreateWithMemoryBlock(allocator: kCFAllocatorDefault, memoryBlock: nil, blockLength: bytes, blockAllocator: kCFAllocatorDefault,
                                                     customBlockSource: nil, offsetToData: 0, dataLength: bytes, flags: kCMBlockBufferAssureMemoryNowFlag, blockBufferOut: &block) == noErr,
                  let block, CMBlockBufferFillDataBytes(with: 0, blockBuffer: block, offsetIntoDestination: 0, dataLength: bytes) == noErr else {
                log("call audio gap fill failed at \(CMTimeGetSeconds(start)) s"); return
            }
            var timing = CMSampleTimingInfo(duration: CMTime(value: 1, timescale: CMTimeScale(rate)), presentationTimeStamp: start, decodeTimeStamp: .invalid)
            var silence: CMSampleBuffer?
            guard CMSampleBufferCreateReady(allocator: kCFAllocatorDefault, dataBuffer: block, formatDescription: fmt, sampleCount: n,
                                            sampleTimingEntryCount: 1, sampleTimingArray: &timing, sampleSizeEntryCount: 0, sampleSizeArray: nil, sampleBufferOut: &silence) == noErr,
                  let silence, input.append(silence) else {
                log("call audio gap fill could not append at \(CMTimeGetSeconds(start)) s"); return
            }
            start = CMTimeAdd(start, CMTime(value: CMTimeValue(n), timescale: CMTimeScale(rate)))
            remaining -= n
        }
        sysGapFilled += gap
        log(String(format: "filled %.2f s call-audio gap with silence to keep tracks in sync", gap))
    }

    // MARK: - Call audio self-heal

    private func startTapWatchdog() {
        guard systemTap != nil else { return }
        lastSysSound = Date(); lastSysCallback = Date(); lastTapRestart = .distantPast; tapRestarts = 0
        let t = DispatchSource.makeTimerSource(queue: tapControl)
        t.schedule(deadline: .now() + 1, repeating: 1)
        t.setEventHandler { [weak self] in self?.checkTap() }
        t.resume(); tapWatchdog = t
        var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultOutputDevice,
                                                 mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        // One plug/unplug fires several notifications while devices come and go: wait for it to settle,
        // then reconnect once.
        let listener: AudioObjectPropertyListenerBlock = { [weak self] _, _ in
            guard let self else { return }
            self.pendingDeviceRestart?.cancel()
            let work = DispatchWorkItem { [weak self] in self?.restartTap(reason: "the Mac's sound output device changed") }
            self.pendingDeviceRestart = work
            self.tapControl.asyncAfter(deadline: .now() + 0.8, execute: work)
        }
        let status = AudioObjectAddPropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &address, tapControl, listener)
        if status == noErr { outputListener = listener } else { log("could not watch output device changes (\(status)); silence watchdog still active") }
    }

    private func stopTapWatchdog() {
        tapWatchdog?.cancel(); tapWatchdog = nil
        pendingDeviceRestart?.cancel(); pendingDeviceRestart = nil
        if let listener = outputListener {
            var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultOutputDevice,
                                                     mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
            AudioObjectRemovePropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &address, tapControl, listener)
            outputListener = nil
        }
    }

    /// Runs on tapControl. No audio callbacks at all for 2 s = the tap died -> rebuild now (seen when
    /// headphones are unplugged). Digital silence for 6 s -> rebuild too; if the Mac is genuinely silent the
    /// rebuild is harmless, so back off (6 s, 12 s, ... up to 30 s) instead of stopping.
    private func checkTap() {
        guard !stopping, systemTap != nil else { return }
        let starved = Date().timeIntervalSince(lastSysCallback)
        if starved >= 2, Date().timeIntervalSince(lastTapRestart) >= 2 {
            restartTap(reason: "call audio stopped arriving for \(Int(starved)) s")
            return
        }
        let quiet = Date().timeIntervalSince(lastSysSound)
        let wait = min(30, 6 * Double(max(1, tapRestarts)))
        guard quiet >= 6, Date().timeIntervalSince(lastTapRestart) >= wait else { return }
        restartTap(reason: "call audio was digital silence for \(Int(quiet)) s")
    }

    /// Runs on tapControl. Old tap stops before the new one starts, so audio timestamps never overlap.
    private func restartTap(reason: String) {
        guard !stopping, let old = systemTap else { return }
        pendingDeviceRestart?.cancel(); pendingDeviceRestart = nil
        lastTapRestart = Date(); tapRestarts += 1
        lastSysCallback = Date()   // give the new tap a fresh 2 s before judging it
        old.stop()
        let tap = SystemAudioTap()
        tap.onSample = old.onSample
        do {
            try tap.start()
            systemTap = tap
            let msg = "Call audio reconnected (\(reason)). Recording continues."
            log(msg + " [restart \(tapRestarts)]")
            if tapRestarts <= 3 { DispatchQueue.main.async { [weak self] in self?.onWarning?(msg) } }
        } catch {
            systemTap = old   // keep the object so the next watchdog tick retries
            log("call audio reconnect failed (\(reason)): \(error.localizedDescription); retrying")
        }
    }

    // MARK: - Switch screen/window mid-recording

    /// Moves the recording to another screen or window, into the same file at the same output size.
    /// Accepts "main", "display:<id>", "window:<id>". Audio is untouched.
    func switchSource(to sourceID: String) async throws {
        guard writer != nil, !stopping, let cfg = videoConfig else { throw err("Not recording.") }
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        let filter: SCContentFilter
        if sourceID.hasPrefix("window:"), let id = UInt32(sourceID.dropFirst(7)) {
            guard let window = content.windows.first(where: { $0.windowID == id }) else { throw err("That window is no longer open. Refresh the list.") }
            filter = SCContentFilter(desktopIndependentWindow: window)
        } else {
            let display: SCDisplay?
            if sourceID.hasPrefix("display:"), let id = UInt32(sourceID.dropFirst(8)) { display = content.displays.first { $0.displayID == id } }
            else { display = content.displays.first { $0.displayID == CGMainDisplayID() } ?? content.displays.first }
            guard let display else { throw err("That screen is not connected.") }
            filter = SCContentFilter(display: display, excludingWindows: [])
            videoDisplayID = display.displayID
        }
        cfg.preservesAspectRatio = true   // the file keeps its size; the new source is fitted inside it
        let st = SCStream(filter: filter, configuration: cfg, delegate: self)
        try st.addStreamOutput(self, type: .screen, sampleHandlerQueue: queue)
        try await st.startCapture()
        guard !stopping else { try? await st.stopCapture(); throw err("Recording stopped.") }
        let old = stream, extras = extraStreams
        queue.sync {
            stream = st
            extraStreams = []; windowIndices = [:]; latestWindows = [:]; compositeWindowCount = 0
        }
        if let old { try? await old.stopCapture() }
        for e in extras { try? await e.stopCapture() }
        currentSourceID = sourceID
        log("switched recording to \(sourceID)")
    }

    private func appendComposite(at pts: CMTime) {
        guard latestWindows.count == compositeWindowCount, let adaptor = compositeAdaptor,
              let pool = adaptor.pixelBufferPool, videoInput?.isReadyForMoreMediaData == true else { return }
        if let last = lastCompositeTime, CMTimeGetSeconds(CMTimeSubtract(pts, last)) < 0.19 { return }
        guard let canvas = WindowLayout.composite((0..<compositeWindowCount).compactMap { latestWindows[$0] }) else { return }
        var pixelBuffer: CVPixelBuffer?
        guard CVPixelBufferPoolCreatePixelBuffer(nil, pool, &pixelBuffer) == kCVReturnSuccess, let pixelBuffer else { return }
        ciContext.render(canvas, to: pixelBuffer)
        if adaptor.append(pixelBuffer, withPresentationTime: pts) { frames += 1; lastCompositeTime = pts }
        if Date().timeIntervalSince(lastPreview) >= 1, let cg = ciContext.createCGImage(canvas, from: canvas.extent) {
            lastPreview = Date()
            DispatchQueue.main.async { [weak self] in self?.onPreview?(cg) }
        }
    }

    private func previewIfDue(_ sb: CMSampleBuffer) {
        guard Date().timeIntervalSince(lastPreview) >= 1.0, let pb = CMSampleBufferGetImageBuffer(sb) else { return }
        lastPreview = Date()
        let ci = CIImage(cvPixelBuffer: pb)
        let scale = 640.0 / ci.extent.width
        let scaled = ci.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
        guard let cg = ciContext.createCGImage(scaled, from: scaled.extent) else { return }
        DispatchQueue.main.async { [weak self] in self?.onPreview?(cg) }
    }

    private func rms(_ sb: CMSampleBuffer) -> Float {
        var sumSq: Float = 0; var count = 0
        do {
            try sb.withAudioBufferList { abl, _ in
                for buf in abl {
                    guard let p = buf.mData else { continue }
                    let n = Int(buf.mDataByteSize) / MemoryLayout<Float>.size
                    let fp = p.bindMemory(to: Float.self, capacity: n)
                    for i in 0..<n { sumSq += fp[i] * fp[i] }
                    count += n
                }
            }
        } catch { return 0 }
        return count > 0 ? min(1, sqrt(sumSq / Float(count)) * 3) : 0
    }

    private func err(_ msg: String) -> NSError {
        NSError(domain: "CaptureEngine", code: 1, userInfo: [NSLocalizedDescriptionKey: msg])
    }
}
