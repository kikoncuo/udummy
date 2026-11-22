// Copilot shell (adds the "window" op, --flag-test and --capture-test). Deliberately thin and generic so it does NOT need rebuilding:
// a floating panel with a web view, the system-audio tap, a screenshot on request, global hotkeys
// chosen by the page. The whole interface is HTML/JS served by the engine on 127.0.0.1.
//
//   Copilot --url "http://127.0.0.1:PORT/?t=TOKEN"                      normal run (through `open`)
//   Copilot --url ... --selftest file.wav [--instruction ".."] [--shot f.jpg] [--model k]
//                                                 no window, no capture: file -> engine -> answer
//   Copilot --render-test "<url>" out.png [--size 480x640]   draws a page into a PNG, nothing on screen
//   Copilot --flag-test        builds the panel WITHOUT showing it, applies the window settings, prints what the window reports
//   Copilot --capture-test [--ask-permission]   FOR THE USER TO RUN: shows a marker panel, takes whole-screen captures (ScreenCaptureKit, as screen
//                              sharing apps do) with the panel hidden and then visible, counts marker pixels in memory, keeps no image
//
// Bridge (page -> shell), window.webkit.messageHandlers.copilot.postMessage({op: ...}) returns a promise:
//   status | hotkeys {keys:[{id,keyCode,modifiers}]} | screenshot {maxWidth, uploadTo} |
//   requestScreenPermission | copy {text} | hide | show | quit | window {onTop?, hideFromCapture?} -> {onTop, hideFromCapture}
// Shell -> page: window.copilotNative({type:"hotkey", id}) and ({type:"tap", ok, message}).
// Captured audio is held for at most a second before it is handed to the engine; nothing is written to disk.
import AppKit
import AVFoundation
import AudioToolbox
import Carbon.HIToolbox
import CoreAudio
import ScreenCaptureKit
import WebKit

let shellVersion = "3.4"

// v3.2: his saved choice of what Preguntar uses (audio | screen | both). In "screen" the app does not listen at all, from launch.
func savedSource() -> String {
    let u = ShellLog.home.appendingPathComponent("settings.json")
    guard let d = try? Data(contentsOf: u), let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any], let s = o["source"] as? String else { return "audio" }
    return s
}

// A content-free log next to the app's engine log (copilot-v3/logs/shell.log): launch, tap result, window events, why it quit.
enum ShellLog {
    // the copilot's folder: from the installed app's Info.plist (CopilotDir), else three levels up from shell/build/<app>
    static let dir: URL = (Bundle.main.object(forInfoDictionaryKey: "CopilotDir") as? String).map { URL(fileURLWithPath: $0) }
        ?? Bundle.main.bundleURL.deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    // the data folder (settings, logs): CopilotHome in the installed app's Info.plist, else ~/Library/Application Support/Copilot
    static let home: URL = (Bundle.main.object(forInfoDictionaryKey: "CopilotHome") as? String).map { URL(fileURLWithPath: $0) }
        ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/Copilot")
    static let url: URL = home.appendingPathComponent("logs/shell.log")
    static var fd: Int32 = -1
    static func open() {
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        fd = Darwin.open(url.path, O_WRONLY | O_APPEND | O_CREAT, 0o600)
    }
    static func write(_ msg: String) {
        guard fd >= 0 else { return }
        let f = DateFormatter(); f.dateFormat = "HH:mm:ss"
        let line = "\(f.string(from: Date())) \(msg)\n"
        _ = line.withCString { Darwin.write(fd, $0, strlen($0)) }
    }
}
// Signals that end the app: written with write(2) only (safe inside a handler).
func installSignalLog() {
    for sig in [SIGTERM, SIGINT, SIGHUP, SIGQUIT] {
        signal(sig) { s in
            let msg = "quit: signal \(s)\n"
            _ = msg.withCString { Darwin.write(ShellLog.fd, $0, strlen($0)) }
            _exit(128 + s)
        }
    }
    NSSetUncaughtExceptionHandler { e in ShellLog.write("quit: uncaught exception \(e.name.rawValue)") }
}

// The panel's top strip: grab it to move the panel (the web view below takes every click, so it cannot be the handle).
final class DragBar: NSView {
    override init(frame: NSRect) {
        super.init(frame: frame)
        let grip = NSView(frame: NSRect(x: (frame.width - 36) / 2, y: (frame.height - 5) / 2, width: 36, height: 5))
        grip.wantsLayer = true; grip.layer?.cornerRadius = 2.5; grip.layer?.backgroundColor = NSColor.tertiaryLabelColor.cgColor
        grip.autoresizingMask = [.minXMargin, .maxXMargin]; addSubview(grip)
        toolTip = "Arrastra para mover"
    }
    required init?(coder: NSCoder) { nil }
    override func mouseDown(with e: NSEvent) { window?.performDrag(with: e) }
    override func resetCursorRects() { addCursorRect(bounds, cursor: .openHand) }
    override var mouseDownCanMoveWindow: Bool { true }
}

