import SwiftUI

struct MenuBarView: View {
    @EnvironmentObject var state: AppState
    @State private var windowSearch = ""

    var body: some View {
        VStack(spacing: 14) {
            header
            if state.isRecording { liveSourcePicker } else { sourcePicker }
            previewPane
            audioControls
            meters
            mainButton
            statusLine
            if !state.jobs.isEmpty { jobsList }
            footer
        }
        .padding(16)
        .frame(width: 400)
        .task { await state.refreshSources() }
    }

    private var listedWindows: [CaptureSource] {
        let available = state.captureSources.filter { $0.id.hasPrefix("window:") }
        let missing = state.selectedWindowIDs.filter { id in !available.contains(where: { $0.id == id }) }
            .sorted().map { CaptureSource(id: $0, name: "Unavailable window — uncheck to remove") }
        return (available + missing).filter { windowSearch.isEmpty || $0.name.localizedCaseInsensitiveContains(windowSearch) }
    }

    private var sourcePicker: some View {
        VStack(alignment: .leading, spacing: 8) {
            Picker("Capture mode", selection: Binding(get: { state.captureMode }, set: state.setCaptureMode)) {
                Text("Screen").tag("screen")
                Text("One window").tag("window")
                Text("Multiple windows").tag("multiple")
            }.pickerStyle(.segmented)
                .disabled(state.isRecording || state.isBusy)
            HStack {
                if state.captureMode == "multiple" {
                    Text("Select up to 4 meeting windows").font(.subheadline.bold())
                    Spacer()
                } else {
                    Picker(state.captureMode == "screen" ? "Screen" : "Window", selection: $state.selectedSourceID) {
                        if state.captureMode == "screen" { Text("Main screen (automatic)").tag("main") }
                        if state.selectedSourceID != "main", !state.captureSources.contains(where: { $0.id == state.selectedSourceID }) {
                            Text(state.selectedSourceID.hasPrefix("display:") ? "Last used screen (main screen if unplugged)" : "Choose an available source").tag(state.selectedSourceID)
                        }
                        ForEach(state.captureSources.filter { $0.id.hasPrefix(state.captureMode == "screen" ? "display:" : "window:") }) { source in
                            Text(source.name).tag(source.id)
                        }
                    }
                }
                Button { Task { await state.refreshSources() } } label: { Image(systemName: "arrow.clockwise") }
                    .help("Refresh screens and windows")
            }.disabled(state.isRecording || state.isBusy || state.refreshingSources)
            if state.captureMode == "multiple" {
                TextField("Search windows or apps", text: $windowSearch).textFieldStyle(.roundedBorder)
                ScrollView {
                    VStack(alignment: .leading, spacing: 7) {
                        ForEach(listedWindows) { source in
                            Toggle(source.name.replacingOccurrences(of: "Window: ", with: ""), isOn: Binding(
                                get: { state.selectedWindowIDs.contains(source.id) },
                                set: { chosen in
                                    if chosen { state.selectedWindowIDs.insert(source.id) }
                                    else { state.selectedWindowIDs.remove(source.id) }
                                }))
                                .toggleStyle(.checkbox)
                                .disabled(state.isRecording || state.isBusy || (!state.selectedWindowIDs.contains(source.id) && state.selectedWindowIDs.count >= WindowLayout.maximumWindows))
                        }
                        if listedWindows.isEmpty { Text(state.refreshingSources ? "Loading windows…" : "No matching windows. Open them, then refresh.").font(.caption) }
                    }.frame(maxWidth: .infinity, alignment: .leading)
                }.frame(height: 110)
                Text("\(state.selectedWindowIDs.count) of 4 selected · Recorded simultaneously in a grid").font(.caption)
                if !state.unavailableWindowIDs.isEmpty {
                    Button("Remove \(state.unavailableWindowIDs.count) unavailable selection(s)", action: state.removeUnavailableWindows)
                        .disabled(state.isRecording || state.isBusy || state.refreshingSources)
                }
            }
            Text(state.sourceError ?? (state.refreshingSources ? "Loading screens and windows…" : state.selectionProblem ?? "Test your selected audio inputs before Start."))
                .font(.caption).foregroundStyle(state.sourceError == nil && state.selectionProblem == nil ? Color.secondary : Color.orange)
        }
    }

