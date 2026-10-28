import Foundation
import Testing

private final class WatchTestVault: @unchecked Sendable {
    private let lock = NSLock()
    private var values: [String: WatchConnection] = [:]
    private var failures: [String] = []
    var credentials: WatchStorage.Credentials {
        .init(read: { endpoint in self.lock.withLock { self.values[endpoint] } },
              save: { connection in self.lock.withLock { self.values[connection.endpoint] = connection } },
              remove: { endpoint in self.lock.withLock { _ = self.values.removeValue(forKey: endpoint) } })
    }
    func record(_ error: Error) { lock.withLock { failures.append(error.localizedDescription) } }
    var errors: [String] { lock.withLock { failures } }
}

private struct WatchStorageFixture {
    let directory = FileManager.default.temporaryDirectory.appendingPathComponent("airkeep-watch-tests-\(UUID().uuidString)")
    let vault = WatchTestVault()
    var connection: WatchConnection { .init(endpoint: "https://relay.example.com", token: "private-test-credential", linkID: "test-link") }
    var store: WatchStorage { .init(directory: directory, credentials: vault.credentials) }
    func connect(_ store: WatchStorage) throws { try store.apply(.init(revision: 1, connection: connection, amsID: "0")) }
    func cleanup() { try? FileManager.default.removeItem(at: directory) }
}

private func widgetStatus(_ request: WidgetRegistrationRequest) -> WidgetRegistrationStatus {
    .init(registered: true, active: !request.amsIDs.isEmpty, generation: request.generation,
          topic: request.bundleID + ".push-type.widgets", configuration: "available", reason: "local_checks_passed")
}

@Suite struct WatchStorageTests {
    @Test func configuredWidgetAMSRemainsInTheDeepLinkWithoutACachedReading() throws {
        var state = WatchStorage.State()
        state.selectedAMS = "0"
        for cache in [state, nil] {
            let entry = WatchHumidityEntry.cached(amsID: "1", state: cache)
            #expect(entry.reading == nil)
            #expect(entry.amsName == "AMS 2")
            let url = try #require(entry.widgetURL)
            #expect(URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.first?.value == "1")
        }
    }

    @Test func phoneHistoryRepliesKeepSpikesAndOriginalTimestampsWithinASmallPacket() throws {
        var readings = (0..<3000).map { Reading(amsID: "0", humidity: 20, observedAt: Double($0)) }
        readings[1500].humidity = 90; readings[1501].humidity = 5
        let compact = WatchHistoryPacket.compact(readings)
        #expect(compact.count <= 240)
        #expect(compact.first?.observedAt == 0)
        #expect(compact.last?.observedAt == 2999)
        #expect(compact.contains { $0.observedAt == 1500 && $0.humidity == 90 })
        #expect(compact.contains { $0.observedAt == 1501 && $0.humidity == 5 })
        let reply = try JSONEncoder().encode(WatchFetchReply(data: JSONEncoder().encode(compact)))
        #expect(reply.count < 60_000)
    }

    @Test func generationsPersistAcrossStoreInstancesAndConcurrentUpdates() throws {
        let fixture = WatchStorageFixture(); defer { fixture.cleanup() }
        let first = fixture.store
        try fixture.connect(first)
        try first.configureWidgets(token: "old-token", amsIDs: ["1", "0", "1"], environment: "sandbox", bundleID: "io.airkeep.watch")
        let old = try #require(first.read().registrations[fixture.connection.endpoint]?.request)
        let second = fixture.store
        try second.configureWidgets(token: "new-token", amsIDs: ["0", "1"], environment: "sandbox", bundleID: "io.airkeep.watch")
        try second.configureWidgets(token: "new-token", amsIDs: ["1", "0", "1"], environment: "sandbox", bundleID: "io.airkeep.watch")
        #expect(try first.read().registrations[fixture.connection.endpoint]?.request.generation == old.generation + 1)
        try first.confirm(endpoint: fixture.connection.endpoint, request: old, status: widgetStatus(old))
        #expect(try second.read().registrations[fixture.connection.endpoint]?.confirmed == nil)
        DispatchQueue.concurrentPerform(iterations: 12) { index in
            do { try fixture.store.configureWidgets(token: "rotation-\(index)", amsIDs: ["0"], environment: "sandbox", bundleID: "io.airkeep.watch") }
            catch { fixture.vault.record(error) }
        }
        #expect(fixture.vault.errors.isEmpty)
        #expect(try first.read().generations[fixture.connection.endpoint] == 14)
        let state = try String(contentsOf: fixture.directory.appendingPathComponent("watch-state.json"), encoding: .utf8)
        #expect(!state.contains(fixture.connection.token))
    }

