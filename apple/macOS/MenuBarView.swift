import SwiftUI

struct MenuBarView: View {
    @Bindable var model: MonitorModel
    @Environment(\.openWindow) private var openWindow
    @Environment(\.openSettings) private var openSettings
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                AirkeepWordmark(size: 22)
                Spacer()
                AirkeepActionButton(kind: .settings) { openSettings(); NSApplication.shared.activate() }
            }
            HStack {
                Text("P2S / \(model.reading?.amsName ?? "AMS")").font(.caption).foregroundStyle(.secondary)
                Spacer()
                Text(model.demo ? "Preview" : model.isStale ? "Stale" : "Connected").font(.caption).foregroundStyle(.secondary)
            }
            HumidityValue(reading: model.reading, size: 66, stale: model.isStale && !model.demo)
            Divider()
            if let temp = model.reading?.temperature {
                Label("\(temp.formatted(.number.precision(.fractionLength(1)))) °C", systemImage: "thermometer.medium")
            }
            if let reading = model.reading {
                Text("Updated \(reading.date, style: .relative) ago").font(.caption).foregroundStyle(.secondary)
            }
            if let error = model.error { Text(error).font(.caption).foregroundStyle(.orange) }
            Button("Open Airkeep") { openDashboard() }
                .buttonStyle(.glass).frame(maxWidth: .infinity)
            HStack {
                Button("Refresh") { Task { await model.refresh(forceHistory: true) } }
                Spacer()
                Button("Quit") { NSApplication.shared.terminate(nil) }
            }.font(.caption).buttonStyle(.plain).foregroundStyle(.secondary)
        }.padding(20).frame(width: 280).background(AirkeepTheme.background)
            .foregroundStyle(AirkeepTheme.ivory).tint(AirkeepTheme.amber).preferredColorScheme(.dark)
            .task { model.start() }
    }

    private func openDashboard() {
        openWindow(id: "dashboard")
        NSApplication.shared.activate()
    }
}
