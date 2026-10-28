import Foundation
import CryptoKit
import Security
import Darwin
import LocalAuthentication

// WatchConnectivity's Objective-C reply blocks predate Sendable annotations.
// Wrap the callback when moving it to the main actor; its payload is encoded Data.
final class WatchCallback<Value>: @unchecked Sendable {
    private let callback: (Value) -> Void
    init(_ callback: @escaping (Value) -> Void) { self.callback = callback }
    func call(_ value: Value) { callback(value) }
}

struct WatchConnection: Codable, Equatable, Sendable {
    var endpoint: String
    var token: String
    var linkID: String
    var isConfigured: Bool {
        guard let url = URLComponents(string: endpoint) else { return false }
        return url.scheme == "https" && url.host != nil && url.user == nil && url.password == nil && !token.isEmpty
    }
    var client: RelayClient { RelayClient(endpoint: endpoint, token: token) }
}

struct WatchConfigurationMessage: Codable, Sendable {
    var senderID = "phone"
    var revision: Int
    var connection: WatchConnection?
    var amsID: String
}

struct WatchConfigurationReceipt: Codable, Sendable {
    var senderID = "phone"
    var revision: Int
    var connected: Bool
    var error: String?
}

struct WatchFetchRequest: Codable, Sendable {
    enum Resource: String, Codable, Sendable { case snapshot, history, alerts }
    var resource: Resource
    var linkID: String
    var amsID = "0"
    var hours = 1
}

struct WatchFetchReply: Codable, Sendable {
    var data: Data?
    var error: String?
}

enum WatchHistoryPacket {
    // Keep phone fallback messages small while retaining the extrema in each
    // interval and the original endpoints/timestamps of the selected range.
    static func compact(_ readings: [Reading], limit: Int = 240) -> [Reading] {
        guard readings.count > limit, limit >= 4 else { return readings }
        let buckets = (limit - 2) / 2
        var indices: Set<Int> = [0, readings.count - 1]
        for bucket in 0..<buckets {
            let lower = 1 + bucket * (readings.count - 2) / buckets
            let upper = 1 + (bucket + 1) * (readings.count - 2) / buckets
            guard lower < upper else { continue }
            let range = lower..<upper
            if let minimum = range.min(by: { readings[$0].humidity < readings[$1].humidity }) { indices.insert(minimum) }
            if let maximum = range.max(by: { readings[$0].humidity < readings[$1].humidity }) { indices.insert(maximum) }
        }
        return indices.sorted().map { readings[$0] }
    }
}

struct WidgetRegistrationRequest: Codable, Equatable, Sendable {
    var token: String
    var platform = "watch"
    var environment: String
    var bundleID: String
    var amsIDs: [String]
    var generation: Int
}

struct WidgetRegistrationStatus: Codable, Equatable, Sendable {
    var registered: Bool
    var active: Bool
    var generation: Int
    var topic: String
    var configuration: String
    var reason: String
}

struct StoredWidgetRegistration: Codable, Sendable {
    var request: WidgetRegistrationRequest
    var confirmed: WidgetRegistrationStatus?
    var error: String?
    var nextRetry: Date = .distantPast
    var failures = 0
    var checkedAt: Date?
}

struct WatchHistoryCache: Codable, Sendable {
    var readings: [Reading]
    var fetchedAt: Date
}

struct WatchSnapshotCache: Codable, Sendable {
    var snapshot: Snapshot
    var fetchedAt: Date
}

