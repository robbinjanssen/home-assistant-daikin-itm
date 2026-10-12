# AGENTS.md

Guidance for AI coding agents working on this repository. Human contributors:
see [CONTRIBUTING](.github/CONTRIBUTING.md).

## Project

A Home Assistant custom integration (distributed through HACS) for the Daikin
intelligent Touch Manager (iTM). Domain: `daikin_itm`. Communication with the
iTM lives in the [`python-daikin-itm`](https://github.com/robbinjanssen/python-daikin-itm)
library (same owner, import name `daikin_itm`). Fix device behaviour in the
library rather than working around it here.

```text
custom_components/daikin_itm/
  __init__.py      setup, unload, removal, the iTM's own device
  api.py           creates the client from config entry data
  config_flow.py   user and reconfigure flow
  coordinator.py   polling, session token store, writes with immediate state
  climate.py, sensor.py, binary_sensor.py, diagnostics.py
  entity.py        base entity and adding entities for new units
  strings.json, translations/{en,nl}.json, icons.json
  brand/          Daikin icon and logo, from home-assistant/brands (daikin_altherma)
tests/             pytest-homeassistant-custom-component tests against a simulated iTM
```

## Commands

The project uses [uv](https://docs.astral.sh/uv/) and
[prek](https://github.com/j178/prek).

```sh
uv sync                        # create .venv with Home Assistant and dev tools
uv run prek install            # git hook running all checks on commit
uv run prek run --all-files    # Ruff, mypy, pylint, codespell, yamllint, ...
uv run pytest                  # tests; CI requires 70% coverage
```

Run all hooks and tests before committing. The hooks refuse commits to `main`;
work on a branch.

## Requirements and the library

- Python 3.14.2+ and Home Assistant 2026.10.0+ (`hacs.json`, README). Only
  raise the minimum on purpose and update both.
- `manifest.json` pins the released library version that users install.
- For development, `pyproject.toml` takes the library from its `main` branch.
  `uv.lock` pins a commit, so after merging library changes run
  `uv lock --upgrade-package python-daikin-itm`.
- Release the library before an integration release that depends on it, and
  bump the manifest to that version.

## The iTM

- **One web session at a time.** The library logs in, works and logs out for
  every call. The client gets a `HassSessionStore`, which keeps the page token
  in `.storage` so a session left behind by a crash is released on the next
  poll. To spare SD cards it writes only once a session has lasted 10 seconds,
  and from then on after every reply; a crash during a normal 1-2 second poll
  leaves the session to the iTM's own timeout. The store is per iTM host and
  the config flow uses it too.
- **Logins are refused** both for wrong credentials and while someone uses the
  web UI; the iTM shows the same dialog. The coordinator keeps the last data
  for a few polls, then fails the update, and raises a repair issue when logins
  stay refused (`LOGIN_REJECTED_ISSUE_AFTER`). There is no reauth flow, because
  a busy web UI would trigger it; credentials are changed with reconfigure.
- **Changes take 3-8 seconds** to reach a unit. Never send a write to a unit
  while the previous change is on its way; the library serializes this.
- **Setpoints of stopped units are ignored** by the iTM. The climate entity
  reports no target temperature while a unit is stopped and refuses a setpoint
  with a `ServiceValidationError`.

## Conventions

- **Typing**: mypy runs in strict mode from `pyproject.toml` against the real
  Home Assistant types. Use `probatio` for schemas (Home Assistant's validation
  library since 2026.9), not `voluptuous`.
- **Name clash**: the integration package and the library are both called
  `daikin_itm`. pylint ignores the module for that reason; keep the
  `ignored-modules` setting.
- **Entities**: `_attr_has_entity_name = True` with a `translation_key`; the
  device is named after the unit's description in the iTM. Unique IDs are the
  config entry ID plus the unit's bus address (e.g. `1:1-04`), so renaming a
  unit or changing the iTM's address keeps entities and history. Keep entity
  IDs stable for existing users. Unit devices hang under the iTM's device
  (`via_device_id`).
- **State updates**: write through `ItmCoordinator.async_write`. It shows the
  change before writing (optimistic update) and undoes it if the write fails.
  The coordinator keeps the units as last read apart from the pending changes
  and shows the changes on top, until a poll shows them or `PENDING_TIMEOUT`
  after the write. Writes use `confirm=False`; the next poll, scheduled 10
  seconds later, confirms them.
- **Errors**: raise `HomeAssistantError` / `ServiceValidationError` with a
  `translation_key` from the `exceptions` section of `strings.json`.
- **Translations**: Home Assistant loads `translations/*.json` for custom
  integrations. When changing `strings.json`, update `en` and `nl` too.
- **Reloading**: use `async_update_reload_and_abort`. Do not add a config
  entry update listener next to it; Home Assistant 2026.12 rejects that
  combination.
- **Diagnostics**: redact the host and credentials.

## Tests

- `tests/fake_itm.py` is a copy of the library's simulated iTM (one session,
  delta replies, ignored setpoints on stopped units, changes that reach a unit
  only after `latency_logins` further logins). Keep it in sync with the
  library's `tests/fake_itm.py`.
- The `itm` fixture patches `daikin_itm.ItmClient` to talk to the simulated
  iTM without delays; `init_integration` sets up a config entry against it.
- Add a test for every bug fix that fails without the fix.

## Pull requests and releases

- Every pull request needs one of these labels: `breaking-change`, `bugfix`,
  `hotfix`, `documentation`, `enhancement`, `refactor`, `performance`,
  `new-feature`, `maintenance`, `ci`, `dependencies`, `skip-changelog`.
- Release Drafter derives the next version from the labels
  (`breaking-change` → major). Bump `version` in `manifest.json` and
  `pyproject.toml` to match before a release.
- Renovate keeps dependencies up to date and automerges minor and patch
  updates.
