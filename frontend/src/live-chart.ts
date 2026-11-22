// The curve of the live view (#17): the house power of the last 15 minutes,
// with a marker on each switch-on ("?" to label, "✓" known). A click on a
// marker fires ``marker-click`` with the event id. ECharts, tree-shaken.

import { LineChart, ScatterChart } from "echarts/charts";
import { GridComponent, TooltipComponent } from "echarts/components";
import { type ECharts, init, use } from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { css, html, LitElement, type PropertyValues } from "lit";

import { formatNumber, formatPower, formatTime, type LocaleInfo } from "./format.ts";
import type { CurvePoint } from "./live.ts";
import { cssVar } from "./theme.ts";

use([LineChart, ScatterChart, GridComponent, TooltipComponent, CanvasRenderer]);

export interface LiveMarker {
  id: number;
  t: number;
  y: number;
  known: boolean;
  title: string;
  selected: boolean;
}

export interface LiveChartModel {
  curve: CurvePoint[];
  /** the right end of the axis (s) and the span shown (s) */
  end: number;
  span: number;
  markers: LiveMarker[];
  info: LocaleInfo;
  labels: { house: string; rest: string; now: string; min: string };
  /** a key of the theme: drawn again when it changes */
  themeKey: string;
}

const escape = (text: string) =>
  text.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);

export class EnergyMapperLiveChart extends LitElement {
  static override properties = {
    model: { attribute: false },
    narrow: { type: Boolean, reflect: true },
  };

  declare model: LiveChartModel | null;
  declare narrow: boolean;
  private chart: ECharts | null = null;
  private resizeObserver: ResizeObserver | null = null;

  constructor() {
    super();
    this.model = null;
    this.narrow = false;
  }

  override connectedCallback(): void {
    super.connectedCallback();
    if (this.hasUpdated) this.setup();
  }

  override disconnectedCallback(): void {
    super.disconnectedCallback();
    this.resizeObserver?.disconnect();
    this.resizeObserver = null;
    this.chart?.dispose();
    this.chart = null;
  }

  protected override firstUpdated(): void {
    this.setup();
  }

  protected override updated(changed: PropertyValues): void {
    if (changed.has("model") || changed.has("narrow")) this.draw();
  }

  private setup(): void {
    const container = this.renderRoot.querySelector<HTMLDivElement>(".chart");
    if (!container || this.chart) return;
    this.chart = init(container, undefined, { renderer: "canvas" });
    this.chart.on("click", (params: { seriesId?: string; data?: unknown }) => {
      if (params.seriesId !== "markers") return;
      const id = (params.data as { id?: number } | undefined)?.id;
      if (typeof id === "number") {
        this.dispatchEvent(new CustomEvent("marker-click", { detail: { id }, bubbles: true, composed: true }));
      }
    });
    this.resizeObserver = new ResizeObserver(() => this.chart?.resize());
    this.resizeObserver.observe(container);
    this.draw();
  }

  private draw(): void {
    if (!this.chart || !this.model) return;
    this.chart.setOption(this.option(this.model), { notMerge: true, lazyUpdate: true });
  }

