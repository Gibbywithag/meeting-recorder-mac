// Draws the app icon (red record dot over a screen, on a dark rounded square) into an .iconset.
// Usage: swift make_icon.swift <out.iconset>   then: iconutil -c icns <out.iconset>
import AppKit

let out = URL(fileURLWithPath: CommandLine.arguments[1])
try? FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)

func draw(_ px: Int) -> Data {
    let s = CGFloat(px)
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8,
                               samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                               bytesPerRow: 0, bitsPerPixel: 0)!
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
    let inset = s * 0.1, body = NSRect(x: inset, y: inset, width: s - 2 * inset, height: s - 2 * inset)
    let bg = NSBezierPath(roundedRect: body, xRadius: body.width * 0.225, yRadius: body.width * 0.225)
    NSGradient(starting: NSColor(calibratedRed: 0.17, green: 0.18, blue: 0.24, alpha: 1),
               ending: NSColor(calibratedRed: 0.07, green: 0.07, blue: 0.10, alpha: 1))!.draw(in: bg, angle: -90)
    // screen
    let scr = NSRect(x: body.minX + body.width * 0.17, y: body.minY + body.height * 0.30,
                     width: body.width * 0.66, height: body.height * 0.44)
    NSColor(calibratedWhite: 0.92, alpha: 1).setStroke()
    let sp = NSBezierPath(roundedRect: scr, xRadius: s * 0.03, yRadius: s * 0.03); sp.lineWidth = s * 0.035; sp.stroke()
    let stand = NSBezierPath(); stand.lineWidth = s * 0.035; stand.lineCapStyle = .round
    stand.move(to: NSPoint(x: body.midX - body.width * 0.12, y: body.minY + body.height * 0.2))
    stand.line(to: NSPoint(x: body.midX + body.width * 0.12, y: body.minY + body.height * 0.2)); stand.stroke()
    // record dot
    let r = body.width * 0.12
    let dot = NSBezierPath(ovalIn: NSRect(x: scr.midX - r, y: scr.midY - r, width: 2 * r, height: 2 * r))
    NSColor(calibratedRed: 1.0, green: 0.27, blue: 0.27, alpha: 1).setFill(); dot.fill()
    NSGraphicsContext.restoreGraphicsState()
    return rep.representation(using: .png, properties: [:])!
}

for base in [16, 32, 128, 256, 512] {
    try! draw(base).write(to: out.appendingPathComponent("icon_\(base)x\(base).png"))
    try! draw(base * 2).write(to: out.appendingPathComponent("icon_\(base)x\(base)@2x.png"))
}
print("wrote \(out.path)")
