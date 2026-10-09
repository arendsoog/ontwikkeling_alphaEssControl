// AlphaESSControl sidebar panel.
//
// Plain web component, no build step and no external libraries: Home
// Assistant hands it `hass`, `narrow` and `panel` as properties. Entities
// are looked up by translation_key (stable across entity_id renames) among
// this integration's own registry entries. Charts are small hand-written
// SVG builders; history and long-term statistics come from the recorder's
// websocket API, and options/override state from `alpha_ess_local/panel_status`.

const DOMAIN = "alpha_ess_local";
const HISTORY_REFRESH_MS = 5 * 60 * 1000;
const STATUS_REFRESH_MS = 30 * 1000;

const COLOR = {
  charge: "#2dd4bf",
  discharge: "#99f6e4",
  import: "#c026d3",
  export: "#f97316",
  solar: "#fbbf24",
  house: "#94a3b8",
  battery: "#2dd4bf",
  grid: "#a855f7",
  soc: "#14b8a6",
  forecast: "#fcd34d",
};

// Labels the scheduler emits (schedule.py's _CHARGE_MESSAGES) -> display.
const ACTIONS = {
  "Charge-grid": { label: "Laden vanaf net", color: "#e879f9" },
  "Charge-PV": { label: "Laden met zon", color: "#fde047" },
  Discharge: { label: "Batterij ontladen", color: "#5eead4" },
  "No-charging": { label: "Niet laden", color: "#cbd5e1" },
  "No-discharging": { label: "Niet ontladen", color: "#94a3b8" },
};

const OVERRIDES = [
  { service: "force_charge_grid", label: "Laden vanaf net", icon: "mdi:transmission-tower-import" },
  { service: "force_charge_pv", label: "Laden met zon", icon: "mdi:solar-power" },
  { service: "force_discharge", label: "Ontladen", icon: "mdi:battery-arrow-down" },
  { service: "force_no_charging", label: "Niet laden", icon: "mdi:battery-off-outline" },
];

const SETTINGS = [
  "max_soc_positive_price",
  "max_soc_negative_price",
  "min_soc_discharge",
  "daily_min_profit",
  "max_grid_load",
  "discharge_enabled",
];

// Cumulative kWh counters whose recorder statistics give per-day totals.
const COUNTERS = {
  pv: "pv_total_energy",
  charged: "battery_total_energy_charge",
  discharged: "battery_total_energy_discharge",
  imported: "total_energy_consume_from_grid",
  exported: "total_energy_feed_to_grid",
};

const RANGES = [
  { key: 1, label: "1u" },
  { key: 6, label: "6u" },
  { key: 12, label: "12u" },
  { key: 0, label: "Alles" },
];

const WEEKDAYS = ["zo", "ma", "di", "wo", "do", "vr", "za"];

// Window for the energy-flow scene's averaged values (see _averaged).
const FLOW_AVERAGE_MS = 2 * 60 * 1000;

// Sensors whose live readings the panel buffers itself (see _recordLive).
const LIVE_KEYS = ["battery_soc", "pv_power", "total_pv_power", "grid_power", "battery_power", "extra_pv_power"];

const TABS = [
  { key: "overview", label: "Overzicht", icon: "mdi:view-dashboard-outline" },
  { key: "devices", label: "Omvormers", icon: "mdi:solar-power-variant-outline" },
  { key: "control", label: "Bediening", icon: "mdi:tune-variant" },
  { key: "history", label: "Historie", icon: "mdi:history" },
];
const TAB_STORAGE_KEY = "alpha-ess-panel-tab";

// Lifetime counters shown on the inverter card.
const LIFETIME = [
  ["pv_total_energy", "PV totaal"],
  ["battery_total_energy_charge", "Batterij geladen"],
  ["battery_total_energy_discharge", "Batterij ontladen"],
  ["total_energy_consume_from_grid", "Net afgenomen"],
  ["total_energy_feed_to_grid", "Net teruggeleverd"],
];

// ---------------------------------------------------------------- helpers

const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]
  );

const num = (value) => {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
};

const fmtNum = (value, digits = 2) =>
  value === null || value === undefined
    ? "–"
    : value.toLocaleString("nl-NL", { minimumFractionDigits: digits, maximumFractionDigits: digits });

const fmtW = (watts) => {
  if (watts === null || watts === undefined) return "–";
  return Math.abs(watts) >= 1000 ? `${fmtNum(watts / 1000, 2)} kW` : `${Math.round(watts)} W`;
};

const fmtKwh = (kwh) => (kwh === null || kwh === undefined ? "–" : `${fmtNum(kwh, 2)} kWh`);

const pad2 = (n) => String(n).padStart(2, "0");
const fmtTime = (ms) => {
  const d = new Date(ms);
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
};

const startOfDay = (offsetDays = 0) => {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  d.setDate(d.getDate() + offsetDays);
  return d;
};

// Value of a step function (sorted [t, v] samples) at time t.
const stepAt = (points, t) => {
  let lo = 0;
  let hi = points.length - 1;
  if (hi < 0 || points[0][0] > t) return null;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (points[mid][0] <= t) lo = mid;
    else hi = mid - 1;
  }
  return points[lo][1];
};

// Nearest sample to t (for chart hover), skipping gaps.
const nearest = (points, t) => {
  let best = null;
  let bestDist = Infinity;
  for (const p of points) {
    if (p[1] === null) continue;
    const dist = Math.abs(p[0] - t);
    if (dist < bestDist) {
      best = p;
      bestDist = dist;
    }
  }
  return best;
};

// Energy (Wh) under a step-function power series (W) between x0 and x1.
const integrateWh = (points, x0, x1) => {
  let wh = 0;
  for (let i = 0; i < points.length; i++) {
    const [t, v] = points[i];
    const next = i + 1 < points.length ? points[i + 1][0] : x1;
    const a = Math.max(t, x0);
    const b = Math.min(next, x1);
    if (b > a && v !== null && v > 0) wh += (v * (b - a)) / 3600000;
  }
  return wh;
};

const loadTab = () => {
  try {
    const tab = window.localStorage.getItem(TAB_STORAGE_KEY);
    return TABS.some((t) => t.key === tab) ? tab : "overview";
  } catch (err) {
    return "overview";
  }
};

const niceMax = (value) => {
  if (value <= 0) return 1;
  const exp = 10 ** Math.floor(Math.log10(value));
  for (const step of [1, 2, 2.5, 5, 10]) {
    if (value <= step * exp) return step * exp;
  }
  return 10 * exp;
};

// ---------------------------------------------------------------- charts

// Multi-series line chart over a time axis. Series may sit on the left or a
// right axis. Returns SVG markup; hover is handled by the panel using the
// meta it stores under `id`.
function lineChart(o, metaStore) {
  const W = o.w;
  const H = o.h;
  const pad = { l: 46, r: o.right ? 40 : 10, t: 10, b: 22 };
  const plotW = W - pad.l - pad.r;
  const plotH = H - pad.t - pad.b;
  const X = (t) => pad.l + ((t - o.x0) / (o.x1 - o.x0)) * plotW;
  const axis = (s) => (s.axis === "right" ? o.right : o.left);
  const Y = (v, ax) => pad.t + (1 - (v - ax.min) / (ax.max - ax.min)) * plotH;

  const parts = [];
  for (const band of o.bands || []) {
    const x0 = Math.max(pad.l, X(band.x0));
    const x1 = Math.min(W - pad.r, X(band.x1));
    if (x1 > x0) {
      parts.push(
        `<rect x="${x0}" y="${pad.t}" width="${x1 - x0}" height="${plotH}" fill="${band.color}" opacity="${band.opacity ?? 0.22}"/>`
      );
    }
  }

  for (let i = 0; i <= 4; i++) {
    const v = o.left.min + ((o.left.max - o.left.min) * i) / 4;
    const y = Y(v, o.left);
    parts.push(`<line x1="${pad.l}" x2="${W - pad.r}" y1="${y}" y2="${y}" class="grid"/>`);
    parts.push(`<text x="${pad.l - 6}" y="${y + 4}" class="axis" text-anchor="end">${esc(o.left.fmt(v))}</text>`);
    if (o.right) {
      const rv = o.right.min + ((o.right.max - o.right.min) * i) / 4;
      parts.push(`<text x="${W - pad.r + 6}" y="${y + 4}" class="axis">${esc(o.right.fmt(rv))}</text>`);
    }
  }
  if (o.left.min < 0 && o.left.max > 0) {
    const y = Y(0, o.left);
    parts.push(`<line x1="${pad.l}" x2="${W - pad.r}" y1="${y}" y2="${y}" class="zero"/>`);
  }
  for (const tick of o.xTicks) {
    parts.push(`<text x="${X(tick.t)}" y="${H - 6}" class="axis" text-anchor="middle">${esc(tick.label)}</text>`);
  }

  for (const s of o.series) {
    const ax = axis(s);
    let d = "";
    let pen = false;
    let prevY = null;
    for (const [t, v] of s.points) {
      if (v === null || t < o.x0 || t > o.x1) {
        pen = false;
        continue;
      }
      const x = X(t).toFixed(1);
      const y = Y(Math.max(ax.min, Math.min(ax.max, v)), ax).toFixed(1);
      if (!pen) d += `M${x},${y}`;
      else if (s.step) d += `H${x}V${y}`;
      else d += `L${x},${y}`;
      pen = true;
      prevY = y;
    }
    if (s.step && pen && s.stepEnd) d += `H${X(Math.min(o.x1, s.stepEnd)).toFixed(1)}`;
    if (!d) continue;
    // Area fill under a single continuous run (gaps would need one path each).
    const first = /^M([\d.]+),/.exec(d);
    if (s.area && first && prevY !== null && d.indexOf("M", 1) === -1) {
      const base = Y(Math.max(ax.min, 0), ax).toFixed(1);
      parts.push(`<path d="${d}V${base}H${first[1]}Z" fill="${s.color}" opacity="0.12"/>`);
    }
    parts.push(
      `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="${s.width ?? 1.6}" ${s.dashed ? 'stroke-dasharray="5 4"' : ""} stroke-linejoin="round"/>`
    );
  }

  if (o.now && o.now > o.x0 && o.now < o.x1) {
    const x = X(o.now);
    parts.push(`<line x1="${x}" x2="${x}" y1="${pad.t}" y2="${pad.t + plotH}" class="now"/>`);
    parts.push(`<text x="${x}" y="${pad.t - 1}" class="now-label" text-anchor="middle">nu</text>`);
  }

  parts.push(`<line class="cursor" x1="0" x2="0" y1="${pad.t}" y2="${pad.t + plotH}" visibility="hidden"/>`);
  metaStore[o.id] = { kind: "line", o, pad, W };

  return `<div class="chart-wrap"><svg viewBox="0 0 ${W} ${H}" data-chart="${o.id}" class="chart">${parts.join("")}</svg><div class="tooltip" hidden></div></div>`;
}

// Grouped bar chart (one group per label).
function barChart(o, metaStore) {
  const W = o.w;
  const H = o.h;
  const pad = { l: 46, r: 10, t: 10, b: 22 };
  const plotW = W - pad.l - pad.r;
  const plotH = H - pad.t - pad.b;
  const max = niceMax(Math.max(0, ...o.groups.flatMap((g) => g.values.map((v) => v ?? 0))));
  const Y = (v) => pad.t + (1 - v / max) * plotH;
  const groupW = plotW / o.groups.length;
  const barW = Math.min(14, (groupW * 0.8) / o.series.length);

  const parts = [];
  for (let i = 0; i <= 4; i++) {
    const v = (max * i) / 4;
    const y = Y(v);
    parts.push(`<line x1="${pad.l}" x2="${W - pad.r}" y1="${y}" y2="${y}" class="grid"/>`);
    parts.push(`<text x="${pad.l - 6}" y="${y + 4}" class="axis" text-anchor="end">${esc(o.fmt(v))}</text>`);
  }
  o.groups.forEach((g, gi) => {
    const gx = pad.l + gi * groupW + (groupW - barW * o.series.length) / 2;
    g.values.forEach((v, si) => {
      if (!v) return;
      const y = Y(v);
      parts.push(
        `<rect x="${gx + si * barW + 1}" y="${y}" width="${barW - 2}" height="${pad.t + plotH - y}" rx="2" fill="${o.series[si].color}"/>`
      );
    });
    parts.push(
      `<text x="${pad.l + gi * groupW + groupW / 2}" y="${H - 6}" class="axis ${g.highlight ? "strong" : ""}" text-anchor="middle">${esc(g.label)}</text>`
    );
  });
  parts.push(
    `<rect class="cursor-band" x="0" y="${pad.t}" width="${groupW}" height="${plotH}" visibility="hidden"/>`
  );
  metaStore[o.id] = { kind: "bar", o, pad, W, groupW };
  return `<div class="chart-wrap"><svg viewBox="0 0 ${W} ${H}" data-chart="${o.id}" class="chart">${parts.join("")}</svg><div class="tooltip" hidden></div></div>`;
}

// ---------------------------------------------------------------- house scene

// Cut-away house illustration (house.webp, 1260x848) with the flow paths
// traced over its drawn cables in the same pixel coordinates.
const SCENE_IMAGE = "/alpha_ess_local/frontend/house.webp";
const SCENE_PATHS = {
  // roof panels -> battery
  solar: ["M566,272 L566,349 L521,349 L521,566"],
  // garage-roof panels (the extra PV installation) -> battery
  extraPv: ["M883,494 L877,518 L528,518 L528,566"],
  // battery -> upstairs and downstairs rooms
  house: ["M506,566 L506,443 L360,443", "M486,622 L423,622 L423,581 L362,581 L362,592"],
  // battery -> pylon (export); reversed for import
  grid: ["M552,566 L552,534 L1130,534 Q1175,526 1205,478"],
  // battery -> EV charger on the garage wall
  ev: ["M558,622 L717,622"],
};
// Same colours as the charts. The grid's depends on direction: magenta
// while importing, orange while feeding in (see gridColour).
const SCENE_COLOR = {
  solar: "#fbbf24",
  extraPv: "#fbbf24",
  house: "#94a3b8",
  ev: "#5aa8ff",
  gridImport: "#c026d3",
  gridExport: "#f97316",
  gridIdle: "#cbd5e1",
};

function gridColour(watts) {
  if (watts === null || Math.abs(watts) < 15) return SCENE_COLOR.gridIdle;
  return watts > 0 ? SCENE_COLOR.gridImport : SCENE_COLOR.gridExport;
}

