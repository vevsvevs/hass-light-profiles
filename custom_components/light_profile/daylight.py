"""Read the light settings that Home Assistant actually reported."""

from collections.abc import Mapping
from math import isfinite
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
_COLOR_LIMITS = {
    Arg.HS_COLOR: (360, 100),
    Arg.RGB_COLOR: (255,) * 3,
    Arg.RGBW_COLOR: (255,) * 4,
    Arg.RGBWW_COLOR: (255,) * 5,
    Arg.XY_COLOR: (1, 1),
}


def _number(value: Any) -> int | float | None:
    if isinstance(value, str):
        try:
            value = float(value)
        except ValueError:
            return None
    if type(value) is int or (type(value) is float and isfinite(value)):
        return value
    return None


def normalize_settings(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Keep reported settings that can safely be sent to light.turn_on."""
    normalized = {}
    for key, value in settings.items():
        if key in (Arg.BRIGHTNESS, Arg.WHITE):
            if (level := _number(value)) is not None and 0 < level <= 255:
                # Zero brightness would turn a light off rather than restore it.
                normalized[key] = max(1, round(level))
        elif key == Arg.COLOR_TEMP_KELVIN:
            if (temperature := _number(value)) is not None and temperature >= 1:
                normalized[key] = round(temperature)
        elif key in _COLOR_LIMITS:
            limits = _COLOR_LIMITS[key]
            if isinstance(value, (list, tuple)) and len(value) == len(limits):
                channels = tuple(_number(channel) for channel in value)
                if all(
                    channel is not None and 0 <= channel <= limit
                    for channel, limit in zip(channels, limits, strict=True)
                ):
                    normalized[key] = (
                        channels
                        if key in (Arg.HS_COLOR, Arg.XY_COLOR)
                        else tuple(round(channel) for channel in channels)
                    )
        elif key == Arg.EFFECT and isinstance(value, str) and value.strip():
            normalized[key] = value
    return normalized


def available_effects(state: State | None) -> tuple[str, ...] | None:
    """Read the device's effect names, distinguishing an unknown capability."""
    if state is None or not isinstance(
        effects := state.attributes.get(ATTR_EFFECT_LIST), (list, tuple)
    ):
        return None
    return tuple(
        dict.fromkeys(
            effect for effect in effects if isinstance(effect, str) and effect.strip()
        )
    )


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
    mode = attrs.get("color_mode")
    key = Arg.WHITE if mode == ColorMode.WHITE else Arg.BRIGHTNESS
    settings = {key: attrs.get(Arg.BRIGHTNESS)}
    if isinstance(mode, str) and (color := COLOR_BY_MODE.get(mode)):
        settings[color] = attrs.get(color)
    effects = available_effects(state) or ()
    effect = attrs.get(Arg.EFFECT)
    if effect in effects:
        settings[Arg.EFFECT] = effect
    elif effect is None and state.state == STATE_ON:
        if off := effect_off(effects):
            settings[Arg.EFFECT] = off
    return normalize_settings(settings)


def restore_missing(params: dict[str, Any], saved: dict[str, Any]) -> dict[str, Any]:
    """Use daylight settings for arguments the next turn-on leaves unspecified."""
    defaults = saved.copy()
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