// always on top, and hidden from screen sharing / capture (sharingType .none: the window server leaves it out of captures).
func applyWindow(_ p: NSPanel, onTop: Bool?, hide: Bool?) -> [String: Any] {
    if let t = onTop { p.isFloatingPanel = t; p.level = t ? .floating : .normal }
    if let h = hide { p.sharingType = h ? .none : .readOnly }
    return ["ok": true, "onTop": p.level == .floating, "hideFromCapture": p.sharingType == .none]
}

func argValue(_ name: String, offset: Int = 1) -> String? {
    let a = CommandLine.arguments
    guard let i = a.firstIndex(of: name), i + offset < a.count else { return nil }
    return a[i + offset]
}

// MARK: - Engine address

struct Engine {
    let page: URL            // what the web view loads
    let origin: String       // http://127.0.0.1:PORT
    let token: String

    init?(_ s: String) {
        guard let u = URL(string: s), let host = u.host, host == "127.0.0.1", let port = u.port,
              let c = URLComponents(url: u, resolvingAgainstBaseURL: false) else { return nil }
        page = u
        origin = "http://127.0.0.1:\(port)"
        token = c.queryItems?.first { $0.name == "t" }?.value ?? ""
    }

    func url(_ path: String) -> URL { URL(string: origin + path)! }

    /// Synchronous request; never call on the main thread.
    func send(_ path: String, method: String = "POST", body: Data? = nil, type: String = "application/json",
              timeout: TimeInterval = 10) -> (Int, Data) {
        var r = URLRequest(url: url(path), timeoutInterval: timeout)
        r.httpMethod = method
        r.httpBody = body
        r.setValue(type, forHTTPHeaderField: "Content-Type")
        r.setValue(token, forHTTPHeaderField: "X-Copilot-Token")
        var out: (Int, Data) = (0, Data())
        let done = DispatchSemaphore(value: 0)
        Engine.session.dataTask(with: r) { data, resp, _ in
            out = ((resp as? HTTPURLResponse)?.statusCode ?? 0, data ?? Data())
            done.signal()
        }.resume()
        done.wait()
        return out
    }

    static let session: URLSession = {
        let c = URLSessionConfiguration.ephemeral
        c.urlCache = nil
        c.httpCookieStorage = nil
        return URLSession(configuration: c)
    }()
}

// MARK: - Audio out to the engine (0.5 s blocks, in order)

final class AudioForwarder {
    private var pending: [Float] = []
    private var lock = os_unfair_lock()
    private let queue = DispatchQueue(label: "app.copilot.panel.forward")
    private var timer: DispatchSourceTimer?
    let engine: Engine
    var sampleRate: Double = 48_000
    private(set) var sentSamples = 0

    init(engine: Engine) { self.engine = engine }

    func append(_ p: UnsafePointer<Float>, _ n: Int) {
        os_unfair_lock_lock(&lock)
        pending.append(contentsOf: UnsafeBufferPointer(start: p, count: n))
        let cap = Int(sampleRate * 60)            // engine unreachable: keep the last minute only
        if pending.count > cap { pending.removeFirst(pending.count - cap) }
        os_unfair_lock_unlock(&lock)
    }

    func flush() {
        os_unfair_lock_lock(&lock)
        let x = pending
        pending.removeAll(keepingCapacity: true)
        os_unfair_lock_unlock(&lock)
        guard !x.isEmpty else { return }
        var data = Data(count: x.count * 2)
        data.withUnsafeMutableBytes { raw in
            let p = raw.bindMemory(to: Int16.self)
            for i in 0..<x.count { p[i] = Int16(max(-1, min(1, x[i])) * 32767).littleEndian }
        }
        let (code, _) = engine.send("/audio?sr=\(Int(sampleRate))", body: data, type: "application/octet-stream")
        if code == 200 { sentSamples += x.count }
    }

    func start() {
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(deadline: .now() + 0.5, repeating: 0.5)
        t.setEventHandler { [weak self] in self?.flush() }
        t.resume()
        timer = t
    }
}

// MARK: - System audio (Core Audio process tap). Needs "System Audio Recording Only".

struct TapError: Error { let message: String }

final class SystemAudioTap {
    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var aggID = AudioObjectID(kAudioObjectUnknown)
    private var procID: AudioDeviceIOProcID?
    private let queue = DispatchQueue(label: "app.copilot.panel.tap", qos: .userInitiated)
    private var scratch = [Float](repeating: 0, count: 8192)
    private let sink: AudioForwarder

    init(sink: AudioForwarder) { self.sink = sink }

