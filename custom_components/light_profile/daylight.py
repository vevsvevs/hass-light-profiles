"""Read the light settings that Home Assistant actually reported."""

from typing import Any

from homeassistant.components.light import ATTR_EFFECT_LIST, ColorMode
from homeassistant.components.light.const import LightServiceArgument as Arg
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import State

COLOR_BY_MODE = {
    ColorMode.COLOR_TEMP: Arg.COLOR_TEMP_KELVIN,
    ColorMode.HS: Arg.HS_COLOR,
    ColorMode.RGB: Arg.RGB_COLOR,
    ColorMode.RGBW: Arg.RGBW_COLOR,
    ColorMode.RGBWW: Arg.RGBWW_COLOR,
    ColorMode.XY: Arg.XY_COLOR,
}
COLOR_PARAMS = frozenset(
    (*COLOR_BY_MODE.values(), Arg.COLOR_NAME, Arg.PROFILE, Arg.WHITE)
)
BRIGHTNESS_PARAMS = frozenset(
    (Arg.BRIGHTNESS, Arg.BRIGHTNESS_PCT, Arg.BRIGHTNESS_STEP, Arg.BRIGHTNESS_STEP_PCT)
)


def available_effects(state: State | None) -> tuple[str, ...] | None:
    """Read the device's effect names, distinguishing an unknown capability."""
    if state is None or (effects := state.attributes.get(ATTR_EFFECT_LIST)) is None:
        return None
    return tuple(dict.fromkeys(effect for effect in effects if effect))


def effect_off(effects: tuple[str, ...]) -> str | None:
    """Find an advertised value that disables an effect."""
    return next(
        (effect for effect in effects if effect.casefold() in {"off", "none"}), None
    )


def reported_settings(state: State | None) -> dict[str, Any]:
    """Return the reported brightness, active color and effect when available."""
    if state is None or state.state not in (STATE_ON, STATE_OFF):
        return {}

    attrs = state.attributes
    settings = {}
    brightness = attrs.get(Arg.BRIGHTNESS)
    mode = attrs.get("color_mode")
    if isinstance(brightness, (int, float)) and 0 < brightness <= 255:
        # Some lights report fractional brightness. A restore command uses an integer,
        # and rounding a positive brightness to zero would turn the light off.
        key = Arg.WHITE if mode == ColorMode.WHITE else Arg.BRIGHTNESS
        settings[key] = max(1, round(brightness))
    if (color := COLOR_BY_MODE.get(mode)) and (value := attrs.get(color)) is not None:
        settings[color] = value
    effects = available_effects(state) or ()
    effect = attrs.get(Arg.EFFECT)
    if effect in effects:
        settings[Arg.EFFECT] = effect
    elif effect is None and state.state == STATE_ON:
        if off := effect_off(effects):
            settings[Arg.EFFECT] = off
    return settings


def restore_missing(params: dict[str, Any], saved: dict[str, Any]) -> dict[str, Any]:
    """Use daylight settings for arguments the next turn-on leaves unspecified."""
    defaults = {
        key: tuple(value) if isinstance(value, list) else value
        for key, value in saved.items()
    }
    has_brightness = bool(BRIGHTNESS_PARAMS.intersection(params) or Arg.WHITE in params)
    has_color = bool((COLOR_PARAMS | {Arg.EFFECT}).intersection(params))
    if has_color:
        white = defaults.get(Arg.WHITE)
        for key in COLOR_PARAMS | {Arg.EFFECT}:
            defaults.pop(key, None)
        if white and not has_brightness:
            defaults[Arg.BRIGHTNESS] = white
    if has_brightness:
        defaults.pop(Arg.BRIGHTNESS, None)
        defaults.pop(Arg.WHITE, None)
    return defaults | params
