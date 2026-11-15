import Foundation
import CoreAudio
import CoreMedia

/// Global process mix: independent of windows, displays, and FaceTime's helper processes.
final class SystemAudioTap {
    var onSample: ((CMSampleBuffer) -> Void)?
    private var tap: AudioObjectID = 0
    private var device: AudioObjectID = 0
    private var ioProc: AudioDeviceIOProcID?
    private let queue = DispatchQueue(label: "meeting.system.tap")

    func start() throws {
        let description = CATapDescription(stereoGlobalTapButExcludeProcesses: [])
        description.name = "Meeting Recorder system audio"
        description.isPrivate = true
        description.isExclusive = true
        description.muteBehavior = .unmuted
        try check(AudioHardwareCreateProcessTap(description, &tap), "create system audio tap")
        do {
            var asbd = AudioStreamBasicDescription()
            var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
            var address = AudioObjectPropertyAddress(mSelector: kAudioTapPropertyFormat, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
            try check(AudioObjectGetPropertyData(tap, &address, 0, nil, &size, &asbd), "read tap format")
            let configuration: [String: Any] = [
                kAudioAggregateDeviceNameKey: "Meeting Recorder private audio tap",
                kAudioAggregateDeviceUIDKey: UUID().uuidString,
                kAudioAggregateDeviceIsPrivateKey: true,
                kAudioAggregateDeviceTapAutoStartKey: true,
                kAudioAggregateDeviceTapListKey: [[kAudioSubTapUIDKey: description.uuid.uuidString, kAudioSubTapDriftCompensationKey: true]],
            ]
            try check(AudioHardwareCreateAggregateDevice(configuration as CFDictionary, &device), "create tap device")
            var format: CMAudioFormatDescription?
            try check(CMAudioFormatDescriptionCreate(allocator: kCFAllocatorDefault, asbd: &asbd, layoutSize: 0, layout: nil, magicCookieSize: 0, magicCookie: nil, extensions: nil, formatDescriptionOut: &format), "describe tap audio")
            guard let format, asbd.mBytesPerFrame > 0, asbd.mSampleRate > 0 else { throw error("Invalid system audio format", -1) }
            NSLog("System tap format: rate=%f channels=%u bytes/frame=%u flags=%u", asbd.mSampleRate, asbd.mChannelsPerFrame, asbd.mBytesPerFrame, asbd.mFormatFlags)
            let bytesPerFrame = asbd.mBytesPerFrame
            let rate = asbd.mSampleRate
            try check(AudioDeviceCreateIOProcIDWithBlock(&ioProc, device, queue) { [weak self] _, input, inputTime, _, _ in
                let frames = Int(input.pointee.mBuffers.mDataByteSize / bytesPerFrame)
                guard frames > 0 else { return }
                let pts = CMClockMakeHostTimeFromSystemUnits(inputTime.pointee.mHostTime)
                var timing = CMSampleTimingInfo(duration: CMTime(value: 1, timescale: CMTimeScale(rate)), presentationTimeStamp: pts, decodeTimeStamp: .invalid)
                var sample: CMSampleBuffer?
                guard CMSampleBufferCreate(allocator: kCFAllocatorDefault, dataBuffer: nil, dataReady: false, makeDataReadyCallback: nil, refcon: nil, formatDescription: format, sampleCount: frames, sampleTimingEntryCount: 1, sampleTimingArray: &timing, sampleSizeEntryCount: 0, sampleSizeArray: nil, sampleBufferOut: &sample) == noErr, let sample else { return }
                // Copy callback-owned audio into a retained CoreMedia data buffer.
                guard CMSampleBufferSetDataBufferFromAudioBufferList(sample, blockBufferAllocator: kCFAllocatorDefault, blockBufferMemoryAllocator: kCFAllocatorDefault, flags: 0, bufferList: input) == noErr else { return }
                CMSampleBufferSetDataReady(sample)
                self?.onSample?(sample)
            }, "register system audio callback")
            try check(AudioDeviceStart(device, ioProc), "start system audio tap")
        } catch { stop(); throw error }
    }

    func stop() {
        if device != 0 {
            if let ioProc { AudioDeviceStop(device, ioProc); AudioDeviceDestroyIOProcID(device, ioProc) }
            AudioHardwareDestroyAggregateDevice(device)
        }
        if tap != 0 { AudioHardwareDestroyProcessTap(tap) }
        device = 0; tap = 0; ioProc = nil
    }
    deinit { stop() }
    private func check(_ status: OSStatus, _ operation: String) throws {
        if status != noErr { throw error("Could not \(operation)", status) }
    }
    private func error(_ text: String, _ code: OSStatus) -> NSError {
        NSError(domain: "SystemAudioTap", code: Int(code), userInfo: [NSLocalizedDescriptionKey: "\(text) (\(code)). Check System Settings → Privacy & Security → System Audio Recording Only."])
    }
}