// Where each label points at in the image (x, y in image pixels). Labels sit
// in a band above or below the image, straight above/below their target,
// with a dashed leader line down/up to it.
const SCENE_TARGETS = {
  house: { x: 270, y: 330, band: "top" },
  solar: { x: 560, y: 150, band: "top" },
  extraPv: { x: 905, y: 400, band: "top" },
  grid: { x: 1185, y: 410, band: "top" },
  battery: { x: 520, y: 700, band: "bottom" },
  ev: { x: 735, y: 640, band: "bottom" },
};

// One flow group: hidden below ~15 W, faster pulses for more power.
// A solid wire over a cable the image draws in another colour (the EV
// charger's is drawn blue, like the rooms'), so it reads as its own
// circuit even when nothing flows; sceneFlow's pulses run on top of it.
function sceneCable(key, colour = SCENE_COLOR[key]) {
  return SCENE_PATHS[key]
    .map((d) => `<path class="cable" d="${d}" style="--c:${colour}" filter="url(#scene-glow)"/>`)
    .join("");
}

function sceneFlow(key, watts, reverse = false, colour = SCENE_COLOR[key]) {
  if (watts === null || Math.abs(watts) < 15) return "";
  const dur = Math.max(1.2, 4 - Math.log10(Math.abs(watts)) * 0.8).toFixed(2);
  return `<g class="flow ${reverse ? "rev" : ""}" style="--c:${colour};--dur:${dur}s" filter="url(#scene-glow)">${SCENE_PATHS[
    key
  ]
    .map((d) => `<path class="pulse" pathLength="100" d="${d}"/><path class="pulse core" pathLength="100" d="${d}"/>`)
    .join("")}</g>`;
}

// ---------------------------------------------------------------- panel

class AlphaEssPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._cards = {};
    this._charts = {};
    this._range = 0;
    this._tab = loadTab();
    this._histPeriod = "day";
    this._histAnchor = Date.now();
    this._histData = null;
    this._devices = [];
    this._history = null;
    this._stats = null;
    this._status = null;
    this._lastStates = null;
    this._timers = [];
  }

  connectedCallback() {
    this._timers.push(setInterval(() => this._loadHistory(), HISTORY_REFRESH_MS));
    this._timers.push(setInterval(() => this._loadStatus(), STATUS_REFRESH_MS));
    // Keep the "now" markers and past-hour shading moving.
    this._timers.push(setInterval(() => this._renderAll(), 60 * 1000));
  }

  disconnectedCallback() {
    this._timers.forEach(clearInterval);
    this._timers = [];
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._renderShell();
    this._update();
    if (first) {
      this._loadHistory();
      this._loadStatus();
    }
  }

  set narrow(narrow) {
    this._narrow = narrow;
    const button = this.shadowRoot.querySelector("ha-menu-button");
    if (button) button.narrow = narrow;
  }

  set panel(panel) {
    this._panel = panel;
  }

  // ------------------------------------------------------------ data

  // translation_key -> entity_id, for this integration's entities only.
  // Also groups them per device (one per AlphaESS config entry) for the
  // inverters tab; the flat map follows the first device found.
  _entityMap() {
    const map = {};
    const byDevice = new Map();
    for (const entry of Object.values(this._hass.entities || {})) {
      if (entry.platform !== DOMAIN || !entry.translation_key) continue;
      if (!(entry.translation_key in map)) map[entry.translation_key] = entry.entity_id;
      const deviceId = entry.device_id || "none";
      if (!byDevice.has(deviceId)) byDevice.set(deviceId, {});
      byDevice.get(deviceId)[entry.translation_key] = entry.entity_id;
    }
    this._devices = [...byDevice.entries()].map(([id, entities]) => {
      const device = (this._hass.devices || {})[id] || {};
      return {
        id,
        entities,
        name: device.name_by_user || device.name || "AlphaESS",
        manufacturer: device.manufacturer,
        model: device.model,
        swVersion: device.sw_version,
        hwVersion: device.hw_version,
        url: device.configuration_url,
      };
    });
    return map;
  }

  // HA pushes a new `hass` on every state change anywhere; only re-render
  // when one of *our* entities' state objects actually changed.
  _update() {
    this._entities = this._entityMap();
    const states = Object.values(this._entities).map((id) => this._hass.states[id]);
    if (
      this._lastStates &&
      states.length === this._lastStates.length &&
      states.every((s, i) => s === this._lastStates[i])
    ) {
      return;
    }
    this._lastStates = states;
    this._recordLive();
    this._renderAll();
  }

  // Keep the live power/SOC readings while the panel is open, so the
  // charts still fill for sensors the recorder excludes (no history).
  _recordLive() {
    this._live = this._live || {};
    for (const key of LIVE_KEYS) {
      for (const device of this._devices) {
        const id = device.entities[key];
        const state = id && this._hass.states[id];
        const value = state ? num(state.state) : null;
        if (!state || value === null) continue;
        const t = Date.parse(state.last_updated);
        const buf = (this._live[id] = this._live[id] || []);
        if (buf.length && buf[buf.length - 1][0] >= t) continue;
        buf.push([t, value]);
        if (buf.length > 3000) buf.splice(0, buf.length - 3000);
      }
    }
  }

  _state(key) {
    const id = this._entities[key];
    return id ? this._hass.states[id] : undefined;
  }

  _value(key) {
    const s = this._state(key);
    return s ? num(s.state) : null;
  }

  _text(key) {
    const s = this._state(key);
    if (!s || s.state === "unavailable" || s.state === "unknown") return null;
    return s.state;
  }

  _formatted(key) {
    const s = this._state(key);
    if (!s) return "–";
    return this._hass.formatEntityState ? this._hass.formatEntityState(s) : s.state;
  }

  async _loadStatus() {
    if (!this._hass) return;
    try {
      this._status = await this._hass.callWS({ type: `${DOMAIN}/panel_status` });
    } catch (err) {
      // Keep the last known status (e.g. while the entry reloads), so the
      // option toggles and settings don't vanish until the next refresh.
      console.debug("alpha-ess-panel: panel_status failed", err);
    }
    const evEnergy = this._evEnergyId();
    if (evEnergy !== this._loadedEvEnergy) {
      this._loadedEvEnergy = evEnergy;
      if (evEnergy) this._loadHistory();
    }
    this._renderAll();
  }

  _evEnergyId() {
    return (this._status && this._status.ev_charger_energy_entity) || null;
  }

  async _loadHistory() {
    if (!this._hass || !this._entities) return;
    const keys = ["battery_soc", "pv_power", "total_pv_power", "grid_power", "battery_power", "extra_pv_power"];
    const ids = [
      ...new Set(this._devices.flatMap((d) => keys.map((k) => d.entities[k])).filter(Boolean)),
    ];
    const counterIds = [
      ...new Set(
        [...this._devices.flatMap((d) => Object.values(COUNTERS).map((k) => d.entities[k])), this._evEnergyId()].filter(
          Boolean
        )
      ),
    ];
    const now = new Date();

    try {
      const raw = await this._hass.callWS({
        type: "history/history_during_period",
        start_time: startOfDay().toISOString(),
        end_time: now.toISOString(),
        entity_ids: ids,
        minimal_response: true,
        no_attributes: true,
        significant_changes_only: false,
      });
      const history = {};
      for (const [id, entries] of Object.entries(raw || {})) {
        history[id] = entries
          .map((e) => [((e.lu ?? e.lc) || 0) * 1000, num(e.s)])
          .filter((p) => p[0] > 0);
      }
      this._history = history;
    } catch (err) {
      this._history = this._history || {};
    }

    try {
      const today = await this._hass.callWS({ type: `${DOMAIN}/today_power` });
      this._todayPower = today && Array.isArray(today.samples) ? today : null;
    } catch (err) {
      // Older backend without the command: fall back to recorder history only.
      this._todayPower = null;
    }

    if (counterIds.length) {
      try {
        const week = await this._hass.callWS({
          type: "recorder/statistics_during_period",
          start_time: startOfDay(-6).toISOString(),
          end_time: now.toISOString(),
          statistic_ids: counterIds,
          period: "day",
          types: ["change"],
          units: { energy: "kWh" },
        });
        this._stats = week || {};
      } catch (err) {
        this._stats = this._stats || {};
      }
    }
    this._renderAll();
  }

  _series(key) {
    return this._seriesById(this._entities[key]);
  }

  // Recorder history, extended with live readings newer than its last point.
  _seriesById(id) {
    if (!id) return [];
    const history = (this._history && this._history[id]) || [];
    const live = (this._live && this._live[id]) || [];
    const last = history.length ? history[history.length - 1][0] : -Infinity;
    return [...history, ...live.filter((p) => p[0] > last)];
  }

  // True when the recorder returned nothing for this sensor today, which
  // usually means it is excluded in configuration.yaml.
  _noHistory(key) {
    const id = this._entities[key];
    return Boolean(id && this._history && !(this._history[id] || []).length);
  }

  // Per-day totals for a counter: {dayStartMs: kWh}.
  _dailyTotals(key, entities = this._entities) {
    return this._dailyTotalsById(entities[COUNTERS[key]]);
  }

  _dailyTotalsById(id) {
    const rows = (id && this._stats && this._stats[id]) || [];
    const out = {};
    for (const row of rows) {
      const start = typeof row.start === "number" ? row.start : Date.parse(row.start);
      out[startOfDayOf(start)] = num(row.change);
    }
    return out;
  }

  _todayTotal(key, entities = this._entities) {
    return this._dailyTotals(key, entities)[startOfDay().getTime()] ?? null;
  }

  // kWh the EV charger delivered today, from its total-energy counter.
  _evTodayKwh() {
    const id = this._evEnergyId();
    return id ? this._dailyTotalsById(id)[startOfDay().getTime()] ?? null : null;
  }

  _plan() {
    const s = this._state("schedule_today_plan");
    return (s && s.attributes.hours) || [];
  }

  // ------------------------------------------------------------ rendering

  _renderShell() {
    this.shadowRoot.innerHTML = `
      <style>${STYLE}</style>
      <div class="toolbar">
        <ha-menu-button></ha-menu-button>
        <div class="brand"><div class="logo">A</div><div><div class="title">AlphaESS</div><div class="brand-sub">Batterijsturing</div></div></div>
        <nav class="tabs">
          ${TABS.map(
            (t) => `<button class="tab" data-tab="${t.key}"><ha-icon icon="${t.icon}"></ha-icon><span>${t.label}</span></button>`
          ).join("")}
        </nav>
      </div>
      <div class="content tab-page" data-page="devices">
        <div class="device-grid" id="devices"></div>
      </div>
      <div class="content tab-page" data-page="history">
        <section class="card" id="history"></section>
      </div>
      <div class="content tab-page" data-page="control">
        <section class="card" id="controls"></section>
      </div>
      <div class="content tab-page" data-page="overview">
        <div class="grid g-status">
          <section class="card" id="system"></section>
          <section class="card" id="status"></section>
        </div>
        <div class="grid g-main">
          <section class="card flow-card" id="flow"></section>
          <section class="card" id="today"></section>
          <section class="card" id="week"></section>
          <section class="card" id="power"></section>
          <section class="card" id="soc"></section>
        </div>
        <section class="card" id="daily"></section>
      </div>`;
    this._applyTab();

    const root = this.shadowRoot;
    const menuButton = root.querySelector("ha-menu-button");
    menuButton.hass = this._hass;
    menuButton.narrow = this._narrow;

    // One set of delegated listeners; cards are re-rendered underneath.
    root.addEventListener("click", (ev) => {
      const target = ev
        .composedPath()
        .find(
          (el) =>
            el.dataset &&
            (el.dataset.entity ||
              el.dataset.service ||
              el.dataset.range ||
              el.dataset.tab ||
              el.dataset.histPeriod ||
              el.dataset.histStep ||
              el.dataset.histToday ||
              el.dataset.histGoto)
        );
      if (!target) return;
      if (target.dataset.histPeriod) {
        this._histPeriod = target.dataset.histPeriod;
        this._loadHistoryPeriod();
        return;
      }
      if (target.dataset.histStep) {
        this._historyStep(Number(target.dataset.histStep));
        return;
      }
      if (target.dataset.histToday) {
        this._histAnchor = Date.now();
        this._loadHistoryPeriod();
        return;
      }
      if (target.dataset.histGoto) {
        const [y, m, d] = target.dataset.histGoto.split("-").map(Number);
        this._histAnchor = new Date(y, m - 1, d || 1).getTime();
        this._histPeriod = target.dataset.histGotoPeriod;
        this._loadHistoryPeriod();
        return;
      }
      if (target.dataset.tab) {
        this._tab = target.dataset.tab;
        try {
          window.localStorage.setItem(TAB_STORAGE_KEY, this._tab);
        } catch (err) {
          // Storage unavailable (private window): the tab just isn't remembered.
        }
        this._applyTab();
      } else if (target.dataset.service) this._callService(target.dataset.service, target.dataset.label);
      else if (target.dataset.range !== undefined) {
        this._range = Number(target.dataset.range);
        this._renderAll();
      } else this._moreInfo(target.dataset.entity);
    });
    root.addEventListener("click", (ev) => {
      const sw = ev.composedPath().find((el) => el.classList && el.classList.contains("switch"));
      if (sw) this._toggle(sw);
    });
    root.addEventListener("pointerdown", (ev) => {
      if (ev.composedPath().some((el) => el.dataset && el.dataset.number)) this._dragging = true;
    });
    // A press without moving fires no "change": release the hold anyway.
    root.addEventListener("pointerup", () => {
      if (!this._dragging) return;
      setTimeout(() => {
        this._dragging = false;
        this._renderAll();
      }, 400);
    });
    root.addEventListener("input", (ev) => {
      const el = ev.composedPath()[0];
      if (!el.dataset || !el.dataset.number) return;
      const min = Number(el.min);
      const max = Number(el.max);
      el.style.setProperty("--pct", `${((Number(el.value) - min) / (max - min || 1)) * 100}%`);
      el.parentElement.querySelector(".slider-value").textContent = `${fmtNum(Number(el.value), Number(el.dataset.digits))} ${el.dataset.unit}`;
    });
    root.addEventListener("change", (ev) => {
      const el = ev.composedPath()[0];
      if (el.dataset && el.dataset.number) this._setNumber(el);
    });
    root.addEventListener("mousemove", (ev) => this._hover(ev));
    root.addEventListener("mouseleave", () => this._hideHover(), true);
  }

  _renderAll() {
    if (!this._hass || !this._entities) return;
    this._charts = {};
    this._card("flow", this._renderFlow());
    this._card("today", this._renderToday());
    this._card("week", this._renderWeek());
    this._card("system", this._renderSystem());
    this._card("status", this._renderStatus());
    this._card("power", this._renderPower());
    this._card("soc", this._renderSoc());
    this._card("daily", this._renderDaily());
    this._card("devices", this._renderDevices());
    this._card("history", this._renderHistory());
    // Don't rebuild the controls while a slider is being dragged.
    if (!this._dragging) this._card("controls", this._renderControls());
  }

  _applyTab() {
    const root = this.shadowRoot;
    if (this._tab === "history" && !this._histData && !this._histLoading && this._hass) this._loadHistoryPeriod();
    root.querySelectorAll(".tab").forEach((el) => el.classList.toggle("active", el.dataset.tab === this._tab));
    root.querySelectorAll(".tab-page").forEach((el) => (el.hidden = el.dataset.page !== this._tab));
    this._hideHover();
  }

  // Only touch the DOM when a card's markup actually changed, so running
  // animations and hover state survive unrelated updates.
  _card(id, html) {
    if (this._cards[id] === html) return;
    this._cards[id] = html;
    const el = this.shadowRoot.getElementById(id);
    if (el) el.innerHTML = html;
  }

  _header(icon, title, extra = "") {
    return `<div class="card-header"><ha-icon icon="${icon}"></ha-icon><span>${title}</span><div class="spacer"></div>${extra}</div>`;
  }

  _legend(items) {
    return `<div class="legend">${items
      .map((i) => `<span class="legend-item"><span class="swatch ${i.line ? "line" : ""}" style="background:${i.color}"></span>${esc(i.label)}</span>`)
      .join("")}</div>`;
  }

  _rangeButtons() {
    return `<div class="ranges">${RANGES.map(
      (r) => `<button class="range ${this._range === r.key ? "active" : ""}" data-range="${r.key}">${r.label}</button>`
    ).join("")}</div>`;
  }

  // EV charger power in W from the sensor chosen in the options (W or kW).
  _evPower() {
    const id = this._status && this._status.ev_charger_power_entity;
    const state = id && this._hass.states[id];
    const value = state ? num(state.state) : null;
    if (value === null) return null;
    return state.attributes.unit_of_measurement === "kW" ? value * 1000 : value;
  }

  // Time-weighted mean of a sensor over the last `windowMs`, from the live
  // readings buffered while the panel is open (see _recordLive). Each value
  // counts for as long as it held, so a brief spike doesn't dominate. Falls
  // back to the current value until there's a reading inside the window.
  _averaged(key, windowMs = FLOW_AVERAGE_MS) {
    const current = this._value(key);
    const id = this._entities[key];
    const points = (id && this._live && this._live[id]) || [];
    const now = Date.now();
    const start = now - windowMs;
    let total = 0;
    let covered = 0;
    for (let i = 0; i < points.length; i++) {
      const [t, v] = points[i];
      const until = i + 1 < points.length ? points[i + 1][0] : now;
      const from = Math.max(t, start);
      if (until <= from || v === null) continue;
      total += v * (until - from);
      covered += until - from;
    }
    return covered > 0 ? total / covered : current;
  }

  _renderFlow() {
    // Averaged so the labels and flow direction don't flip back and forth
    // with every 30 s reading while the battery chases a switching load.
    const roofPv = this._averaged("pv_power");
    const extraPv = this._averaged("extra_pv_power");
    const pv = roofPv === null && extraPv === null ? this._value("total_pv_power") : (roofPv || 0) + (extraPv || 0);
    const grid = this._averaged("grid_power");
    const battery = this._averaged("battery_power");
    const soc = this._value("battery_soc");
    const house = pv !== null && grid !== null && battery !== null ? pv + grid + battery : null;
    const capacity = this._status ? num(this._status.usable_battery_capacity) : null;
    const hasExtraPv =
      Boolean(this._entities.extra_pv_power) &&
      (extraPv !== null || Boolean(this._status && this._status.extra_pv_configured));
    const hasEv = Boolean(this._status && this._status.ev_charger_power_entity);
    const ev = hasEv ? this._evPower() : null;
    // The rooms get the house load minus what the car takes.
    const rooms = house !== null && ev !== null ? Math.max(0, house - ev) : house;

    const solarToday = this._value("solar_energy_today");
    const exportedToday = this._todayTotal("exported");
    const selfUse =
      solarToday && exportedToday !== null && solarToday > 0
        ? Math.max(0, Math.min(100, ((solarToday - exportedToday) / solarToday) * 100))
        : null;

    let gridName = "NET";
    if (grid !== null && grid < 0) gridName = "TERUGLEVERING";
    else if (grid !== null && grid > 0) gridName = "AFNAME";
    let batteryName = "BATTERIJ";
    if (battery !== null && battery < 0) batteryName = "LADEN";
    else if (battery !== null && battery > 0) batteryName = "ONTLADEN";
    let batterySub = "";
    if (soc !== null) {
      batterySub = `${Math.round(soc)}%`;
      if (capacity) batterySub += ` · ${fmtNum((soc / 100) * (capacity / 1000), 1)} kWh`;
    }

    const labels = [
      {
        key: "house",
        color: SCENE_COLOR.house,
        value: fmtW(rooms),
        name: hasEv ? "HUIS (ZONDER EV)" : "HUIS",
        entity: this._entities.house_load_today,
      },
      // No reading from a PV inverter means it isn't producing (an SMA
      // sleeps at night and stops answering): show 0 W rather than "–".
      { key: "solar", color: SCENE_COLOR.solar, value: fmtW(roofPv ?? 0), name: "ZON DAK", entity: this._entities.pv_power },
      hasExtraPv && {
        key: "extraPv",
        color: SCENE_COLOR.extraPv,
        value: fmtW(extraPv ?? 0),
        name: "ZON GARAGE",
        entity: this._entities.extra_pv_power,
      },
      {
        key: "grid",
        color: gridColour(grid),
        value: fmtW(grid === null ? null : Math.abs(grid)),
        name: gridName,
        entity: this._entities.grid_power,
      },
      {
        key: "battery",
        color: COLOR.battery,
        value: fmtW(battery === null ? null : Math.abs(battery)),
        name: batteryName,
        entity: this._entities.battery_soc,
        sub: batterySub,
      },
      hasEv && {
        key: "ev",
        color: SCENE_COLOR.ev,
        value: fmtW(ev),
        name: "EV-LADER",
        entity: this._status.ev_charger_power_entity,
        sub: this._evTodayKwh() === null ? "" : `${fmtNum(this._evTodayKwh(), 2)} kWh vandaag`,
      },
    ].filter(Boolean);

    const band = (which) =>
      labels
        .filter((l) => SCENE_TARGETS[l.key].band === which)
        .map((l) => {
          const pct = (SCENE_TARGETS[l.key].x / 1260) * 100;
          // Keep edge labels inside the scene instead of centring them.
          let pos = `left:${pct}%;transform:translateX(-50%)`;
          if (pct > 88) pos = "right:0";
          else if (pct < 12) pos = "left:0";
          return `
            <div class="scene-label" style="${pos}" data-entity="${esc(l.entity || "")}">
              <div class="scene-value"><span class="dot" style="background:${l.color}"></span>${esc(l.value)}</div>
              <div class="scene-name">${esc(l.name)}</div>
              ${l.sub ? `<div class="scene-sub">${esc(l.sub)}</div>` : ""}
            </div>`;
        })
        .join("");

    const leaders = labels
      .map((l) => {
        const t = SCENE_TARGETS[l.key];
        const y0 = t.band === "top" ? 0 : 848;
        return `<line x1="${t.x}" y1="${y0}" x2="${t.x}" y2="${t.y}" class="leader" stroke="${l.color}"/><circle cx="${t.x}" cy="${t.y}" r="5" fill="${l.color}" class="leader-dot"/>`;
      })
      .join("");

    return `
      ${this._header("mdi:transit-connection-variant", "Energiestroom", '<span class="live" title="Gemiddelde over de laatste 2 minuten"><span class="dot"></span>Live · gem. 2 min</span>')}
      <div class="scene">
        <div class="scene-band top">${band("top")}</div>
        <div class="scene-img">
          <img src="${SCENE_IMAGE}" alt="" draggable="false">
          <svg viewBox="0 0 1260 848" aria-hidden="true">
            <defs><filter id="scene-glow" filterUnits="userSpaceOnUse" x="0" y="0" width="1260" height="848"><feGaussianBlur stdDeviation="3.5" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter></defs>
            ${leaders}
            ${sceneFlow("solar", roofPv)}
            ${hasExtraPv ? sceneFlow("extraPv", extraPv) : ""}
            ${sceneFlow("house", rooms)}
            ${hasEv ? sceneCable("ev") + sceneFlow("ev", ev) : ""}
            ${sceneCable("grid", gridColour(grid))}
            ${sceneFlow("grid", grid, grid !== null && grid > 0, gridColour(grid))}
          </svg>
        </div>
        <div class="scene-band bottom">${band("bottom")}</div>
      </div>
      <div class="scene-foot">${selfUse === null ? "&nbsp;" : `<b>${Math.round(selfUse)}%</b> zelfconsumptie vandaag`}</div>`;
  }

  _renderToday() {
    const plan = this._plan();
    const expectedLoad = plan.length ? plan.reduce((sum, h) => sum + (num(h.house_load_w) || 0), 0) / 1000 : null;
    const forecast = this._value("solar_forecast_today");
    const remaining = this._value("schedule_remaining_solar_surplus_today");
    const rows = [
      { label: "Geladen", value: this._todayTotal("charged"), color: COLOR.charge, entity: COUNTERS.charged },
      { label: "Ontladen", value: this._todayTotal("discharged"), color: COLOR.discharge, entity: COUNTERS.discharged },
      { label: "Zon", value: this._value("solar_energy_today"), color: COLOR.solar, entity: "solar_energy_today" },
      { label: "Huis", value: this._value("house_load_today"), color: COLOR.house, entity: "house_load_today" },
      { label: "Net ingevoerd", value: this._todayTotal("imported"), color: COLOR.import, entity: COUNTERS.imported },
      { label: "Net teruggeleverd", value: this._todayTotal("exported"), color: COLOR.export, entity: COUNTERS.exported },
    ];
    if (this._evEnergyId()) {
      rows.push({ label: "EV geladen", value: this._evTodayKwh(), color: SCENE_COLOR.ev, entityId: this._evEnergyId() });
    }
    const max = Math.max(1, ...rows.map((r) => r.value || 0));
    const bars = rows
      .map(
        (r) => `
        <div class="energy-row" data-entity="${esc(r.entityId || this._entities[r.entity] || "")}">
          <div class="energy-top"><span>${r.label}</span><span class="energy-value">${fmtNum(r.value, 2)}<small> kWh</small></span></div>
          <div class="energy-bar"><div style="width:${((r.value || 0) / max) * 60}%;background:${r.color}"></div></div>
        </div>`
      )
      .join("");
    const extra = [
      { label: "Zonneverwachting vandaag", value: forecast === null ? null : forecast / 1000, entity: "solar_forecast_today" },
      { label: "Resterend zonne-overschot", value: remaining === null ? null : remaining / 1000, entity: "schedule_remaining_solar_surplus_today" },
      { label: "Verwacht verbruik", value: expectedLoad, entity: "schedule_today_plan" },
    ]
      .map(
        (r) => `
        <div class="energy-row plain" data-entity="${esc(this._entities[r.entity] || "")}">
          <div class="energy-top"><span>${r.label}</span><span class="energy-value">${fmtNum(r.value, 2)}<small> kWh</small></span></div>
        </div>`
      )
      .join("");
    return `${this._header("mdi:calendar-today", "Energie vandaag")}<div class="card-body">${bars}<div class="sep"></div>${extra}</div>`;
  }

  _renderWeek() {
    const series = [
      { key: "charged", name: "Laden", color: COLOR.charge },
      { key: "discharged", name: "Ontladen", color: COLOR.discharge },
      { key: "imported", name: "Ingevoerd", color: COLOR.import },
      { key: "exported", name: "Teruggeleverd", color: COLOR.export },
    ];
    const totals = Object.fromEntries(series.map((s) => [s.key, this._dailyTotals(s.key)]));
    const groups = [];
    for (let i = -6; i <= 0; i++) {
      const day = startOfDay(i);
      groups.push({
        label: i === 0 ? "vand." : WEEKDAYS[day.getDay()],
        highlight: i === 0,
        values: series.map((s) => totals[s.key][day.getTime()] ?? null),
      });
    }
    const hasData = groups.some((g) => g.values.some((v) => v));
    const chart = hasData
      ? barChart(
          { id: "week", w: 460, h: 240, groups, series: series.map((s) => ({ name: s.name, color: s.color })), fmt: (v) => `${fmtNum(v, 0)}` },
          this._charts
        )
      : `<div class="empty">${this._stats === null ? "Laden…" : "Nog geen statistieken (de recorder bouwt die per uur op)."}</div>`;
    const legend = this._legend(series.map((s) => ({ label: s.name, color: s.color })));
    return `${this._header("mdi:calendar-week", "Energie per week", legend)}<div class="card-body">${chart}<div class="unit-note">kWh per dag</div></div>`;
  }

  _renderSystem() {
    const soc = this._value("battery_soc");
    const battery = this._value("battery_power");
    const capacity = this._status ? num(this._status.usable_battery_capacity) : null;
    const nominal = this._status ? num(this._status.inverter_nominal_power) : null;
    const r = 70;
    const circ = 2 * Math.PI * r;
    const arc = 0.8;
    const filled = soc === null ? 0 : (Math.max(0, Math.min(100, soc)) / 100) * arc;
    const charging = battery !== null && battery < 0 ? -battery : 0;
    const discharging = battery !== null && battery > 0 ? battery : 0;
    const usage = nominal ? Math.min(100, ((charging || discharging) / nominal) * 100) : 0;

    return `
      ${this._header("mdi:battery-charging-high", "Systeemstatus")}
      <div class="card-body system">
        <div class="ring" data-entity="${esc(this._entities.battery_soc || "")}">
          <svg viewBox="0 0 180 180">
            <circle cx="90" cy="90" r="${r}" class="ring-bg" stroke-dasharray="${circ * arc} ${circ}" transform="rotate(126 90 90)"/>
            <circle cx="90" cy="90" r="${r}" class="ring-fg" stroke-dasharray="${circ * filled} ${circ}" transform="rotate(126 90 90)"/>
          </svg>
          <div class="ring-text">
            <div class="ring-value">${soc === null ? "–" : Math.round(soc)}<small>%</small></div>
            <div class="ring-sub">${
              capacity && soc !== null ? `${fmtNum((soc / 100) * (capacity / 1000), 2)} / ${fmtNum(capacity / 1000, 2)} kWh` : "&nbsp;"
            }</div>
          </div>
        </div>
        <div class="io" data-entity="${esc(this._entities.battery_power || "")}">
          <div><div class="io-label">+ Laden</div><div class="io-value ${charging ? "on" : ""}">${fmtW(charging)}</div></div>
          <div class="right"><div class="io-label">− Ontladen</div><div class="io-value ${discharging ? "on" : ""}">${fmtW(discharging)}</div></div>
        </div>
        <div class="io-bar"><div style="width:${usage}%"></div></div>
        <div class="io-note">${nominal ? `van ${fmtNum(nominal / 1000, 1)} kW omvormervermogen` : "&nbsp;"}</div>
      </div>`;
  }

  _renderStatus() {
    const st = this._status;
    const action = this._text("schedule_current_hour_action");
    const next = this._text("schedule_next_hour_action");
    const actionLabel = (value) => (ACTIONS[value] ? ACTIONS[value].label : value ?? "–");
    const tone = (ok) => (ok ? "accent" : "muted");
    const row = (label, value, cls = "", entity = "") =>
      `<div class="status-row" ${entity ? `data-entity="${esc(this._entities[entity] || "")}"` : ""}><span>${label}</span><span class="${cls}">${esc(value)}</span></div>`;
    // Sources in the order they're tried ("1. … 2. …"); the one today's
    // data actually came from in green.
    const sourcesRow = (label, sources) => {
      if (!sources) return row(label, "–");
      if (!sources.length) return row(label, "Niet ingesteld", "muted");
      const list = sources
        .map((s, i) => `<span class="source ${s.active ? "in-use" : ""}" title="${s.active ? "In gebruik" : "Niet in gebruik"}">${i + 1}. ${esc(s.name)}</span>`)
        .join("");
      return `<div class="status-row"><span>${label}</span><span class="sources">${list}</span></div>`;
    };

    const left = [
      row("Batterijsturing", st ? (st.control_enabled ? "Actief" : "Uit (alleen loggen)") : "–", st ? tone(st.control_enabled) : ""),
      row("Planning nu", actionLabel(action), "accent", "schedule_current_hour_action"),
      row("Volgend uur", actionLabel(next), "", "schedule_next_hour_action"),
      row("Dispatch-modus", this._text("dispatch_current_mode") ?? "–", "", "dispatch_current_mode"),
      row("Handmatige override", st ? (st.manual_override ? actionLabel(st.manual_override) : "Geen") : "–", st && st.manual_override ? "warn" : ""),
    ];
    const right = [
      row("Ontladen naar net via schema", this._formatted("discharge_enabled"), "", "discharge_enabled"),
      row("Prijs nu (all-in)", this._formatted("price_current_hour"), "", "price_current_hour"),
      row("Winststatus", this._formatted("price_earning_status"), "", "price_earning_status"),
      sourcesRow("Prijsbron", st && st.price_sources),
      sourcesRow("Zonnevoorspelling", st && st.solar_sources),
      row("Extra PV-sturing negatieve prijzen", st ? (st.extra_pv_control_enabled ? "Actief" : "Uit") : "–", st ? tone(st.extra_pv_control_enabled) : ""),
    ];
    return `${this._header("mdi:shield-check-outline", "Integratiestatus")}<div class="card-body status-grid"><div>${left.join("")}</div><div>${right.join("")}</div></div>`;
  }

  // Power (kW) for the charts. Primary source is the integration's own
  // 5-minute samples (today_power), which exist even when the raw power
  // sensors are excluded from the recorder; the minutes since the last
  // sample come from the live readings buffered while the panel is open.
  // Without samples it falls back to recorder history.
  _powerSeries(x0, x1) {
    const samples = (this._todayPower && this._todayPower.samples) || [];
    if (!samples.length) return this._powerSeriesFromHistory(x0, x1);

    const rows = samples.map((s) => ({
      t: s.t,
      pv: s.pv_roof + s.extra_pv,
      grid: s.grid,
      battery: s.battery,
      house: s.house,
    }));
    const last = rows[rows.length - 1].t;
    const livePv = this._series("pv_power");
    const liveExtra = this._series("extra_pv_power");
    const liveGrid = this._series("grid_power");
    const liveBattery = this._series("battery_power");
    const liveTimes = [...new Set([...livePv, ...liveGrid, ...liveBattery].map((p) => p[0]))]
      .filter((t) => t > last)
      .sort((a, b) => a - b);
    for (const t of liveTimes) {
      const p = stepAt(livePv, t);
      const g = stepAt(liveGrid, t);
      const b = stepAt(liveBattery, t);
      if (p === null || g === null || b === null) continue;
      const pvTotal = p + (stepAt(liveExtra, t) || 0);
      rows.push({ t, pv: pvTotal, grid: g, battery: b, house: Math.max(0, pvTotal + g + b) });
    }

    const out = { pv: [], house: [], battery: [], grid: [] };
    // Hours restored without samples carry no grid/battery (null): keep them
    // as gaps instead of letting null / 1000 turn into a 0 kW line.
    const kw = (w) => (w === null || w === undefined ? null : w / 1000);
    for (const r of rows) {
      if (r.t < x0 || r.t > x1) continue;
      out.pv.push([r.t, kw(r.pv)]);
      out.grid.push([r.t, kw(r.grid)]);
      // Shown as "+ = charging", the intuitive direction for a chart.
      out.battery.push([r.t, r.battery === null || r.battery === undefined ? null : -r.battery / 1000]);
      out.house.push([r.t, kw(r.house)]);
    }
    return out;
  }

  _powerSeriesFromHistory(x0, x1) {
    const pvKey = this._series("total_pv_power").length ? "total_pv_power" : "pv_power";
    const pv = this._series(pvKey);
    const grid = this._series("grid_power");
    const battery = this._series("battery_power");
    const step = Math.max(60 * 1000, (x1 - x0) / 360);
    const out = { pv: [], house: [], battery: [], grid: [] };
    for (let t = x0; t <= x1; t += step) {
      const p = stepAt(pv, t);
      const g = stepAt(grid, t);
      const b = stepAt(battery, t);
      out.pv.push([t, p === null ? null : p / 1000]);
      out.grid.push([t, g === null ? null : g / 1000]);
      out.battery.push([t, b === null ? null : -b / 1000]);
      out.house.push([t, p === null || g === null || b === null ? null : Math.max(0, p + g + b) / 1000]);
    }
    return out;
  }

  _renderPower() {
    const now = Date.now();
    const x0 = this._range ? now - this._range * 3600 * 1000 : startOfDay().getTime();
    const x1 = this._range ? now : startOfDay(1).getTime();
    const s = this._powerSeries(x0, Math.min(x1, now));
    const values = [...s.pv, ...s.house, ...s.battery, ...s.grid].map((p) => p[1]).filter((v) => v !== null);
    const header = this._header("mdi:flash", "Vermogen");
    const legend = this._legend([
      { label: "Zon", color: COLOR.solar },
      { label: "Huis", color: COLOR.house },
      { label: "Batterij (+ laden)", color: COLOR.battery },
      { label: "Net (+ afname)", color: COLOR.grid },
    ]);
    const unrecorded = [
      ["grid_power", "net"],
      ["battery_power", "batterij"],
    ]
      .filter(([key]) => !(this._todayPower && this._todayPower.samples.length) && this._noHistory(key))
      .map(([, name]) => name);
    const note = unrecorded.length
      ? `<p class="hint">Geen geschiedenis voor ${unrecorded.join(" en ")}: deze sensor wordt niet door de recorder opgeslagen (uitgesloten in configuration.yaml). De lijn vult zich zolang dit paneel open staat.</p>`
      : "";
    if (!values.length) {
      return `${header}<div class="card-body">${legend}<div class="empty">${this._history === null ? "Laden…" : "Geen geschiedenis beschikbaar."}</div>${this._rangeButtons()}</div>`;
    }
    const top = niceMax(Math.max(...values, 0.1));
    const bottom = Math.min(...values) < 0 ? -niceMax(-Math.min(...values)) : 0;
    const ticks = this._timeTicks(x0, x1);
    const chart = lineChart(
      {
        id: "power",
        w: 560,
        h: 230,
        x0,
        x1,
        left: { min: bottom, max: top, fmt: (v) => `${fmtNum(v, 1)} kW` },
        xTicks: ticks,
        series: [
          { name: "Zon", color: COLOR.solar, points: s.pv, area: true },
          { name: "Huis", color: COLOR.house, points: s.house },
          { name: "Batterij", color: COLOR.battery, points: s.battery },
          { name: "Net", color: COLOR.grid, points: s.grid },
        ],
        unit: "kW",
      },
      this._charts
    );
    return `${header}<div class="card-body">${legend}${chart}${note}${this._rangeButtons()}</div>`;
  }

  _renderSoc() {
    const soc = this._value("battery_soc");
    const points = this._series("battery_soc");
    const header = this._header("mdi:chart-areaspline", "SOC · vandaag", `<span class="muted big">${soc === null ? "" : `${Math.round(soc)}%`}</span>`);
    if (!points.length) {
      return `${header}<div class="card-body"><div class="empty">${this._history === null ? "Laden…" : "Geen geschiedenis beschikbaar."}</div></div>`;
    }
    const x0 = startOfDay().getTime();
    const x1 = startOfDay(1).getTime();
    const chart = lineChart(
      {
        id: "soc",
        w: 560,
        h: 230,
        x0,
        x1,
        left: { min: 0, max: 100, fmt: (v) => `${Math.round(v)}%` },
        xTicks: this._timeTicks(x0, x1),
        series: [{ name: "SOC", color: COLOR.soc, points: [...points, [Date.now(), soc]], area: true, step: true }],
        now: Date.now(),
        unit: "%",
      },
      this._charts
    );
    return `${header}<div class="card-body">${chart}</div>`;
  }

  _renderDaily() {
    const plan = this._plan();
    const header = this._header("mdi:chart-timeline-variant", "Dagelijkse werking");
    if (!plan.length) {
      return `${header}<div class="card-body"><div class="empty">Nog geen planning beschikbaar (prijzen of voorspelling ontbreken nog).</div></div>`;
    }
    const day0 = startOfDay().getTime();
    const x0 = day0;
    const x1 = startOfDay(1).getTime();
    const hourMs = 3600 * 1000;
    const now = Date.now();

    const bands = plan.map((h) => ({
      x0: day0 + h.hour * hourMs,
      x1: day0 + (h.hour + 1) * hourMs,
      color: (ACTIONS[h.action] || { color: "transparent" }).color,
      opacity: h.action === "Charge-grid" || h.action === "Discharge" ? 0.3 : 0.12,
    }));
    const hourly = (field) => {
      const pts = plan.map((h) => [day0 + h.hour * hourMs, num(h[field]) === null ? null : num(h[field]) / 1000]);
      const last = plan[plan.length - 1];
      return { pts, end: day0 + (last.hour + 1) * hourMs };
    };
    const solarFc = hourly("solar_forecast_w");
    const loadFc = hourly("house_load_w");
    const actual = this._powerSeries(x0, Math.min(now, x1));
    // HA records a state only when it changes: an SOC that has stayed put
    // since its last change would otherwise end the line there. Carry the
    // current value through to now.
    const socPts = this._series("battery_soc");
    const socNow = this._value("battery_soc");
    const socEnd = Math.min(now, x1);
    if (socNow !== null && (!socPts.length || socPts[socPts.length - 1][0] < socEnd)) {
      socPts.push([socEnd, socNow]);
    }

    const values = [
      ...solarFc.pts.map((p) => p[1]),
      ...loadFc.pts.map((p) => p[1]),
      ...actual.pv.map((p) => p[1]),
      ...actual.house.map((p) => p[1]),
    ].filter((v) => v !== null);
    const top = niceMax(Math.max(0.5, ...values));

    const chart = lineChart(
      {
        id: "daily",
        w: 1100,
        h: 250,
        x0,
        x1,
        left: { min: 0, max: top, fmt: (v) => `${fmtNum(v, 1)} kW` },
        right: { min: 0, max: 100, fmt: (v) => `${Math.round(v)}%` },
        xTicks: Array.from({ length: 13 }, (_, i) => ({ t: day0 + i * 2 * hourMs, label: pad2(i * 2) })),
        bands,
        now,
        series: [
          { name: "Zon (verwacht)", color: COLOR.forecast, points: solarFc.pts, step: true, stepEnd: solarFc.end, dashed: true },
          { name: "Verbruik (verwacht)", color: COLOR.house, points: loadFc.pts, step: true, stepEnd: loadFc.end, dashed: true },
          { name: "Zon", color: COLOR.solar, points: actual.pv, width: 2 },
          { name: "Verbruik", color: "#64748b", points: actual.house, width: 2 },
          { name: "SOC", color: COLOR.soc, points: socPts, axis: "right", step: true, width: 2 },
        ],
        unit: "kW",
        units: { SOC: "%" },
        plan,
      },
      this._charts
    );

    // Price strip on the same x axis, coloured by the planned action.
    const W = 1100;
    const H = 90;
    const pad = { l: 46, r: 40 };
    const plotW = W - pad.l - pad.r;
    const prices = plan.map((h) => num(h.price) ?? 0);
    const pMax = Math.max(0, ...prices);
    const pMin = Math.min(0, ...prices);
    const range = pMax - pMin || 1;
    const zeroY = 6 + (pMax / range) * 70;
    const bars = plan
      .map((h) => {
        const price = num(h.price) ?? 0;
        const x = pad.l + (h.hour / 24) * plotW;
        const height = (Math.abs(price) / range) * 70;
        const y = price >= 0 ? zeroY - height : zeroY;
        const info = ACTIONS[h.action] || { color: "#94a3b8" };
        const past = day0 + (h.hour + 1) * hourMs < now;
        return `<rect x="${x + 1.5}" y="${y}" width="${plotW / 24 - 3}" height="${Math.max(height, 1)}" rx="2" fill="${info.color}" opacity="${past ? 0.4 : 1}"/>`;
      })
      .join("");
    // Hover shows that hour's price and plan (see _hover's "strip" branch).
    this._charts["daily-strip"] = { kind: "strip", W, pad, plotW, plan };
    const strip = `
      <div class="chart-wrap">
        <svg viewBox="0 0 ${W} ${H}" class="chart strip" data-chart="daily-strip">
          <text x="${pad.l - 6}" y="12" class="axis" text-anchor="end">€ ${fmtNum(pMax, 2)}</text>
          ${pMin < 0 ? `<text x="${pad.l - 6}" y="${H - 8}" class="axis" text-anchor="end">€ ${fmtNum(pMin, 2)}</text>` : ""}
          <line x1="${pad.l}" x2="${W - pad.r}" y1="${zeroY}" y2="${zeroY}" class="zero"/>
          <rect class="cursor-band" x="0" y="0" width="${plotW / 24}" height="${H}" visibility="hidden"/>
          ${bars}
        </svg>
        <div class="tooltip" hidden></div>
      </div>`;

    const usedActions = Object.entries(ACTIONS).filter(([key]) => plan.some((h) => h.action === key));
    const legend = this._legend([
      ...usedActions.map(([, a]) => ({ label: a.label, color: a.color })),
      { label: "Zon (verwacht)", color: COLOR.forecast, line: true },
      { label: "Verbruik (verwacht)", color: COLOR.house, line: true },
      { label: "Zon", color: COLOR.solar, line: true },
      { label: "Verbruik", color: "#64748b", line: true },
      { label: "SOC", color: COLOR.soc, line: true },
    ]);
    return `${header}<div class="card-body"><div class="subtitle">Werkelijk en verwacht per uur · achtergrond = geplande actie · staven = marktprijs</div>${legend}${chart}${strip}</div>`;
  }

  _renderDevices() {
    if (!this._devices.length) {
      return `<section class="card"><div class="empty">Geen AlphaESS-apparaten gevonden.</div></section>`;
    }
    const cards = this._devices.map((device) => this._inverterCard(device));
    for (const device of this._devices) {
      const extra = this._extraPvCard(device);
      if (extra) cards.push(extra);
    }
    return cards.join("");
  }

  _stateOf(entities, key) {
    const id = entities[key];
    return id ? this._hass.states[id] : undefined;
  }

  _valueOf(entities, key) {
    const s = this._stateOf(entities, key);
    return s ? num(s.state) : null;
  }

  _historyOf(entities, key) {
    return this._seriesById(entities[key]);
  }

  _energyRows(rows) {
    const max = Math.max(0.1, ...rows.map((r) => r.value || 0));
    return rows
      .map(
        (r) => `
        <div class="energy-row" ${r.entity ? `data-entity="${esc(r.entity)}"` : ""}>
          <div class="energy-top"><span>${r.label}</span><span class="energy-value">${fmtNum(r.value, 2)}<small> kWh</small></span></div>
          <div class="energy-bar"><div style="width:${((r.value || 0) / max) * 100}%;background:${r.color}"></div></div>
        </div>`
      )
      .join("");
  }

  _inverterCard(device) {
    const e = device.entities;
    const st = this._status || {};
    const soc = this._valueOf(e, "battery_soc");
    const battery = this._valueOf(e, "battery_power");
    const pv = this._valueOf(e, "pv_power");
    const grid = this._valueOf(e, "grid_power");
    const capacity = num(st.usable_battery_capacity);
    const nominal = num(st.inverter_nominal_power);
    const pvPeak = num(st.pv_power);
    const charging = battery !== null && battery < 0;
    const discharging = battery !== null && battery > 0;
    const stateLabel = charging ? "Laden" : discharging ? "Ontladen" : "Rust";
    const power = battery === null ? null : Math.abs(battery);
    const usage = nominal && power ? Math.min(100, (power / nominal) * 100) : 0;

    const r = 52;
    const circ = 2 * Math.PI * r;
    const filled = soc === null ? 0 : (Math.max(0, Math.min(100, soc)) / 100) * 0.8;

    // Roof PV today: the inverter's own PV counter (recorder statistics),
    // else the integration's hourly values.
    let pvToday = this._todayTotal("pv", e);
    if (pvToday === null && this._todayPower) pvToday = this._todayPower.solar_roof_wh / 1000;
    const today = this._energyRows([
      { label: "Geladen", value: this._todayTotal("charged", e), color: COLOR.charge, entity: e[COUNTERS.charged] },
      { label: "Ontladen", value: this._todayTotal("discharged", e), color: COLOR.discharge, entity: e[COUNTERS.discharged] },
      { label: "Zon (dak)", value: pvToday, color: COLOR.solar, entity: e.pv_power },
      { label: "Net ingevoerd", value: this._todayTotal("imported", e), color: COLOR.import, entity: e[COUNTERS.imported] },
      { label: "Net teruggeleverd", value: this._todayTotal("exported", e), color: COLOR.export, entity: e[COUNTERS.exported] },
    ]);

    const lifetime = LIFETIME.filter(([key]) => e[key])
      .map(([key, label]) => {
        const s = this._stateOf(e, key);
        return `<div class="kv" data-entity="${esc(e[key])}"><span>${label}</span><span>${esc(
          s && this._hass.formatEntityState ? this._hass.formatEntityState(s) : s ? s.state : "–"
        )}</span></div>`;
      })
      .join("");

    const info = [
      ["Fabrikant", device.manufacturer],
      ["Model", device.model],
      ["Firmware", device.swVersion],
      ["Hardware", device.hwVersion],
    ]
      .filter(([, v]) => v)
      .map(([k, v]) => `<div class="kv"><span>${k}</span><span>${esc(v)}</span></div>`)
      .join("");

    return `
      <section class="card device">
        <div class="device-head">
          <ha-icon icon="mdi:home-battery-outline"></ha-icon>
          <div class="device-name">${esc(device.name)}</div>
          <div class="spacer"></div>
          <span class="${charging || discharging ? "accent" : "muted"}">${stateLabel}</span>
        </div>

        <div class="device-top">
          <div class="ring small" data-entity="${esc(e.battery_soc || "")}">
            <svg viewBox="0 0 130 130">
              <circle cx="65" cy="65" r="${r}" class="ring-bg" stroke-dasharray="${circ * 0.8} ${circ}" transform="rotate(126 65 65)"/>
              <circle cx="65" cy="65" r="${r}" class="ring-fg" stroke-dasharray="${circ * filled} ${circ}" transform="rotate(126 65 65)"/>
            </svg>
            <div class="ring-text"><div class="ring-value">${soc === null ? "–" : Math.round(soc)}<small>%</small></div></div>
          </div>
          <div class="device-main" data-entity="${esc(e.battery_power || "")}">
            <div class="big ${charging || discharging ? "accent" : "muted"}">${fmtW(power)}</div>
            <div class="muted">${charging ? "Laden" : discharging ? "Ontladen" : "Rust"}</div>
            <div class="io-bar"><div style="width:${usage}%"></div></div>
            <div class="io-note left">${nominal ? `van ${fmtNum(nominal / 1000, 1)} kW omvormervermogen` : ""}${
              capacity && soc !== null ? ` · ${fmtNum((soc / 100) * (capacity / 1000), 2)} / ${fmtNum(capacity / 1000, 2)} kWh` : ""
            }</div>
          </div>
        </div>

        ${this._healthSection(e)}

        <div class="section-title">Ingangen &amp; net</div>
        <div class="kv-grid">
          <div class="kv" data-entity="${esc(e.pv_power || "")}"><span>Zon (PV-ingang)</span><span>${fmtW(pv)}</span></div>
          <div class="kv"><span>PV-piekvermogen</span><span>${pvPeak ? `${fmtNum(pvPeak / 1000, 2)} kWp` : "–"}</span></div>
          <div class="kv" data-entity="${esc(e.grid_power || "")}"><span>Net</span><span>${
            grid === null ? "–" : `${fmtW(Math.abs(grid))} ${grid < 0 ? "terug" : "afname"}`
          }</span></div>
          <div class="kv" data-entity="${esc(e.dispatch_current_mode || "")}"><span>Dispatch</span><span>${esc(
            this._text("dispatch_current_mode") ?? "–"
          )}</span></div>
        </div>

        ${this._mpptSection([e.pv1_power, e.pv2_power, e.pv3_power])}

        <div class="section-title">Energie vandaag</div>
        ${today}

        <details>
          <summary><ha-icon icon="mdi:counter"></ha-icon>Totalen (levensduur)</summary>
          <div class="kv-grid">${lifetime || '<div class="muted">–</div>'}</div>
        </details>
        <details>
          <summary><ha-icon icon="mdi:information-outline"></ha-icon>Apparaatinformatie</summary>
          <div class="kv-grid">${info || '<div class="muted">Geen apparaatinformatie.</div>'}</div>
        </details>
      </section>`;
  }

  // Power per PV input (MPPT), one chip per power sensor entity_id.
  _mpptSection(ids) {
    const chips = ids
      .filter(Boolean)
      .map((id, i) => {
        const s = this._hass.states[id];
        const w = s ? num(s.state) : null;
        return `<span class="mppt ${w ? "active" : ""}" data-entity="${esc(id)}">MPPT${i + 1} · ${fmtW(w)}</span>`;
      })
      .join("");
    if (!chips) return "";
    return `
      <div class="section-title">Zon (MPPT)</div>
      <div class="mppt-row">${chips}</div>`;
  }

  // Battery health from the BMS registers (sensor.py's battery_* sensors).
  _healthSection(e) {
    if (!e.battery_soh && !e.battery_voltage) return "";
    const v = (key) => this._valueOf(e, key);
    const fmt = (key, digits, unit) => (v(key) === null ? "–" : `${fmtNum(v(key), digits)} ${unit}`);
    const tMin = v("battery_min_cell_temperature");
    const tMax = v("battery_max_cell_temperature");
    const temp =
      tMin === null || tMax === null
        ? "–"
        : tMin === tMax
          ? `${fmtNum(tMax, 1)} °C`
          : `${fmtNum(tMin, 1)} – ${fmtNum(tMax, 1)} °C`;
    const delta = v("battery_cell_voltage_delta");
    // Near full the spread is naturally larger while the BMS top-balances
    // (~70 mV at 99.6% here); only flag it well beyond that.
    const deltaClass = delta !== null && delta > 100 ? "warn" : "";
    const modules = v("battery_module_count");
    const capacity = v("battery_capacity");
    const item = (label, value, key, cls = "") =>
      `<div class="kv" ${key && e[key] ? `data-entity="${esc(e[key])}"` : ""}><span>${label}</span><span class="${cls}">${esc(value)}</span></div>`;

    return `
      <div class="section-title">Gezondheid &amp; cellen</div>
      <div class="kv-grid">
        ${item("Temperatuur (cellen)", temp, "battery_max_cell_temperature")}
        ${item("Omvormer", fmt("inverter_temperature", 1, "°C"), "inverter_temperature")}
        ${item("Spanning", fmt("battery_voltage", 1, "V"), "battery_voltage")}
        ${item("Stroom", fmt("battery_current", 1, "A"), "battery_current")}
        ${item("Cel max", fmt("battery_max_cell_voltage", 3, "V"), "battery_max_cell_voltage")}
        ${item("Cel min", fmt("battery_min_cell_voltage", 3, "V"), "battery_min_cell_voltage")}
        ${item("Δ cel", delta === null ? "–" : `${Math.round(delta)} mV`, "battery_cell_voltage_delta", deltaClass)}
        ${item("Gezondheid (SOH)", fmt("battery_soh", 1, "%"), "battery_soh", "accent")}
        ${item("Cycli (geschat)", fmt("battery_cycles", 1, ""), "battery_cycles")}
        ${item(
          "Modules · capaciteit",
          `${modules === null ? "–" : modules} · ${capacity === null ? "–" : `${fmtNum(capacity, 1)} kWh`}`,
          "battery_capacity"
        )}
      </div>`;
  }

  _extraPvCard(device) {
    const e = device.entities;
    const st = this._status || {};
    const power = this._valueOf(e, "extra_pv_power");
    if (!e.extra_pv_power || (!st.extra_pv_configured && power === null)) return "";
    const capacity = num(st.extra_pv_power_capacity);
    const usage = capacity && power ? Math.min(100, (power / capacity) * 100) : 0;
    const dayStart = startOfDay().getTime();
    const todayKwh = this._todayPower
      ? this._todayPower.extra_pv_wh / 1000
      : integrateWh(this._historyOf(e, "extra_pv_power"), dayStart, Date.now()) / 1000;
    const producing = power !== null && power > 15;

    return `
      <section class="card device">
        <div class="device-head">
          <ha-icon icon="mdi:solar-panel"></ha-icon>
          <div class="device-name">Extra PV-installatie</div>
          <div class="spacer"></div>
          <span class="${producing ? "accent" : "muted"}">${power === null ? "Niet beschikbaar" : producing ? "Produceert" : "Rust"}</span>
        </div>
        <div class="device-main wide" data-entity="${esc(e.extra_pv_power)}">
          <div class="big ${producing ? "solar" : "muted"}">${fmtW(power)}</div>
          <div class="muted">Actueel vermogen</div>
          <div class="io-bar solar"><div style="width:${usage}%"></div></div>
          <div class="io-note left">${capacity ? `van ${fmtNum(capacity / 1000, 2)} kWp` : "Piekvermogen niet ingesteld"}</div>
        </div>

        ${this._mpptSection(st.extra_pv_string_entities || [])}

        <div class="section-title">Energie vandaag</div>
        ${this._energyRows([{ label: "Opgewekt", value: this._history ? todayKwh : null, color: COLOR.solar, entity: e.extra_pv_power }])}

        <div class="section-title">Sturing bij negatieve prijzen</div>
        <div class="kv-grid">
          <div class="kv"><span>Status</span><span class="${st.extra_pv_control_enabled ? "accent" : "muted"}">${
            st.extra_pv_control_enabled ? "Actief" : "Uit"
          }</span></div>
          <div class="kv"><span>Modbus hub</span><span>${esc(st.extra_pv_modbus_hub || "Niet ingesteld")}</span></div>
        </div>
      </section>`;
  }

  // Control tab: settings grouped in columns, operated in place -- toggles
  // for switches/options, sliders for the number entities, override buttons.
  _renderControls() {
    const st = this._status;
    const toggle = (spec) => {
      let on = null;
      let attr = "";
      if (spec.option) {
        on = st ? Boolean(st[spec.option]) : null;
        attr = `data-option="${spec.option}"`;
      } else {
        const s = this._state(spec.entity);
        on = s ? s.state === "on" : null;
        attr = `data-switch="${esc(this._entities[spec.entity] || "")}"`;
      }
      if (on === null) return "";
      return `
        <div class="ctl">
          ${this._ctlLabel(spec)}
          <button class="switch ${on ? "on" : ""}" role="switch" aria-checked="${on}" ${attr} data-on="${on}" data-confirm="${esc(spec.confirm || "")}"><span></span></button>
        </div>`;
    };
    const slider = (spec) => {
      const id = this._entities[spec.entity];
      const s = id && this._hass.states[id];
      if (!s) return "";
      const value = num(s.state);
      const { min = 0, max = 100, step = 1, unit_of_measurement: unit = "" } = s.attributes;
      const shown = value === null ? "–" : `${fmtNum(value, step < 1 ? 1 : 0)} ${unit}`;
      const pct = value === null ? 0 : ((value - min) / (max - min || 1)) * 100;
      return `
        <div class="ctl">
          ${this._ctlLabel(spec)}
          <div class="slider-row">
            <input type="range" min="${min}" max="${max}" step="${step}" value="${value ?? min}" data-number="${esc(id)}" data-unit="${esc(unit)}" data-digits="${step < 1 ? 1 : 0}" style="--pct:${pct}%">
            <span class="slider-value">${esc(shown)}</span>
          </div>
        </div>`;
    };
    const column = (icon, title, body) =>
      `<section class="ctl-col">${this._header(icon, title)}${body}</section>`;

    const control = column(
      "mdi:robot-outline",
      "Sturing",
      [
        toggle({
          option: "control_enabled",
          icon: "mdi:power",
          label: "Batterijsturing",
          info: "Schrijft de beslissingen echt naar de omvormer. Uit = alleen berekenen en loggen.",
          confirm: "Batterijsturing inschakelen? De integratie gaat dan echte commando's naar de omvormer schrijven.",
        }),
        toggle({
          option: "extra_pv_control_enabled",
          icon: "mdi:solar-panel",
          label: "Extra PV-sturing negatieve prijzen",
          info: "Schakelt de extra PV-installatie uit bij negatieve prijzen, zodra de batterij haar doel-SOC heeft bereikt.",
        }),
        toggle({
          option: "persist_daily_charge_limit",
          icon: "mdi:content-save-outline",
          label: "Daglimiet bewaren na herstart",
          info: "Bewaart de eenmalige dagelijkse net-laad/ontlaadlimiet over een herstart van Home Assistant heen.",
        }),
        toggle({
          entity: "discharge_enabled",
          icon: "mdi:battery-arrow-down-outline",
          label: "Ontladen naar het net via schema toestaan",
          info: "Mag de planning één keer per dag op een duur uur extra ontladen (ook naar het net), als het schema de minimale dagelijkse winst haalt. Huisverbruik uit de batterij gaat altijd door.",
        }),
      ].join("") || '<div class="empty">Status laden…</div>'
    );

    const limits = column(
      "mdi:battery-charging-80",
      "Batterijlimieten",
      [
        slider({
          entity: "max_soc_positive_price",
          icon: "mdi:battery-arrow-up-outline",
          label: "Max SOC bij positieve prijzen",
          info: "Tot hoeveel procent de batterij laadt op uren met een positieve prijs.",
        }),
        slider({
          entity: "max_soc_negative_price",
          icon: "mdi:battery-plus-outline",
          label: "Max SOC bij negatieve prijzen",
          info: "Tot hoeveel procent de batterij laadt bij negatieve prijzen.",
        }),
        slider({
          entity: "min_soc_discharge",
          icon: "mdi:battery-low",
          label: "Min SOC bij ontladen",
          info: "Onder deze SOC wordt de batterij niet verder ontladen.",
        }),
      ].join("")
    );

    const planning = column(
      "mdi:calendar-clock",
      "Planning",
      [
        slider({
          entity: "daily_min_profit",
          icon: "mdi:cash-check",
          label: "Minimale dagelijkse winst",
          info: "Minimale verwachte winst per dag (in cent) voordat een geoptimaliseerd schema wordt gebruikt.",
        }),
        slider({
          entity: "max_grid_load",
          icon: "mdi:transmission-tower-import",
          label: "Max belasting net-laden",
          info: "Maximaal vermogen waarmee vanaf het net wordt geladen (capaciteitstarief).",
        }),
      ].join("")
    );

    const active = st && st.manual_override;
    const overrides = column(
      "mdi:hand-back-right-outline",
      "Handmatig overschrijven",
      `<p class="hint">Blijft actief tot je het opheft. Wordt alleen echt naar de omvormer geschreven als batterijsturing aan staat.</p>
      <div class="actions">${OVERRIDES.map(
        (o) => `
          <button class="action ${active === serviceToAction(o.service) ? "active" : ""}" data-service="${o.service}" data-label="${esc(o.label)}">
            <ha-icon icon="${o.icon}"></ha-icon><span>${o.label}</span>
          </button>`
      ).join("")}</div>
      <button class="action clear" data-service="clear_manual_override" data-label="">
        <ha-icon icon="mdi:restore"></ha-icon><span>Opheffen — terug naar planning</span>
      </button>`
    );

    return `<div class="ctl-grid">${control}${limits}${planning}${overrides}</div>`;
  }

  _ctlLabel(spec) {
    return `
      <div class="ctl-label">
        <ha-icon icon="${spec.icon}"></ha-icon>
        <span>${esc(spec.label)}</span>
        <span class="info" title="${esc(spec.info)}">ⓘ</span>
      </div>`;
  }

  async _toggle(el) {
    const on = el.dataset.on !== "true";
    if (on && el.dataset.confirm && !window.confirm(el.dataset.confirm)) return;
    try {
      if (el.dataset.option) {
        await this._hass.callWS({ type: `${DOMAIN}/set_option`, key: el.dataset.option, value: on });
        // These options apply live (no reload): show the new state right away.
        if (this._status) this._status = { ...this._status, [el.dataset.option]: on };
        this._renderAll();
        this._loadStatus();
      } else {
        await this._hass.callService("switch", on ? "turn_on" : "turn_off", { entity_id: el.dataset.switch });
      }
    } catch (err) {
      window.alert(`Mislukt: ${err.message || err}`);
    }
  }

  async _setNumber(el) {
    this._dragging = false;
    try {
      await this._hass.callService("number", "set_value", { entity_id: el.dataset.number, value: Number(el.value) });
    } catch (err) {
      window.alert(`Mislukt: ${err.message || err}`);
    }
  }

  _timeTicks(x0, x1) {
    const span = x1 - x0;
    const hour = 3600 * 1000;
    const step = span <= 2 * hour ? 15 * 60 * 1000 : span <= 7 * hour ? hour : span <= 13 * hour ? 2 * hour : 6 * hour;
    const ticks = [];
    for (let t = Math.ceil(x0 / step) * step; t <= x1; t += step) ticks.push({ t, label: fmtTime(t) });
    return ticks;
  }

  // ------------------------------------------------------------ history tab

  // The period shown: start/end dates (inclusive) and how rows are grouped.
  _historyRange() {
    const a = new Date(this._histAnchor);
    a.setHours(0, 0, 0, 0);
    if (this._histPeriod === "day") return { start: a, end: a, group: "hour" };
    if (this._histPeriod === "week") {
      const start = new Date(a);
      start.setDate(a.getDate() - ((a.getDay() + 6) % 7)); // Monday
      const end = new Date(start);
      end.setDate(start.getDate() + 6);
      return { start, end, group: "day" };
    }
    if (this._histPeriod === "month") {
      return { start: new Date(a.getFullYear(), a.getMonth(), 1), end: new Date(a.getFullYear(), a.getMonth() + 1, 0), group: "day" };
    }
    return { start: new Date(a.getFullYear(), 0, 1), end: new Date(a.getFullYear(), 11, 31), group: "month" };
  }

  _historyLabel() {
    const { start, end } = this._historyRange();
    const d = (x, opts) => x.toLocaleDateString("nl-NL", opts);
    if (this._histPeriod === "day") return d(start, { weekday: "long", day: "numeric", month: "long", year: "numeric" });
    if (this._histPeriod === "week") return `${d(start, { day: "numeric", month: "short" })} – ${d(end, { day: "numeric", month: "short", year: "numeric" })}`;
    if (this._histPeriod === "month") return d(start, { month: "long", year: "numeric" });
    return String(start.getFullYear());
  }

  async _loadHistoryPeriod() {
    const { start, end, group } = this._historyRange();
    const iso = (x) => `${x.getFullYear()}-${pad2(x.getMonth() + 1)}-${pad2(x.getDate())}`;
    const request = `${iso(start)}|${iso(end)}|${group}`;
    this._histRequest = request;
    this._histLoading = true;
    this._renderAll();
    try {
      const data = await this._hass.callWS({ type: `${DOMAIN}/history`, start: iso(start), end: iso(end), group });
      if (this._histRequest !== request) return; // a newer request superseded this one
      this._histData = data;
      this._histError = null;
    } catch (err) {
      this._histData = null;
      this._histError = err.message || String(err);
    }
    this._histLoading = false;
    this._renderAll();
  }

  _historyStep(step) {
    const a = new Date(this._histAnchor);
    if (this._histPeriod === "day") a.setDate(a.getDate() + step);
    else if (this._histPeriod === "week") a.setDate(a.getDate() + 7 * step);
    else if (this._histPeriod === "month") a.setMonth(a.getMonth() + step, 1);
    else a.setFullYear(a.getFullYear() + step, 0, 1);
    this._histAnchor = a.getTime();
    this._loadHistoryPeriod();
  }

  _historyRowLabel(key, group) {
    if (group === "hour") {
      const h = Number(key);
      return `${pad2(h)}:00 – ${pad2((h + 1) % 24)}:00`;
    }
    if (group === "day") {
      const [y, m, d] = key.split("-").map(Number);
      return new Date(y, m - 1, d).toLocaleDateString("nl-NL", { weekday: "short", day: "numeric", month: "short" });
    }
    const [y, m] = key.split("-").map(Number);
    return new Date(y, m - 1, 1).toLocaleDateString("nl-NL", { month: "long" });
  }

  _renderHistory() {
    const periods = [
      ["day", "Dag"],
      ["week", "Week"],
      ["month", "Maand"],
      ["year", "Jaar"],
    ];
    const controls = `
      <div class="hist-controls">
        <div class="seg">${periods
          .map(([k, l]) => `<button class="range ${this._histPeriod === k ? "active" : ""}" data-hist-period="${k}">${l}</button>`)
          .join("")}</div>
        <div class="hist-nav">
          <button class="range" data-hist-step="-1" title="Vorige">‹</button>
          <span class="hist-label">${esc(this._historyLabel())}</span>
          <button class="range" data-hist-step="1" title="Volgende">›</button>
          <button class="range" data-hist-today="1">Vandaag</button>
        </div>
      </div>`;
    const header = this._header("mdi:history", "Historie");

    if (this._histError) {
      return `${header}${controls}<div class="empty">Kon de historie niet laden: ${esc(this._histError)}</div>`;
    }
    const data = this._histData;
    if (!data) {
      return `${header}${controls}<div class="empty">${this._histLoading ? "Laden…" : ""}</div>`;
    }
    const t = data.totals;
    if (!t.hours) {
      return `${header}${controls}<div class="empty">Geen opgeslagen gegevens voor deze periode.</div>`;
    }

    const kwh = (wh) => fmtNum(wh / 1000, 2);
    const eur = (v) => `€ ${fmtNum(v, 2)}`;
    const tile = (label, value, unit, color, sub = "") => `
      <div class="hist-tile">
        <div class="hist-tile-label"><span class="swatch" style="background:${color}"></span>${label}</div>
        <div class="hist-tile-value">${value}<small> ${unit}</small></div>
        ${sub ? `<div class="hist-tile-sub">${sub}</div>` : ""}
      </div>`;
    const savings = t.savings_solar_eur + t.savings_battery_eur;
    const selfUse = t.solar_wh > 0 ? Math.max(0, Math.min(100, ((t.solar_wh - t.export_wh) / t.solar_wh) * 100)) : null;
    const tiles = `
      <div class="hist-tiles">
        ${tile("Zon dak", kwh(t.solar_wh_roof), "kWh", COLOR.solar)}
        ${tile("Zon extra", kwh(t.solar_wh_extra), "kWh", COLOR.forecast)}
        ${tile("Zon totaal", kwh(t.solar_wh), "kWh", COLOR.solar, selfUse === null ? "" : `${Math.round(selfUse)}% zelf verbruikt`)}
        ${tile("Huisverbruik", kwh(t.house_wh), "kWh", COLOR.house)}
        ${tile("Net afname", kwh(t.import_wh), "kWh", COLOR.import)}
        ${tile("Teruglevering", kwh(t.export_wh), "kWh", COLOR.export)}
        ${tile("Batterij geladen", kwh(t.battery_charge_wh), "kWh", COLOR.charge)}
        ${tile("Batterij ontladen", kwh(t.battery_discharge_wh), "kWh", COLOR.discharge)}
        ${tile("Besparing zon", eur(t.savings_solar_eur), "", COLOR.solar)}
        ${tile("Besparing batterij", eur(t.savings_battery_eur), "", COLOR.charge)}
        ${tile("Besparing totaal", eur(savings), "", "var(--accent)")}
      </div>`;

    const group = data.group;
    const chart = barChart(
      {
        id: "hist",
        w: 1100,
        h: 220,
        groups: data.rows.map((r) => ({
          label: group === "hour" ? r.key : this._historyRowLabel(r.key, group).split(" ")[group === "day" ? 1 : 0],
          title: this._historyRowLabel(r.key, group),
          values: [r.solar_wh_roof / 1000, r.solar_wh_extra / 1000, r.house_wh / 1000],
        })),
        series: [
          { name: "Zon dak", color: COLOR.solar },
          { name: "Zon extra", color: COLOR.forecast },
          { name: "Huis", color: COLOR.house },
        ],
        sums: [{ name: "Zon totaal", of: [0, 1], color: COLOR.solar }],
        fmt: (v) => fmtNum(v, v < 10 ? 1 : 0),
      },
      this._charts
    );

    // Week/month rows open that day; year rows open that month.
    const drill = group === "day" ? "day" : group === "month" ? "month" : null;
    const rows = data.rows
      .map((r) => {
        const empty = !r.hours;
        const cell = (wh) => (empty ? "–" : kwh(wh));
        const target = drill ? `data-hist-goto="${r.key}" data-hist-goto-period="${drill}"` : "";
        return `
          <tr class="${empty ? "empty-row" : ""} ${drill ? "clickable" : ""}" ${target}>
            <td>${esc(this._historyRowLabel(r.key, group))}</td>
            <td>${cell(r.solar_wh_roof)}</td>
            <td>${cell(r.solar_wh_extra)}</td>
            <td>${cell(r.solar_wh)}</td>
            <td>${cell(r.house_wh)}</td>
            <td>${cell(r.import_wh)}</td>
            <td>${cell(r.export_wh)}</td>
            <td>${cell(r.battery_charge_wh)}</td>
            <td>${cell(r.battery_discharge_wh)}</td>
            <td>${empty ? "–" : eur(r.savings_solar_eur + r.savings_battery_eur)}</td>
          </tr>`;
      })
      .join("");
    const firstCol = group === "hour" ? "Tijd" : group === "day" ? "Dag" : "Maand";
    const table = `
      <div class="table-scroll">
        <table class="hist-table">
          <thead><tr>
            <th>${firstCol}</th><th>Zon dak</th><th>Zon extra</th><th>Zon totaal</th><th>Huis</th>
            <th>Net af</th><th>Terug</th><th>Bat. geladen</th><th>Bat. ontladen</th><th>Besparing</th>
          </tr></thead>
          <tbody>${rows}</tbody>
          <tfoot><tr>
            <td>Totaal</td><td>${kwh(t.solar_wh_roof)}</td><td>${kwh(t.solar_wh_extra)}</td><td>${kwh(t.solar_wh)}</td>
            <td>${kwh(t.house_wh)}</td><td>${kwh(t.import_wh)}</td><td>${kwh(t.export_wh)}</td>
            <td>${kwh(t.battery_charge_wh)}</td><td>${kwh(t.battery_discharge_wh)}</td><td>${eur(savings)}</td>
          </tr></tfoot>
        </table>
      </div>
      <div class="unit-note">Energie in kWh · besparing t.o.v. geen zon en geen batterij${drill ? " · klik een regel voor details" : ""}</div>`;

    return `${header}${controls}${tiles}${chart}${table}`;
  }

  // ------------------------------------------------------------ hover

  _hover(ev) {
    const svg = ev.composedPath().find((el) => el.dataset && el.dataset.chart);
    if (!svg) {
      this._hideHover();
      return;
    }
    const meta = this._charts[svg.dataset.chart];
    if (!meta) return;
    const rect = svg.getBoundingClientRect();
    const px = ((ev.clientX - rect.left) / rect.width) * meta.W;
    const tooltip = svg.parentElement.querySelector(".tooltip");
    if (this._hoverSvg && this._hoverSvg !== svg) this._hideHover();
    this._hoverSvg = svg;

    let html = "";
    if (meta.kind === "line") {
      const { o, pad } = meta;
      const plotW = meta.W - pad.l - pad.r;
      const t = o.x0 + ((px - pad.l) / plotW) * (o.x1 - o.x0);
      if (t < o.x0 || t > o.x1) return this._hideHover();
      const cursor = svg.querySelector(".cursor");
      cursor.setAttribute("x1", px);
      cursor.setAttribute("x2", px);
      cursor.setAttribute("visibility", "visible");
      const lines = [];
      if (o.plan) {
        const h = new Date(t).getHours();
        const entry = o.plan.find((p) => p.hour === h);
        if (entry) {
          const info = ACTIONS[entry.action] || { label: entry.action, color: "#94a3b8" };
          lines.push(`<div class="tt-row"><span class="swatch" style="background:${info.color}"></span>${esc(info.label)}</div>`);
          lines.push(`<div class="tt-row"><span class="swatch" style="background:${COLOR.import}"></span>Marktprijs<b>€ ${fmtNum(num(entry.price), 3)}</b></div>`);
        }
      }
      for (const s of o.series) {
        const p = s.step ? [t, stepAt(s.points.filter((q) => q[1] !== null), t)] : nearest(s.points, t);
        if (!p || p[1] === null) continue;
        const unit = (o.units && o.units[s.name]) || o.unit;
        const value = unit === "%" ? `${fmtNum(p[1], 0)} %` : `${fmtNum(p[1], 2)} ${unit}`;
        lines.push(`<div class="tt-row"><span class="swatch" style="background:${s.color}"></span>${esc(s.name)}<b>${value}</b></div>`);
      }
      html = `<div class="tt-title">${fmtTime(t)}</div>${lines.join("")}`;
    } else if (meta.kind === "strip") {
      const { pad, plotW, plan } = meta;
      const h = Math.floor(((px - pad.l) / plotW) * 24);
      const entry = plan.find((p) => p.hour === h);
      if (!entry) return this._hideHover();
      const band = svg.querySelector(".cursor-band");
      band.setAttribute("x", pad.l + (h / 24) * plotW);
      band.setAttribute("visibility", "visible");
      const info = ACTIONS[entry.action] || { label: entry.action, color: "#94a3b8" };
      const price = num(entry.price);
      const eur = (v) => (v === null ? "–" : `€ ${fmtNum(v, 3)}`);
      // Same formulas as prices.mk_use_price / mk_return_price (and the
      // "Prijs nu (all-in)" sensor), so the numbers match.
      const st = this._status;
      let allIn = "";
      if (st && price !== null) {
        const vat = num(st.vat_percentage) || 0;
        const useAllIn = price * (1 + vat / 100) + (num(st.use_fee) || 0);
        const returnVat = st.apply_vat_on_return ? vat : 0;
        const returnAllIn = price * (1 + returnVat / 100) + (num(st.return_fee) || 0);
        const netNormal = num(st.network_use_fee_normal);
        const netLow = num(st.network_use_fee_low);
        let network = "";
        if (netNormal) {
          network = netLow && netLow !== netNormal ? `${eur(netLow)} – ${eur(netNormal)}` : eur(netNormal);
        }
        allIn = `
          <div class="tt-row">Afname all-in<b>${eur(useAllIn)}/kWh</b></div>
          <div class="tt-row">Teruglevering<b>${eur(returnAllIn)}/kWh</b></div>
          ${network ? `<div class="tt-row tt-sub">+ netwerkkosten bij afname<b>${network}</b></div>` : ""}`;
      }
      html = `
        <div class="tt-title">${pad2(h)}:00 – ${pad2((h + 1) % 24)}:00</div>
        <div class="tt-row"><span class="swatch" style="background:${info.color}"></span>${esc(info.label)}</div>
        <div class="tt-row">Marktprijs<b>${eur(price)}/kWh</b></div>
        ${allIn}
        ${price !== null && price < 0 ? '<div class="tt-row tt-note">Negatieve prijs</div>' : ""}
        <div class="tt-row"><span class="swatch" style="background:${COLOR.forecast}"></span>Zon (verwacht)<b>${fmtW(num(entry.solar_forecast_w))}</b></div>
        <div class="tt-row"><span class="swatch" style="background:${COLOR.house}"></span>Verbruik (verwacht)<b>${fmtW(num(entry.house_load_w))}</b></div>`;
    } else {
      const { o, pad, groupW } = meta;
      const gi = Math.floor((px - pad.l) / groupW);
      if (gi < 0 || gi >= o.groups.length) return this._hideHover();
      const band = svg.querySelector(".cursor-band");
      band.setAttribute("x", pad.l + gi * groupW);
      band.setAttribute("visibility", "visible");
      const g = o.groups[gi];
      const row = (color, name, value, cls = "") =>
        `<div class="tt-row ${cls}"><span class="swatch" style="background:${color}"></span>${esc(name)}<b>${fmtKwh(value)}</b></div>`;
      // Optional sums of several series, e.g. roof + extra PV = total solar.
      const sums = (o.sums || []).map((sum) =>
        row(sum.color, sum.name, sum.of.reduce((total, si) => total + (g.values[si] || 0), 0), "tt-sum")
      );
      html = `<div class="tt-title">${esc(g.title || g.label)}</div>${o.series
        .map((s, si) => row(s.color, s.name, g.values[si]))
        .join("")}${sums.join("")}`;
    }
    tooltip.innerHTML = html;
    tooltip.hidden = false;
    const wrap = svg.parentElement.getBoundingClientRect();
    const x = ev.clientX - wrap.left;
    const left = x > wrap.width / 2 ? x - tooltip.offsetWidth - 12 : x + 12;
    tooltip.style.left = `${Math.max(0, left)}px`;
    tooltip.style.top = `${Math.max(0, ev.clientY - wrap.top - tooltip.offsetHeight / 2)}px`;
  }

  _hideHover() {
    const svg = this._hoverSvg;
    if (!svg) return;
    this._hoverSvg = null;
    svg.querySelectorAll(".cursor, .cursor-band").forEach((el) => el.setAttribute("visibility", "hidden"));
    const tooltip = svg.parentElement && svg.parentElement.querySelector(".tooltip");
    if (tooltip) tooltip.hidden = true;
  }

  // ------------------------------------------------------------ actions

  _moreInfo(entityId) {
    if (!entityId) return;
    this.dispatchEvent(new CustomEvent("hass-more-info", { detail: { entityId }, bubbles: true, composed: true }));
  }

  async _callService(service, label) {
    if (label && !window.confirm(`Planning overschrijven: "${label}"?`)) return;
    try {
      await this._hass.callService(DOMAIN, service);
    } catch (err) {
      window.alert(`Mislukt: ${err.message || err}`);
    }
    this._loadStatus();
  }
}

