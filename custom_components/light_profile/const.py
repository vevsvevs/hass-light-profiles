"""Constants for Light Profiles."""

DOMAIN = "light_profile"
GROUP = "light_group"
GROUP_ENABLED = "enabled"
GROUP_AREA = "area"
DATA_ASSIST = "light_profile.assist"
PROFILE_STATE_UPDATED = "light_profile_state_updated"

LIGHTS = "lights"
SOURCES = "sources"
INCLUDE_UI = "include_ui"
DEVICE_PREFIX = "device:"
ENTITY_PREFIX = "entity:"
BRIGHTNESS = "brightness"
TEMPERATURE = "temperature"
COLOR = "color"
TRANSITION_MS = "transition_ms"
EFFECTS = "effects"
CONFIGURE_EFFECTS = "configure_effects"
PRIORITY = "priority"
PROFILE_NUMBER = "profile_number"


def area_switch_id(entry_id: str, area_id: str) -> str:
    """Return the stable registry key for a room switch."""
    return f"{entry_id}:{area_id}"