    @Test func disconnectSuspendsBeforeRemovingCredentialsAndRejectsOldSetup() throws {
        let fixture = WatchStorageFixture(); defer { fixture.cleanup() }
        let store = fixture.store
        try fixture.connect(store)
        try store.configureWidgets(token: "widget-token", amsIDs: ["0"], environment: "sandbox", bundleID: "io.airkeep.watch")
        let active = try #require(store.read().registrations[fixture.connection.endpoint]?.request)
        try store.apply(.init(revision: 2, connection: nil, amsID: "0"))
        #expect(try store.connection() == nil)
        #expect(try store.connection(endpoint: fixture.connection.endpoint) != nil)
        let suspended = try #require(store.read().registrations[fixture.connection.endpoint]?.request)
        #expect(suspended.amsIDs.isEmpty)
        #expect(suspended.generation > active.generation)
        try store.apply(.init(revision: 1, connection: fixture.connection, amsID: "0"))
        #expect(try store.connection() == nil)
        try store.confirm(endpoint: fixture.connection.endpoint, request: suspended, status: widgetStatus(suspended))
        #expect(try store.connection(endpoint: fixture.connection.endpoint) == nil)
        let id = try store.read().installationID
        try store.apply(.init(revision: 3, connection: fixture.connection, amsID: "0"))
        #expect(try store.read().installationID == id)
        #expect(try store.read().registrations[fixture.connection.endpoint]!.request.generation > suspended.generation)
    }

    @Test func relayChangesKeepGenerationsAndCachesSeparate() throws {
        let fixture = WatchStorageFixture(); defer { fixture.cleanup() }
        let store = fixture.store
        try fixture.connect(store)
        try store.configureWidgets(token: "widget-token", amsIDs: ["1"], environment: "sandbox", bundleID: "io.airkeep.watch")
        var other = fixture.connection; other.endpoint = "https://another.example.com"; other.linkID = "other-link"
        try store.apply(.init(revision: 2, connection: other, amsID: "1"))
        let state = try store.read()
        #expect(state.registrations[fixture.connection.endpoint]?.request.amsIDs == [])
        #expect(state.registrations[other.endpoint]?.request.amsIDs == ["1"])
        #expect(state.registrations[other.endpoint]?.request.generation == 1)
        #expect(state.selectedAMS == "1")
        #expect(try store.connection() == other)
    }

    @Test func phoneReinstallCanEstablishANewSetupSequence() throws {
        let fixture = WatchStorageFixture(); defer { fixture.cleanup() }
        let store = fixture.store
        try store.apply(.init(senderID: "old-phone", revision: 100, connection: fixture.connection, amsID: "0"))
        let receipt = try store.apply(.init(senderID: "new-phone", revision: 1, connection: fixture.connection, amsID: "1"))
        #expect(receipt.senderID == "new-phone")
        #expect(receipt.revision == 1)
    }

    @Test func aDeletedSuspensionCanRemoveCredentialsWithoutRecreatingTheRegistration() throws {
        let fixture = WatchStorageFixture(); defer { fixture.cleanup() }
        let store = fixture.store; try fixture.connect(store)
        try store.configureWidgets(token: "widget", amsIDs: ["0"], environment: "sandbox", bundleID: "io.airkeep.watch")
        try store.apply(.init(revision: 2, connection: nil, amsID: "0"))
        let request = try #require(store.read().registrations[fixture.connection.endpoint]?.request)
        try store.resolveSuspension(endpoint: fixture.connection.endpoint, request: request, serverGeneration: 8, deleted: true)
        #expect(try store.connection(endpoint: fixture.connection.endpoint) == nil)
        #expect(try store.read().registrations[fixture.connection.endpoint] == nil)
        #expect(try store.read().generations[fixture.connection.endpoint] == 8)
    }

