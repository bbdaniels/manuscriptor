import AppKit
import WebKit

/// A window, a web view, a menu, and the server as a child process.
///
/// The server is the product; this is a client, and any number of clients can
/// attach to the same port. Nothing here renders, parses or edits anything: it
/// starts a process, loads a URL, and gets out of the way.
final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKScriptMessageHandler, NSWindowDelegate {

    /// Paths handed over on the command line, for `Manuscriptor.app/.../Manuscriptor file.tex`.
    var openArguments: [String] = []

    /// `--snapshot <path>`: write a PNG of the rendered page and quit.
    ///
    /// `screencapture` needs Screen Recording consent, which an automated
    /// session does not have, and "it compiled" is not evidence that anything
    /// is on screen. A web view can photograph itself, so the app can show its
    /// own work.
    var snapshotPath: String?

    private var window: NSWindow!
    private var webView: WKWebView!
    private let server = ServerProcess()

    private var currentRoot: URL?
    private var pendingJump = ""
    private var serverURL: URL?
    private var openHandled = false
    private var retried = false
    private var signalSources: [DispatchSourceSignal] = []
    private var openRecentItem: NSMenuItem?
    private var statusItem: NSStatusItem?

    private static let frameName = "ManuscriptorMainWindow"
    private static let lastKey = "LastManuscript"
    private static let recentsKey = "RecentManuscripts"
    private static let recentsMax = 12

    // MARK: - recents

    /// The recently opened manuscript roots, most-recent-first, filtered to
    /// paths that still exist.
    func recents() -> [String] {
        let raw = UserDefaults.standard.stringArray(forKey: AppDelegate.recentsKey) ?? []
        return raw.filter { FileManager.default.fileExists(atPath: $0) }
    }

    func pushRecent(_ path: String) {
        var list = UserDefaults.standard.stringArray(forKey: AppDelegate.recentsKey) ?? []
        list.removeAll { $0 == path }
        list.insert(path, at: 0)
        list = Array(list.prefix(AppDelegate.recentsMax))
        UserDefaults.standard.set(list, forKey: AppDelegate.recentsKey)
    }

    // MARK: - launch

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        buildWindow()
        buildStatusItem()
        catchTerminationSignals()
        NSApp.activate(ignoringOtherApps: true)

        if let first = openArguments.first {
            openHandled = true
            open(path: first)
            return
        }
        // `application(_:open:)` arrives after this on a Finder open, so the
        // no-document fallback waits to see whether one is coming.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in
            guard let self, !self.openHandled else { return }
            self.openHandled = true
            let fm = FileManager.default
            if let last = UserDefaults.standard.string(forKey: AppDelegate.lastKey),
               fm.fileExists(atPath: last) {
                self.open(path: last)
            } else {
                self.loadHome()
            }
        }
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        guard let first = urls.first(where: { $0.isFileURL }) else { return }
        openHandled = true
        open(path: first.path)
    }

    /// The app persists in the menubar with no window, so closing the window no
    /// longer quits. The server is still stopped on window close (see
    /// `windowWillClose`) — the invariant is that the server never outlives its
    /// window, not that the app dies with it.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return false
    }

    /// Closing the window stops the server and drops the app to a menubar-only
    /// launcher. A server outliving its window is a process quietly holding a
    /// manuscript open with nothing on screen to say so.
    func windowWillClose(_ notification: Notification) {
        server.stop()                            // the server never outlives its window
        currentRoot = nil
        serverURL = nil
        NSApp.setActivationPolicy(.accessory)    // live on as a menubar launcher
    }

    func applicationWillTerminate(_ notification: Notification) {
        server.stop()
    }

    /// AppKit does not route SIGTERM through `terminate`, so a `pkill` or a
    /// logout would kill the window and leave the server running against the
    /// manuscript with nothing on screen to say so.
    private func catchTerminationSignals() {
        for sig in [SIGTERM, SIGINT] {
            signal(sig, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            source.setEventHandler { NSApp.terminate(nil) }
            source.resume()
            signalSources.append(source)
        }
    }

    // MARK: - window and web view

    private func buildWindow() {
        let config = WKWebViewConfiguration()
        // Persistent, so the drafts the viewer mirrors into localStorage
        // survive a relaunch the way they survive a reload.
        config.websiteDataStore = .default()
        // The home surface posts open/openPanel actions back over this channel.
        config.userContentController.add(self, name: "ms")

        webView = WKWebView(frame: NSRect(x: 0, y: 0, width: 1280, height: 860),
                            configuration: config)
        webView.navigationDelegate = self
        if #available(macOS 13.3, *) { webView.isInspectable = true }

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1280, height: 860),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false)
        window.title = "Manuscriptor"
        window.contentView = webView
        window.tabbingMode = .disallowed
        window.minSize = NSSize(width: 720, height: 480)
        // The app persists in the menubar after the window closes, so the window
        // must survive its own close to be reshown from the quill.
        window.isReleasedWhenClosed = false
        window.delegate = self
        // Restore before naming: setFrameAutosaveName saves the current frame
        // as a side effect, which would overwrite what we are trying to read.
        window.setFrameUsingName(AppDelegate.frameName)
        window.setFrameAutosaveName(AppDelegate.frameName)
        if window.frame.isEmpty { window.center() }
        window.makeKeyAndOrderFront(nil)
    }

    // MARK: - the home surface

    /// The cold-open front door: a bundled page listing vault projects and
    /// recents, each routed back through `open(path:)`. Falls back to the open
    /// panel if the resource is somehow missing, so the app stays usable.
    private func loadHome() {
        guard let url = Bundle.main.url(forResource: "home", withExtension: "html") else {
            showOpenPanel(nil); return   // fallback keeps the app usable
        }
        let projectsJSON = ServerProcess.projectsJSON()          // "[]" on failure
        let recentsJSON = (try? JSONSerialization.data(withJSONObject: recents()))
            .flatMap { String(data: $0, encoding: .utf8) } ?? "[]"
        let inject = "window.__ms_data__={projects:\(projectsJSON),recents:\(recentsJSON)};"
        let js = WKUserScript(source: inject, injectionTime: .atDocumentStart, forMainFrameOnly: true)
        // Re-injecting fresh data on every home load, not stacking stale copies.
        webView.configuration.userContentController.removeAllUserScripts()
        webView.configuration.userContentController.addUserScript(js)
        webView.loadFileURL(url, allowingReadAccessTo: url.deletingLastPathComponent())
    }

    func userContentController(_ ucc: WKUserContentController, didReceive message: WKScriptMessage) {
        guard message.name == "ms", let body = message.body as? [String: Any],
              let action = body["action"] as? String else { return }
        let arg = body["arg"] as? String ?? ""
        switch action {
        case "open" where !arg.isEmpty: openHandled = true; open(path: arg)
        case "openPanel": showOpenPanel(nil)
        default: break
        }
    }

    // MARK: - opening a manuscript

    private func open(path: String) {
        let resolved: ManuscriptRoot.Resolved
        do {
            resolved = try ManuscriptRoot.resolve(path)
        } catch {
            present(error: "Cannot open \(path)", detail: "\(error)")
            return
        }

        UserDefaults.standard.set(resolved.root.path, forKey: AppDelegate.lastKey)
        pushRecent(resolved.root.path)
        rebuildOpenRecentMenu()
        window.title = resolved.root.lastPathComponent
        window.representedFilename = resolved.root.path

        // Already serving this manuscript: jump rather than restart. A restart
        // would throw away the render and the reader's place for nothing.
        if let cur = currentRoot, cur.path == resolved.root.path {
            jump(to: resolved.rel)
            return
        }

        guard let binary = ServerProcess.locateBinary() else {
            present(error: "manuscriptor is not installed",
                    detail: """
                    The app runs <code>manuscriptor serve</code> as a child process and could not \
                    find it. Install it with <code>pip install -e ~/Projects/manuscriptor</code>, \
                    or point the app at a copy:<br><br>
                    <code>defaults write com.bbdaniels.manuscriptor ServerBinary /path/to/manuscriptor</code>
                    """)
            return
        }

        currentRoot = resolved.root
        pendingJump = resolved.rel
        serverURL = nil
        retried = false
        show(status: "Rendering \(resolved.root.lastPathComponent)…")

        // A manuscript window is open: become a regular app so the Dock icon and
        // the Edit-menu key equivalents (Cmd-V into the source editor) are live.
        NSApp.setActivationPolicy(.regular)
        server.start(binary: binary,
                     directory: resolved.root,
                     main: resolved.main,
                     onURL: { [weak self] url in
                         guard let self else { return }
                         self.serverURL = url
                         self.webView.load(URLRequest(url: url))
                     },
                     onLog: { line in
                         FileHandle.standardError.write(Data(("[server] " + line + "\n").utf8))
                     },
                     onExit: { [weak self] status in
                         guard let self, self.serverURL == nil else { return }
                         self.present(error: "The server stopped before it served anything",
                                      detail: "Exit status \(status). Run the same command in a "
                                            + "terminal to see why.")
                     })
    }

    // MARK: - jumping to the opened file

    /// Land the page on the file that was double-clicked.
    ///
    /// The viewer has no `goto` frame yet, so this drives its public API:
    /// `window.MS.blocks` carries each block's file, and `MSViewer.select`
    /// opens one. `keepDoc: false` is load-bearing — without it the inspector's
    /// scroll restore lands on top of the jump and nothing appears to happen,
    /// which is the fight recorded in the technical notes.
    private func jump(to rel: String) {
        guard !rel.isEmpty else { return }
        let want = jsString(rel)
        let js = """
        (function (want) {
          var V = window.MSViewer, MS = window.MS;
          if (!V || !MS || !MS.blocks) return 'no-viewer';
          if (typeof V.handle === 'function' && MS.gotoFrame) {
            V.handle({ type: 'goto', file: want });
            return 'frame';
          }
          var els = document.querySelectorAll('[data-mx]');
          var firstEl = null, firstId = null, hitEl = null, hitId = null;
          for (var i = 0; i < els.length; i++) {
            var raw = els[i].getAttribute('data-mx');
            var key = (MS.blocks[raw] !== undefined) ? raw
                    : (MS.blocks['b-' + raw] !== undefined) ? 'b-' + raw
                    : raw.replace(/^b-/, '');
            var b = MS.blocks[key];
            if (!b || String(b.file || '') !== want) continue;
            if (!firstEl) { firstEl = els[i]; firstId = key; }
            // The first block of a file is routinely \\clearpage, and landing
            // on an editor holding one command reads as the wrong place.
            // Prefer the first block that shows the reader something.
            if ((els[i].textContent || '').trim() ||
                els[i].querySelector('img,table,svg')) { hitEl = els[i]; hitId = key; break; }
          }
          if (!hitEl) { hitEl = firstEl; hitId = firstId; }
          if (!hitEl) return 'no-block:' + want;
          if (typeof V.select === 'function') {
            V.select('block', hitId, hitId, undefined, { keepDoc: false });
          }
          hitEl.scrollIntoView({ block: 'center' });
          return hitId;
        })(\(want));
        """
        webView.evaluateJavaScript(js) { result, error in
            let what = error.map { "\($0)" } ?? "\(result ?? "nil")"
            FileHandle.standardError.write(Data("[shell] goto \(rel) -> \(what)\n".utf8))
        }
    }

    private func jsString(_ s: String) -> String {
        let data = try? JSONSerialization.data(withJSONObject: [s], options: [])
        let arr = data.flatMap { String(data: $0, encoding: .utf8) } ?? "[\"\"]"
        return String(arr.dropFirst().dropLast())
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        // The status page finishes loading too, and has no viewer to talk to.
        // Firing the jump there consumed it and the page never moved.
        guard let served = serverURL,
              webView.url?.host == served.host, webView.url?.port == served.port
        else { return }

        if !pendingJump.isEmpty {
            let rel = pendingJump
            pendingJump = ""
            // The viewer boots on DOMContentLoaded and hydrates from window.MS;
            // one turn of the run loop is enough for it to have run.
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { [weak self] in
                self?.jump(to: rel)
            }
        }
        if let path = snapshotPath {
            snapshotPath = nil
            DispatchQueue.main.asyncAfter(deadline: .now() + 2.5) { [weak self] in
                self?.writeSnapshot(to: path)
            }
        }
    }

    private func writeSnapshot(to path: String) {
        let config = WKSnapshotConfiguration()
        config.afterScreenUpdates = true
        webView.takeSnapshot(with: config) { image, error in
            defer { NSApp.terminate(nil) }
            guard let image,
                  let tiff = image.tiffRepresentation,
                  let rep = NSBitmapImageRep(data: tiff),
                  let png = rep.representation(using: .png, properties: [:]) else {
                FileHandle.standardError.write(
                    Data("[shell] snapshot failed: \(error.map { "\($0)" } ?? "no image")\n".utf8))
                return
            }
            try? png.write(to: URL(fileURLWithPath: path))
            FileHandle.standardError.write(
                Data("[shell] snapshot \(rep.pixelsWide)x\(rep.pixelsHigh) -> \(path)\n".utf8))
        }
    }

    func webView(_ webView: WKWebView,
                 didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) {
        guard let url = serverURL, !retried else {
            present(error: "Could not load the manuscript", detail: "\(error.localizedDescription)")
            return
        }
        retried = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) {
            webView.load(URLRequest(url: url))
        }
    }

    // MARK: - the page shown when there is nothing to show

    private func show(status: String) {
        webView.loadHTMLString(page(title: status, body: ""), baseURL: nil)
    }

    private func present(error: String, detail: String) {
        webView.loadHTMLString(page(title: error, body: detail), baseURL: nil)
    }

    private func page(title: String, body: String) -> String {
        """
        <!doctype html><meta charset="utf-8">
        <style>
          :root { color-scheme: light dark; }
          body { margin: 0; height: 100vh; display: grid; place-items: center;
                 font: 15px/1.6 -apple-system, system-ui, sans-serif;
                 background: Canvas; color: CanvasText; }
          .box { max-width: 34rem; padding: 2rem; }
          h1 { font-size: 1.05rem; font-weight: 600; margin: 0 0 .6rem; }
          p { margin: 0; opacity: .72; }
          code { font: 12.5px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
                 background: color-mix(in srgb, CanvasText 8%, transparent);
                 padding: .1em .35em; border-radius: 4px; }
        </style>
        <div class="box"><h1>\(title)</h1><p>\(body)</p></div>
        """
    }

    // MARK: - menu

    /// About, Open…, Close Window, Quit, Reload, and an Edit menu.
    ///
    /// The Edit menu is not decoration: WKWebView gets Cut, Copy, Paste and
    /// Select All from the main menu's key equivalents, and without it the
    /// source editor cannot be pasted into, which is most of what this app is
    /// for.
    private func buildMenu() {
        let main = NSMenu()

        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About Manuscriptor",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
                        keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Quit Manuscriptor",
                        action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        main.addItem(appItem)

        let fileItem = NSMenuItem()
        let fileMenu = NSMenu(title: "File")
        fileMenu.addItem(withTitle: "Open…", action: #selector(showOpenPanel(_:)), keyEquivalent: "o")
            .target = self
        let recent = NSMenuItem(title: "Open Recent", action: nil, keyEquivalent: "")
        fileMenu.addItem(recent)
        openRecentItem = recent
        rebuildOpenRecentMenu()
        fileMenu.addItem(withTitle: "Close Window",
                         action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        fileItem.submenu = fileMenu
        main.addItem(fileItem)

        let editItem = NSMenuItem()
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        editMenu.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        editMenu.addItem(.separator())
        editMenu.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        editMenu.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        editMenu.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        editMenu.addItem(withTitle: "Select All",
                         action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = editMenu
        main.addItem(editItem)

        let viewItem = NSMenuItem()
        let viewMenu = NSMenu(title: "View")
        viewMenu.addItem(withTitle: "Reload", action: #selector(reload(_:)), keyEquivalent: "r")
            .target = self
        viewItem.submenu = viewMenu
        main.addItem(viewItem)

        NSApp.mainMenu = main
    }

    private func rebuildOpenRecentMenu() {
        let submenu = NSMenu(title: "Open Recent")
        for path in recents() {
            let title = (path as NSString).lastPathComponent
            let item = NSMenuItem(title: title, action: #selector(openRecentPath(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = path
            submenu.addItem(item)
        }
        openRecentItem?.submenu = submenu
        openRecentItem?.isEnabled = !recents().isEmpty
    }

    @objc private func openRecentPath(_ sender: NSMenuItem) {
        guard let path = sender.representedObject as? String else { return }
        openHandled = true
        open(path: path)
    }

    @objc func showOpenPanel(_ sender: Any?) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.message = "Choose a .tex file or a manuscript directory."
        panel.prompt = "Open"
        if panel.runModal() == .OK, let url = panel.url {
            openHandled = true
            open(path: url.path)
        }
    }

    @objc func reload(_ sender: Any?) {
        if let url = serverURL { webView.load(URLRequest(url: url)) } else { webView.reload() }
    }

    // MARK: - menubar quill

    /// The always-present menubar launcher. The app lives here with no window,
    /// so the quill is how a manuscript is opened once every window is closed.
    private func buildStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        // TODO: replace with final quill art
        if let img = NSImage(named: "quill") ?? Bundle.main.image(forResource: "quill") {
            img.isTemplate = true
            img.size = NSSize(width: 18, height: 18)
            item.button?.image = img
        } else {
            item.button?.title = "✒"
        }
        // Differentiated clicks: left = focus the work, right/control-click = the
        // menu. So we do NOT set item.menu (that pops the menu on ANY click); we
        // take a button action and inspect the event instead.
        item.button?.target = self
        item.button?.action = #selector(statusClicked(_:))
        item.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])
        statusItem = item
    }

    @objc private func statusClicked(_ sender: Any?) {
        let ev = NSApp.currentEvent
        let wantsMenu = ev?.type == .rightMouseUp
            || (ev?.modifierFlags.contains(.control) ?? false)
        if wantsMenu, let button = statusItem?.button {
            let menu = buildStatusMenu()
            menu.popUp(positioning: nil,
                       at: NSPoint(x: 0, y: button.bounds.height + 4),
                       in: button)
        } else {
            showWindow(nil)   // left-click: focus the window, or open the home
        }
    }

    private func buildStatusMenu() -> NSMenu {
        let menu = NSMenu()
        for it in projectsMenuItems() { menu.addItem(it) }
        for path in recents() {
            let m = NSMenuItem(title: (path as NSString).lastPathComponent,
                               action: #selector(openRecentPath(_:)), keyEquivalent: "")
            m.target = self; m.representedObject = path; menu.addItem(m)
        }
        menu.addItem(.separator())
        let open = NSMenuItem(title: "Open Folder…", action: #selector(showOpenPanel(_:)), keyEquivalent: "")
        open.target = self; menu.addItem(open)
        let show = NSMenuItem(title: "Show Window", action: #selector(showWindow(_:)), keyEquivalent: "")
        show.target = self; menu.addItem(show)
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit Manuscriptor", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "")
        return menu
    }

    private func projectsMenuItems() -> [NSMenuItem] {
        guard let data = ServerProcess.projectsJSON().data(using: .utf8),
              let arr = try? JSONSerialization.jsonObject(with: data) as? [[String: Any]]
        else { return [] }
        return arr.compactMap { p in
            guard let name = p["name"] as? String, let root = p["root"] as? String else { return nil }
            let m = NSMenuItem(title: name, action: #selector(openProject(_:)), keyEquivalent: "")
            m.target = self; m.representedObject = root; return m
        }
    }

    @objc private func openProject(_ sender: NSMenuItem) {
        guard let root = sender.representedObject as? String else { return }
        openHandled = true; open(path: root)
    }

    /// Left-clicking the quill (or "Show Window"): bring the manuscript window
    /// forward, or present the home surface if no window is showing.
    @objc func showWindow(_ sender: Any?) {
        NSApp.setActivationPolicy(.regular)
        if let w = window, w.isVisible {
            w.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        } else {
            loadHome()
            window?.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        }
    }
}
