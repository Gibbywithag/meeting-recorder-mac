import AVFoundation
import CoreAudio

struct AudioSource: Identifiable {
    let id: String
    let name: String
    static func validate(ids: [String], system: Bool, available: [AudioSource]) throws {
        guard ids.count <= 3, Set(ids).count == ids.count, !ids.contains("default") || ids.count == 1,
              system || !ids.isEmpty, ids.allSatisfy({ id in available.contains(where: { $0.id == id }) }) else {
            throw NSError(domain: "AudioSelection", code: 1, userInfo: [NSLocalizedDescriptionKey: "Select up to three available microphones, or the system default, and/or system audio. Refresh if an input disconnected."])
        }
    }
    static func isPhone(transport: UInt32, name: String, continuity: Bool) -> Bool {
        continuity || [kAudioDeviceTransportTypeContinuityCaptureWired, kAudioDeviceTransportTypeContinuityCaptureWireless, 0x63636170].contains(transport)
            || name.localizedCaseInsensitiveContains("iPhone") || name.localizedCaseInsensitiveContains("Continuity")
    }
    private static func devices() -> [AVCaptureDevice] {
        AVCaptureDevice.DiscoverySession(deviceTypes: [.microphone, .external], mediaType: .audio, position: .unspecified).devices.filter {
            !isPhone(transport: UInt32(bitPattern: $0.transportType), name: $0.localizedName, continuity: $0.isContinuityCamera)
        }
    }
    static func safeDefaultID() throws -> String {
        let allowed = devices()
        guard let device = allowed.first(where: { UInt32(bitPattern: $0.transportType) == kAudioDeviceTransportTypeBuiltIn }) ?? allowed.first else {
            throw NSError(domain: "AudioSelection", code: 2, userInfo: [NSLocalizedDescriptionKey: "No Mac microphone is available. iPhone microphones are excluded."])
        }
        return device.uniqueID
    }
    static func available() -> [AudioSource] {
        [AudioSource(id: "default", name: "Mac microphone (no iPhone)")] + devices().map { AudioSource(id: $0.uniqueID, name: $0.localizedName) }
    }
}
