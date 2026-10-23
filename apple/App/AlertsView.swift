import SwiftUI
import UserNotifications

struct AlertsView: View {
    var model: MonitorModel
    @Environment(\.dismiss) private var dismiss
    @State private var rule = AlertRule()
    @State private var message: String?
    @State private var saving = false
    var body: some View {
        NavigationStack {
            Form {
                Section("Humidity rule") {
                    Toggle("Alert above my limit", isOn: $rule.enabled)
                    Stepper("Limit: \(Int(rule.threshold))% RH", value: $rule.threshold, in: 10...90, step: 1)
                    Picker("Stay above limit for", selection: $rule.holdSeconds) {
                        Text("1 minute").tag(60); Text("5 minutes").tag(300); Text("15 minutes").tag(900)
                    }
                    Stepper("Reset \(Int(rule.hysteresis))% below limit", value: $rule.hysteresis, in: 1...9, step: 1)
                    Text("You'll get one alert after humidity stays above your limit for the chosen time, then one when it falls below the reset point. Old readings reset the hold timer. AMS air humidity doesn't measure moisture inside a spool.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Section("Push notifications") {
                    LabeledContent("Relay APNs", value: model.snapshot?.pushConfigured == true ? "Configured" : "Not configured")
                    Button("Enable notifications on this device") {
                        Task {
                            do { try await NotificationManager.shared.requestPermission(); message = "Check the registration status below. You can change notification permission in system settings." }
                            catch { message = error.localizedDescription }
                        }
                    }
                    Text(NotificationManager.shared.status).font(.caption).foregroundStyle(.secondary)
                    Text("Your relay keeps checking the rule when Airkeep is closed. Remote alerts need APNs configured on the relay and notifications enabled for this app.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if let message { Text(message).font(.caption).foregroundStyle(.orange) }
            }.formStyle(.grouped).navigationTitle("Humidity alerts")
                .onAppear { rule = model.snapshot?.alertRule ?? AlertRule(); rule.amsID = model.selectedAMS }
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                    ToolbarItem(placement: .confirmationAction) {
                        Button("Save") {
                            Task {
                                saving = true
                                defer { saving = false }
                                do { try await model.saveRule(rule); dismiss() }
                                catch { message = error.localizedDescription }
                            }
                        }.disabled(saving || model.demo)
                    }
                }
        }.preferredColorScheme(.dark).tint(AirkeepTheme.amber)
        #if os(macOS)
        .frame(width: 500, height: 580)
        #endif
    }
}
