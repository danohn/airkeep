import Foundation
import Testing

@MainActor private final class NotificationFixture {
    var permission = NotificationManager.Permission(authorization: .allowed, banners: true, sounds: true)
    var connection = NotificationManager.Connection(endpoint: "https://relay.example.com", token: "test-access-token")
    var registered = false
    var configuration = NotificationRelayStatus.Configuration.available
    var reason = "local_checks_passed"
    var failure: Error?
    var legacy = false
    var prompts = 0
    var appleRequests = 0
    var registrations: [NotificationManager.Connection] = []
    var holdEndpoint: String?
    var pending: CheckedContinuation<NotificationRelayStatus, Error>?
    var timeout: Duration = .seconds(20)
    var receiveAlerts = true
    var savedIntent: Bool?
    var subscriptionFailure: Error?
    var subscriptionCalls = 0
    var holdSubscription = false
    var pendingSubscription: CheckedContinuation<Bool, Error>?

    func makeManager() -> NotificationManager {
        NotificationManager(dependencies: .init(permission: { self.permission }, requestAuthorization: {
            self.prompts += 1
            self.permission.authorization = .allowed
        }, registerWithApple: { self.appleRequests += 1 }, connection: { self.connection }, status: { connection in
            if connection.endpoint == self.holdEndpoint {
                return try await withCheckedThrowingContinuation { self.pending = $0 }
            }
            if self.legacy { throw RelayClient.RelayError.http(404) }
            if let failure = self.failure { throw failure }
            return NotificationRelayStatus(registered: self.registered, configuration: self.configuration,
                                           reason: self.reason, environment: "sandbox", receiveAlerts: self.registered ? self.receiveAlerts : false)
        }, register: { connection, _ in
            self.registrations.append(connection)
            if let failure = self.failure { throw failure }
            self.registered = true
        }, registrationTimeout: timeout, setSubscription: { _, enabled in
            self.subscriptionCalls += 1
            if let failure = self.subscriptionFailure { throw failure }
            if self.holdSubscription {
                self.receiveAlerts = try await withCheckedThrowingContinuation { self.pendingSubscription = $0 }
            } else { self.receiveAlerts = enabled }
            return self.receiveAlerts
        }, readIntent: { _ in self.savedIntent }, writeIntent: { _, enabled in self.savedIntent = enabled }))
    }
    var deviceToken: Data { Data(repeating: 0xAB, count: 32) }
}

@Suite @MainActor struct NotificationManagerTests {
    @Test func openingTheScreenNeverRequestsPermission() async {
        let fixture = NotificationFixture()
        fixture.permission.authorization = .notAsked
        let manager = fixture.makeManager()
        await manager.refresh()
        #expect(manager.permission.authorization == .notAsked)
        #expect(manager.registration == .notRegistered)
        #expect(fixture.prompts == 0)
        #expect(fixture.appleRequests == 0)
        #expect(fixture.registrations.isEmpty)
    }

    @Test func explicitPermissionRequestRechecksSystemState() async {
        let fixture = NotificationFixture()
        fixture.permission.authorization = .notAsked
        let manager = fixture.makeManager()
        await manager.requestPermission()
        #expect(fixture.prompts == 1)
        #expect(manager.permission.authorization == .allowed)
        #expect(fixture.appleRequests == 1)
        await manager.received(token: fixture.deviceToken)
        #expect(manager.registration == .registered)
        // Reopening with permission already granted must not ask again.
        await manager.requestPermission()
        #expect(fixture.prompts == 1)
    }

    @Test func registrationIsConfirmedWithoutClaimingDelivery() async {
        let fixture = NotificationFixture()
        let manager = fixture.makeManager()
        await manager.refresh()
        #expect(manager.registration == .registeringApple)
        await manager.received(token: fixture.deviceToken)
        #expect(manager.registration == .registered)
        #expect(manager.server == .available)
        #expect(fixture.registrations.count == 1)
        await manager.refresh()
        #expect(fixture.registrations.count == 1)
    }

