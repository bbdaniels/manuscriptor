import Foundation

/// The app owns the server.
///
/// `manuscriptor serve <dir> --no-window --port 0` is launched as a child, the
/// port is read off its stdout, and the child is terminated when the app quits.
/// A server outliving its window is a process quietly holding a manuscript
/// open: still watching the tree, still writing on save, with nothing on screen
/// to say so.
///
/// This file knows nothing about Claude and talks to nothing but a process.
final class ServerProcess {

    private var process: Process?
    private var pipe: Pipe?
    private var buffer = Data()
    private var announced = false

    var isRunning: Bool { process?.isRunning ?? false }

    // MARK: - finding the command

    /// A Finder-launched app inherits `PATH=/usr/bin:/bin:/usr/sbin:/sbin`, so
    /// the shell has to look for pip's user bin directory itself. Without this
    /// the app works from a terminal and fails on a double-click, which is the
    /// only way anyone will ever launch it.
    static var searchDirectories: [String] {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        var dirs = ["\(home)/.local/bin", "/usr/local/bin", "/opt/homebrew/bin"]
        let pyRoot = "\(home)/Library/Python"
        if let versions = try? FileManager.default.contentsOfDirectory(atPath: pyRoot) {
            dirs += versions.sorted().reversed().map { "\(pyRoot)/\($0)/bin" }
        }
        return dirs
    }

    static func locateBinary() -> URL? {
        var candidates: [String] = []
        if let env = ProcessInfo.processInfo.environment["MANUSCRIPTOR_BIN"], !env.isEmpty {
            candidates.append((env as NSString).expandingTildeInPath)
        }
        if let pref = UserDefaults.standard.string(forKey: "ServerBinary"), !pref.isEmpty {
            candidates.append((pref as NSString).expandingTildeInPath)
        }
        candidates += searchDirectories.map { "\($0)/manuscriptor" }
        candidates.append("/usr/bin/manuscriptor")
        for c in candidates where FileManager.default.isExecutableFile(atPath: c) {
            return URL(fileURLWithPath: c)
        }
        return nil
    }

    /// The child needs pandoc and pdftotext, which live in `/usr/local/bin`
    /// here and are therefore invisible to a Finder-launched process.
    private static func childEnvironment() -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        let inherited = env["PATH"] ?? "/usr/bin:/bin:/usr/sbin:/sbin"
        env["PATH"] = (searchDirectories + [inherited]).joined(separator: ":")
        // Python block-buffers a piped stdout. Without this the banner sits in
        // the child's buffer and the window loads nothing: measured at zero
        // bytes after twelve seconds against the demo manuscript.
        env["PYTHONUNBUFFERED"] = "1"
        return env
    }

    // MARK: - lifecycle

    /// Start the server. `onURL` fires once, with the address to load.
    func start(binary: URL,
               directory: URL,
               main: String,
               onURL: @escaping (URL) -> Void,
               onLog: @escaping (String) -> Void,
               onExit: @escaping (Int32) -> Void) {
        stop()
        announced = false
        buffer = Data()

        var args = ["serve", directory.path, "--no-window", "--port", "0"]
        // The server's own fallback is the alphabetically first .tex, which is
        // wrong exactly when the root was found by the documentclass rule.
        if !main.isEmpty && main != ManuscriptRoot.mainName {
            args += ["--main", main]
        }

        let p = Process()
        p.executableURL = binary
        p.arguments = args
        p.currentDirectoryURL = directory
        p.environment = ServerProcess.childEnvironment()

        let out = Pipe()
        p.standardOutput = out
        p.standardError = out          // one stream, so a traceback is visible too
        p.standardInput = FileHandle.nullDevice

        out.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let chunk = handle.availableData
            guard !chunk.isEmpty else { return }
            // The pipe must always be drained. A child whose stdout buffer
            // fills blocks on write and the manuscript stops rendering.
            self?.consume(chunk, onURL: onURL, onLog: onLog)
        }

        p.terminationHandler = { proc in
            DispatchQueue.main.async { onExit(proc.terminationStatus) }
        }

        do {
            try p.run()
        } catch {
            onLog("could not start \(binary.path): \(error)")
            DispatchQueue.main.async { onExit(-1) }
            return
        }
        process = p
        pipe = out
    }

    private func consume(_ chunk: Data,
                         onURL: @escaping (URL) -> Void,
                         onLog: @escaping (String) -> Void) {
        buffer.append(chunk)
        while let nl = buffer.firstIndex(of: UInt8(ascii: "\n")) {
            let line = String(decoding: buffer[buffer.startIndex..<nl], as: UTF8.self)
            buffer.removeSubrange(buffer.startIndex...nl)
            DispatchQueue.main.async { onLog(line) }
            if !announced, let port = ManuscriptRoot.parsePort(line) {
                announced = true
                let url = URL(string: "http://127.0.0.1:\(port)/")!
                DispatchQueue.main.async { onURL(url) }
            }
        }
    }

    /// Terminate the child and wait for it. Called on quit and before a
    /// different manuscript is served.
    func stop() {
        pipe?.fileHandleForReading.readabilityHandler = nil
        pipe = nil
        guard let p = process else { return }
        process = nil
        guard p.isRunning else { return }
        p.terminate()
        let deadline = Date().addingTimeInterval(3)
        while p.isRunning && Date() < deadline { usleep(50_000) }
        if p.isRunning { kill(p.processIdentifier, SIGKILL) }
    }
}
