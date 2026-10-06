// AlphaESSControl sidebar panel.
//
// Plain web component, no build step: Home Assistant hands it `hass`,
// `narrow` and `panel` as properties. Entities are looked up by their
// translation_key (stable across entity_id renames) among this integration's
// own registry entries, so nothing here hardcodes an entity_id.

const DOMAIN = "alpha_ess_local";

// Labels the scheduler emits (schedule.py's _CHARGE_MESSAGES) -> display.
const ACTIONS = {
  "Charge-grid": { label: "Laden van net", color: "#e8590c" },
  "Charge-PV": { label: "Laden op zon", color: "#f2b705" },
  Discharge: { label: "Ontladen", color: "#2f9e44" },
  "No-charging": { label: "Niet laden", color: "#748ffc" },
  "No-discharging": { label: "Niet ontladen", color: "#868e96" },
};

const OVERRIDES = [
  { service: "force_charge_grid", label: "Laden van net", icon: "mdi:transmission-tower-import" },
  { service: "force_charge_pv", label: "Laden op zon", icon: "mdi:solar-power" },
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

const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]
  );

const num = (value) => {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
};

const fmtPower = (watts) => {
  if (watts === null) return "–";
  return Math.abs(watts) >= 1000 ? `${(watts / 1000).toFixed(2)} kW` : `${Math.round(watts)} W`;
};

const fmtPrice = (eur) => (eur === null ? "–" : `€ ${eur.toFixed(3)}`);

class AlphaEssPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._lastStates = null;
    this._showTable = false;
  }

  set hass(hass) {
    this._hass = hass;
    this._update();
  }

  set narrow(narrow) {
    this._narrow = narrow;
    const button = this.shadowRoot.querySelector("ha-menu-button");
    if (button) button.narrow = narrow;
  }

  set panel(panel) {
    this._panel = panel;
  }

  // translation_key -> entity_id, for this integration's entities only.
  _entityMap() {
    const map = {};
    for (const entry of Object.values(this._hass.entities || {})) {
      if (entry.platform === DOMAIN && entry.translation_key) {
        map[entry.translation_key] = entry.entity_id;
      }
    }
    return map;
  }

  // HA pushes a new `hass` on every state change anywhere; only re-render
  // when one of *our* entities' state objects actually changed.
  _update() {
    if (!this._hass) return;
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
    this._render();
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

  _render() {
    const root = this.shadowRoot;
    root.innerHTML = `
      <style>${STYLE}</style>
      <div class="toolbar">
        <ha-menu-button></ha-menu-button>
        <div class="title">AlphaESS</div>
      </div>
      <div class="content">
        ${this._renderStatus()}
        ${this._renderLive()}
        ${this._renderPlan()}
        <div class="columns">
          ${this._renderToday()}
          ${this._renderOverrides()}
          ${this._renderSettings()}
        </div>
      </div>
    `;

    const menuButton = root.querySelector("ha-menu-button");
    menuButton.hass = this._hass;
    menuButton.narrow = this._narrow;

    root.querySelectorAll("[data-entity]").forEach((el) =>
      el.addEventListener("click", () => this._moreInfo(el.dataset.entity))
    );
    root.querySelectorAll("[data-service]").forEach((el) =>
      el.addEventListener("click", () => this._callService(el.dataset.service, el.dataset.label))
    );
    const toggle = root.querySelector("#toggle-table");
    if (toggle) {
      toggle.addEventListener("click", () => {
        this._showTable = !this._showTable;
        this._render();
      });
    }
  }

  _renderStatus() {
    const action = this._text("schedule_current_hour_action");
    const next = this._text("schedule_next_hour_action");
    const mode = this._text("dispatch_current_mode");
    const chip = (title, value, key) => {
      const info = ACTIONS[value];
      const color = info ? info.color : "var(--secondary-text-color)";
      return `
        <div class="status-item" data-entity="${esc(this._entities[key] || "")}">
          <div class="status-title">${title}</div>
          <div class="status-value"><span class="dot" style="background:${color}"></span>${esc(
            info ? info.label : value ?? "–"
          )}</div>
        </div>`;
    };
    return `
      <div class="status">
        ${chip("Planning nu", action, "schedule_current_hour_action")}
        ${chip("Volgend uur", next, "schedule_next_hour_action")}
        <div class="status-item wide" data-entity="${esc(this._entities.dispatch_current_mode || "")}">
          <div class="status-title">Dispatch-modus</div>
          <div class="status-value">${esc(mode ?? "–")}</div>
        </div>
      </div>`;
  }

  _renderLive() {
    const soc = this._value("battery_soc");
    const pv = this._value("total_pv_power") ?? this._value("pv_power");
    const grid = this._value("grid_power");
    const battery = this._value("battery_power");
    // Same balance orchestrator.py uses for real_house_load.
    const house = pv !== null && grid !== null && battery !== null ? pv + grid + battery : null;

    const gridLabel = grid === null ? "" : grid < 0 ? "teruglevering" : "afname";
    const batteryLabel =
      battery === null ? "" : battery < 0 ? "laden" : battery > 0 ? "ontladen" : "rust";

    const tile = (icon, title, value, sub, key, extra = "") => `
      <ha-card class="tile" data-entity="${esc(this._entities[key] || "")}">
        <ha-icon icon="${icon}"></ha-icon>
        <div class="tile-title">${title}</div>
        <div class="tile-value">${value}</div>
        <div class="tile-sub">${sub}</div>
        ${extra}
      </ha-card>`;

    const socBar =
      soc === null
        ? ""
        : `<div class="bar"><div class="bar-fill" style="width:${Math.max(
            0,
            Math.min(100, soc)
          )}%"></div></div>`;

    return `
      <div class="tiles">
        ${tile("mdi:home-battery", "Batterij", soc === null ? "–" : `${Math.round(soc)} %`, "laadniveau", "battery_soc", socBar)}
        ${tile("mdi:solar-power-variant", "Zon", fmtPower(pv), "PV totaal", this._entities.total_pv_power ? "total_pv_power" : "pv_power")}
        ${tile("mdi:home-lightning-bolt", "Huis", fmtPower(house), "verbruik (berekend)", "house_load_today")}
        ${tile("mdi:transmission-tower", "Net", fmtPower(grid === null ? null : Math.abs(grid)), gridLabel, "grid_power")}
        ${tile("mdi:battery-charging", "Batterijvermogen", fmtPower(battery === null ? null : Math.abs(battery)), batteryLabel, "battery_power")}
      </div>`;
  }

  _renderPlan() {
    const planState = this._state("schedule_today_plan");
    const hours = (planState && planState.attributes.hours) || [];
    if (!hours.length) {
      return `<ha-card header="Planning vandaag"><div class="empty">Nog geen planning beschikbaar (prijzen of voorspelling ontbreken nog).</div></ha-card>`;
    }

    const nowHour = new Date().getHours();
    const prices = hours.map((h) => num(h.price) ?? 0);
    const max = Math.max(0, ...prices);
    const min = Math.min(0, ...prices);
    const range = max - min || 1;
    const zeroPct = (max / range) * 100;

    const bars = hours
      .map((h) => {
        const price = num(h.price) ?? 0;
        const info = ACTIONS[h.action] || { label: h.action, color: "var(--secondary-text-color)" };
        const height = (Math.abs(price) / range) * 100;
        const top = price >= 0 ? zeroPct - height : zeroPct;
        const tip = `${String(h.hour).padStart(2, "0")}:00 — ${info.label}\nMarktprijs: ${fmtPrice(
          price
        )}/kWh\nZon: ${fmtPower(num(h.solar_forecast_w))}\nHuis: ${fmtPower(num(h.house_load_w))}`;
        return `
          <div class="col ${h.hour === nowHour ? "now" : ""} ${h.hour < nowHour ? "past" : ""}" title="${esc(tip)}">
            <div class="plot">
              <div class="price-bar" style="top:${top}%;height:${Math.max(height, 0.5)}%;background:${info.color}"></div>
            </div>
            <div class="hour">${h.hour}</div>
          </div>`;
      })
      .join("");

    const legend = Object.values(ACTIONS)
      .filter((info) => hours.some((h) => ACTIONS[h.action] === info))
      .map((info) => `<span class="legend-item"><span class="dot" style="background:${info.color}"></span>${info.label}</span>`)
      .join("");

    const table = this._showTable
      ? `
        <table>
          <thead><tr><th>Uur</th><th>Actie</th><th>Marktprijs</th><th>Zon</th><th>Huis</th></tr></thead>
          <tbody>
            ${hours
              .map((h) => {
                const info = ACTIONS[h.action] || { label: h.action, color: "transparent" };
                return `<tr class="${h.hour === nowHour ? "now" : ""}">
                  <td>${String(h.hour).padStart(2, "0")}:00</td>
                  <td><span class="dot" style="background:${info.color}"></span>${esc(info.label)}</td>
                  <td>${fmtPrice(num(h.price))}</td>
                  <td>${fmtPower(num(h.solar_forecast_w))}</td>
                  <td>${fmtPower(num(h.house_load_w))}</td>
                </tr>`;
              })
              .join("")}
          </tbody>
        </table>`
      : "";

    return `
      <ha-card header="Planning vandaag">
        <div class="card-content">
          <div class="chart" style="--zero:${zeroPct}%">${bars}</div>
          <div class="legend">${legend}</div>
          <button class="link" id="toggle-table">${this._showTable ? "Verberg tabel" : "Toon tabel"}</button>
          ${table}
        </div>
      </ha-card>`;
  }

  _renderToday() {
    const row = (label, key) =>
      this._entities[key]
        ? `<div class="row" data-entity="${esc(this._entities[key])}"><span>${label}</span><span>${esc(
            this._formatted(key)
          )}</span></div>`
        : "";
    const savings = this._state("yesterday_savings");
    const savingsSplit =
      savings && savings.attributes.savings_solar_eur_total !== undefined
        ? `<div class="row sub"><span>waarvan zon / batterij</span><span>€ ${Number(
            savings.attributes.savings_solar_eur_total
          ).toFixed(2)} / € ${Number(savings.attributes.savings_battery_eur_total).toFixed(2)}</span></div>`
        : "";
    return `
      <ha-card header="Vandaag">
        <div class="card-content">
          ${row("Prijs nu (all-in)", "price_current_hour")}
          ${row("Laagste marktprijs", "price_lowest_today")}
          ${row("Hoogste marktprijs", "price_highest_today")}
          ${row("Zonneprognose vandaag", "solar_forecast_today")}
          ${row("Zonneprognose morgen", "solar_forecast_tomorrow")}
          ${row("Zonne-energie vandaag", "solar_energy_today")}
          ${row("Huisverbruik vandaag", "house_load_today")}
          ${row("Zon → batterij", "solar_to_battery_today")}
          ${row("Net → batterij", "grid_to_battery_today")}
          ${row("Besparing gisteren", "yesterday_savings")}
          ${savingsSplit}
        </div>
      </ha-card>`;
  }

  _renderOverrides() {
    const buttons = OVERRIDES.map(
      (o) => `
        <button class="action" data-service="${o.service}" data-label="${esc(o.label)}">
          <ha-icon icon="${o.icon}"></ha-icon><span>${o.label}</span>
        </button>`
    ).join("");
    return `
      <ha-card header="Handmatig overschrijven">
        <div class="card-content">
          <p class="hint">Blijft actief tot je het opheft. Alleen echt geschreven naar de omvormer als batterijsturing aan staat.</p>
          <div class="actions">${buttons}</div>
          <button class="action clear" data-service="clear_manual_override" data-label="">
            <ha-icon icon="mdi:restore"></ha-icon><span>Opheffen — terug naar planning</span>
          </button>
        </div>
      </ha-card>`;
  }

  _renderSettings() {
    const rows = SETTINGS.filter((key) => this._entities[key])
      .map((key) => {
        const s = this._state(key);
        const name = (s && s.attributes.friendly_name) || key;
        return `<div class="row" data-entity="${esc(this._entities[key])}"><span>${esc(
          name
        )}</span><span>${esc(this._formatted(key))}</span></div>`;
      })
      .join("");
    return `
      <ha-card header="Instellingen">
        <div class="card-content">
          ${rows || '<div class="empty">Geen instellingen gevonden.</div>'}
          <p class="hint">Klik op een regel om de waarde aan te passen.</p>
        </div>
      </ha-card>`;
  }

  _moreInfo(entityId) {
    if (!entityId) return;
    this.dispatchEvent(
      new CustomEvent("hass-more-info", { detail: { entityId }, bubbles: true, composed: true })
    );
  }

  async _callService(service, label) {
    if (label && !window.confirm(`Planning overschrijven: "${label}"?`)) return;
    try {
      await this._hass.callService(DOMAIN, service);
    } catch (err) {
      window.alert(`Mislukt: ${err.message || err}`);
    }
  }
}

