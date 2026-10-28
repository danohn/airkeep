import Foundation
import Observation
import WidgetKit

@MainActor @Observable final class MonitorModel {
    enum SettingsSection: String, CaseIterable, Identifiable {
        case alerts, notifications, liveActivities, relay
        var id: String { rawValue }
        var title: String {
            switch self {
            case .alerts: "Humidity alerts"
            case .notifications: "Notifications"
            case .liveActivities: "Live Activities"
            case .relay: "Relay"
            }
        }
        var symbol: String {
            switch self {
            case .alerts: "bell"
            case .notifications: "app.badge"
            case .liveActivities: "waveform.path"
            case .relay: "network"
            }
        }
    }

    var reading: Reading?
    var history: [Reading] = []
    var snapshot: Snapshot?
    var error: String?
    var refreshing = false
    var hours = 1
    var selectedAMS = SharedConfiguration.amsID
    var showSettings = false
    var settingsSection: SettingsSection?
    var settingsVisible = false
    var relayReachable = false
    var relayCheckedAt: Date?
    let alertRules = AlertRulesModel()
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
        var snapshotReceived = false
        let client = RelayClient()
        do {
            let data = try await client.snapshot()
            guard client.endpoint == SharedConfiguration.endpoint && client.token == TokenStore.read() else { return }
            snapshotReceived = true
            relayReachable = true
            relayCheckedAt = .now
            snapshot = data
            reading = data.readings.first { $0.amsID == selectedAMS }
            SharedConfiguration.cached = reading
            SharedConfiguration.amsID = selectedAMS
            if forceHistory || Date.now.timeIntervalSince(lastHistory) > 30 {
                let loaded = try await client.history(amsID: selectedAMS, hours: hours)
                guard client.endpoint == SharedConfiguration.endpoint && client.token == TokenStore.read() else { return }
                history = loaded
                lastHistory = .now
            }
            error = nil
            if Date.now.timeIntervalSince(lastWidget) > 60 {
                WidgetCenter.shared.reloadAllTimelines()
                lastWidget = .now
            }
            #if os(iOS)
            await LiveMonitor.shared.update(data.readings, staleAfter: data.staleAfter)
            #endif
        } catch {
            guard client.endpoint == SharedConfiguration.endpoint && client.token == TokenStore.read() else { return }
            if !snapshotReceived { relayReachable = false; relayCheckedAt = .now }
            self.error = error.localizedDescription
        }
    }
    func connectionChanged() {
        snapshot = nil; reading = nil; history = []; error = nil
        relayReachable = false; relayCheckedAt = nil
        lastHistory = .distantPast; lastWidget = .distantPast
    }
}
