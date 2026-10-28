import SwiftUI
import Charts
import WidgetKit

struct WatchDashboardView: View {
    @Bindable var model: WatchMonitorModel
    @Environment(\.scenePhase) private var phase
    @Environment(\.isLuminanceReduced) private var ambient
    @State private var path: [Destination] = []
    private enum Destination: String, Hashable { case history, alerts, connection, ams }
    var body: some View {
        NavigationStack(path: $path) {
            ScrollView {
                VStack(alignment: .leading, spacing: 4) {
                    HStack(alignment: .firstTextBaseline) {
                        AirkeepWordmark(size: 17).foregroundStyle(AirkeepTheme.ivory)
                        Spacer(minLength: 4)
                        if model.amsIDs.count > 1 {
                            NavigationLink(value: Destination.ams) {
                                HStack(spacing: 3) { Text(amsName); Image(systemName: "chevron.down").font(.system(size: 9)) }
                            }.buttonStyle(.plain).font(.system(size: 12))
                        } else { Text(amsName).font(.system(size: 12)).foregroundStyle(.secondary) }
                    }
                    HumidityValue(reading: model.reading, size: 60, stale: model.isStale && !model.demo)
                        .fixedSize(horizontal: false, vertical: true).frame(maxWidth: .infinity, alignment: .leading)
                    if let temperature = model.reading?.temperature {
                        Label("\(temperature.formatted(.number.precision(.fractionLength(1)))) °C", systemImage: "thermometer.medium")
                            .font(.system(size: 16, weight: .medium)).labelStyle(.titleAndIcon)
                    }
                    if let reading = model.reading {
                        Text("Updated \(reading.date, style: .relative) ago").font(.system(size: 10)).foregroundStyle(.secondary)
                    } else {
                        Text(model.hasConnection ? "Waiting for a reading" : "Open Airkeep on iPhone to connect.")
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                    VStack(spacing: 0) {
                        destination(.history, title: "History", icon: "chart.bar.fill")
                        Divider().padding(.leading, 30)
                        destination(.alerts, title: "Alerts", icon: "bell", detail: "\(model.enabledCount) enabled")
                    }
                    .background(.white.opacity(0.06), in: RoundedRectangle(cornerRadius: 16))
                    .overlay(RoundedRectangle(cornerRadius: 16).strokeBorder(.white.opacity(0.08)))
                    .padding(.top, 5)
                    if let error = model.error ?? model.storageError { Text(error).font(.caption2).foregroundStyle(.orange) }
                    Button { Task { await model.refresh(); WidgetCenter.shared.reloadAllTimelines() } } label: {
                        Label(model.refreshing ? "Refreshing…" : "Refresh", systemImage: "arrow.clockwise")
                    }.disabled(model.refreshing || model.demo || !model.hasConnection)
                    NavigationLink("Connection", value: Destination.connection).font(.caption)
                }.padding(.horizontal, 7).padding(.bottom, 8)
            }
            .navigationDestination(for: Destination.self) { destination in
                switch destination {
                case .history: WatchHistoryView(model: model)
                case .alerts: WatchAlertsView(model: model)
                case .connection: WatchConnectionView(model: model)
                case .ams: WatchAMSSelectionView(model: model)
                }
            }
            .background(AirkeepTheme.background).foregroundStyle(AirkeepTheme.ivory)
        }
        .tint(AirkeepTheme.amber)
        .task { if !model.demo { WatchBridge.shared.activate() }; model.setActive(phase == .active && !ambient) }
        .onChange(of: phase) { _, value in model.setActive(value == .active && !ambient) }
        .onChange(of: ambient) { _, value in model.setActive(phase == .active && !value) }
        .onReceive(NotificationCenter.default.publisher(for: .airkeepWatchChanged)) { _ in
            model.reloadCache()
            if model.isActive { Task { await model.refresh() } }
        }
        .onOpenURL { url in
            guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false), components.scheme == "airkeep" else { return }
            if let ams = components.queryItems?.first(where: { $0.name == "ams" })?.value { model.chooseAMS(ams) }
            path = []
            Task { await model.refresh() }
        }
    }
    private var amsName: String { model.reading?.amsName ?? "AMS \((Int(model.selectedAMS) ?? 0) + 1)" }
    private func destination(_ target: Destination, title: String, icon: String, detail: String? = nil) -> some View {
        NavigationLink(value: target) {
            HStack(spacing: 8) {
                Image(systemName: icon).foregroundStyle(AirkeepTheme.amber).frame(width: 19)
                Text(title).font(.body.weight(.medium))
                Spacer(minLength: 0)
                if let detail { Text(detail).font(.caption2).foregroundStyle(.secondary) }
                Image(systemName: "chevron.right").font(.caption).foregroundStyle(.secondary)
            }.padding(.horizontal, 10).frame(minHeight: 44).contentShape(Rectangle())
        }.buttonStyle(.plain)
    }
}

