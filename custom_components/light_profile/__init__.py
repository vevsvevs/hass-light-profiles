"""Light Profiles for Home Assistant."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, HomeAssistantError
from homeassistant.helpers.typing import ConfigType

from .const import PRIORITY
from .controller import LightProfiles

PLATFORMS = (Platform.SWITCH,)
DATA_CONTROLLER = "light_profile.controller"
type LightProfileEntry = ConfigEntry[LightProfiles]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Prepare one controller shared by every profile."""
    controller = LightProfiles(hass)
    await controller.async_load()
    hass.data[DATA_CONTROLLER] = controller
    return True


async def async_setup_entry(hass: HomeAssistant, entry: LightProfileEntry) -> bool:
    """Load one profile and its independent switch."""
    controller = entry.runtime_data = hass.data[DATA_CONTROLLER]
    try:
        await _async_update_profile(hass, entry)
    except HomeAssistantError as err:
        raise ConfigEntryError(str(err)) from err
    entry.async_on_unload(entry.add_update_listener(_async_update_profile))

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await controller.async_unload_profile(entry.entry_id)
        raise
    return True


async def _async_update_profile(hass: HomeAssistant, entry: LightProfileEntry) -> None:
    await entry.runtime_data.async_set_profile(
        entry.entry_id, entry.data[PRIORITY], entry.subentries
    )


async def async_unload_entry(hass: HomeAssistant, entry: LightProfileEntry) -> bool:
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await entry.runtime_data.async_unload_profile(entry.entry_id)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: LightProfileEntry) -> None:
    if controller := hass.data.get(DATA_CONTROLLER):
        await controller.async_forget_profile(entry.entry_id)
