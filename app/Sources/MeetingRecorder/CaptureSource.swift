import AppKit
import ScreenCaptureKit

struct CaptureSource: Identifiable, Equatable {
    let id: String
    let name: String

    @MainActor
    static func available() async throws -> [CaptureSource] {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        let displays = content.displays.sorted { $0.displayID < $1.displayID }.map { display in
            let screen = NSScreen.screens.first { ($0.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value == display.displayID }
            let main = display.displayID == CGMainDisplayID() ? " (main)" : ""
            return CaptureSource(id: "display:\(display.displayID)", name: "Screen: \(screen?.localizedName ?? "Display \(display.displayID)")\(main) — \(display.width)×\(display.height)")
        }
        let windows = content.windows.filter {
            $0.windowLayer == 0 && $0.frame.width > 100 && $0.frame.height > 100 && $0.owningApplication?.processID != ProcessInfo.processInfo.processIdentifier
        }.map { window in
            CaptureSource(id: "window:\(window.windowID)", name: "Window: \(window.owningApplication?.applicationName ?? "App") — \(window.title ?? "Untitled")")
        }.sorted { $0.name.localizedStandardCompare($1.name) == .orderedAscending }
        return displays + windows
    }
}
