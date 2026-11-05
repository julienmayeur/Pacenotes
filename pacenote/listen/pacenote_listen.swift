// pacenote-listen: ONE process that captures the microphone once and transcribes the same audio in
// every expected language at the same time (one SpeechAnalyzer + SpeechTranscriber per locale).
//
// Why not `yap dictate` per language: every yap run releases ALL reserved speech locales and
// re-reserves its own, which makes macOS start removing the very model it is about to load
// ("Failed to fetch the GeneralASR asset", CancellationError), whatever the spacing between runs.
// This helper reserves the expected locales once, never releases them, installs what is
// missing, and loads every model before it says "ready".
//
// Usage:
//   pacenote-listen prepare --locale fr-FR --locale en-US     reserve + install + load, then exit
//   pacenote-listen listen  --locale fr-FR --locale en-US     same, then transcribe the microphone
//   pacenote-listen listen  --locale fr-FR --file talk.aiff   same, from an audio file (tests)
//   options: --max-length N (characters per segment, default 60)
//
// Output: one JSON object per line on stdout ("event" says what it is):
//   locale   {locale, resolved, status}            asset state found at start (installed|supported|downloading)
//   install  {locale, seconds}                     a missing model was downloaded and installed
//   released {locale}                              a reservation of a language NOT expected, to make room
//   loaded   {locale, seconds}                     the model is loaded (prepareToAnalyze)
//   locale_failed {locale, code, message}          this language could not be made ready: it is
//                                                  skipped, the others go on (no language ready -> error)
//   prepared {locales, failed}                     `prepare` only: models ready, then exit 0
//   ready    {locales, failed, source, sample_rate} listening; segment times count from here
//   segment  {locale, start, end, text}            a final transcription segment (seconds)
//   heartbeat {buffers, rms}                       about every second once ready: audio buffers
//                                                  received so far and the loudest buffer's level
//                                                  since the last heartbeat (RMS, 0..1);
//                                                  buffers not growing = no audio
//   error    {code, message, locale?}              then exit 3 (setup) or 4 (while listening)
//   stopped  {}                                    SIGINT/SIGTERM or end of file: flushed, exit 0
// Nothing else is written to stdout. Diagnostics go to stderr.

import Foundation
@preconcurrency import AVFoundation
import Speech

// MARK: - output

final class Out: @unchecked Sendable {
    static let shared = Out()
    private let lock = NSLock()

    func emit(_ event: String, _ fields: [String: Any] = [:]) {
        var obj = fields
        obj["event"] = event
        guard let data = try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys]) else { return }
        lock.lock()
        defer { lock.unlock() }
        FileHandle.standardOutput.write(data)
        FileHandle.standardOutput.write(Data([0x0A]))
    }
}

struct Failure: Error {
    let code: String
    let message: String
    var locale: String? = nil
}

func fail(_ f: Failure, exitCode: Int32) -> Never {
    var fields: [String: Any] = ["code": f.code, "message": f.message]
    if let l = f.locale { fields["locale"] = l }
    Out.shared.emit("error", fields)
    exit(exitCode)
}

func describe(_ error: Error) -> String {
    let ns = error as NSError
    var text = ns.localizedDescription
    if let reason = ns.localizedFailureReason, !text.contains(reason) { text += " (\(reason))" }
    if ns.domain != NSCocoaErrorDomain || ns.code != 0 { text += " [\(ns.domain) \(ns.code)]" }
    return text
}

func bcp47(_ l: Locale) -> String { l.identifier(.bcp47) }

// MARK: - arguments

struct Options {
    var command = ""
    var locales: [String] = []
    var file: String? = nil
    var maxLength = 60
}

func parseArgs() -> Options {
    var o = Options()
    var args = Array(CommandLine.arguments.dropFirst())
    guard let cmd = args.first, ["prepare", "listen"].contains(cmd) else {
        fail(Failure(code: "usage", message: "usage: pacenote-listen prepare|listen --locale fr-FR [--locale en-US] [--file X] [--max-length N]"), exitCode: 2)
    }
    o.command = cmd
    args.removeFirst()
    while !args.isEmpty {
        let a = args.removeFirst()
        func value() -> String {
            guard !args.isEmpty else { fail(Failure(code: "usage", message: "\(a) needs a value"), exitCode: 2) }
            return args.removeFirst()
        }
        switch a {
        case "--locale": o.locales.append(value())
        case "--file": o.file = value()
        case "--max-length": o.maxLength = Int(value()) ?? 60
        default: fail(Failure(code: "usage", message: "unknown argument \(a)"), exitCode: 2)
        }
    }
    if o.locales.isEmpty { fail(Failure(code: "usage", message: "give at least one --locale"), exitCode: 2) }
    return o
}

