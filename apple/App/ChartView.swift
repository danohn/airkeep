import SwiftUI
import Charts

struct HumidityChart: View {
    var readings: [Reading]
    var compact = false
    private var upperBound: Double {
        max(60, ceil((readings.map(\.humidity).max() ?? 60) / 20) * 20)
    }
    var body: some View {
        if readings.isEmpty {
            VStack(spacing: 8) {
                Image(systemName: "chart.xyaxis.line").font(.title2)
                Text("History appears as readings arrive").font(.caption)
            }.foregroundStyle(AirkeepTheme.secondary).frame(maxWidth: .infinity, minHeight: compact ? 40 : 160)
        } else {
            Chart(readings) { reading in
                LineMark(x: .value("Time", reading.date), y: .value("Humidity", reading.humidity))
                    .foregroundStyle(AirkeepTheme.amber).lineStyle(StrokeStyle(lineWidth: compact ? 2 : 2.5))
            }
            .chartYScale(domain: 0...upperBound)
            .chartXAxis(compact ? .hidden : .automatic)
            .chartYAxis(compact ? .hidden : .automatic)
            .chartYAxis {
                if !compact {
                    AxisMarks(values: [0.0, 20, 40, 60, 80, 100].filter { $0 <= upperBound }) { _ in
                        AxisGridLine().foregroundStyle(.white.opacity(0.06))
                        AxisValueLabel().foregroundStyle(AirkeepTheme.secondary)
                    }
                }
            }
            .chartXAxis {
                if !compact {
                    AxisMarks(values: .automatic(desiredCount: 4)) { _ in
                        AxisValueLabel(format: .dateTime.hour().minute()).foregroundStyle(AirkeepTheme.secondary)
                    }
                }
            }
            .accessibilityLabel("Relative humidity history, percent")
        }
    }
}
