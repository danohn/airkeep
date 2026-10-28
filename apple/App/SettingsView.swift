import SwiftUI
#if os(macOS)
import ServiceManagement
#endif

struct SettingsView: View {
    @Bindable var model: MonitorModel
    @Environment(\.dismiss) private var dismiss
    @Environment(\.scenePhase) private var scenePhase
    @State private var selection: MonitorModel.SettingsSection? = .alerts
    @State private var path: [MonitorModel.SettingsSection] = []
    #if os(macOS)
    @State private var loginStatus = SMAppService.mainApp.status
    @State private var loginError: String?
    #endif

    var body: some View {
        Group {
            #if os(macOS)
            NavigationSplitView {
                List(MonitorModel.SettingsSection.allCases, selection: $selection) { section in
                    Label(section.title, systemImage: section.symbol).tag(section).listItemTint(AirkeepTheme.amber)
                }
                .navigationTitle("Settings")
                .navigationSplitViewColumnWidth(min: 170, ideal: 190, max: 220)
                .safeAreaInset(edge: .bottom) { launchAtLogin.padding(14) }
            } detail: {
                if let selection { page(selection) }
            }
            .frame(minWidth: 680, idealWidth: 740, minHeight: 610, idealHeight: 680)
            #else
            NavigationStack(path: $path) {
                List {
                    Section("On your relay") { destination(.alerts) }
                    Section("This device") {
                        destination(.notifications)
                        destination(.liveActivities)
                    }
                    Section { destination(.relay) }
                }
                .navigationTitle("Settings")
                .navigationDestination(for: MonitorModel.SettingsSection.self) { page($0) }
                .toolbar { ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } } }
            }
            #endif
        }
        .preferredColorScheme(.dark).tint(AirkeepTheme.amber)
        .onAppear {
            model.settingsVisible = true
            if let section = model.settingsSection { selection = section; path = [section] }
        }
        .onDisappear { model.settingsVisible = false }
        .onChange(of: model.settingsSection) { _, section in
            if let section { selection = section; path = [section] }
        }
        .onChange(of: scenePhase) { _, phase in
            if phase == .active {
                #if os(macOS)
                loginStatus = SMAppService.mainApp.status
                #endif
                if !model.demo { Task { await refresh() } }
            }
        }
        .task {
            guard !model.demo else { return }
            await refresh()
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(5)) } catch { break }
                if model.settingsVisible { await model.alertRules.refresh() }
            }
        }
    }

    @ViewBuilder private func page(_ section: MonitorModel.SettingsSection) -> some View {
        switch section {
        case .alerts: AlertsView(model: model)
        case .notifications: DeviceNotificationsView(model: model)
        case .liveActivities: LiveActivitiesView(model: model)
        case .relay: RelaySettingsView(model: model)
        }
    }
    private func destination(_ section: MonitorModel.SettingsSection) -> some View {
        NavigationLink(value: section) {
            HStack {
                Label(section.title, systemImage: section.symbol)
                Spacer()
                Text(summary(section)).font(.caption).foregroundStyle(.secondary)
            }
        }
    }
    private func summary(_ section: MonitorModel.SettingsSection) -> String {
        if model.demo { return "Preview" }
        switch section {
        case .alerts: return "\(model.alertRules.rules.filter(\.enabled).count) enabled"
        case .notifications:
            let notifications = NotificationManager.shared
            guard let enabled = notifications.receiveAlerts else { return "Checking…" }
            if !enabled { return "Off" }
            return notifications.permission.allowsNotifications && notifications.registration == .registered && notifications.server == .available ? "On" : "Needs attention"
        case .relay: return model.relayReachable ? "Connected" : "Check connection"
        case .liveActivities:
            #if os(iOS)
            return LiveMonitor.shared.currentActivity == nil ? "Not active" : "Active"
            #else
            return "On iPhone"
            #endif
        }
    }
    private func refresh() async {
        await model.alertRules.refresh()
        await NotificationManager.shared.refresh()
        #if os(iOS)
        LiveMonitor.shared.observeAuthorization()
        #endif
    }
    #if os(macOS)
    private var launchAtLogin: some View {
        VStack(alignment: .leading, spacing: 8) {
            Toggle("Launch at login", isOn: Binding(
                get: { loginStatus == .enabled || loginStatus == .requiresApproval },
                set: { enabled in
                    do {
                        if enabled { try SMAppService.mainApp.register() }
                        else { try SMAppService.mainApp.unregister() }
                        loginError = nil
                    } catch { loginError = error.localizedDescription }
                    loginStatus = SMAppService.mainApp.status
                }))
            if loginStatus == .requiresApproval {
                Text("Approval needed in Login Items.").font(.caption).foregroundStyle(.secondary)
                Button("Open Login Items") { SMAppService.openSystemSettingsLoginItems() }
            }
            if let loginError { Text(loginError).font(.caption).foregroundStyle(.orange) }
        }.font(.callout)
    }
    #endif
}

