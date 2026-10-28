import Foundation
import Observation

@MainActor @Observable final class ActivityUpdateRegistration {
    enum State: Equatable {
        case waitingForToken, registering, registered, failed(String)
        var title: String {
            switch self {
            case .waitingForToken: "Waiting for Apple token…"
            case .registering: "Registering with relay…"
            case .registered: "Registration confirmed"
            case .failed: "Needs attention"
            }
        }
    }
    struct Connection: Equatable, Sendable { var endpoint: String; var token: String }
    struct Request: Sendable {
        var id: String
        var token: String
        var amsID: String
        var expiresAt: Date
    }
    struct Dependencies {
        var connection: @MainActor () -> Connection
        var send: @MainActor (Connection, Request) async throws -> Void
        var retryDelay: Duration = .seconds(15)
        static var live: Self {
            .init(connection: { .init(endpoint: SharedConfiguration.endpoint, token: TokenStore.read()) },
                  send: { connection, request in
                      try await RelayClient(endpoint: connection.endpoint, token: connection.token)
                          .registerActivity(id: request.id, token: request.token, amsID: request.amsID,
                                            expiresAt: request.expiresAt.timeIntervalSince1970)
                  })
        }
    }
    var state: State = .waitingForToken
    @ObservationIgnored private let dependencies: Dependencies
    @ObservationIgnored private var request: Request?
    @ObservationIgnored private var connection: Connection?
    @ObservationIgnored private var task: Task<Void, Never>?
    @ObservationIgnored private var generation = UUID()

    init(dependencies: Dependencies = .live) { self.dependencies = dependencies }
    func submit(_ request: Request) {
        if let previous = self.request, previous.id == request.id && previous.token == request.token &&
            previous.amsID == request.amsID && previous.expiresAt == request.expiresAt && connection == dependencies.connection() { return }
        stop()
        self.request = request
        begin()
    }
    func reconcile() {
        if request != nil && connection != dependencies.connection() { retry() }
    }
    func retry() {
        task?.cancel()
        begin()
    }
    private func begin() {
        guard let request, request.expiresAt > .now else { return }
        let current = dependencies.connection()
        connection = current
        let id = UUID()
        generation = id
        state = .registering
        task = Task { [weak self] in
            guard let self else { return }
            while !Task.isCancelled && Date.now < request.expiresAt {
                do {
                    try await self.dependencies.send(current, request)
                    guard !Task.isCancelled && self.generation == id && current == self.dependencies.connection() else { return }
                    self.state = .registered
                    return
                } catch {
                    guard !Task.isCancelled && self.generation == id && current == self.dependencies.connection() else { return }
                    self.state = .failed(error.localizedDescription)
                    do { try await Task.sleep(for: self.dependencies.retryDelay) } catch { return }
                    guard self.generation == id else { return }
                    self.state = .registering
                }
            }
        }
    }
    func stop() {
        generation = UUID()
        task?.cancel(); task = nil; request = nil; connection = nil
        state = .waitingForToken
    }
}
