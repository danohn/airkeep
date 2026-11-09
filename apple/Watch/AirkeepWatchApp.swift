import SwiftUI
import WatchKit

@main struct AirkeepWatchApp: App {
    @WKApplicationDelegateAdaptor(WatchAppDelegate.self) private var delegate
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

final class WatchAppDelegate: NSObject, WKApplicationDelegate {
    func didRegisterForRemoteNotifications(withDeviceToken deviceToken: Data) {
        Task { await NotificationManager.shared.received(token: deviceToken) }
    }

    func didFailToRegisterForRemoteNotificationsWithError(_ error: Error) {
        Task { @MainActor in NotificationManager.shared.registrationFailed(error) }
    }
}
