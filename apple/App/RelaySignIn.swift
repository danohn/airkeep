import AuthenticationServices
import CryptoKit
import os
import SwiftUI

#if os(macOS)
import AppKit
#else
import UIKit
#endif

@MainActor
final class RelaySignIn: NSObject, ASWebAuthenticationPresentationContextProviding {
    private var session: ASWebAuthenticationSession?
    private var anchor: ASPresentationAnchor?

    func signIn(endpoint: String, installation: String) async throws -> RelayTokens {
        #if os(macOS)
        anchor = NSApp.keyWindow ?? NSApp.windows.first
        #else
        anchor = UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }
            .flatMap(\.windows).first(where: \.isKeyWindow)
        #endif
        guard anchor != nil else { throw RelayClient.RelayError.transport }
        defer { session = nil; anchor = nil }
        let origin = try RelayOAuth.origin(endpoint)
        let verifier = try random()
        let state = try random()
        let challenge = base64URL(Data(SHA256.hash(data: Data(verifier.utf8))))
        #if os(macOS)
        let client = "apple-mac"
        #else
        let client = "apple-ios"
        #endif
        guard var connect = URLComponents(string: origin + "/auth/authorize") else { throw RelayClient.RelayError.configuration }
        connect.queryItems = [
            URLQueryItem(name: "client_id", value: client),
            URLQueryItem(name: "redirect_uri", value: RelayOAuth.callback),
            URLQueryItem(name: "state", value: state),
            URLQueryItem(name: "code_challenge", value: challenge),
            URLQueryItem(name: "code_challenge_method", value: "S256"),
            URLQueryItem(name: "response_type", value: "code"),
            URLQueryItem(name: "installation_id", value: installation),
        ]
        guard let url = connect.url else { throw RelayClient.RelayError.configuration }
        let callbackURL: URL = try await withCheckedThrowingContinuation { continuation in
            let pending = OSAllocatedUnfairLock(initialState: Optional(continuation))
            // Safari can call this on its XPC queue, including when startup fails.
            let completion: @Sendable (URL?, (any Error)?) -> Void = { url, error in
                let waiting = pending.withLock { continuation in
                    defer { continuation = nil }
                    return continuation
                }
                if let url { waiting?.resume(returning: url) }
                else { waiting?.resume(throwing: error ?? RelayClient.RelayError.transport) }
            }
            let signIn = ASWebAuthenticationSession(url: url, callback: .customScheme("io.airkeep.oauth"),
                                                    completionHandler: completion)
            signIn.presentationContextProvider = self
            signIn.prefersEphemeralWebBrowserSession = true
            session = signIn
            if !signIn.start() { completion(nil, RelayClient.RelayError.transport) }
        }
        guard let result = URLComponents(url: callbackURL, resolvingAgainstBaseURL: false),
              result.scheme == "io.airkeep.oauth", result.host == nil, result.path == "/callback",
              result.queryItems?.filter({ $0.name == "state" }).map(\.value) == [state],
              let codes = result.queryItems?.filter({ $0.name == "code" }), codes.count == 1,
              let code = codes.first?.value else { throw RelayClient.RelayError.transport }
        let tokens = try await RelayOAuth.token(endpoint: origin, clientID: client, fields: [
            "grant_type": "authorization_code", "code": code, "code_verifier": verifier, "redirect_uri": RelayOAuth.callback,
        ])
        try RelayOAuth.save(tokens, endpoint: origin, clientID: client)
        return tokens
    }

    func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor {
        guard let anchor else { preconditionFailure("Sign-in requires a presentation window.") }
        return anchor
    }

    private func random() throws -> String {
        var bytes = [UInt8](repeating: 0, count: 32)
        guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else {
            throw RelayClient.RelayError.transport
        }
        return base64URL(Data(bytes))
    }

    private func base64URL(_ data: Data) -> String {
        data.base64EncodedString().replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
    }
}
