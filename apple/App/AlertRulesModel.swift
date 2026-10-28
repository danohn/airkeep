import Foundation
import Observation

@MainActor @Observable final class AlertRulesModel {
    struct Connection: Equatable, Sendable {
        var endpoint: String
        var token: String
        var isConfigured: Bool { !endpoint.isEmpty && !token.isEmpty }
        var client: RelayClient { RelayClient(endpoint: endpoint, token: token) }
    }
    struct Dependencies {
        var connection: @MainActor () -> Connection
        var load: @MainActor (Connection) async throws -> [ManagedAlertRule]
        var save: @MainActor (Connection, ManagedAlertRule, Bool) async throws -> ManagedAlertRule
        var delete: @MainActor (Connection, ManagedAlertRule) async throws -> Void
        var read: @MainActor (Connection, String) async throws -> ManagedAlertRule
        var cached: @MainActor (String) -> AlertRulesCache? = { _ in nil }
        var cache: @MainActor (String, AlertRulesCache) -> Void = { _, _ in }

        static var live: Self {
            .init(connection: { .init(endpoint: SharedConfiguration.endpoint, token: TokenStore.read()) },
                  load: { try await $0.client.alertRules() },
                  save: { try await $0.client.saveRule($1, creating: $2) },
                  delete: { try await $0.client.deleteRule($1) },
                  read: { try await $0.client.alertRule(id: $1) },
                  cached: { SharedConfiguration.cachedRules(endpoint: $0) },
                  cache: { SharedConfiguration.cacheRules($1, endpoint: $0) })
        }
    }

    var rules: [ManagedAlertRule] = []
    var lastSynced: Date?
    var isRefreshing = false
    var isOnline = false
    var requiresUpdate = false
    var error: String?
    var busyIDs: Set<String> = []
    @ObservationIgnored private let dependencies: Dependencies
    @ObservationIgnored private var connection: Connection
    @ObservationIgnored private var generation = UUID()
    var canEdit: Bool { isOnline && !requiresUpdate && connection == dependencies.connection() }
    var nextName: String {
        var index = 1
        while rules.contains(where: { $0.name == "Alert \(index)" }) { index += 1 }
        return "Alert \(index)"
    }

    init(dependencies: Dependencies = .live) {
        self.dependencies = dependencies
        connection = dependencies.connection()
        restoreCache()
    }
    private func restoreCache() {
        let cached = dependencies.cached(connection.endpoint)
        rules = cached?.rules ?? []
        lastSynced = cached?.syncedAt
    }
    private func cache() {
        guard let lastSynced else { return }
        dependencies.cache(connection.endpoint, .init(rules: rules, syncedAt: lastSynced))
    }
    func refresh() async {
        let current = dependencies.connection()
        if current != connection {
            connection = current
            isOnline = false
            requiresUpdate = false
            restoreCache()
        } else if isRefreshing { return }
        let id = UUID()
        generation = id
        guard current.isConfigured else {
            isOnline = false
            isRefreshing = false
            error = "Connect to your relay to load alert rules."
            return
        }
        isRefreshing = true
        defer { if generation == id { isRefreshing = false } }
        do {
            let loaded = try await dependencies.load(current)
            guard generation == id && current == dependencies.connection() else { return }
            rules = loaded
            lastSynced = .now
            isOnline = true
            requiresUpdate = false
            error = nil
            cache()
        } catch {
            guard generation == id && current == dependencies.connection() else { return }
            isOnline = false
            requiresUpdate = (error as? RelayClient.RelayError) == .http(404)
            self.error = requiresUpdate ? "Update your relay to manage named humidity alerts." : error.localizedDescription
        }
    }
    func save(_ draft: ManagedAlertRule, creating: Bool = false) async throws -> ManagedAlertRule {
        guard canEdit else { throw RelayClient.RelayError.offlineRules }
        guard !busyIDs.contains(draft.id) else { throw RelayClient.RelayError.transport }
        let current = connection
        busyIDs.insert(draft.id)
        defer { busyIDs.remove(draft.id) }
        let saved = try await dependencies.save(current, draft, creating)
        guard current == dependencies.connection() else { throw RelayClient.RelayError.connectionChanged }
        generation = UUID()
        isRefreshing = false
        if let index = rules.firstIndex(where: { $0.id == saved.id }) { rules[index] = saved }
        else { rules.append(saved) }
        cache()
        await refresh()
        return saved
    }
    func delete(_ rule: ManagedAlertRule) async throws {
        guard canEdit else { throw RelayClient.RelayError.offlineRules }
        let current = connection
        busyIDs.insert(rule.id)
        defer { busyIDs.remove(rule.id) }
        try await dependencies.delete(current, rule)
        guard current == dependencies.connection() else { throw RelayClient.RelayError.connectionChanged }
        generation = UUID()
        isRefreshing = false
        rules.removeAll { $0.id == rule.id }
        cache()
        await refresh()
    }
    func reload(_ id: String) async throws -> ManagedAlertRule {
        let current = dependencies.connection()
        let loaded = try await dependencies.read(current, id)
        guard current == dependencies.connection() else { throw RelayClient.RelayError.connectionChanged }
        return loaded
    }
}
