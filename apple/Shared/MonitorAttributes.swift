#if os(iOS)
import ActivityKit
import Foundation

struct MonitorAttributes: ActivityAttributes, Sendable {
    struct ContentState: Codable, Hashable, Sendable {
        var humidity: Double?
        var temperature: Double?
        var observedAt: Double
        var connected: Bool
    }
    var amsName: String
    var amsID: String
    var endsAt: Date
}
#endif