    /// While recording: switch to any screen or window; it continues in the same file.
    private var liveSourcePicker: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Picker("Recording now", selection: Binding(get: { state.liveSourceID }, set: { state.switchSource(to: $0) })) {
                    Text("Main screen").tag("main")
                    if !state.captureSources.contains(where: { $0.id == state.liveSourceID }) && state.liveSourceID != "main" {
                        Text(state.liveSourceID.hasPrefix("windows:") ? "Multiple windows" : "Current source").tag(state.liveSourceID)
                    }
                    ForEach(state.captureSources.filter { $0.id.hasPrefix("display:") }) { Text($0.name).tag($0.id) }
                    Divider()
                    ForEach(state.captureSources.filter { $0.id.hasPrefix("window:") }) { Text($0.name).tag($0.id) }
                }
                .disabled(state.switchingSource)
                if state.switchingSource { ProgressView().controlSize(.small) }
                Button { Task { await state.refreshSources() } } label: { Image(systemName: "arrow.clockwise") }
                    .help("Refresh screens and windows").disabled(state.refreshingSources)
            }
            Text("Switch any time — e.g. when someone shares their screen. It all stays in one recording.")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    /// AI work runs here in the background; it never blocks Start.
    private var jobsList: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("AI summaries").font(.caption.bold()).foregroundStyle(.secondary)
            ForEach(state.jobs) { job in
                HStack(alignment: .top, spacing: 8) {
                    Group {
                        switch job.state {
                        case .running: ProgressView().controlSize(.small)
                        case .done: Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
                        case .failed: Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                        }
                    }.frame(width: 16)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(job.name).font(.caption.monospaced()).lineLimit(1)
                        Text(job.status).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                    }
                    Spacer(minLength: 4)
                    if job.state == .done { Button("Summary") { state.openSummary(of: job) }.font(.caption) }
                    if job.state == .failed { Button("Retry") { state.retry(job.folder) }.font(.caption) }
                    if job.state != .running {
                        Button { state.dismiss(job) } label: { Image(systemName: "xmark") }.buttonStyle(.borderless).font(.caption)
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var header: some View {
        HStack {
            Circle().fill(state.isRecording ? Color.red : Color.secondary.opacity(0.4)).frame(width: 10, height: 10)
            Text("Meeting Recorder").font(.headline)
            Spacer()
            Button { state.panelPinned.toggle() } label: {
                Image(systemName: state.panelPinned ? "pin.fill" : "pin")
            }.buttonStyle(.borderless).help(state.panelPinned ? "Unpin panel" : "Keep panel open")
            Text(state.elapsedText).font(.system(.title3, design: .monospaced)).foregroundStyle(state.isRecording ? .primary : .secondary)
        }
    }

    private var previewPane: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 10).fill(Color.black.opacity(0.85))
            if let img = state.preview {
                Image(decorative: img, scale: 1).resizable().aspectRatio(contentMode: .fit)
                    .clipShape(RoundedRectangle(cornerRadius: 10))
            } else {
                VStack(spacing: 6) {
                    Image(systemName: "rectangle.dashed.badge.record").font(.system(size: 28)).foregroundStyle(.secondary)
                    Text(state.isRecording ? "Waiting for first frame…" : state.previewStatus)
                        .font(.caption).foregroundStyle(.secondary).multilineTextAlignment(.center)
                }
            }
        }
        .frame(height: 190)
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.primary.opacity(0.12)))
        .overlay(alignment: .topLeading) {
            if state.preview != nil && !state.isRecording {
                Text("PREVIEW · NOT RECORDING").font(.caption2.bold())
                    .padding(6).background(.ultraThinMaterial, in: Capsule()).padding(8)
            }
            if state.isRecording {
                HStack(spacing: 4) { Circle().fill(.red).frame(width: 6, height: 6); Text("LIVE").font(.caption2.bold()) }
                    .padding(.horizontal, 7).padding(.vertical, 3)
                    .background(.ultraThinMaterial, in: Capsule()).padding(8)
            }
        }
    }

    private var audioControls: some View {
        VStack(alignment: .leading, spacing: 6) {
            DisclosureGroup("Audio sources (system + up to 3 microphones)") {
                Toggle("System audio — all apps, including FaceTime", isOn: $state.includeSystemAudio)
                Picker("System capture", selection: $state.useCoreAudio) {
                    Text("Core Audio (call audio)").tag(true)
                    Text("ScreenCaptureKit (fallback)").tag(false)
                }
                ForEach(state.audioSources) { source in
                    Toggle(source.name, isOn: Binding(get: { state.selectedMicIDs.contains(source.id) }, set: { state.selectMicrophone(source.id, selected: $0) }))
                        .disabled(!state.selectedMicIDs.contains(source.id) && source.id != "default" && state.selectedMicIDs.count >= 3)
                }
                Button("Refresh audio devices") { state.audioSources = AudioSource.available() }
            }.disabled(state.isRecording || state.isBusy || state.isAudioTesting)
            Button(state.isAudioTesting ? "Stop audio test" : "Test audio before recording", action: state.toggleAudioTest)
                .disabled(state.isRecording || state.isBusy || (state.selectedMicIDs.isEmpty && !state.includeSystemAudio))
            if state.isAudioTesting || state.audioTestStatus.contains("failed") {
                Text(state.audioTestStatus).font(.caption).foregroundStyle(.secondary)
                Text(state.audioTestDetected.isEmpty ? "Waiting for audio signal…" : "Signal received: " + state.audioTestDetected.sorted().joined(separator: ", "))
                    .font(.caption).foregroundStyle(state.audioTestDetected.isEmpty ? Color.orange : Color.green)
            }
        }
    }

    private var meters: some View {
        VStack(spacing: 8) {
            ForEach(state.audioSources.filter { state.selectedMicIDs.contains($0.id) }) { source in
                MeterRow(title: source.name, icon: "mic.fill", level: state.inputLevels[source.id] ?? 0, silent: state.micSilent)
            }
            if state.includeSystemAudio { MeterRow(title: "System audio", icon: "speaker.wave.2.fill", level: state.sysLevel, silent: state.sysSilent) }
        }
    }

    private var mainButton: some View {
        VStack(spacing: 8) {
        Button(action: state.toggle) {
            HStack {
                Image(systemName: state.isRecording ? "stop.fill" : "record.circle")
                Text(state.isRecording ? "Stop recording" : "Start recording").fontWeight(.semibold)
            }
            .frame(maxWidth: .infinity).padding(.vertical, 8)
        }
        .buttonStyle(.borderedProminent)
        .tint(state.isRecording ? .red : .accentColor)
        .controlSize(.large)
        .disabled(state.isBusy || (!state.isRecording && (state.selectionProblem != nil || state.refreshingSources)))
        Button(action: state.addVideo) {
            Label("Analyze a client video…", systemImage: "film.stack")
                .frame(maxWidth: .infinity)
        }
        .buttonStyle(.bordered)
        .help("Pick a video the client sent. The AI lists every request with a screenshot, using your last meeting as background.")
        .disabled(state.isRecording || state.isBusy)
        }
    }

    private var statusLine: some View {
        HStack(alignment: .top, spacing: 8) {
            if state.phase == .processing { ProgressView().controlSize(.small) }
            Text(statusText)
                .font(.callout)
                .foregroundStyle(state.phase == .failed ? Color.red : Color.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var statusText: String {
        state.status == "Ready" ? "Tell the client the call is being recorded, then press Start." : state.status
    }

    private var footer: some View {
        HStack {
            Button("Open folder", action: state.openFolder).disabled(state.folder == nil)
            Button("Summary", action: state.openSummary).disabled(!state.summaryAvailable || state.isBusy)
            Button("Retry AI", action: state.retryAnalysis).disabled(!state.canRetryAnalysis)
            Spacer()
            Toggle("Open at login", isOn: Binding(get: { state.opensAtLogin }, set: { state.setOpensAtLogin($0) }))
                .toggleStyle(.checkbox)
            Button("Quit") { NSApplication.shared.terminate(nil) }.disabled(state.isRecording || state.isBusy || state.jobsRunning)
                .help(state.jobsRunning ? "Wait for the AI summaries to finish" : "Quit Meeting Recorder")
        }
        .buttonStyle(.borderless).font(.callout)
    }
}

struct MeterRow: View {
    let title: String; let icon: String; let level: Float; let silent: Bool
    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: icon).frame(width: 16).foregroundStyle(silent ? .red : .secondary)
            Text(silent ? "\(title) — no signal" : title).font(.callout).foregroundStyle(silent ? .red : .primary)
                .frame(width: 150, alignment: .leading)
            GeometryReader { g in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.primary.opacity(0.1))
                    Capsule().fill(LinearGradient(colors: [.green, .yellow, .red], startPoint: .leading, endPoint: .trailing))
                        .frame(width: g.size.width * CGFloat(min(1, level)))
                        .animation(.linear(duration: 0.1), value: level)
                }
            }
            .frame(height: 8)
        }
    }
}
