import SwiftUI
import WidgetKit
import AppIntents

struct AirkeepEntry: TimelineEntry {
    var date: Date
    var reading: Reading?
    var history: [Reading] = []
    var preview = false
}

struct AMSConfigurationIntent: WidgetConfigurationIntent {
    static let title: LocalizedStringResource = "AMS selection"
    static let description = IntentDescription("Select the AMS identifier reported by your printer.")
    @Parameter(title: "AMS ID", default: "0") var amsID: String
}

struct Provider: AppIntentTimelineProvider {
    func placeholder(in context: Context) -> AirkeepEntry { .init(date: .now, reading: PreviewData.reading, history: PreviewData.history, preview: true) }
    func snapshot(for configuration: AMSConfigurationIntent, in context: Context) async -> AirkeepEntry {
        context.isPreview ? placeholder(in: context) : .init(date: .now, reading: SharedConfiguration.cached)
    }
    func timeline(for configuration: AMSConfigurationIntent, in context: Context) async -> Timeline<AirkeepEntry> {
        var reading = SharedConfiguration.cached
        if reading?.amsID != configuration.amsID { reading = nil }
        var history: [Reading] = []
        do {
            let client = RelayClient()
            let snapshot = try await client.snapshot()
            reading = snapshot.readings.first { $0.amsID == configuration.amsID }
            history = try await client.history(amsID: configuration.amsID, hours: 1)
            if configuration.amsID == SharedConfiguration.amsID { SharedConfiguration.cached = reading }
        } catch { /* Retain original timestamp: offline data becomes stale. */ }
        let now = Date.now
        let entry = AirkeepEntry(date: now, reading: reading, history: history)
        let staleAt = reading?.date.addingTimeInterval(91) ?? now
        let entries = staleAt > now ? [entry, AirkeepEntry(date: staleAt, reading: reading, history: history)] : [entry]
        return Timeline(entries: entries, policy: .after(now.addingTimeInterval(15 * 60)))
    }
}

struct WidgetView: View {
    var entry: AirkeepEntry
    @Environment(\.widgetFamily) private var family
    var stale: Bool { entry.reading?.isStale(now: entry.date) ?? true }
    private var accessory: Bool {
        #if os(iOS)
        family == .accessoryRectangular
        #else
        false
        #endif
    }
    var body: some View {
        VStack(alignment: .leading, spacing: accessory ? 2 : 8) {
            HStack {
                Text(entry.reading?.amsName ?? "Airkeep").font(.caption.weight(.medium))
                Spacer()
                if family == .systemMedium {
                    Button(intent: RefreshAirkeepIntent()) { Image(systemName: "arrow.clockwise") }
                        .buttonStyle(.plain).accessibilityLabel("Refresh humidity")
                }
            }
            HStack(alignment: .firstTextBaseline, spacing: 4) {
                Text(entry.reading?.humidityText ?? "—").font(.system(size: accessory ? 25 : 48, weight: .bold, design: .rounded)).monospacedDigit()
                Text("% RH").font(.caption.weight(.semibold)).foregroundStyle(AirkeepTheme.amber)
                if family == .systemMedium {
                    Spacer()
                    if let temperature = entry.reading?.temperature { Text("\(temperature.formatted(.number.precision(.fractionLength(1)))) °C").font(.title3) }
                }
            }.foregroundStyle(stale && !entry.preview ? AirkeepTheme.secondary : AirkeepTheme.ivory)
            if !accessory && !entry.history.isEmpty {
                // Draw the history directly from readings with a SwiftUI Path.
                Sparkline(readings: entry.history).frame(height: 24).foregroundStyle(AirkeepTheme.amber)
            }
            if entry.preview { Text("Preview").font(.caption2).foregroundStyle(.secondary) }
            else if let reading = entry.reading {
                Text("\(stale ? "Stale · " : "Updated ")\(reading.date, style: .relative) ago").font(.caption2).foregroundStyle(.secondary)
            } else { Text("Set up Airkeep").font(.caption2).foregroundStyle(.secondary) }
        }
        .containerBackground(AirkeepTheme.background, for: .widget)
        .widgetURL(URL(string: "airkeep://dashboard"))
    }
}

struct Sparkline: View {
    var readings: [Reading]
    var body: some View {
        GeometryReader { proxy in
            Path { path in
                let lo = max(0, (readings.map(\.humidity).min() ?? 0) - 2)
                let hi = min(100, (readings.map(\.humidity).max() ?? 100) + 2)
                for (index, reading) in readings.enumerated() {
                    let point = CGPoint(x: proxy.size.width * Double(index) / Double(max(1, readings.count - 1)),
                                        y: proxy.size.height * (1 - (reading.humidity-lo) / max(1,hi-lo)))
                    if index == 0 { path.move(to: point) } else { path.addLine(to: point) }
                }
            }.stroke(lineWidth: 2)
        }.accessibilityHidden(true)
    }
}

struct HumidityWidget: Widget {
    let kind = "AirkeepHumidity"
    var body: some WidgetConfiguration {
        AppIntentConfiguration(kind: kind, intent: AMSConfigurationIntent.self, provider: Provider()) { entry in WidgetView(entry: entry) }
            .configurationDisplayName("AMS humidity")
            .description("Humidity and temperature from your Airkeep relay. Updates are scheduled by the system.")
            #if os(iOS)
            .supportedFamilies([.systemSmall, .systemMedium, .accessoryRectangular])
            #else
            .supportedFamilies([.systemSmall, .systemMedium])
            #endif
    }
}

@main struct AirkeepWidgets: WidgetBundle {
    var body: some Widget {
        HumidityWidget()
        #if os(iOS)
        MonitorLiveActivity()
        #endif
    }
}