struct DeviceNotificationsView: View {
    var model: MonitorModel
    @State private var attemptedSubscription: Bool?
    private var notifications: NotificationManager { .shared }
    var body: some View {
        Form {
            Section {
                Toggle("Receive alerts on this device", isOn: Binding(
                    get: { notifications.receiveAlerts ?? false },
                    set: { enabled in
                        attemptedSubscription = enabled
                        Task { await notifications.setReceiveAlerts(enabled) }
                    }))
                    .disabled(model.demo || !notifications.canChangeSubscription)
                if notifications.isChangingSubscription { ProgressView("Waiting for relay confirmation…") }
                Text("Get humidity and recovery alerts from your enabled rules on this device.")
                    .font(.caption).foregroundStyle(.secondary)
                if !model.demo && !notifications.supportsSubscription && !notifications.isRefreshing {
                    Text("Update your relay to manage alerts on this device.").font(.caption).foregroundStyle(.orange)
                }
                if let message = notifications.subscriptionError {
                    Text(message).font(.caption).foregroundStyle(.orange)
                    if let attemptedSubscription {
                        Button("Retry change") { Task { await notifications.setReceiveAlerts(attemptedSubscription) } }
                            .disabled(notifications.isChangingSubscription || model.demo)
                    }
                }
            }
            Section("System permission") {
                LabeledContent("Notifications", value: model.demo ? "Preview" : notifications.permission.title)
                if notifications.permission.authorization == .notAsked {
                    Button("Allow notifications") { Task { await notifications.requestPermission() } }
                        .disabled(model.demo || notifications.isRequestingPermission)
                } else {
                    Button("Notification settings") { notifications.openSettings() }.disabled(model.demo)
                }
                if notifications.permission.allowsNotifications {
                    LabeledContent("Banners", value: notifications.permission.banners ? "On" : "Off")
                    LabeledContent("Sounds", value: notifications.permission.sounds ? "On" : "Off")
                    deliveryRow("Lock Screen", notifications.permission.lockScreen)
                    deliveryRow("Notification Centre", notifications.permission.notificationCenter)
                    deliveryRow("Badges", notifications.permission.badges)
                }
                if let error = notifications.permissionError { Text(error).font(.caption).foregroundStyle(.orange) }
            }
            Section("Relay registration") {
                LabeledContent("This device", value: model.demo ? "Preview" : notifications.registration.title)
                if notifications.canRetryRegistration {
                    Button("Retry registration") { Task { await notifications.retryRegistration() } }
                        .disabled(model.demo || notifications.isRefreshing)
                }
                if let error = notifications.registrationError { Text(error).font(.caption).foregroundStyle(.orange) }
                Text(notifications.summary).font(.caption).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped).navigationTitle("Notifications")
        .task { if !model.demo { await notifications.refresh() } }
    }
    @ViewBuilder private func deliveryRow(_ title: String, _ setting: NotificationManager.DeliverySetting) -> some View {
        if setting != .unsupported { LabeledContent(title, value: setting.title) }
    }
}

struct RelaySettingsView: View {
    var model: MonitorModel
    @State private var editConnection = false
    private var notifications: NotificationManager { .shared }
    var body: some View {
        Form {
            Section("Connection") {
                LabeledContent("Status", value: model.demo ? "Preview" : model.relayCheckedAt == nil ? "Checking…" : model.relayReachable ? "Connected" : "Unavailable")
                if !model.demo {
                    LabeledContent("Relay URL") {
                        Text(SharedConfiguration.endpoint.isEmpty ? "Not configured" : SharedConfiguration.endpoint)
                            .textSelection(.enabled).multilineTextAlignment(.trailing)
                    }
                }
                Button("Edit connection") { editConnection = true }.disabled(model.demo)
                Button("Check connection") { Task { await model.refresh(forceHistory: true); await notifications.refresh() } }
                    .disabled(model.demo || model.refreshing)
                if let error = model.error { Text(error).font(.caption).foregroundStyle(.orange) }
            }
            Section("Apple push setup") {
                LabeledContent("Configuration", value: model.demo ? "Preview" : notifications.server.title)
                Text(notifications.server.detail).font(.caption).foregroundStyle(.secondary)
                DisclosureGroup("Advanced") {
                    LabeledContent("Push environment", value: SharedConfiguration.environment == "sandbox" ? "Development" : "Production")
                    LabeledContent("App topic", value: Bundle.main.bundleIdentifier ?? "Unknown")
                }
            }
            #if os(iOS)
            WatchCompanionSettings(amsID: model.selectedAMS, demo: model.demo)
            #endif
            Section("Privacy") {
                Text("Your connection token is stored securely in Keychain.")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
        .formStyle(.grouped).navigationTitle("Relay")
        .sheet(isPresented: $editConnection) { ConnectionView(model: model) }
        .task { if !model.demo { await model.refresh(); await notifications.refresh() } }
    }
}

#if os(iOS)
private struct WatchCompanionSettings: View {
    var amsID: String
    var demo: Bool
    private var watch: WatchCompanion { .shared }
    var body: some View {
        Section("Apple Watch") {
            LabeledContent("Connection", value: demo ? "Preview" : watch.waiting ? "Waiting for Apple Watch…" : watch.error != nil ? "Needs attention" : watch.connected ? "Connected" : "Not connected")
            if !watch.paired {
                Text("Pair an Apple Watch with this iPhone to connect Airkeep.").font(.caption).foregroundStyle(.secondary)
            } else if !watch.installed {
                Text("Install Airkeep on your Apple Watch to connect.").font(.caption).foregroundStyle(.secondary)
            }
            Button(watch.connected ? "Update Watch connection" : "Connect Apple Watch") {
                Task { await watch.connect(amsID: amsID) }
            }.disabled(demo || !watch.ready || !watch.paired || !watch.installed || watch.waiting)
            if watch.waiting || watch.error != nil { Button("Retry") { watch.retry() }.disabled(demo) }
            if watch.connected || watch.waiting {
                Button("Disconnect Apple Watch", role: .destructive) { watch.disconnect() }.disabled(demo)
            }
            if let error = watch.error { Text(error).font(.caption).foregroundStyle(.orange) }
        }.task { watch.activate() }
    }
}
#endif
