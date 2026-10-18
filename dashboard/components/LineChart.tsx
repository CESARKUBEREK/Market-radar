"use client";

import { useEffect, useRef } from "react";
import { ColorType, LineSeries, createChart, type IChartApi } from "lightweight-charts";

type Series = { name: string; data: { time: string; value: number }[]; color?: string };

function cssVar(name: string, fallback: string) {
  if (typeof window === "undefined") return fallback;
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
}

export default function LineChart({
  series,
  height = 320,
  precision = 2,
  zeroLine = false,
}: {
  series: Series[];
  height?: number;
  precision?: number;
  zeroLine?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current) return;
    const text = cssVar("--muted", "#6e7781");
    const grid = cssVar("--grid", "#eaeef2");
    const palette = [cssVar("--accent", "#2f6feb"), cssVar("--accent-2", "#bf3989"), cssVar("--accent-3", "#1a7f37")];
    const chart: IChartApi = createChart(ref.current, {
      height,
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: "transparent" }, textColor: text, fontSize: 12,
        attributionLogo: true },
      grid: { vertLines: { visible: false }, horzLines: { color: grid } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderVisible: false },
      crosshair: { horzLine: { labelVisible: true }, vertLine: { labelVisible: true } },
      localization: { locale: "es-ES" },
    });
    series.forEach((s, i) => {
      const line = chart.addSeries(LineSeries, {
        color: s.color || palette[i % palette.length],
        lineWidth: 2,
        priceLineVisible: false,
        priceFormat: { type: "price", precision, minMove: 1 / 10 ** precision },
        title: series.length > 1 ? s.name : "",
      });
      line.setData(s.data);
      if (zeroLine && i === 0) {
        line.createPriceLine({ price: 0, color: text, lineWidth: 1, lineStyle: 2, axisLabelVisible: false });
      }
    });
    chart.timeScale().fitContent();
    return () => chart.remove();
  }, [series, height, precision, zeroLine]);

  return <div ref={ref} style={{ width: "100%", height }} />;
}
