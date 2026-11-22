// The "Live" tab of the panel (#17): the house curve of the last 15 minutes,
// what runs now, the feed of what just happened with inline labelling, and
// the guided mode "I'm switching an appliance on". It subscribes to
// ``energy_mapper/live/subscribe`` while it is shown (the panel renders it
// only on its tab) and the page is visible.

import { mdiDotsVertical, mdiLightbulbOutline } from "@mdi/js";
import { html, LitElement, nothing, svg, type PropertyValues, type TemplateResult } from "lit";

import { frequentNames } from "./appliance-names.ts";
import { formatNumber, formatPercent, formatPower, formatTime, type LocaleInfo, localeInfo } from "./format.ts";
import {
  type Guided,
  guidedArm,
  guidedDone,
  guidedFailed,
  guidedFinish,
  guidedIdle,
  guidedMessage,
  guidedName,
  guidedNo,
  guidedRetrySave,
  guidedSaving,
  guidedStart,
  guidedTick,
  guidedYes,
} from "./guided.ts";
import { type StringKey, type Translate, translator } from "./i18n.ts";
import {
  applyMessage,
  clockOffset,
  effectiveThreshold,
  emptyStore,
  type FeedEntry,
  feedEntries,
  houseAt,
  type LiveEvent,
  type LiveMessage,
  type LiveStore,
  zoomDrawing,
} from "./live.ts";
import "./live-chart.ts";
import type { LiveChartModel, LiveMarker } from "./live-chart.ts";
import {
  bestCandidate,
  candidateName,
  candidateNamed,
  candidateReason,
  type FeedRow,
  feedRow,
  initialForm,
  type LabelForm,
  labelError,
  labelMessage,
  labelPlan,
  type LabelResult,
  type Labelled,
  liveDuration,
  type NameContext,
  nowItems,
  nowSplit,
  pastUses,
  planEffect,
  planMissing,
  resultEffect,
  saveLabel,
  statusView,
  stepText,
  type Track,
  whenText,
} from "./live-feed.ts";
import { linkable, linkDomains } from "./review.ts";
import { liveStyles } from "./live-styles.ts";
import { panelStyles } from "./styles.ts";
import type { HomeAssistant, Registry } from "./types.ts";

const defined = (tag: string) => customElements.get(tag) !== undefined;

/** One labelling form open under its line. */
interface OpenForm extends LabelForm {
  id: number;
  busy: boolean;
  error: string | null;
}

export class EnergyMapperLive extends LitElement {
  static override properties = {
    hass: { attribute: false },
    narrow: { type: Boolean, reflect: true },
    registry: { attribute: false },
    appliances: { attribute: false },
    _store: { state: true },
    _clock: { state: true },
    _filter: { state: true },
    _form: { state: true },
    _labelled: { state: true },
    _hidden: { state: true },
    _menu: { state: true },
    _guided: { state: true },
    _connecting: { state: true },
    _themeKey: { state: true },
    _rowError: { state: true },
  };

  static override styles = [
    panelStyles,
    liveStyles,
  ];

  declare hass: HomeAssistant | undefined;
  declare narrow: boolean;
  declare registry: Registry | null;
  declare appliances: ReadonlyArray<{ key: string; name: string }> | null;
  declare _store: LiveStore;
  /** the local clock (s), every second: "3 s ago" */
  declare _clock: number;
  declare _filter: "all" | "todo";
  declare _form: OpenForm | null;
  declare _labelled: Map<number, Labelled>;
  declare _hidden: Set<number>;
  /** the event whose "⋯" menu is open */
  declare _menu: number | null;
  declare _guided: Guided;
  declare _connecting: boolean;
  declare _themeKey: string;
  /** an action of a line failed ("never propose") */
  declare _rowError: { id: number; text: string } | null;

  private unsubscribe: (() => unknown) | null = null;
  private token = 0;
  private timer: ReturnType<typeof setInterval> | undefined;
  private offset = 0;
  private lastMessage = 0;
  private infoCache: { key: string; info: LocaleInfo } | null = null;
  private namesCache: { key: unknown[]; ctx: NameContext } | null = null;
  private focusAfter: string | null = null;
  private chartCache: { key: unknown[]; model: LiveChartModel } | null = null;

  constructor() {
    super();
    this.hass = undefined;
    this.narrow = false;
    this.registry = null;
    this.appliances = null;
    this._store = emptyStore();
    this._clock = Date.now() / 1000;
    this._filter = "all";
    this._form = null;
    this._labelled = new Map();
    this._hidden = new Set();
    this._menu = null;
    this._guided = guidedIdle();
    this._connecting = false;
    this._themeKey = "";
    this._rowError = null;
  }

  // -- subscription -------------------------------------------------------------
  override connectedCallback(): void {
    super.connectedCallback();
    document.addEventListener("visibilitychange", this.visibility);
    this.timer = setInterval(() => this.tick(), 1000);
    void this.start();
  }

  override disconnectedCallback(): void {
    super.disconnectedCallback();
    document.removeEventListener("visibilitychange", this.visibility);
    clearInterval(this.timer);
    this.saveOpenGuided();
    this.stop();
  }

  private visibility = () => {
    if (document.hidden) this.stop();
    else void this.start();
  };

