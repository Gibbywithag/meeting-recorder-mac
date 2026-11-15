import Foundation

@main
struct SourceSmokeTest {
    @MainActor static func main() async throws {
        let validIDs = try WindowLayout.ids(from: "windows:1,2")
        assert(validIDs == [1, 2])
        for invalid in ["windows:", "windows:1,1", "windows:bad", "windows:1,2,3,4,5"] {
            do { _ = try WindowLayout.ids(from: invalid); fatalError("Invalid selection accepted") } catch { }
        }
        let sources = try await CaptureSource.available()
        for source in sources { print("SOURCE \(source.id) \(source.name)") }
        let displays = sources.filter { $0.id.hasPrefix("display:") }
        let window = sources.first { $0.id.hasPrefix("window:") && $0.name.contains("Chrome") } ?? sources.first { $0.id.hasPrefix("window:") }
        var targets = displays
        if let window { targets.append(window) }
        let chosen = Array(sources.filter { $0.id.hasPrefix("window:") && ($0.name.contains("Claude") || $0.name.contains("Chrome")) }.prefix(2))
        if chosen.count == 2 {
            targets.append(CaptureSource(id: "windows:" + chosen.map { String($0.id.dropFirst(7)) }.joined(separator: ","), name: "Two windows together"))
        } else { throw NSError(domain: "Test needs two open windows", code: 1) }
        let folder = URL(fileURLWithPath: "/tmp/meeting-source-tests-\(UUID().uuidString.prefix(8))")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        for source in targets {
            let engine = CaptureEngine()
            engine.log = { print($0) }
            engine.onError = { print("ERROR \($0)") }
            let file = folder.appendingPathComponent(source.id.replacingOccurrences(of: ":", with: "-") + ".mov")
            do {
                try await engine.start(sourceID: source.id, outputURL: file)
                let sound = Process(); sound.executableURL = URL(fileURLWithPath: "/usr/bin/afplay")
                sound.arguments = ["/System/Library/Sounds/Hero.aiff"]; try sound.run()
                try await Task.sleep(nanoseconds: 5_000_000_000)
                let frames = await engine.stop()
                guard frames > 0 else { throw NSError(domain: "Test", code: 1) }
                print("PASS \(file.path)")
            } catch {
                _ = await engine.stop()
                throw error
            }
        }
        print("OUTPUT \(folder.path)")
    }
}
