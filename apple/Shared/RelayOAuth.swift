import Darwin
import Foundation

struct RelayTokens: Codable, Sendable {
    let access_token: String
    let refresh_token: String
    let grant_id: String
    let expires_in: Int
}

private struct RelayGrant: Codable {
    let endpoint: String
    let clientID: String
    let tokens: RelayTokens
    let expiresAt: Date
}

private final class NoRelayRedirects: NSObject, URLSessionTaskDelegate {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest, completionHandler: @escaping @Sendable (URLRequest?) -> Void) {
        completionHandler(nil)
    }
}

enum RelayOAuth {
    static let callback = "io.airkeep.oauth:/callback"
    private static let session: URLSession = {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.httpShouldSetCookies = false
        configuration.httpCookieStorage = nil
        return URLSession(configuration: configuration, delegate: NoRelayRedirects(), delegateQueue: nil)
    }()

    static func send(_ request: URLRequest) async throws -> (Data, URLResponse) {
        try await session.data(for: request)
    }

    static func origin(_ endpoint: String) throws -> String {
        guard var url = URLComponents(string: endpoint), url.scheme == "https",
              let host = url.host, !host.isEmpty, url.user == nil, url.password == nil,
              url.query == nil, url.fragment == nil, url.path.isEmpty || url.path == "/" else {
            throw RelayClient.RelayError.configuration
        }
        url.host = host.lowercased()
        url.path = ""
        if url.port == 443 { url.port = nil }
        guard let value = url.string else { throw RelayClient.RelayError.configuration }
        return value
    }

    static func token(endpoint: String, clientID: String, fields: [String: String]) async throws -> RelayTokens {
        guard let url = URL(string: try origin(endpoint) + "/auth/token") else { throw RelayClient.RelayError.configuration }
        var request = URLRequest(url: url, timeoutInterval: 15)
        request.httpMethod = "POST"
        request.setValue("application/x-www-form-urlencoded", forHTTPHeaderField: "Content-Type")
        var form = URLComponents()
        form.queryItems = (fields.merging(["client_id": clientID]) { _, fixed in fixed }).map(URLQueryItem.init(name:value:))
        request.httpBody = form.percentEncodedQuery?.replacingOccurrences(of: "+", with: "%2B").data(using: .utf8)
        let (data, response) = try await send(request)
        guard let http = response as? HTTPURLResponse else { throw RelayClient.RelayError.transport }
        guard http.statusCode == 200 else {
            throw RelayClient.RelayError.http(http.statusCode == 400 ? 401 : http.statusCode)
        }
        return try JSONDecoder().decode(RelayTokens.self, from: data)
    }

    static func marker(_ grantID: String) -> String { "oauth:" + grantID }

    static func save(_ tokens: RelayTokens, endpoint: String, clientID: String) throws {
        let grant = RelayGrant(endpoint: try origin(endpoint), clientID: clientID, tokens: tokens,
                               expiresAt: Date().addingTimeInterval(TimeInterval(tokens.expires_in)))
        let encoded = try JSONEncoder().encode(grant)
        try TokenStore.save(String(decoding: encoded, as: UTF8.self), account: marker(tokens.grant_id))
    }

    static func access(endpoint: String, marker: String) async throws -> String {
        let origin = try origin(endpoint)
        guard let directory = FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: SharedConfiguration.group) else {
            throw RelayClient.RelayError.configuration
        }
        let descriptor = open(directory.appendingPathComponent("relay-oauth.lock").path, O_CREAT | O_RDWR, S_IRUSR | S_IWUSR)
        guard descriptor >= 0 else { throw RelayClient.RelayError.transport }
        defer { close(descriptor) }
        while flock(descriptor, LOCK_EX | LOCK_NB) != 0 {
            guard errno == EWOULDBLOCK || errno == EINTR else { throw RelayClient.RelayError.transport }
            try await Task.sleep(for: .milliseconds(40))
        }
        defer { flock(descriptor, LOCK_UN) }

        let data = Data(TokenStore.read(account: marker).utf8)
        guard let grant = try? JSONDecoder().decode(RelayGrant.self, from: data),
              grant.endpoint == origin, marker == self.marker(grant.tokens.grant_id) else {
            throw RelayClient.RelayError.http(401)
        }
        if grant.expiresAt.timeIntervalSinceNow > 30 { return grant.tokens.access_token }
        let tokens = try await token(endpoint: origin, clientID: grant.clientID,
                                     fields: ["grant_type": "refresh_token", "refresh_token": grant.tokens.refresh_token])
        guard tokens.grant_id == grant.tokens.grant_id else { throw RelayClient.RelayError.transport }
        try save(tokens, endpoint: origin, clientID: grant.clientID)
        return tokens.access_token
    }
}