  protected override willUpdate(changed: PropertyValues): void {
    if (changed.has("hass") && this.hass && !this.unsubscribe && !this._connecting) void this.start();
    if (changed.has("hass") && this.hass) {
      const key = JSON.stringify([this.hass.themes?.theme, this.hass.themes?.darkMode]);
      if (key !== this._themeKey) requestAnimationFrame(() => (this._themeKey = key));
    }
  }

  private async start(): Promise<void> {
    if (!this.isConnected || document.hidden || this.unsubscribe || this._connecting || !this.hass) return;
    const conn = this.hass.connection;
    const token = ++this.token;
    if (!conn) {
      this.onMessage({ type: "status", available: false, reason: "not_loaded" });
      return;
    }
    this._connecting = true;
    try {
      const unsub = await conn.subscribeMessage<LiveMessage>((msg) => {
        if (token === this.token) this.onMessage(msg);
      }, { type: "energy_mapper/live/subscribe" });
      if (token !== this.token) {
        void unsub();
        return;
      }
      this.unsubscribe = unsub;
    } catch (err) {
      if (token !== this.token) return;
      const e = (err ?? {}) as { code?: string; message?: string };
      this.onMessage(
        e.code === "not_loaded"
          ? { type: "status", available: false, reason: "not_loaded" }
          : { type: "status", available: false, reason: "error", error: { code: e.code, message: e.message ?? String(err) } },
      );
    } finally {
      if (token === this.token) this._connecting = false;
    }
  }

  private stop(): void {
    this.token++;
    this._connecting = false;
    const unsub = this.unsubscribe;
    this.unsubscribe = null;
    if (unsub) {
      try {
        void Promise.resolve(unsub()).catch(() => undefined);
      } catch {
        // the connection is already closed
      }
    }
    this.tellState(false);
  }

  private tellState(up: boolean): void {
    this.dispatchEvent(new CustomEvent("live-state", { detail: { up }, bubbles: true, composed: true }));
  }

  private onMessage(msg: LiveMessage): void {
    const local = Date.now() / 1000;
    const store = applyMessage(this._store, msg, local + this.offset);
    const offset = clockOffset(store, local);
    if (offset !== null && msg.type !== "status") this.offset = offset;
    this.lastMessage = local;
    this._store = store;
    if (this._form && !store.events.has(this._form.id)) this._form = null;
    this.tellState(store.available);
    this.advanceGuided();
  }

  /** The clock of the stream (s). */
  private get streamNow(): number {
    return Date.now() / 1000 + this.offset;
  }

  private tick(): void {
    this._clock = Date.now() / 1000;
    this.advanceGuided();
  }

  // -- helpers ----------------------------------------------------------------
  private get t(): Translate {
    return translator(this.hass?.locale?.language || this.hass?.language);
  }

  private get info(): LocaleInfo {
    const h = this.hass;
    const key = JSON.stringify([h?.locale, h?.language, h?.config?.time_zone]);
    if (this.infoCache?.key !== key) {
      this.infoCache = { key, info: localeInfo(h?.locale, h?.language ?? "en", h?.config?.time_zone ?? "UTC") };
    }
    return this.infoCache.info;
  }

  private get isAdmin(): boolean {
    return Boolean(this.hass?.user?.is_admin);
  }

  private get names(): NameContext {
    const key = [this.t, this.info, this.appliances, this.registry, this.hass?.states];
    if (!this.namesCache || this.namesCache.key.some((k, i) => k !== key[i])) {
      const states = this.hass?.states;
      this.namesCache = {
        key,
        ctx: {
          t: this.t,
          info: this.info,
          appliances: this.appliances ?? [],
          signatures: new Map((this.registry?.signatures ?? []).map((s) => [s.id, s])),
          entityName: (id) => {
            const name = states?.[id]?.attributes?.friendly_name;
            return typeof name === "string" ? name : undefined;
          },
        },
      };
    }
    return this.namesCache.ctx;
  }

  private textButton(label: string, onClick: () => void, opts: { disabled?: boolean; accent?: boolean; title?: string; id?: string } = {}): TemplateResult {
    const appearance = opts.accent ? "accent" : "plain";
    if (defined("ha-button")) {
      return html`<ha-button
        id=${opts.id ?? nothing}
        appearance=${appearance}
        size="small"
        title=${opts.title ?? ""}
        ?disabled=${opts.disabled ?? false}
        @click=${onClick}
        >${label}</ha-button
      >`;
    }
    return html`<button
      type="button"
      id=${opts.id ?? nothing}
      class="text-button ${appearance}"
      title=${opts.title ?? ""}
      ?disabled=${opts.disabled ?? false}
      @click=${onClick}
    >
      ${label}
    </button>`;
  }

  private alert(type: "info" | "warning" | "error" | "success", text: string, title?: string): TemplateResult {
    if (defined("ha-alert")) return html`<ha-alert alert-type=${type} .title=${title ?? ""}>${text}</ha-alert>`;
    return html`<div class="alert ${type}" role="alert">
      <div>${title ? html`<div class="alert-title">${title}</div>` : nothing}${text}</div>
    </div>`;
  }

  private labelledChanged(): void {
    this.dispatchEvent(new CustomEvent("live-labelled", { bubbles: true, composed: true }));
  }

  protected override updated(): void {
    if (this.focusAfter) {
      const el = this.renderRoot.querySelector<HTMLElement>(this.focusAfter);
      this.focusAfter = null;
      el?.focus();
      el?.scrollIntoView?.({ block: "nearest" });
    }
  }