private struct WatchAMSSelectionView: View {
    var model: WatchMonitorModel
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        List(model.amsIDs, id: \.self) { identifier in
            Button {
                model.chooseAMS(identifier)
                dismiss()
            } label: {
                HStack {
                    Text("AMS \((Int(identifier) ?? 0) + 1)")
                    Spacer()
                    if model.selectedAMS == identifier { Image(systemName: "checkmark").foregroundStyle(AirkeepTheme.amber) }
                }
            }
        }.navigationTitle("AMS")
    }
}

struct WatchHistoryView: View {
    @Bindable var model: WatchMonitorModel
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                Text(model.reading?.amsName ?? "AMS").font(.caption).foregroundStyle(.secondary)
                HStack(spacing: 5) {
                    ForEach([1, 6, 24], id: \.self) { hours in
                        Button("\(hours)h") {
                            model.hours = hours
                            model.reloadCache()
                            Task { await model.refreshHistory(force: true) }
                        }
                        .font(.caption.weight(.semibold)).buttonStyle(.plain)
                        .frame(maxWidth: .infinity, minHeight: 38)
                        .background(model.hours == hours ? AirkeepTheme.amber : .white.opacity(0.06), in: Capsule())
                        .foregroundStyle(model.hours == hours ? AirkeepTheme.background : AirkeepTheme.ivory)
                        .accessibilityAddTraits(model.hours == hours ? .isSelected : [])
                    }
                }
                if model.history.isEmpty {
                    Text(model.historyRefreshing ? "Loading history…" : "History appears as readings arrive.").font(.caption).foregroundStyle(.secondary)
                        .frame(minHeight: 100)
                } else {
                    Chart(model.history) { reading in
                        LineMark(x: .value("Time", reading.date), y: .value("Humidity", reading.humidity))
                            .foregroundStyle(AirkeepTheme.amber)
                    }
                    .chartYScale(domain: 0...100).chartYAxis { AxisMarks(values: [0, 50, 100]) }
                    .chartXAxis { AxisMarks(values: .automatic(desiredCount: 3)) }
                    .frame(height: 105)
                    .accessibilityLabel("\(model.hours)-hour relative humidity history")
                }
                if let error = model.historyError { Text(error).font(.caption2).foregroundStyle(.orange) }
                Button("Refresh") { Task { await model.refreshHistory(force: true) } }.disabled(model.historyRefreshing || model.demo)
            }.padding(.horizontal, 7)
        }
        .navigationTitle("History")
        .onAppear { model.historyVisible = true; Task { await model.refreshHistory(force: true) } }
        .onDisappear { model.historyVisible = false }
    }
}

struct WatchAlertsView: View {
    var model: WatchMonitorModel
    var body: some View {
        List {
            ForEach(model.rules) { rule in
                VStack(alignment: .leading, spacing: 5) {
                    Text(rule.name).font(.headline)
                    Text(rule.conditionSummary).font(.caption2).foregroundStyle(.secondary)
                    Text(!rule.enabled ? "Off" : rule.active ? "Above limit" : "Watching humidity")
                        .font(.caption2).foregroundStyle(rule.active ? AirkeepTheme.amber : AirkeepTheme.secondary)
                }.padding(.vertical, 4)
            }
            if model.rules.isEmpty { Text("Add alerts in Airkeep on iPhone or Mac.").font(.caption).foregroundStyle(.secondary) }
            if let synced = model.rulesSyncedAt { Text("Synced \(synced, style: .relative) ago").font(.caption2).foregroundStyle(.secondary) }
        }.navigationTitle("Alerts")
    }
}

struct WatchConnectionView: View {
    var model: WatchMonitorModel
    @State private var retrying = false
    var body: some View {
        List {
            Section("Relay") {
                Text(model.hasConnection ? "Set up from iPhone" : "Open Airkeep on iPhone to connect.").font(.caption)
            }
            Section("Complication updates") {
                if let registration = model.registration, !registration.request.amsIDs.isEmpty {
                    Text(registration.confirmed == nil ? registration.error == nil ? "Registration pending" : "Needs attention" :
                            registration.confirmed?.configuration == "available" ? "Registered" : "Check push setup on iPhone")
                        .font(.caption)
                    if let error = registration.error { Text(error).font(.caption2).foregroundStyle(.orange) }
                    Button(retrying ? "Retrying…" : "Retry") {
                        Task { retrying = true; await WatchWidgetRegistration.live.retry(); model.reloadCache(); retrying = false }
                    }.disabled(retrying || model.demo)
                } else { Text("Add an Airkeep complication or Smart Stack widget.").font(.caption) }
            }
        }.navigationTitle("Connection")
    }
}
