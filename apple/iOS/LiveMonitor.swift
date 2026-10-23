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
    func state(_ reading: Reading?) -> MonitorAttributes.ContentState {
        .init(humidity: reading?.humidity, temperature: reading?.temperature,
              observedAt: reading?.observedAt ?? 0, connected: !(reading?.isStale() ?? true))
    }
    func start(_ reading: Reading?, demo: Bool) async throws {
        guard let reading, !demo, !reading.isStale(), ActivityAuthorizationInfo().areActivitiesEnabled else {
            throw LiveError.unavailable
        }
        await stopAll()
        let end = Date.now.addingTimeInterval(4 * 3600)
        let activity = try Activity.request(attributes: MonitorAttributes(amsName: reading.amsName, amsID: reading.amsID, endsAt: end),
            content: ActivityContent(state: state(reading), staleDate: reading.date.addingTimeInterval(90),
                                     relevanceScore: relevanceScore), pushType: .token)
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
    func update(_ reading: Reading?) async {
        activities = Activity<MonitorAttributes>.activities
        for activity in activities {
            if Date.now >= activity.attributes.endsAt {
                await activity.end(nil, dismissalPolicy: .immediate)
            } else {
                observe(activity)
                if activity.attributes.amsID == reading?.amsID {
                    await activity.update(ActivityContent(state: state(reading), staleDate: reading?.date.addingTimeInterval(90),
                                                          relevanceScore: relevanceScore))
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