  // -- feed actions -------------------------------------------------------------
  private openForm(entry: FeedEntry): void {
    this._menu = null;
    this._form = { ...initialForm(entry.ev, this.names), id: entry.ev.id, busy: false, error: null };
    this.focusAfter = `#form-${entry.ev.id} .first-focus`;
  }

  private closeForm(): void {
    const id = this._form?.id;
    this._form = null;
    if (id !== undefined) this.focusAfter = `#ev-${id} .its`;
  }

  private patchForm(patch: Partial<OpenForm>): void {
    if (this._form) this._form = { ...this._form, ...patch, error: null };
  }

  /** Pick a candidate (again: unpick it). A named one replaces the name typed. */
  private pick(ev: LiveEvent, index: number): void {
    const form = this._form;
    if (!form) return;
    if (form.pick === index) return this.patchForm({ pick: null });
    const c = ev.candidates[index];
    this.patchForm({ pick: index, name: candidateNamed(c, this.names) ? "" : form.name });
  }

  /** Type a name: a named candidate picked is dropped (an unnamed signature
   * picked keeps it: the name is its name). */
  private typeName(ev: LiveEvent, name: string): void {
    const form = this._form;
    if (!form) return;
    const c = form.pick !== null ? ev.candidates[form.pick] : undefined;
    const keep = c && c.source === "signature" && !candidateNamed(c, this.names);
    this.patchForm({ name, pick: keep ? form.pick : name.trim() ? null : form.pick });
  }

  private hide(id: number): void {
    this._menu = null;
    this._hidden = new Set([...this._hidden, id]);
    if (this._form?.id === id) this._form = null;
  }

  private async save(entry: FeedEntry): Promise<void> {
    const form = this._form;
    if (!form || form.busy || !this.hass) return;
    const plan = labelPlan(entry.ev, form, this.names);
    const missing = planMissing(plan);
    if (missing || plan.kind === "none") {
      this._form = { ...form, error: this.t(missing ?? "live_need_choice") };
      return;
    }
    this._form = { ...form, busy: true, error: null };
    try {
      const result = await this.hass.callWS<LabelResult>(labelMessage(plan, entry));
      const name = result.appliance?.name ?? plan.name;
      this._labelled = new Map(this._labelled).set(entry.ev.id, { name, effect: resultEffect(result, this.t, this.info) });
      this._form = null;
      this.focusAfter = `#ev-${entry.ev.id}`;
      this.labelledChanged();
    } catch (err) {
      if (this._form?.id === form.id) this._form = { ...this._form, busy: false, error: labelError(err, this.t) };
    }
  }

  /** "Never propose this again": the review's ignore decision. */
  private async never(row: FeedRow): Promise<void> {
    this._menu = null;
    this._rowError = null;
    if (!row.ignorable || !this.hass) return;
    try {
      const result = await this.hass.callWS<LabelResult>({
        type: "energy_mapper/live/label",
        target: "ignore",
        signature_id: row.ignorable,
      });
      this._labelled = new Map(this._labelled).set(row.id, { name: row.title, effect: resultEffect(result, this.t, this.info) });
      this._hidden = new Set([...this._hidden, row.id]);
      this.labelledChanged();
    } catch (err) {
      this._rowError = { id: row.id, text: labelError(err, this.t) };
    }
  }

  private markerClick = (ev: CustomEvent<{ id: number }>) => {
    const entry = this._store.events.get(ev.detail.id);
    if (!entry) return;
    this._filter = "all";
    if (this._hidden.has(entry.ev.id)) this._hidden = new Set([...this._hidden].filter((x) => x !== entry.ev.id));
    const row = feedRow(entry, this.names, this.streamNow, this.isAdmin, this._labelled.get(entry.ev.id));
    if (row.canLabel) this.openForm(entry);
    else this.focusAfter = `#ev-${entry.ev.id}`;
  };

  // -- guided mode --------------------------------------------------------------
  private advanceGuided(): void {
    const g = this._guided;
    if (!["wait", "confirm", "pick", "running"].includes(g.step)) return;
    const store = this._store;
    const next = guidedTick(g, store.events.values(), store.curve, effectiveThreshold(store), this.streamNow);
    if (next !== g) this._guided = next;
    if (next.step === "save") void this.saveGuided();
  }

  private async saveGuided(): Promise<void> {
    const g = this._guided;
    if (g.step !== "save" || !this.hass) return;
    const entry = g.event !== undefined ? this._store.events.get(g.event) : undefined;
    if (!entry) {
      this._guided = guidedFailed(g, this.t("live_err_event"));
      return;
    }
    this._guided = guidedSaving(g);
    try {
      const result = await this.hass.callWS<LabelResult>(guidedMessage(g, entry, this.appliances ?? []));
      const effect = resultEffect(result, this.t, this.info);
      this._guided = guidedDone(g, effect, entry.ev);
      this._labelled = new Map(this._labelled).set(entry.ev.id, { name: result.appliance?.name ?? g.name, effect });
      this.labelledChanged();
    } catch (err) {
      this._guided = guidedFailed(g, labelError(err, this.t));
    }
  }