    func start() throws {
        let desc = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
        desc.uuid = UUID()
        desc.name = "Copilot"
        desc.isPrivate = true
        desc.muteBehavior = .unmuted   // listening only: what you hear is not changed
        var tap = AudioObjectID(kAudioObjectUnknown)
        var err = AudioHardwareCreateProcessTap(desc, &tap)
        guard err == noErr else { throw TapError(message: "No se pudo crear la toma de audio (\(err)).") }
        tapID = tap

        var asbd = AudioStreamBasicDescription()
        var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        var addr = AudioObjectPropertyAddress(mSelector: kAudioTapPropertyFormat,
                                              mScope: kAudioObjectPropertyScopeGlobal,
                                              mElement: kAudioObjectPropertyElementMain)
        err = AudioObjectGetPropertyData(tapID, &addr, 0, nil, &size, &asbd)
        guard err == noErr else { stop(); throw TapError(message: "No se pudo leer el formato del audio (\(err)).") }
        guard asbd.mFormatID == kAudioFormatLinearPCM, asbd.mFormatFlags & kAudioFormatFlagIsFloat != 0,
              asbd.mBitsPerChannel == 32, asbd.mSampleRate > 0 else {
            stop(); throw TapError(message: "Formato de audio no previsto.")
        }
        sink.sampleRate = asbd.mSampleRate

        // A private aggregate device that contains only the tap: no output device, no microphone.
        let agg: [String: Any] = [
            kAudioAggregateDeviceNameKey: "Copilot",
            kAudioAggregateDeviceUIDKey: UUID().uuidString,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceTapListKey: [[kAudioSubTapUIDKey: desc.uuid.uuidString,
                                               kAudioSubTapDriftCompensationKey: true]],
        ]
        err = AudioHardwareCreateAggregateDevice(agg as CFDictionary, &aggID)
        guard err == noErr else { stop(); throw TapError(message: "No se pudo crear el dispositivo privado (\(err)).") }

        err = AudioDeviceCreateIOProcIDWithBlock(&procID, aggID, queue) { [weak self] _, input, _, _, _ in
            self?.consume(input)
        }
        guard err == noErr else { stop(); throw TapError(message: "No se pudo leer el audio (\(err)).") }
        err = AudioDeviceStart(aggID, procID)
        guard err == noErr else { stop(); throw TapError(message: "No se pudo empezar a escuchar (\(err)).") }
    }

    /// Mix whatever channels arrive down to mono and hand them to the forwarder.
    private func consume(_ input: UnsafePointer<AudioBufferList>) {
        let list = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
        var frames = 0
        var channels = 0
        for b in list where b.mData != nil && b.mNumberChannels > 0 {
            frames = max(frames, Int(b.mDataByteSize) / (4 * Int(b.mNumberChannels)))
            channels += Int(b.mNumberChannels)
        }
        guard frames > 0, channels > 0 else { return }
        if scratch.count < frames { scratch = [Float](repeating: 0, count: frames) }
        for i in 0..<frames { scratch[i] = 0 }
        for b in list {
            guard let data = b.mData, b.mNumberChannels > 0 else { continue }
            let ch = Int(b.mNumberChannels)
            let n = Int(b.mDataByteSize) / (4 * ch)
            let p = data.assumingMemoryBound(to: Float.self)
            for i in 0..<min(n, frames) {
                var s: Float = 0
                for c in 0..<ch { s += p[i * ch + c] }
                scratch[i] += s
            }
        }
        let g = 1 / Float(channels)
        for i in 0..<frames { scratch[i] *= g }
        scratch.withUnsafeBufferPointer { sink.append($0.baseAddress!, frames) }
    }

    func stop() {
        if aggID != kAudioObjectUnknown {
            if let p = procID {
                AudioDeviceStop(aggID, p)
                AudioDeviceDestroyIOProcID(aggID, p)
            }
            AudioHardwareDestroyAggregateDevice(aggID)
        }
        if tapID != kAudioObjectUnknown { AudioHardwareDestroyProcessTap(tapID) }
        procID = nil
        aggID = AudioObjectID(kAudioObjectUnknown)
        tapID = AudioObjectID(kAudioObjectUnknown)
    }
}

// MARK: - Screenshot (only when the page asks; needs Screen Recording, requested only by the user)

enum Screenshot {
    static var allowed: Bool { CGPreflightScreenCaptureAccess() }

    /// JPEG of the display under the mouse pointer, without this app's own panel.
    static func capture(maxWidth: Double) async -> Data? {
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
            let mouse = NSEvent.mouseLocation
            let screen = NSScreen.screens.first { NSMouseInRect(mouse, $0.frame, false) } ?? NSScreen.main
            let wanted = (screen?.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber)?.uint32Value
            guard let display = content.displays.first(where: { $0.displayID == wanted }) ?? content.displays.first
            else { return nil }
            let mine = content.applications.filter { $0.processID == getpid() }
            let filter = SCContentFilter(display: display, excludingApplications: mine, exceptingWindows: [])
            let cfg = SCStreamConfiguration()
            let scale = min(2.0, maxWidth / Double(display.width))
            cfg.width = Int(Double(display.width) * scale)
            cfg.height = Int(Double(display.height) * scale)
            cfg.showsCursor = false
            let image = try await SCScreenshotManager.captureImage(contentFilter: filter, configuration: cfg)
            return NSBitmapImageRep(cgImage: image).representation(using: .jpeg, properties: [.compressionFactor: 0.7])
        } catch {
            return nil
        }
    }
}

