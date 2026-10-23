#if os(iOS)
import ActivityKit
import SwiftUI
import WidgetKit

struct MonitorLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: MonitorAttributes.self) { context in
            HStack {
                VStack(alignment: .leading, spacing: 6) {
                    Text("Airkeep · \(context.attributes.amsName)").font(.caption)
                    HStack(alignment: .firstTextBaseline, spacing: 4) {
                        Text(context.state.humidity.map { "\(Int($0))" } ?? "—").font(.system(size: 38, weight: .bold, design: .rounded)).monospacedDigit()
                        Text("% RH").foregroundStyle(AirkeepTheme.amber)
                    }
                }
                Spacer()
                VStack(alignment: .trailing, spacing: 6) {
                    if let temp = context.state.temperature { Text("\(temp.formatted(.number.precision(.fractionLength(1)))) °C") }
                    if context.isStale || !context.state.connected {
                        Text("Reading out of date").font(.caption2)
                    } else {
                        Text("Updated \(Date(timeIntervalSince1970: context.state.observedAt), style: .relative) ago").font(.caption2)
                    }
                    Text(timerInterval: Date.now...max(Date.now, context.attributes.endsAt), countsDown: true).font(.caption.monospacedDigit())
                }.foregroundStyle(.secondary)
            }.padding(18).activityBackgroundTint(AirkeepTheme.background).activitySystemActionForegroundColor(AirkeepTheme.ivory)
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    Label(context.attributes.amsName, systemImage: "humidity").font(.caption).foregroundStyle(AirkeepTheme.amber)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    Text(context.state.humidity.map { "\(Int($0))% RH" } ?? "—% RH").font(.title2.bold()).monospacedDigit()
                }
                DynamicIslandExpandedRegion(.bottom) {
                    HStack {
                        Text(context.isStale || !context.state.connected ? "Reading out of date" : "Airkeep monitoring").font(.caption)
                        Spacer()
                        Text(timerInterval: Date.now...max(Date.now, context.attributes.endsAt), countsDown: true).font(.caption.monospacedDigit()).frame(width: 70)
                    }
                }
            } compactLeading: {
                Image(systemName: "humidity.fill").foregroundStyle(AirkeepTheme.amber)
            } compactTrailing: {
                Text(context.isStale || !context.state.connected ? "—" : context.state.humidity.map { "\(Int($0))%" } ?? "—").font(.caption.bold()).monospacedDigit()
            } minimal: {
                Text(context.isStale || !context.state.connected ? "—" : context.state.humidity.map { "\(Int($0))" } ?? "—").font(.caption.bold()).foregroundStyle(AirkeepTheme.amber)
            }.widgetURL(URL(string: "airkeep://dashboard")).keylineTint(AirkeepTheme.amber)
        }
    }
}
#endif
