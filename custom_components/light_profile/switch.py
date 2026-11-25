"""Switches for individual lighting profiles."""

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import LightProfileEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LightProfileEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([ProfileSwitch(entry)])


class ProfileSwitch(SwitchEntity):
    """Activate one lighting profile."""

    _attr_icon = "mdi:lightbulb-group-outline"
    _attr_should_poll = False

    def __init__(self, entry: LightProfileEntry) -> None:
        self._entry = entry
        self._attr_unique_id = entry.entry_id
        self._attr_name = entry.title
        self._controller = entry.runtime_data

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._entry.add_update_listener(self._async_update_name))

    async def _async_update_name(
        self, hass: HomeAssistant, entry: LightProfileEntry
    ) -> None:
        if self._attr_name != entry.title:
            self._attr_name = entry.title
            self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        return self._controller.is_enabled(self._entry.entry_id)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._controller.async_set_enabled(self._entry.entry_id, True)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._controller.async_set_enabled(self._entry.entry_id, False)
        self.async_write_ha_state()
