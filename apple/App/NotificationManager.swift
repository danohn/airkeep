import Foundation
import Observation
import UserNotifications
#if os(macOS)
import AppKit
#else
import UIKit
#endif

@MainActor @Observable final class NotificationManager: NSObject, UNUserNotificationCenterDelegate {
    static let shared = NotificationManager()
    var status = "Not registered"
    override init() {
        super.init()
        UNUserNotificationCenter.current().delegate = self
    }
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter,
        willPresent notification: UNNotification) async -> UNNotificationPresentationOptions {
        [.banner, .sound]
    }
    func requestPermission() async throws {
        let allowed = try await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge])
        guard allowed else { status = "Notifications are disabled in system settings"; return }
        #if os(macOS)
        NSApplication.shared.registerForRemoteNotifications()
        #else
        UIApplication.shared.registerForRemoteNotifications()
        #endif
        status = "Registering with Apple…"
    }
    func received(token: Data) {
        let value = token.map { String(format: "%02x", $0) }.joined()
        Task {
            do { try await RelayClient().registerDevice(value); status = "Registered with your relay" }
            catch { status = "Registration failed: \(error.localizedDescription). Tap Enable to retry." }
        }
    }
}
