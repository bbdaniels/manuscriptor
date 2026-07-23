import AppKit
import WebKit

/// One opened directory: its window, its web view, and its own server child.
///
/// Manuscriptor is a plain multi-document app now (like Preview with two PDFs
/// open): each opened directory gets one of these, and each owns exactly one
/// `ServerProcess`. Closing the window stops that window's server and nothing
/// else. The app quits when the last one closes.
///
/// Nothing here renders, parses or edits: it starts a process, loads a URL, and
/// gets out of the way. It knows nothing about Claude.
final class DocumentWindow: NSObject, WKNavigationDelegate, NSWindowDelegate {

    /// The top-level directory this window serves.
    let root: URL
    private let main: String
    private let rel: String

    private var window: NSWindow!
    private var webView: WKWebView!
    /// The window controller owns the window's memory (it sets
    /// `isReleasedWhenClosed = false` for us), so the window is a normal window
    /// that lives exactly as long as this object is retained in the app's list.
    private var windowController: NSWindowController!
    private let server = ServerProcess()

    private var serverURL: URL?
    private var pendingJump = ""
    private var retried = false

    /// `--snapshot <path>`: photograph the rendered page and quit. Only the
    /// first window opened in a launch carries it.
    private var snapshotPath: String?

    /// Called once, when the window has closed, so the app can drop this object.
    private let onClose: (DocumentWindow) -> Void

    init(resolved: ManuscriptRoot.Resolved,
         binary: URL,
         snapshotPath: String?,
         onClose: @escaping (DocumentWindow) -> Void) {
        self.root = resolved.root
        self.main = resolved.main
        self.rel = resolved.rel
        self.snapshotPath = snapshotPath
        self.onClose = onClose
        super.init()

        buildWindow()
        startServer(binary: binary)
    }

    /// Bring an already-open document to the front and land on a file inside it.
    func focus(jumpTo rel: String) {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        if serverURL != nil {
            jump(to: rel)
        } else {
            pendingJump = rel   // still rendering; the load handler will jump
        }
    }

    func reload() {
        if let url = serverURL { webView.load(URLRequest(url: url)) } else { webView.reload() }
    }

    var isKey: Bool { window?.isKeyWindow ?? false }

    func stopServer() { server.stop() }

    // MARK: - window and web view

    private func buildWindow() {
        let config = WKWebViewConfiguration()
        // Persistent, so the drafts the viewer mirrors into localStorage survive
        // a relaunch the way they survive a reload.
        config.websiteDataStore = .default()
        // In the app the real macOS title bar is transparent and the web content
        // runs up under it (below), so the viewer's own title row IS the top bar.
        // This class lets the viewer inset for the real traffic lights and hide
        // its decorative fake dots — only in the app, never in a browser or the
        // static export.
        config.userContentController.addUserScript(WKUserScript(
            source: "document.documentElement.classList.add('ms-native-titlebar')",
            injectionTime: .atDocumentEnd, forMainFrameOnly: true))

        webView = WKWebView(frame: NSRect(x: 0, y: 0, width: 1280, height: 860),
                            configuration: config)
        webView.navigationDelegate = self
        if #available(macOS 13.3, *) { webView.isInspectable = true }

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1280, height: 860),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false)
        window.title = root.lastPathComponent
        window.representedFilename = root.path
        // Unify the title bar: transparent + full-height content so the viewer's
        // own title row (with the idle/watching indicators) rises into the top
        // bar beside the real traffic lights, instead of sitting as a second bar
        // below it. The top band stays draggable (it is still the real title bar).
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.styleMask.insert(.fullSizeContentView)
        window.contentView = webView
        window.tabbingMode = .disallowed
        window.minSize = NSSize(width: 720, height: 480)
        window.delegate = self
        // A per-directory autosave name so two open documents remember their own
        // frames instead of fighting over one. Restore before naming:
        // setFrameAutosaveName saves the current frame as a side effect, which
        // would overwrite what we are trying to read.
        let name = DocumentWindow.autosaveName(for: root)
        window.setFrameUsingName(name)
        window.setFrameAutosaveName(name)
        if window.frame.isEmpty { window.center() }

        windowController = NSWindowController(window: window)
        window.makeKeyAndOrderFront(nil)
    }

    private static func autosaveName(for root: URL) -> String {
        "MSWindow_" + root.path.replacingOccurrences(of: "/", with: "_")
    }

    // MARK: - the server child

    private func startServer(binary: URL) {
        serverURL = nil
        retried = false
        pendingJump = rel
        show(status: "Rendering \(root.lastPathComponent)…")

        server.start(binary: binary,
                     directory: root,
                     main: main,
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

    // MARK: - window lifecycle

    /// Closing the window stops this window's server and lets the app drop the
    /// object. A server outliving its window is a process quietly holding a
    /// manuscript open with nothing on screen to say so. Removal is deferred one
    /// run-loop turn so this delegate call returns before the object is freed.
    func windowWillClose(_ notification: Notification) {
        server.stop()
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.onClose(self)
        }
    }

    // MARK: - jumping to the opened file

    /// Land the page on the file that was double-clicked.
    ///
    /// The viewer has no `goto` frame yet, so this drives its public API:
    /// `window.MS.blocks` carries each block's file, and `MSViewer.select`
    /// opens one. `keepDoc: false` is load-bearing — without it the inspector's
    /// scroll restore lands on top of the jump and nothing appears to happen.
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
}