  /** The panel closes while an appliance runs: keep its switch-on. */
  private saveOpenGuided(): void {
    const g = this._guided;
    if (g.step !== "running" || !this.hass) return;
    const entry = g.event !== undefined ? this._store.events.get(g.event) : undefined;
    if (!entry) return;
    void this.hass.callWS(guidedMessage(guidedFinish(g), entry, this.appliances ?? [])).catch(() => undefined);
    this._guided = guidedIdle();
  }

  // -- render -------------------------------------------------------------------
  protected override render(): TemplateResult {
    const t = this.t;
    const store = this._store;
    const status = statusView(store.status, !store.available && store.status === null, t, this.info);
    if (!store.available && store.events.size === 0) {
      // nothing to show but why, and the limits
      return html`<div class="live">
        ${status ? this.alert(status.type, status.text, status.title) : nothing} ${this.renderKnow(t)}
      </div>`;
    }
    return html`
      <div class="live">
        ${status ? this.alert(status.type, status.text, status.title) : nothing}
        ${store.available ? this.renderCurve(t) : nothing}
        <div class="live-columns">
          ${this.renderFeed(t)}
          <div class="live-side">${this.renderNow(t)} ${this.isAdmin ? this.renderGuided(t) : nothing}
            ${this.narrow ? nothing : this.renderKnow(t)}</div>
          ${this.narrow ? this.renderKnow(t) : nothing}
        </div>
      </div>
    `;
  }

  /** The chart's model, rebuilt only when what it shows changes (not every
   * second: the tooltip would be reset). */
  private chartModel(t: Translate): LiveChartModel {
    const key = [this._store, this._labelled, this._hidden, this._form?.id, this._themeKey, this.names];
    if (this.chartCache && this.chartCache.key.every((k, i) => k === key[i])) return this.chartCache.model;
    const store = this._store;
    const info = this.info;
    const now = this.streamNow;
    const end = store.curve.length ? Math.max(store.curve[store.curve.length - 1][0], now) : now;
    const markers: LiveMarker[] = [];
    for (const { ev } of store.events.values()) {
      if (ev.delta_w <= 0 || ev.t_on < end - store.history_s || this._hidden.has(ev.id)) continue;
      const best = bestCandidate(ev);
      const known = this._labelled.has(ev.id) || (best !== undefined && candidateNamed(best, this.names));
      const row = feedRow({ ev, stream: null }, this.names, now, false, this._labelled.get(ev.id));
      const y = Math.max(houseAt(store.curve, ev.t_on + 5) ?? 0, houseAt(store.curve, ev.t_on) ?? 0);
      markers.push({
        id: ev.id,
        t: ev.t_on,
        y,
        known,
        title: t("live_marker", { delta: stepText(ev.delta_w, info), time: formatTime(ev.t_on, info, true), title: row.title }),
        selected: this._form?.id === ev.id,
      });
    }
    const model: LiveChartModel = {
      curve: store.curve,
      end,
      span: store.history_s,
      markers,
      info,
      labels: { house: t("live_legend_house"), rest: t("live_legend_rest"), now: t("live_axis_now"), min: t("live_axis_min") },
      themeKey: this._themeKey,
    };
    this.chartCache = { key, model };
    return model;
  }

  private renderCurve(t: Translate): TemplateResult {
    const store = this._store;
    const info = this.info;
    const model = this.chartModel(t);
    const updated = this.lastMessage ? whenText(this.lastMessage, this._clock, t, info) : "";
    return html`<section class="card live-card" aria-labelledby="live-curve-title">
      <div class="live-card-head">
        <h2 id="live-curve-title">${this.narrow ? t("live_curve_title_short") : t("live_curve_title")}</h2>
        ${store.available ? html`<span class="live-badge"><i class="pulse"></i>${t("live_updated", { ago: updated })}</span>` : nothing}
        <span class="spacer"></span>
        ${this.narrow || !this.isAdmin ? nothing : html`<span class="live-hint">${t("live_curve_hint")}</span>`}
      </div>
      <energy-mapper-live-chart
        role="img"
        aria-label=${t("live_curve_label")}
        .model=${model}
        ?narrow=${this.narrow}
        @marker-click=${this.markerClick}
      ></energy-mapper-live-chart>
      <div class="live-legend">
        <span><i class="sw house"></i>${t("live_legend_house")}</span>
        <span><i class="sw rest"></i>${t("live_legend_rest")}</span>
        <span><i class="mk todo">?</i>${t("live_legend_todo")}</span>
        <span><i class="mk known">✓</i>${t("live_legend_known")}</span>
      </div>
    </section>`;
  }

  private rows(): FeedRow[] {
    const now = this.streamNow;
    return feedEntries(this._store)
      .filter((e) => !this._hidden.has(e.ev.id))
      .map((e) => feedRow(e, this.names, now, this.isAdmin, this._labelled.get(e.ev.id)));
  }

