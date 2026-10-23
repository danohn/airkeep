import SwiftUI

enum AirkeepTheme {
    static let amber = Color(red: 1, green: 0.72, blue: 0.18)
    static let ivory = Color(red: 0.96, green: 0.94, blue: 0.90)
    static let background = Color(red: 0.055, green: 0.06, blue: 0.06)
    static let secondary = Color(red: 0.62, green: 0.62, blue: 0.60)
}

struct HumidityValue: View {
    var reading: Reading?
    var size: CGFloat = 110
    var stale = false
    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(reading?.humidityText ?? "—")
                .font(.system(size: size, weight: .bold, design: .rounded))
                .monospacedDigit().tracking(-size * 0.065)
                .foregroundStyle(stale ? AirkeepTheme.secondary : AirkeepTheme.ivory)
                .minimumScaleFactor(0.5).lineLimit(1)
            Text("% RH").font(.system(size: size * 0.22, weight: .semibold)).foregroundStyle(AirkeepTheme.amber)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Relative humidity")
        .accessibilityValue(reading.map { "\($0.humidityText) percent\(stale ? ", stale" : "")" } ?? "No reading")
    }
}
