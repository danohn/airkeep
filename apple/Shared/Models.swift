import Foundation

struct Reading: Codable, Identifiable, Sendable {
    var amsID: String
    var humidity: Double
    var temperature: Double?
    var rawIndex: Int?
    var observedAt: Double
    var id: Double { observedAt }
    var date: Date { Date(timeIntervalSince1970: observedAt) }
    var amsName: String { "AMS \((Int(amsID) ?? 0) + 1)" }
    var humidityText: String { humidity.formatted(.number.precision(.fractionLength(0))) }
    func isStale(after seconds: Double = 90, now: Date = .now) -> Bool {
        now.timeIntervalSince(date) > seconds
    }
}

struct AlertRule: Codable, Equatable, Sendable {
    var enabled = false
    var amsID = "0"
    var threshold = 30.0
    var holdSeconds = 300
    var hysteresis = 3.0
}

struct ManagedAlertRule: Codable, Equatable, Identifiable, Sendable {
    var id: String
    var name: String
    var revision: Int
    var enabled: Bool
    var amsID: String
    var threshold: Double
    var holdSeconds: Int
    var hysteresis: Double
    var active: Bool

    init(id: String = UUID().uuidString, name: String, revision: Int = 0,
         enabled: Bool = false, amsID: String = "0", threshold: Double = 30,
         holdSeconds: Int = 300, hysteresis: Double = 3, active: Bool = false) {
        self.id = id; self.name = name; self.revision = revision; self.enabled = enabled
        self.amsID = amsID; self.threshold = threshold; self.holdSeconds = holdSeconds
        self.hysteresis = hysteresis; self.active = active
    }
    var amsName: String { "AMS \((Int(amsID) ?? 0) + 1)" }
    var conditionSummary: String {
        let duration = holdSeconds == 0 ? "immediately" : "for \(holdSeconds / 60) min"
        return "\(amsName) · above \(Int(threshold))% RH \(duration)"
    }
}

struct AlertRulesCache: Codable, Sendable {
    var rules: [ManagedAlertRule]
    var syncedAt: Date
}

struct Snapshot: Codable, Sendable {
    var serverTime: Double
    var staleAfter: Double
    var printerConnected: Bool
    var pushConfigured: Bool
    var readings: [Reading]
    var alertRule: AlertRule
    var alertActive: Bool
}

struct NotificationRelayStatus: Decodable, Sendable {
    enum Configuration: String, Decodable, Sendable { case missing, invalid, available }
    var registered: Bool
    var configuration: Configuration
    var reason: String
    var environment: String
    var receiveAlerts: Bool? = nil
}

enum PreviewData {
    static var reading: Reading {
        Reading(amsID: "0", humidity: 24, temperature: 31.6, rawIndex: 3, observedAt: Date.now.timeIntervalSince1970)
    }
    static var history: [Reading] {
        let now = Date.now.timeIntervalSince1970
        var result: [Reading] = []
        for index in 0..<120 {
            let wave = sin(Double(index) / 7.0) * 1.1
            let detail = sin(Double(index) / 2.1) * 0.4
            let at = now - Double(119 - index) * 30.0
            result.append(Reading(amsID: "0", humidity: 24.0 + wave + detail,
                                  temperature: 31.6, rawIndex: 3, observedAt: at))
        }
        return result
    }
}
