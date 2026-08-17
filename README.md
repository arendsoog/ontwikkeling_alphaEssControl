# AlphaESSControl

Home Assistant custom integration (HACS) to monitor and control an AlphaESS
inverter/battery system over the local network (Modbus TCP), with
price-and-solar-aware charge scheduling.

## Installation

### HACS (recommended)

This integration isn't in the default HACS store, so it needs to be added
as a custom repository first:

1. In Home Assistant, go to **HACS → Integrations**.
2. Click the **⋮** menu (top right) → **Custom repositories**.
3. Add repository `https://github.com/arendsoog/ontwikkeling_alphaEssControl`,
   category **Integration**.
4. Find **AlphaESSControl** in HACS and click **Download**.
5. Restart Home Assistant.
6. Go to **Settings → Devices & Services → Add Integration**, search for
   **AlphaESSControl**, and enter your inverter's host/port.

### Manual

Copy `custom_components/alpha_ess_local` into your Home Assistant config's
`custom_components/` folder, restart Home Assistant, then add the
integration as in step 6 above.

### Project layout

The integration talks Modbus TCP directly to the inverter for
measurements/control, and — for everything it can't get from the inverter
itself (day-ahead prices, solar forecasts) — reads it from *other* Home
Assistant entities you point it at in the options flow, rather than calling
external APIs itself. That keeps this integration's own dependencies down
to just `pymodbus` (see "Other components" below).

### Other components

`alpha_ess_local` has no hard integration dependencies (`manifest.json`'s
`dependencies` is empty) — it degrades gracefully if these aren't set up.
But for full functionality, configure it (via **Settings → Devices &
Services → AlphaESSControl → Configure**) to point at entities from:

- **Day-ahead prices** — either the [ENTSO-e](https://www.home-assistant.io/integrations/entsoe/)
  or [Frank Energie](https://github.com/HiDiHo01/home-assistant-frank_energie)
  integration (whichever you have; ENTSO-e is tried first, Frank Energie is
  the fallback).
- **Solar forecast** — [Forecast.Solar](https://www.home-assistant.io/integrations/forecast_solar/)
  and/or [Solcast](https://github.com/BJReplay/ha-solcast-solar) (both can
  be combined; Forecast.Solar is tried first).
- **House load / extra PV / peak load sensors** (optional) — any existing
  power sensor entities in your setup.
- **Home Assistant's built-in [Modbus](https://www.home-assistant.io/integrations/modbus/)
  integration** (optional) — only needed if you enable control of a second,
  separate PV inverter via Modbus (`extra_pv_control_enabled`).

### How it works

A battery inverter's built-in logic is usually simple: charge from the sun,
use the stored energy at night. That leaves money on the table if you're on
a dynamic/day-ahead electricity contract, where prices swing hour by hour
and sometimes go very high (worth pre-buying cheap grid power for) or even
negative (worth consuming instead of exporting). This integration replaces
that simple logic with an automated planner that decides, hour by hour,
whether to charge from solar, charge from the grid, hold, or deliberately
sell stored energy back — aiming to minimize your electricity bill while
protecting battery health and (for Belgian users) your monthly grid peak
fee.

**What it looks at.** Straight from the inverter (over a local, direct
Modbus connection — no cloud account needed): battery charge level, live
solar production, live battery power, and grid import/export. From other
Home Assistant integrations you configure: day-ahead electricity prices and
a solar forecast for the day ahead. It also keeps its own history of actual
measured solar output and household consumption, which it uses to
fine-tune those forecasts over time.

**How it decides.** Once a day it plans the full next 24 (or 48) hours at
once, weighing every hour's options against each other rather than
reacting hour-by-hour. On principle it only allows itself *one* deliberate
grid-charge and *one* deliberate sell-off per day — reserved for the single
cheapest and single priciest hour — so it doesn't act on every cheap or
expensive moment, only the best one. It also leaves room in the battery for
solar production still expected later that day, so an early grid-charge
doesn't crowd out free sunshine. Because forecasts are never perfect, it
doesn't chase the average-case plan: it checks the plan against several
more pessimistic "what if the sun/load forecast is off" scenarios, and only
commits to it if it still clears a minimum profit margin (which you set)
even in a bad-case scenario — otherwise it just falls back to the safe
default. Live commands to the inverter are refreshed roughly every 20
seconds, and on negative-price hours it never exports to the grid — it will
even curtail a second, separately controlled PV array once the battery is
full rather than sell at a loss.

**What you get.** Sensors for live power flows, state of charge, today's
totals, current/forecast prices and solar, and the plan for the current and
next hour. Sliders and switches on the device page let you tune the
charge/discharge limits and minimum profit threshold without editing the
config. Services let you manually override the mode (force a grid charge,
force a discharge, force no charging) when you want to. A master "control
enabled" switch (off by default) gates whether it's actually allowed to
command the inverter — until you flip it on, it only computes and logs what
it *would* do, so you can watch it before trusting it.

**What you configure.** Battery capacity and inverter power, your
electricity use/return fees and VAT, the minimum daily profit you require
before acting, safe state-of-charge limits for charging/discharging, an
optional monthly peak-load cap, and — if your provider can remotely control
your battery — the hours during which you allow that to override this
integration's plan.