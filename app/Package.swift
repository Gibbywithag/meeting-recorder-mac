// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "MeetingRecorder",
    platforms: [.macOS(.v15)],
    targets: [
        .executableTarget(
            name: "MeetingRecorder",
            path: "Sources/MeetingRecorder",
            swiftSettings: [.swiftLanguageMode(.v5)]
        ),
        .testTarget(name: "MeetingRecorderTests", dependencies: ["MeetingRecorder"], swiftSettings: [.swiftLanguageMode(.v5)])
    ]
)
