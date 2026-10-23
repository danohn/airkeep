import Foundation
import Testing

struct LiveActivityTimingTests {
    private let now = Date(timeIntervalSince1970: 1050)
    private var reading: Reading {
        Reading(amsID: "0", humidity: 24, temperature: 25.6, rawIndex: 2, observedAt: 1000)
    }

    @Test func freshReadingLastsThroughTheNextBackgroundUpdate() {
        let end = now.addingTimeInterval(LiveActivityTiming.duration)
        let deadline = LiveActivityTiming.staleDate(for: reading, now: now, endsAt: end)
        #expect(LiveActivityTiming.duration == 8 * 3600)
        #expect(deadline == now.addingTimeInterval(360))
        #expect(deadline > now.addingTimeInterval(300))
        #expect(reading.observedAt == 1000)
    }

    @Test func oldOrMissingSensorDataIsStaleImmediately() {
        let end = now.addingTimeInterval(LiveActivityTiming.duration)
        let later = now.addingTimeInterval(41)
        #expect(LiveActivityTiming.staleDate(for: reading, now: later, endsAt: end) == later)
        #expect(LiveActivityTiming.staleDate(for: nil, now: now, endsAt: end) == now)
        #expect(LiveActivityTiming.staleDate(for: reading, now: now, endsAt: end, sensorStaleAfter: 30) == now)
    }

    @Test func freshnessNeverExtendsPastTheSessionEnd() {
        let end = now.addingTimeInterval(30)
        #expect(LiveActivityTiming.staleDate(for: reading, now: now, endsAt: end) == end)
    }
}
