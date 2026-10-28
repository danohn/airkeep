import SwiftUI
#if os(iOS)
import UIKit
#endif

enum AirkeepTheme {
    static let amber = Color(red: 1, green: 0.72, blue: 0.18)
    static let ivory = Color(red: 0.96, green: 0.94, blue: 0.90)
    static let background = Color(red: 0.055, green: 0.06, blue: 0.06)
    static let secondary = Color(red: 0.62, green: 0.62, blue: 0.60)
}

struct AirkeepWordmark: View {
    var size: CGFloat = 34

    var body: some View {
        Text("Airkeep")
            .font(.system(size: size, weight: .medium, design: .serif))
            .lineLimit(1)
            .minimumScaleFactor(0.75)
    }
}

struct AirkeepActionButton: View {
    enum Kind {
        case settings, humidityAlerts

        var title: String {
            switch self {
            case .settings: "Settings"
            case .humidityAlerts: "Humidity alerts"
            }
        }
        var symbol: String {
            switch self {
            case .settings: "gearshape"
            case .humidityAlerts: "bell"
            }
        }
    }

    var kind: Kind
    var action: () -> Void

    var body: some View {
        Button(action: action) {
            if kind == .settings {
                Label(kind.title, systemImage: kind.symbol).labelStyle(.iconOnly)
            } else {
                Label(kind.title, systemImage: kind.symbol)
            }
        }
        .buttonStyle(AirkeepActionStyle(iconOnly: kind == .settings))
        .accessibilityLabel(kind.title)
        .help(kind.title)
    }
}

struct AirkeepActionStyle: ButtonStyle {
    var iconOnly = false
    @Environment(\.isEnabled) private var isEnabled
    @ScaledMetric(relativeTo: .body) private var labelSize: CGFloat = 15
    @ScaledMetric(relativeTo: .body) private var minimumSize: CGFloat = 44
    @State private var hovered = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: labelSize, weight: .medium))
            .foregroundStyle(AirkeepTheme.ivory)
            .padding(.horizontal, iconOnly ? 0 : 16)
            .padding(.vertical, 10)
            .frame(width: iconOnly ? minimumSize : nil)
            .frame(minHeight: minimumSize)
            .background(AirkeepTheme.ivory.opacity(configuration.isPressed ? 0.18 : hovered ? 0.12 : 0.06), in: Capsule())
            .overlay(Capsule().strokeBorder(AirkeepTheme.ivory.opacity(configuration.isPressed || hovered ? 0.28 : 0.16)))
            .contentShape(Capsule())
            .opacity(isEnabled ? 1 : 0.4)
            .onHover { hovered = $0 }
    }
}

#if os(iOS)
struct AirkeepHistoryRangeControl: UIViewRepresentable {
    @Binding var selection: Int
    private static let hours = [1, 6, 24]

    func makeUIView(context: Context) -> UISegmentedControl {
        let control = UISegmentedControl(items: Self.hours.map { "\($0)h" })
        control.selectedSegmentTintColor = UIColor(AirkeepTheme.amber)
        control.setTitleTextAttributes([.foregroundColor: UIColor(AirkeepTheme.background)], for: .selected)
        control.setTitleTextAttributes([.foregroundColor: UIColor(AirkeepTheme.ivory)], for: .normal)
        control.accessibilityLabel = "History range"
        control.addTarget(context.coordinator, action: #selector(Coordinator.selectionChanged(_:)), for: .valueChanged)
        return control
    }

    func updateUIView(_ control: UISegmentedControl, context: Context) {
        context.coordinator.parent = self
        control.selectedSegmentIndex = Self.hours.firstIndex(of: selection) ?? UISegmentedControl.noSegment
    }

    func makeCoordinator() -> Coordinator { Coordinator(parent: self) }

    @MainActor final class Coordinator: NSObject {
        var parent: AirkeepHistoryRangeControl
        init(parent: AirkeepHistoryRangeControl) { self.parent = parent }

        @objc func selectionChanged(_ control: UISegmentedControl) {
            guard AirkeepHistoryRangeControl.hours.indices.contains(control.selectedSegmentIndex) else { return }
            parent.selection = AirkeepHistoryRangeControl.hours[control.selectedSegmentIndex]
        }
    }
}
#endif

struct HumidityValue: View {
    var reading: Reading?
    var size: CGFloat = 110
    var stale = false
    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(reading?.humidityText ?? "—")
                .font(.system(size: size, weight: .bold, design: .rounded))
                .monospacedDigit()
                .foregroundStyle(stale ? AirkeepTheme.secondary : AirkeepTheme.ivory)
                .minimumScaleFactor(0.5).lineLimit(1)
            Text("% RH").font(.system(size: size * 0.22, weight: .semibold)).foregroundStyle(AirkeepTheme.amber)
                .lineLimit(1).fixedSize(horizontal: true, vertical: false)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Relative humidity")
        .accessibilityValue(reading.map { "\($0.humidityText) percent\(stale ? ", stale" : "")" } ?? "No reading")
    }
}
