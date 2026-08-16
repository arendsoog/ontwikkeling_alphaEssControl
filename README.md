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

## Releasing a new version

HACS shows whatever GitHub Release is newest, so bumping the version means:

1. Update `"version"` in
   [manifest.json](custom_components/alpha_ess_local/manifest.json)
   (follow [semver](https://semver.org)) and commit it.
2. Tag the commit and push the tag:
   ```bash
   git tag v0.1.0
   git push origin v0.1.0
   ```
3. [.github/workflows/release.yaml](.github/workflows/release.yaml) then
   checks that the tag matches `manifest.json`'s version and publishes a
   GitHub Release automatically (with auto-generated release notes). HACS
   picks up the new release from there — no manual step in HACS itself.

## Publishing status

- [x] `manifest.json` / `hacs.json` point at the real repo
      (`arendsoog/ontwikkeling_alphaEssControl`)
- [x] Real Modbus TCP protocol implemented in
      [api.py](custom_components/alpha_ess_local/api.py)
- [ ] Repo is public on GitHub so HACS can add it as a custom repository
- [ ] Submit to the default HACS integration list (optional, once stable)