// MARK: - Global hotkeys (Carbon RegisterEventHotKey: no Accessibility, no Input Monitoring)

final class HotKeys {
    static var handlers: [UInt32: () -> Void] = [:]
    private var refs: [EventHotKeyRef] = []
    private var installed = false

    private func install() {
        guard !installed else { return }
        installed = true
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, event, _ in
            var id = EventHotKeyID()
            GetEventParameter(event, EventParamName(kEventParamDirectObject), EventParamType(typeEventHotKeyID),
                              nil, MemoryLayout<EventHotKeyID>.size, nil, &id)
            HotKeys.handlers[id.id]?()
            return noErr
        }, 1, &spec, nil, nil)
    }

    /// Replaces all hotkeys. Returns the ids that could not be registered (taken by another app).
    func set(_ keys: [[String: Any]], fire: @escaping (String) -> Void) -> [String] {
        install()
        for r in refs { UnregisterEventHotKey(r) }
        refs.removeAll()
        HotKeys.handlers.removeAll()
        var failed: [String] = []
        for (i, k) in keys.prefix(8).enumerated() {
            guard let id = k["id"] as? String, let code = k["keyCode"] as? Int else { continue }
            var mods = 0
            for m in k["modifiers"] as? [String] ?? [] {
                switch m {
                case "control": mods |= controlKey
                case "option": mods |= optionKey
                case "command": mods |= cmdKey
                case "shift": mods |= shiftKey
                default: break
                }
            }
            guard mods != 0 else { failed.append(id); continue }   // never a bare key
            var ref: EventHotKeyRef?
            let err = RegisterEventHotKey(UInt32(code), UInt32(mods), EventHotKeyID(signature: OSType(0x4F4D4350), id: UInt32(i + 1)),
                                          GetApplicationEventTarget(), 0, &ref)
            if err == noErr, let ref = ref {
                refs.append(ref)
                HotKeys.handlers[UInt32(i + 1)] = { fire(id) }
            } else {
                failed.append(id)
            }
        }
        return failed
    }
}

// MARK: - Panel + web view

final class CopilotPanel: NSPanel {
    override var canBecomeKey: Bool { true }
}

final class ShellController: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKScriptMessageHandlerWithReply {
    let engine: Engine
    let forwarder: AudioForwarder
    let tap: SystemAudioTap
    let hotkeys = HotKeys()
    var panel: CopilotPanel!
    var web: WKWebView!
    var tapState: [String: Any] = ["ok": false, "message": "starting"]
    var loaded = false
    var retries = 0

    init(engine: Engine) {
        self.engine = engine
        forwarder = AudioForwarder(engine: engine)
        tap = SystemAudioTap(sink: forwarder)
        super.init()
    }

    static func makeWebView(handler: (NSObject & WKScriptMessageHandlerWithReply)?, frame: NSRect) -> WKWebView {
        let cfg = WKWebViewConfiguration()
        cfg.websiteDataStore = .nonPersistent()      // nothing from the page is kept on disk
        if let h = handler { cfg.userContentController.addScriptMessageHandler(h, contentWorld: .page, name: "copilot") }
        let w = WKWebView(frame: frame, configuration: cfg)
        w.setValue(false, forKey: "drawsBackground")
        w.autoresizingMask = [.width, .height]
        w.allowsMagnification = false
        return w
    }

    func applicationDidFinishLaunching(_ n: Notification) {
        panel = CopilotPanel(contentRect: NSRect(x: 0, y: 0, width: 480, height: 640),
                             styleMask: [.titled, .closable, .resizable, .fullSizeContentView, .nonactivatingPanel,
                                         .utilityWindow, .hudWindow],
                             backing: .buffered, defer: false)
        panel.title = "Copilot"
        panel.titleVisibility = .hidden
        panel.titlebarAppearsTransparent = true
        panel.isFloatingPanel = true
        panel.level = .floating
        panel.sharingType = .none            // hidden from capture from the first frame; the page then applies his saved setting
        panel.hidesOnDeactivate = false
        panel.becomesKeyOnlyIfNeeded = false
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.minSize = NSSize(width: 400, height: 420)
        panel.appearance = nil                 // light or dark as the system is; the page follows with prefers-color-scheme
        panel.isMovableByWindowBackground = true
        panel.delegate = self
        panel.isReleasedWhenClosed = false
        let back = NSVisualEffectView(frame: panel.contentLayoutRect)
        back.material = .hudWindow
        back.blendingMode = .behindWindow
        back.state = .active
        let barH: CGFloat = 22
        var webFrame = back.bounds; webFrame.size.height -= barH
        web = ShellController.makeWebView(handler: self, frame: webFrame)
        web.navigationDelegate = self
        back.addSubview(web)
        let bar = DragBar(frame: NSRect(x: 0, y: back.bounds.height - barH, width: back.bounds.width, height: barH))
        bar.autoresizingMask = [.width, .minYMargin]
        back.addSubview(bar)
        panel.contentView = back
        ShellLog.write("launch: shell \(shellVersion), panel \(Int(panel.frame.width))x\(Int(panel.frame.height))")
        if !panel.setFrameUsingName("CopilotPanel"), let s = NSScreen.main?.visibleFrame {
            panel.setFrameOrigin(NSPoint(x: s.maxX - panel.frame.width - 24, y: s.maxY - panel.frame.height - 24))
        }
        panel.setFrameAutosaveName("CopilotPanel")
        panel.orderFrontRegardless()
        web.load(URLRequest(url: engine.page))

        forwarder.start()
        if savedSource() == "screen" {
            tapState = ["ok": true, "off": true]; ShellLog.write("tap: not started (solo pantalla)")
        } else { startTap() }
    }

