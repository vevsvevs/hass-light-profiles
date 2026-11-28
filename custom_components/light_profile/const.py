"""Constants for Light Profiles."""

from base64 import b32encode
from hashlib import blake2s

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


def switch_code(unique_id: str) -> str:
    """Derive a short code from Home Assistant's immutable, random ID."""
    return b32encode(blake2s(unique_id.encode(), digest_size=5).digest()).decode()


def group_switch_id(entry_id: str, group_id: str) -> str:
    """Return the registry key of a group's switch."""
    return f"{entry_id}:group:{group_id}"
