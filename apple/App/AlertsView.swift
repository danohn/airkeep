import SwiftUI

struct AlertsView: View {
    var model: MonitorModel
    private var alerts: AlertRulesModel { model.alertRules }
    @State private var editing: ManagedAlertRule?
    @State private var creating = false
    @State private var deleting: ManagedAlertRule?
    @State private var actionError: String?

    var body: some View {
        List {
            Section {
                Text("Your alerts sync across devices through this relay. Choose which devices receive them in Notifications.")
                    .font(.callout).foregroundStyle(.secondary)
                if alerts.isRefreshing && alerts.lastSynced == nil { ProgressView("Loading alerts…") }
                if let message = alerts.error {
                    Text(message).font(.caption).foregroundStyle(.orange)
                    if alerts.lastSynced != nil { Text("Showing cached rules. Reconnect before saving changes.").font(.caption).foregroundStyle(.secondary) }
                    Button("Retry") { Task { await alerts.refresh() } }.disabled(alerts.isRefreshing || model.demo)
                }
                if let synced = alerts.lastSynced {
                    Text("Last synced \(synced, style: .relative) ago").font(.caption).foregroundStyle(.secondary)
                }
            }
            Section("Rules") {
                if alerts.rules.isEmpty && !alerts.isRefreshing {
                    Text("Add your first alert to choose its humidity limit and hold time.").foregroundStyle(.secondary)
                }
                ForEach(alerts.rules) { rule in
                    VStack(alignment: .leading, spacing: 8) {
                        HStack {
                            Button {
                                creating = false
                                editing = rule
                            } label: {
                                VStack(alignment: .leading, spacing: 5) {
                                    Text(rule.name).font(.headline).foregroundStyle(AirkeepTheme.ivory)
                                    Text(rule.conditionSummary).font(.caption).foregroundStyle(.secondary)
                                }.frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
                            }.buttonStyle(.plain)
                            if alerts.busyIDs.contains(rule.id) { ProgressView().controlSize(.small) }
                            Toggle("Enable \(rule.name)", isOn: Binding(get: { rule.enabled }, set: { enabled in
                                var updated = rule
                                updated.enabled = enabled
                                Task {
                                    do { _ = try await alerts.save(updated) }
                                    catch { actionError = error.localizedDescription }
                                }
                            })).labelsHidden().disabled(!alerts.canEdit || alerts.busyIDs.contains(rule.id) || model.demo)
                        }
                        HStack {
                            Text(!rule.enabled ? "Off" : rule.active ? "Humidity above limit" : "Watching humidity")
                                .font(.caption).foregroundStyle(rule.enabled && rule.active ? AirkeepTheme.amber : AirkeepTheme.secondary)
                            Spacer()
                            Button("Edit") { creating = false; editing = rule }.font(.caption)
                            Button("Delete", role: .destructive) { deleting = rule }.font(.caption)
                                .disabled(!alerts.canEdit || alerts.busyIDs.contains(rule.id) || model.demo)
                        }
                    }.padding(.vertical, 6)
                }
                Button {
                    creating = true
                    editing = ManagedAlertRule(name: alerts.nextName, amsID: model.selectedAMS)
                } label: { Label("Add alert", systemImage: "plus") }
                    .disabled(!alerts.canEdit || model.demo)
            }
        }
        .navigationTitle("Humidity alerts")
        .refreshable { if !model.demo { await alerts.refresh() } }
        .sheet(item: $editing) { rule in AlertRuleEditor(model: model, original: rule, creating: creating) }
        .confirmationDialog("Delete \(deleting?.name ?? "alert")?", isPresented: Binding(
            get: { deleting != nil }, set: { if !$0 { deleting = nil } }), titleVisibility: .visible) {
                Button("Delete alert", role: .destructive) {
                    guard let rule = deleting else { return }
                    deleting = nil
                    Task {
                        do { try await alerts.delete(rule) }
                        catch { actionError = error.localizedDescription }
                    }
                }
            } message: { Text("This deletes the alert from all devices connected to this relay.") }
        .alert("Could not update alert", isPresented: Binding(get: { actionError != nil }, set: { if !$0 { actionError = nil } })) {
            Button("OK") { actionError = nil }
        } message: { Text(actionError ?? "") }
    }
}

struct AlertRuleEditor: View {
    var model: MonitorModel
    var creating: Bool
    private let relayEndpoint: String
    @Environment(\.dismiss) private var dismiss
    @State private var draft: ManagedAlertRule
    @State private var baseline: ManagedAlertRule
    @State private var saving = false
    @State private var error: String?
    @State private var conflict = false
    @State private var removed = false
    @State private var discard = false
    private var dirty: Bool { draft != baseline }
    private var holds: [Int] { Array(Set([0, 60, 300, 900, draft.holdSeconds])).sorted() }
    private var units: [String] {
        Array(Set((model.snapshot?.readings.map(\.amsID) ?? []) + model.alertRules.rules.map(\.amsID) + [draft.amsID])).sorted()
    }