    var tapRunning = false
    func startTap() {
        guard !tapRunning else { return }
        do {
            try tap.start()        // the first time, macOS asks for "System Audio Recording"
            tapRunning = true
            tapState = ["ok": true]
            ShellLog.write("tap: started")
        } catch let e as TapError {
            tapState = ["ok": false, "message": e.message]; ShellLog.write("tap: failed (\(e.message))")
        } catch {
            tapState = ["ok": false, "message": "No se pudo escuchar el audio del sistema."]; ShellLog.write("tap: failed")
        }
    }
    func stopTap() {
        guard tapRunning else { return }
        tap.stop(); tapRunning = false; tapState = ["ok": true, "off": true]; ShellLog.write("tap: stopped (solo pantalla)")
    }

    // v3.1: closing the panel only hides it (in v3.0 the close button quit the whole app); Salir quits.
    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { false }
    func applicationWillTerminate(_ n: Notification) { ShellLog.write("quit: app terminating (Salir or system)"); tap.stop() }
    func windowShouldClose(_ w: NSWindow) -> Bool { ShellLog.write("window: close button, panel hidden"); w.orderOut(nil); return false }
    func webViewWebContentProcessDidTerminate(_ w: WKWebView) { ShellLog.write("web: content process ended, reloading"); w.load(URLRequest(url: engine.page)) }

    func tell(_ event: [String: Any]) {
        guard loaded, let data = try? JSONSerialization.data(withJSONObject: event),
              let json = String(data: data, encoding: .utf8) else { return }
        web.evaluateJavaScript("window.copilotNative && window.copilotNative(\(json))", completionHandler: nil)
    }

    // Only the engine's own pages are ever shown.
    func webView(_ w: WKWebView, decidePolicyFor a: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        let u = a.request.url
        let ok = u?.scheme == "about" || (u?.host == "127.0.0.1" && u?.port == engine.page.port)
        decisionHandler(ok ? .allow : .cancel)
    }
    func webView(_ w: WKWebView, didFinish n: WKNavigation!) {
        loaded = true
        retries = 0
        var ev = tapState
        ev["type"] = "tap"
        tell(ev)
    }
    func webView(_ w: WKWebView, didFailProvisionalNavigation n: WKNavigation!, withError e: Error) { retry() }
    func webView(_ w: WKWebView, didFail n: WKNavigation!, withError e: Error) { retry() }
    func retry() {
        loaded = false
        retries += 1
        guard retries < 60 else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 1) { [weak self] in
            guard let self = self else { return }
            self.web.load(URLRequest(url: self.engine.page))
        }
    }

    // Requests from the page.
    func userContentController(_ c: WKUserContentController, didReceive message: WKScriptMessage,
                               replyHandler: @escaping (Any?, String?) -> Void) {
        guard let body = message.body as? [String: Any], let op = body["op"] as? String,
              message.frameInfo.isMainFrame, message.frameInfo.request.url?.port == engine.page.port else {
            replyHandler(["ok": false, "error": "bad-request"], nil)
            return
        }
        switch op {
        case "status":
            replyHandler(["ok": true, "version": shellVersion, "tap": tapState, "screenAllowed": Screenshot.allowed,
                          "sampleRate": forwarder.sampleRate], nil)
        case "hotkeys":
            let failed = hotkeys.set(body["keys"] as? [[String: Any]] ?? []) { [weak self] id in
                guard let self = self else { return }
                if !self.panel.isVisible { self.panel.orderFrontRegardless() }
                self.tell(["type": "hotkey", "id": id])
            }
            replyHandler(["ok": failed.isEmpty, "failed": failed], nil)
        case "screenshot":
            guard Screenshot.allowed else { replyHandler(["ok": false, "error": "permission"], nil); return }
            guard let target = body["uploadTo"] as? String, let u = URL(string: target),
                  u.host == "127.0.0.1", u.port == engine.page.port else {
                replyHandler(["ok": false, "error": "bad-target"], nil); return
            }
            let width = body["maxWidth"] as? Double ?? 1568
            let engine = self.engine
            Task.detached {
                var reply: [String: Any] = ["ok": false, "error": "failed"]
                if let jpeg = await Screenshot.capture(maxWidth: max(400, min(3000, width))) {
                    let (code, data) = engine.send(u.path, body: jpeg, type: "image/jpeg")
                    if code == 200, let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any], let id = obj["id"] {
                        reply = ["ok": true, "id": id, "bytes": jpeg.count]
                    }
                }
                let out = reply
                DispatchQueue.main.async { replyHandler(out, nil) }
            }
        case "requestScreenPermission":      // only ever from a button the user pressed
            replyHandler(["ok": CGRequestScreenCaptureAccess()], nil)
        case "copy":
            NSPasteboard.general.clearContents()
            replyHandler(["ok": NSPasteboard.general.setString(body["text"] as? String ?? "", forType: .string)], nil)
        case "window":
            replyHandler(applyWindow(panel, onTop: body["onTop"] as? Bool, hide: body["hideFromCapture"] as? Bool), nil)
        case "audio":                        // his setting: listen (audio / both) or not at all (screen)
            if body["on"] as? Bool == true { startTap() } else { stopTap() }
            var r = tapState; r["listening"] = tapRunning
            replyHandler(r, nil)
        case "openPrivacy":                  // only from a button he pressed: opens System Settings at the right list
            let pane = (body["pane"] as? String) == "screen" ? "Privacy_ScreenCapture" : "Privacy_AudioCapture"
            if let u = URL(string: "x-apple.systempreferences:com.apple.preference.security?\(pane)") { NSWorkspace.shared.open(u) }
            replyHandler(["ok": true], nil)
        case "hide":
            panel.orderOut(nil)
            replyHandler(["ok": true], nil)
        case "show":
            panel.orderFrontRegardless()
            replyHandler(["ok": true], nil)
        case "quit":
            ShellLog.write("quit: Salir pressed")
            replyHandler(["ok": true], nil)
            NSApp.terminate(nil)
        default:
            replyHandler(["ok": false, "error": "unknown-op"], nil)
        }
    }
}

