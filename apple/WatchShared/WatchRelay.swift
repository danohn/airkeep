import Foundation
import Observation

struct WidgetRegistrationConflict: Error, Sendable {
    var generation: Int?
    var deleted = false
}

struct WatchRelay: Sendable {
    var connection: WatchConnection
    func fetch(_ resource: WatchFetchRequest.Resource, ams: String = "0", hours: Int = 1) async throws -> Data {
        let client = connection.client
        switch resource {
        case .snapshot: return try await client.request("v1/snapshot")
        case .alerts: return try await client.request("v2/alerts")
        case .history: return try JSONEncoder().encode(await client.history(amsID: ams, hours: hours))
        }
    }
    func register(_ request: WidgetRegistrationRequest, installationID: String) async throws -> WidgetRegistrationStatus {
        let (data, response) = try await connection.client.response("v2/widgets/\(installationID)", method: "PUT", body: JSONEncoder().encode(request))
        if response.statusCode == 409 {
            struct Problem: Decodable {
                struct Detail: Decodable { var code: String; var generation: Int; var deleted: Bool? }
                var detail: Detail
            }
            let problem = try? JSONDecoder().decode(Problem.self, from: data)
            throw WidgetRegistrationConflict(generation: problem?.detail.generation, deleted: problem?.detail.deleted ?? false)
        }
        guard (200..<300).contains(response.statusCode) else { throw RelayClient.RelayError.http(response.statusCode) }
        return try JSONDecoder().decode(WidgetRegistrationStatus.self, from: data)
    }
    func status(installationID: String) async throws -> WidgetRegistrationStatus {
        try JSONDecoder().decode(WidgetRegistrationStatus.self, from: await connection.client.request("v2/widgets/\(installationID)"))
    }
}

actor WatchWidgetRegistration {
    struct Dependencies: Sendable {
        var send: @Sendable (WatchConnection, String, WidgetRegistrationRequest) async throws -> WidgetRegistrationStatus
        var status: @Sendable (WatchConnection, String) async throws -> WidgetRegistrationStatus
        static var live: Self {
            .init(send: { try await WatchRelay(connection: $0).register($2, installationID: $1) },
                  status: { try await WatchRelay(connection: $0).status(installationID: $1) })
        }
    }
    static let live = WatchWidgetRegistration(storage: .live)
    let storage: WatchStorage
    private let dependencies: Dependencies
    private var sending = false
    init(storage: WatchStorage, dependencies: Dependencies = .live) {
        self.storage = storage; self.dependencies = dependencies
    }

    func sync() async {
        guard !sending else { return }
        sending = true
        defer { sending = false }
        if let state = try? storage.read(), let connection = try? storage.connection(),
           let record = state.registrations[connection.endpoint], record.confirmed != nil,
           Date.now.timeIntervalSince(record.checkedAt ?? .distantPast) >= 60 {
            do {
                let current = try await dependencies.status(connection, state.installationID)
                if current.generation != record.request.generation {
                    try storage.fail(endpoint: connection.endpoint, request: record.request,
                        error: "Registration changed. Tap Retry to reconnect.", conflict: true)
                } else { try storage.confirm(endpoint: connection.endpoint, request: record.request, status: current) }
            } catch RelayClient.RelayError.http(404) {
                try? storage.reconcileMissing(endpoint: connection.endpoint, request: record.request)
            } catch {
                try? storage.fail(endpoint: connection.endpoint, request: record.request, error: error.localizedDescription)
            }
        }
        // A token/configuration callback may replace a request while it is in
        // flight. Re-read shared storage after each pass and acknowledge only its generation.
        for _ in 0..<4 {
            guard let state = try? storage.read() else { return }
            var sent = false
            for (endpoint, record) in state.registrations where record.confirmed == nil && record.nextRetry <= .now {
                guard let connection = try? storage.connection(endpoint: endpoint) else { continue }
                sent = true
                do {
                    let result = try await dependencies.send(connection, state.installationID, record.request)
                    try storage.confirm(endpoint: endpoint, request: record.request, status: result)
                } catch {
                    let widgetConflict = error as? WidgetRegistrationConflict
                    let conflict = widgetConflict != nil || (error as? RelayClient.RelayError) == .http(409)
                    if record.request.amsIDs.isEmpty, let widgetConflict, let generation = widgetConflict.generation {
                        try? storage.resolveSuspension(endpoint: endpoint, request: record.request,
                            serverGeneration: generation, deleted: widgetConflict.deleted)
                        continue
                    }
                    let message = conflict ? "Registration changed. Tap Retry to reconnect." : error.localizedDescription
                    try? storage.fail(endpoint: endpoint, request: record.request, error: message, conflict: conflict, serverGeneration: widgetConflict?.generation)
                }
            }
            if !sent { return }
        }
    }
    func retry() async {
        guard let state = try? storage.read(), let connection = try? storage.connection() else { return }
        let status = try? await dependencies.status(connection, state.installationID)
        // Retry is an explicit user action; ordinary refreshes never advance a
        // conflicting request past a server-side deletion tombstone.
        try? storage.forceRegistration(after: status?.generation)
        await sync()
    }
}

