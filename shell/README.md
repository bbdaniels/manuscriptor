# shell

The standalone `Manuscriptor.app`: a window, a `WKWebView`, a menu bar, and the server as a child process, so double-clicking works and quitting cleans up. Roughly 300 lines of Swift.

Separate bundle from ClaudeHUD, so ClaudeHUD relaunching cannot disturb it, and not an Arc tab. Set `isInspectable` to keep Web Inspector available.

**This is off the critical path and lands last (M7).** The server is the product and the shell is a client; any number of clients can attach to the same port. That is what lets the author work in this window while Claude verifies the same page in a browser through devtools. If the shell slips there is still a working editor.

ClaudeHUD's Xcode project, signing setup, and build scripts serve as the template. Electron is the fallback if Swift fights back, at the cost of 150MB and a slower launch; swapping later costs almost nothing because the shell is only ever a client.
