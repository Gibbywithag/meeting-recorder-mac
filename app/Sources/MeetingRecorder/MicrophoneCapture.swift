import Foundation
import AVFoundation

final class MicrophoneCapture: NSObject, AVCaptureAudioDataOutputSampleBufferDelegate {
    var onSample: ((CMSampleBuffer) -> Void)?
    var log: ((String) -> Void) = { _ in }
    private let session = AVCaptureSession()
    private let queue = DispatchQueue(label: "meeting.microphone")
    private var requestedID = "default"
    private var input: AVCaptureDeviceInput?
    private var observers: [NSObjectProtocol] = []
    private var stopped = false

    func start(deviceID: String) async throws {
        requestedID = deviceID; stopped = false
        let device = try Self.device(for: deviceID)
        let input = try AVCaptureDeviceInput(device: device)
        let output = AVCaptureAudioDataOutput()
        output.audioSettings = [AVFormatIDKey: kAudioFormatLinearPCM, AVLinearPCMIsFloatKey: true, AVLinearPCMBitDepthKey: 32, AVLinearPCMIsNonInterleaved: false]
        output.setSampleBufferDelegate(self, queue: queue)
        guard session.canAddInput(input), session.canAddOutput(output) else {
            throw NSError(domain: "MicrophoneCapture", code: 2, userInfo: [NSLocalizedDescriptionKey: "Could not configure microphone capture."])
        }
        session.addInput(input); session.addOutput(output); self.input = input
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            DispatchQueue.global().async(execute: DispatchWorkItem { self.session.startRunning(); continuation.resume() })
        }
        guard session.isRunning else {
            throw NSError(domain: "MicrophoneCapture", code: 3, userInfo: [NSLocalizedDescriptionKey: "Microphone did not start. Check Microphone permission for Meeting Recorder."])
        }
        watchForDisconnects()
    }

    func stop() {
        stopped = true
        observers.forEach(NotificationCenter.default.removeObserver); observers = []
        session.stopRunning()
    }

    /// Headphones/AirPods unplugged mid-meeting: move to the Mac's current default mic instead of
    /// recording silence. Same session and output, so the track continues in the same file.
    private func watchForDisconnects() {
        let nc = NotificationCenter.default
        observers.append(nc.addObserver(forName: AVCaptureDevice.wasDisconnectedNotification, object: nil, queue: nil) { [weak self] note in
            guard let self, let gone = note.object as? AVCaptureDevice, gone.uniqueID == self.input?.device.uniqueID else { return }
            self.reconnect(reason: "\(gone.localizedName) disconnected")
        })
        observers.append(nc.addObserver(forName: AVCaptureSession.runtimeErrorNotification, object: session, queue: nil) { [weak self] note in
            let err = (note.userInfo?[AVCaptureSessionErrorKey] as? NSError)?.localizedDescription ?? "unknown"
            self?.reconnect(reason: "microphone error: \(err)")
        })
    }

    private func reconnect(reason: String, attempt: Int = 1) {
        queue.async { [weak self] in
            guard let self, !self.stopped else { return }
            do {
                // A specifically chosen mic that's gone falls back to the default one.
                let device = try (try? Self.device(for: self.requestedID)) ?? Self.device(for: "default")
                let newInput = try AVCaptureDeviceInput(device: device)
                self.session.beginConfiguration()
                if let old = self.input { self.session.removeInput(old) }
                if self.session.canAddInput(newInput) { self.session.addInput(newInput); self.input = newInput }
                self.session.commitConfiguration()
                if !self.session.isRunning { self.session.startRunning() }
                self.log("Microphone reconnected to \(device.localizedName) (\(reason)). Recording continues.")
            } catch {
                self.log("microphone reconnect attempt \(attempt) failed (\(reason)): \(error.localizedDescription)")
                if attempt < 10 {
                    self.queue.asyncAfter(deadline: .now() + 1) { self.reconnect(reason: reason, attempt: attempt + 1) }
                }
            }
        }
    }

    private static func device(for deviceID: String) throws -> AVCaptureDevice {
        let id = deviceID == "default" ? try AudioSource.safeDefaultID() : deviceID
        guard let device = AVCaptureDevice.DiscoverySession(deviceTypes: [.microphone, .external], mediaType: .audio, position: .unspecified).devices.first(where: { $0.uniqueID == id }) else {
            throw NSError(domain: "MicrophoneCapture", code: 1, userInfo: [NSLocalizedDescriptionKey: "Selected Mac microphone is unavailable. Refresh audio devices."])
        }
        return device
    }

    func captureOutput(_ output: AVCaptureOutput, didOutput sampleBuffer: CMSampleBuffer, from connection: AVCaptureConnection) {
        onSample?(sampleBuffer)
    }
}
