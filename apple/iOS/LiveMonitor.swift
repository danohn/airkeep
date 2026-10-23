@preconcurrency import ActivityKit
import Foundation
import Observation

@MainActor @Observable final class LiveMonitor {
    static let shared = LiveMonitor()
    // Match the relay's relevance score. During device testing, the default
    // score of zero left an active session missing from Dynamic Island.
    private let relevanceScore = 50.0
    var activities: [Activity<MonitorAttributes>] = Activity<MonitorAttributes>.activities
    private var tokenTasks: [String: Task<Void, Never>] = [:]
    var currentActivity: Activity<MonitorAttributes>? {
        activities.first {
            ($0.activityState == .active || $0.activityState == .stale) && $0.attributes.endsAt > .now
        }
    }
    func state(_ reading: Reading?, now: Date, staleAfter: TimeInterval) -> MonitorAttributes.ContentState {
        .init(humidity: reading?.humidity, temperature: reading?.temperature,
              observedAt: reading?.observedAt ?? 0,
              connected: !(reading?.isStale(after: staleAfter, now: now) ?? true))
    }
    private func content(_ reading: Reading?, endsAt: Date, staleAfter: TimeInterval) -> ActivityContent<MonitorAttributes.ContentState> {
        let now = Date.now
        return ActivityContent(state: state(reading, now: now, staleAfter: staleAfter),
            staleDate: LiveActivityTiming.staleDate(for: reading, now: now, endsAt: endsAt, sensorStaleAfter: staleAfter),
            relevanceScore: relevanceScore)
    }
    func start(_ reading: Reading?, demo: Bool, staleAfter: TimeInterval = 90) async throws {
        guard let reading, !demo, !reading.isStale(after: staleAfter), ActivityAuthorizationInfo().areActivitiesEnabled else {
            throw LiveError.unavailable
        }
        await stopAll()
        let end = Date.now.addingTimeInterval(LiveActivityTiming.duration)
        let activity = try Activity.request(attributes: MonitorAttributes(amsName: reading.amsName, amsID: reading.amsID, endsAt: end),
            content: content(reading, endsAt: end, staleAfter: staleAfter), pushType: .token)
        activities = Activity<MonitorAttributes>.activities
        observe(activity)
    }
    private func observe(_ activity: Activity<MonitorAttributes>) {
        guard tokenTasks[activity.id] == nil else { return }
        tokenTasks[activity.id] = Task {
            for await token in activity.pushTokenUpdates {
                let value = token.map { String(format: "%02x", $0) }.joined()
                // Retry transient relay errors until this session expires or the task is cancelled.
                while !Task.isCancelled && Date.now < activity.attributes.endsAt {
                    do {
                        try await RelayClient().registerActivity(id: activity.id, token: value, amsID: activity.attributes.amsID,
                            expiresAt: activity.attributes.endsAt.timeIntervalSince1970)
                        break
                    } catch { try? await Task.sleep(for: .seconds(15)) }
                }
            }
        }
    }
    func update(_ reading: Reading?, staleAfter: TimeInterval = 90) async {
        activities = Activity<MonitorAttributes>.activities
        for activity in activities {
            if Date.now >= activity.attributes.endsAt {
                await activity.end(nil, dismissalPolicy: .immediate)
            } else {
                observe(activity)
                if activity.attributes.amsID == reading?.amsID {
                    await activity.update(content(reading, endsAt: activity.attributes.endsAt, staleAfter: staleAfter))
                }
            }
        }
    }
    func stopAll() async {
        for activity in Activity<MonitorAttributes>.activities {
            tokenTasks[activity.id]?.cancel()
            tokenTasks[activity.id] = nil
            await activity.end(nil, dismissalPolicy: .immediate)
            _ = try? await RelayClient().request("v1/activities/\(activity.id)", method: "DELETE")
        }
        activities = []
    }
    enum LiveError: LocalizedError {
        case unavailable
        var errorDescription: String? { "Live Activities need a fresh reading and permission in system settings." }
    }
}
