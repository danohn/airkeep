import Foundation
import Observation
import UserNotifications
#if os(macOS)
import AppKit
#else
import UIKit
#endif

@MainActor @Observable final class NotificationManager: NSObject, UNUserNotificationCenterDelegate {
    enum Authorization: Equatable { case checking, notAsked, denied, allowed, quiet }
    struct Permission: Equatable {
        var authorization: Authorization
        var banners = false
        var sounds = false
        var title: String {
            switch authorization {
            case .checking: "Checking…"
            case .notAsked: "Not enabled"
            case .denied: "Off in Settings"
            case .quiet: "Quiet delivery"
            case .allowed: banners ? "Allowed" : "Allowed · banners off"
            }
        }
        var allowsNotifications: Bool { authorization == .allowed || authorization == .quiet }
    }
    enum Registration: Equatable {
        case unchecked, needsConnection, notRegistered, registeringApple, registeringRelay, registered, failed(String)
        var title: String {
            switch self {
            case .unchecked: "Not checked"
            case .needsConnection: "Connect to a relay"
            case .notRegistered: "Not registered"
            case .registeringApple: "Registering with Apple…"
            case .registeringRelay: "Registering with relay…"
            case .registered: "Registered"
            case .failed: "Needs attention"
            }
        }
    }
    enum Server: Equatable {
        case unchecked, checking, available, needsSetup(String), unavailable(String), updateRequired
        var title: String {
            switch self {
            case .unchecked: "Not checked"
            case .checking: "Checking…"
            case .available: "Configuration found"
            case .needsSetup: "Needs attention"
            case .unavailable: "Could not check"
            case .updateRequired: "Relay update needed"
            }
        }
        var detail: String {
            switch self {
            case .unchecked: "Connect to your relay to check its push setup."
            case .checking: "Checking the relay for this app and push environment."
            case .available: "The relay has a readable signing key for this app's push environment. Apple authorization and notification delivery have not been verified."
            case .needsSetup(let message), .unavailable(let message): message
            case .updateRequired: "This relay doesn't support notification status checks yet. Update the relay to check its push setup."
            }
        }
    }
    struct Connection: Equatable, Sendable {
        var endpoint: String
        var token: String
        var isConfigured: Bool { !endpoint.isEmpty && !token.isEmpty }
    }
    struct Dependencies {
        var permission: @MainActor () async -> Permission
        var requestAuthorization: @MainActor () async throws -> Void
        var registerWithApple: @MainActor () -> Void
        var connection: @MainActor () -> Connection
        var status: @MainActor (Connection) async throws -> NotificationRelayStatus
        var register: @MainActor (Connection, String) async throws -> Void
        var registrationTimeout: Duration = .seconds(20)