  private renderFeed(t: Translate): TemplateResult {
    const rows = this.rows();
    const todo = rows.filter((r) => r.todo).length;
    const shown = this._filter === "todo" ? rows.filter((r) => r.todo || r.id === this._form?.id) : rows;
    const threshold = formatPower(effectiveThreshold(this._store), this.info);
    return html`<section class="card live-feed" aria-labelledby="live-feed-title">
      <div class="live-feed-head">
        <h2 id="live-feed-title">${t("live_feed_title")}</h2>
        <span class="spacer"></span>
        <div class="live-filters" role="group" aria-label=${t("live_feed_title")}>
          <button
            type="button"
            class="filter-chip ${this._filter === "all" ? "selected" : ""}"
            aria-pressed=${this._filter === "all" ? "true" : "false"}
            @click=${() => (this._filter = "all")}
          >
            ${t("live_filter_all")}
          </button>
          <button
            type="button"
            class="filter-chip ${this._filter === "todo" ? "selected" : ""}"
            aria-pressed=${this._filter === "todo" ? "true" : "false"}
            @click=${() => (this._filter = "todo")}
          >
            ${t("live_filter_todo", { n: formatNumber(todo, this.info) })}
          </button>
        </div>
      </div>
      ${shown.length
        ? html`<ol class="live-rows">
            ${shown.map((r) => this.renderRow(r, t))}
          </ol>`
        : html`<div class="live-empty">${this._filter === "todo" && rows.length ? t("live_feed_empty_todo") : t("live_feed_empty")}</div>`}
      <div class="live-foot">${t("live_feed_foot", { threshold })}</div>
    </section>`;
  }