// MARK: - Headless modes

/// No window, no capture: a file goes through the same forwarder and HTTP client as live audio.
func selfTest(engine: Engine, file: String) -> Int32 {
    guard let f = try? AVAudioFile(forReading: URL(fileURLWithPath: file)),
          let buf = AVAudioPCMBuffer(pcmFormat: f.processingFormat, frameCapacity: AVAudioFrameCount(f.length)),
          (try? f.read(into: buf)) != nil, let ch = buf.floatChannelData else {
        print("selftest: cannot read \(file)")
        return 2
    }
    let fw = AudioForwarder(engine: engine)
    fw.sampleRate = f.processingFormat.sampleRate
    let n = Int(buf.frameLength), channels = Int(f.processingFormat.channelCount)
    let block = Int(fw.sampleRate / 2)
    var mono = [Float](repeating: 0, count: 512)
    var pos = 0, sinceFlush = 0
    while pos < n {                       // 512-frame callbacks, flushed every 0.5 s of audio
        let k = min(512, n - pos)
        for i in 0..<k {
            var s: Float = 0
            for c in 0..<channels { s += ch[c][pos + i] }
            mono[i] = s / Float(channels)
        }
        mono.withUnsafeBufferPointer { fw.append($0.baseAddress!, k) }
        pos += k
        sinceFlush += k
        if sinceFlush >= block { fw.flush(); sinceFlush = 0 }
    }
    fw.flush()
    print("selftest: sent \(fw.sentSamples) of \(n) samples at \(Int(fw.sampleRate)) Hz to the engine")
    guard fw.sentSamples == n else { print("selftest: engine did not accept the audio"); return 1 }
    var ask: [String: Any] = ["instruction": argValue("--instruction") ?? ""]
    if let m = argValue("--model") { ask["model"] = m }
    if let shot = argValue("--shot"), let jpeg = FileManager.default.contents(atPath: shot) {
        let (code, data) = engine.send("/shot", body: jpeg, type: "image/jpeg")
        guard code == 200, let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any], let id = obj["id"] else {
            print("selftest: screenshot upload failed (\(code))"); return 1
        }
        ask["shot_id"] = id
        print("selftest: image uploaded, id \(id)")
    }
    let t0 = DispatchTime.now()
    let (code, data) = engine.send("/ask", body: try? JSONSerialization.data(withJSONObject: ask))
    guard code == 200, let started = try? JSONSerialization.jsonObject(with: data) as? [String: Any], let id = started["id"] as? Int else {
        print("selftest: ask refused (\(code))"); return 1
    }
    for _ in 0..<600 {
        usleep(50_000)
        let (_, s) = engine.send("/state", method: "GET")
        guard let state = try? JSONSerialization.jsonObject(with: s) as? [String: Any],
              let history = state["history"] as? [[String: Any]],
              let e = history.first(where: { $0["id"] as? Int == id }), e["finished"] as? Bool == true else { continue }
        let wall = Int((DispatchTime.now().uptimeNanoseconds - t0.uptimeNanoseconds) / 1_000_000)
        if let err = e["error"] as? String { print("error: \(err)"); return 1 }
        print("heard (tail transcription \(e["stt_ms"] ?? "?") ms, \(e["words"] ?? "?") words in the transcript): …\(String((e["heard"] as? String ?? "").suffix(140)))")
        print("first text \(e["first_ms"] ?? "?") ms after ask (model \(e["llm_first_ms"] ?? "?") ms) · complete \(e["done_ms"] ?? "?") ms · seen finished by the shell at \(wall) ms · \(e["model"] ?? "")")
        print(e["answer"] as? String ?? "")
        return (e["answer"] as? String ?? "").count > 10 ? 0 : 1
    }
    print("selftest: no answer within 30 s")
    return 1
}

