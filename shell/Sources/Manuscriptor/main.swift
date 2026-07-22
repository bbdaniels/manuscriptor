import AppKit

// Two flags run the resolution rule without a GUI, so `tests/test_shell.py`
// can hold this binary and `shell/resolve_root.py` to the same answers. They
// are parsed before AppKit is touched; anything else launches the app.
let arguments = Array(CommandLine.arguments.dropFirst())

func flagValue(_ name: String) -> String? {
    guard let i = arguments.firstIndex(of: name) else { return nil }
    return i + 1 < arguments.count ? arguments[i + 1] : ""
}

if let path = flagValue("--resolve-root") {
    do {
        let r = try ManuscriptRoot.resolve(path)
        print("\(r.root.path)\t\(r.main)\t\(r.rel)")
        exit(0)
    } catch {
        FileHandle.standardError.write(Data("\(error)\n".utf8))
        exit(2)
    }
}

if let line = flagValue("--parse-port") {
    if let port = ManuscriptRoot.parsePort(line) {
        print(port)
        exit(0)
    }
    exit(1)
}

let delegate = AppDelegate()
let snapshot = flagValue("--snapshot")
delegate.snapshotPath = (snapshot?.isEmpty == false) ? snapshot : nil
// Finder passes a `-psn_...` argument on some launches; a path never starts
// with a dash. The snapshot destination is a value, not a document.
delegate.openArguments = arguments
    .filter { !$0.hasPrefix("-") && $0 != snapshot }

let app = NSApplication.shared
app.setActivationPolicy(.regular)
app.delegate = delegate
app.run()
