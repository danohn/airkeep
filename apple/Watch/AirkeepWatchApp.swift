import SwiftUI

@main struct AirkeepWatchApp: App {
    @State private var model: WatchMonitorModel
    init() {
        _model = State(initialValue: WatchMonitorModel(dependencies: .init(
            fetch: { try await WatchRelay(connection: $0).fetch($1, ams: $2, hours: $3) },
            fallback: { try await WatchBridge.shared.fetch(connection: $0, resource: $1, ams: $2, hours: $3) },
            syncWidgets: { await WatchWidgetSetup.reconcile(); await WatchWidgetRegistration.live.sync() })))
    }
    var body: some Scene {
        WindowGroup { WatchDashboardView(model: model) }
            .backgroundTask(.watchConnectivity) { await WatchBridge.shared.finishBackgroundDelivery() }
    }
}
