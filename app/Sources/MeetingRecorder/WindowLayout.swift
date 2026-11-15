import Foundation
import CoreImage

/// Multi-window recordings use a stable grid instead of desktop overlap.
enum WindowLayout {
    static let maximumWindows = 4
    static func ids(from source: String) throws -> [UInt32] {
        let parts = source.dropFirst("windows:".count).split(separator: ",", omittingEmptySubsequences: false)
        let ids = parts.compactMap { UInt32($0) }
        guard source.hasPrefix("windows:"), ids.count == parts.count, !ids.isEmpty,
              ids.count <= maximumWindows, Set(ids).count == ids.count else {
            throw NSError(domain: "CaptureSelection", code: 1, userInfo: [NSLocalizedDescriptionKey: "Choose one to four distinct windows."])
        }
        return ids
    }
    static func composite(_ images: [CIImage]) -> CIImage? {
        guard !images.isEmpty, images.count <= maximumWindows else { return nil }
        var canvas = CIImage(color: CIColor(red: 0.06, green: 0.07, blue: 0.09)).cropped(to: CGRect(x: 0, y: 0, width: 1920, height: 1080))
        let columns = images.count == 1 ? 1 : 2
        let rows = images.count <= 2 ? 1 : 2
        let cellWidth = 1920.0 / Double(columns)
        let cellHeight = 1080.0 / Double(rows)
        for index in 0..<images.count {
            let image = images[index]
            let scale = min((cellWidth - 12) / image.extent.width, (cellHeight - 12) / image.extent.height)
            let x = Double(index % columns) * cellWidth + (cellWidth - image.extent.width * scale) / 2
            let y = 1080 - Double(index / columns + 1) * cellHeight + (cellHeight - image.extent.height * scale) / 2
            let tile = image.transformed(by: CGAffineTransform(translationX: -image.extent.minX, y: -image.extent.minY))
                .transformed(by: CGAffineTransform(scaleX: scale, y: scale))
                .transformed(by: CGAffineTransform(translationX: x, y: y))
            canvas = tile.composited(over: canvas)
        }
        return canvas
    }
}
