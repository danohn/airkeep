import Foundation

enum LiveActivityTiming {
    static let duration: TimeInterval = 8 * 3600
    // Keep these in sync with the relay's Live Activity timing constants.
    static let updateInterval: TimeInterval = 300
    static let deliveryGrace: TimeInterval = 60

    static func staleDate(for reading: Reading?, now: Date, endsAt: Date,
                          sensorStaleAfter: TimeInterval = 90) -> Date {
        guard let reading, !reading.isStale(after: sensorStaleAfter, now: now) else {
            return min(now, endsAt)
        }
        return min(now.addingTimeInterval(updateInterval + deliveryGrace), endsAt)
    }
}
