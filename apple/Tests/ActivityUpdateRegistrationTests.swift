import Foundation
import Testing

@MainActor private final class ActivityRegistrationFixture {
    var connection = ActivityUpdateRegistration.Connection(endpoint: "https://relay.example.com", token: "test-token")
    var pending: CheckedContinuation<Void, Error>?
    var calls = 0
    func manager() -> ActivityUpdateRegistration {
        .init(dependencies: .init(connection: { self.connection }, send: { _, _ in
            self.calls += 1
            try await withCheckedThrowingContinuation { self.pending = $0 }
        }, retryDelay: .seconds(60)))
    }
    var request: ActivityUpdateRegistration.Request { .init(id: "session", token: "test-token", amsID: "0", expiresAt: Date.now.addingTimeInterval(3600)) }
}

@Suite @MainActor struct ActivityUpdateRegistrationTests {
    @Test func pendingFailureAndRetryRemainVisibleUntilAcknowledged() async {
        let fixture = ActivityRegistrationFixture()
        let manager = fixture.manager()
        #expect(manager.state == .waitingForToken)
        manager.submit(fixture.request)
        while fixture.pending == nil { await Task.yield() }
        #expect(manager.state == .registering)
        fixture.pending?.resume(throwing: RelayClient.RelayError.http(503))
        while manager.state == .registering { await Task.yield() }
        if case .failed = manager.state {} else { Issue.record("Failed registration was treated as ready") }
        fixture.pending = nil
        manager.retry()
        while fixture.pending == nil { await Task.yield() }
        fixture.pending?.resume()
        while manager.state == .registering { await Task.yield() }
        #expect(manager.state == .registered)
        #expect(fixture.calls == 2)
        manager.stop()
    }

    @Test func stoppedSessionsAndOldRelayRepliesCannotMarkRegistrationReady() async {
        let fixture = ActivityRegistrationFixture()
        let manager = fixture.manager()
        manager.submit(fixture.request)
        while fixture.pending == nil { await Task.yield() }
        let old = fixture.pending
        fixture.pending = nil
        fixture.connection.endpoint = "https://other.example.com"
        manager.reconcile()
        while fixture.pending == nil { await Task.yield() }
        old?.resume()
        manager.stop()
        fixture.pending?.resume()
        await Task.yield()
        #expect(manager.state == .waitingForToken)
    }
}
