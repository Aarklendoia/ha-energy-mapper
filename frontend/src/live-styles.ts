// Styles of the Live tab (#17), on top of the panel's: only Home Assistant
// theme variables, so light and dark themes follow; from 360 px wide.

import { css } from "lit";

export const liveStyles = css`
  :host {
    display: block;
    min-height: 0;
    background: none;
  }
  h2 {
    margin: 0;
    font-size: var(--ha-font-size-l, 16px);
    font-weight: var(--ha-font-weight-medium, 500);
  }
  .secondary {
    color: var(--secondary-text-color);
  }
  .live {
    display: flex;
    flex-direction: column;
    gap: var(--ha-space-4, 16px);
  }
  :host([narrow]) .live {
    gap: var(--ha-space-3, 12px);
  }

  /* the pulsing dot: "live" */
  @keyframes live-pulse {
    0% {
      opacity: 1;
    }
    50% {
      opacity: 0.35;
    }
    100% {
      opacity: 1;
    }
  }
  .pulse {
    display: inline-block;
    flex: none;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--error-color, #db4437);
    animation: live-pulse 1.4s infinite;
  }
  @media (prefers-reduced-motion: reduce) {
    .pulse {
      animation: none;
    }
  }
  .live-badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: var(--ha-font-size-s, 12px);
    font-weight: var(--ha-font-weight-medium, 500);
    color: var(--error-color, #db4437);
  }
  .live-badge.waiting {
    color: var(--primary-color);
  }
  .live-badge.waiting .pulse {
    background: var(--primary-color);
  }

  /* the curve card */
  .live-card {
    padding: 16px 20px;
  }
  :host([narrow]) .live-card {
    padding: 14px 12px;
  }
  .live-card-head {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 6px 16px;
    margin-bottom: 4px;
  }
  .live-hint {
    color: var(--secondary-text-color);
    font-size: var(--ha-font-size-s, 12px);
  }
  .live-legend {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 16px;
    margin-top: 8px;
    font-size: var(--ha-font-size-s, 12px);
  }
  .live-legend span {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  .sw {
    width: 10px;
    height: 10px;
    border-radius: 2px;
    display: inline-block;
  }
  .sw.house {
    background: var(--primary-color);
    opacity: 0.6;
  }
  .sw.rest {
    background: var(--disabled-color, #bdbdbd);
  }
  .mk {
    font-style: normal;
    min-width: 18px;
    height: 16px;
    border-radius: 6px;
    color: #fff;
    font-size: 11px;
    font-weight: 700;
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }
  .mk.todo {
    background: var(--warning-color, #ffa600);
  }
  .mk.known {
    background: var(--success-color, #43a047);
  }

  /* the columns: feed | now, guided, good to know */
  .live-columns {
    display: flex;
    flex-wrap: wrap;
    gap: var(--ha-space-4, 16px);
    align-items: flex-start;
  }
  .live-feed {
    flex: 2 1 520px;
    min-width: 0;
    overflow: hidden;
  }
  .live-side {
    flex: 1 1 300px;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: var(--ha-space-4, 16px);
  }
  :host([narrow]) .live-columns {
    gap: var(--ha-space-3, 12px);
  }
  :host([narrow]) .live-feed,
  :host([narrow]) .live-side,
  :host([narrow]) .live-know {
    flex: 1 1 100%;
  }
  :host([narrow]) .live-side {
    order: -1;
    gap: var(--ha-space-3, 12px);
  }

  /* the feed */
  .live-feed-head {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px 12px;
    padding: 14px 20px;
    border-bottom: 1px solid var(--divider-color);
  }
  :host([narrow]) .live-feed-head {
    padding: 12px 12px;
  }
  .live-filters {
    display: flex;
    gap: 8px;
  }
  .filter-chip.small {
    min-height: 28px;
    font-size: var(--ha-font-size-s, 12px);
  }
  .live-rows {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .live-row {
    padding: 14px 20px;
    border-top: 1px solid var(--divider-color);
    outline: none;
  }
  .live-row:first-child {
    border-top: none;
  }
  .live-row:focus-visible {
    box-shadow: inset 0 0 0 2px var(--primary-color);
  }
  .live-row.open {
    box-shadow: inset 3px 0 0 var(--primary-color);
  }
  .live-row.provisional .delta,
  .live-row.provisional .live-row-text {
    opacity: 0.6;
  }
  :host([narrow]) .live-row {
    padding: 12px 12px;
  }
  .live-row-main {
    display: flex;
    gap: 14px;
    align-items: flex-start;
  }
  :host([narrow]) .live-row-main {
    flex-wrap: wrap;
    gap: 6px 10px;
  }
  .delta {
    flex: none;
    width: 66px;
    white-space: nowrap;
    text-align: end;
    font-size: var(--ha-font-size-m, 15px);
    font-weight: var(--ha-font-weight-medium, 500);
    padding-top: 1px;
  }
  .delta.down {
    color: var(--secondary-text-color);
  }
  .delta.todo {
    color: var(--warning-color, #ffa600);
    filter: brightness(0.8);
  }
  :host([narrow]) .delta {
    width: 58px;
  }
  .live-row-text {
    flex: 1 1 0;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 4px;
  }
  .live-row-title {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 4px 10px;
  }
  .live-row-title strong {
    font-weight: var(--ha-font-weight-medium, 500);
    font-size: var(--ha-font-size-m, 15px);
  }
  .live-row-sub,
  .live-row-effect {
    color: var(--secondary-text-color);
    font-size: var(--ha-font-size-s, 13px);
  }
  .live-row-effect {
    color: var(--primary-text-color);
  }
  .tag {
    font-size: 11px;
    font-weight: var(--ha-font-weight-medium, 500);
    padding: 2px 8px;
    border-radius: 10px;
    white-space: nowrap;
    background: var(--secondary-background-color, #e5e5e5);
    color: var(--primary-text-color);
  }
  .tag-new {
    background: rgba(255, 152, 0, 0.16);
  }
  .tag-seen {
    background: rgba(255, 193, 7, 0.2);
  }
  .tag-known {
    background: rgba(67, 160, 71, 0.18);
  }
  .tag-labelled {
    background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.16);
  }
  .live-row-actions {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 4px;
    flex: none;
  }
  :host([narrow]) .live-row-actions {
    flex: 1 1 100%;
    padding-inline-start: 68px;
  }
  .menu-wrap {
    position: relative;
  }
  .icon-button.small {
    width: 36px;
    height: 36px;
  }
  .icon-button.small svg {
    width: 20px;
    height: 20px;
  }
  .menu {
    position: absolute;
    inset-inline-end: 0;
    top: 100%;
    z-index: 2;
    min-width: 200px;
    padding: 4px 0;
    background: var(--card-background-color, #fff);
    border: 1px solid var(--divider-color);
    border-radius: var(--ha-border-radius-md, 8px);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2);
  }
  .menu button {
    display: block;
    width: 100%;
    text-align: start;
    padding: 10px 16px;
    border: none;
    background: none;
    color: var(--primary-text-color);
    font: inherit;
    font-size: var(--ha-font-size-m, 14px);
    cursor: pointer;
  }
  .menu button:hover,
  .menu button:focus-visible {
    background: rgba(127, 127, 127, 0.12);
    outline: none;
  }
  .live-empty {
    padding: 20px;
    color: var(--secondary-text-color);
  }
  .live-foot {
    color: var(--secondary-text-color);
    font-size: var(--ha-font-size-s, 12px);
    padding: 12px 20px;
    border-top: 1px solid var(--divider-color);
  }
  :host([narrow]) .live-foot,
  :host([narrow]) .live-empty {
    padding: 12px;
  }

  /* inline labelling */
  .labelling {
    margin: 14px 0 4px 80px;
    padding: 16px;
    border-radius: 10px;
    background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.06);
    border: 1px solid rgba(var(--rgb-primary-color, 3, 169, 244), 0.3);
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  :host([narrow]) .labelling {
    margin: 12px 0 4px;
    padding: 12px;
  }
  .lb-head {
    display: flex;
    gap: 16px;
    align-items: center;
    font-size: var(--ha-font-size-s, 13px);
    line-height: 1.5;
  }
  .lb-head strong {
    font-weight: var(--ha-font-weight-medium, 500);
  }
  .zoom {
    flex: none;
    width: 150px;
    height: 56px;
    border-bottom: 1px solid var(--divider-color);
  }
  :host([narrow]) .zoom {
    width: 110px;
    height: 48px;
  }
  .zoom-span {
    fill: var(--primary-color);
    opacity: 0.18;
  }
  .zoom-line {
    fill: none;
    stroke: var(--primary-color);
    stroke-width: 1.5;
    vector-effect: non-scaling-stroke;
  }
  .lb-section {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .lb-label {
    color: var(--secondary-text-color);
    font-size: var(--ha-font-size-s, 12px);
    font-weight: var(--ha-font-weight-medium, 500);
  }
  .cands {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }
  .cand {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 12px;
    border: 1px solid var(--divider-color);
    border-radius: var(--ha-border-radius-md, 8px);
    background: var(--card-background-color, #fff);
    color: var(--primary-text-color);
    font: inherit;
    text-align: start;
    cursor: pointer;
  }
  .cand:focus-visible {
    outline: 2px solid var(--primary-color);
  }
  .cand.on {
    border-color: var(--primary-color);
  }
  .radio {
    flex: none;
    width: 16px;
    height: 16px;
    border-radius: 50%;
    box-sizing: border-box;
    border: 2px solid var(--secondary-text-color);
  }
  .cand.on .radio {
    border: 5px solid var(--primary-color);
  }
  .cand-text {
    flex: 1 1 0;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
    font-size: var(--ha-font-size-s, 12px);
  }
  .cand-name {
    font-weight: var(--ha-font-weight-medium, 500);
    font-size: var(--ha-font-size-m, 14px);
  }
  .cand-score {
    flex: none;
    width: 80px;
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 3px;
    font-size: var(--ha-font-size-s, 12px);
    font-weight: var(--ha-font-weight-medium, 500);
  }
  :host([narrow]) .cand-score {
    width: 52px;
  }
  .bar {
    width: 100%;
    height: 4px;
    border-radius: 2px;
    background: var(--divider-color);
    overflow: hidden;
  }
  .bar span {
    display: block;
    height: 100%;
  }
  .bar .hi {
    background: var(--success-color, #43a047);
  }
  .bar .mid {
    background: var(--warning-color, #ffa600);
  }
  .bar .lo {
    background: var(--disabled-color, #bdbdbd);
  }
  .lb-name .text-input {
    max-width: none;
  }
  .quick {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
    font-size: var(--ha-font-size-s, 12px);
  }
  .lb-effect {
    font-size: var(--ha-font-size-s, 12px);
    line-height: 1.5;
    background: var(--card-background-color, #fff);
    border: 1px solid var(--divider-color);
    border-radius: var(--ha-border-radius-md, 8px);
    padding: 10px 12px;
  }
  .lb-actions,
  .g-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }

  /* now */
  .live-now,
  .live-guided,
  .live-know {
    padding: 16px 20px;
  }
  :host([narrow]) .live-now,
  :host([narrow]) .live-guided,
  :host([narrow]) .live-know {
    padding: 14px 12px;
  }
  .live-now-head {
    display: flex;
    align-items: baseline;
    gap: 10px;
  }
  .live-now-head h2 {
    flex: 1;
  }
  .live-now-power {
    font-size: 28px;
    white-space: nowrap;
  }
  .live-now-list {
    list-style: none;
    margin: 12px 0 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }
  .live-now-list li {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .now-dot {
    flex: none;
    width: 12px;
    height: 12px;
    border-radius: 50%;
    box-sizing: border-box;
    background: var(--dot);
  }
  .now-dot.dashed {
    background: none;
    border: 2px dashed var(--dot);
  }
  .now-text {
    flex: 1 1 0;
    min-width: 0;
    display: flex;
    flex-direction: column;
    font-size: var(--ha-font-size-s, 12px);
  }
  .now-name {
    font-weight: var(--ha-font-weight-medium, 500);
    font-size: var(--ha-font-size-m, 14px);
  }
  .now-power {
    font-weight: var(--ha-font-weight-medium, 500);
    white-space: nowrap;
  }
  .live-now-empty {
    margin-top: 8px;
  }
  .split {
    margin-top: 12px;
    height: 10px;
    border-radius: 5px;
    overflow: hidden;
    display: flex;
    background: var(--divider-color);
  }
  .split span {
    display: block;
    height: 100%;
  }

  /* guided mode */
  .live-guided {
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .live-guided-head {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .live-guided-head svg {
    width: 22px;
    height: 22px;
    fill: var(--primary-color);
    flex: none;
  }
  .live-guided p {
    margin: 0;
    font-size: var(--ha-font-size-s, 13px);
    line-height: 1.5;
  }
  .g-steps {
    list-style: none;
    margin: 0;
    padding: 0;
    display: flex;
    flex-direction: column;
    gap: 14px;
  }
  .g-step {
    display: flex;
    gap: 12px;
    align-items: flex-start;
  }
  .g-num {
    flex: none;
    width: 24px;
    height: 24px;
    border-radius: 50%;
    font-size: 12px;
    font-weight: var(--ha-font-weight-medium, 500);
    display: inline-flex;
    align-items: center;
    justify-content: center;
    background: var(--secondary-background-color, #e5e5e5);
    color: var(--secondary-text-color);
  }
  .g-step.on .g-num {
    background: var(--primary-color);
    color: var(--text-primary-color, #fff);
  }
  .g-step.done .g-num {
    background: var(--success-color, #43a047);
    color: #fff;
  }
  .g-body {
    flex: 1 1 0;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 8px;
    font-size: var(--ha-font-size-s, 13px);
    line-height: 1.5;
  }
  .g-title {
    font-weight: var(--ha-font-weight-medium, 500);
    font-size: var(--ha-font-size-m, 14px);
    color: var(--disabled-text-color, #9e9e9e);
  }
  .g-step.on .g-title,
  .g-step.done .g-title {
    color: var(--primary-text-color);
  }
  .g-step.final .g-title {
    color: var(--success-color, #43a047);
  }
  .g-body .text-input {
    max-width: none;
  }

  /* good to know */
  .live-know {
    font-size: var(--ha-font-size-s, 12px);
    line-height: 1.6;
  }
  .live-know h2 {
    font-size: var(--ha-font-size-m, 14px);
  }
  .live-know ul {
    margin: 6px 0 0;
    padding-inline-start: 18px;
  }
`;
