import AppIntents
import WidgetKit

struct WatchHumidityEntry: TimelineEntry {
    var date: Date
    var amsID = "0"
    var reading: Reading?
    var staleAfter = 90.0
    var preview = false
    var amsName: String { "AMS \((Int(amsID) ?? 0) + 1)" }
    var widgetURL: URL? {
        var components = URLComponents()
        components.scheme = "airkeep"; components.host = "watch"
        components.queryItems = [URLQueryItem(name: "ams", value: amsID)]
        return components.url
    }
    static func cached(amsID: String?, state: WatchStorage.State?, now: Date = .now) -> Self {
        let selected = amsID ?? state?.selectedAMS ?? "0"
        guard let state, let endpoint = state.endpoint, let cached = state.snapshot[endpoint] else {
            return .init(date: now, amsID: selected)
        }
        return .init(date: now, amsID: selected,
            reading: cached.snapshot.readings.first { $0.amsID == selected }, staleAfter: cached.snapshot.staleAfter)
    }
}

struct WatchAMSEntity: AppEntity, Sendable {
    static let typeDisplayRepresentation = TypeDisplayRepresentation(name: "AMS")
    static let defaultQuery = WatchAMSQuery()
    var id: String
    var displayRepresentation: DisplayRepresentation { DisplayRepresentation(title: "AMS \((Int(id) ?? 0) + 1)") }
}

struct WatchAMSQuery: EntityQuery {
    func entities(for identifiers: [String]) async throws -> [WatchAMSEntity] { identifiers.map { .init(id: $0) } }
    func suggestedEntities() async throws -> [WatchAMSEntity] {
        let state = try WatchStorage.live.read()
        let identifiers = state.endpoint.flatMap { state.snapshot[$0]?.snapshot.readings.map(\.amsID) } ?? [state.selectedAMS]
        return Array(Set(identifiers)).sorted().map { .init(id: $0) }
    }
    func defaultResult() async -> WatchAMSEntity? {
        .init(id: (try? WatchStorage.live.read().selectedAMS) ?? "0")
    }
}

struct WatchAMSIntent: WidgetConfigurationIntent {
    static let title: LocalizedStringResource = "AMS selection"
    static let description = IntentDescription("Choose an AMS for this complication.")
    @Parameter(title: "AMS") var ams: WatchAMSEntity?
}

enum WatchWidgetSetup {
    static let kind = "AirkeepWatchHumidity"
    static var bundleID: String {
        Bundle.main.object(forInfoDictionaryKey: "AirkeepWatchAppBundleID") as? String ?? "io.airkeep.watch"
    }
    static func amsIDs(from widgets: [WidgetInfo], fallback: String) -> [String] {
        Array(Set(widgets.filter { $0.kind == kind }.map {
            $0.widgetConfigurationIntent(of: WatchAMSIntent.self)?.ams?.id ?? fallback
        })).sorted()
    }
    static func reconcile() async {
        guard let state = try? WatchStorage.live.read(), let token = state.widgetToken,
              let widgets = try? await WidgetCenter.shared.currentConfigurations() else { return }
        try? WatchStorage.live.configureWidgets(token: token, amsIDs: amsIDs(from: widgets, fallback: state.selectedAMS),
            environment: SharedConfiguration.environment, bundleID: bundleID)
    }
}
