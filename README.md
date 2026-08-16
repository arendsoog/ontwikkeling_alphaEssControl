# AlphaESSControl

Home Assistant custom integration (HACS) to monitor and control an AlphaESS
inverter/battery system over the local network.

> **Status:** scaffold only. `custom_components/alpha_ess_local/api.py`
> contains a placeholder client — the real Modbus/HTTP protocol calls still
> need to be implemented against actual hardware.

## Development environment

This repo is set up to be opened in VS Code's **Dev Containers** extension
(Docker required). Open the folder and choose "Reopen in Container" — this
runs `scripts/setup`, which installs Home Assistant core plus test/lint
dependencies and creates a scratch config in `config/`.

Once inside the container:

| Script | Purpose |
| --- | --- |
| `scripts/setup` | Install dependencies, bootstrap `config/` (runs automatically on container create) |
| `scripts/develop` | Symlink the integration into `config/custom_components/` and launch Home Assistant at `http://localhost:8123` with debug logging |
| `scripts/lint` | Run `ruff check` + `ruff format --check` |

Run the test suite with:

```bash
pytest --cov=custom_components.alpha_ess_local tests
```

### Without Docker

If you don't want to use the devcontainer, create a local venv instead:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements-test.txt
```

Then run `pytest` directly, or symlink `custom_components/alpha_ess_local`
into an existing local Home Assistant config's `custom_components/` folder.

## Project layout

```
custom_components/alpha_ess_local/
    __init__.py        # setup/unload entry points
    api.py              # device client (placeholder — TODO real protocol)
    config_flow.py       # UI config flow (host/port)
    const.py             # domain, defaults
    coordinator.py        # DataUpdateCoordinator, polls every 30s
    entity.py             # shared entity base (device info)
    sensor.py              # example sensors (battery SOC, PV/grid power)
    manifest.json
    strings.json / translations/{en,nl}.json
tests/
```

## Before publishing to HACS

- [ ] Replace `@your-github-username` / repo URLs in
      [manifest.json](custom_components/alpha_ess_local/manifest.json) and
      [hacs.json](hacs.json)
- [ ] Implement the real device protocol in
      [api.py](custom_components/alpha_ess_local/api.py)
- [ ] Push to a public GitHub repo, then add it in HACS as a
      custom repository (category: Integration)
