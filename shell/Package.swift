// swift-tools-version:5.9
import PackageDescription

// A plain executable. `build.sh` wraps the product in Manuscriptor.app with the
// Info.plist that declares .tex, because that declaration is the whole point of
// there being an app rather than a terminal command.
let package = Package(
    name: "Manuscriptor",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(name: "Manuscriptor", path: "Sources/Manuscriptor")
    ]
)
