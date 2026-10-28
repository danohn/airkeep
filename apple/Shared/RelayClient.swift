import Foundation

struct RelayClient: Sendable {
    var endpoint: String
    var token: String
    init(endpoint: String = SharedConfiguration.endpoint, token: String = TokenStore.read()) {
        self.endpoint = endpoint
        self.token = token
    }
    func request(_ path: String, method: String = "GET", body: Data? = nil, revision: Int? = nil) async throws -> Data {
        let (data, http) = try await response(path, method: method, body: body, revision: revision)
        guard (200..<300).contains(http.statusCode) else { throw RelayError.http(http.statusCode) }
        return data
    }
    func response(_ path: String, method: String = "GET", body: Data? = nil, revision: Int? = nil) async throws -> (Data, HTTPURLResponse) {
        guard var components = URLComponents(string: endpoint),
              ["http", "https"].contains(components.scheme ?? ""), components.host != nil,
              components.user == nil, components.password == nil,
              let base = components.url else { throw RelayError.configuration }
        let parts = path.split(separator: "?", maxSplits: 1)
        components = URLComponents(url: base.appendingPathComponent(String(parts[0])), resolvingAgainstBaseURL: false)!
        if parts.count == 2 { components.percentEncodedQuery = String(parts[1]) }
        guard let url = components.url else { throw RelayError.configuration }
        var request = URLRequest(url: url, timeoutInterval: 10)
        request.httpMethod = method
        request.httpBody = body
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let revision { request.setValue("\"\(revision)\"", forHTTPHeaderField: "If-Match") }
        request.cachePolicy = .reloadIgnoringLocalCacheData
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw RelayError.transport }
        return (data, http)
    }
    func snapshot() async throws -> Snapshot { try JSONDecoder().decode(Snapshot.self, from: await request("v1/snapshot")) }
    func history(amsID: String, hours: Int) async throws -> [Reading] {
        let allowed = CharacterSet.urlQueryAllowed.subtracting(CharacterSet(charactersIn: "&=?+#"))
        let query = amsID.addingPercentEncoding(withAllowedCharacters: allowed) ?? "0"
        return try JSONDecoder().decode([Reading].self, from: await request("v1/history?ams=\(query)&hours=\(hours)"))
    }
    func alertRules() async throws -> [ManagedAlertRule] {
        try JSONDecoder().decode([ManagedAlertRule].self, from: await request("v2/alerts"))
    }
    func alertRule(id: String) async throws -> ManagedAlertRule {
        try JSONDecoder().decode(ManagedAlertRule.self, from: await request("v2/alerts/\(id)"))
    }
    func saveRule(_ rule: ManagedAlertRule, creating: Bool) async throws -> ManagedAlertRule {
        try JSONDecoder().decode(ManagedAlertRule.self,
            from: await request(creating ? "v2/alerts" : "v2/alerts/\(rule.id)", method: creating ? "POST" : "PUT",
                                body: JSONEncoder().encode(rule), revision: creating ? nil : rule.revision))
    }
    func deleteRule(_ rule: ManagedAlertRule) async throws {
        _ = try await request("v2/alerts/\(rule.id)", method: "DELETE", revision: rule.revision)
    }
    func registerDevice(_ token: String) async throws {
        #if os(macOS)
        let platform = "mac"
        #else
        let platform = "ios"
        #endif
        _ = try await request("v2/devices/\(SharedConfiguration.deviceID)", method: "PUT", body: JSONSerialization.data(withJSONObject:
            ["token": token, "platform": platform, "environment": SharedConfiguration.environment,
             "initialReceiveAlerts": SharedConfiguration.receiveAlertsIntent(endpoint: endpoint) ?? false]))
    }
    func setReceiveAlerts(_ enabled: Bool) async throws -> Bool {
        struct Subscription: Decodable { var receiveAlerts: Bool }
        return try JSONDecoder().decode(Subscription.self,
            from: await request("v2/devices/\(SharedConfiguration.deviceID)", method: "PATCH",
                                body: JSONSerialization.data(withJSONObject: ["receiveAlerts": enabled]))).receiveAlerts
    }
    func notificationStatus() async throws -> NotificationRelayStatus {
        #if os(macOS)
        let platform = "mac"
        #else
        let platform = "ios"
        #endif
        var query = URLComponents()
        query.queryItems = [URLQueryItem(name: "device", value: SharedConfiguration.deviceID),
                           URLQueryItem(name: "platform", value: platform),
                           URLQueryItem(name: "environment", value: SharedConfiguration.environment),
                           URLQueryItem(name: "bundle_id", value: Bundle.main.bundleIdentifier ?? "")]
        return try JSONDecoder().decode(NotificationRelayStatus.self,
            from: await request("v1/notifications/status?\(query.percentEncodedQuery ?? "")"))
    }
    func registerActivity(id: String, token: String, amsID: String, expiresAt: Double) async throws {
        _ = try await request("v1/activities", method: "POST", body: JSONSerialization.data(withJSONObject:
            ["id": id, "token": token, "amsID": amsID, "expiresAt": expiresAt, "environment": SharedConfiguration.environment]))
    }
    enum RelayError: LocalizedError, Equatable {
        case configuration, transport, connectionChanged, offlineRules, http(Int)
        var errorDescription: String? {
            switch self {
            case .configuration: "Add your relay URL and token in Settings → Relay."
            case .transport: "Could not read the relay response."
            case .connectionChanged: "The relay connection changed. Reload before making changes."
            case .offlineRules: "Connect to your relay before saving alert changes."
            case .http(401): "The relay rejected this token. Check it in Settings → Relay."
            case .http(412): "This alert changed on another device. Reload it before making changes."
            case .http(404): "This item is no longer available, or the relay needs an update."
            case .http(409): "Update Airkeep to manage named alert rules."
            case .http(let code): "The relay returned HTTP \(code)."
            }
        }
    }
}