// MARK: - speech assets: reserve once, never release an expected locale, install what is missing

// A language that cannot be made ready is skipped (`locale_failed`) so the interview can start with
// the others; only when NO language is ready does the helper stop with an error.
nonisolated(unsafe) var skipped: [Failure] = []

func skip(_ f: Failure) {
    skipped.append(f)
    var fields: [String: Any] = ["code": f.code, "message": f.message]
    if let l = f.locale { fields["locale"] = l }
    Out.shared.emit("locale_failed", fields)
}

/// No language left: the error of the first one that failed (its code says the fix), naming all.
func noLanguageReady() -> Failure {
    guard let first = skipped.first else { return Failure(code: "usage", message: "no language to listen to") }
    if skipped.count == 1 { return first }
    return Failure(code: first.code, message: "No spoken language is ready. " + skipped.map(\.message).joined(separator: " "),
                   locale: first.locale)
}

struct Lane {
    let id: String          // the locale as pacenote asked for it, e.g. "fr-FR" (used in every event)
    let locale: Locale      // the equivalent locale SpeechTranscriber supports
    let transcriber: SpeechTranscriber
}

func prepareAssets(_ requested: [String]) async throws -> [Lane] {
    guard SpeechTranscriber.isAvailable else {
        throw Failure(code: "speech_unavailable",
                      message: "On-device speech transcription (SpeechTranscriber) is not available on this Mac. macOS 26 or later on Apple silicon is required.")
    }
    var resolved: [(String, Locale)] = []
    for id in requested {
        guard let loc = await SpeechTranscriber.supportedLocale(equivalentTo: Locale(identifier: id)) else {
            skip(Failure(code: "unsupported_locale",
                         message: "\(id) is not a speech language this Mac can transcribe on device.", locale: id))
            continue
        }
        resolved.append((id, loc))
    }
    let wanted = Set(resolved.map { bcp47($0.1) })
    let limit = AssetInventory.maximumReservedLocales
    if wanted.count > limit {
        throw Failure(code: "too_many_locales",
                      message: "\(wanted.count) languages were asked for but macOS lets one app keep at most \(limit) speech languages.")
    }

    // Reserve every expected locale. Never release one of them (that is what made macOS remove
    // the model under yap). Only if the per-app limit is reached, release a locale nobody expects.
    for (id, loc) in resolved {
        var reserved = await AssetInventory.reservedLocales
        if reserved.contains(where: { bcp47($0) == bcp47(loc) }) { continue }
        while reserved.count >= limit, let extra = reserved.first(where: { !wanted.contains(bcp47($0)) }) {
            await AssetInventory.release(reservedLocale: extra)
            Out.shared.emit("released", ["locale": bcp47(extra)])
            reserved = await AssetInventory.reservedLocales
        }
        do {
            _ = try await AssetInventory.reserve(locale: loc)
        } catch {
            skip(Failure(code: "reserve_failed",
                         message: "macOS refused to reserve the \(id) speech model: \(describe(error)). Reserved now: \(reserved.map(bcp47)) (limit \(limit)).",
                         locale: id))
            continue
        }
    }

    var lanes: [Lane] = []
    for (id, loc) in resolved where !skipped.contains(where: { $0.locale == id }) {
        let t = SpeechTranscriber(locale: loc, transcriptionOptions: [], reportingOptions: [],
                                  attributeOptions: [.audioTimeRange])
        let status = await AssetInventory.status(forModules: [t])
        Out.shared.emit("locale", ["locale": id, "resolved": bcp47(loc), "status": "\(status)"])
        if status != .installed {
            let started = Date()
            do {
                if let request = try await AssetInventory.assetInstallationRequest(supporting: [t]) {
                    // Progress is reported from a separate, non-throwing task: never cancel a
                    // sleeping poller inside the install's task group (yap 1.2.1's CancellationError).
                    let progress = request.progress
                    let reporter = Task.detached {
                        while !progress.isFinished && !Task.isCancelled {
                            FileHandle.standardError.write(Data("pacenote-listen: \(id) model \(Int(progress.fractionCompleted * 100))%\n".utf8))
                            try? await Task.sleep(nanoseconds: 2_000_000_000)
                        }
                    }
                    defer { reporter.cancel() }
                    try await request.downloadAndInstall()
                }
            } catch {
                skip(Failure(code: "install_failed",
                             message: "The \(id) speech model could not be downloaded or installed: \(describe(error)).",
                             locale: id))
                continue
            }
            let after = await AssetInventory.status(forModules: [t])
            if after != .installed {
                skip(Failure(code: "not_installed",
                             message: "The \(id) speech model is still not installed after the install finished (macOS reports \"\(after)\").",
                             locale: id))
                continue
            }
            Out.shared.emit("install", ["locale": id, "seconds": Date().timeIntervalSince(started)])
        }
        lanes.append(Lane(id: id, locale: loc, transcriber: t))
    }
    return lanes
}

