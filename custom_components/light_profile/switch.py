"""Switches for whole lighting profiles and their rooms."""

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import slugify

from . import LightProfileEntry
from .const import (
    DATA_ASSIST,
    DOMAIN,
    GROUP,
    GROUP_AREA,
    PROFILE_NUMBER,
    PROFILE_STATE_UPDATED,
    area_switch_id,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LightProfileEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    areas = ar.async_get(hass)
    registry = er.async_get(hass)
    voice = hass.data.get(DATA_ASSIST)
    main = ProfileSwitch(entry)
    rooms: dict[str, ProfileSwitch] = {}
    async_add_entities([main])

    @callback
    def sync_rooms() -> None:
        assigned = {
            area_id: area.name
            for subentry in entry.subentries.values()
            if subentry.subentry_type == GROUP
            and (area_id := subentry.data.get(GROUP_AREA))
            and (area := areas.async_get_area(area_id))
        }
        for area_id in rooms.keys() - assigned.keys():
            rooms.pop(area_id)
            if entity_id := registry.async_get_entity_id(
                "switch", entry.domain, area_switch_id(entry.entry_id, area_id)
            ):
                registry.async_remove(entity_id)

        added = []
        for area_id, name in assigned.items():
            if area_id not in rooms:
                rooms[area_id] = ProfileSwitch(entry, area_id, name)
                added.append(rooms[area_id])
            else:
                rooms[area_id].update_name(name)
        if added:
            async_add_entities(added)
        main.update_name()
        if voice is not None:
            voice.update(entry.entry_id, entry.title, set(assigned))

    async def entry_updated(hass: HomeAssistant, entry: LightProfileEntry) -> None:
        sync_rooms()

    @callback
    def area_updated(event: Event) -> None:
        sync_rooms()

    entry.async_on_unload(entry.add_update_listener(entry_updated))
    entry.async_on_unload(
        hass.bus.async_listen(ar.EVENT_AREA_REGISTRY_UPDATED, area_updated)
    )
    if voice is not None:
        entry.async_on_unload(lambda: voice.remove(entry.entry_id))
    sync_rooms()


class ProfileSwitch(SwitchEntity):
    """Activate one lighting profile or its groups in one room."""

    _attr_icon = "mdi:lightbulb-group-outline"
    _attr_should_poll = False

    def __init__(
        self,
        entry: LightProfileEntry,
        area_id: str | None = None,
        area_name: str | None = None,
    ) -> None:
        self._entry = entry
        self._area_id = area_id
        self._attr_unique_id = (
            area_switch_id(entry.entry_id, area_id) if area_id else entry.entry_id
        )
        self._attr_name = self._name(area_name)
        self._controller = entry.runtime_data

    @property
    def suggested_object_id(self) -> str:
        base = f"{DOMAIN}_{self._entry.data[PROFILE_NUMBER]}"
        return f"{base}_{slugify(self._area_id)}" if self._area_id else base

    def _name(self, area_name: str | None) -> str:
        return f"{self._entry.title} · {area_name}" if area_name else self._entry.title

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._area_id is not None and self.registry_entry is not None:
            er.async_get(self.hass).async_update_entity(
                self.entity_id, area_id=self._area_id
            )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, PROFILE_STATE_UPDATED, self._state_changed
            )
        )

    @callback
    def update_name(self, area_name: str | None = None) -> None:
        self._attr_name = self._name(area_name)
        if self.hass is not None and self.entity_id in self.platform.entities:
            self.async_write_ha_state()

    @callback
    def _state_changed(self, entry_id: str) -> None:
        if entry_id == self._entry.entry_id:
            self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        return self._controller.is_enabled(self._entry.entry_id, self._area_id)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._controller.async_set_enabled(
            self._entry.entry_id, True, self._area_id
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._controller.async_set_enabled(
            self._entry.entry_id, False, self._area_id
        )
