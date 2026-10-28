import SwiftUI
import WidgetKit

struct WatchHumidityProvider: AppIntentTimelineProvider {
    func recommendations() -> [AppIntentRecommendation<WatchAMSIntent>] {
        let state = try? WatchStorage.live.read()
        let identifiers = state?.endpoint.flatMap { state?.snapshot[$0]?.snapshot.readings.map(\.amsID) } ?? [state?.selectedAMS ?? "0"]
        return Array(Set(identifiers)).sorted().map { identifier in
            let intent = WatchAMSIntent()
            intent.ams = WatchAMSEntity(id: identifier)
            return AppIntentRecommendation(intent: intent, description: "AMS \((Int(identifier) ?? 0) + 1)")
        }
    }
    func placeholder(in context: Context) -> WatchHumidityEntry {
        .init(date: .now, reading: Reading(amsID: "0", humidity: 19, temperature: 24.4, observedAt: Date.now.timeIntervalSince1970), preview: true)
    }
    func snapshot(for configuration: WatchAMSIntent, in context: Context) async -> WatchHumidityEntry {
        if context.isPreview {
            var entry = placeholder(in: context)
            entry.amsID = configuration.ams?.id ?? entry.amsID
            entry.reading?.amsID = entry.amsID
            return entry
        }
        return cached(configuration)
    }
    private func cached(_ configuration: WatchAMSIntent) -> WatchHumidityEntry {
        WatchHumidityEntry.cached(amsID: configuration.ams?.id, state: try? WatchStorage.live.read())
    }
    func timeline(for configuration: WatchAMSIntent, in context: Context) async -> Timeline<WatchHumidityEntry> {
        await WatchWidgetSetup.reconcile()
        await WatchWidgetRegistration.live.sync()
        if let connection = try? WatchStorage.live.connection(),
           let snapshot = try? await connection.client.snapshot() {
            try? WatchStorage.live.cache(snapshot, endpoint: connection.endpoint)
        }
        let entry = cached(configuration)
        var entries = [entry]
        if let reading = entry.reading {
            let staleAt = reading.date.addingTimeInterval(entry.staleAfter + 1)
            if staleAt > entry.date { entries.append(.init(date: staleAt, amsID: entry.amsID, reading: reading, staleAfter: entry.staleAfter)) }
        }
        return Timeline(entries: entries, policy: .after(Date.now.addingTimeInterval(15 * 60)))
    }
}

struct WatchHumidityPushHandler: WidgetPushHandler {
    func pushTokenDidChange(_ pushInfo: WidgetPushInfo, widgets: [WidgetInfo]) {
        let token = pushInfo.token.map { String(format: "%02x", $0) }.joined()
        guard let state = try? WatchStorage.live.read() else { return }
        try? WatchStorage.live.configureWidgets(token: token,
            amsIDs: WatchWidgetSetup.amsIDs(from: widgets, fallback: state.selectedAMS),
            environment: SharedConfiguration.environment, bundleID: WatchWidgetSetup.bundleID)
        Task { await WatchWidgetRegistration.live.sync() }
    }
}

struct WatchHumidityWidgetView: View {
    var entry: WatchHumidityEntry
    @Environment(\.widgetFamily) private var family
    @Environment(\.widgetRenderingMode) private var renderingMode
    private var stale: Bool { !entry.preview && (entry.reading?.isStale(after: entry.staleAfter, now: entry.date) ?? true) }
    private var value: String { entry.reading?.humidityText ?? "—" }
    private var accent: Color { renderingMode == .fullColor ? AirkeepTheme.amber : .primary }
    var body: some View {
        Group {
            switch family {
            case .accessoryInline:
                if let reading = entry.reading {
                    Text("\(value)% · \(Text(reading.date, style: .relative)) ago")
                } else { Text("Airkeep · Set up on iPhone") }
            case .accessoryCircular:
                VStack(spacing: 1) {
                    Image(systemName: "humidity.fill").font(.caption2).foregroundStyle(accent).widgetAccentable()
                    Text("\(value)%").font(.system(size: 20, weight: .bold, design: .rounded)).monospacedDigit()
                    if let reading = entry.reading { Text(reading.date, style: .relative).font(.system(size: 8)) }
                }
            default:
                VStack(alignment: .leading, spacing: 3) {
                    HStack {
                        Text(entry.amsName).font(.caption.weight(.medium))
                        Spacer()
                        if let temperature = entry.reading?.temperature {
                            Text("\(temperature.formatted(.number.precision(.fractionLength(1))))°C").font(.caption2)
                        }
                    }
                    HStack(alignment: .firstTextBaseline, spacing: 4) {
                        Text(value).font(.system(size: 30, weight: .bold, design: .rounded)).monospacedDigit()
                        Text("% RH").font(.caption.weight(.semibold)).foregroundStyle(accent).widgetAccentable()
                    }
                    if let reading = entry.reading { Text("Updated \(reading.date, style: .relative) ago").font(.caption2) }
                    else { Text("Set up on iPhone").font(.caption2) }
                }
            }
        }
        .foregroundStyle(stale ? .secondary : .primary)
        .containerBackground(for: .widget) { AirkeepTheme.background }
        .widgetURL(entry.widgetURL)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(entry.amsName) humidity")
        .accessibilityValue(entry.reading.map { "\($0.humidityText) percent, updated \($0.date.formatted(.relative(presentation: .named)))" } ?? "Set up Airkeep on iPhone")
    }
}

struct WatchHumidityWidget: Widget {
    var body: some WidgetConfiguration {
        AppIntentConfiguration(kind: WatchWidgetSetup.kind, intent: WatchAMSIntent.self, provider: WatchHumidityProvider()) {
            WatchHumidityWidgetView(entry: $0)
        }
        .configurationDisplayName("AMS humidity")
        .description("Humidity and temperature at a glance.")
        .supportedFamilies([.accessoryCircular, .accessoryInline, .accessoryRectangular])
        .pushHandler(WatchHumidityPushHandler.self)
    }
}

@main struct AirkeepWatchWidgets: WidgetBundle {
    var body: some Widget { WatchHumidityWidget() }
}