// MARK: - transcription

final class Pipeline: @unchecked Sendable {
    var lanes: [Lane]
    let maxLength: Int
    var analyzers: [SpeechAnalyzer] = []
    var continuations: [AsyncStream<AnalyzerInput>.Continuation] = []
    var readers: [Task<Void, Never>] = []
    var format: AVAudioFormat!

    init(lanes: [Lane], maxLength: Int) {
        self.lanes = lanes
        self.maxLength = maxLength
    }

    /// One analyzer per language, all loaded before audio flows. A language whose model does not
    /// load is skipped (`locale_failed`); `lanes` keeps the ones that are listening.
    func load() async throws {
        let modules: [any SpeechModule] = lanes.map(\.transcriber)
        guard let fmt = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: modules) else {
            throw Failure(code: "audio_format", message: "macOS found no audio format usable by the speech models.")
        }
        format = fmt
        var live: [Lane] = []
        for lane in lanes {
            let started = Date()
            let analyzer = SpeechAnalyzer(modules: [lane.transcriber])
            do {
                try await analyzer.prepareToAnalyze(in: fmt)
            } catch {
                skip(Failure(code: "model_load_failed",
                             message: "The \(lane.id) speech model is installed but could not be loaded: \(describe(error)).",
                             locale: lane.id))
                continue
            }
            let (stream, cont) = AsyncStream.makeStream(of: AnalyzerInput.self)
            do {
                try await analyzer.start(inputSequence: stream)
            } catch {
                cont.finish()
                skip(Failure(code: "model_load_failed",
                             message: "The \(lane.id) speech analyzer did not start: \(describe(error)).", locale: lane.id))
                continue
            }
            analyzers.append(analyzer)
            continuations.append(cont)
            readers.append(Task { await self.read(lane) })
            live.append(lane)
            Out.shared.emit("loaded", ["locale": lane.id, "seconds": Date().timeIntervalSince(started)])
        }
        lanes = live
        if lanes.isEmpty { throw noLanguageReady() }
    }

    /// The same buffer goes to every language.
    func feed(_ buffer: AVAudioPCMBuffer) {
        for c in continuations { c.yield(AnalyzerInput(buffer: buffer)) }
    }

    func finish() async {
        for c in continuations { c.finish() }
        for a in analyzers { try? await a.finalizeAndFinishThroughEndOfInput() }
        for r in readers { await r.value }
    }

    private func read(_ lane: Lane) async {
        do {
            for try await result in lane.transcriber.results {
                for seg in segments(result.text, fallback: result.range) {
                    Out.shared.emit("segment", ["locale": lane.id, "start": seg.0, "end": seg.1, "text": seg.2])
                }
            }
        } catch {
            fail(Failure(code: "transcription_failed",
                         message: "The \(lane.id) transcription stopped: \(describe(error)).", locale: lane.id), exitCode: 4)
        }
    }

    /// Split a result into short segments: at pauses of 1.5 s or more, after a sentence end,
    /// and before a word that would make the segment longer than maxLength characters.
    func segments(_ text: AttributedString, fallback: CMTimeRange) -> [(Double, Double, String)] {
        var out: [(Double, Double, String)] = []
        var acc = ""
        var start: Double? = nil
        var end: Double? = nil
        func flush() {
            let t = acc.trimmingCharacters(in: .whitespacesAndNewlines)
            if !t.isEmpty {
                out.append((start ?? fallback.start.seconds, end ?? fallback.end.seconds, t))
            }
            acc = ""; start = nil; end = nil
        }
        for run in text.runs {
            let piece = String(text[run.range].characters)
            let range = run.audioTimeRange
            if let r = range, let e = end, r.start.seconds - e >= 1.5 { flush() }
            let wordStart = piece.first?.isWhitespace ?? false
            if !acc.isEmpty, wordStart, acc.count + piece.count > maxLength { flush() }
            acc += piece
            if let r = range {
                if start == nil { start = r.start.seconds }
                end = r.end.seconds
            }
            let trimmed = acc.trimmingCharacters(in: .whitespaces)
            if let last = trimmed.last, ".?!…".contains(last), trimmed.count >= 12 { flush() }
        }
        flush()
        return out
    }
}

