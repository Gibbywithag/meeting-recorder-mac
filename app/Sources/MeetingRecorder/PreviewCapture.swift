import Foundation
import ScreenCaptureKit
import CoreImage

/// Read-only snapshots: no writer, microphone capture, files, or AI requests.
@MainActor
final class PreviewCapture {
    private let context = CIContext()
    func image(sourceID: String) async throws -> CGImage {
        let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        func unavailable() -> NSError {
            NSError(domain: "Preview", code: 1, userInfo: [NSLocalizedDescriptionKey: "Selected source is unavailable. Refresh or choose another."])
        }
        let filters: [SCContentFilter]
        let config = SCStreamConfiguration()
        config.capturesAudio = false; config.captureMicrophone = false; config.showsCursor = true
        if sourceID.hasPrefix("windows:") || sourceID.hasPrefix("window:") {
            let ids = sourceID.hasPrefix("windows:") ? try WindowLayout.ids(from: sourceID) : [UInt32(sourceID.dropFirst(7))].compactMap { $0 }
            guard !ids.isEmpty else { throw unavailable() }
            filters = try ids.map { id in
                guard let window = content.windows.first(where: { $0.windowID == id }) else { throw unavailable() }
                return SCContentFilter(desktopIndependentWindow: window)
            }
            config.width = 1920; config.height = 1080
        } else {
            let display = sourceID == "main" ? content.displays.first(where: { $0.displayID == CGMainDisplayID() }) ?? content.displays.first : content.displays.first(where: { "display:\($0.displayID)" == sourceID })
            guard let display else { throw unavailable() }
            filters = [SCContentFilter(display: display, excludingWindows: [])]
            config.width = display.width; config.height = display.height
        }
        var images: [CIImage] = []
        for filter in filters {
            try Task.checkCancellation()
            let cg = try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: config)
            images.append(CIImage(cgImage: cg))
        }
        try Task.checkCancellation()
        let image = sourceID.hasPrefix("windows:") ? WindowLayout.composite(images)! : images[0]
        let scale = min(1, 800 / image.extent.width)
        let scaled = image.transformed(by: CGAffineTransform(scaleX: scale, y: scale))
        guard let cg = context.createCGImage(scaled, from: scaled.extent) else { throw unavailable() }
        return cg
    }
}
