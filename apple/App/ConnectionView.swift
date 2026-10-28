import SwiftUI

struct ConnectionView: View {
    var model: MonitorModel
    @Environment(\.dismiss) private var dismiss
    @State private var endpoint = SharedConfiguration.endpoint
    @State private var token = TokenStore.read()
    @State private var initialEndpoint = SharedConfiguration.endpoint
    @State private var initialToken = TokenStore.read()
    @State private var error: String?
    @State private var saving = false
    @State private var discard = false
    private var changed: Bool { endpoint != initialEndpoint || token != initialToken }
    var body: some View {
        NavigationStack {
            Form {
                Section("Airkeep relay") {
                    TextField("Relay URL", text: $endpoint).textContentType(.URL)
                        #if os(iOS)
                        .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                        #endif
                    SecureField("Relay bearer token", text: $token)
                    Text("Enter your relay's HTTPS address, such as https://airkeep.example.com.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Section("Privacy") {
                    Text("Your connection token is stored securely in Keychain.")
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
                    Button(saving ? "Connecting…" : "Save & connect") {
                        Task {
                            saving = true
                            defer { saving = false }
                            do {
                                let url = endpoint.trimmingCharacters(in: .whitespacesAndNewlines)
                                let previous = RelayClient()
                                _ = try await RelayClient(endpoint: url, token: token).snapshot()
                                try TokenStore.save(token)
                                SharedConfiguration.endpoint = url
                                model.demo = false
                                model.connectionChanged()
                                #if os(iOS)
                                if previous.endpoint != url { await LiveMonitor.shared.stopAll(relay: previous) }
                                #endif
                                await model.refresh(forceHistory: true)
                                await model.alertRules.refresh()
                                await NotificationManager.shared.refresh()
                                dismiss()
                            } catch { self.error = error.localizedDescription }
                        }
                    }.disabled(saving || token.isEmpty || endpoint.isEmpty)
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
