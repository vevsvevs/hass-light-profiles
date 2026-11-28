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
IDENTIFIER = "identifier"


SWITCH_CODE = "switch_code"


def switch_object_id(profile_code: str, group_code: str | None = None) -> str:
    """Build the entity object ID from a profile and optional group code."""
    base = f"{DOMAIN}_{profile_code}"
    return f"{base}_group_{group_code}" if group_code else base


def group_switch_id(entry_id: str, group_id: str) -> str:
    """Return the registry key of a group's switch."""
    return f"{entry_id}:group:{group_id}"
