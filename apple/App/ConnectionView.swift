import SwiftUI

struct ConnectionView: View {
    var model: MonitorModel
    @Environment(\.dismiss) private var dismiss
    @State private var endpoint = SharedConfiguration.endpoint
    @State private var initialEndpoint = SharedConfiguration.endpoint
    @State private var error: String?
    @State private var saving = false
    @State private var discard = false
    private var changed: Bool { endpoint != initialEndpoint }
    var body: some View {
        NavigationStack {
            Form {
                Section("Airkeep relay") {
                    TextField("Relay URL", text: $endpoint).textContentType(.URL)
                        #if os(iOS)
                        .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                        #endif
                    Text("Enter your relay's HTTPS address, such as https://airkeep.example.com.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Section("Privacy") {
                    Text("Airkeep keeps your sign-in in Keychain.")
                        .font(.caption)
                }
                #if os(iOS)
                if LiveMonitor.shared.currentActivity != nil {
                    Text("Changing relay addresses ends your current Live Activity.").font(.caption).foregroundStyle(.secondary)
                }
                #endif
                if let error { Text(error).foregroundStyle(.orange).font(.caption) }
            }
            .formStyle(.grouped).navigationTitle("Relay connection")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { if changed { discard = true } else { dismiss() } }.disabled(saving)
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button(saving ? "Connecting…" : "Sign in & connect") {
                        Task {
                            saving = true
                            defer { saving = false }
                            do {
                                let url = try RelayOAuth.origin(endpoint.trimmingCharacters(in: .whitespacesAndNewlines))
                                let previous = RelayClient()
                                let signIn = RelaySignIn()
                                let installation = UUID().uuidString
                                let tokens = try await signIn.signIn(endpoint: url, installation: installation)
                                let marker = RelayOAuth.marker(tokens.grant_id)
                                _ = try await RelayClient(endpoint: url, token: marker).snapshot()
                                if previous.token.hasPrefix("oauth:"), previous.token != marker {
                                    _ = try? await previous.request("auth/revoke", method: "POST")
                                    try? TokenStore.delete(account: previous.token)
                                }
                                SharedConfiguration.clearAccountData(endpoint: previous.endpoint)
                                try TokenStore.save(marker)
                                SharedConfiguration.deviceID = installation
                                SharedConfiguration.endpoint = url
                                model.demo = false
                                model.connectionChanged()
                                #if os(iOS)
                                if previous.token != marker { await LiveMonitor.shared.stopAll(relay: previous) }
                                WatchCompanion.shared.clearConnection()
                                #endif
                                await model.refresh(forceHistory: true)
                                await model.alertRules.refresh()
                                await NotificationManager.shared.refresh()
                                dismiss()
                            } catch { self.error = error.localizedDescription }
                        }
                    }.disabled(saving || endpoint.isEmpty)
                }
            }
        }.preferredColorScheme(.dark).tint(AirkeepTheme.amber)
        .interactiveDismissDisabled(changed || saving)
        .confirmationDialog("Discard your connection changes?", isPresented: $discard, titleVisibility: .visible) {
            Button("Discard changes", role: .destructive) { dismiss() }
        }
        #if os(macOS)
        .frame(width: 480, height: 460)
        #endif
    }
}