const STYLE = `
  :host {
    display: block;
    min-height: 100vh;
    background: var(--primary-background-color);
    color: var(--primary-text-color);
    font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif);
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
  .title { margin-left: 12px; }
  .content {
    padding: 16px;
    max-width: 1200px;
    margin: 0 auto;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  ha-card { display: block; }
  .card-content { padding: 0 16px 16px; }
  [data-entity] { cursor: pointer; }

  .status {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px;
  }
  .status-item {
    background: var(--card-background-color);
    border-radius: var(--ha-card-border-radius, 12px);
    border: 1px solid var(--divider-color);
    padding: 12px 16px;
  }
  .status-title { font-size: 12px; color: var(--secondary-text-color); }
  .status-value { font-size: 18px; margin-top: 4px; display: flex; align-items: center; }
  .dot {
    display: inline-block;
    width: 10px;
    height: 10px;
    border-radius: 50%;
    margin-right: 8px;
    flex: none;
  }

  .tiles {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 12px;
  }
  .tile { padding: 16px; }
  .tile ha-icon { color: var(--state-icon-color, var(--primary-color)); }
  .tile-title { font-size: 13px; color: var(--secondary-text-color); margin-top: 6px; }
  .tile-value { font-size: 26px; font-weight: 500; margin-top: 2px; }
  .tile-sub { font-size: 12px; color: var(--secondary-text-color); min-height: 16px; }
  .bar {
    height: 6px;
    border-radius: 3px;
    background: var(--divider-color);
    margin-top: 8px;
    overflow: hidden;
  }
  .bar-fill { height: 100%; background: var(--success-color, #43a047); }

  .chart {
    display: flex;
    gap: 3px;
    height: 180px;
    padding-top: 8px;
  }
  .col { flex: 1; display: flex; flex-direction: column; min-width: 0; }
  .col.past { opacity: 0.45; }
  .plot {
    position: relative;
    flex: 1;
    border-bottom: 1px solid transparent;
    background: linear-gradient(var(--divider-color), var(--divider-color)) no-repeat 0 var(--zero) / 100% 1px;
  }
  .price-bar { position: absolute; left: 0; right: 0; border-radius: 2px; }
  .col.now .plot { outline: 2px solid var(--primary-color); outline-offset: 1px; border-radius: 3px; }
  .hour { text-align: center; font-size: 10px; color: var(--secondary-text-color); margin-top: 4px; }
  .col.now .hour { color: var(--primary-color); font-weight: 600; }
  .legend { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 12px; font-size: 13px; }
  .legend-item { display: flex; align-items: center; }

  table { width: 100%; border-collapse: collapse; margin-top: 8px; font-size: 14px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--divider-color); }
  th { color: var(--secondary-text-color); font-weight: 500; }
  td:nth-child(n+3), th:nth-child(n+3) { text-align: right; }
  tr.now td { background: rgba(var(--rgb-primary-color, 3, 169, 244), 0.12); }

  .columns {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
    gap: 16px;
    align-items: start;
  }
  .row {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    padding: 8px 0;
    border-bottom: 1px solid var(--divider-color);
    font-size: 14px;
  }
  .row:last-child { border-bottom: none; }
  .row span:last-child { text-align: right; font-weight: 500; }
  .row.sub { font-size: 12px; color: var(--secondary-text-color); padding-top: 0; }
  .hint { font-size: 12px; color: var(--secondary-text-color); margin: 0 0 12px; }
  .empty { padding: 0 16px 16px; color: var(--secondary-text-color); }

  .actions { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  button.action {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 12px;
    border-radius: 8px;
    border: 1px solid var(--divider-color);
    background: var(--secondary-background-color);
    color: var(--primary-text-color);
    font: inherit;
    font-size: 14px;
    cursor: pointer;
    text-align: left;
  }
  button.action:hover { border-color: var(--primary-color); }
  button.action.clear { width: 100%; margin-top: 8px; justify-content: center; }
  button.link {
    background: none;
    border: none;
    color: var(--primary-color);
    font: inherit;
    font-size: 14px;
    padding: 12px 0 0;
    cursor: pointer;
  }

  @media (max-width: 600px) {
    .content { padding: 12px; }
    .chart { height: 140px; gap: 1px; }
    .hour { font-size: 8px; }
    .actions { grid-template-columns: 1fr; }
  }
`;

customElements.define("alpha-ess-panel", AlphaEssPanel);