    @Test func oldSnapshotsCannotReplaceNewerSamplesOrRewriteTheirAge() throws {
        let fixture = WatchStorageFixture(); defer { fixture.cleanup() }
        let store = fixture.store
        let reading = Reading(amsID: "0", humidity: 19, observedAt: 100)
        let fresh = Snapshot(serverTime: 300, staleAfter: 90, printerConnected: true, pushConfigured: true,
                             readings: [reading], alertRule: .init(), alertActive: false)
        try store.cache(fresh, endpoint: fixture.connection.endpoint)
        var old = fresh; old.serverTime = 200; old.readings[0].humidity = 80
        try store.cache(old, endpoint: fixture.connection.endpoint)
        let result = try #require(store.read().snapshot[fixture.connection.endpoint]?.snapshot.readings.first)
        #expect(result.humidity == 19)
        #expect(result.observedAt == 100)
        #expect(result.isStale(after: 90, now: Date(timeIntervalSince1970: 400)))
    }
}

private actor WatchRegistrationFixture {
    var pending: [(WidgetRegistrationRequest, CheckedContinuation<WidgetRegistrationStatus, Error>)] = []
    var calls = 0
    var conflict = false
    func send(_ request: WidgetRegistrationRequest) async throws -> WidgetRegistrationStatus {
        calls += 1
        if conflict { throw WidgetRegistrationConflict(generation: 10) }
        return try await withCheckedThrowingContinuation { pending.append((request, $0)) }
    }
    func acknowledgeFirst() {
        let item = pending.removeFirst()
        item.1.resume(returning: widgetStatus(item.0))
    }
    func enableConflict(_ value: Bool) { conflict = value }
    var waiting: Int { pending.count }
}

@Suite struct WatchRegistrationTests {
    @Test func tokenRotationWaitsForTheLatestGenerationAcknowledgement() async throws {
        let fixture = WatchStorageFixture(); defer { fixture.cleanup() }
        let store = fixture.store; try fixture.connect(store)
        try store.configureWidgets(token: "first", amsIDs: ["0"], environment: "sandbox", bundleID: "io.airkeep.watch")
        let network = WatchRegistrationFixture()
        let manager = WatchWidgetRegistration(storage: store, dependencies: .init(send: { _, _, request in try await network.send(request) },
            status: { _, _ in throw RelayClient.RelayError.http(404) }))
        let task = Task { await manager.sync() }
        while await network.waiting == 0 { await Task.yield() }
        #expect(try store.read().registrations[fixture.connection.endpoint]?.confirmed == nil)
        try store.configureWidgets(token: "second", amsIDs: ["1"], environment: "sandbox", bundleID: "io.airkeep.watch")
        await network.acknowledgeFirst()
        while await network.waiting == 0 { await Task.yield() }
        #expect(try store.read().registrations[fixture.connection.endpoint]?.confirmed == nil)
        await network.acknowledgeFirst()
        await task.value
        let result = try #require(store.read().registrations[fixture.connection.endpoint])
        #expect(result.confirmed?.generation == 2)
        #expect(result.request.amsIDs == ["1"])
    }

    @Test func tombstoneConflictsStayFailedUntilExplicitRetry() async throws {
        let fixture = WatchStorageFixture(); defer { fixture.cleanup() }
        let store = fixture.store; try fixture.connect(store)
        try store.configureWidgets(token: "widget", amsIDs: ["0"], environment: "sandbox", bundleID: "io.airkeep.watch")
        let network = WatchRegistrationFixture(); await network.enableConflict(true)
        let manager = WatchWidgetRegistration(storage: store, dependencies: .init(send: { _, _, request in try await network.send(request) },
            status: { _, _ in throw RelayClient.RelayError.http(404) }))
        await manager.sync(); await manager.sync()
        #expect(await network.calls == 1)
        #expect(try store.read().registrations[fixture.connection.endpoint]?.confirmed == nil)
        #expect(try store.read().generations[fixture.connection.endpoint] == 10)
        await network.enableConflict(false)
        let task = Task { await manager.retry() }
        while await network.waiting == 0 { await Task.yield() }
        #expect(try store.read().registrations[fixture.connection.endpoint]?.request.generation == 11)
        await network.acknowledgeFirst(); await task.value
        #expect(try store.read().registrations[fixture.connection.endpoint]?.confirmed?.generation == 11)
    }
}

