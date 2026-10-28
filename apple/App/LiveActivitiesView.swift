import SwiftUI
#if os(iOS)
import UIKit
#endif

struct LiveActivitiesView: View {
    var model: MonitorModel
    @State private var error: String?
    #if os(iOS)
    private var live: LiveMonitor { .shared }
    private var canStart: Bool {
        live.activitiesAllowed && !model.isStale && model.reading != nil && !model.demo &&
        NotificationManager.shared.server == .available && !live.isStarting
    }
    #endif

    var body: some View {
        #if os(iOS)
        Form {
            Section("On this device") {
                LabeledContent("Live Activities", value: model.demo ? "Preview" : live.activitiesAllowed ? "Allowed" : "Not currently allowed")
                Button("Open system settings") {
                    if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) }
                }.disabled(model.demo)
                Text("Follow humidity on your Lock Screen and Dynamic Island on supported iPhones.")
                    .font(.caption).foregroundStyle(.secondary)
                if live.requestsFrequentUpdates {
                    LabeledContent("Frequent updates", value: live.frequentUpdatesAllowed ? "Allowed" : "Off in Settings")
                }
            }
            Section("Session") {
                if let activity = live.currentActivity {
                    LabeledContent("AMS", value: activity.attributes.amsName)
                    LabeledContent("Ends", value: activity.attributes.endsAt.formatted(date: .abbreviated, time: .shortened))
                    LabeledContent("Background updates", value: live.updateRegistration.state.title)
                    if case .failed(let message) = live.updateRegistration.state {
                        Text(message).font(.caption).foregroundStyle(.orange)
                        Button("Retry update registration") { live.updateRegistration.retry() }
                    }
                    Button("End Live Activity", role: .destructive) { Task { await live.stopAll() } }
                } else {
                    LabeledContent("AMS", value: model.reading?.amsName ?? "Waiting for a reading")
                    Text("Monitor the AMS selected on your dashboard for eight hours.").font(.caption).foregroundStyle(.secondary)
                    Button(live.isStarting ? "Starting…" : "Start Live Activity") {
                        Task {
                            do { try await live.start(model.reading, demo: model.demo, staleAfter: model.snapshot?.staleAfter ?? 90); error = nil }
                            catch { self.error = error.localizedDescription }
                        }
                    }.disabled(!canStart)
                    if !live.activitiesAllowed { Text("Allow Live Activities in system settings to start a session.").font(.caption).foregroundStyle(.orange) }
                    else if model.isStale { Text("A fresh relay reading is needed before starting.").font(.caption).foregroundStyle(.orange) }
                    else if NotificationManager.shared.server != .available {
                        Text("Check Apple push setup under Relay before starting.").font(.caption).foregroundStyle(.orange)
                    }
                }
                if let error { Text(error).font(.caption).foregroundStyle(.orange) }
            }
            Section("Updates") {
                Text("Readings update about every five minutes.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped).navigationTitle("Live Activities")
        .task {
            live.observeAuthorization()
            if !model.demo { await NotificationManager.shared.refresh(); await model.refresh() }
        }
        #else
        Form {
            Section("Managed on iPhone") {
                Label("Start Live Activities in Airkeep on your iPhone.", systemImage: "iphone")
                Text("Follow your iPhone's Live Activity from your Mac's menu bar or a paired Apple Watch.")
                    .font(.callout).foregroundStyle(.secondary)
            }
        }.formStyle(.grouped).navigationTitle("Live Activities")
        #endif
    }
}
