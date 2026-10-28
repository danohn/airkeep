import Foundation
import Security
import LocalAuthentication

enum SharedConfiguration {
    static var group: String {
        guard let value = Bundle.main.object(forInfoDictionaryKey: "AirkeepAppGroup") as? String,
              !value.isEmpty, !value.contains("$(") else { return "group.io.airkeep" }
        return value
    }
    static var defaults: UserDefaults { UserDefaults(suiteName: group) ?? .standard }
    static var endpoint: String {
        get { defaults.string(forKey: "relayURL") ?? "" }
        set { defaults.set(newValue, forKey: "relayURL") }
    }
    static var amsID: String {
        get { defaults.string(forKey: "amsID") ?? "0" }
        set { defaults.set(newValue, forKey: "amsID") }
    }
    static var environment: String {
        #if DEBUG
        "sandbox"
        #else
        "production"
        #endif
    }
    static var deviceID: String {
        if let id = defaults.string(forKey: "deviceID") { return id }
        let id = UUID().uuidString
        defaults.set(id, forKey: "deviceID")
        return id
    }
    static func cachedRules(endpoint: String) -> AlertRulesCache? {
        guard let data = defaults.data(forKey: "alertRules.\(endpoint)") else { return nil }
        return try? JSONDecoder().decode(AlertRulesCache.self, from: data)
    }
    static func cacheRules(_ cache: AlertRulesCache, endpoint: String) {
        if let data = try? JSONEncoder().encode(cache) { defaults.set(data, forKey: "alertRules.\(endpoint)") }
    }
    static func receiveAlertsIntent(endpoint: String) -> Bool? {
        defaults.object(forKey: "receiveAlerts.\(endpoint)") as? Bool
    }
    static func saveReceiveAlertsIntent(_ enabled: Bool, endpoint: String) {
        defaults.set(enabled, forKey: "receiveAlerts.\(endpoint)")
    }
    static var cached: Reading? {
        get {
            guard let data = defaults.data(forKey: "cachedReading") else { return nil }
            return try? JSONDecoder().decode(Reading.self, from: data)
        }
        set {
            defaults.set(try? JSONEncoder().encode(newValue), forKey: "cachedReading")
        }
    }
}

enum TokenStore {
    private static var query: [String: Any] {
        var query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                   kSecAttrService as String: "Airkeep relay",
                                   kSecAttrAccount as String: "bearer-token"]
        #if os(macOS)
        // Use the data-protection Keychain so signed access groups control sharing.
        // The login Keychain's per-binary ACLs repeatedly prompted after rebuilds.
        query[kSecUseDataProtectionKeychain as String] = true
        #endif
        // Signing expands the team prefix in Info.plist.
        if let group = Bundle.main.object(forInfoDictionaryKey: "AirkeepKeychainGroup") as? String,
           !group.contains("$("), !group.isEmpty {
            query[kSecAttrAccessGroup as String] = group
        }
        return query
    }
    static func read() -> String {
        var query = query
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne
        // A background read must not open an authentication prompt.
        let context = LAContext()
        context.interactionNotAllowed = true
        query[kSecUseAuthenticationContext as String] = context
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data else { return "" }
        return String(data: data, encoding: .utf8) ?? ""
    }
    static func save(_ value: String) throws {
        let data = Data(value.utf8)
        let update = SecItemUpdate(query as CFDictionary, [kSecValueData as String: data] as CFDictionary)
        if update == errSecSuccess { return }
        guard update == errSecItemNotFound else { throw StorageError.keychain(update) }
        var query = query
        query[kSecValueData as String] = data
        query[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
        let result = SecItemAdd(query as CFDictionary, nil)
        guard result == errSecSuccess else { throw StorageError.keychain(result) }
    }
    enum StorageError: LocalizedError {
        case keychain(OSStatus)
        var errorDescription: String? { "Could not save relay token to the shared Keychain. Check signing and app group capabilities." }
    }
}