/// Loads a page in a web view that is never put on screen and writes what it draws to a PNG.
final class Renderer: NSObject, WKNavigationDelegate {
    let out: String
    var web: WKWebView!
    var window: NSWindow!
    init(out: String) { self.out = out }

    func start(url: URL, size: NSSize) {
        window = NSWindow(contentRect: NSRect(x: -20000, y: -20000, width: size.width, height: size.height),
                          styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        web = ShellController.makeWebView(handler: nil, frame: NSRect(origin: .zero, size: size))
        web.navigationDelegate = self
        window.contentView = web
        web.load(URLRequest(url: url))
        DispatchQueue.main.asyncAfter(deadline: .now() + 20) { print("render-test: timed out"); exit(1) }
    }
    func webView(_ w: WKWebView, didFinish n: WKNavigation!) {
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.2) {
            let cfg = WKSnapshotConfiguration()
            w.takeSnapshot(with: cfg) { image, _ in
                guard let image = image, let tiff = image.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
                      let png = rep.representation(using: .png, properties: [:]),
                      (try? png.write(to: URL(fileURLWithPath: self.out))) != nil else {
                    print("render-test: snapshot failed"); exit(1)
                }
                print("render-test: wrote \(self.out) \(rep.pixelsWide)x\(rep.pixelsHigh)")
                exit(0)
            }
        }
    }
    func webView(_ w: WKWebView, didFailProvisionalNavigation n: WKNavigation!, withError e: Error) {
        print("render-test: page did not load"); exit(1)
    }
}

// MARK: - tests of the window settings

func makeTestPanel() -> CopilotPanel {
    let p = CopilotPanel(contentRect: NSRect(x: 0, y: 0, width: 480, height: 640),
                         styleMask: [.titled, .closable, .resizable, .fullSizeContentView, .nonactivatingPanel, .utilityWindow, .hudWindow],
                         backing: .buffered, defer: false)
    p.isReleasedWhenClosed = false
    return p
}

// No window is shown: the panel is created and never ordered front.
func flagTest() -> Int32 {
    let p = makeTestPanel(); var rows: [[String: Any]] = []; var ok = true
    for (t, h) in [(true, true), (false, true), (true, false), (false, false)] {
        let r = applyWindow(p, onTop: t, hide: h)
        let good = (r["onTop"] as? Bool) == t && (r["hideFromCapture"] as? Bool) == h && p.isVisible == false
        ok = ok && good
        rows.append(["asked": ["onTop": t, "hideFromCapture": h], "level": p.level.rawValue, "sharingType": p.sharingType.rawValue, "visible": p.isVisible, "ok": good])
    }
    let r = applyWindow(p, onTop: nil, hide: nil)
    ok = ok && (r["onTop"] as? Bool) == false && (r["hideFromCapture"] as? Bool) == false      // nil changes nothing
    if let d = try? JSONSerialization.data(withJSONObject: ["shell": shellVersion, "rows": rows, "ok": ok], options: [.sortedKeys]) { print(String(data: d, encoding: .utf8)!) }
    return ok ? 0 : 1
}

// Counts the marker colour (pure magenta) in a capture, scaled down, in memory only.
func markerPixels(_ img: CGImage) -> Int {
    let w = 640, h = max(1, Int(Double(img.height) * 640.0 / Double(max(1, img.width))))
    var buf = [UInt8](repeating: 0, count: w * h * 4)
    guard let ctx = CGContext(data: &buf, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4, space: CGColorSpaceCreateDeviceRGB(),
                              bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return -1 }
    ctx.draw(img, in: CGRect(x: 0, y: 0, width: w, height: h))
    var n = 0
    for i in stride(from: 0, to: buf.count, by: 4) where buf[i] > 200 && buf[i + 1] < 70 && buf[i + 2] > 200 { n += 1 }
    return n
}

