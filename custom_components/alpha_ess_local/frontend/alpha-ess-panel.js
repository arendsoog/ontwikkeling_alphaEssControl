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
  export: "#facc15",
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

const TABS = [
  { key: "overview", label: "Overzicht", icon: "mdi:view-dashboard-outline" },
  { key: "devices", label: "Omvormers", icon: "mdi:solar-power-variant-outline" },
  { key: "control", label: "Bediening", icon: "mdi:tune-variant" },
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

// Isometric projection for the energy-flow illustration (viewBox 400x400).
const ISO = { s: 34, cx: 185, cy: 172 };
const iso = (x, y, z) => [ISO.cx + (x - y) * 0.866 * ISO.s, ISO.cy + (x + y) * 0.5 * ISO.s - z * ISO.s];
const poly = (pts, attrs) =>
  `<polygon points="${pts.map((p) => iso(...p).map((n) => n.toFixed(1)).join(",")).join(" ")}" ${attrs}/>`;

function houseStatic() {
  const out = [];
  // ground slab
  out.push(poly([[-1.6, -1.6, 0], [5.6, -1.6, 0], [5.6, 4.6, 0], [-1.6, 4.6, 0]], 'fill="#1f2937"'));
  out.push(poly([[5.6, -1.6, 0], [5.6, 4.6, 0], [5.6, 4.6, -0.35], [5.6, -1.6, -0.35]], 'fill="#111827"'));
  out.push(poly([[-1.6, 4.6, 0], [5.6, 4.6, 0], [5.6, 4.6, -0.35], [-1.6, 4.6, -0.35]], 'fill="#0b1220"'));
  // lawn + path
  out.push(poly([[-1.3, 3.3, 0], [3.0, 3.3, 0], [3.0, 4.3, 0], [-1.3, 4.3, 0]], 'fill="#14532d" opacity="0.8"'));
  out.push(poly([[3.1, 3.0, 0], [3.7, 3.0, 0], [3.7, 4.6, 0], [3.1, 4.6, 0]], 'fill="#4b5563"'));
  // trees behind the house, drawn before it so it overlaps them
  out.push(...[[4.9, -0.7, 15], [-1.2, 0.1, 12], [2.0, -1.2, 11]].map(([x, y, r]) => tree(x, y, r)));
  // back roof plane (mostly hidden, drawn first)
  out.push(poly([[-0.2, -0.3, 1.9], [4.2, -0.3, 1.9], [4.2, 1.5, 3.2], [-0.2, 1.5, 3.2]], 'fill="#374151"'));
  // walls
  out.push(poly([[0, 3, 0], [4, 3, 0], [4, 3, 2], [0, 3, 2]], 'fill="#e5e7eb"'));
  out.push(poly([[4, 0, 0], [4, 3, 0], [4, 3, 2], [4, 0, 2]], 'fill="#cbd5e1"'));
  // gable end
  out.push(poly([[4, 0, 2], [4, 3, 2], [4, 1.5, 3.1]], 'fill="#cbd5e1"'));
  // wood cladding strip
  out.push(poly([[4, 0.2, 0.9], [4, 1.0, 0.9], [4, 1.0, 1.9], [4, 0.2, 1.9]], 'fill="#b45309" opacity="0.85"'));
  // windows (front) + door
  for (const [x0, x1] of [[0.35, 1.25], [1.7, 2.6]]) {
    out.push(poly([[x0, 3, 0.9], [x1, 3, 0.9], [x1, 3, 1.65], [x0, 3, 1.65]], 'fill="#0f172a"'));
    out.push(poly([[x0, 3, 0.15], [x1, 3, 0.15], [x1, 3, 0.7], [x0, 3, 0.7]], 'fill="#1e293b"'));
  }
  out.push(poly([[3.1, 3, 0], [3.7, 3, 0], [3.7, 3, 1.35], [3.1, 3, 1.35]], 'fill="#334155"'));
  // window (side)
  out.push(poly([[4, 1.4, 0.9], [4, 2.6, 0.9], [4, 2.6, 1.6], [4, 1.4, 1.6]], 'fill="#0f172a"'));
  // front roof plane with panels
  out.push(poly([[-0.2, 3.3, 1.9], [4.2, 3.3, 1.9], [4.2, 1.5, 3.2], [-0.2, 1.5, 3.2]], 'fill="#4b5563"'));
  const roof = (u, v) => [u, 3.3 - 1.8 * v, 1.9 + 1.3 * v];
  const cols = 5;
  const rows = 2;
  for (let c = 0; c < cols; c++) {
    for (let r = 0; r < rows; r++) {
      const u0 = 0.15 + c * 0.78;
      const u1 = u0 + 0.72;
      const v0 = 0.1 + r * 0.42;
      const v1 = v0 + 0.38;
      out.push(poly([roof(u0, v0), roof(u1, v0), roof(u1, v1), roof(u0, v1)], 'fill="#1d4ed8" stroke="#93c5fd" stroke-width="0.4"'));
    }
  }
  // ridge
  const [rx0, ry0] = iso(-0.2, 1.5, 3.2);
  const [rx1, ry1] = iso(4.2, 1.5, 3.2);
  out.push(`<line x1="${rx0}" y1="${ry0}" x2="${rx1}" y2="${ry1}" stroke="#9ca3af" stroke-width="1.5"/>`);
  // battery box on the side wall
  out.push(poly([[4, 2.05, 0], [4.45, 2.05, 0], [4.45, 2.05, 1.15], [4, 2.05, 1.15]], 'fill="#9ca3af"'));
  out.push(poly([[4.45, 2.05, 0], [4.45, 2.75, 0], [4.45, 2.75, 1.15], [4.45, 2.05, 1.15]], 'fill="#f8fafc"'));
  out.push(poly([[4, 2.75, 0], [4.45, 2.75, 0], [4.45, 2.75, 1.15], [4, 2.75, 1.15]], 'fill="#e2e8f0"'));
  out.push(poly([[4, 2.05, 1.15], [4.45, 2.05, 1.15], [4.45, 2.75, 1.15], [4, 2.75, 1.15]], 'fill="#ffffff"'));
  out.push(poly([[4.45, 2.2, 0.75], [4.45, 2.6, 0.75], [4.45, 2.6, 0.8], [4.45, 2.2, 0.8]], 'fill="#2dd4bf"'));
  // grid pylon (front-left)
  const base = [-0.9, 3.9];
  const [px, py] = iso(base[0], base[1], 0);
  const [tx, ty] = iso(base[0], base[1], 3.1);
  out.push(
    `<path d="M${px - 9},${py} L${tx},${ty} L${px + 9},${py} M${px - 6},${py - 30} H${px + 6} M${tx - 14},${ty + 14} H${tx + 14} M${tx - 10},${ty + 26} H${tx + 10} M${px - 7},${py - 18} L${px + 4},${py - 50} M${px + 7},${py - 18} L${px - 4},${py - 50}" stroke="#6b7280" stroke-width="1.6" fill="none"/>`
  );
  // trees in front, clear of the flow lines
  out.push(...[[1.0, 4.4, 12], [5.4, 0.8, 11]].map(([x, y, r]) => tree(x, y, r)));
  return out.join("");
}

function tree(x, y, r) {
  const [cx, cy] = iso(x, y, 0.5);
  return `<circle cx="${cx}" cy="${cy}" r="${r}" fill="#166534"/><circle cx="${cx - r / 3}" cy="${cy - r / 3}" r="${r / 2.2}" fill="#22c55e" opacity="0.45"/>`;
}

const HOUSE_STATIC = houseStatic();

// Anchor points (label end -> device) for the four flow lines.
const FLOW_PATHS = (() => {
  const pylonTop = iso(-0.9, 3.9, 3.0);
  const houseLeft = iso(0.4, 3, 1.25);
  const panels = iso(2.0, 2.45, 2.55);
  const houseSide = iso(4, 0.6, 1.4);
  const battery = iso(4.45, 2.4, 0.55);
  return {
    grid: `M44,64 V${pylonTop[1]} L${pylonTop[0]},${pylonTop[1]} L${houseLeft[0]},${houseLeft[1]}`,
    solar: `M200,64 V${panels[1] - 30} L${panels[0]},${panels[1]}`,
    house: `M${houseSide[0]},${houseSide[1]} L356,${houseSide[1] - 60} V64`,
    battery: `M${battery[0]},${battery[1]} L${battery[0]},${battery[1] + 30} L200,336 V340`,
  };
})();

function flowLine(key, color, watts, forward) {
  const active = watts !== null && Math.abs(watts) >= 15;
  const speed = active ? Math.max(0.5, 2.6 - Math.log10(Math.abs(watts)) * 0.5) : 0;
  return `
    <path d="${FLOW_PATHS[key]}" stroke="${color}" stroke-width="2" fill="none" opacity="${active ? 0.35 : 0.12}"/>
    ${
      active
        ? `<path d="${FLOW_PATHS[key]}" stroke="${color}" stroke-width="3" fill="none" class="flow ${forward ? "" : "rev"}" style="animation-duration:${speed.toFixed(2)}s" filter="url(#glow)"/>`
        : ""
    }`;
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
    this._renderAll();
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
      this._status = null;
    }
    this._renderAll();
  }

  async _loadHistory() {
    if (!this._hass || !this._entities) return;
    const keys = ["battery_soc", "pv_power", "total_pv_power", "grid_power", "battery_power", "extra_pv_power"];
    const ids = [
      ...new Set(this._devices.flatMap((d) => keys.map((k) => d.entities[k])).filter(Boolean)),
    ];
    const counterIds = [
      ...new Set(this._devices.flatMap((d) => Object.values(COUNTERS).map((k) => d.entities[k])).filter(Boolean)),
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
    const id = this._entities[key];
    return (id && this._history && this._history[id]) || [];
  }

  // Per-day totals for a counter: {dayStartMs: kWh}.
  _dailyTotals(key, entities = this._entities) {
    const id = entities[COUNTERS[key]];
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
      <div class="content tab-page" data-page="control">
        <section class="card" id="control-status"></section>
        <div class="grid g2">
          <section class="card" id="overrides"></section>
          <section class="card" id="settings"></section>
        </div>
      </div>
      <div class="content tab-page" data-page="overview">
        <div class="grid g3">
          <section class="card" id="flow"></section>
          <section class="card" id="today"></section>
          <section class="card" id="week"></section>
        </div>
        <div class="grid g-status">
          <section class="card" id="system"></section>
          <section class="card" id="status"></section>
        </div>
        <div class="grid g2">
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
        .find((el) => el.dataset && (el.dataset.entity || el.dataset.service || el.dataset.range || el.dataset.tab));
      if (!target) return;
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
    this._card("control-status", this._renderStatus());
    this._card("overrides", this._renderOverrides());
    this._card("settings", this._renderSettings());
  }

  _applyTab() {
    const root = this.shadowRoot;
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

  _renderFlow() {
    const pv = this._value("total_pv_power") ?? this._value("pv_power");
    const grid = this._value("grid_power");
    const battery = this._value("battery_power");
    const soc = this._value("battery_soc");
    const house = pv !== null && grid !== null && battery !== null ? pv + grid + battery : null;
    const capacity = this._status ? num(this._status.usable_battery_capacity) : null;

    const solarToday = this._value("solar_energy_today");
    const exportedToday = this._todayTotal("exported");
    const selfUse =
      solarToday && exportedToday !== null && solarToday > 0
        ? Math.max(0, Math.min(100, ((solarToday - exportedToday) / solarToday) * 100))
        : null;

    const gridLabel = grid === null ? "NET" : grid < 0 ? "TERUGLEVERING" : "INVOER";
    const batteryLabel = battery === null ? "BATTERIJ" : battery < 0 ? "LADEN" : battery > 0 ? "ONTLADEN" : "RUST";
    const batterySub =
      soc === null
        ? ""
        : `${Math.round(soc)}%${capacity ? ` · ${fmtNum((soc / 100) * (capacity / 1000), 1)} kWh` : ""}`;

    const label = (x, y, value, name, entity, sub = "") => `
      <g class="flow-label" data-entity="${esc(this._entities[entity] || "")}">
        <text x="${x}" y="${y}" class="flow-value" text-anchor="middle">${esc(value)}</text>
        <text x="${x}" y="${y + 14}" class="flow-name" text-anchor="middle">${esc(name)}</text>
        ${sub ? `<text x="${x}" y="${y + 27}" class="flow-sub" text-anchor="middle">${esc(sub)}</text>` : ""}
      </g>`;

    return `
      ${this._header("mdi:transit-connection-variant", "Energiestroom", '<span class="live"><span class="dot"></span>Live</span>')}
      <div class="scene">
        <svg viewBox="0 0 400 400">
          <defs>
            <filter id="glow" x="-50%" y="-50%" width="200%" height="200%">
              <feGaussianBlur stdDeviation="2.2" result="b"/>
              <feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge>
            </filter>
          </defs>
          ${HOUSE_STATIC}
          ${flowLine("grid", COLOR.grid, grid, grid !== null && grid > 0)}
          ${flowLine("solar", COLOR.solar, pv, true)}
          ${flowLine("house", COLOR.discharge, house, true)}
          ${flowLine("battery", COLOR.battery, battery, battery !== null && battery < 0)}
          ${label(44, 34, fmtW(grid === null ? null : Math.abs(grid)), gridLabel, "grid_power")}
          ${label(200, 34, fmtW(pv), "ZON", this._entities.total_pv_power ? "total_pv_power" : "pv_power")}
          ${label(356, 34, fmtW(house), "HUIS", "house_load_today")}
          ${label(200, 356, fmtW(battery === null ? null : Math.abs(battery)), batteryLabel, "battery_soc", batterySub)}
        </svg>
        <div class="scene-foot">${selfUse === null ? "&nbsp;" : `<b>${Math.round(selfUse)}%</b> zelfconsumptie vandaag`}</div>
      </div>`;
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
    const max = Math.max(1, ...rows.map((r) => r.value || 0));
    const bars = rows
      .map(
        (r) => `
        <div class="energy-row" data-entity="${esc(this._entities[r.entity] || "")}">
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
    return `${this._header("mdi:calendar-week", "Energie per week")}<div class="card-body">${this._legend(
      series.map((s) => ({ label: s.name, color: s.color }))
    )}${chart}<div class="unit-note">kWh per dag</div></div>`;
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

    const left = [
      row("Batterijsturing", st ? (st.control_enabled ? "Actief" : "Uit (alleen loggen)") : "–", st ? tone(st.control_enabled) : ""),
      row("Planning nu", actionLabel(action), "accent", "schedule_current_hour_action"),
      row("Volgend uur", actionLabel(next), "", "schedule_next_hour_action"),
      row("Dispatch-modus", this._text("dispatch_current_mode") ?? "–", "", "dispatch_current_mode"),
      row("Handmatige override", st ? (st.manual_override ? actionLabel(st.manual_override) : "Geen") : "–", st && st.manual_override ? "warn" : ""),
    ];
    const right = [
      row("Ontladen naar net", this._formatted("discharge_enabled"), "", "discharge_enabled"),
      row("Prijs nu (all-in)", this._formatted("price_current_hour"), "", "price_current_hour"),
      row("Winststatus", this._formatted("price_earning_status"), "", "price_earning_status"),
      row("Prijsbron", st ? st.price_source ?? "Niet ingesteld" : "–", st && st.price_source ? "" : "muted"),
      row("Zonnevoorspelling", st ? (st.solar_forecast_sources ? `${st.solar_forecast_sources} locatie(s)` : "Niet ingesteld") : "–", st && st.solar_forecast_sources ? "" : "muted"),
      row("Extra PV-sturing", st ? (st.extra_pv_control_enabled ? "Actief" : "Uit") : "–", st ? tone(st.extra_pv_control_enabled) : ""),
    ];
    return `${this._header("mdi:shield-check-outline", "Integratiestatus")}<div class="card-body status-grid"><div>${left.join("")}</div><div>${right.join("")}</div></div>`;
  }

  _powerSeries(x0, x1) {
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
      // Shown as "+ = charging", the intuitive direction for a chart.
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
    return `${header}<div class="card-body">${legend}${chart}${this._rangeButtons()}</div>`;
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
    const socPts = this._series("battery_soc");

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
          { name: "SOC", color: COLOR.import, points: socPts, axis: "right", step: true, width: 2 },
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
    const strip = `
      <svg viewBox="0 0 ${W} ${H}" class="chart strip">
        <text x="${pad.l - 6}" y="12" class="axis" text-anchor="end">€ ${fmtNum(pMax, 2)}</text>
        ${pMin < 0 ? `<text x="${pad.l - 6}" y="${H - 8}" class="axis" text-anchor="end">€ ${fmtNum(pMin, 2)}</text>` : ""}
        <line x1="${pad.l}" x2="${W - pad.r}" y1="${zeroY}" y2="${zeroY}" class="zero"/>
        ${bars}
      </svg>`;

    const usedActions = Object.entries(ACTIONS).filter(([key]) => plan.some((h) => h.action === key));
    const legend = this._legend([
      ...usedActions.map(([, a]) => ({ label: a.label, color: a.color })),
      { label: "Zon (verwacht)", color: COLOR.forecast, line: true },
      { label: "Verbruik (verwacht)", color: COLOR.house, line: true },
      { label: "Zon", color: COLOR.solar, line: true },
      { label: "Verbruik", color: "#64748b", line: true },
      { label: "SOC", color: COLOR.import, line: true },
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
    const id = entities[key];
    return (id && this._history && this._history[id]) || [];
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

    const dayStart = startOfDay().getTime();
    const pvToday = integrateWh(this._historyOf(e, "pv_power"), dayStart, Date.now()) / 1000;
    const today = this._energyRows([
      { label: "Geladen", value: this._todayTotal("charged", e), color: COLOR.charge, entity: e[COUNTERS.charged] },
      { label: "Ontladen", value: this._todayTotal("discharged", e), color: COLOR.discharge, entity: e[COUNTERS.discharged] },
      { label: "Zon (dak)", value: this._history ? pvToday : null, color: COLOR.solar, entity: e.pv_power },
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
    const todayKwh = integrateWh(this._historyOf(e, "extra_pv_power"), dayStart, Date.now()) / 1000;
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

        <div class="section-title">Energie vandaag</div>
        ${this._energyRows([{ label: "Opgewekt", value: this._history ? todayKwh : null, color: COLOR.solar, entity: e.extra_pv_power }])}

        <div class="section-title">Prijssturing (curtailment)</div>
        <div class="kv-grid">
          <div class="kv"><span>Status</span><span class="${st.extra_pv_control_enabled ? "accent" : "muted"}">${
            st.extra_pv_control_enabled ? "Actief" : "Uit"
          }</span></div>
          <div class="kv"><span>Modbus hub</span><span>${esc(st.extra_pv_modbus_hub || "Niet ingesteld")}</span></div>
        </div>
      </section>`;
  }

  _renderOverrides() {
    const active = this._status && this._status.manual_override;
    const buttons = OVERRIDES.map(
      (o) => `
        <button class="action ${active === serviceToAction(o.service) ? "active" : ""}" data-service="${o.service}" data-label="${esc(o.label)}">
          <ha-icon icon="${o.icon}"></ha-icon><span>${o.label}</span>
        </button>`
    ).join("");
    return `
      ${this._header("mdi:hand-back-right-outline", "Handmatig overschrijven")}
      <div class="card-body">
        <p class="hint">Blijft actief tot je het opheft. Wordt alleen echt naar de omvormer geschreven als batterijsturing aan staat.</p>
        <div class="actions">${buttons}</div>
        <button class="action clear" data-service="clear_manual_override" data-label="">
          <ha-icon icon="mdi:restore"></ha-icon><span>Opheffen — terug naar planning</span>
        </button>
      </div>`;
  }

  _renderSettings() {
    const rows = SETTINGS.filter((key) => this._entities[key])
      .map((key) => {
        const s = this._state(key);
        const name = (s && s.attributes.friendly_name) || key;
        return `<div class="status-row" data-entity="${esc(this._entities[key])}"><span>${esc(name)}</span><span>${esc(this._formatted(key))}</span></div>`;
      })
      .join("");
    return `
      ${this._header("mdi:tune-variant", "Instellingen")}
      <div class="card-body">
        ${rows || '<div class="empty">Geen instellingen gevonden.</div>'}
        <p class="hint">Klik op een regel om de waarde aan te passen.</p>
      </div>`;
  }

  _timeTicks(x0, x1) {
    const span = x1 - x0;
    const hour = 3600 * 1000;
    const step = span <= 2 * hour ? 15 * 60 * 1000 : span <= 7 * hour ? hour : span <= 13 * hour ? 2 * hour : 6 * hour;
    const ticks = [];
    for (let t = Math.ceil(x0 / step) * step; t <= x1; t += step) ticks.push({ t, label: fmtTime(t) });
    return ticks;
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
    } else {
      const { o, pad, groupW } = meta;
      const gi = Math.floor((px - pad.l) / groupW);
      if (gi < 0 || gi >= o.groups.length) return this._hideHover();
      const band = svg.querySelector(".cursor-band");
      band.setAttribute("x", pad.l + gi * groupW);
      band.setAttribute("visibility", "visible");
      const g = o.groups[gi];
      html = `<div class="tt-title">${esc(g.label)}</div>${o.series
        .map((s, si) => `<div class="tt-row"><span class="swatch" style="background:${s.color}"></span>${esc(s.name)}<b>${fmtKwh(g.values[si])}</b></div>`)
        .join("")}`;
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
    max-width: 1500px;
    margin: 0 auto;
    display: flex;
    flex-direction: column;
    gap: 20px;
    box-sizing: border-box;
  }
  .grid { display: grid; gap: 20px; align-items: stretch; }
  .g3 { grid-template-columns: minmax(300px, 1.05fr) minmax(260px, 0.9fr) minmax(300px, 1fr); }
  .g2 { grid-template-columns: 1fr 1fr; }
  .g-status { grid-template-columns: minmax(240px, 0.45fr) 1fr; }
  @media (max-width: 1100px) {
    .g3, .g2, .g-status { grid-template-columns: 1fr; }
  }

  .card {
    background: var(--card-background-color, #fff);
    border-radius: var(--ha-card-border-radius, 16px);
    border: 1px solid var(--line);
    padding: 18px 20px 20px;
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
    font-size: 13px;
    margin-bottom: 14px;
  }
  .card-header ha-icon { --mdc-icon-size: 18px; opacity: 0.7; }
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

  /* energy flow scene */
  .scene { background: #0b0f17; border-radius: 14px; overflow: hidden; }
  .scene svg { display: block; width: 100%; height: auto; }
  .flow-value { fill: #fff; font-size: 17px; font-weight: 600; }
  .flow-name { fill: #cbd5e1; font-size: 8.5px; letter-spacing: 0.12em; font-weight: 600; }
  .flow-sub { fill: #94a3b8; font-size: 8.5px; }
  .flow-label:hover .flow-value { fill: var(--accent); }
  .flow { stroke-dasharray: 3 9; animation: flow 1.4s linear infinite; stroke-linecap: round; }
  .flow.rev { animation-direction: reverse; }
  @keyframes flow { to { stroke-dashoffset: -24; } }
  .scene-foot { color: #cbd5e1; font-size: 13px; text-align: center; padding: 0 0 14px; }
  .scene-foot b { color: var(--accent); }

  /* energy today */
  .energy-row { padding: 6px 0; }
  .energy-top { display: flex; justify-content: space-between; font-size: 15px; }
  .energy-value { font-weight: 500; font-variant-numeric: tabular-nums; }
  .energy-value small { color: var(--muted); font-weight: 400; font-size: 11px; }
  .energy-bar { height: 7px; margin-top: 6px; border-radius: 4px; }
  .energy-bar div { height: 100%; border-radius: 4px; min-width: 6px; transition: width 0.6s; }
  .sep { height: 12px; }
  .energy-row.plain { padding: 9px 0; }

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
  .status-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 0 40px; }
  @media (max-width: 700px) { .status-grid { grid-template-columns: 1fr; } }
  .status-row {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    padding: 12px 0;
    border-bottom: 1px solid var(--line);
    font-size: 14px;
  }
  .status-row span:first-child { color: var(--muted); }
  .status-row span:last-child { text-align: right; }

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
  .kv { display: flex; justify-content: space-between; gap: 12px; padding: 9px 0; font-size: 14px; border-bottom: 1px solid var(--line); }
  .kv span:first-child { color: var(--muted); }
  .kv span:last-child { text-align: right; font-variant-numeric: tabular-nums; }
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
