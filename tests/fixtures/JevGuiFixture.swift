import AppKit
import Foundation

// Disposable, local-only native window for the actual Jev + Cua runner smoke test.
let eventLog = CommandLine.arguments[1]
func record(_ line: String) {
    let data = Data((line + "\n").utf8)
    if let handle = FileHandle(forWritingAtPath: eventLog) {
        handle.seekToEndOfFile()
        handle.write(data)
        try? handle.close()
    } else {
        try? data.write(to: URL(fileURLWithPath: eventLog))
    }
}
final class Fixture: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    func applicationDidFinishLaunching(_ note: Notification) {
        window = NSWindow(contentRect: NSRect(x: 240, y: 280, width: 440, height: 270),
                          styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.title = "Jev GUI Fixture - idle"
        let button = NSButton(frame: NSRect(x: 60, y: 100, width: 300, height: 50))
        button.title = "Activate fixture"
        button.target = self
        button.action = #selector(activate(_:))
        window.contentView?.addSubview(button)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        record("ready")
    }
    @objc func activate(_ button: NSButton) {
        record("activated")
        window.title = "Jev GUI Fixture - activated by Jev"
    }
}
let app = NSApplication.shared
app.setActivationPolicy(.regular)
let fixture = Fixture()
app.delegate = fixture
app.run()
