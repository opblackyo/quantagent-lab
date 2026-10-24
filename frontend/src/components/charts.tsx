"use client";
import { useEffect, useRef } from "react";
import {
  CandlestickSeries,
  createChart,
  createSeriesMarkers,
  LineSeries,
  ColorType,
  type Time,
} from "lightweight-charts";
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
  Area,
  AreaChart,
} from "recharts";
import {
  type Bar,
  type Equity,
  type Trade,
  type Comparison,
  money,
  strategyColors,
  strategyNames,
} from "@/lib/api";

const grid = "#e9edf3";
export function EquityChart({
  points,
  initial,
}: {
  points: Equity[];
  initial: number;
}) {
  return (
    <div
      className="equity-chart"
      data-testid="equity-chart"
      role="img"
      aria-label="每日收盤資產曲線"
    >
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart
          data={points}
          margin={{ top: 15, right: 12, left: 5, bottom: 0 }}
        >
          <defs>
            <linearGradient id="equity-fill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#3157c8" stopOpacity={0.14} />
              <stop offset="100%" stopColor="#3157c8" stopOpacity={0} />
            </linearGradient>
          </defs>
          <CartesianGrid stroke={grid} vertical={false} />
          <XAxis
            dataKey="session_date"
            minTickGap={55}
            tickFormatter={(v) => v.slice(5)}
            axisLine={false}
            tickLine={false}
            tick={{ fill: "#7c8b9d", fontSize: 11 }}
          />
          <YAxis
            domain={["auto", "auto"]}
            tickFormatter={(v) => `${(v / 1000).toFixed(0)}k`}
            axisLine={false}
            tickLine={false}
            tick={{ fill: "#7c8b9d", fontSize: 11 }}
            width={48}
          />
          <Tooltip
            formatter={(v) => [money(Number(v), 2), "資產淨值"]}
            contentStyle={{ borderRadius: 8, borderColor: grid }}
          />
          <ReferenceLine y={initial} stroke="#b6c0d0" strokeDasharray="4 4" />
          <Area
            type="monotone"
            dataKey="equity"
            stroke="#3157c8"
            strokeWidth={2.5}
            fill="url(#equity-fill)"
            isAnimationActive={false}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
}

export function MarketChart({
  bars,
  trades,
}: {
  bars: Bar[];
  trades: Trade[];
}) {
  const container = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!container.current || !bars.length) return;
    const chart = createChart(container.current, {
      height: 360,
      width: container.current.clientWidth,
      layout: {
        background: { type: ColorType.Solid, color: "#ffffff" },
        textColor: "#718196",
        attributionLogo: true,
      },
      grid: {
        vertLines: { color: "#f3f5f8" },
        horzLines: { color: "#edf1f6" },
      },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false, timeVisible: false },
      crosshair: {
        vertLine: { color: "#9cabc1" },
        horzLine: { color: "#9cabc1" },
      },
    });
    const candle = chart.addSeries(CandlestickSeries, {
      upColor: "#218679",
      downColor: "#c45d69",
      wickUpColor: "#218679",
      wickDownColor: "#c45d69",
      borderVisible: false,
    });
    candle.setData(
      bars.map((b) => ({
        time: b.session_date as Time,
        open: b.open,
        high: b.high,
        low: b.low,
        close: b.close,
      })),
    );
    for (const [field, color] of [
      ["ma20", "#bb8c46"],
      ["ma60", "#5379c1"],
    ] as const) {
      const series = chart.addSeries(LineSeries, {
        color,
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      series.setData(
        bars
          .filter((b) => b[field] != null)
          .map((b) => ({ time: b.session_date as Time, value: b[field]! })),
      );
    }
    const days = new Set(bars.map((b) => b.session_date));
    createSeriesMarkers(
      candle,
      trades
        .filter((t) => days.has(t.execution_session))
        .map((t) => ({
          time: t.execution_session as Time,
          position:
            t.side === "BUY" ? ("belowBar" as const) : ("aboveBar" as const),
          color: t.side === "BUY" ? "#147d71" : "#bc4d5b",
          shape:
            t.side === "BUY" ? ("arrowUp" as const) : ("arrowDown" as const),
          text: t.side === "BUY" ? "買入" : "賣出",
        })),
    );
    chart.timeScale().fitContent();
    const observer = new ResizeObserver((entries) =>
      chart.applyOptions({ width: entries[0].contentRect.width }),
    );
    observer.observe(container.current);
    return () => {
      observer.disconnect();
      chart.remove();
    };
  }, [bars, trades]);
  return (
    <div
      ref={container}
      className="market-chart"
      data-testid="market-chart"
      role="img"
      aria-label="日線 K 線、MA20、MA60 與買賣位置"
    />
  );
}

export function ComparisonChart({
  comparison,
  drawdown = false,
}: {
  comparison: Comparison;
  drawdown?: boolean;
}) {
  const sessions = comparison.curves[0]?.equity || [];
  const rows = sessions.map((p, i) =>
    Object.fromEntries([
      ["date", p.session_date],
      ...comparison.curves.map((curve) => [
        curve.run_id,
        drawdown ? curve.equity[i]?.drawdown : curve.equity[i]?.equity,
      ]),
    ]),
  );
  return (
    <div
      className={drawdown ? "drawdown-chart" : "equity-chart"}
      role="img"
      aria-label={drawdown ? "策略回撤比較" : "策略資產比較"}
    >
      <ResponsiveContainer width="100%" height="100%">
        <LineChart
          data={rows}
          margin={{ top: 10, right: 12, left: 0, bottom: 0 }}
        >
          <CartesianGrid stroke={grid} vertical={false} />
          <XAxis
            dataKey="date"
            minTickGap={55}
            tickFormatter={(v) => String(v).slice(5)}
            axisLine={false}
            tickLine={false}
            tick={{ fill: "#7c8b9d", fontSize: 11 }}
          />
          <YAxis
            domain={["auto", "auto"]}
            tickFormatter={(v) =>
              drawdown
                ? `${(v * 100).toFixed(0)}%`
                : `${(v / 1000).toFixed(0)}k`
            }
            width={48}
            axisLine={false}
            tickLine={false}
            tick={{ fill: "#7c8b9d", fontSize: 11 }}
          />
          <Tooltip
            formatter={(v, name) => [
              drawdown ? `${(Number(v) * 100).toFixed(2)}%` : money(Number(v)),
              strategyNames[
                comparison.curves.find((c) => c.run_id === name)?.strategy_id ||
                  ""
              ],
            ]}
            contentStyle={{ borderRadius: 8, borderColor: grid }}
          />
          {comparison.curves.map((c) => (
            <Line
              key={c.run_id}
              dataKey={c.run_id}
              stroke={strategyColors[c.strategy_id]}
              strokeWidth={c.strategy_id === "multi_agent" ? 2.6 : 1.6}
              dot={false}
              isAnimationActive={false}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