/// Shared by the Watch app and its widget extension. A file lock makes generation
/// allocation and pending registration updates atomic across both processes.
final class WatchStorage: @unchecked Sendable {
    struct Credentials: Sendable {
        var read: @Sendable (String) throws -> WatchConnection?
        var save: @Sendable (WatchConnection) throws -> Void
        var remove: @Sendable (String) throws -> Void
        static var keychain: Self {
            .init(read: { try WatchKeychain.read(endpoint: $0) }, save: { try WatchKeychain.save($0) },
                  remove: { try WatchKeychain.remove(endpoint: $0) })
        }
    }
    struct State: Codable {
        var installationID = UUID().uuidString
        var endpoint: String?
        var selectedAMS = "0"
        var phoneRevision = 0
        var phoneSenderID: String?
        var snapshot: [String: WatchSnapshotCache] = [:]
        var rules: [String: AlertRulesCache] = [:]
        var history: [String: WatchHistoryCache] = [:]
        var registrations: [String: StoredWidgetRegistration] = [:]
        var generations: [String: Int] = [:]
        var widgetToken: String?
        var widgetAMS: [String] = []
        var widgetEnvironment = "sandbox"
        var widgetBundleID = "io.airkeep.watch"
    }
    static let live: WatchStorage = {
        let manager = FileManager.default
        let directory = manager.containerURL(forSecurityApplicationGroupIdentifier: SharedConfiguration.group)
            ?? manager.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0].appendingPathComponent("AirkeepWatch")
        return WatchStorage(directory: directory)
    }()

    private let directory: URL
    private let credentials: Credentials
    private let processLock = NSLock()
    init(directory: URL, credentials: Credentials = .keychain) {
        self.directory = directory; self.credentials = credentials
    }

    @discardableResult private func transaction<T>(persist: Bool = true, _ update: (inout State) throws -> T) throws -> T {
        processLock.lock()
        defer { processLock.unlock() }
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let descriptor = open(directory.appendingPathComponent("watch-state.lock").path, O_CREAT | O_RDWR, S_IRUSR | S_IWUSR)
        guard descriptor >= 0 else { throw CocoaError(.fileWriteUnknown) }
        defer { close(descriptor) }
        guard flock(descriptor, LOCK_EX) == 0 else { throw CocoaError(.fileWriteUnknown) }
        defer { flock(descriptor, LOCK_UN) }
        let url = directory.appendingPathComponent("watch-state.json")
        var state: State
        let exists = FileManager.default.fileExists(atPath: url.path)
        if exists {
            state = try JSONDecoder().decode(State.self, from: Data(contentsOf: url))
        } else { state = State() }
        let result = try update(&state)
        if persist || !exists { try JSONEncoder().encode(state).write(to: url, options: .atomic) }
        #if os(iOS) || os(watchOS)
        try FileManager.default.setAttributes([.protectionKey: FileProtectionType.completeUntilFirstUserAuthentication], ofItemAtPath: url.path)
        #endif
        return result
    }

    func read() throws -> State { try transaction(persist: false) { $0 } }
    func connection() throws -> WatchConnection? {
        guard let endpoint = try read().endpoint else { return nil }
        return try credentials.read(endpoint)
    }
    func connection(endpoint: String) throws -> WatchConnection? { try credentials.read(endpoint) }

    @discardableResult func apply(_ message: WatchConfigurationMessage) throws -> WatchConfigurationReceipt {
        if let connection = message.connection, !connection.isConfigured { throw RelayClient.RelayError.configuration }
        return try transaction { state in
            guard state.phoneSenderID != message.senderID || message.revision > state.phoneRevision else {
                return WatchConfigurationReceipt(senderID: state.phoneSenderID ?? message.senderID,
                    revision: state.phoneRevision, connected: state.endpoint != nil, error: nil)
            }
            if let connection = message.connection {
                if let old = state.endpoint, old != connection.endpoint { suspend(endpoint: old, state: &state) }
                try credentials.save(connection)
                if state.endpoint != connection.endpoint { state.selectedAMS = message.amsID }
                state.endpoint = connection.endpoint
                updateRegistration(state: &state, force: false)
            } else {
                if let endpoint = state.endpoint { suspend(endpoint: endpoint, state: &state) }
                state.endpoint = nil
            }
            state.phoneRevision = message.revision
            state.phoneSenderID = message.senderID
            return WatchConfigurationReceipt(senderID: message.senderID, revision: message.revision, connected: state.endpoint != nil, error: nil)
        }
    }

    private func suspend(endpoint: String, state: inout State) {
        guard var registration = state.registrations[endpoint] else {
            try? credentials.remove(endpoint)
            return
        }
        let generation = (state.generations[endpoint] ?? 0) + 1
        state.generations[endpoint] = generation
        registration.request.generation = generation
        registration.request.amsIDs = []
        registration.confirmed = nil; registration.error = nil; registration.failures = 0; registration.nextRetry = .distantPast
        state.registrations[endpoint] = registration
    }
    private func updateRegistration(state: inout State, force: Bool) {
        guard let endpoint = state.endpoint, let token = state.widgetToken else { return }
        let old = state.registrations[endpoint]
        var request = WidgetRegistrationRequest(token: token, environment: state.widgetEnvironment,
            bundleID: state.widgetBundleID, amsIDs: state.widgetAMS.sorted(), generation: old?.request.generation ?? 0)
        guard force || request != old?.request else { return }
        let next = (state.generations[endpoint] ?? 0) + 1
        state.generations[endpoint] = next; request.generation = next
        state.registrations[endpoint] = .init(request: request)
    }
    func configureWidgets(token: String, amsIDs: [String], environment: String, bundleID: String) throws {
        try transaction { state in
            state.widgetToken = token
            state.widgetAMS = Array(Set(amsIDs)).sorted()
            state.widgetEnvironment = environment; state.widgetBundleID = bundleID
            updateRegistration(state: &state, force: false)
        }
    }
    func forceRegistration(after serverGeneration: Int? = nil) throws {
        try transaction { state in
            guard let endpoint = state.endpoint else { return }
            if let serverGeneration { state.generations[endpoint] = max(state.generations[endpoint] ?? 0, serverGeneration) }
            updateRegistration(state: &state, force: true)
        }
    }
    func confirm(endpoint: String, request: WidgetRegistrationRequest, status: WidgetRegistrationStatus) throws {
        try transaction { state in
            guard state.registrations[endpoint]?.request == request else { return }
            guard status.registered, status.generation == request.generation, status.active == !request.amsIDs.isEmpty,
                  status.topic == request.bundleID + ".push-type.widgets" else { throw RelayClient.RelayError.transport }
            state.registrations[endpoint]?.confirmed = status
            state.registrations[endpoint]?.error = nil
            state.registrations[endpoint]?.failures = 0
            state.registrations[endpoint]?.checkedAt = .now
            if state.endpoint != endpoint && request.amsIDs.isEmpty { try credentials.remove(endpoint) }
        }
    }
    func reconcileMissing(endpoint: String, request: WidgetRegistrationRequest) throws {
        try transaction { state in
            guard state.registrations[endpoint]?.request == request else { return }
            state.registrations[endpoint]?.confirmed = nil
            state.registrations[endpoint]?.nextRetry = .distantPast
        }
    }
    func resolveSuspension(endpoint: String, request: WidgetRegistrationRequest, serverGeneration: Int, deleted: Bool) throws {
        try transaction { state in
            guard request.amsIDs.isEmpty, state.registrations[endpoint]?.request == request else { return }
            state.generations[endpoint] = max(state.generations[endpoint] ?? 0, serverGeneration)
            if deleted {
                state.registrations.removeValue(forKey: endpoint)
                if state.endpoint != endpoint { try credentials.remove(endpoint) }
            } else {
                var newer = request
                newer.generation = (state.generations[endpoint] ?? 0) + 1
                state.generations[endpoint] = newer.generation
                state.registrations[endpoint] = .init(request: newer)
            }
        }
    }
    func fail(endpoint: String, request: WidgetRegistrationRequest, error: String, conflict: Bool = false, serverGeneration: Int? = nil) throws {
        try transaction { state in
            guard var current = state.registrations[endpoint], current.request == request else { return }
            current.error = error; current.confirmed = nil; current.failures += 1
            if let serverGeneration { state.generations[endpoint] = max(state.generations[endpoint] ?? 0, serverGeneration) }
            current.nextRetry = conflict ? .distantFuture : Date.now.addingTimeInterval(min(900, 15 * pow(2, Double(min(6, current.failures - 1)))))
            state.registrations[endpoint] = current
        }
    }
    func selectAMS(_ amsID: String) throws { try transaction { $0.selectedAMS = amsID } }
    func cache(_ snapshot: Snapshot, endpoint: String) throws {
        try transaction { state in
            if let previous = state.snapshot[endpoint], previous.snapshot.serverTime > snapshot.serverTime { return }
            state.snapshot[endpoint] = .init(snapshot: snapshot, fetchedAt: .now)
        }
    }
    func cache(rules: [ManagedAlertRule], endpoint: String) throws {
        try transaction { $0.rules[endpoint] = .init(rules: rules, syncedAt: .now) }
    }
    static func historyKey(endpoint: String, ams: String, hours: Int) -> String { "\(endpoint)|\(ams)|\(hours)" }
    func cache(history: [Reading], endpoint: String, ams: String, hours: Int) throws {
        try transaction { $0.history[Self.historyKey(endpoint: endpoint, ams: ams, hours: hours)] = .init(readings: history, fetchedAt: .now) }
    }
}