// MARK: - health: a heartbeat about every second (audio buffers received, last level)

final class Level: @unchecked Sendable {
    static let shared = Level()
    private let lock = NSLock()
    private var buffers = 0
    private var rms: Float = 0

    func add(_ buffer: AVAudioPCMBuffer) {
        let n = Int(buffer.frameLength)
        var sum: Float = 0
        if n > 0, let ch = buffer.floatChannelData {
            for i in 0..<n { sum += ch[0][i] * ch[0][i] }
        } else if n > 0, let ch = buffer.int16ChannelData {
            for i in 0..<n { let v = Float(ch[0][i]) / 32768; sum += v * v }
        }
        lock.lock()
        buffers += 1
        if n > 0 { rms = max(rms, (sum / Float(n)).squareRoot()) }
        lock.unlock()
    }

    func beat() {
        lock.lock()
        let fields: [String: Any] = ["buffers": buffers, "rms": Double((rms * 10000).rounded() / 10000)]
        rms = 0
        lock.unlock()
        Out.shared.emit("heartbeat", fields)
    }
}

func startHeartbeat() -> Task<Void, Never> {
    Task.detached {
        while !Task.isCancelled {
            Level.shared.beat()
            try? await Task.sleep(nanoseconds: 1_000_000_000)
        }
    }
}

// MARK: - audio sources

final class Microphone: @unchecked Sendable {
    let engine = AVAudioEngine()
    let converter: AVAudioConverter
    let target: AVAudioFormat
    let sink: (AVAudioPCMBuffer) -> Void

    init(target: AVAudioFormat, sink: @escaping (AVAudioPCMBuffer) -> Void) throws {
        self.target = target
        self.sink = sink
        let input = engine.inputNode
        let inFormat = input.outputFormat(forBus: 0)
        guard inFormat.sampleRate > 0, inFormat.channelCount > 0 else {
            throw Failure(code: "microphone",
                          message: "No microphone input is available (no input device, or the microphone permission is denied for the app that runs pacenote).")
        }
        guard let conv = AVAudioConverter(from: inFormat, to: target) else {
            throw Failure(code: "audio_format", message: "Cannot convert the microphone format \(inFormat) to \(target).")
        }
        converter = conv
        input.installTap(onBus: 0, bufferSize: 4096, format: nil) { [self] buffer, _ in
            Level.shared.add(buffer)
            if let out = convert(buffer, converter, target) { sink(out) }
        }
    }

    func start() throws {
        do {
            try engine.start()
        } catch {
            throw Failure(code: "microphone",
                          message: "The microphone could not be opened: \(describe(error)). Allow the app that runs pacenote (Terminal, iTerm2, Ghostty, VS Code…) in System Settings > Privacy & Security > Microphone, then reopen it.")
        }
    }

    func stop() {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
    }
}

