import SwiftUI

struct DashboardView: View {
    @Bindable var model: MonitorModel
    @Environment(\.scenePhase) private var scenePhase
    #if os(macOS)
    @Environment(\.openSettings) private var openSettings
    #endif
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                HStack {
                    AirkeepWordmark()
                    Spacer()
                    Button { Task { await model.refresh(forceHistory: true) } } label: { Image(systemName: "arrow.clockwise") }
                        .buttonStyle(AirkeepActionStyle(iconOnly: true))
                        .disabled(model.refreshing).accessibilityLabel("Refresh readings")
                    AirkeepActionButton(kind: .settings) {
                        #if os(macOS)
                        openSettings()
                        NSApplication.shared.activate()
                        #else
                        model.settingsSection = nil
                        model.showSettings = true
                        #endif
                    }
                }
                HStack(spacing: 8) {
                    Text("P2S / \(model.reading?.amsName ?? "AMS")").font(.subheadline.weight(.medium))
                    Circle().fill(model.demo ? AirkeepTheme.amber : model.isStale ? .gray : .green).frame(width: 7, height: 7)
                    Text(model.demo ? "Preview · sample data" : model.isStale ? "Waiting for fresh data" : "Connected locally")
                        .font(.caption).foregroundStyle(AirkeepTheme.secondary)
                }
                if let units = model.snapshot?.readings, units.count > 1 {
                    Picker("AMS", selection: $model.selectedAMS) {
                        ForEach(units, id: \.amsID) { unit in Text(unit.amsName).tag(unit.amsID) }
                    }.onChange(of: model.selectedAMS) { Task { await model.refresh(forceHistory: true) } }
                }
                Divider().overlay(.white.opacity(0.1))
                VStack(alignment: .leading, spacing: 0) {
                    Text("RELATIVE HUMIDITY").font(.caption.weight(.medium)).tracking(1.7).foregroundStyle(AirkeepTheme.secondary)
                    HumidityValue(reading: model.reading, size: 132, stale: model.isStale && !model.demo)
                }
                HStack {
                    Text("Last \(model.hours == 1 ? "hour" : "\(model.hours) hours")").font(.caption).foregroundStyle(AirkeepTheme.secondary)
                    Spacer()
                    Group {
                        #if os(iOS)
                        AirkeepHistoryRangeControl(selection: $model.hours).frame(height: 32)
                        #else
                        Picker("History range", selection: $model.hours) {
                            Text("1h").tag(1); Text("6h").tag(6); Text("24h").tag(24)
                        }.pickerStyle(.segmented).labelsHidden()
                        #endif
                    }.frame(maxWidth: 150)
                        .onChange(of: model.hours) { Task { await model.refresh(forceHistory: true) } }
                }
                HumidityChart(readings: model.history).frame(height: 175)
                Divider().overlay(.white.opacity(0.1))
                HStack(spacing: 16) {
                    Image(systemName: "thermometer.medium").font(.title2).foregroundStyle(AirkeepTheme.secondary)
                    if let temperature = model.reading?.temperature {
                        Text("\(temperature.formatted(.number.precision(.fractionLength(1)))) °C")
                            .font(.title2.monospacedDigit())
                    } else { Text("— °C").font(.title2) }
                    Spacer()
                    Text("Temperature").font(.caption).foregroundStyle(AirkeepTheme.secondary)
                }
                #if os(iOS)
                if let activity = LiveMonitor.shared.currentActivity {
                    Button {
                        model.settingsSection = .liveActivities
                        model.showSettings = true
                    } label: {
                        HStack {
                            VStack(alignment: .leading, spacing: 6) {
                                Label("Live Activity · \(activity.attributes.amsName)", systemImage: "waveform.path")
                                Text("Ends \(activity.attributes.endsAt, format: .dateTime.hour().minute())")
                                    .font(.caption).foregroundStyle(AirkeepTheme.secondary)
                            }
                            Spacer()
                            Image(systemName: "chevron.right").font(.caption).foregroundStyle(.secondary)
                        }.padding(16).background(AirkeepTheme.ivory.opacity(0.06), in: RoundedRectangle(cornerRadius: 14))
                    }
                    .buttonStyle(.plain)
                    .accessibilityHint("Open Live Activity settings")
                }
                #endif
                if let error = model.error {
                    Text(error).font(.caption).foregroundStyle(AirkeepTheme.amber).textSelection(.enabled)
                }
                if model.reading == nil && SharedConfiguration.endpoint.isEmpty {
                    Button("Preview sample data") {
                        model.demo = true
                        model.reading = PreviewData.reading
                        model.history = PreviewData.history
                        model.error = nil
                    }.buttonStyle(.glass)
                }
                TimelineView(.periodic(from: .now, by: 15)) { context in
                    if let reading = model.reading {
                        HStack(spacing: 4) {
                            Text("Updated")
                            Text(reading.date, style: .relative)
                            Text("ago\(reading.isStale(now: context.date) && !model.demo ? " · stale" : "")")
                        }
                    } else { Text("Add your relay connection to begin") }
                }.font(.caption).foregroundStyle(AirkeepTheme.secondary).frame(maxWidth: .infinity)
            }.padding(24)
        }
        .background(AirkeepTheme.background).foregroundStyle(AirkeepTheme.ivory)
        .tint(AirkeepTheme.amber).preferredColorScheme(.dark)
        .task {
            model.start()
            if !model.demo { await NotificationManager.shared.refresh(); await model.alertRules.refresh() }
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active && !model.demo { Task { await NotificationManager.shared.refresh(); await model.alertRules.refresh() } }
        }
        .sheet(isPresented: $model.showSettings) { SettingsView(model: model) }
        #if os(macOS)
        .frame(minWidth: 390, idealWidth: 430, minHeight: 680)
        #endif
    }
}
