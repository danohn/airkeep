import Foundation
import Observation
import WidgetKit

@MainActor @Observable final class MonitorModel {
    var reading: Reading?
    var history: [Reading] = []
    var snapshot: Snapshot?
    var error: String?
    var refreshing = false
    var hours = 1
    var selectedAMS = SharedConfiguration.amsID
    var showSettings = false
    var showAlerts = false
    var demo = ProcessInfo.processInfo.arguments.contains("--demo")
    private var task: Task<Void, Never>?
    private var lastHistory = Date.distantPast
    private var lastWidget = Date.distantPast
    var isStale: Bool { reading?.isStale(after: snapshot?.staleAfter ?? 90) ?? true }

    init() {
        if demo { reading = PreviewData.reading; history = PreviewData.history }
        else { reading = SharedConfiguration.cached }
    }
    func start() {
        guard task == nil else { return }
        task = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                await self.refresh()
                try? await Task.sleep(for: .seconds(5))
            }
        }
    }
    func refresh(forceHistory: Bool = false) async {
        if demo { return }
        guard !refreshing else { return }
        refreshing = true
        defer { refreshing = false }
        do {
            let client = RelayClient()
            let data = try await client.snapshot()
            snapshot = data
            reading = data.readings.first { $0.amsID == selectedAMS }
            SharedConfiguration.cached = reading
            SharedConfiguration.amsID = selectedAMS
            if forceHistory || Date.now.timeIntervalSince(lastHistory) > 30 {
                history = try await client.history(amsID: selectedAMS, hours: hours)
                lastHistory = .now
            }
            error = nil
            if Date.now.timeIntervalSince(lastWidget) > 60 {
                WidgetCenter.shared.reloadAllTimelines()
                lastWidget = .now
            }
            #if os(iOS)
            await LiveMonitor.shared.update(reading)
            #endif
        } catch { self.error = error.localizedDescription }
    }
    func saveRule(_ rule: AlertRule) async throws {
        try await RelayClient().saveRule(rule)
        await refresh()
    }
}
