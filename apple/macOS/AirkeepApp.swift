import SwiftUI
import AppKit

@main struct AirkeepApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @State private var model = MonitorModel()
    var body: some Scene {
        WindowGroup("Airkeep", id: "dashboard") { DashboardView(model: model) }
            .defaultSize(width: 430, height: 790)
        MenuBarExtra {
            MenuBarView(model: model)
        } label: {
            Label(model.reading.map { "\($0.humidityText)%\(model.isStale && !model.demo ? " ·" : "")" } ?? "—%", systemImage: "humidity")
        }.menuBarExtraStyle(.window)
        Settings { ConnectionView(model: model) }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func application(_ application: NSApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        NotificationManager.shared.received(token: deviceToken)
    }
    func application(_ application: NSApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        NotificationManager.shared.status = error.localizedDescription
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
}
