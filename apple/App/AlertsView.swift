import SwiftUI
import UserNotifications

struct AlertsView: View {
    var model: MonitorModel
    @Environment(\.dismiss) private var dismiss
    @Environment(\.scenePhase) private var scenePhase
    private var notifications: NotificationManager { .shared }
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
                    Text("Your relay applies this rule to registered devices. It sends one alert after humidity stays above the limit, then one after recovery. Old readings reset the hold timer.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Section("Notifications") {
                    #if os(macOS)
                    LabeledContent("On this Mac", value: model.demo ? "Preview" : notifications.permission.title)
                    #else
                    LabeledContent("On this device", value: model.demo ? "Preview" : notifications.permission.title)
                    #endif
                    if !model.demo && notifications.permission.authorization == .notAsked {
                        Button("Enable notifications") { Task { await notifications.requestPermission() } }
                            .disabled(notifications.isRequestingPermission || model.demo)
                    } else if !model.demo && notifications.permission.authorization != .checking {
                        Button("Notification settings") { notifications.openSettings() }
                            .disabled(model.demo)
                    }
                    LabeledContent("Relay registration", value: model.demo ? "Preview" : notifications.registration.title)
                    if notifications.canRetryRegistration {
                        Button("Retry registration") { Task { await notifications.retryRegistration() } }
                            .disabled(notifications.isRefreshing || model.demo)
                    }
                    LabeledContent("Server push setup", value: model.demo ? "Preview" : notifications.server.title)
                    if !model.demo {
                        if let error = notifications.permissionError ?? notifications.registrationError {
                            Text(error).font(.caption).foregroundStyle(.orange)
                        }
                        Text(notifications.summary).font(.caption).foregroundStyle(.secondary)
                        DisclosureGroup("Server details") {
                            LabeledContent("Push environment", value: SharedConfiguration.environment == "sandbox" ? "Development" : "Production")
                            Text(notifications.server.detail).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    Text("Device notification changes take effect immediately. Save applies the humidity rule; Cancel discards rule edits.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if let message { Text(message).font(.caption).foregroundStyle(.orange) }
            }.formStyle(.grouped).navigationTitle("Humidity alerts")
                .onAppear { rule = model.snapshot?.alertRule ?? AlertRule(); rule.amsID = model.selectedAMS }
                .task { if !model.demo { await notifications.refresh() } }
                .onChange(of: scenePhase) { _, phase in
                    if phase == .active && !model.demo { Task { await notifications.refresh() } }
                }
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
        .frame(width: 500, height: 700)
        #endif
    }
}
