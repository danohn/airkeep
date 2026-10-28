import Foundation
import Observation
import WatchConnectivity

@MainActor @Observable final class WatchCompanion: NSObject, WCSessionDelegate {
    static let shared = WatchCompanion()
    var paired = false
    var installed = false
    var ready = false
    var waiting = false
    var connected = false
    var error: String?
    private let session: WCSession?
    private let defaults = SharedConfiguration.defaults
    private var pending: WatchConfigurationMessage?
    private var connectionAttempt = UUID()

    override init() {
        session = WCSession.isSupported() ? .default : nil
        super.init()
        if let data = TokenStore.read(account: "watch-pending-configuration").data(using: .utf8) {
            pending = try? JSONDecoder().decode(WatchConfigurationMessage.self, from: data)
        }
        connected = defaults.bool(forKey: "watch.connected")
        waiting = pending != nil
        session?.delegate = self
        session?.activate()
    }
    func activate() { session?.activate(); updateAvailability() }
    private func updateAvailability() {
        guard let session, session.activationState == .activated else { ready = false; return }
        ready = true; paired = session.isPaired; installed = session.isWatchAppInstalled
    }
    func connect(amsID: String) async {
        let attempt = UUID(); connectionAttempt = attempt
        do {
            let endpoint = SharedConfiguration.endpoint
            let token = TokenStore.read()
            _ = try await RelayClient(endpoint: endpoint, token: token).snapshot()
            guard attempt == connectionAttempt, endpoint == SharedConfiguration.endpoint, token == TokenStore.read() else { return }
            let connection = WatchConnection(endpoint: endpoint, token: token, linkID: UUID().uuidString)
            guard connection.isConfigured else { throw RelayClient.RelayError.configuration }
            try queue(connection: connection, amsID: amsID)
        } catch { if attempt == connectionAttempt { self.error = error.localizedDescription } }
    }
    func disconnect() {
        connectionAttempt = UUID()
        do { try queue(connection: nil, amsID: SharedConfiguration.amsID) }
        catch { self.error = error.localizedDescription }
    }
    func connectionChanged() {
        guard defaults.bool(forKey: "watch.linkEnabled") else { return }
        Task {
            guard defaults.bool(forKey: "watch.linkEnabled") else { return }
            await connect(amsID: SharedConfiguration.amsID)
        }
    }
    private func queue(connection: WatchConnection?, amsID: String) throws {
        let revision = defaults.integer(forKey: "watch.configurationRevision") + 1
        let message = WatchConfigurationMessage(senderID: SharedConfiguration.deviceID, revision: revision, connection: connection, amsID: amsID)
        let encoded = try JSONEncoder().encode(message)
        try TokenStore.save(String(decoding: encoded, as: UTF8.self), account: "watch-pending-configuration")
        defaults.set(revision, forKey: "watch.configurationRevision")
        defaults.set(connection?.linkID, forKey: "watch.activeLinkID")
        defaults.set(connection != nil, forKey: "watch.linkEnabled")
        pending = message; waiting = true; error = nil
        sendPending()
    }
    func retry() {
        error = nil
        if pending == nil, let data = TokenStore.read(account: "watch-pending-configuration").data(using: .utf8) {
            pending = try? JSONDecoder().decode(WatchConfigurationMessage.self, from: data)
        }
        waiting = pending != nil
        sendPending()
    }
    private func sendPending() {
        guard let session, session.activationState == .activated, let pending else { return }
        do {
            try session.updateApplicationContext(["configuration": JSONEncoder().encode(pending)])
            if session.isReachable {
                // WCSession invokes reply blocks on its own queue.
                session.sendMessage(["configuration": try JSONEncoder().encode(pending)], replyHandler: { @Sendable [weak self] reply in
                    guard let data = reply["receipt"] as? Data else { return }
                    Task { @MainActor in self?.receiveReceipt(data) }
                }, errorHandler: { @Sendable _ in /* Application context remains available for background delivery. */ })
            }
        } catch { self.error = "Couldn't send setup to Apple Watch. Tap Retry." }
    }
    private func receiveReceipt(_ data: Data) {
        guard let receipt = try? JSONDecoder().decode(WatchConfigurationReceipt.self, from: data),
              receipt.revision == pending?.revision, receipt.senderID == pending?.senderID else { return }
        if let error = receipt.error { self.error = error; waiting = false; return }
        connected = receipt.connected; waiting = false; pending = nil; error = nil
        defaults.set(connected, forKey: "watch.connected")
        try? TokenStore.delete(account: "watch-pending-configuration")
    }
    nonisolated func session(_ session: WCSession, activationDidCompleteWith activationState: WCSessionActivationState, error: Error?) {
        Task { @MainActor in
            self.updateAvailability()
            if let error { self.error = error.localizedDescription }
            if self.pending == nil, let data = TokenStore.read(account: "watch-pending-configuration").data(using: .utf8) {
                self.pending = try? JSONDecoder().decode(WatchConfigurationMessage.self, from: data)
            }
            self.waiting = self.pending != nil
            self.sendPending()
        }
    }
    nonisolated func sessionDidBecomeInactive(_ session: WCSession) {}
    nonisolated func sessionDidDeactivate(_ session: WCSession) { session.activate() }
    nonisolated func sessionWatchStateDidChange(_ session: WCSession) { Task { @MainActor in self.updateAvailability() } }
    nonisolated func session(_ session: WCSession, didReceiveApplicationContext applicationContext: [String: Any]) {
        guard let data = applicationContext["receipt"] as? Data else { return }
        Task { @MainActor in self.receiveReceipt(data) }
    }
    nonisolated func session(_ session: WCSession, didReceiveMessageData messageData: Data,
                             replyHandler: @escaping (Data) -> Void) {
        let reply = WatchCallback(replyHandler)
        Task { @MainActor in
            let response: WatchFetchReply
            do {
                let request = try JSONDecoder().decode(WatchFetchRequest.self, from: messageData)
                guard self.defaults.bool(forKey: "watch.linkEnabled"),
                      self.defaults.string(forKey: "watch.activeLinkID") == request.linkID else { throw RelayClient.RelayError.connectionChanged }
                let client = RelayClient()
                let data: Data
                switch request.resource {
                case .snapshot: data = try await client.request("v1/snapshot")
                case .alerts: data = try await client.request("v2/alerts")
                case .history:
                    let readings = try await client.history(amsID: request.amsID, hours: request.hours)
                    data = try JSONEncoder().encode(WatchHistoryPacket.compact(readings))
                }
                response = .init(data: data)
            } catch { response = .init(error: "Open Airkeep on iPhone to reconnect.") }
            if let encoded = try? JSONEncoder().encode(response) { reply.call(encoded) }
        }
    }
    func share(_ snapshot: Snapshot) {
        guard defaults.bool(forKey: "watch.linkEnabled"), let session, session.activationState == .activated else { return }
        guard Date.now.timeIntervalSince(lastShare) >= 60 else { return }
        var context = session.applicationContext
        if let data = try? JSONEncoder().encode(snapshot), let link = defaults.string(forKey: "watch.activeLinkID") {
            context["snapshot"] = data; context["linkID"] = link
            try? session.updateApplicationContext(context)
            lastShare = .now
        }
    }
    private var lastShare = Date.distantPast
}
