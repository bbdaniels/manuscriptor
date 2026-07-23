import AppKit

/// A plain multi-document app: one window and one server per opened directory.
///
/// ClaudeHUD (or Finder, or the CLI) hands Manuscriptor a top-level directory;
/// this opens it in its own window with its own `ServerProcess` and browses the
/// tree, exactly the way Preview opens each PDF in its own window. This object
/// owns nothing but the list of open documents and the app-wide menu: every
/// window's server, web view and lifecycle live in its `DocumentWindow`.
final class AppDelegate: NSObject, NSApplicationDelegate {

    /// Paths handed over on the command line, for `Manuscriptor.app/.../Manuscriptor dir`.
    var openArguments: [String] = []

    /// `--snapshot <path>`: write a PNG of the rendered page and quit. Only the
    /// first window opened in a launch carries it (automated verification opens one).
    var snapshotPath: String?

    /// One entry per open directory. This is the only thing keeping a window and
    /// its server alive; dropping an entry closes the book on that document.
    private var documents: [DocumentWindow] = []
    private var signalSources: [DispatchSourceSignal] = []
    private var openHandled = false

    // MARK: - launch

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        catchTerminationSignals()
        NSApp.activate(ignoringOtherApps: true)

        if !openArguments.isEmpty {
            openHandled = true
            for path in openArguments { open(path: path) }
            return
        }
        // `application(_:open:)` arrives after this on a Finder open, so the
        // no-document fallback waits to see whether one is coming. With nothing
        // to open, present the standard folder chooser — ordinary document-app
        // behaviour on a cold launch.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in
            guard let self, !self.openHandled else { return }
            self.openHandled = true
            self.showOpenPanel(nil)
        }
    }

    func application(_ application: NSApplication, open urls: [URL]) {
        openHandled = true
        for url in urls where url.isFileURL { open(path: url.path) }
    }

    /// The last window closing quits the app: there is no menubar launcher to
    /// persist into anymore.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return true
    }

    func applicationWillTerminate(_ notification: Notification) {
        for doc in documents { doc.stopServer() }
    }

    /// AppKit does not route SIGTERM through `terminate`, so a `pkill` or a
    /// logout would kill the windows and leave the servers running against the
    /// manuscripts with nothing on screen to say so.
    private func catchTerminationSignals() {
        for sig in [SIGTERM, SIGINT] {
            signal(sig, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            source.setEventHandler { NSApp.terminate(nil) }
            source.resume()
            signalSources.append(source)
        }
    }

    // MARK: - opening a directory

    /// Open a directory in its own window + server, or, if one is already showing
    /// that directory, focus it and jump — a restart would throw away the render
    /// and the reader's place for nothing.
    private func open(path: String) {
        let resolved: ManuscriptRoot.Resolved
        do {
            resolved = try ManuscriptRoot.resolve(path)
        } catch {
            presentAlert(title: "Cannot open \(path)", detail: "\(error)")
            return
        }

        if let existing = documents.first(where: { $0.root.path == resolved.root.path }) {
            existing.focus(jumpTo: resolved.rel)
            return
        }

        guard let binary = ServerProcess.locateBinary() else {
            presentAlert(
                title: "manuscriptor is not installed",
                detail: "The app runs `manuscriptor serve` as a child process and could not find "
                      + "it. Install it with `pip install -e ~/Projects/manuscriptor`, or point the "
                      + "app at a copy: defaults write com.bbdaniels.manuscriptor ServerBinary "
                      + "/path/to/manuscriptor")
            return
        }

        let doc = DocumentWindow(resolved: resolved,
                                 binary: binary,
                                 snapshotPath: snapshotPath,
                                 onClose: { [weak self] d in self?.remove(d) })
        snapshotPath = nil          // only the first window photographs itself
        documents.append(doc)
    }

    private func remove(_ doc: DocumentWindow) {
        documents.removeAll { $0 === doc }
    }

    private func presentAlert(title: String, detail: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = detail
        alert.alertStyle = .warning
        alert.runModal()
    }

    // MARK: - menu

    /// About, Open…, Close Window, Quit, Reload, and an Edit menu.
    ///
    /// The Edit menu is not decoration: WKWebView gets Cut, Copy, Paste and
    /// Select All from the main menu's key equivalents, and without it the
    /// source editor cannot be pasted into, which is most of what this app is for.
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

    /// Reload the front document's page (the key window's, falling back to the
    /// most recently opened) so Cmd-R does the obvious thing with many windows open.
    @objc func reload(_ sender: Any?) {
        let target = documents.first(where: { $0.isKey }) ?? documents.last
        target?.reload()
    }
}