    init(model: MonitorModel, original: ManagedAlertRule, creating: Bool) {
        self.model = model; self.creating = creating
        relayEndpoint = SharedConfiguration.endpoint
        _draft = State(initialValue: original); _baseline = State(initialValue: original)
    }
    var body: some View {
        NavigationStack {
            Form {
                Section("Alert") {
                    TextField("Name", text: $draft.name)
                    Toggle("Enabled", isOn: $draft.enabled)
                    Picker("AMS", selection: $draft.amsID) {
                        ForEach(units, id: \.self) { Text("AMS \((Int($0) ?? 0) + 1)").tag($0) }
                    }
                }
                Section("Condition") {
                    Stepper("Alert above \(Int(draft.threshold))% RH", value: $draft.threshold, in: 5...95, step: 1)
                    Picker("Stay above limit for", selection: $draft.holdSeconds) {
                        ForEach(holds, id: \.self) { Text($0 == 0 ? "Immediately" : "\($0 / 60) minutes").tag($0) }
                    }
                    Stepper("Reset \(Int(draft.hysteresis))% below limit", value: $draft.hysteresis, in: 1...min(20, draft.threshold - 1), step: 1)
                    Text("Get an alert when humidity stays above your limit, then another when it recovers. Changing the condition restarts the hold time.")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if relayEndpoint != SharedConfiguration.endpoint {
                    Text("The relay connection changed. Close this draft and reopen the rule.")
                        .font(.caption).foregroundStyle(.orange)
                } else if !model.alertRules.canEdit {
                    Text("Connect to your relay to save. Your draft will stay here while you reconnect.")
                        .font(.caption).foregroundStyle(.orange)
                }
                if let error {
                    Section {
                        Text(error).font(.callout).foregroundStyle(.orange)
                        if conflict { Button("Reload latest rule") { Task { await reload() } } }
                    }
                }
            }
            .formStyle(.grouped).navigationTitle(creating ? "New humidity alert" : "Edit humidity alert")
            .disabled(saving)
            .onChange(of: model.alertRules.rules) { _, rules in
                guard !creating, !saving, model.alertRules.isOnline, relayEndpoint == SharedConfiguration.endpoint else { return }
                if let latest = rules.first(where: { $0.id == draft.id }) {
                    if latest.revision != baseline.revision {
                        conflict = true
                        error = "This alert changed on another device. Your draft is still here. Reload the latest rule before saving."
                    }
                } else {
                    removed = true
                    error = "This rule was removed on another device. Your changes haven't been saved."
                }
            }
            .onChange(of: draft.threshold) { _, threshold in
                if draft.hysteresis >= threshold { draft.hysteresis = threshold - 1 }
            }
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button(removed ? "Close" : "Cancel") {
                        if dirty && !removed { discard = true } else { dismiss() }
                    }.disabled(saving)
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button(saving ? "Saving…" : "Save") { Task { await save() } }
                        .disabled(saving || removed || model.demo || !model.alertRules.canEdit || relayEndpoint != SharedConfiguration.endpoint || draft.name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || draft.name.count > 80)
                }
            }
        }
        .preferredColorScheme(.dark).tint(AirkeepTheme.amber)
        .interactiveDismissDisabled(dirty || saving)
        .confirmationDialog("Discard your changes?", isPresented: $discard, titleVisibility: .visible) {
            Button("Discard changes", role: .destructive) { dismiss() }
        }
        #if os(macOS)
        .frame(width: 510, height: 570)
        #endif
    }
    private func save() async {
        guard relayEndpoint == SharedConfiguration.endpoint else { error = "The relay connection changed. Close this draft and reopen the rule."; return }
        saving = true
        defer { saving = false }
        do {
            draft.name = draft.name.trimmingCharacters(in: .whitespacesAndNewlines)
            _ = try await model.alertRules.save(draft, creating: creating)
            dismiss()
        } catch {
            conflict = (error as? RelayClient.RelayError) == .http(412)
            removed = !creating && (error as? RelayClient.RelayError) == .http(404)
            self.error = removed ? "This rule was removed on another device. Your changes haven't been saved." : error.localizedDescription
        }
    }
    private func reload() async {
        guard relayEndpoint == SharedConfiguration.endpoint else { error = "The relay connection changed. Close this draft and reopen the rule."; return }
        saving = true
        defer { saving = false }
        do {
            let latest = try await model.alertRules.reload(draft.id)
            draft = latest; baseline = latest; error = nil; conflict = false
        } catch {
            removed = (error as? RelayClient.RelayError) == .http(404)
            self.error = removed ? "This rule was removed on another device." : error.localizedDescription
        }
    }
}
