import Foundation
import AppKit

/// Build+run: S=app/Sources/MeetingRecorder; swiftc -O -parse-as-library -swift-version 5 -o /tmp/recovery_smoke tests/recovery_smoke.swift $S/CaptureEngine.swift $S/CaptureSource.swift $S/WindowLayout.swift $S/AudioSource.swift $S/MicrophoneCapture.swift $S/SystemAudioTap.swift && /tmp/recovery_smoke
/// Real-hardware test of mid-recording video recovery: record a TextEdit window, quit
/// TextEdit (the window disappears, exactly like a call window being recreated), and
/// require the engine to reconnect to the screen and keep writing the same file.
@main
struct RecoverySmokeTest {
    @MainActor static func main() async throws {
        let doc = URL(fileURLWithPath: "/tmp/recovery_smoke_window.txt")
        try "Recovery smoke test window".write(to: doc, atomically: true, encoding: .utf8)
        let opener = Process(); opener.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        opener.arguments = ["-n", "-a", "TextEdit", doc.path]; try opener.run(); opener.waitUntilExit()
        try await Task.sleep(nanoseconds: 3_000_000_000)
        let sources = try await CaptureSource.available()
        guard let window = sources.first(where: { $0.id.hasPrefix("window:") && $0.name.contains("TextEdit") }) else {
            print("FAIL: TextEdit window not found; sources: \(sources.map(\.name))"); exit(1)
        }
        print("recording \(window.id) \(window.name)")
        let out = URL(fileURLWithPath: "/tmp/recovery_smoke/meeting.mov")
        try? FileManager.default.removeItem(at: out.deletingLastPathComponent())
        try FileManager.default.createDirectory(at: out.deletingLastPathComponent(), withIntermediateDirectories: true)
        let engine = CaptureEngine()
        var lines: [String] = []
        engine.log = { print("LOG \($0)"); lines.append($0) }
        engine.onWarning = { print("WARN \($0)") }
        engine.onError = { print("ERROR \($0)"); lines.append("ERROR \($0)") }
        // Audio off: a bare CLI cannot raise mic/system-audio permission prompts. The full app test covers audio.
        try await engine.start(sourceID: window.id, outputURL: out, micDeviceIDs: [], includeSystemAudio: true, useCoreAudio: false)
        try await Task.sleep(nanoseconds: 4_000_000_000)
        let framesBefore = engine.frames
        print("frames before window closes: \(framesBefore)")
        let killer = Process(); killer.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        killer.arguments = ["-x", "TextEdit"]; try killer.run(); killer.waitUntilExit()
        try await Task.sleep(nanoseconds: 8_000_000_000)
        let framesAfter = await engine.stop()
        print("frames total: \(framesAfter), recoveries: \(engine.videoRecoveries)")
        let reconnected = lines.contains { $0.contains("Screen reconnected") }
        let errored = lines.contains { $0.hasPrefix("ERROR") }
        if reconnected && !errored && framesAfter > framesBefore + 10 {
            print("PASS: recording survived the window closing and kept writing video (\(framesAfter - framesBefore) frames after)")
        } else {
            print("FAIL: reconnected=\(reconnected) errored=\(errored) before=\(framesBefore) after=\(framesAfter)"); exit(1)
        }
    }
}
