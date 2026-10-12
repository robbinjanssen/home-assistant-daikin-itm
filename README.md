<!-- PROJECT SHIELDS -->
[![hacs_badge][hacs-shield]][hacs-url]
![Project Stage][project-stage-shield]
![Project Maintenance][maintenance-shield]

# Daikin intelligent Touch Manager Integration for Home Assistant

Control the indoor units connected to a Daikin **intelligent Touch Manager (iTM)** from Home Assistant,
locally and without a cloud service. The integration talks to the iTM the way its built-in web interface
does, using the [python-daikin-itm][library] library.

## Features

The iTM gets a device of its own, with a device per indoor unit under it. Each unit has:

- a **climate** entity: off plus the operation modes the unit offers, the setpoint, the fan speed (when
  the unit allows changing it) and the room temperature. Choosing a mode for a unit that is off starts it
  in that mode.
- a **room temperature** sensor, with long-term statistics.
- **Malfunction** and **Filter** problem sensors (diagnostic), for alerts and automations.

Devices and entities are identified by the unit's bus address (e.g. `1:1-04`, from the iTM's Details
screen), so renaming a unit in the iTM keeps its entities and history. Devices are named after the
description entered in the iTM (e.g. "Meeting room"), or the unit name when there is none.

## Requirements

- Home Assistant 2026.10 or newer.
- An iTM reachable from Home Assistant, and an iTM account. Preferably create a dedicated account for Home
  Assistant.

## HACS installation

Add `https://github.com/robbinjanssen/home-assistant-daikin-itm` as a custom repository of type
`Integration` in HACS, then install `Daikin intelligent Touch Manager` and restart Home Assistant.

## Manual installation

Create a directory called `daikin_itm` in the `<config directory>/custom_components/` directory on your
Home Assistant instance. Install this integration by copying all files in `/custom_components/daikin_itm/`
from this repo into the new `<config directory>/custom_components/daikin_itm/` directory you just created,
and restart Home Assistant.

## Configuration

[![ha_badge][ha-add-shield]][ha-add-url]

Add the integration using [Home Assistant integrations][ha-add-url]. Enter:

- **Host**: the address of the iTM, e.g. `192.168.1.20`. Use a full `https://` address if your iTM offers
  HTTPS.
- **Username** and **Password** of the iTM account.
- **Zone with the indoor units**: the path of zone names in the iTM's icon view, separated by `/`, e.g.
  `Building/Indoor units`. Leave it empty when the units are at the top level. If the zone is not found,
  or has no units, the form lists the zones that are there.

To change these settings later, for example after changing the password or when the iTM got another
address, choose **Reconfigure** in the integration's menu. The entities stay the same.

## Things to know

- **The iTM allows one web session at a time.** Home Assistant logs in, reads all units and logs out every
  60 seconds, so the web interface is free in between. While someone is logged in to the web interface,
  Home Assistant keeps showing the last known state for a few minutes and then marks the units
  unavailable. If logins keep being refused for 30 minutes, a repair issue tells you: either someone is
  still logged in, or the credentials have changed.
- **Changes take a while.** Writing a change to the iTM takes a few seconds, and it takes another 3-8
  seconds to reach the unit. Home Assistant shows a change right away, goes back if the iTM does not
  accept it, and a read 10 seconds later confirms it.
- **The iTM ignores the setpoint of a unit that is off.** The climate entity shows no target temperature
  while the unit is off; turn it on (or choose a mode) first.
- **Indoor units allow a limited number of setting changes** (Daikin mentions about 7000 a year per unit).
  Avoid automations that change setpoints over and over.
- **Security:** the iTM's login is weak by design (an unsalted password hash, over plain HTTP by default).
  Keep the iTM and Home Assistant on an isolated network segment and use HTTPS if your iTM offers it. See
  the [library's security notes][library-security].

## Diagnostics

Download the diagnostics from the integration's menu when reporting an issue. They contain what was read
from the iTM; the host and credentials are left out.

## Contributing

Please see [CONTRIBUTING](.github/CONTRIBUTING.md) and [CODE_OF_CONDUCT](.github/CODE_OF_CONDUCT.md) for
details, and [AGENTS.md](AGENTS.md) for the layout and conventions.

## References & Thanks

- [python-daikin-itm][library]

[library]: https://github.com/robbinjanssen/python-daikin-itm
[library-security]: https://github.com/robbinjanssen/python-daikin-itm#security-notes

[maintenance-shield]: https://img.shields.io/maintenance/yes/2026.svg
[project-stage-shield]: https://img.shields.io/badge/project%20stage-experimental-yellow.svg?style=for-the-badge

[hacs-url]: https://github.com/hacs/integration
[hacs-shield]: https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge

[ha-add-url]: https://my.home-assistant.io/redirect/config_flow_start/?domain=daikin_itm
[ha-add-shield]: https://my.home-assistant.io/badges/config_flow_start.svg
