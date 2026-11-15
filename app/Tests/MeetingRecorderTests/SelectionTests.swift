import XCTest
@testable import MeetingRecorder

final class SelectionTests: XCTestCase {
    /// Every launch starts on a whole screen (call apps recreate their windows mid-call, which ended
    /// recordings early), but the windows picked last time are remembered for when multi-window mode is chosen.
    @MainActor
    func testLaunchStartsOnScreenButRemembersPickedWindows() async {
        let prefs = UserDefaults.standard
        let oldSource = prefs.object(forKey: "captureSourceID")
        let oldWindows = prefs.object(forKey: "captureWindowIDs")
        defer {
            prefs.set(oldSource, forKey: "captureSourceID")
            prefs.set(oldWindows, forKey: "captureWindowIDs")
        }
        prefs.set("windows:12,34", forKey: "captureSourceID")
        let state = AppState(previewEnabled: false)
        XCTAssertEqual(state.selectedSourceID, "main")
        XCTAssertEqual(state.captureMode, "screen")
        XCTAssertEqual(state.selectedWindowIDs, ["window:12", "window:34"])
        XCTAssertNil(state.selectionProblem)

        state.setCaptureMode("multiple")
        XCTAssertEqual(state.captureMode, "multiple")
        state.captureSources = [CaptureSource(id: "window:12", name: "A"), CaptureSource(id: "window:34", name: "B")]
        XCTAssertNil(state.selectionProblem)
        state.captureSources.removeLast()
        XCTAssertNotNil(state.selectionProblem)
        state.phase = .failed
        state.elapsed = 48
        state.removeUnavailableWindows()
        XCTAssertEqual(state.phase, .idle)
        XCTAssertEqual(state.elapsed, 0)
        XCTAssertEqual(state.selectedWindowIDs, ["window:12"])
        XCTAssertNil(state.selectionProblem)

        let restored = AppState(previewEnabled: false)
        XCTAssertEqual(restored.captureMode, "screen")
        XCTAssertEqual(restored.selectedWindowIDs, ["window:12"])
    }

    @MainActor
    func testUnpluggedScreenDoesNotBlockStart() async {
        let prefs = UserDefaults.standard
        let oldSource = prefs.object(forKey: "captureSourceID")
        defer { prefs.set(oldSource, forKey: "captureSourceID") }
        prefs.set("display:999", forKey: "captureSourceID")
        let state = AppState(previewEnabled: false)
        XCTAssertEqual(state.selectedSourceID, "display:999")
        state.captureSources = [CaptureSource(id: "display:1", name: "Built-in")]
        XCTAssertNil(state.selectionProblem)   // capture falls back to the main screen
    }

    func testWindowSelectionRejectsDuplicatesAndTooManyWindows() throws {
        XCTAssertEqual(try WindowLayout.ids(from: "windows:12,34"), [12, 34])
        for invalid in ["windows:", "windows:12,12", "windows:1,2,3,4,5", "windows:abc", "window:12"] {
            XCTAssertThrowsError(try WindowLayout.ids(from: invalid))
        }
    }

    func testAudioSelectionPreventsDuplicateAndMissingInputs() throws {
        let available = [AudioSource(id: "default", name: "Default"), AudioSource(id: "usb", name: "USB"), AudioSource(id: "built-in", name: "Built-in")]
        XCTAssertNoThrow(try AudioSource.validate(ids: ["usb", "built-in"], system: true, available: available))
        XCTAssertNoThrow(try AudioSource.validate(ids: [], system: true, available: available))
        for ids in [["default", "usb"], ["usb", "usb"], ["missing"]] {
            XCTAssertThrowsError(try AudioSource.validate(ids: ids, system: true, available: available))
        }
        XCTAssertThrowsError(try AudioSource.validate(ids: [], system: false, available: available))
    }

    func testPhoneMicrophonesAreExcludedEvenWithCustomNames() {
        XCTAssertTrue(AudioSource.isPhone(transport: 0x6363776c, name: "Sam Microphone", continuity: false))
        XCTAssertTrue(AudioSource.isPhone(transport: 0x63637764, name: "My microphone", continuity: false))
        XCTAssertTrue(AudioSource.isPhone(transport: 0, name: "iPhone Microphone", continuity: false))
        XCTAssertFalse(AudioSource.isPhone(transport: 0x626c746e, name: "MacBook Pro Microphone", continuity: false))
    }

    /// AI work must never block recording: a running job doesn't make the app "busy", but blocks Quit
    /// and a second retry of the same folder.
    @MainActor
    func testRunningAIJobDoesNotBlockRecording() {
        let state = AppState(previewEnabled: false)
        let dir = URL(fileURLWithPath: "/tmp/mr-test-meeting")
        state.jobs = [AIJob(folder: dir, status: "Uploading to Gemini…")]
        XCTAssertTrue(state.jobsRunning)
        XCTAssertFalse(state.isBusy)
        state.folder = dir
        XCTAssertFalse(state.canRetryAnalysis)   // already being analyzed
        XCTAssertTrue(state.keepingAwake)        // a sleeping Mac would cut the upload
        state.jobs[0].state = .done
        XCTAssertFalse(state.jobsRunning)
        XCTAssertFalse(state.keepingAwake)
        state.phase = .recording
        XCTAssertTrue(state.keepingAwake)
    }

    @MainActor
    func testSwitchingSourceIsIgnoredWhenNotRecording() {
        let state = AppState(previewEnabled: false)
        state.switchSource(to: "display:1")
        XCTAssertFalse(state.switchingSource)
        XCTAssertEqual(state.liveSourceID, "main")
    }

    /// A hung mic start must not block recording: the timeout fires, a fast start passes, an error surfaces.
    func testMicStartTimeoutNeverBlocks() async {
        let t = Date()
        if case .timedOut = await CaptureEngine.startWithTimeout(seconds: 0.3, { try await Task.sleep(nanoseconds: 60_000_000_000) }) {} else { XCTFail("expected timeout") }
        XCTAssertLessThan(Date().timeIntervalSince(t), 2)
        if case .ok = await CaptureEngine.startWithTimeout(seconds: 2, {}) {} else { XCTFail("expected ok") }
        struct Boom: Error {}
        if case .failed = await CaptureEngine.startWithTimeout(seconds: 2, { throw Boom() }) {} else { XCTFail("expected failure") }
    }

    /// The first-run key prompt replaces an empty key and keeps every other setting.
    func testSavingAKeyKeepsOtherSettings() {
        let before = "# comment\nGEMINI_API_KEY=\nLOCAL_SPEAKER_NAMES=Sam\n"
        XCTAssertEqual(AppState.settings(before, withKey: "abc"), "GEMINI_API_KEY=abc\n# comment\nLOCAL_SPEAKER_NAMES=Sam\n")
        XCTAssertEqual(AppState.settings("", withKey: "abc"), "GEMINI_API_KEY=abc\n")
        XCTAssertEqual(AppState.settings("GEMINI_API_KEY=old\n  GEMINI_API_KEY=older", withKey: "new"), "GEMINI_API_KEY=new\n")
    }
}
