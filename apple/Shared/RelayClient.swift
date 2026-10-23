import Foundation

struct RelayClient: Sendable {
    var endpoint: String
    var token: String
    init(endpoint: String = SharedConfiguration.endpoint, token: String = TokenStore.read()) {
        self.endpoint = endpoint
        self.token = token
    }
    func request(_ path: String, method: String = "GET", body: Data? = nil) async throws -> Data {
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
        request.cachePolicy = .reloadIgnoringLocalCacheData
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw RelayError.transport }
        guard (200..<300).contains(http.statusCode) else { throw RelayError.http(http.statusCode) }
        return data
    }
    func snapshot() async throws -> Snapshot { try JSONDecoder().decode(Snapshot.self, from: await request("v1/snapshot")) }
    func history(amsID: String, hours: Int) async throws -> [Reading] {
        let allowed = CharacterSet.urlQueryAllowed.subtracting(CharacterSet(charactersIn: "&=?+#"))
        let query = amsID.addingPercentEncoding(withAllowedCharacters: allowed) ?? "0"
        return try JSONDecoder().decode([Reading].self, from: await request("v1/history?ams=\(query)&hours=\(hours)"))
    }
    func saveRule(_ rule: AlertRule) async throws {
        _ = try await request("v1/alerts", method: "PUT", body: JSONEncoder().encode(rule))
    }
    func registerDevice(_ token: String) async throws {
        #if os(macOS)
        let platform = "mac"
        #else
        let platform = "ios"
        #endif
        _ = try await request("v1/devices", method: "POST", body: JSONSerialization.data(withJSONObject:
            ["id": SharedConfiguration.deviceID, "token": token, "platform": platform, "environment": SharedConfiguration.environment]))
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
    enum RelayError: LocalizedError {
        case configuration, transport, http(Int)
        var errorDescription: String? {
            switch self {
            case .configuration: "Add your relay URL and token in Connection settings."
            case .transport: "Could not read the relay response."
            case .http(401): "The relay rejected this token. Check it in Connection settings."
            case .http(let code): "The relay returned HTTP \(code)."
            }
        }
    }
}
