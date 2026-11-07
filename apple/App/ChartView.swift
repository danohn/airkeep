import SwiftUI
import Charts

struct HumidityChart: View {
    var readings: [Reading]

    var body: some View {
        let values = readings.map(\.humidity)
        if let low = values.min(), let high = values.max() {
            history(low: low, high: high)
        } else {
            VStack(spacing: 8) {
                Image(systemName: "chart.xyaxis.line").font(.title2)
                Text("History appears as readings arrive").font(.caption)
            }.foregroundStyle(AirkeepTheme.secondary).frame(maxWidth: .infinity, minHeight: 160)
        }
    }

    private func history(low: Double, high: Double) -> some View {
        // Leave at least two points of breathing room and keep quiet days at a readable scale.
        let padding = max(2, (10 - (high - low)) / 2)
        let span = high - low + 2 * padding
        let step = [2.0, 5, 10, 20].first { span <= $0 * 6 } ?? 20
        let lower = min(90, max(0, floor((low - padding) / step) * step))
        let upper = max(10, min(100, ceil((high + padding) / step) * step))
        let format = FloatingPointFormatStyle<Double>.number.precision(.fractionLength(0...1))
        let range = low == high ? low.formatted(format) : "\(low.formatted(format))–\(high.formatted(format))"
        #if os(watchOS)
        let tickStep = step * 2
        let timeTickCount = 3
        #else
        let tickStep = step
        let timeTickCount = 4
        #endif

        return VStack(alignment: .leading, spacing: 8) {
            Text("Range · \(range)% RH")
                .font(.caption2).foregroundStyle(AirkeepTheme.secondary)
            Chart(readings) { reading in
                LineMark(x: .value("Time", reading.date), y: .value("Humidity", reading.humidity))
                    .foregroundStyle(AirkeepTheme.amber).lineStyle(StrokeStyle(lineWidth: 1.5))
            }
            .chartYScale(domain: lower...upper)
            .chartYAxis {
                AxisMarks(values: Array(stride(from: lower, through: upper, by: tickStep))) { _ in
                    AxisGridLine().foregroundStyle(.white.opacity(0.06))
                    AxisValueLabel().foregroundStyle(AirkeepTheme.secondary)
                }
            }
            .chartXAxis {
                AxisMarks(values: .automatic(desiredCount: timeTickCount)) { _ in
                    AxisValueLabel(format: .dateTime.hour().minute()).foregroundStyle(AirkeepTheme.secondary)
                }
            }
            .accessibilityLabel("Relative humidity history, percent")
        }
    }
}
