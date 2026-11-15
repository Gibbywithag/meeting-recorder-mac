import Foundation
import Darwin

@main struct AudioSmokeTest {
    @MainActor static func main() async throws {
        setbuf(stdout, nil)
        let devices = AudioSource.available()
        for d in devices { print("INPUT \(d.id) \(d.name)") }
        let physical = devices.filter { $0.id != "default" }
        let ids = physical.count >= 2 ? Array(physical.prefix(2).map(\.id)) : ["default"]
        let engine = CaptureEngine()
        var peaks: [String: Float] = [:]; var sys: Float = 0
        engine.onLevels = { l in
            sys = max(sys, l.sys)
            for (id, level) in l.inputs { peaks[id] = max(peaks[id] ?? 0, level) }
        }
        engine.onError = { print("ERROR \($0)") }
        engine.log = { print($0) }
        try await engine.startAudioMonitoring(micDeviceIDs: ids, includeSystemAudio: true)
        let sound = Process(); sound.executableURL = URL(fileURLWithPath: "/usr/bin/afplay"); sound.arguments = ["/System/Library/Sounds/Hero.aiff"]; try sound.run()
        try await Task.sleep(nanoseconds: 5_000_000_000)
        _ = await engine.stop()
        print("Observed system=\(sys), inputs=\(peaks)")
        guard sys > 0, ids.allSatisfy({ peaks[$0] != nil }) else { throw NSError(domain: "AudioTestMissingSignal", code: 1) }
        print("PASS: idle audio test system=\(sys), microphone peaks=\(peaks), no output URL/writer")
        let folder = URL(fileURLWithPath: "/tmp/meeting-audio-test-\(UUID().uuidString.prefix(8))")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        try await engine.start(outputURL: folder.appendingPathComponent("meeting.mov"), micDeviceIDs: ids, includeSystemAudio: true)
        let playback = Process(); playback.executableURL = sound.executableURL; playback.arguments = sound.arguments; try playback.run()
        try await Task.sleep(nanoseconds: 5_000_000_000)
        let frames = await engine.stop()
        guard frames > 0 else { throw NSError(domain: "NoFrames", code: 1) }
        print("PASS: recorded \(ids.count) microphones + system audio at \(folder.path)")
    }
}
