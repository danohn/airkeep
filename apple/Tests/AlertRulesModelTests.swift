import Foundation
import Testing

@MainActor private final class RulesFixture {
    var connection = AlertRulesModel.Connection(endpoint: "https://relay.example.com", token: "test-token")
    var remote = [ManagedAlertRule(id: "rule-one", name: "Alert 1", revision: 1)]
    var failure: Error?
    var saves = 0
    var cache: [String: AlertRulesCache] = [:]
    var heldEndpoint: String?
    var pending: CheckedContinuation<[ManagedAlertRule], Error>?

    func model() -> AlertRulesModel {
        .init(dependencies: .init(connection: { self.connection }, load: { connection in
            if connection.endpoint == self.heldEndpoint {
                return try await withCheckedThrowingContinuation { self.pending = $0 }
            }
            if let failure = self.failure { throw failure }
            return self.remote
        }, save: { _, draft, creating in
            self.saves += 1
            if let failure = self.failure { throw failure }
            var saved = draft
            if creating {
                saved.id = UUID().uuidString
                saved.revision = 1
                self.remote.append(saved)
            } else {
                guard let index = self.remote.firstIndex(where: { $0.id == draft.id }) else { throw RelayClient.RelayError.http(404) }
                guard self.remote[index].revision == draft.revision else { throw RelayClient.RelayError.http(412) }
                saved.revision += 1
                self.remote[index] = saved
            }
            return saved
        }, delete: { _, draft in
            guard self.remote.first(where: { $0.id == draft.id })?.revision == draft.revision else { throw RelayClient.RelayError.http(412) }
            self.remote.removeAll { $0.id == draft.id }
        }, read: { _, id in
            guard let rule = self.remote.first(where: { $0.id == id }) else { throw RelayClient.RelayError.http(404) }
            return rule
        }, cached: { self.cache[$0] }, cache: { self.cache[$0] = $1 }))
    }
}

@Suite @MainActor struct AlertRulesModelTests {
    @Test func cachedRulesAreReadOnlyUntilTheRelayResponds() async {
        let fixture = RulesFixture()
        fixture.cache[fixture.connection.endpoint] = .init(rules: fixture.remote, syncedAt: .now)
        fixture.failure = RelayClient.RelayError.http(503)
        let model = fixture.model()
        #expect(model.rules.count == 1)
        #expect(!model.canEdit)
        await model.refresh()
        do { _ = try await model.save(model.rules[0]); Issue.record("Offline save was sent") }
        catch { #expect((error as? RelayClient.RelayError) == .offlineRules) }
        #expect(fixture.saves == 0)
        #expect(model.rules == fixture.remote)
        fixture.failure = nil
        await model.refresh()
        #expect(model.canEdit)
    }

    @Test func failedSavesAndConflictsPreserveDrafts() async {
        let fixture = RulesFixture()
        let model = fixture.model()
        await model.refresh()
        var draft = model.rules[0]
        draft.name = "Phone draft"
        fixture.failure = RelayClient.RelayError.http(503)
        do { _ = try await model.save(draft); Issue.record("Failed save was accepted") } catch {}
        #expect(draft.name == "Phone draft")
        #expect(model.rules[0].name == "Alert 1")
        fixture.failure = nil
        fixture.remote[0].name = "Mac edit"
        fixture.remote[0].revision = 2
        await model.refresh()
        do { _ = try await model.save(draft); Issue.record("A stale draft overwrote the relay") }
        catch { #expect((error as? RelayClient.RelayError) == .http(412)) }
        #expect(draft.revision == 1)
        #expect(model.rules[0].name == "Mac edit")
        let latest = try? await model.reload(draft.id)
        #expect(latest?.revision == 2)
    }

    @Test func createsAndDeletesAppearOnAnotherDeviceAfterRefresh() async throws {
        let fixture = RulesFixture()
        let phone = fixture.model(), mac = fixture.model()
        await phone.refresh(); await mac.refresh()
        let created = try await phone.save(.init(name: phone.nextName), creating: true)
        #expect(created.name == "Alert 2" && !created.enabled)
        await mac.refresh()
        #expect(mac.rules.contains(where: { $0.id == created.id }))
        try await mac.delete(created)
        await phone.refresh()
        #expect(!phone.rules.contains(where: { $0.id == created.id }))
    }

    @Test func switchingRelaysClearsCachedRulesAndIgnoresOldResponses() async {
        let fixture = RulesFixture()
        let model = fixture.model()
        fixture.heldEndpoint = fixture.connection.endpoint
        let old = Task { await model.refresh() }
        while fixture.pending == nil { await Task.yield() }
        fixture.connection.endpoint = "https://other.example.com"
        fixture.remote = [.init(id: "other", name: "Other relay", revision: 1)]
        await model.refresh()
        fixture.pending?.resume(returning: [.init(id: "old", name: "Stale response", revision: 1)])
        await old.value
        #expect(model.rules.first?.name == "Other relay")
        #expect(model.canEdit)
    }
}