        static var live: Self {
            .init(permission: {
                let settings = await UNUserNotificationCenter.current().notificationSettings()
                let authorization: Authorization
                switch settings.authorizationStatus {
                case .notDetermined: authorization = .notAsked
                case .denied: authorization = .denied
                case .authorized: authorization = .allowed
                case .provisional: authorization = .quiet
                #if os(iOS)
                case .ephemeral: authorization = .quiet
                #endif
                @unknown default: authorization = .denied
                }
                return Permission(authorization: authorization,
                                  banners: settings.alertSetting == .enabled,
                                  sounds: settings.soundSetting == .enabled)
            }, requestAuthorization: {
                _ = try await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge])
            }, registerWithApple: {
                #if os(macOS)
                NSApplication.shared.registerForRemoteNotifications()
                #else
                UIApplication.shared.registerForRemoteNotifications()
                #endif
            }, connection: {
                Connection(endpoint: SharedConfiguration.endpoint, token: TokenStore.read())
            }, status: { connection in
                try await RelayClient(endpoint: connection.endpoint, token: connection.token).notificationStatus()
            }, register: { connection, token in
                try await RelayClient(endpoint: connection.endpoint, token: connection.token).registerDevice(token)
            })
        }
    }

    static let shared: NotificationManager = {
        let manager = NotificationManager(dependencies: .live)
        UNUserNotificationCenter.current().delegate = manager
        return manager
    }()
    var permission = Permission(authorization: .checking)
    var registration: Registration = .unchecked
    var server: Server = .unchecked
    var isRefreshing = false
    var isRequestingPermission = false
    var permissionError: String?
    @ObservationIgnored private let dependencies: Dependencies
    @ObservationIgnored private var token: String?
    @ObservationIgnored private var confirmedConnection: Connection?
    @ObservationIgnored private var confirmedToken: String?
    @ObservationIgnored private var refreshTask: Task<Void, Never>?
    @ObservationIgnored private var appleTimeout: Task<Void, Never>?
    @ObservationIgnored private var generation = UUID()

    init(dependencies: Dependencies) {
        self.dependencies = dependencies
        super.init()
    }
    var canRetryRegistration: Bool {
        guard permission.allowsNotifications else { return false }
        switch registration {
        case .failed, .notRegistered: return true
        default: return false
        }
    }
    var registrationError: String? {
        if case .failed(let message) = registration { return message }
        return nil
    }
    var summary: String {
        switch permission.authorization {
        case .checking: return "Checking notification permission."
        case .notAsked: return "Enable notifications to receive humidity alerts on this device."
        case .denied: return "Allow Airkeep notifications in Settings to receive alerts on this device."
        case .allowed, .quiet: break
        }
        guard registration == .registered else { return "This device still needs a confirmed relay registration." }
        guard server == .available else { return "Device registration and server push setup are separate checks. See server details below." }
        return "Permission and registration are in place. Notification delivery has not been verified."
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter,
        willPresent notification: UNNotification) async -> UNNotificationPresentationOptions {
        [.banner, .sound]
    }
    func requestPermission() async {
        guard !isRequestingPermission else { return }
        isRequestingPermission = true
        permissionError = nil
        defer { isRequestingPermission = false }
        do {
            // Only an explicit button press may request permission.
            let current = await dependencies.permission()
            if current.authorization == .notAsked { try await dependencies.requestAuthorization() }
        } catch { permissionError = error.localizedDescription }
        await refresh()
    }
    func refresh(forceRegistration: Bool = false) async {
        refreshTask?.cancel()
        let id = UUID()
        generation = id
        let connection = dependencies.connection()
        isRefreshing = true
        if connection != confirmedConnection {
            registration = .unchecked
            confirmedConnection = nil
            confirmedToken = nil
        }
        server = .checking
        let task = Task { [weak self] in
            guard let self else { return }
            await self.reconcile(connection, id: id, forceRegistration: forceRegistration)
        }
        refreshTask = task
        await task.value
    }
    private func isCurrent(_ connection: Connection, id: UUID) -> Bool {
        !Task.isCancelled && generation == id && dependencies.connection() == connection
    }
    private func reconcile(_ connection: Connection, id: UUID, forceRegistration: Bool) async {
        defer { if generation == id { isRefreshing = false } }
        let currentPermission = await dependencies.permission()
        guard isCurrent(connection, id: id) else { return }
        permission = currentPermission
        if !permission.allowsNotifications {
            appleTimeout?.cancel()
            appleTimeout = nil
        }
        guard connection.isConfigured else {
            registration = .needsConnection
            server = .unchecked
            return
        }
        if permission.allowsNotifications && token == nil && appleTimeout == nil {
            startAppleRegistration()
        }
        do {
            var status: NotificationRelayStatus?
            do { status = try await dependencies.status(connection) }
            catch RelayClient.RelayError.http(404) { status = nil }
            guard isCurrent(connection, id: id) else { return }
            applyServer(status)
            if status?.reason == "topic_mismatch" {
                registration = .failed("The relay's push topic doesn't match this app. Correct the relay configuration before retrying.")
                return
            }
            guard permission.allowsNotifications else {
                registration = status?.registered == true ? .registered : .notRegistered
                return
            }
            guard let token else {
                // Preserve any registration failure when no Apple callback is pending.
                if appleTimeout != nil { registration = .registeringApple }
                return
            }
            if forceRegistration || status?.registered != true || confirmedConnection != connection || confirmedToken != token {
                registration = .registeringRelay
                try await dependencies.register(connection, token)
                guard isCurrent(connection, id: id) else { return }
                if status != nil {
                    status = try await dependencies.status(connection)
                    guard isCurrent(connection, id: id) else { return }
                    applyServer(status)
                    guard status?.registered == true else {
                        registration = .failed("The relay didn't retain this device's registration. Try registering again.")
                        return
                    }
                }
                confirmedConnection = connection
                confirmedToken = token
            }
            registration = .registered
        } catch {
            guard isCurrent(connection, id: id) else { return }
            server = .unavailable(error.localizedDescription)
            registration = .failed("Could not confirm registration with this relay. \(error.localizedDescription)")
        }
    }
    private func applyServer(_ status: NotificationRelayStatus?) {
        guard let status else { server = .updateRequired; return }
        switch status.configuration {
        case .available: server = .available
        case .missing: server = .needsSetup("The relay has no push signing credentials for this app's environment.")
        case .invalid:
            let message: String
            switch status.reason {
            case "topic_mismatch": message = "The relay's push topic doesn't match this app. Check its iOS or Mac topic configuration."
            case "key_unreadable": message = "The relay cannot read its push signing key. Check the key mount and file permissions."
            default: message = "The relay's push signing key is invalid. Check its APNs key configuration."
            }
            server = .needsSetup(message)
        }
    }
    private func startAppleRegistration() {
        registration = .registeringApple
        let timeout = dependencies.registrationTimeout
        appleTimeout = Task { [weak self] in
            do { try await Task.sleep(for: timeout) } catch { return }
            guard let self else { return }
            self.appleTimeout = nil
            if self.token == nil {
                self.registration = .failed("Apple registration didn't finish. Check your connection and retry registration.")
            }
        }
        dependencies.registerWithApple()
    }
    func received(token: Data) async {
        appleTimeout?.cancel()
        appleTimeout = nil
        self.token = token.map { String(format: "%02x", $0) }.joined()
        await refresh(forceRegistration: true)
    }
    func registrationFailed(_ error: Error) {
        appleTimeout?.cancel()
        appleTimeout = nil
        registration = .failed("Apple registration failed. \(error.localizedDescription)")
    }
    func retryRegistration() async {
        if token == nil {
            appleTimeout?.cancel()
            appleTimeout = nil
        }
        await refresh(forceRegistration: true)
    }
    func openSettings() {
        #if os(macOS)
        if let url = URL(string: "x-apple.systempreferences:com.apple.Notifications-Settings.extension") {
            NSWorkspace.shared.open(url)
        }
        #else
        if let url = URL(string: UIApplication.openNotificationSettingsURLString) {
            UIApplication.shared.open(url)
        }
        #endif
    }
}
