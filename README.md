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
- for units that offer heating and cooling: **auto mode** (see below), with an **Auto mode** sensor that
  shows what it chose and why, and a **Ventilate in auto mode** switch (configuration).

The iTM's device also gets an **Outdoor temperature** sensor, which auto mode uses.

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

## Auto mode

Choose **Auto** as the mode of a unit to keep its room at the target temperature. The unit keeps the room
at its setpoint itself; auto mode chooses between heating, cooling and ventilating, with the target as the
setpoint. It changes the mode rarely, so the room doesn't bounce between heating and cooling and the unit's
limited number of setting changes lasts:

- It compares the **20-minute average** room temperature with the target, and waits for 15 minutes of
  readings after starting Home Assistant.
- It **changes over** from heating to cooling at 1.5 °C above the target, and from cooling to heating at
  1.5 °C below it; not within 2 hours of the previous changeover, and not while the room is already
  heading back. When the outdoor temperature averaged over 24 hours is below 12 °C, cooling starts only
  at 2.5 °C above the target; above 20 °C heating starts only at 2.5 °C below it.
- With **Ventilate in auto mode** on (the default, for units that offer fan mode) it switches to fan once
  the room is 0.5 °C past the target after at least 30 minutes of heating or cooling, which saves energy,
  and back to heating (or cooling) when the room is 0.5 °C on the other side.
- It writes at most **12 changes a day**: mode changes, at least 20 minutes apart, and the setpoints it
  sets after them. It stops ventilating when only 4 are left. After a mode change it sets the setpoint
  on the next read, when the unit shows the new mode; when the unit doesn't allow the target in that
  mode, it uses the nearest setpoint and keeps the target.
- Changing the temperature changes the target. Choosing another mode, or changing the mode at the remote
  controller or in the iTM, stops auto mode. A setpoint changed at the remote controller becomes the new
  target, also while ventilating.
- While logins are refused (someone uses the iTM's web interface) it doesn't decide on the old readings.

The **Auto mode** sensor shows the mode auto mode chose. Its attributes show the reason, since when, the
room and outdoor temperature it was based on, the number of changes in the last 24 hours, and which change
it holds back and why. Every decision also shows in the **logbook**, e.g. *Meeting room: Cool: too warm,
changing over to cooling (Room temperature 22.6 °C, Target 21 °C, Outdoor temperature 15.2 °C)*, and is
fired as a `daikin_itm_auto_mode` event for automations.

The outdoor temperature comes from [Open-Meteo.com](https://open-meteo.com/) every 15 minutes, for the
location of Home Assistant. To use another location, choose **Configure** in the integration's menu.
When Open-Meteo can't be read, auto mode keeps the last average for 3 hours; without internet it works
without the outdoor temperature.

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