private enum WatchKeychain {
    private static func query(_ endpoint: String) -> [String: Any] {
        var result: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: "Airkeep Watch relay",
            kSecAttrAccount as String: SHA256.hash(data: Data(endpoint.utf8)).map { String(format: "%02x", $0) }.joined()]
        if let group = Bundle.main.object(forInfoDictionaryKey: "AirkeepKeychainGroup") as? String, !group.contains("$(") {
            result[kSecAttrAccessGroup as String] = group
        }
        #if os(macOS)
        result[kSecUseDataProtectionKeychain as String] = true
        #endif
        return result
    }
    static func read(endpoint: String) throws -> WatchConnection? {
        var request = query(endpoint)
        request[kSecReturnData as String] = true; request[kSecMatchLimit as String] = kSecMatchLimitOne
        let context = LAContext(); context.interactionNotAllowed = true
        request[kSecUseAuthenticationContext as String] = context
        var item: CFTypeRef?
        let status = SecItemCopyMatching(request as CFDictionary, &item)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess, let data = item as? Data else { throw TokenStore.StorageError.keychain(status) }
        return try JSONDecoder().decode(WatchConnection.self, from: data)
    }
    static func save(_ connection: WatchConnection) throws {
        let data = try JSONEncoder().encode(connection)
        let request = query(connection.endpoint)
        let status = SecItemUpdate(request as CFDictionary, [kSecValueData as String: data] as CFDictionary)
        if status == errSecSuccess { return }
        guard status == errSecItemNotFound else { throw TokenStore.StorageError.keychain(status) }
        var item = request; item[kSecValueData as String] = data
        item[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let inserted = SecItemAdd(item as CFDictionary, nil)
        guard inserted == errSecSuccess else { throw TokenStore.StorageError.keychain(inserted) }
    }
    static func remove(endpoint: String) throws {
        let status = SecItemDelete(query(endpoint) as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else { throw TokenStore.StorageError.keychain(status) }
    }
}
