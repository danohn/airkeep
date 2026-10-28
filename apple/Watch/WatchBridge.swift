import Foundation
import Observation
import WatchConnectivity
import WidgetKit

extension Notification.Name { static let airkeepWatchChanged = Notification.Name("AirkeepWatchChanged") }

@MainActor final class WatchBridge: NSObject, WCSessionDelegate {
    static let shared = WatchBridge()
    private let session = WCSession.default
    private let storage = WatchStorage.live
    override init() {
        super.init()
        session.delegate = self
        session.activate()
    }
    func activate() { session.activate() }
    private func receive(configuration: Data?, snapshot: Data?, linkID: String?) {
        if let configuration, let message = try? JSONDecoder().decode(WatchConfigurationMessage.self, from: configuration) {
            let receipt: WatchConfigurationReceipt
            do { receipt = try storage.apply(message) }
            catch { receipt = .init(senderID: message.senderID, revision: message.revision, connected: false, error: "Couldn't connect. Retry from iPhone.") }
            if let data = try? JSONEncoder().encode(receipt) { try? session.updateApplicationContext(["receipt": data]) }
            Task { await WatchWidgetRegistration.live.sync() }
        }
        if let snapshot, let current = try? storage.connection(), current.linkID == linkID,
           let value = try? JSONDecoder().decode(Snapshot.self, from: snapshot) {
            try? storage.cache(value, endpoint: current.endpoint)
        }
        WidgetCenter.shared.reloadAllTimelines()
        NotificationCenter.default.post(name: .airkeepWatchChanged, object: nil)
    }
    func finishBackgroundDelivery() async {
        let context = session.receivedApplicationContext
        receive(configuration: context["configuration"] as? Data, snapshot: context["snapshot"] as? Data, linkID: context["linkID"] as? String)
        await WatchWidgetSetup.reconcile()
        await WatchWidgetRegistration.live.sync()
    }
    func fetch(connection: WatchConnection, resource: WatchFetchRequest.Resource, ams: String, hours: Int) async throws -> Data {
        guard session.activationState == .activated, session.isReachable else { throw RelayClient.RelayError.transport }
        let request = WatchFetchRequest(resource: resource, linkID: connection.linkID, amsID: ams, hours: hours)
        let encoded = try JSONEncoder().encode(request)
        let reply: Data = try await withCheckedThrowingContinuation { continuation in
            let gate = WatchReplyGate(continuation)
            session.sendMessageData(encoded, replyHandler: { @Sendable in gate.finish(.success($0)) },
                                    errorHandler: { @Sendable in gate.finish(.failure($0)) })
            Task {
                try? await Task.sleep(for: .seconds(8))
                gate.finish(.failure(RelayClient.RelayError.transport))
            }
        }
        try Task.checkCancellation()
        let result = try JSONDecoder().decode(WatchFetchReply.self, from: reply)
        guard let data = result.data else { throw RelayClient.RelayError.transport }
        return data
    }
    nonisolated func session(_ session: WCSession, activationDidCompleteWith activationState: WCSessionActivationState, error: Error?) {
        guard activationState == .activated else { return }
        let configuration = session.receivedApplicationContext["configuration"] as? Data
        let snapshot = session.receivedApplicationContext["snapshot"] as? Data
        let link = session.receivedApplicationContext["linkID"] as? String
        Task { @MainActor in self.receive(configuration: configuration, snapshot: snapshot, linkID: link) }
    }
    nonisolated func session(_ session: WCSession, didReceiveApplicationContext applicationContext: [String: Any]) {
        let configuration = applicationContext["configuration"] as? Data
        let snapshot = applicationContext["snapshot"] as? Data
        let link = applicationContext["linkID"] as? String
        Task { @MainActor in self.receive(configuration: configuration, snapshot: snapshot, linkID: link) }
    }
    nonisolated func session(_ session: WCSession, didReceiveMessage message: [String: Any],
                             replyHandler: @escaping ([String: Any]) -> Void) {
        guard let data = message["configuration"] as? Data else { replyHandler([:]); return }
        let reply = WatchCallback(replyHandler)
        Task { @MainActor in
            self.receive(configuration: data, snapshot: nil, linkID: nil)
            if let receipt = self.session.applicationContext["receipt"] as? Data { reply.call(["receipt": receipt]) }
            else { reply.call([:]) }
        }
    }
}

private final class WatchReplyGate: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<Data, Error>?
    init(_ continuation: CheckedContinuation<Data, Error>) { self.continuation = continuation }
    func finish(_ result: Result<Data, Error>) {
        lock.lock()
        let pending = continuation; continuation = nil
        lock.unlock()
        pending?.resume(with: result)
    }
}