    @Test func permissionRevocationAndPresentationChangesAreRefreshed() async {
        let fixture = NotificationFixture()
        let manager = fixture.makeManager()
        await manager.received(token: fixture.deviceToken)
        fixture.permission.authorization = .denied
        await manager.refresh()
        #expect(manager.permission.authorization == .denied)
        #expect(manager.registration == .registered)
        #expect(!manager.canRetryRegistration)
        #expect(fixture.registrations.count == 1)
        fixture.permission = .init(authorization: .allowed, banners: false, sounds: true)
        await manager.refresh()
        #expect(!manager.permission.banners)
        #expect(manager.permission.sounds)
    }

    @Test func serverConfigurationAndDeviceRegistrationAreIndependent() async {
        let fixture = NotificationFixture()
        fixture.configuration = .missing
        let manager = fixture.makeManager()
        await manager.received(token: fixture.deviceToken)
        #expect(manager.registration == .registered)
        if case .needsSetup = manager.server {} else { Issue.record("Missing production/server setup was treated as available") }
        fixture.configuration = .invalid
        fixture.reason = "topic_mismatch"
        await manager.refresh()
        if case .needsSetup(let detail) = manager.server { #expect(detail.contains("doesn't match")) }
        else { Issue.record("Topic mismatch must remain visible") }
    }

    @Test func relayFailureClearsSuccessfulReadinessAndCanRecover() async {
        let fixture = NotificationFixture()
        let manager = fixture.makeManager()
        await manager.received(token: fixture.deviceToken)
        fixture.failure = RelayClient.RelayError.http(401)
        await manager.refresh()
        #expect(manager.canRetryRegistration)
        if case .unavailable = manager.server {} else { Issue.record("A cached success survived relay failure") }
        fixture.failure = nil
        fixture.registered = false
        await manager.retryRegistration()
        #expect(manager.registration == .registered)
        #expect(fixture.registrations.count == 2)
    }

    @Test func wrongAppTopicDoesNotOverwriteDeviceRegistration() async {
        let fixture = NotificationFixture()
        fixture.configuration = .invalid
        fixture.reason = "topic_mismatch"
        let manager = fixture.makeManager()
        await manager.received(token: fixture.deviceToken)
        #expect(fixture.registrations.isEmpty)
        #expect(manager.registrationError?.contains("push topic") == true)
        #expect(manager.canRetryRegistration)
    }

    @Test func oldRelaysSupportRegistrationButDoNotClaimReadiness() async {
        let fixture = NotificationFixture()
        fixture.legacy = true
        let manager = fixture.makeManager()
        await manager.received(token: fixture.deviceToken)
        #expect(manager.registration == .registered)
        #expect(manager.server == .updateRequired)
        #expect(fixture.registrations.count == 1)
    }

    @Test func tokenRotationAndRelayDataLossAreReconciled() async {
        let fixture = NotificationFixture()
        let manager = fixture.makeManager()
        await manager.received(token: fixture.deviceToken)
        await manager.received(token: Data(repeating: 0xCD, count: 32))
        #expect(fixture.registrations.count == 2)
        fixture.registered = false
        await manager.refresh()
        #expect(fixture.registrations.count == 3)
        #expect(manager.registration == .registered)
    }

    @Test func missingConnectionDoesNotRegisterWithApple() async {
        let fixture = NotificationFixture()
        fixture.connection.endpoint = ""
        let manager = fixture.makeManager()
        await manager.refresh()
        #expect(manager.registration == .needsConnection)
        #expect(manager.server == .unchecked)
        #expect(fixture.appleRequests == 0)
    }

    @Test func stalledAppleRegistrationBecomesRetryableAndLateTokensRecover() async {
        let fixture = NotificationFixture()
        fixture.timeout = .milliseconds(10)
        let manager = fixture.makeManager()
        await manager.refresh()
        let deadline = ContinuousClock.now.advanced(by: .seconds(2))
        while !manager.canRetryRegistration && ContinuousClock.now < deadline { await Task.yield() }
        #expect(manager.canRetryRegistration)
        await manager.received(token: fixture.deviceToken)
        #expect(manager.registration == .registered)
    }

    @Test func quietPermissionDoesNotRequestFullAuthorizationAgain() async {
        let fixture = NotificationFixture()
        fixture.permission = .init(authorization: .quiet, banners: false)
        let manager = fixture.makeManager()
        await manager.received(token: fixture.deviceToken)
        #expect(manager.permission.authorization == .quiet)
        #expect(manager.registration == .registered)
        #expect(fixture.prompts == 0)
    }

    @Test(arguments: [false, true]) func oldResponsesCannotApplyToANewConnection(credentialOnly: Bool) async {
        let fixture = NotificationFixture()
        fixture.holdEndpoint = fixture.connection.endpoint
        let manager = fixture.makeManager()
        let oldCheck = Task { await manager.received(token: fixture.deviceToken) }
        while fixture.pending == nil { await Task.yield() }
        let pending = fixture.pending
        fixture.holdEndpoint = nil
        if credentialOnly { fixture.connection.token = "replacement-access-token" }
        else { fixture.connection.endpoint = "https://replacement.example.com" }
        await manager.refresh()
        pending?.resume(returning: .init(registered: false, configuration: .invalid,
                                        reason: "topic_mismatch", environment: "sandbox"))
        await oldCheck.value
        #expect(manager.registration == .registered)
        #expect(manager.server == .available)
        #expect(fixture.registrations == [fixture.connection])
    }

    @Test func optingOutSurvivesTokenRotationAndRelaunch() async {
        let fixture = NotificationFixture()
        let manager = fixture.makeManager()
        await manager.received(token: fixture.deviceToken)
        await manager.setReceiveAlerts(false)
        #expect(manager.receiveAlerts == false)
        #expect(fixture.savedIntent == false)
        await manager.received(token: Data(repeating: 0xCD, count: 32))
        await manager.refresh()
        let relaunched = fixture.makeManager()
        await relaunched.received(token: fixture.deviceToken)
        #expect(relaunched.receiveAlerts == false)
        #expect(fixture.subscriptionCalls == 1)
        #expect(fixture.prompts == 0)
    }

    @Test func subscriptionChangesWaitForAcknowledgementAndFailuresKeepTheConfirmedState() async {
        let fixture = NotificationFixture()
        fixture.receiveAlerts = false
        let manager = fixture.makeManager()
        await manager.received(token: fixture.deviceToken)
        fixture.holdSubscription = true
        let enabling = Task { await manager.setReceiveAlerts(true) }
        while fixture.pendingSubscription == nil { await Task.yield() }
        #expect(manager.receiveAlerts == false)
        #expect(manager.isChangingSubscription)
        fixture.pendingSubscription?.resume(returning: true)
        await enabling.value
        #expect(manager.receiveAlerts == true)
        fixture.holdSubscription = false
        fixture.subscriptionFailure = RelayClient.RelayError.http(503)
        await manager.setReceiveAlerts(false)
        #expect(manager.receiveAlerts == true)
        #expect(fixture.savedIntent == true)
        #expect(manager.subscriptionError != nil)
    }

    @Test func enablingDeliveryRequestsPermissionOnlyOnExplicitAction() async {
        let fixture = NotificationFixture()
        fixture.permission.authorization = .notAsked
        fixture.receiveAlerts = false
        let manager = fixture.makeManager()
        await manager.refresh()
        #expect(fixture.prompts == 0)
        let enabling = Task { await manager.setReceiveAlerts(true) }
        while fixture.appleRequests == 0 { await Task.yield() }
        await manager.received(token: fixture.deviceToken)
        await enabling.value
        #expect(fixture.prompts == 1)
        #expect(manager.receiveAlerts == true)
        fixture.permission.authorization = .denied
        await manager.setReceiveAlerts(true)
        #expect(fixture.prompts == 1)
        #expect(manager.subscriptionError?.contains("system settings") == true)
    }

    @Test func subscriptionAcknowledgementsCannotApplyToAnotherRelay() async {
        let fixture = NotificationFixture()
        fixture.receiveAlerts = false
        let manager = fixture.makeManager()
        await manager.received(token: fixture.deviceToken)
        fixture.holdSubscription = true
        let enabling = Task { await manager.setReceiveAlerts(true) }
        while fixture.pendingSubscription == nil { await Task.yield() }
        fixture.connection.endpoint = "https://other.example.com"
        fixture.pendingSubscription?.resume(returning: true)
        await enabling.value
        #expect(manager.receiveAlerts == false)
        #expect(fixture.savedIntent == false)
        #expect(manager.subscriptionError?.contains("changed") == true)
    }
}
