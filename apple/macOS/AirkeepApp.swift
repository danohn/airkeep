import SwiftUI
import AppKit

@main struct AirkeepApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @State private var model = MonitorModel()
    var body: some Scene {
        Window("Airkeep", id: "dashboard") { DashboardView(model: model) }
            .defaultSize(width: 1_200, height: 1_000)
        MenuBarExtra {
            MenuBarView(model: model)
        } label: {
            Label(model.reading.map { "\($0.humidityText)% RH\(model.isStale && !model.demo ? " ·" : "")" } ?? "—% RH", systemImage: "humidity")
                .labelStyle(.titleAndIcon)
        }.menuBarExtraStyle(.window)
        Settings { SettingsView(model: model) }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func application(_ application: NSApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        Task { await NotificationManager.shared.received(token: deviceToken) }
    }
    func application(_ application: NSApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        NotificationManager.shared.registrationFailed(error)
    }
    func applicationDidBecomeActive(_ notification: Notification) {
        Task { await NotificationManager.shared.refresh() }
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
}
