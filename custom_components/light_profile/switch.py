"""Switches for whole lighting profiles and individual groups."""

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import LightProfileEntry
from .const import (
    DATA_ASSIST,
    DOMAIN,
    GROUP,
    GROUP_AREA,
    PROFILE_STATE_UPDATED,
    group_switch_id,
    switch_code,
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
    groups: dict[str, ProfileSwitch] = {}
    async_add_entities([main])

    # Older versions registered a switch for each room instead of each group.
    prefix = f"{entry.entry_id}:"
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            entity.platform == DOMAIN
            and entity.domain == "switch"
            and entity.unique_id.startswith(prefix)
            and not entity.unique_id.startswith(f"{prefix}group:")
        ):
            registry.async_remove(entity.entity_id)

    @callback
    def sync_groups() -> None:
        configured = {
            subentry.subentry_id: subentry
            for subentry in entry.subentries.values()
            if subentry.subentry_type == GROUP
        }
        for group_id in groups.keys() - configured.keys():
            groups.pop(group_id)
            if entity_id := registry.async_get_entity_id(
                "switch", DOMAIN, group_switch_id(entry.entry_id, group_id)
            ):
                registry.async_remove(entity_id)

        assigned: dict[str, set[str]] = {}
        for group_id, subentry in configured.items():
            area_id = subentry.data.get(GROUP_AREA)
            if area_id and not areas.async_get_area(area_id):
                area_id = None
            if area_id:
                assigned.setdefault(area_id, set()).add(group_id)
            if group_id not in groups:
                group = groups[group_id] = ProfileSwitch(entry, group_id)
                group.update(subentry, area_id)
                async_add_entities([group], config_subentry_id=group_id)
            else:
                groups[group_id].update(subentry, area_id)

        main.update()
        if voice is not None:
            voice.update(entry.entry_id, entry.title, assigned)

    async def entry_updated(hass: HomeAssistant, entry: LightProfileEntry) -> None:
        sync_groups()

    @callback
    def area_updated(event: Event) -> None:
        sync_groups()

    entry.async_on_unload(entry.add_update_listener(entry_updated))
    entry.async_on_unload(
        hass.bus.async_listen(ar.EVENT_AREA_REGISTRY_UPDATED, area_updated)
    )
    if voice is not None:
        entry.async_on_unload(lambda: voice.remove(entry.entry_id))
    sync_groups()


class ProfileSwitch(SwitchEntity):
    """Activate one lighting profile or one of its groups."""

    _attr_icon = "mdi:lightbulb-group-outline"
    _attr_should_poll = False

    def __init__(self, entry: LightProfileEntry, group_id: str | None = None) -> None:
        self._entry = entry
        self._group_id = group_id
        self._area_id: str | None = None
        self._attr_unique_id = (
            group_switch_id(entry.entry_id, group_id) if group_id else entry.entry_id
        )
        self._attr_name = entry.title
        self._controller = entry.runtime_data

    @property
    def suggested_object_id(self) -> str:
        base = f"{DOMAIN}_{switch_code(self._entry.entry_id).lower()}"
        if self._group_id is not None:
            return f"{base}_group_{switch_code(self._group_id).lower()}"
        return base

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._update_area()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, PROFILE_STATE_UPDATED, self._state_changed
            )
        )

    @callback
    def _update_area(self) -> None:
        if (
            self._group_id is not None
            and self.registry_entry is not None
            and self.registry_entry.area_id != self._area_id
        ):
            er.async_get(self.hass).async_update_entity(
                self.entity_id, area_id=self._area_id
            )

    @callback
    def update(
        self, group: ConfigSubentry | None = None, area_id: str | None = None
    ) -> None:
        self._area_id = area_id
        self._attr_name = (
            f"{self._entry.title} · {group.title}" if group else self._entry.title
        )
        if self.hass is not None and self.entity_id in self.platform.entities:
            self._update_area()
            self.async_write_ha_state()

    @callback
    def _state_changed(self, entry_id: str) -> None:
        if entry_id == self._entry.entry_id:
            self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        return self._controller.is_enabled(self._entry.entry_id, self._group_id)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._controller.async_set_enabled(
            self._entry.entry_id, True, self._group_id
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._controller.async_set_enabled(
            self._entry.entry_id, False, self._group_id
        )