  private option(m: LiveChartModel) {
    const el = this as Element;
    const text = cssVar(el, "--secondary-text-color", "#727272");
    const divider = cssVar(el, "--divider-color", "rgba(0, 0, 0, 0.12)");
    const card = cssVar(el, "--card-background-color", "#ffffff");
    const primaryText = cssVar(el, "--primary-text-color", "#212121");
    const primary = cssVar(el, "--primary-color", "#03a9f4");
    const grey = cssVar(el, "--disabled-color", "#bdbdbd");
    const todo = cssVar(el, "--warning-color", "#ffa600");
    const known = cssVar(el, "--success-color", "#43a047");
    const font = cssVar(el, "--ha-font-family-body", "Roboto, Noto, sans-serif");
    const { info } = m;
    const start = m.end - m.span;
    const points = m.curve.filter((p) => p[0] >= start - 30);
    const textStyle = { color: text, fontFamily: font, fontSize: 11 };
    return {
      animation: false,
      textStyle: { fontFamily: font },
      grid: { left: 4, right: 12, top: 28, bottom: 4, outerBoundsMode: "same" as const, outerBoundsContain: "axisLabel" as const },
      tooltip: {
        trigger: "axis" as const,
        confine: true,
        backgroundColor: card,
        borderColor: divider,
        textStyle: { color: primaryText, fontFamily: font, fontSize: 12 },
        axisPointer: { type: "line" as const, lineStyle: { color: text } },
        formatter: (params: Array<{ seriesId: string; seriesName: string; color: string; value: [number, number | null] }>) => {
          if (!params.length) return "";
          const rows = params
            .filter((p) => p.seriesId !== "markers" && p.value[1] !== null)
            .map(
              (p) => `<div style="display:flex;gap:8px;align-items:center">
                <span style="width:8px;height:8px;border-radius:50%;background:${p.color};flex:none"></span>
                <span style="flex:1">${escape(p.seriesName)}</span><span style="margin-left:12px">${formatPower(p.value[1] ?? 0, info)}</span></div>`,
            )
            .join("");
          return `<div style="font-weight:500;margin-bottom:4px">${formatTime(params[0].value[0] / 1000, info, true)}</div>${rows}`;
        },
      },
      xAxis: {
        type: "time" as const,
        min: start * 1000,
        max: m.end * 1000,
        interval: 300_000,
        axisLabel: {
          ...textStyle,
          hideOverlap: true,
          formatter: (v: number) => {
            const ago = Math.round((m.end * 1000 - v) / 60_000);
            return ago <= 0 ? m.labels.now : m.labels.min.replace("{n}", formatNumber(ago, info));
          },
        },
        axisLine: { lineStyle: { color: divider } },
        axisTick: { show: false },
        splitLine: { show: false },
      },
      yAxis: {
        type: "value" as const,
        min: 0,
        axisLabel: { ...textStyle, formatter: (v: number) => formatPower(v, info) },
        splitLine: { lineStyle: { color: divider } },
      },
      series: [
        {
          id: "rest",
          name: m.labels.rest,
          type: "line" as const,
          step: "end" as const,
          symbol: "none",
          color: grey,
          lineStyle: { width: 0 },
          areaStyle: { color: grey, opacity: 0.55 },
          emphasis: { disabled: true },
          data: points.map((p) => [p[0] * 1000, p[2]]),
        },
        {
          id: "house",
          name: m.labels.house,
          type: "line" as const,
          step: "end" as const,
          symbol: "none",
          color: primary,
          lineStyle: { width: 1.5, color: primary },
          areaStyle: { color: primary, opacity: 0.18 },
          emphasis: { disabled: true },
          z: 3,
          data: points.map((p) => [p[0] * 1000, p[1]]),
        },
        {
          id: "markers",
          name: "",
          type: "scatter" as const,
          symbol: "roundRect",
          symbolSize: [26, 20],
          symbolOffset: [0, -14],
          z: 5,
          cursor: "pointer",
          tooltip: { show: false },
          data: m.markers.map((mk) => ({
            id: mk.id,
            name: mk.title,
            value: [mk.t * 1000, mk.y],
            itemStyle: {
              color: mk.known ? known : todo,
              borderColor: mk.selected ? primary : card,
              borderWidth: 2,
              shadowBlur: 3,
              shadowColor: "rgba(0, 0, 0, 0.25)",
            },
            label: {
              show: true,
              position: "inside" as const,
              formatter: mk.known ? "✓" : "?",
              color: "#ffffff",
              fontWeight: 700,
              fontSize: 12,
              fontFamily: font,
            },
          })),
        },
      ],
    };
  }

  protected override render() {
    return html`<div class="chart"></div>`;
  }

  static override styles = css`
    :host {
      display: block;
    }
    .chart {
      width: 100%;
      height: 220px;
    }
    :host([narrow]) .chart {
      height: 170px;
    }
  `;
}

if (!customElements.get("energy-mapper-live-chart")) {
  customElements.define("energy-mapper-live-chart", EnergyMapperLiveChart);
}
