# Light Profiles

[Русская версия](README.ru.md)

Light Profiles is a custom Home Assistant integration that applies lighting presets without changing the automations that turn your lights on. Each profile has its own switch, a unique numeric priority, and one or more groups of lights.

## What it does

- Sets brightness, RGB or color temperature, and transition time per group. Compatible lights can use individual effects.
- Set brightness to 0% to turn off lights in a group when the profile takes over and keep them off on matching turn-on commands.
- Temporarily disable individual groups from their settings while keeping the rest of the profile active.
- Filters turn-on commands by device, entity, automation, or script. Commands from the Home Assistant interface are controlled separately.
- Uses the highest-priority active profile for each light. Already-on lights follow the new profile when it takes over. After the last applicable profile is switched off, original settings are restored immediately for lights that are on and on the next turn-on for lights that are off.

## Example setups

| Profile | Priority | Use case |
| --- | ---: | --- |
| Evening | 10 | Set the living room to a warm, dim light |
| Night | 30 | Turn on the bathroom at 8% when a motion sensor triggers an automation |
| Movie | 50 | Dim the lights and select an effect for a compatible LED strip |

If Evening and Movie are active and share a light, Movie takes precedence. Turning Movie off returns the light to Evening.

## Install

Copy `custom_components/light_profile` into the `custom_components` folder of your Home Assistant configuration. Restart Home Assistant, then go to **Settings → Devices & services → Add integration → Light Profiles**. Give the profile a name and priority, add a group, and turn on the profile switch.

Tested on Home Assistant Core 2026.11.0 and 2026.11.3. The integration hooks into `light.turn_on` and `light.toggle` and may conflict with integrations that also replace those handlers.

Built with AI assistance.