@MainActor @Observable final class WatchMonitorModel {
    struct Dependencies {
        var fetch: @MainActor (WatchConnection, WatchFetchRequest.Resource, String, Int) async throws -> Data
        var fallback: @MainActor (WatchConnection, WatchFetchRequest.Resource, String, Int) async throws -> Data
        var syncWidgets: @MainActor () async -> Void = { await WatchWidgetRegistration.live.sync() }
        static var live: Self {
            .init(fetch: { try await WatchRelay(connection: $0).fetch($1, ams: $2, hours: $3) },
                  fallback: { _, _, _, _ in throw RelayClient.RelayError.transport })
        }
    }
    var reading: Reading? { snapshot?.readings.first { $0.amsID == selectedAMS } }
    var snapshot: Snapshot?
    var rules: [ManagedAlertRule] = []
    var history: [Reading] = []
    var hours = 1
    var selectedAMS = "0"
    var refreshing = false
    var historyRefreshing = false
    var error: String?
    var historyError: String?
    var rulesSyncedAt: Date?
    var connected = false
    var historyVisible = false
    var registration: StoredWidgetRegistration?
    var storageError: String?
    var demo: Bool
    private(set) var isActive = false
    @ObservationIgnored private let storage: WatchStorage
    @ObservationIgnored private let dependencies: Dependencies
    @ObservationIgnored private var connection: WatchConnection?
    @ObservationIgnored private var pollTask: Task<Void, Never>?
    @ObservationIgnored private var requestID = UUID()
    @ObservationIgnored private var historyRequestID = UUID()
    @ObservationIgnored private var lastHistory = Date.distantPast

    init(storage: WatchStorage = .live, dependencies: Dependencies = .live,
         demo: Bool = ProcessInfo.processInfo.arguments.contains("--demo")) {
        self.storage = storage; self.dependencies = dependencies; self.demo = demo
        if demo {
            var reading = PreviewData.reading
            reading.humidity = 19; reading.temperature = 24.4
            snapshot = Snapshot(serverTime: Date.now.timeIntervalSince1970, staleAfter: 90, printerConnected: true,
                pushConfigured: true, readings: [reading], alertRule: .init(), alertActive: false)
            rules = [.init(name: "Alert 1", enabled: true, threshold: 25), .init(name: "Alert 2", enabled: true, threshold: 35)]
            history = PreviewData.history.map { var reading = $0; reading.humidity -= 5; reading.temperature = 24.4; return reading }
        } else { reloadCache() }
    }
    var isStale: Bool { reading?.isStale(after: snapshot?.staleAfter ?? 90) ?? true }
    var enabledCount: Int { rules.filter(\.enabled).count }
    var hasConnection: Bool { demo || connection?.isConfigured == true }
    var amsIDs: [String] { Array(Set((snapshot?.readings.map(\.amsID) ?? []) + [selectedAMS])).sorted() }

