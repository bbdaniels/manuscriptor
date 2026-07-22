import Foundation

/// The manuscript-root rule.
///
/// Finder hands the app whatever file was double-clicked. Serving
/// `appendix/e_data_details.tex` on its own would render a fragment with no
/// preamble, no bibliography and no cross-references, so an opened file has to
/// resolve to the manuscript it belongs to and the page jumps to the file
/// afterwards.
///
/// This mirrors `shell/resolve_root.py` line for line. Two copies of a rule is
/// a split that rots quietly, so `tests/test_shell.py` runs this binary's
/// `--resolve-root` against the Python reference over one shared case table.
/// If either side drifts the cross-check fails.
enum ManuscriptRoot {

    static let mainName = "main.tex"

    /// `\documentclass` lives in the first line or two of a root file. Reading
    /// the head keeps a directory scan from paging in a 300KB appendix per
    /// candidate.
    static let headBytes = 65536

    struct Resolved {
        let root: URL
        let main: String
        let rel: String
    }

    enum Failure: Error, CustomStringConvertible {
        case missing(String)
        var description: String {
            switch self {
            case .missing(let p): return "no such file: \(p)"
            }
        }
    }

    // MARK: - the server's banner

    /// Read the port off `manuscriptor  http://127.0.0.1:PORT/`.
    ///
    /// Anchored on the loopback address rather than on any URL, so a line that
    /// merely mentions a host cannot send the window somewhere else.
    static func parsePort(_ line: String) -> Int? {
        guard let re = try? NSRegularExpression(pattern: "http://127\\.0\\.0\\.1:([0-9]+)/")
        else { return nil }
        let ns = line as NSString
        let all = NSRange(location: 0, length: ns.length)
        guard let m = re.firstMatch(in: line, options: [], range: all) else { return nil }
        return Int(ns.substring(with: m.range(at: 1)))
    }

    // MARK: - the rule

    /// Everything before the first unescaped `%`.
    static func stripComment(_ line: String) -> String {
        var out = ""
        let chars = Array(line)
        var i = 0
        while i < chars.count {
            let c = chars[i]
            if c == "\\" {
                out.append(c)
                if i + 1 < chars.count { out.append(chars[i + 1]) }
                i += 2
                continue
            }
            if c == "%" { break }
            out.append(c)
            i += 1
        }
        return out
    }

    /// True when the file really declares a document class.
    ///
    /// A commented-out `\documentclass` does not count: manuscripts routinely
    /// carry a dead journal-template header, and treating one as a root would
    /// pick the wrong file.
    static func hasDocumentClass(_ url: URL) -> Bool {
        guard let fh = try? FileHandle(forReadingFrom: url) else { return false }
        defer { try? fh.close() }
        let data = (try? fh.read(upToCount: headBytes)) ?? Data()
        let text = String(decoding: data, as: UTF8.self)
        for line in text.split(separator: "\n", omittingEmptySubsequences: false) {
            if stripComment(String(line)).contains("\\documentclass") { return true }
        }
        return false
    }

    static func texFiles(_ dir: URL) -> [URL] {
        let fm = FileManager.default
        guard let names = try? fm.contentsOfDirectory(atPath: dir.path) else { return [] }
        return names
            .filter { !$0.hasPrefix(".") && ($0 as NSString).pathExtension == "tex" }
            .sorted()
            .map { dir.appendingPathComponent($0) }
            .filter { url in
                var isDir: ObjCBool = false
                return fm.fileExists(atPath: url.path, isDirectory: &isDir) && !isDir.boolValue
            }
    }

    /// The name of this directory's root `.tex`, or "" if it is not a root.
    ///
    /// `main.tex` first, because that is the convention every manuscript in the
    /// corpus follows and the server's own `find_main_tex` agrees. Otherwise the
    /// directory qualifies only if EXACTLY ONE `.tex` declares a document class:
    /// two roots is not a root, and picking one would silently serve the wrong
    /// paper.
    static func rootHere(_ dir: URL) -> String {
        let named = dir.appendingPathComponent(mainName)
        var isDir: ObjCBool = false
        if FileManager.default.fileExists(atPath: named.path, isDirectory: &isDir),
           !isDir.boolValue {
            return mainName
        }
        let roots = texFiles(dir).filter(hasDocumentClass)
        return roots.count == 1 ? roots[0].lastPathComponent : ""
    }

    /// Stop climbing here, after checking this directory.
    ///
    /// A repository root is the edge of a project: a `main.tex` above it belongs
    /// to some other paper and must never be served in place of this one. Home
    /// and the filesystem root are the same idea, less often reached.
    static func isBoundary(_ dir: URL) -> Bool {
        if FileManager.default.fileExists(atPath: dir.appendingPathComponent(".git").path) {
            return true
        }
        let home = FileManager.default.homeDirectoryForCurrentUser
            .resolvingSymlinksInPath().path
        if dir.path == home { return true }
        return dir.deletingLastPathComponent().path == dir.path
    }

    /// Walk up from `start` to the manuscript root.
    static func findRoot(_ start: URL) -> (URL, String) {
        var d = start
        while true {
            let name = rootHere(d)
            if !name.isEmpty { return (d, name) }
            if isBoundary(d) { break }
            d = d.deletingLastPathComponent()
        }
        // Nothing above it is a manuscript. Serve where the file sits: a
        // fragment rendered alone is more useful than an error, and the
        // server's own diagnostics then say what is missing.
        return (start, "")
    }

    /// Resolve an opened path to the manuscript directory, its root `.tex`, and
    /// the file to jump to.
    ///
    /// `main` is "" when no root could be identified, in which case the server
    /// picks for itself. `rel` is relative to the manuscript directory with
    /// forward slashes, and "" when a directory was opened.
    static func resolve(_ path: String) throws -> Resolved {
        let raw = URL(fileURLWithPath: (path as NSString).expandingTildeInPath)
        var isDir: ObjCBool = false
        guard FileManager.default.fileExists(atPath: raw.path, isDirectory: &isDir) else {
            throw Failure.missing(raw.path)
        }
        // Finder hands over symlinked and aliased paths, and /var is a symlink
        // to /private/var on this platform.
        let p = raw.resolvingSymlinksInPath()
        let start = isDir.boolValue ? p : p.deletingLastPathComponent()
        let (root, main) = findRoot(start)
        if isDir.boolValue { return Resolved(root: root, main: main, rel: "") }
        let base = root.path == "/" ? "" : root.path
        let rel = p.path.hasPrefix(base + "/")
            ? String(p.path.dropFirst(base.count + 1))
            : p.lastPathComponent
        return Resolved(root: root, main: main, rel: rel)
    }
}
