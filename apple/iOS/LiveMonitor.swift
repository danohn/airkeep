@preconcurrency import ActivityKit
import Foundation
import Observation

@MainActor @Observable final class LiveMonitor {
    static let shared = LiveMonitor()
    private let relevanceScore = 50.0
    private let authorization = ActivityAuthorizationInfo()
    let updateRegistration = ActivityUpdateRegistration()
    var activities: [Activity<MonitorAttributes>] = Activity<MonitorAttributes>.activities
    var activitiesAllowed = ActivityAuthorizationInfo().areActivitiesEnabled
    var frequentUpdatesAllowed = ActivityAuthorizationInfo().frequentPushesEnabled
    var isStarting = false
    @ObservationIgnored private var authorizationTask: Task<Void, Never>?
    @ObservationIgnored private var frequentTask: Task<Void, Never>?
    @ObservationIgnored private var tokenTask: Task<Void, Never>?
    @ObservationIgnored private var stateTask: Task<Void, Never>?
    @ObservationIgnored private var observedID: String?

    var currentActivity: Activity<MonitorAttributes>? {
        activities.first {
            ($0.activityState == .active || $0.activityState == .stale) && $0.attributes.endsAt > .now
        }
    }
    var requestsFrequentUpdates: Bool { Bundle.main.object(forInfoDictionaryKey: "NSSupportsLiveActivitiesFrequentUpdates") as? Bool == true }

    func observeAuthorization() {
        activitiesAllowed = authorization.areActivitiesEnabled
        frequentUpdatesAllowed = authorization.frequentPushesEnabled
        guard authorizationTask == nil else { return }
        authorizationTask = Task { [weak self] in
            guard let self else { return }
            for await allowed in self.authorization.activityEnablementUpdates {
                self.activitiesAllowed = allowed
                self.activities = Activity<MonitorAttributes>.activities
            }
        }
        if requestsFrequentUpdates {
            frequentTask = Task { [weak self] in
                guard let self else { return }
                for await allowed in self.authorization.frequentPushEnablementUpdates { self.frequentUpdatesAllowed = allowed }
            }
        }
    }
    private func content(_ reading: Reading?, endsAt: Date, staleAfter: TimeInterval) -> ActivityContent<MonitorAttributes.ContentState> {
        let now = Date.now
        let state = MonitorAttributes.ContentState(humidity: reading?.humidity, temperature: reading?.temperature,
            observedAt: reading?.observedAt ?? 0, connected: !(reading?.isStale(after: staleAfter, now: now) ?? true))
        return ActivityContent(state: state,
            staleDate: LiveActivityTiming.staleDate(for: reading, now: now, endsAt: endsAt, sensorStaleAfter: staleAfter),
            relevanceScore: relevanceScore)
    }
    func start(_ reading: Reading?, demo: Bool, staleAfter: TimeInterval = 90) async throws {
        guard !isStarting else { return }
        observeAuthorization()
        guard let reading, !demo, !reading.isStale(after: staleAfter), activitiesAllowed,
              NotificationManager.shared.server == .available else { throw LiveError.unavailable }
        isStarting = true
        defer { isStarting = false }
        await stopAll()
        let end = Date.now.addingTimeInterval(LiveActivityTiming.duration)
        let activity = try Activity.request(attributes: MonitorAttributes(amsName: reading.amsName, amsID: reading.amsID, endsAt: end),
            content: content(reading, endsAt: end, staleAfter: staleAfter), pushType: .token)
        activities = Activity<MonitorAttributes>.activities
        observe(activity)
    }
    private func observe(_ activity: Activity<MonitorAttributes>) {
        guard observedID != activity.id else { updateRegistration.reconcile(); return }
        tokenTask?.cancel(); stateTask?.cancel(); updateRegistration.stop()
        observedID = activity.id
        tokenTask = Task { [weak self] in
            guard let self else { return }
            if let token = activity.pushToken { self.register(activity, token: token) }
            for await token in activity.pushTokenUpdates {
                guard !Task.isCancelled && self.observedID == activity.id else { return }
                self.register(activity, token: token)
            }
        }
        stateTask = Task { [weak self] in
            for await state in activity.activityStateUpdates {
                guard let self, !Task.isCancelled else { return }
                self.activities = Activity<MonitorAttributes>.activities
                if state == .ended || state == .dismissed {
                    self.tokenTask?.cancel()
                    self.updateRegistration.stop()
                    self.observedID = nil
                    return
                }
            }
        }
    }
    private func register(_ activity: Activity<MonitorAttributes>, token: Data) {
        updateRegistration.submit(.init(id: activity.id, token: token.map { String(format: "%02x", $0) }.joined(),
                                        amsID: activity.attributes.amsID, expiresAt: activity.attributes.endsAt))
    }
    func update(_ readings: [Reading], staleAfter: TimeInterval = 90) async {
        observeAuthorization()
        activities = Activity<MonitorAttributes>.activities
        for activity in activities {
            if Date.now >= activity.attributes.endsAt {
                await activity.end(nil, dismissalPolicy: .immediate)
            } else {
                observe(activity)
                let reading = readings.first { $0.amsID == activity.attributes.amsID }
                await activity.update(content(reading, endsAt: activity.attributes.endsAt, staleAfter: staleAfter))
            }
        }
        activities = Activity<MonitorAttributes>.activities
    }
    func stopAll(relay: RelayClient = RelayClient()) async {
        tokenTask?.cancel(); stateTask?.cancel(); observedID = nil
        updateRegistration.stop()
        let ending = Activity<MonitorAttributes>.activities
        for activity in ending { await activity.end(nil, dismissalPolicy: .immediate) }
        activities = Activity<MonitorAttributes>.activities
        for activity in ending {
            _ = try? await relay.request("v1/activities/\(activity.id)", method: "DELETE")
        }
    }
    enum LiveError: LocalizedError {
        case unavailable
        var errorDescription: String? { "Live Activities need permission, a fresh reading, and configured relay push credentials." }
    }
}
