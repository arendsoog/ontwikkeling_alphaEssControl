<p align="center">
  <img src="https://raw.githubusercontent.com/arendsoog/ontwikkeling_alphaEssControl/master/custom_components/alpha_ess_local/brand/logo@2x.png" alt="AlphaESS" height="96">
</p>

# AlphaESSControl

A Home Assistant integration that runs an **AlphaESS home battery** for you on a **dynamic (day-ahead) electricity contract**. It talks to the inverter directly over your local network (Modbus TCP, no cloud account), plans the battery hour by hour from the electricity prices and the solar forecast, and sends the inverter the commands to carry out that plan.

It decides when to charge from the sun, when to buy cheap power from the grid, when to hold, and when to sell. The goal is the lowest electricity bill without wearing out the battery. It also comes with its own dashboard in the sidebar.

> **Safe by default.** Battery control is **off** after installing. Until you turn it on, the integration only calculates and shows what it *would* do, so you can follow its plan for a few days before you let it act.

## Contents

- [What it does](#what-it-does)
- [Requirements](#requirements)
- [Installation](#installation)
- [Setup](#setup)
- [The dashboard](#the-dashboard)
- [How the planning works](#how-the-planning-works)
- [Entities and services](#entities-and-services)
- [Recipes](#recipes)
- [Troubleshooting](#troubleshooting)
- [Development](#development)

## What it does

- **Plans the battery by price and sun.** It plans every hour from now to the end of tomorrow, as soon as tomorrow's prices are published. It charges from the grid in the cheapest hours only when the sun won't fill the battery, keeps room for the solar still to come, and sells or uses the stored energy in the most expensive hours.
- **Robust plans.** A plan is only used when it still pays off, by a minimum profit you set, if the sun or your consumption turn out worse than forecast. Otherwise the battery runs in its safe default mode.
- **Negative prices.** It never exports at a negative price. It can switch off a second solar installation (for example an SMA inverter) once the battery is full.
- **Belgian capacity tariff.** Grid charging stays under the month's peak, or under the limit you set.
- **Learns your home.** It keeps its own history of measured solar production and house consumption. It uses that to correct the solar forecast and to predict your consumption per hour of the day.
- **Works with your own integrations.** Prices, solar forecast, smart meter, EV chargers and a second solar installation all come from entities you pick. Any integration that provides them works.
- **Dashboard.** Live energy flow, today's plan and decisions, every inverter and charger, the controls, and history.

## Requirements

- **An AlphaESS hybrid inverter with Modbus TCP** on your network: default port `502`, unit/slave ID `85`. Developed and tested on a SMILE-series system with three PV inputs. Other AlphaESS models that use the same register map should work, but haven't been tested.
- **Home Assistant 2025.3 or newer.** The integration's own icon in Home Assistant needs a recent Home Assistant (2026.x).
- **A dynamic electricity contract** with day-ahead prices in Home Assistant, from one of these integrations (one is enough; more can serve as backup):
  - [ENTSO-e](https://github.com/JaccoR/hass-entso-e)
  - [Frank Energie](https://github.com/HiDiHo01/home-assistant-frank_energie)
  - [Nord Pool (HACS)](https://github.com/custom-components/nordpool). HA's built-in Nord Pool integration doesn't put the day's prices in its sensors' attributes, so it can't be read.
- **A solar forecast** (recommended):
  - [Forecast.Solar](https://www.home-assistant.io/integrations/forecast_solar/)
  - [Solcast](https://github.com/BJReplay/ha-solcast-solar)
  - [Open-Meteo Solar Forecast](https://github.com/rany2/ha-open-meteo-solar-forecast)
- **Optional:**
  - a smart (P1) meter integration (DSMR, HomeWizard, P1 Monitor, DSMR Reader, ESPHome, ...);
  - your EV charger's sensors;
  - a second solar installation's power sensors and a switch to turn it off.

Only one client can usually talk Modbus to the inverter at a time. If something else also polls it (an older Modbus YAML setup, another integration), put a Modbus proxy in between or turn the other client off.

## Installation

### HACS (recommended)

This integration isn't in the default HACS store yet, so add it as a custom repository:

1. In Home Assistant, open **HACS**, click the **⋮** menu (top right) and choose **Custom repositories**.
2. Add `https://github.com/arendsoog/ontwikkeling_alphaEssControl`, type **Integration**.
3. Search for **AlphaESSControl** in HACS and click **Download**.
4. Restart Home Assistant.

### Manual

Copy the `custom_components/alpha_ess_local` folder into the `custom_components/` folder of your Home Assistant configuration and restart Home Assistant.

## Setup

1. Go to **Settings → Devices & services → Add integration** and pick **AlphaESSControl**.
2. Enter the inverter's IP address and port (`502`). The integration checks the connection before it saves.
3. Open the integration's **Configure** menu and go through the sections below. Each section is saved on its own, and every field has a short explanation in the form.

### Installation

The physical specs of your system: roof panels (Wp per panel, number of panels), usable battery capacity (Wh) and the inverter's nominal power (W). The planner needs these to know how much the battery holds and how fast it charges.

### Prices

- **Price sources.** Pick a sensor with day-ahead prices. Add more with **Add entity** as backups and drag them into the order you want: the first one that has prices for a day is used.
- **Your contract's costs:**
  - the supplier's fees for use and for feed-in (EUR/kWh);
  - VAT, and whether VAT also applies to the feed-in price;
  - the network costs per kWh for the normal and the low (night) tariff.

  Enter the raw market price as the source (without VAT and fees): the integration adds these itself.

### Solar forecast

Pick one sensor of each forecast location, such as Forecast.Solar's "Estimated energy production today". The integration uses the location that sensor belongs to.

- **Several roof planes.** Locations of the same service are added up, for example one Forecast.Solar location for the front and one for the back of the roof.
- **Backup service.** The service on top of the list is used. A second service (say Solcast) is only used when the first has no forecast.

The form shows how it reads your list, for example "1. Forecast.Solar: Front + Back (summed)".

### P1 meter

| Field | What to pick |
|---|---|
| Grid import power | The power drawn from the grid right now (DSMR "Power consumption"). If your meter has one sensor that is positive when importing and negative when exporting (HomeWizard "Power"), pick that and leave export empty. |
| Grid export power | The power fed into the grid right now (DSMR "Power production"). |
| Active tariff | The current day/night tariff, used for the network cost. Words (normal/low, day/night) and the meter's tariff number are understood. The number means the opposite in Belgium and the Netherlands; Home Assistant's country setting decides which. |
| Peak load this month | Belgian meters: the month's highest 15-minute peak. Grid charging may go up to this peak, since it's already paid for. |

A DSMR or HomeWizard meter is filled in automatically. Without a meter, the inverter's own grid reading is used.

### PV installations

A solar installation next to the AlphaESS, with its own inverter. These aren't in the options menu: on the integration's page, click **Add PV installation**, give it a name, and fill in:

- **Measurement**
  - **Power:** the inverter's power sensor, such as its AC power.
  - **Total produced:** its total-production counter in kWh, for "Produced today" on the dashboard. Optional: without it, the dashboard works it out from the power.
  - **Power per MPPT (string):** for the dashboard only. An SMA integration is detected and filled in automatically.
- **Installation:** Wp per panel and number of panels, used to estimate its power when its reading is gone (an SMA asleep at night or out of reach).
- **Control at negative prices:** a **switch** that turns the source off and on (on = producing). Any switch works: a Modbus switch, a relay, an `input_boolean`, ... See [Switching off an SMA at negative prices](#switching-off-an-sma-at-negative-prices).

Add one for each installation; their power is added up. To change one later, use the ⋮ menu next to it on the integration's page.

Its power counts in the energy flow, the house consumption and the learning of the solar forecast.

### Consumption sources

Big, irregular consumers would spoil the learned consumption per hour, so their power is subtracted from the house consumption first.

- **EV chargers:** power (summed in the dashboard's energy flow), total energy counter (kWh today, per charger) and power per phase (on the charger's card). Any number of chargers; sensors are grouped per device into one card per charger.
- **Other consumers:** power only, for example a heat pump. Used for the learning only.

Nothing here is used to control anything.

### Control

- **Enable battery control:** the master switch. Off: the integration only plans and logs. On: it sends commands to the inverter.
- **At most one full battery a day:**
  - On: the battery takes in at most one full battery per day (sun and grid together), and gives at most one full battery per day (house and grid together).
  - Off: there's no daily limit, and scheduled discharging is only done for your own use.
- **Hours the provider may control the battery:** if your energy provider can control your battery remotely, the hours in which that overrides the plan.

## The dashboard

The integration adds **AlphaESS Control** to the sidebar, with four tabs.

- **Overview**
  - the system status and the live energy flow of sun, battery, house, grid and EV charger;
  - today's and this week's energy, and today's power and state of charge;
  - the day's prices, solar and plan, with a decision log of what was planned per hour and why;
  - the integration status: which price/solar source is in use, which grid meter, and which grid-charge limit applies.
- **Installations:** one card per AlphaESS (power, MPPTs, battery health, lifetime totals), a card for the PV installations with their switches, and one card per EV charger.
- **Control:** the switches and sliders below, and buttons to override the plan for the current hour.
- **History:** energy and savings per day, week, month or year.

## How the planning works

A battery's built-in logic is simple: charge from the sun, use it at night. On a dynamic contract that leaves money on the table. Prices change every hour, and are sometimes very high (worth buying cheap grid power for beforehand) or negative (worth using instead of exporting).

**What it looks at.**
- From the inverter, every 30 seconds over Modbus: state of charge, solar power, battery power and grid power.
- From your other integrations: the day-ahead prices and the solar forecast.
- From its own history (a local database): the measured solar production per hour, used to correct the forecast, and your consumption per hour of the day.

**How it decides.**
- **The whole period at once.** It weighs the options for the whole period ahead at once, not hour by hour: charge from the sun, charge from the grid (in steps), hold, use the battery for the house, or sell. It replans whenever new prices, a new forecast or a new hour comes in.
- **Room for later sun.** When the sun can still fill the battery later in the day, it doesn't charge from the grid first.
- **Grid charging.** It only buys grid power for the most expensive hours ahead, and only when that's cheaper than buying in those hours.
- **Pessimistic check.** Forecasts are never perfect. Before it uses a plan, it checks the plan against more pessimistic scenarios (less sun, more consumption). It only commits when the plan still makes the minimum daily profit you set; otherwise the battery just runs in its default mode.
- **Live control.** Every 20 seconds (10 while charging from the grid) it sets the inverter's mode. It regulates grid charging on the smart meter's reading, so it stays under the grid limit, and at the target level it lets the sun finish the charge.
- **Negative prices.** It never exports at a negative price. Once the battery is full it can switch off the PV installations rather than export at a loss.

**When something is missing.**
- **No prices:** if the price sources give nothing (for example right after a restart), it keeps the last known prices, or plans with yesterday's prices meanwhile. It replans as soon as the real prices arrive.
- **No inverter:** if the inverter doesn't answer, it stops sampling and plans with what it last knew.
- **No smart meter:** if the meter stops reporting, grid charging is regulated on the inverter's own grid reading.

## Entities and services

All entities belong to the AlphaESS device.

**Sensors**
- **Power:** solar (roof, per MPPT, additional source, total), battery and grid.
- **Energy:** today's house consumption, solar energy, solar and grid energy into the battery; lifetime totals.
- **Prices:** this hour's price, today's lowest and highest price, and whether this hour earns on use or on feed-in.
- **Solar forecast:** this hour, today and tomorrow, and the solar surplus still expected today.
- **Plan:** this hour's and next hour's action, today's full plan (as attributes), the inverter's current mode, and yesterday's savings.
- **Battery health:** voltage, current, lowest/highest cell voltage and temperature, cell voltage spread, state of health, capacity, modules, estimated cycles, inverter temperature.

**Settings you can change live**

| Entity | What it does |
|---|---|
| Max SOC on positive-price hours | The highest charge level the plan uses in normal hours. |
| Max SOC on negative-price hours | The highest charge level the plan uses in negative-price hours. |
| Min SOC on battery discharge | The level a planned discharge to the grid stops at. |
| Minimum daily profit for optimized schedule | The profit (EUR) a plan must make in the pessimistic case before it's used. |
| Max grid-charge load | The grid power (kW) grid charging may use. A higher month peak from the smart meter raises it. |
| Allow scheduled discharge to grid | Whether the plan may sell stored energy to the grid. Using the battery for your own consumption isn't affected. |

**Services**

The overrides apply to the current hour and stay until you clear them.

| Service | What it does |
|---|---|
| `alpha_ess_local.force_charge_grid` | Charge from the grid. |
| `alpha_ess_local.force_charge_pv` | Charge from the sun only. |
| `alpha_ess_local.force_discharge` | Discharge the battery. |
| `alpha_ess_local.force_no_charging` | Don't charge. |
| `alpha_ess_local.clear_manual_override` | Back to the plan. |

## Recipes

### Switching off an SMA at negative prices

Home Assistant's [Modbus](https://www.home-assistant.io/integrations/modbus/) integration can make a switch that writes the SMA's power limit. Add it under your SMA hub in `modbus.yaml`, restart Home Assistant, and pick `switch.sma_production` in the PV installation, under **Control at negative prices**:

```yaml
- name: sma_tripower
  type: tcp
  host: 192.168.1.50
  port: 502
  switches:
    - name: SMA Production
      slave: 3
      address: 40016
      write_type: holding
      command_on: 100   # % of its power: producing
      command_off: 0    # switched off
```

Check your SMA model's Modbus register list before you rely on it. Then test the switch once in daylight and watch the SMA's power drop to 0. The control itself is turned on with **PV control at negative prices** on the dashboard's **Controls** tab.

### SMA readings at night

An SMA that's asleep reports "no value" as the lowest 32-bit number (−2147483648). The integration and the dashboard read that as 0 W. For your own Modbus sensors, add `min_value: 0` so Home Assistant shows 0 W too.

## Troubleshooting

- **No prices.** Check that your price sensor has the day's prices in its attributes: `prices_today`/`prices_tomorrow` (ENTSO-e), `prices` (Frank Energie) or `raw_today`/`raw_tomorrow` (Nord Pool, HACS). Only sensors with those attributes are offered in the price list. Tomorrow's prices are usually published around noon or 1 PM.
- **The inverter doesn't answer.** Check that Modbus TCP is enabled on the inverter and that no other client is connected to it at the same time. While it doesn't answer, the log says "inverter not answering".
- **It doesn't control the battery.** Check that **Enable battery control** is on (Configure → Control) and that no manual override is active. The decision log on the dashboard shows what was planned for every hour and why.
- **Debug logging.** Add this to `configuration.yaml`:

  ```yaml
  logger:
    logs:
      custom_components.alpha_ess_local: debug
  ```

## Development

The repository has a devcontainer: open it in VS Code, then run `scripts/develop` to start Home Assistant with the integration on port 8123.

- **Tests:** `python -m pytest`.
- **Linting:** `scripts/lint` (ruff).
- **Behind a TLS-intercepting antivirus:** put its root certificate in `.devcontainer/local-ca/`; `scripts/trust-local-ca` adds it to the container.

Issues and suggestions: [GitHub issues](https://github.com/arendsoog/ontwikkeling_alphaEssControl/issues).

## License

MIT, see [LICENSE](LICENSE). AlphaESS and the AlphaESS logo are trademarks of their owner; this project isn't affiliated with or endorsed by AlphaESS.
