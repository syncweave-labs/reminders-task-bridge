// MakeIcon.swift — draws the app icon into an .iconset folder:
//
//   swiftc Tools/MakeIcon.swift -o make-icon && ./make-icon AppIcon.iconset
//   iconutil -c icns AppIcon.iconset
//
// A calendar page (red header, grid of days) with one day checked.

import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

let arguments = CommandLine.arguments
guard arguments.count == 2 else {
    FileHandle.standardError.write(Data("usage: make-icon OUTPUT.iconset\n".utf8))
    exit(64)
}
let output = URL(fileURLWithPath: arguments[1], isDirectory: true)
try? FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)

let sRGB = CGColorSpace(name: CGColorSpace.sRGB)!

func rgb(_ hex: UInt32, _ alpha: CGFloat = 1) -> CGColor {
    CGColor(colorSpace: sRGB, components: [
        CGFloat((hex >> 16) & 0xFF) / 255, CGFloat((hex >> 8) & 0xFF) / 255, CGFloat(hex & 0xFF) / 255, alpha,
    ])!
}

func linear(_ context: CGContext, _ colors: [CGColor], from start: CGPoint, to end: CGPoint) {
    let gradient = CGGradient(colorsSpace: sRGB, colors: colors as CFArray, locations: nil)!
    context.drawLinearGradient(gradient, start: start, end: end, options: [.drawsBeforeStartLocation, .drawsAfterEndLocation])
}

func drawIcon(pixels: Int) -> CGImage {
    let context = CGContext(data: nil, width: pixels, height: pixels, bitsPerComponent: 8, bytesPerRow: 0,
                            space: sRGB, bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
    context.scaleBy(x: CGFloat(pixels) / 1024, y: CGFloat(pixels) / 1024)
    context.setShouldAntialias(true)

    // macOS icon grid: an 824-point rounded square centred on the 1024 canvas.
    let page = CGRect(x: 100, y: 100, width: 824, height: 824)
    let shape = CGPath(roundedRect: page, cornerWidth: 186, cornerHeight: 186, transform: nil)

    context.saveGState()
    context.setShadow(offset: CGSize(width: 0, height: -10), blur: 26, color: rgb(0x000000, 0.28))
    context.addPath(shape)
    context.setFillColor(rgb(0xFFFFFF))
    context.fillPath()
    context.restoreGState()

    context.saveGState()
    context.addPath(shape)
    context.clip()
    linear(context, [rgb(0xFFFFFF), rgb(0xECEEF3)], from: CGPoint(x: 0, y: 924), to: CGPoint(x: 0, y: 100))
    let header = CGRect(x: 100, y: 690, width: 824, height: 234)
    context.saveGState()
    context.clip(to: header)
    linear(context, [rgb(0xFF6259), rgb(0xE8382E)], from: CGPoint(x: 0, y: 924), to: CGPoint(x: 0, y: 690))
    context.restoreGState()
    context.setFillColor(rgb(0x000000, 0.08))
    context.fill(CGRect(x: 100, y: 684, width: 824, height: 6))
    context.restoreGState()

    // Binder rings on the header.
    for x in [330.0, 694.0] {
        context.setFillColor(rgb(0xFFFFFF, 0.92))
        context.addPath(CGPath(roundedRect: CGRect(x: x - 22, y: 770, width: 44, height: 110),
                               cornerWidth: 22, cornerHeight: 22, transform: nil))
        context.fillPath()
    }

    // A 7 × 4 grid of days, one of them checked.
    let columns = 7, rows = 4
    let left = 196.0, right = 828.0, top = 604.0, bottom = 214.0
    let dx = (right - left) / Double(columns - 1)
    let dy = (top - bottom) / Double(rows - 1)
    let checkedRow = 1, checkedColumn = 4
    for row in 0..<rows {
        for column in 0..<columns where !(row == checkedRow && column == checkedColumn) {
            let center = CGPoint(x: left + Double(column) * dx, y: top - Double(row) * dy)
            context.setFillColor(column == 0 ? rgb(0xFF3B30, 0.55) : column == 6 ? rgb(0x0A84FF, 0.5) : rgb(0xB9BDC7))
            context.fillEllipse(in: CGRect(x: center.x - 21, y: center.y - 21, width: 42, height: 42))
        }
    }
    let checked = CGPoint(x: left + Double(checkedColumn) * dx, y: top - Double(checkedRow) * dy)
    context.saveGState()
    context.setShadow(offset: CGSize(width: 0, height: -6), blur: 14, color: rgb(0x0A5FD8, 0.45))
    context.setFillColor(rgb(0x0A84FF))
    context.fillEllipse(in: CGRect(x: checked.x - 78, y: checked.y - 78, width: 156, height: 156))
    context.restoreGState()
    let tick = CGMutablePath()
    tick.move(to: CGPoint(x: checked.x - 38, y: checked.y + 2))
    tick.addLine(to: CGPoint(x: checked.x - 10, y: checked.y - 28))
    tick.addLine(to: CGPoint(x: checked.x + 40, y: checked.y + 30))
    context.addPath(tick)
    context.setStrokeColor(rgb(0xFFFFFF))
    context.setLineWidth(24)
    context.setLineCap(.round)
    context.setLineJoin(.round)
    context.strokePath()

    return context.makeImage()!
}

let variants: [(String, Int)] = [
    ("icon_16x16", 16), ("icon_16x16@2x", 32), ("icon_32x32", 32), ("icon_32x32@2x", 64),
    ("icon_128x128", 128), ("icon_128x128@2x", 256), ("icon_256x256", 256), ("icon_256x256@2x", 512),
    ("icon_512x512", 512), ("icon_512x512@2x", 1024),
]
for (name, pixels) in variants {
    let url = output.appendingPathComponent("\(name).png")
    guard let destination = CGImageDestinationCreateWithURL(url as CFURL, UTType.png.identifier as CFString, 1, nil) else {
        FileHandle.standardError.write(Data("cannot write \(url.path)\n".utf8))
        exit(1)
    }
    CGImageDestinationAddImage(destination, drawIcon(pixels: pixels), nil)
    guard CGImageDestinationFinalize(destination) else {
        FileHandle.standardError.write(Data("cannot finalize \(url.path)\n".utf8))
        exit(1)
    }
}
