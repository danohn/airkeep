import SwiftUI

struct DashboardView: View {
    @Bindable var model: MonitorModel
    @Environment(\.scenePhase) private var scenePhase
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                HStack {
                    AirkeepWordmark()
                    Spacer()
                    Button { model.showSettings = true } label: { Image(systemName: "gearshape") }
                        .buttonStyle(.glass).accessibilityLabel("Connection settings")
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
                    Picker("History range", selection: $model.hours) {
                        Text("1h").tag(1); Text("6h").tag(6); Text("24h").tag(24)
                    }.pickerStyle(.segmented).labelsHidden().frame(maxWidth: 150)
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
                Divider().overlay(.white.opacity(0.1))
                HStack {
                    Button { model.showAlerts = true } label: { Label("Humidity alerts", systemImage: "bell") }
                    Spacer()
                    Button { Task { await model.refresh(forceHistory: true) } } label: { Image(systemName: "arrow.clockwise") }
                        .disabled(model.refreshing).accessibilityLabel("Refresh readings")
                }.buttonStyle(.glass)
                #if os(iOS)
                if let activity = LiveMonitor.shared.currentActivity {
                    VStack(alignment: .leading, spacing: 8) {
                        Label("Live Activity · \(activity.attributes.amsName)", systemImage: "waveform.path")
                        Text("Ends \(activity.attributes.endsAt, format: .dateTime.hour().minute())")
                            .font(.caption).foregroundStyle(AirkeepTheme.secondary)
                        Button("End Live Activity", role: .destructive) { Task { await LiveMonitor.shared.stopAll() } }
                            .buttonStyle(.glass)
                    }
                } else {
                    Button {
                        Task {
                            do { try await LiveMonitor.shared.start(model.reading, demo: model.demo,
                                                                   staleAfter: model.snapshot?.staleAfter ?? 90) }
                            catch { model.error = error.localizedDescription }
                        }
                    } label: { Label("Start Live Activity", systemImage: "waveform.path") }
                        .buttonStyle(.glass).disabled(model.reading == nil || model.demo || model.isStale)
                    Text("Show humidity on your Lock Screen and Dynamic Island for up to eight hours.")
                        .font(.caption).foregroundStyle(AirkeepTheme.secondary)
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
            if !model.demo { await NotificationManager.shared.refresh() }
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active && !model.demo { Task { await NotificationManager.shared.refresh() } }
        }
        .sheet(isPresented: $model.showSettings) { ConnectionView(model: model) }
        .sheet(isPresented: $model.showAlerts) { AlertsView(model: model) }
        #if os(macOS)
        .frame(minWidth: 390, idealWidth: 430, minHeight: 680)
        #endif
    }
}