@MainActor private final class WatchModelFixture {
    var directFails = false
    var pending: CheckedContinuation<Data, Error>?
    var heldResource: WatchFetchRequest.Resource?
    var fallbackCalls = 0
    var snapshot: Snapshot {
        .init(serverTime: 500, staleAfter: 90, printerConnected: true, pushConfigured: true,
              readings: [.init(amsID: "0", humidity: 19, observedAt: 450), .init(amsID: "1", humidity: 28, observedAt: 450)],
              alertRule: .init(), alertActive: false)
    }
    func data(_ resource: WatchFetchRequest.Resource, ams: String) throws -> Data {
        switch resource {
        case .snapshot: return try JSONEncoder().encode(snapshot)
        case .alerts: return try JSONEncoder().encode([ManagedAlertRule(name: "Shared alert", enabled: true)])
        case .history: return try JSONEncoder().encode(snapshot.readings.filter { $0.amsID == ams })
        }
    }
    func model(_ storage: WatchStorage) -> WatchMonitorModel {
        WatchMonitorModel(storage: storage, dependencies: .init(fetch: { _, resource, ams, _ in
            if self.heldResource == resource { return try await withCheckedThrowingContinuation { self.pending = $0 } }
            if self.directFails { throw RelayClient.RelayError.transport }
            return try self.data(resource, ams: ams)
        }, fallback: { _, resource, ams, _ in self.fallbackCalls += 1; return try self.data(resource, ams: ams) }, syncWidgets: {}))
    }
}

@Suite @MainActor struct WatchMonitorTests {
    @Test func reachablePhoneFallbackPreservesSampleTimestampAndSharedRules() async throws {
        let storage = WatchStorageFixture(); defer { storage.cleanup() }
        let store = storage.store; try storage.connect(store)
        let fixture = WatchModelFixture(); fixture.directFails = true
        let model = fixture.model(store)
        await model.refresh()
        #expect(model.reading?.humidity == 19)
        #expect(model.reading?.observedAt == 450)
        #expect(model.rules.first?.name == "Shared alert")
        #expect(fixture.fallbackCalls == 2)
        #expect(model.isStale)
    }

    @Test func relayChangesIgnoreInFlightOldReplies() async throws {
        let storage = WatchStorageFixture(); defer { storage.cleanup() }
        let store = storage.store; try storage.connect(store)
        let fixture = WatchModelFixture(); fixture.heldResource = .snapshot
        let model = fixture.model(store)
        let task = Task { await model.refresh() }
        while fixture.pending == nil { await Task.yield() }
        var other = storage.connection; other.endpoint = "https://new.example.com"; other.linkID = "new-link"
        try store.apply(.init(revision: 2, connection: other, amsID: "1"))
        model.reloadCache()
        fixture.pending?.resume(returning: try fixture.data(.snapshot, ams: "0"))
        await task.value
        #expect(model.reading == nil)
        #expect(model.selectedAMS == "1")
        #expect(!model.refreshing)
    }

    @Test func ambientLifecycleStopsPollingAndDiscardsLateUpdates() async throws {
        let storage = WatchStorageFixture(); defer { storage.cleanup() }
        let store = storage.store; try storage.connect(store)
        let fixture = WatchModelFixture(); fixture.heldResource = .snapshot
        let model = fixture.model(store)
        model.setActive(true)
        while fixture.pending == nil { await Task.yield() }
        model.setActive(false)
        fixture.pending?.resume(returning: try fixture.data(.snapshot, ams: "0"))
        for _ in 0..<20 { await Task.yield() }
        #expect(!model.isActive)
        #expect(!model.refreshing)
        #expect(model.reading == nil)
    }

    @Test func historySelectionRejectsOldAMSResponses() async throws {
        let storage = WatchStorageFixture(); defer { storage.cleanup() }
        let store = storage.store; try storage.connect(store)
        let fixture = WatchModelFixture(); let model = fixture.model(store)
        await model.refresh()
        fixture.heldResource = .history
        let old = Task { await model.refreshHistory(force: true) }
        while fixture.pending == nil { await Task.yield() }
        model.chooseAMS("1")
        fixture.heldResource = nil
        await model.refreshHistory(force: true)
        fixture.pending?.resume(returning: try fixture.data(.history, ams: "0"))
        await old.value
        #expect(model.history.first?.amsID == "1")
        #expect(model.reading?.amsID == "1")
    }
}
