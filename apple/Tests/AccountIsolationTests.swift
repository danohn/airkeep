import Foundation
import Testing

struct AccountIsolationTests {
    @Test func cacheKeysSeparateSameOriginAccountsAndOrigins() {
        let hosted = SharedConfiguration.hostedEndpoint
        let first = SharedConfiguration.accountKey(endpoint: hosted, token: "oauth:first-account")
        let next = SharedConfiguration.accountKey(endpoint: hosted, token: "oauth:second-account")
        #expect(first != next)
        #expect(first != SharedConfiguration.accountKey(endpoint: "https://relay.example.org", token: "oauth:first-account"))
        #expect(first == SharedConfiguration.accountKey(endpoint: hosted, token: "oauth:first-account"))
        #expect(!first.contains("first-account"))
    }
}
