import SwiftUI
#if os(macOS)
import ServiceManagement
#endif

struct ConnectionView: View {
    var model: MonitorModel
    @Environment(\.dismiss) private var dismiss
    @State private var endpoint = SharedConfiguration.endpoint
    @State private var token = TokenStore.read()
    @State private var error: String?
    @State private var saving = false
    var body: some View {
        NavigationStack {
            Form {
                Section("Airkeep relay") {
                    TextField("Relay URL", text: $endpoint).textContentType(.URL)
                        #if os(iOS)
                        .keyboardType(.URL).textInputAutocapitalization(.never).autocorrectionDisabled()
                        #endif
                    SecureField("Relay bearer token", text: $token)
                    Text("Enter your relay's address, such as https://airkeep.your-tailnet.ts.net. The printer's access code stays on the relay.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Section("Privacy") {
                    Text("Your relay token stays in Keychain and is shared with Airkeep's widgets. You don't need a Bambu cloud account.")
                        .font(.caption)
                }
                #if os(macOS)
                Section("Menu bar") {
                    Toggle("Launch Airkeep at login", isOn: Binding(
                        get: { SMAppService.mainApp.status == .enabled },
                        set: { enable in
                            do { if enable { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() } }
                            catch { self.error = error.localizedDescription }
                        }))
                }
                #endif
                if let error { Text(error).foregroundStyle(.orange).font(.caption) }
            }
            .formStyle(.grouped).navigationTitle("Connection")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button(saving ? "Connecting…" : "Save & connect") {
                        Task {
                            saving = true
                            defer { saving = false }
                            do {
                                let url = endpoint.trimmingCharacters(in: .whitespacesAndNewlines)
                                _ = try await RelayClient(endpoint: url, token: token).snapshot()
                                try TokenStore.save(token)
                                SharedConfiguration.endpoint = url
                                model.demo = false
                                await model.refresh(forceHistory: true)
                                dismiss()
                            } catch { self.error = error.localizedDescription }
                        }
                    }.disabled(saving || token.isEmpty || endpoint.isEmpty)
                }
            }
        }.preferredColorScheme(.dark).tint(AirkeepTheme.amber)
        #if os(macOS)
        .frame(width: 480, height: 460)
        #endif
    }
}
