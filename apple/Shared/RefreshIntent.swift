import AppIntents
import WidgetKit

struct RefreshAirkeepIntent: AppIntent {
    static let title: LocalizedStringResource = "Refresh AMS humidity"
    static let description = IntentDescription("Fetch the latest reading from your Airkeep relay.")
    func perform() async throws -> some IntentResult & ProvidesDialog {
        let snapshot = try await RelayClient().snapshot()
        let reading = snapshot.readings.first { $0.amsID == SharedConfiguration.amsID }
        SharedConfiguration.cached = reading
        WidgetCenter.shared.reloadAllTimelines()
        guard let reading else { return .result(dialog: "No AMS reading is available yet.") }
        if reading.isStale(after: snapshot.staleAfter) {
            return .result(dialog: "The last reading was \(reading.humidityText) percent. It's out of date.")
        }
        return .result(dialog: "\(reading.amsName) humidity is \(reading.humidityText) percent.")
    }
}

struct AirkeepShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(intent: RefreshAirkeepIntent(), phrases: ["Check humidity in \(.applicationName)", "Refresh \(.applicationName)"],
                    shortTitle: "Check humidity", systemImageName: "humidity")
    }
}