    func reloadCache() {
        guard !demo else { return }
        do {
            let state = try storage.read()
            let latest = try storage.connection()
            if latest != connection {
                requestID = UUID(); historyRequestID = UUID()
                refreshing = false; historyRefreshing = false
                connected = false; snapshot = nil; rules = []; history = []; registration = nil; rulesSyncedAt = nil; lastHistory = .distantPast
                error = nil; historyError = nil
            }
            connection = latest
            selectedAMS = state.selectedAMS
            guard let endpoint = latest?.endpoint else { return }
            snapshot = state.snapshot[endpoint]?.snapshot
            rules = state.rules[endpoint]?.rules ?? []
            rulesSyncedAt = state.rules[endpoint]?.syncedAt
            history = state.history[WatchStorage.historyKey(endpoint: endpoint, ams: selectedAMS, hours: hours)]?.readings ?? []
            registration = state.registrations[endpoint]
            storageError = nil
        } catch { storageError = "Open Airkeep on iPhone to reconnect." }
    }
    func setActive(_ active: Bool) {
        isActive = active
        pollTask?.cancel(); pollTask = nil
        if !active {
            requestID = UUID(); historyRequestID = UUID()
            refreshing = false; historyRefreshing = false
        }
        guard active, !demo else { return }
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                await self.refresh()
                do { try await Task.sleep(for: .seconds(15)) } catch { return }
            }
        }
    }
    func chooseAMS(_ ams: String) {
        selectedAMS = ams
        if !demo { try? storage.selectAMS(ams) }
        historyRequestID = UUID(); historyRefreshing = false; lastHistory = .distantPast
        reloadCache()
        if historyVisible { Task { await refreshHistory(force: true) } }
    }
    private func fetch(_ resource: WatchFetchRequest.Resource, connection: WatchConnection, ams: String, hours: Int) async throws -> Data {
        do { return try await dependencies.fetch(connection, resource, ams, hours) }
        catch {
            try Task.checkCancellation()
            return try await dependencies.fallback(connection, resource, ams, hours)
        }
    }
    func refresh() async {
        guard !demo else { return }
        reloadCache()
        guard !refreshing, let current = connection else { return }
        let id = UUID(); requestID = id
        refreshing = true
        defer { if id == requestID { refreshing = false } }
        do {
            let value = try JSONDecoder().decode(Snapshot.self, from: await fetch(.snapshot, connection: current, ams: selectedAMS, hours: hours))
            guard id == requestID, current == (try? storage.connection()) else { return }
            try storage.cache(value, endpoint: current.endpoint)
            snapshot = (try storage.read()).snapshot[current.endpoint]?.snapshot
            connected = true; error = nil
            do {
                let loaded = try JSONDecoder().decode([ManagedAlertRule].self, from: await fetch(.alerts, connection: current, ams: selectedAMS, hours: hours))
                guard id == requestID, current == (try? storage.connection()) else { return }
                try storage.cache(rules: loaded, endpoint: current.endpoint)
                rules = loaded; rulesSyncedAt = .now
            } catch { /* Keep the last shared rule list available while offline. */ }
            if historyVisible { await refreshHistory() }
        } catch {
            guard id == requestID, current == (try? storage.connection()), !Task.isCancelled else { return }
            connected = false; self.error = "Couldn't refresh. Try again."
        }
        await dependencies.syncWidgets()
        reloadCache()
    }
    func refreshHistory(force: Bool = false) async {
        guard !demo, !historyRefreshing, let current = connection,
              force || Date.now.timeIntervalSince(lastHistory) >= 60 else { return }
        let ams = selectedAMS; let range = hours; let id = UUID(); historyRequestID = id
        historyRefreshing = true
        defer { if id == historyRequestID { historyRefreshing = false } }
        do {
            let loaded = try JSONDecoder().decode([Reading].self, from: await fetch(.history, connection: current, ams: ams, hours: range))
            guard id == historyRequestID, current == (try? storage.connection()), ams == selectedAMS, range == hours else { return }
            try storage.cache(history: loaded, endpoint: current.endpoint, ams: ams, hours: range)
            history = loaded; lastHistory = .now; historyError = nil
        } catch {
            guard id == historyRequestID, current == (try? storage.connection()), !Task.isCancelled else { return }
            historyError = "Couldn't refresh history."
        }
    }
}