const startOfDayOf = (ms) => {
  const d = new Date(ms);
  d.setHours(0, 0, 0, 0);
  return d.getTime();
};

const serviceToAction = (service) =>
  ({
    force_charge_grid: "Charge-grid",
    force_charge_pv: "Charge-PV",
    force_discharge: "Discharge",
    force_no_charging: "No-charging",
  })[service];

// ---------------------------------------------------------------- styles

const STYLE = `
  :host {
    display: block;
    min-height: 100vh;
    background: var(--primary-background-color);
    color: var(--primary-text-color);
    font-family: var(--ha-font-family-body, Roboto, sans-serif);
    --muted: var(--secondary-text-color);
    --line: var(--divider-color);
    --accent: #14b8a6;
  }
  .toolbar {
    display: flex;
    align-items: center;
    height: var(--header-height, 56px);
    padding: 0 12px;
    background: var(--app-header-background-color, var(--primary-color));
    color: var(--app-header-text-color, var(--text-primary-color, #fff));
    font-size: 20px;
    box-sizing: border-box;
  }
  .brand { display: flex; align-items: center; gap: 10px; margin: 0 24px 0 8px; }
  .logo {
    width: 34px;
    height: 34px;
    border-radius: 10px;
    background: var(--accent);
    color: #062925;
    display: flex;
    align-items: center;
    justify-content: center;
    font-weight: 700;
    font-size: 18px;
  }
  .title { font-size: 18px; line-height: 1.1; }
  .brand-sub { font-size: 11px; opacity: 0.7; }
  .tabs { display: flex; height: 100%; gap: 4px; overflow-x: auto; }
  .tab {
    display: flex;
    align-items: center;
    gap: 8px;
    height: 100%;
    padding: 0 14px;
    background: none;
    border: none;
    border-bottom: 2px solid transparent;
    color: inherit;
    opacity: 0.75;
    font: inherit;
    font-size: 15px;
    cursor: pointer;
    white-space: nowrap;
  }
  .tab ha-icon { --mdc-icon-size: 20px; }
  .tab.active { opacity: 1; border-bottom-color: currentColor; }
  .tab-page[hidden] { display: none; }
  @media (max-width: 700px) {
    .brand > div:last-child, .tab span { display: none; }
    .brand { margin-right: 8px; }
  }
  .content {
    padding: 20px;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 20px;
    box-sizing: border-box;
  }
  .grid { display: grid; gap: 20px; align-items: stretch; }
  .g-main { grid-template-columns: minmax(420px, 1.35fr) minmax(280px, 1fr) minmax(300px, 1fr); column-gap: 48px; }
  .g-main .flow-card { grid-row: span 2; }
  .g2 { grid-template-columns: 1fr 1fr; }
  .g-status { grid-template-columns: minmax(240px, 0.32fr) 1fr; column-gap: 48px; }
  @media (max-width: 1300px) {
    .g-main { grid-template-columns: 1fr 1fr; }
    .g-main .flow-card { grid-column: 1 / -1; grid-row: auto; }
  }
  @media (max-width: 1100px) {
    .g2, .g-status { grid-template-columns: 1fr; }
  }
  @media (max-width: 800px) {
    .g-main { grid-template-columns: 1fr; }
  }

  .card {
    background: none;
    border: none;
    padding: 12px 4px 20px;
    min-width: 0;
    box-sizing: border-box;
  }
  .card-header {
    display: flex;
    align-items: center;
    gap: 10px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.12em;
    font-size: 14px;
    font-weight: 400;
    margin-bottom: 18px;
  }
  .card-header ha-icon { --mdc-icon-size: 18px; opacity: 0.7; }
  .card-header .legend { text-transform: none; letter-spacing: 0; margin: 0; justify-content: flex-end; max-width: 60%; }
  .spacer { flex: 1; }
  .card-body { position: relative; }
  .subtitle { font-size: 13px; color: var(--muted); margin: -6px 0 10px; }
  [data-entity] { cursor: pointer; }
  .muted { color: var(--muted); }
  .muted.big { font-size: 15px; letter-spacing: 0; }
  .accent { color: var(--accent); }
  .warn { color: var(--warning-color, #f59e0b); }
  .empty { color: var(--muted); font-size: 14px; padding: 30px 0; text-align: center; }
  .hint { font-size: 12px; color: var(--muted); margin: 8px 0 12px; }
  .unit-note { font-size: 11px; color: var(--muted); text-align: right; }

  .live { display: flex; align-items: center; gap: 6px; text-transform: none; letter-spacing: 0; }
  .live .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--accent); animation: pulse 2s infinite; }
  @keyframes pulse { 50% { opacity: 0.3; } }

  /* energy flow scene (photo + traced flow paths, labels in bands) */
  .scene { background: #fff; border-radius: 14px; overflow: hidden; border: 1px solid var(--line); }
  .scene-img { position: relative; }
  .scene-img img { display: block; width: 100%; height: auto; user-select: none; }
  .scene-img svg { position: absolute; inset: 0; width: 100%; height: 100%; }
  .scene-band { position: relative; height: 74px; }
  .scene-band .scene-label { position: absolute; top: 12px; }
  .scene-band.bottom .scene-label { top: auto; bottom: 12px; }
  .scene-band .scene-label[style*="right:0"] { right: 12px !important; }
  .scene-band .scene-label[style*="left:0"] { left: 12px !important; }
  .leader { stroke-width: 1.6; stroke-dasharray: 4 6; opacity: 0.55; }
  .leader-dot { opacity: 0.8; }
  .pulse {
    fill: none;
    stroke: var(--c);
    stroke-linecap: round;
    stroke-linejoin: round;
    stroke-width: 5;
    stroke-dasharray: 7 93;
    animation: run var(--dur, 2.4s) linear infinite;
  }
  .pulse.core { stroke: #fff; stroke-width: 2.2; }
  .cable { fill: none; stroke: var(--c); stroke-width: 3.5; stroke-linecap: round; stroke-linejoin: round; opacity: 0.95; }
  .flow.rev .pulse { animation-direction: reverse; }
  @keyframes run { from { stroke-dashoffset: 100; } to { stroke-dashoffset: 0; } }
  @media (prefers-reduced-motion: reduce) { .pulse { animation-duration: 6s; } }
  .scene-label {
    padding: 6px 12px;
    border-radius: 10px;
    background: #fff;
    color: #1d2321;
    box-shadow: 0 2px 12px rgba(0, 0, 0, 0.1);
    line-height: 1.2;
    white-space: nowrap;
    z-index: 1;
  }
  .scene-label:hover { box-shadow: 0 0 0 2px var(--accent); }
  .scene-value { display: flex; align-items: center; gap: 6px; font-size: 18px; font-weight: 600; font-variant-numeric: tabular-nums; }
  .scene-value .dot { width: 8px; height: 8px; border-radius: 50%; }
  .scene-name { font-size: 10px; letter-spacing: 0.1em; color: #66706c; font-weight: 600; margin-top: 2px; }
  .scene-sub { font-size: 11px; color: #66706c; }
  .scene-foot { color: var(--muted); font-size: 13px; text-align: center; padding-top: 10px; }
  .scene-foot b { color: var(--accent); }
  @media (max-width: 600px) {
    .scene-band { height: 56px; }
    .scene-label { padding: 3px 6px; }
    .scene-value { font-size: 12px; }
    .scene-name, .scene-sub { font-size: 8px; }
  }

  /* energy today */
  .energy-row { padding: 8px 0; }
  .energy-top { display: flex; justify-content: space-between; font-size: 16px; font-weight: 300; }
  .energy-value { font-weight: 500; font-variant-numeric: tabular-nums; }
  .energy-value small { color: var(--muted); font-weight: 400; font-size: 11px; }
  .energy-bar { height: 6px; margin-top: 8px; border-radius: 3px; }
  .energy-bar div { height: 100%; border-radius: 4px; min-width: 6px; transition: width 0.6s; }
  .sep { height: 12px; }
  .energy-row.plain { padding: 12px 0; }

  /* charts */
  .chart-wrap { position: relative; }
  .chart { display: block; width: 100%; height: auto; overflow: visible; }
  .chart .grid { stroke: var(--line); stroke-width: 0.6; }
  .chart .zero { stroke: var(--muted); stroke-width: 0.6; opacity: 0.6; }
  .chart .axis { fill: var(--muted); font-size: 11px; }
  .chart .axis.strong { fill: var(--primary-text-color); font-weight: 600; }
  .chart .now { stroke: var(--accent); stroke-width: 1.2; stroke-dasharray: 3 3; }
  .chart .now-label { fill: var(--accent); font-size: 10px; }
  .chart .cursor { stroke: var(--primary-text-color); stroke-width: 0.8; opacity: 0.5; }
  .chart .cursor-band { fill: var(--primary-text-color); opacity: 0.06; }
  .strip { margin-top: 4px; }
  .tooltip {
    position: absolute;
    pointer-events: none;
    background: var(--card-background-color, #fff);
    border: 1px solid var(--line);
    border-radius: 8px;
    padding: 8px 10px;
    font-size: 12px;
    box-shadow: 0 4px 14px rgba(0, 0, 0, 0.15);
    min-width: 150px;
    z-index: 2;
  }
  .tt-title { font-weight: 600; margin-bottom: 4px; }
  .tt-row { display: flex; align-items: center; gap: 6px; white-space: nowrap; }
  .tt-row b { margin-left: auto; padding-left: 12px; font-weight: 500; }
  .tt-note { color: var(--warning-color, #f59e0b); font-size: 11px; }
  .tt-sub { color: var(--muted); font-size: 11px; }
  .tt-sum { border-top: 1px solid var(--line); margin-top: 3px; padding-top: 3px; font-weight: 500; }
  .legend { display: flex; flex-wrap: wrap; gap: 6px 16px; font-size: 12px; color: var(--muted); margin-bottom: 10px; }
  .legend-item { display: flex; align-items: center; gap: 6px; }
  .swatch { width: 10px; height: 10px; border-radius: 2px; display: inline-block; flex: none; }
  .swatch.line { height: 3px; border-radius: 2px; }
  .ranges { display: flex; justify-content: flex-end; gap: 4px; margin-top: 8px; }
  .range {
    background: none;
    border: none;
    color: var(--muted);
    font: inherit;
    font-size: 13px;
    padding: 4px 10px;
    border-radius: 6px;
    cursor: pointer;
  }
  .range.active { color: var(--accent); background: rgba(20, 184, 166, 0.1); }

  /* system status */
  .system { display: flex; flex-direction: column; align-items: stretch; }
  .ring { position: relative; width: 190px; margin: 0 auto; }
  .ring svg { display: block; width: 100%; }
  .ring-bg, .ring-fg { fill: none; stroke-width: 14; stroke-linecap: round; }
  .ring-bg { stroke: var(--line); }
  .ring-fg { stroke: var(--accent); transition: stroke-dasharray 0.6s; }
  .ring-text { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; }
  .ring-value { font-size: 46px; font-weight: 600; line-height: 1; }
  .ring-value small { font-size: 18px; color: var(--muted); font-weight: 400; }
  .ring-sub { font-size: 12px; color: var(--muted); margin-top: 6px; }
  .io { display: flex; justify-content: space-between; margin-top: 10px; }
  .io .right { text-align: right; }
  .io-label { font-size: 13px; color: var(--muted); }
  .io-value { font-size: 22px; color: var(--muted); font-variant-numeric: tabular-nums; }
  .io-value.on { color: var(--accent); }
  .io-bar { height: 6px; background: var(--line); border-radius: 3px; margin-top: 8px; overflow: hidden; }
  .io-bar div { height: 100%; background: var(--accent); border-radius: 3px; transition: width 0.6s; }
  .io-note { font-size: 11px; color: var(--muted); text-align: center; margin-top: 6px; }

  /* status rows */
  .status-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0 56px; }
  @media (max-width: 700px) { .status-grid { grid-template-columns: 1fr; } }
  .status-row {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    padding: 15px 0;
    font-size: 15px;
    font-weight: 300;
  }
  .status-row > span:first-child { color: var(--muted); }
  .status-row > span:last-child { font-weight: 400; }
  .status-row > span:last-child { text-align: right; }

  /* history tab */
  .hist-controls { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 20px; }
  .hist-controls .seg { display: flex; gap: 4px; }
  .hist-nav { display: flex; align-items: center; gap: 6px; }
  .hist-label { display: inline-block; min-width: 220px; text-align: center; font-size: 16px; }
  .hist-label::first-letter { text-transform: uppercase; }
  .hist-nav .range[data-hist-step] { font-size: 20px; line-height: 1; padding: 2px 12px; }
  .hist-tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 16px 24px; margin-bottom: 24px; }
  .hist-tile-label { display: flex; align-items: center; gap: 6px; font-size: 13px; color: var(--muted); font-weight: 300; }
  .hist-tile-value { font-size: 24px; font-weight: 500; margin-top: 4px; font-variant-numeric: tabular-nums; }
  .hist-tile-value small { font-size: 12px; color: var(--muted); font-weight: 400; }
  .hist-tile-sub { font-size: 12px; color: var(--muted); }
  .table-scroll { overflow-x: auto; margin-top: 16px; }
  .hist-table { width: 100%; border-collapse: collapse; font-size: 14px; font-variant-numeric: tabular-nums; }
  .hist-table th, .hist-table td { padding: 8px 10px; text-align: right; border-bottom: 1px solid var(--line); white-space: nowrap; }
  .hist-table th:first-child, .hist-table td:first-child { text-align: left; }
  .hist-table th { color: var(--muted); font-weight: 400; font-size: 12px; }
  .hist-table tfoot td { font-weight: 600; border-top: 2px solid var(--line); border-bottom: none; }
  .hist-table tr.empty-row td { color: var(--muted); opacity: 0.6; }
  .hist-table tr.clickable { cursor: pointer; }
  .hist-table tr.clickable:hover td { background: rgba(20, 184, 166, 0.08); }

  /* control tab */
  .ctl-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 24px 48px; align-items: start; }
  .ctl { padding: 10px 0 14px; }
  .ctl-label { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 15px; font-weight: 300; }
  .ctl-label ha-icon { --mdc-icon-size: 18px; opacity: 0.7; }
  .ctl-label .info { margin-left: auto; cursor: help; opacity: 0.6; font-size: 14px; }
  .switch {
    margin-top: 10px;
    width: 40px;
    height: 22px;
    border-radius: 11px;
    border: none;
    padding: 0;
    background: var(--line);
    position: relative;
    cursor: pointer;
    transition: background 0.2s;
  }
  .switch span {
    position: absolute;
    top: 3px;
    left: 3px;
    width: 16px;
    height: 16px;
    border-radius: 50%;
    background: var(--muted);
    transition: transform 0.2s, background 0.2s;
  }
  .switch.on { background: rgba(20, 184, 166, 0.35); }
  .switch.on span { transform: translateX(18px); background: var(--accent); }
  .slider-row { display: flex; align-items: center; gap: 16px; margin-top: 10px; }
  .slider-row input[type="range"] {
    flex: 1;
    -webkit-appearance: none;
    appearance: none;
    height: 6px;
    border-radius: 3px;
    background: linear-gradient(to right, var(--accent) var(--pct), var(--line) var(--pct));
    outline: none;
  }
  .slider-row input[type="range"]::-webkit-slider-thumb {
    -webkit-appearance: none;
    width: 18px;
    height: 18px;
    border-radius: 50%;
    background: var(--accent);
    cursor: pointer;
  }
  .slider-row input[type="range"]::-moz-range-thumb { width: 18px; height: 18px; border: none; border-radius: 50%; background: var(--accent); cursor: pointer; }
  .slider-value { min-width: 64px; text-align: right; font-variant-numeric: tabular-nums; }

  /* overrides */
  .actions { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  button.action {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 12px;
    border-radius: 10px;
    border: 1px solid var(--line);
    background: var(--secondary-background-color);
    color: var(--primary-text-color);
    font: inherit;
    font-size: 14px;
    cursor: pointer;
    text-align: left;
  }
  button.action:hover { border-color: var(--accent); }
  button.action.active { border-color: var(--accent); color: var(--accent); }
  button.action.clear { width: 100%; margin-top: 8px; justify-content: center; }

  /* inverters tab */
  .device-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 20px; align-items: start; }
  @media (max-width: 500px) { .device-grid { grid-template-columns: 1fr; } }
  .device-head { display: flex; align-items: center; gap: 10px; margin-bottom: 16px; font-size: 14px; }
  .device-head ha-icon { color: var(--muted); }
  .device-name { font-size: 19px; font-weight: 500; }
  .device-top { display: flex; align-items: center; gap: 24px; }
  .ring.small { width: 130px; margin: 0; flex: none; }
  .ring.small .ring-value { font-size: 34px; }
  .ring.small .ring-bg, .ring.small .ring-fg { stroke-width: 11; }
  .device-main { flex: 1; min-width: 0; }
  .big { font-size: 34px; font-weight: 500; line-height: 1.1; font-variant-numeric: tabular-nums; }
  .big.solar { color: #f59e0b; }
  .io-bar.solar div { background: ${COLOR.solar}; }
  .io-note.left { text-align: left; }
  .section-title {
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.12em;
    font-size: 12px;
    margin: 22px 0 8px;
  }
  .kv-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0 28px; }
  @media (max-width: 500px) { .kv-grid { grid-template-columns: 1fr; } }
  .kv { display: flex; justify-content: space-between; gap: 12px; padding: 11px 0; font-size: 15px; font-weight: 300; }
  .kv span:first-child { color: var(--muted); }
  .kv span:last-child { text-align: right; font-variant-numeric: tabular-nums; }
  .sources { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 4px 12px; }
  .source { color: var(--muted); }
  .source.in-use { color: var(--accent); }
  .mppt-row { display: flex; flex-wrap: wrap; gap: 8px; }
  .mppt { padding: 6px 12px; border-radius: 999px; background: var(--secondary-background-color, rgba(127,127,127,0.12)); color: var(--muted); font-size: 13px; cursor: pointer; }
  .mppt.active { color: var(--primary-text-color); box-shadow: inset 0 0 0 1px ${COLOR.solar}; }
  details { margin-top: 14px; }
  summary { display: flex; align-items: center; gap: 8px; cursor: pointer; color: var(--primary-text-color); font-size: 15px; padding: 6px 0; list-style: none; }
  summary::-webkit-details-marker { display: none; }
  summary::before { content: "▸"; color: var(--muted); font-size: 11px; transition: transform 0.2s; }
  details[open] summary::before { transform: rotate(90deg); }
  summary ha-icon { --mdc-icon-size: 18px; color: var(--muted); }

  @media (max-width: 600px) {
    .content { padding: 12px; gap: 12px; }
    .device-top { flex-direction: column; align-items: stretch; }
    .ring.small { margin: 0 auto; }
    .grid { gap: 12px; }
    .card { padding: 14px; }
    .actions { grid-template-columns: 1fr; }
  }
`;

customElements.define("alpha-ess-panel", AlphaEssPanel);