func convert(_ buffer: AVAudioPCMBuffer, _ converter: AVAudioConverter, _ target: AVAudioFormat) -> AVAudioPCMBuffer? {
    let capacity = AVAudioFrameCount(ceil(Double(buffer.frameLength) * target.sampleRate / converter.inputFormat.sampleRate)) + 16
    guard capacity > 0, let out = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return nil }
    var error: NSError?
    nonisolated(unsafe) var consumed = false
    nonisolated(unsafe) let source = buffer
    converter.convert(to: out, error: &error) { _, status in
        if consumed {
            status.pointee = .noDataNow
            return nil
        }
        consumed = true
        status.pointee = .haveData
        return source
    }
    return (error == nil && out.frameLength > 0) ? out : nil
}

func feedFile(_ path: String, into pipeline: Pipeline) throws {
    let file: AVAudioFile
    do {
        file = try AVAudioFile(forReading: URL(fileURLWithPath: path))
    } catch {
        throw Failure(code: "file", message: "Cannot read \(path): \(describe(error)).")
    }
    guard let conv = AVAudioConverter(from: file.processingFormat, to: pipeline.format) else {
        throw Failure(code: "audio_format", message: "Cannot convert \(path) to the speech format.")
    }
    while file.framePosition < file.length {
        guard let buf = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: 8192) else { break }
        try file.read(into: buf)
        if buf.frameLength == 0 { break }
        Level.shared.add(buf)
        if let out = convert(buf, conv, pipeline.format) { pipeline.feed(out) }
    }
}

// MARK: - main

var signalSources: [DispatchSourceSignal] = []

@main
struct FdeListen {
    static func main() async {
        let opts = parseArgs()
        let lanes: [Lane]
        do {
            lanes = try await prepareAssets(opts.locales)
        } catch let f as Failure {
            fail(f, exitCode: 3)
        } catch {
            fail(Failure(code: "assets", message: describe(error)), exitCode: 3)
        }
        if lanes.isEmpty { fail(noLanguageReady(), exitCode: 3) }
        let pipeline = Pipeline(lanes: lanes, maxLength: opts.maxLength)
        do {
            try await pipeline.load()
        } catch let f as Failure {
            fail(f, exitCode: 3)
        } catch {
            fail(Failure(code: "model_load_failed", message: describe(error)), exitCode: 3)
        }
        let ids = pipeline.lanes.map(\.id)
        let failed = skipped.compactMap(\.locale)
        if opts.command == "prepare" {
            await pipeline.finish()
            Out.shared.emit("prepared", ["locales": ids, "failed": failed])
            exit(0)
        }

        if let path = opts.file {
            Out.shared.emit("ready", ["locales": ids, "failed": failed, "source": "file", "sample_rate": pipeline.format.sampleRate])
            let heartbeat = startHeartbeat()
            do { try feedFile(path, into: pipeline) } catch let f as Failure { fail(f, exitCode: 4) } catch {
                fail(Failure(code: "file", message: describe(error)), exitCode: 4)
            }
            Level.shared.beat()  // the whole file was read
            await pipeline.finish()
            heartbeat.cancel()
            Out.shared.emit("stopped")
            exit(0)
        }

        let mic: Microphone
        do {
            mic = try Microphone(target: pipeline.format) { pipeline.feed($0) }
            try mic.start()
        } catch let f as Failure {
            fail(f, exitCode: 3)
        } catch {
            fail(Failure(code: "microphone", message: describe(error)), exitCode: 3)
        }
        Out.shared.emit("ready", ["locales": ids, "failed": failed, "source": "microphone", "sample_rate": pipeline.format.sampleRate])
        let heartbeat = startHeartbeat()

        let stopping = NSLock()
        nonisolated(unsafe) var stopped = false
        let stop: @Sendable () -> Void = {
            stopping.lock()
            if stopped { stopping.unlock(); return }
            stopped = true
            stopping.unlock()
            mic.stop()
            heartbeat.cancel()
            Task {
                await pipeline.finish()
                Out.shared.emit("stopped")
                exit(0)
            }
        }
        for sig in [SIGINT, SIGTERM] {
            signal(sig, SIG_IGN)
            let src = DispatchSource.makeSignalSource(signal: sig, queue: .main)
            src.setEventHandler(handler: stop)
            src.resume()
            signalSources.append(src)
        }
        for r in pipeline.readers { await r.value }  // returns only once finish() ran (or exits on error)
        while true { try? await Task.sleep(nanoseconds: 1_000_000_000) }
    }
}
