// Snapshot.swift — renders the real window views with DemoBackend data into
// PNG files, offscreen. Used to check layout without touching Reminders:
//
//   bash scripts/build-due-picker-app.sh --snapshots OUTPUT_DIR

import AppKit
import SwiftUI

/// A titled window that may sit outside every screen while it renders.
final class OffscreenWindow: NSWindow {
    override func constrainFrameRect(_ frameRect: NSRect, to screen: NSScreen?) -> NSRect { frameRect }
}

final class Flag {
    var done = false
}

@main
enum SnapshotTool {
    @MainActor
    static func main() {
        let arguments = CommandLine.arguments
        guard arguments.count == 2 else {
            FileHandle.standardError.write(Data("usage: snapshot OUTPUT_DIR\n".utf8))
            exit(64)
        }
        let output = URL(fileURLWithPath: arguments[1], isDirectory: true)
        try? FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.prohibited)

        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "Asia/Seoul")!
        calendar.locale = Locale(identifier: "ko_KR")
        calendar.firstWeekday = 1
        let now = calendar.date(from: DateComponents(year: 2026, month: 9, day: 30, hour: 18, minute: 0))!

        func makeModel(_ configure: (DemoBackend) -> Void = { _ in }) -> AppModel {
            let backend = DemoBackend(now: now, calendar: calendar)
            configure(backend)
            let model = AppModel(backend: backend, now: { now }, tickClock: false)
            let flag = Flag()
            Task { @MainActor in
                await model.start()
                flag.done = true
            }
            pump(until: { flag.done }, limit: 5)
            return model
        }

        let single = makeModel()
        single.selection = ["r-quiz"]
        single.dateInput = "다음 주 금 오후 3시"
        render(single, appearance: .aqua, to: output.appendingPathComponent("due-picker-single.png"))

        let multiple = makeModel()
        multiple.selection = ["r-scholarship", "r-gym", "r-backup"]
        multiple.apply(.tomorrow)
        render(multiple, appearance: .darkAqua, to: output.appendingPathComponent("due-picker-multiple-dark.png"))

        let denied = makeModel { $0.access = .denied }
        render(denied, appearance: .aqua, size: NSSize(width: 1040, height: 660),
               to: output.appendingPathComponent("due-picker-access.png"))

        // Offscreen capture can leave out the vibrant sidebar and, depending on
        // the display, the inspector's scroll view; render both on their own too.
        let sidebarModel = makeModel()
        sidebarModel.filter = .today
        renderView(SidebarView(model: sidebarModel, plainStyle: true).frame(width: 240, height: 420),
                   appearance: .aqua, size: NSSize(width: 240, height: 420),
                   to: output.appendingPathComponent("due-picker-sidebar.png"))
        renderView(InspectorPreview(model: single), appearance: .aqua, size: NSSize(width: 390, height: 880),
                   to: output.appendingPathComponent("due-picker-inspector.png"))
        renderView(InspectorPreview(model: multiple), appearance: .darkAqua, size: NSSize(width: 390, height: 880),
                   to: output.appendingPathComponent("due-picker-inspector-multiple-dark.png"))
        exit(0)
    }

    @MainActor
    static func renderView<V: View>(_ view: V, appearance: NSAppearance.Name, size: NSSize, to url: URL) {
        let hosting = NSHostingView(rootView: view
            .frame(width: size.width, height: size.height)
            .background(Color(nsColor: .windowBackgroundColor)))
        hosting.frame = NSRect(origin: .zero, size: size)
        let window = OffscreenWindow(contentRect: NSRect(origin: NSPoint(x: -30_000, y: -30_000), size: size),
                                     styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        NSApp.appearance = NSAppearance(named: appearance)
        window.appearance = NSAppearance(named: appearance)
        window.contentView = hosting
        window.orderFrontRegardless()
        pump(limit: 1.0)
        write(hosting, to: url)
        window.orderOut(nil)
        window.close()
    }

    @MainActor
    static func write(_ view: NSView, to url: URL) {
        view.layoutSubtreeIfNeeded()
        guard let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) else {
            FileHandle.standardError.write(Data("could not allocate bitmap\n".utf8))
            exit(1)
        }
        view.cacheDisplay(in: view.bounds, to: bitmap)
        guard let data = bitmap.representation(using: .png, properties: [:]) else {
            FileHandle.standardError.write(Data("could not encode PNG\n".utf8))
            exit(1)
        }
        do {
            try data.write(to: url)
            print("wrote \(url.path)")
        } catch {
            FileHandle.standardError.write(Data("could not write \(url.path): \(error)\n".utf8))
            exit(1)
        }
    }

    @MainActor
    static func pump(until condition: () -> Bool = { false }, limit: TimeInterval) {
        let end = Date().addingTimeInterval(limit)
        while Date() < end && !condition() {
            RunLoop.main.run(mode: .default, before: Date().addingTimeInterval(0.02))
        }
    }

    @MainActor
    static func render(_ model: AppModel, appearance: NSAppearance.Name,
                       size: NSSize = NSSize(width: 1220, height: 880), to url: URL) {
        let origin = NSPoint(x: -30_000, y: -30_000)
        let window = OffscreenWindow(
            contentRect: NSRect(origin: origin, size: size),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        // Standalone hosting views resolve the app's appearance, not the
        // window's, so pin both to render the same thing on any system setting.
        NSApp.appearance = NSAppearance(named: appearance)
        window.appearance = NSAppearance(named: appearance)
        window.title = "미리알림 날짜"
        let hosting = NSHostingView(rootView: ContentView(model: model))
        hosting.sceneBridgingOptions = [.toolbars, .title]
        window.contentView = hosting
        window.setFrame(NSRect(origin: origin, size: size), display: false)
        window.orderFrontRegardless()
        pump(limit: 1.5)
        write(window.contentView?.superview ?? hosting, to: url)
        window.orderOut(nil)
        window.close()
    }
}

/// The date panel on its own, with the focus state MainView normally owns.
struct InspectorPreview: View {
    @ObservedObject var model: AppModel
    @FocusState private var focus: FocusTarget?

    var body: some View { InspectorView(model: model, focus: $focus) }
}