final class CaptureTest: NSObject, NSApplicationDelegate {
    let panel = makeTestPanel()
    func applicationDidFinishLaunching(_ n: Notification) {
        guard CGPreflightScreenCaptureAccess() else {
            // asks macOS only when HE ran it with --ask-permission (his Run button); otherwise never
            if CommandLine.arguments.contains("--ask-permission") { _ = CGRequestScreenCaptureAccess() }
            print("capture-test: Copilot has no Screen Recording permission yet. Allow it in System Settings > Privacy & Security > Screen & System Audio Recording, then run the test again. Nothing captured.")
            exit(3)
        }
        let v = NSView(frame: panel.contentLayoutRect); v.wantsLayer = true; v.layer?.backgroundColor = NSColor(srgbRed: 1, green: 0, blue: 1, alpha: 1).cgColor
        panel.contentView = v; panel.level = .floating
        if let s = NSScreen.main?.visibleFrame { panel.setFrameOrigin(NSPoint(x: s.midX - 240, y: s.midY - 320)) }
        panel.orderFrontRegardless()
        Task { await self.run() }
    }
    func capture() async -> Int {
        do {
            let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
            guard let display = content.displays.first(where: { $0.displayID == CGMainDisplayID() }) ?? content.displays.first else { return -1 }
            let cfg = SCStreamConfiguration(); cfg.width = display.width; cfg.height = display.height; cfg.showsCursor = false
            let img = try await SCScreenshotManager.captureImage(contentFilter: SCContentFilter(display: display, excludingWindows: []), configuration: cfg)
            return markerPixels(img)                               // the image is dropped here; nothing is written
        } catch { return -1 }
    }
    func run() async {
        var res: [String: Int] = [:]
        for hide in [true, false] {
            await MainActor.run { _ = applyWindow(self.panel, onTop: true, hide: hide) }
            try? await Task.sleep(nanoseconds: 900_000_000)
            res[hide ? "hidden" : "visible"] = await capture()
        }
        await MainActor.run { self.panel.orderOut(nil) }
        let hidden = res["hidden"] ?? -1, visible = res["visible"] ?? -1
        let verdict = visible <= 1000 ? "INVALID (the control capture did not show the visible panel: no result)"
                    : hidden == 0 ? "PASS: hidden from a whole-screen capture (marker pixels hidden 0, visible \(visible))"
                    : "FAIL: the panel still shows in a whole-screen capture (marker pixels hidden \(hidden), visible \(visible))"
        print("capture-test: " + verdict)
        exit(verdict.hasPrefix("PASS") ? 0 : 1)
    }
}

// MARK: - Entry

if CommandLine.arguments.contains("--flag-test") {
    let app = NSApplication.shared
    app.setActivationPolicy(.prohibited)
    exit(flagTest())
}
if CommandLine.arguments.contains("--capture-test") {
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    let t = CaptureTest(); app.delegate = t
    DispatchQueue.main.asyncAfter(deadline: .now() + 30) { print("capture-test: timed out"); exit(4) }
    app.run()
}

if let page = argValue("--render-test"), let out = argValue("--render-test", offset: 2), let url = URL(string: page) {
    var size = NSSize(width: 480, height: 640)
    if let s = argValue("--size")?.split(separator: "x"), s.count == 2, let w = Double(s[0]), let h = Double(s[1]) {
        size = NSSize(width: w, height: h)
    }
    let app = NSApplication.shared
    app.setActivationPolicy(.prohibited)
    let r = Renderer(out: out)
    r.start(url: url, size: size)
    app.run()
    exit(0)
}
// v3.3: opened from Finder / the Dock with no arguments: start run.sh (engine + panel) and step aside; run.sh opens this same app with --url.
if CommandLine.arguments.count == 1 {
    let p = Process(); p.executableURL = URL(fileURLWithPath: "/bin/zsh")
    p.arguments = [ShellLog.dir.appendingPathComponent("run.sh").path]
    var env = ProcessInfo.processInfo.environment; env["COPILOT_APP"] = Bundle.main.bundlePath; env["COPILOT_HOME"] = ShellLog.home.path; p.environment = env
    try? FileManager.default.createDirectory(at: ShellLog.home.appendingPathComponent("logs"), withIntermediateDirectories: true)
    let logURL = ShellLog.home.appendingPathComponent("logs/launcher.log")
    FileManager.default.createFile(atPath: logURL.path, contents: nil, attributes: [.posixPermissions: 0o600])
    if let h = try? FileHandle(forWritingTo: logURL) { h.seekToEndOfFile(); p.standardOutput = h; p.standardError = h }
    p.standardInput = FileHandle.nullDevice
    do { try p.run() } catch { exit(1) }
    exit(0)
}
guard let raw = argValue("--url"), let engine = Engine(raw) else {
    print("usage: Copilot --url http://127.0.0.1:PORT/?t=TOKEN [--selftest file.wav] | --render-test <url> out.png")
    exit(2)
}
if let file = argValue("--selftest") {
    exit(selfTest(engine: engine, file: file))
}
ShellLog.open(); installSignalLog()
let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let controller = ShellController(engine: engine)
app.delegate = controller
app.run()