  private renderRow(r: FeedRow, t: Translate): TemplateResult {
    const open = this._form?.id === r.id;
    const labelled = this._labelled.get(r.id);
    const tagKey = `live_tag_${r.tag}` as StringKey;
    const menu = this._menu === r.id;
    return html`<li
      id="ev-${r.id}"
      tabindex="-1"
      class="live-row ${r.provisional ? "provisional" : ""} ${open ? "open" : ""}"
    >
      <div class="live-row-main">
        <span class="delta ${r.delta < 0 ? "down" : r.todo ? "todo" : ""}">${stepText(r.delta, this.info)}</span>
        <div class="live-row-text">
          <div class="live-row-title">
            <strong>${r.title}</strong>
            <span class="tag tag-${r.tag}">${t(tagKey)}</span>
          </div>
          <span class="live-row-sub">${r.when} · ${r.sub}</span>
          ${labelled ? html`<span class="live-row-effect">${labelled.effect}</span>` : nothing}
          ${this._rowError?.id === r.id ? html`<span class="field-error" role="alert">${this._rowError.text}</span>` : nothing}
        </div>
        ${r.canLabel && !open
          ? html`<div class="live-row-actions">
              <span class="its">${this.textButton(t("live_its"), () => this.openForm(r.entry), { accent: r.todo })}</span>
              ${this.textButton(t("live_ignore"), () => this.hide(r.id), { title: t("live_ignore_hint") })}
              ${r.ignorable
                ? html`<span class="menu-wrap">
                    <button
                      type="button"
                      class="icon-button small"
                      aria-label=${t("live_more")}
                      title=${t("live_more")}
                      aria-haspopup="menu"
                      aria-expanded=${menu ? "true" : "false"}
                      @click=${() => {
                        this._menu = menu ? null : r.id;
                        if (!menu) this.focusAfter = `#ev-${r.id} [role="menuitem"]`;
                      }}
                    >
                      <svg viewBox="0 0 24 24" aria-hidden="true"><path d=${mdiDotsVertical}></path></svg>
                    </button>
                    ${menu
                      ? html`<div
                          class="menu"
                          role="menu"
                          @keydown=${(ev: KeyboardEvent) => {
                            if (ev.key === "Escape") {
                              this._menu = null;
                              this.focusAfter = `#ev-${r.id} [aria-haspopup]`;
                            }
                          }}
                        >
                          <button type="button" role="menuitem" @click=${() => void this.never(r)}>${t("live_never")}</button>
                        </div>`
                      : nothing}
                  </span>`
                : nothing}
            </div>`
          : nothing}
      </div>
      ${open ? this.renderForm(r, t) : nothing}
    </li>`;
  }

  private renderZoom(ev: LiveEvent): TemplateResult | typeof nothing {
    const end = ev.running || ev.t_off === null ? this.streamNow : ev.t_off;
    const from = ev.t_on - 60;
    const to = Math.max(end + 30, ev.t_on + 90);
    const d = zoomDrawing(this._store.curve, from, to, ev.t_on, end, 150, 56);
    if (!d) return nothing;
    return html`<svg class="zoom" viewBox="0 0 150 56" preserveAspectRatio="none" aria-hidden="true">
      ${svg`<rect class="zoom-span" x=${d.x} y="0" width=${d.w} height="56"></rect>
      <path class="zoom-line" d=${d.path}></path>`}
    </svg>`;
  }

  private renderForm(r: FeedRow, t: Translate): TemplateResult {
    const form = this._form!;
    const ev = r.entry.ev;
    const names = this.names;
    const info = this.info;
    const best = bestCandidate(ev);
    const plan = labelPlan(ev, form, names);
    const picked = form.pick !== null ? ev.candidates[form.pick] : undefined;
    const pickedUnnamed = picked && picked.source === "signature" && !candidateNamed(picked, names);
    let match = t("live_match_none");
    if (best && candidateNamed(best, names)) match = t("live_match_known", { name: candidateName(best, names) });
    else if (best) {
      const n = pastUses(best, names);
      match = n ? t("live_match_seen", { n: formatNumber(n, info) }) : "";
    }
    const state = ev.running ? t("live_state_running") : ev.duration_s ? t("live_ran_for", { d: liveDuration(ev.duration_s, info) }) : "";
    const quick = frequentNames(ev.power_w, this.hass?.locale?.language || this.hass?.language, (this.appliances ?? []).map((a) => a.name));
    const missing = planMissing(plan);
    const busy = form.busy;
    const listId = `live-appliances-${r.id}`;
    const showHow = form.pick === null && plan.kind !== "appliance";
    return html`<div
      class="labelling"
      id="form-${r.id}"
      role="group"
      aria-label=${t("live_its")}
      @keydown=${(e: KeyboardEvent) => {
        if (e.key === "Escape" && !busy) this.closeForm();
      }}
    >
      <div class="lb-head">
        ${this.renderZoom(ev)}
        <div class="lb-head-text">
          <strong>${t("live_label_title", { delta: stepText(ev.delta_w, info), time: formatTime(ev.t_on, info, true) })}</strong>${state ? `, ${state}` : ""}
          ${match ? html`<br /><span class="secondary">${match}</span>` : nothing}
        </div>
      </div>
      ${ev.candidates.length
        ? html`<div class="lb-section">
            <span class="lb-label" id="cands-${r.id}">${best && best.score >= 0.6 ? t("live_cands_likely") : t("live_cands_maybe")}</span>
            <div class="cands" role="group" aria-labelledby="cands-${r.id}">
              ${ev.candidates.map((c, i) => {
                const on = form.pick === i;
                const score = Math.max(0, Math.min(1, c.score));
                return html`<button
                  type="button"
                  class="cand ${on ? "on" : ""} ${i === 0 ? "first-focus" : ""}"
                  aria-pressed=${on ? "true" : "false"}
                  ?disabled=${busy}
                  @click=${() => this.pick(ev, i)}
                >
                  <span class="radio" aria-hidden="true"></span>
                  <span class="cand-text">
                    <span class="cand-name">${candidateName(c, names)}</span>
                    <span class="secondary">${candidateReason(c, ev, names)}</span>
                  </span>
                  <span class="cand-score">
                    <span>${formatPercent(score, info)}</span>
                    <span class="bar"><span class="${score >= 0.6 ? "hi" : score >= 0.3 ? "mid" : "lo"}" style="width:${Math.round(score * 100)}%"></span></span>
                  </span>
                </button>`;
              })}
            </div>
          </div>`
        : nothing}
      <label class="field lb-name">
        <span>${pickedUnnamed ? t("live_name_signature") : t("live_new_name")}</span>
        <input
          class="text-input ${ev.candidates.length ? "" : "first-focus"}"
          list=${listId}
          placeholder=${t("live_new_placeholder")}
          .value=${form.name}
          ?disabled=${busy}
          @input=${(e: Event) => this.typeName(ev, (e.target as HTMLInputElement).value)}
          @keydown=${(e: KeyboardEvent) => {
            if (e.key === "Enter") void this.save(r.entry);
          }}
        />
        <datalist id=${listId}>${(this.appliances ?? []).map((a) => html`<option value=${a.name}></option>`)}</datalist>
      </label>
      ${quick.length
        ? html`<div class="quick">
            <span class="secondary">${t("live_frequent")}</span>
            ${quick.map(
              (q) => html`<button type="button" class="filter-chip small" ?disabled=${busy} @click=${() => this.typeName(ev, q)}>${q}</button>`,
            )}
          </div>`
        : nothing}
      ${showHow
        ? html`<div class="lb-section">
            <span class="lb-label" id="how-${r.id}">${t("rq_how")}</span>
            <div class="seg" role="group" aria-labelledby="how-${r.id}">
              ${(["curve", "switch", "sensor"] as Track[]).map(
                (k) => html`<button
                  type="button"
                  class=${form.track === k ? "selected" : ""}
                  aria-pressed=${form.track === k ? "true" : "false"}
                  ?disabled=${busy}
                  @click=${() => this.patchForm({ track: k, entity: k === form.track ? form.entity : "" })}
                >
                  ${t(k === "curve" ? "live_track_curve" : k === "switch" ? "rq_track_switch" : "rq_track_sensor")}
                </button>`,
              )}
            </div>
            ${form.track !== "curve" ? this.renderEntityPicker(form, t) : nothing}
          </div>`
        : nothing}
      <div class="lb-effect" aria-live="polite">${planEffect(plan, ev, names)}</div>
      ${form.error ? html`<div class="field-error" role="alert">${form.error}</div>` : nothing}
      <div class="lb-actions">
        ${this.textButton(busy ? t("live_saving") : saveLabel(plan, t), () => void this.save(r.entry), {
          accent: true,
          disabled: busy || missing !== null,
        })}
        ${this.textButton(t("live_cancel"), () => this.closeForm(), { disabled: busy })}
      </div>
    </div>`;
  }

  private renderEntityPicker(form: OpenForm, t: Translate): TemplateResult {
    const mode = form.track === "sensor" ? "sensor" : "switch";
    const domains = linkDomains(mode).filter((d) => mode === "sensor" || d !== "sensor");
    const ok = (id: string, attrs?: Record<string, unknown>) =>
      linkable(mode, id, attrs) && domains.includes(id.split(".")[0]) && (mode !== "sensor" || attrs?.device_class === "power");
    if (defined("ha-entity-picker")) {
      return html`<ha-entity-picker
        .hass=${this.hass}
        .value=${form.entity}
        .label=${t("review_entity")}
        .includeDomains=${domains}
        .includeDeviceClasses=${mode === "sensor" ? ["power"] : undefined}
        .entityFilter=${(st: { entity_id: string; attributes?: Record<string, unknown> }) => ok(st.entity_id, st.attributes)}
        @value-changed=${(ev: CustomEvent<{ value?: string }>) => this.patchForm({ entity: ev.detail?.value ?? "" })}
      ></ha-entity-picker>`;
    }
    const listId = `live-entities-${mode}`;
    const choices = Object.entries(this.hass?.states ?? {})
      .filter(([id, st]) => ok(id, st.attributes))
      .map(([id, st]) => ({ id, name: String(st.attributes?.friendly_name ?? id) }))
      .sort((a, b) => a.name.localeCompare(b.name));
    return html`<label class="field">
      <span>${t("review_entity")}</span>
      <input
        class="text-input"
        list=${listId}
        .value=${form.entity}
        @change=${(ev: Event) => this.patchForm({ entity: (ev.target as HTMLInputElement).value.trim() })}
      />
      <datalist id=${listId}>${choices.map((e) => html`<option value=${e.id}>${e.name}</option>`)}</datalist>
    </label>`;
  }

  private renderNow(t: Translate): TemplateResult {
    const store = this._store;
    const items = nowItems(store.now, store.events, this.names, this._labelled);
    const split = nowSplit(items);
    const house = store.now?.house_w;
    return html`<section class="card live-now" aria-labelledby="live-now-title">
      <div class="live-now-head">
        <h2 id="live-now-title">${t("live_now_title")}</h2>
        <span class="live-now-power">${house !== null && house !== undefined ? formatPower(house, this.info) : "—"}</span>
      </div>
      ${items.length
        ? html`<ul class="live-now-list">
            ${items.map(
              (i) => html`<li>
                <span class="now-dot ${i.dashed ? "dashed" : ""}" style="--dot:${i.color}" aria-hidden="true"></span>
                <span class="now-text"><span class="now-name">${i.name}</span><span class="secondary">${i.sub}</span></span>
                <span class="now-power">${i.power !== null ? formatPower(i.power, this.info) : t("live_now_on")}</span>
              </li>`,
            )}
          </ul>`
        : html`<div class="secondary live-now-empty">${t("live_now_empty")}</div>`}
      ${split.length
        ? html`<div class="split" role="img" aria-label=${t("live_split")}>
            ${split.map((p) => html`<span style="width:${(p.share * 100).toFixed(1)}%;background:${p.color}"></span>`)}
          </div>`
        : nothing}
    </section>`;
  }

  private renderGuided(t: Translate): TemplateResult {
    const g = this._guided;
    const info = this.info;
    const store = this._store;
    const threshold = formatPower(effectiveThreshold(store), info);
    const open = g.step !== "idle";
    const ev = (id: number | undefined) => (id !== undefined ? store.events.get(id)?.ev : undefined);
    const active = { name: 0, wait: 1, weak: 1, none: 1, confirm: 2, pick: 2, running: 3, save: 3, saving: 3, error: 3, done: 4, idle: -1 }[g.step];
    const steps: Array<{ title: string; body?: TemplateResult }> = [];
    // 1. which appliance
    steps.push({
      title: active > 0 ? t("live_g_named", { name: g.name }) : t("live_g_name"),
      body: html`<span class="secondary">${t("live_g_name_text")}</span>
        <input
          class="text-input g-first"
          list="live-guided-appliances"
          aria-label=${t("live_g_name_placeholder")}
          placeholder=${t("live_g_name_placeholder")}
          .value=${g.name}
          @input=${(e: Event) => (this._guided = guidedName(this._guided, (e.target as HTMLInputElement).value))}
          @keydown=${(e: KeyboardEvent) => {
            if (e.key === "Enter") this.arm();
          }}
        />
        <datalist id="live-guided-appliances">${(this.appliances ?? []).map((a) => html`<option value=${a.name}></option>`)}</datalist>
        ${store.available ? nothing : html`<span class="secondary">${t("live_guided_unavailable")}</span>`}
        <div class="g-actions">${this.textButton(t("live_g_arm"), () => this.arm(), { accent: true, disabled: !g.name.trim() || !store.available })}</div>`,
    });
    // 2. switch it on
    const left = Math.max(0, Math.ceil((g.deadline ?? 0) - this.streamNow));
    if (g.step === "weak") {
      steps.push({
        title: t("live_g_weak"),
        body: html`<span class="secondary">${t("live_g_weak_text", { power: formatPower(g.rise ?? 0, info), threshold })}</span>
          <div class="g-actions">${this.textButton(t("live_g_retry"), () => this.arm(), { accent: true })}</div>`,
      });
    } else if (g.step === "none") {
      steps.push({
        title: t("live_g_none"),
        body: html`<span class="secondary">${t("live_g_none_text", { threshold })}</span>
          <div class="g-actions">${this.textButton(t("live_g_retry"), () => this.arm(), { accent: true })}</div>`,
      });
    } else {
      steps.push({
        title: t("live_g_wait"),
        body: html`<span class="secondary">${t("live_g_wait_text")}</span>
          <span class="live-badge waiting"><i class="pulse"></i>${t("live_g_waiting", { s: formatNumber(left, info) })}</span>`,
      });
    }
    // 3. is it this one?
    const proposed = ev(g.event);
    if (g.step === "pick") {
      steps.push({
        title: t("live_g_pick"),
        body: html`<span class="secondary">${t("live_g_pick_text", { name: g.name })}</span>
          <div class="g-actions">
            ${(g.choices ?? []).map((id) => {
              const e = ev(id);
              return e
                ? this.textButton(t("live_label_title", { delta: stepText(e.delta_w, info), time: formatTime(e.t_on, info, true) }), () => (this._guided = guidedYes(this._guided, id)), { accent: true })
                : nothing;
            })}
            ${this.textButton(t("live_g_neither"), () => (this._guided = guidedNo(this._guided, this.streamNow)))}
          </div>`,
      });
    } else if (active >= 2 && proposed) {
      steps.push({
        title: t("live_g_detected", { delta: stepText(proposed.delta_w, info), time: formatTime(proposed.t_on, info, true) }),
        body: html`<span class="secondary">${t("live_g_detected_text", { s: formatNumber(Math.max(0, Math.round(proposed.t_on - (g.since ?? proposed.t_on))), info) })}</span>
          <div class="g-actions">
            ${this.textButton(t("live_g_yes"), () => (this._guided = guidedYes(this._guided)), { accent: true })}
            ${this.textButton(t("live_g_no"), () => (this._guided = guidedNo(this._guided, this.streamNow)))}
          </div>`,
      });
    } else {
      steps.push({ title: t("live_g_confirm") });
    }
    // 4. switch it off
    const running = proposed && g.step === "running" ? Math.max(0, this.streamNow - proposed.t_on) : 0;
    let off: TemplateResult;
    if (g.step === "saving" || g.step === "save") off = html`<span class="secondary">${t("live_saving")}</span>`;
    else if (g.step === "error") {
      off = html`<div class="field-error" role="alert">${t("live_g_error")}: ${g.error}</div>
        <div class="g-actions">${this.textButton(t("live_g_retry"), () => {
          this._guided = guidedRetrySave(this._guided);
          void this.saveGuided();
        }, { accent: true })}</div>`;
    } else {
      off = html`<span class="secondary">${t("live_g_off_text")}</span>
        <span class="live-badge waiting"><i class="pulse"></i>${t("live_g_running", { d: liveDuration(running, info) })}</span>
        <div class="g-actions">${this.textButton(t("live_g_finish"), () => {
          this._guided = guidedFinish(this._guided);
          void this.saveGuided();
        })}</div>`;
    }
    steps.push({ title: t("live_g_off"), body: off });

    return html`<section class="card live-guided" aria-labelledby="live-guided-title">
      <div class="live-guided-head">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d=${mdiLightbulbOutline}></path></svg>
        <h2 id="live-guided-title">${t("live_guided_title")}</h2>
        <span class="spacer"></span>
        ${open ? this.textButton(t("live_guided_close"), () => (this._guided = guidedIdle())) : nothing}
      </div>
      ${!open
        ? html`<p class="secondary">${t("live_guided_intro")}</p>
            <div>${this.textButton(t("live_guided_start"), () => {
              this._guided = guidedStart();
              this.focusAfter = ".g-first";
            }, { accent: true })}</div>`
        : html`<ol class="g-steps" aria-live="polite">
            ${steps.map((s, k) => {
              const done = active > k;
              const on = active === k;
              return html`<li class="g-step ${done ? "done" : on ? "on" : ""}" aria-current=${on ? "step" : nothing}>
                <span class="g-num" aria-hidden="true">${done ? "✓" : k + 1}</span>
                <div class="g-body"><span class="g-title">${s.title}</span>${on && s.body ? s.body : nothing}</div>
              </li>`;
            })}
            ${g.step === "done"
              ? html`<li class="g-step done final">
                  <span class="g-num" aria-hidden="true">✓</span>
                  <div class="g-body">
                    <span class="g-title">${g.duration !== null && g.duration !== undefined
                      ? t("live_g_done", { name: g.name, power: formatPower(g.power ?? 0, info), d: liveDuration(g.duration, info) })
                      : t("live_g_done_open", { name: g.name, power: formatPower(g.power ?? 0, info) })}</span>
                    <span class="secondary">${g.effect}</span>
                    <div class="g-actions">${this.textButton(t("live_g_again"), () => {
                      this._guided = guidedStart();
                      this.focusAfter = ".g-first";
                    }, { accent: true })}</div>
                  </div>
                </li>`
              : nothing}
          </ol>`}
    </section>`;
  }

  private arm(): void {
    const next = guidedArm(this._guided, this.streamNow);
    if (next !== this._guided) this._guided = next;
  }

  private renderKnow(t: Translate): TemplateResult {
    const threshold = formatPower(this._store.threshold_w, this.info);
    const night = this._store.night_threshold_w;
    return html`<section class="card live-know" aria-labelledby="live-know-title">
      <h2 id="live-know-title">${t("live_know_title")}</h2>
      <ul>
        <li>${t("live_know_cycles")}</li>
        <li>${t("live_know_together")}</li>
        <li>
          ${night
            ? t("live_know_small_night", { threshold, night: formatPower(night, this.info) })
            : t("live_know_small", { threshold })}
        </li>
        <li>${t("live_know_panel")}</li>
      </ul>
    </section>`;
  }
}

if (!customElements.get("energy-mapper-live")) {
  customElements.define("energy-mapper-live", EnergyMapperLive);
}
