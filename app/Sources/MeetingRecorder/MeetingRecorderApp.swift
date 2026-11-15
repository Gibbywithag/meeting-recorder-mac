import SwiftUI
import AppKit
import UniformTypeIdentifiers
import Combine

@main
struct MeetingRecorderApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    var body: some Scene { Settings { EmptyView() } }   // no regular windows; the UI lives in the menu bar
}

/// Menu-bar presence via AppKit NSStatusItem + NSPopover (SwiftUI MenuBarExtra never produced an item on this Mac).
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let state = AppState()
    private var statusItem: NSStatusItem!
    private let popover = NSPopover()
    private var recorderWindow: NSWindow?
    private var sub: AnyCancellable?
    private var panelSignal: DispatchSourceSignal?

    /// Videos dropped on the app icon or opened with "Open With > Meeting Recorder" are analyzed
    /// as client videos (also how tests start a video analysis without the file picker).
    func application(_ application: NSApplication, open urls: [URL]) {
        let videos = urls.filter { UTType(filenameExtension: $0.pathExtension)?.conforms(to: .movie) ?? false }
        guard let first = videos.first else { state.log("ignored non-video open: \(urls.map(\.lastPathComponent))"); return }
        if state.isRecording || state.isBusy {
            state.status = "Stop the current recording first, then open the video again."
            return
        }
        state.analyzeVideo(first)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.autosaveName = "MeetingRecorderStatusItem"
        statusItem.isVisible = true
        if let b = statusItem.button {
            b.target = self
            b.action = #selector(togglePopover)
            b.imagePosition = .imageLeading
            b.font = NSFont.monospacedDigitSystemFont(ofSize: 12, weight: .medium)
        }
        popover.contentViewController = NSHostingController(rootView: MenuBarView().environmentObject(state))
        state.setUpLoginItem()
        popover.behavior = .transient
        popover.animates = true
        sub = state.objectWillChange.receive(on: DispatchQueue.main).sink { [weak self] _ in
            DispatchQueue.main.async { self?.updateLabel() }
        }
        updateLabel()
        showRecorderWindow()
        // Test hook: `kill -USR2 <pid>` toggles the panel (used by tests/integration_app.sh for screenshots).
        signal(SIGUSR2, SIG_IGN)
        let src = DispatchSource.makeSignalSource(signal: SIGUSR2, queue: .main)
        src.setEventHandler { [weak self] in self?.togglePopover() }
        src.resume()
        panelSignal = src
        state.log("status item created, frame=\(statusItem.button?.window?.frame.debugDescription ?? "nil") visible=\(statusItem.isVisible)")
    }

    private func updateLabel() {
        guard let b = statusItem.button else { return }
        popover.behavior = state.panelPinned ? .applicationDefined : .transient
        recorderWindow?.level = state.panelPinned ? .floating : .normal
        switch state.phase {
        case .recording:
            b.image = NSImage(systemSymbolName: "record.circle.fill", accessibilityDescription: "Recording")
            b.contentTintColor = .systemRed
            b.title = " " + state.elapsedText
        case .starting, .processing:
            b.image = NSImage(systemSymbolName: "arrow.up.circle", accessibilityDescription: "Uploading")
            b.contentTintColor = nil
            b.title = " AI…"
        default:
            b.image = NSImage(systemSymbolName: "video.circle", accessibilityDescription: "Meeting Recorder")
            b.contentTintColor = nil
            b.title = state.jobsRunning ? " AI…" : ""
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showRecorderWindow()
        return true
    }

    private func showRecorderWindow() {
        if recorderWindow == nil {
            let view = MenuBarView().environmentObject(state)
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 432, height: 820), styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
            window.title = "Meeting Recorder"
            window.contentViewController = NSHostingController(rootView: ScrollView { view }.frame(minWidth: 432, minHeight: 700))
            window.contentMinSize = NSSize(width: 432, height: 700)
            window.setContentSize(NSSize(width: 432, height: 820))
            window.isReleasedWhenClosed = false
            window.collectionBehavior = [.moveToActiveSpace, .fullScreenAuxiliary]
            window.center()
            recorderWindow = window
        }
        if let window = recorderWindow, window.frame.height < 700 { window.setContentSize(NSSize(width: 432, height: 820)) }
        recorderWindow?.level = state.panelPinned ? .floating : .normal
        if recorderWindow?.isMiniaturized == true { recorderWindow?.deminiaturize(nil) }
        recorderWindow?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc func togglePopover() { showRecorderWindow() }
}
