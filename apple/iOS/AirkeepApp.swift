import SwiftUI
import UIKit
import UserNotifications

@main struct AirkeepApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @State private var model = MonitorModel()
    var body: some Scene {
        WindowGroup {
            DashboardView(model: model)
                .onOpenURL { _ in Task { await model.refresh(forceHistory: true) } }
                .task { WatchCompanion.shared.activate() }
                #if DEBUG
                .task {
                    await configureDevelopmentConnection()
                    await reportDevelopmentNotificationStatus()
                }
                #endif
        }
    }

    #if DEBUG
    // Provision a paired development device without putting credentials in the
    // binary, source, URL scheme, or preferences. Release builds omit this hook.
    @MainActor private func configureDevelopmentConnection() async {
        let environment = ProcessInfo.processInfo.environment
        guard let endpoint = environment["AIRKEEP_DEVELOPMENT_RELAY_URL"],
              let token = environment["AIRKEEP_DEVELOPMENT_RELAY_TOKEN"],
              URL(string: endpoint)?.scheme == "https", !token.isEmpty else { return }
        do {
            _ = try await RelayClient(endpoint: endpoint, token: token).snapshot()
            try TokenStore.save(token)
            SharedConfiguration.endpoint = endpoint
            await model.refresh(forceHistory: true)
        } catch {
            model.error = "Development connection setup failed: \(error.localizedDescription)"
        }
    }

    @MainActor private func reportDevelopmentNotificationStatus() async {
        guard ProcessInfo.processInfo.arguments.contains("--notification-diagnostics") else { return }
        let center = UNUserNotificationCenter.current()
        let settings = await center.notificationSettings()
        let delivered = await center.deliveredNotifications()
        // Counts/settings only: never log device tokens or notification contents.
        print("Airkeep notification diagnostics: authorization=\(settings.authorizationStatus.rawValue) alert=\(settings.alertSetting.rawValue) lockScreen=\(settings.lockScreenSetting.rawValue) center=\(settings.notificationCenterSetting.rawValue) delivered=\(delivered.count)")
    }
    #endif
}

final class AppDelegate: NSObject, UIApplicationDelegate {
    func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        Task { await NotificationManager.shared.received(token: deviceToken) }
    }
    func application(_ application: UIApplication, didFailToRegisterForRemoteNotificationsWithError error: Error) {
        NotificationManager.shared.registrationFailed(error)
    }
}
