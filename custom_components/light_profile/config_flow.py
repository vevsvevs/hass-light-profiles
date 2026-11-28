"""Configure Light Profiles and its groups in Home Assistant."""

import asyncio
import re
from secrets import choice
from string import ascii_lowercase, digits
from typing import Any

import probatio as vol
from homeassistant.components.http import KEY_HASS_USER
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentry,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from homeassistant.helpers.http import current_request
from homeassistant.helpers.target import (
    TargetSelection,
    async_extract_referenced_entity_ids,
)
from homeassistant.helpers.translation import async_get_translations

from .const import (
    BRIGHTNESS,
    COLOR,
    CONFIGURE_EFFECTS,
    DEVICE_PREFIX,
    DOMAIN,
    EFFECTS,
    ENTITY_PREFIX,
    GROUP,
    GROUP_AREA,
    GROUP_ENABLED,
    IDENTIFIER,
    INCLUDE_UI,
    LIGHTS,
    PRIORITY,
    SOURCES,
    SWITCH_CODE,
    TEMPERATURE,
    TRANSITION_MS,
    group_switch_id,
    switch_object_id,
)
from .daylight import available_effects

SOURCE_DEVICES = "source_devices"
SOURCE_ENTITIES = "source_entities"

PRESET_FIELDS = {
    vol.Optional(BRIGHTNESS, default=10): selector.NumberSelector(
        {"min": 0, "max": 100, "unit_of_measurement": "%"}
    ),
    vol.Optional(TEMPERATURE): selector.NumberSelector(
        {"min": 0, "max": 10000, "mode": "box"}
    ),
    vol.Optional(COLOR): selector.ColorRGBSelector(),
    vol.Optional(TRANSITION_MS): selector.NumberSelector(
        {"min": 0, "max": 60_000, "step": 1, "mode": "box"}
    ),
}


PROFILE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_NAME): selector.TextSelector(),
        vol.Optional(PRIORITY): selector.NumberSelector({"mode": "box", "step": 1}),
    }
)


def _new_switch_code(
    hass: HomeAssistant, used: set[str], profile_code: str | None = None
) -> str:
    registry = er.async_get(hass)
    while True:
        code = "".join(choice(ascii_lowercase + digits) for _ in range(4))
        object_id = (
            switch_object_id(profile_code, code)
            if profile_code
            else switch_object_id(code)
        )
        entity_id = f"switch.{object_id}"
        if (
            code not in used
            and not registry.async_get(entity_id)
            and not hass.states.get(entity_id)
        ):
            return code


def _switch_identifier(hass: HomeAssistant, registry_id: str, object_id: str) -> str:
    return er.async_get(hass).async_get_entity_id("switch", DOMAIN, registry_id) or (
        f"switch.{object_id}"
    )


def _selected_lights(hass: HomeAssistant, target: dict[str, Any]) -> list[str]:
    selection = TargetSelection(vol.Schema(cv.TARGET_SERVICE_FIELDS)(target))
    registry = er.async_get(hass)
    for item in (selection, selection.exclude):
        if item is not None:
            item.entity_ids = {
                er.async_resolve_entity_id(registry, entity_id) or entity_id
                for entity_id in item.entity_ids
            }
    selected = async_extract_referenced_entity_ids(
        hass, selection, expand_group=False, primary_entities_only=False
    )
    return sorted(
        entity_id
        for entity_id in selected.referenced | selected.indirectly_referenced
        if entity_id.startswith("light.")
    )


class LightProfileConfigFlow(ConfigFlow, domain=DOMAIN):
    """Create and edit independent lighting profiles."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        priorities = {entry.data[PRIORITY] for entry in self._async_current_entries()}
        errors = {}
        defaults = {PRIORITY: max(priorities, default=0) + 10}
        if user_input is not None:
            defaults = user_input
            title, priority, errors = _profile_values(self.hass, user_input, priorities)
            if not errors:
                await self.async_set_unique_id(str(priority))
                self._abort_if_unique_id_configured()
                used = {
                    entry.data[SWITCH_CODE]
                    for entry in self._async_current_entries()
                    if SWITCH_CODE in entry.data
                }
                return self.async_create_entry(
                    title=title,
                    data={
                        PRIORITY: priority,
                        SWITCH_CODE: _new_switch_code(self.hass, used),
                    },
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(PROFILE_SCHEMA, defaults),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return LightProfileOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Expose light groups on the integration page."""
        return {GROUP: LightGroupFlow}


class LightProfileOptionsFlow(OptionsFlow):
    """Open profile settings from the gear on the integration page."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self.config_entry
        defaults = {CONF_NAME: entry.title, PRIORITY: entry.data[PRIORITY]}
        errors = {}
        if user_input is not None:
            defaults = user_input
            priorities = {
                other.data[PRIORITY]
                for other in self.hass.config_entries.async_entries(
                    DOMAIN, include_ignore=False
                )
                if other is not entry
            }
            title, priority, errors = _profile_values(self.hass, user_input, priorities)
            if not errors:
                self.hass.config_entries.async_update_entry(
                    entry,
                    title=title,
                    data={**entry.data, PRIORITY: priority},
                    unique_id=str(priority),
                )
                return self.async_create_entry(title="", data=entry.options)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(PROFILE_SCHEMA, defaults),
            errors=errors,
            description_placeholders={
                IDENTIFIER: _switch_identifier(
                    self.hass, entry.entry_id, switch_object_id(entry.data[SWITCH_CODE])
                )
            },
        )


@callback
def _profile_values(
    hass: HomeAssistant, user_input: dict[str, Any], priorities: set[int]
) -> tuple[str, int | None, dict[str, str]]:
    title = user_input.get(CONF_NAME, "").strip()
    value = user_input.get(PRIORITY)
    priority = int(value) if value is not None and float(value).is_integer() else None
    errors = {}
    if not title:
        errors[CONF_NAME] = "empty_profile_name"
    if priority is None:
        errors[PRIORITY] = "invalid_priority"
    elif priority in priorities or hass.config_entries.flow.async_progress_by_handler(
        DOMAIN, match_context={"unique_id": str(priority)}
    ):
        errors[PRIORITY] = "duplicate_priority"
    return title, priority, errors


class LightGroupFlow(ConfigSubentryFlow):
    """Create or edit one group."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show the group creation form."""
        return await self._group_form("user", user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Show the existing group configuration."""
        return await self._group_form(
            "reconfigure", user_input, self._get_reconfigure_subentry()
        )

    async def async_step_effects(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        subentry = (
            self._get_reconfigure_subentry()
            if self.source == SOURCE_RECONFIGURE
            else None
        )
        if user_input is None:
            return await self._effects_form(self._effect_choices, subentry)

        effects = {
            entity_id: user_input[field]
            for field, entity_id in self._effect_fields.items()
            if user_input.get(field)
        }
        return await self._group_form(
            self.source, self._draft, subentry, effects=effects
        )

    async def _effects_form(
        self,
        available: dict[str, tuple[str, ...]],
        subentry: ConfigSubentry | None,
    ) -> SubentryFlowResult:
        options = await _effect_options(self.hass, available)
        saved = subentry.data.get(EFFECTS, {}) if subentry else {}
        fields = {}
        defaults = {}
        self._effect_fields = {}
        used_names = {
            "__proto__",
            "constructor",
            "prototype",
            "toString",
            "valueOf",
            "toLocaleString",
            "hasOwnProperty",
            "isPrototypeOf",
            "propertyIsEnumerable",
            "__defineGetter__",
            "__defineSetter__",
            "__lookupGetter__",
            "__lookupSetter__",
        }
        for entity_id, (name, labels) in options.items():
            # Home Assistant displays the field name when no static translation exists.
            field_name = name
            number = 2
            while field_name in used_names:
                field_name = f"{name} ({number})"
                number += 1
            used_names.add(field_name)
            self._effect_fields[field_name] = entity_id
            fields[vol.Optional(field_name)] = selector.SelectSelector(
                {
                    "options": [{"value": "", "label": "—"}]
                    + [
                        {"value": effect, "label": label}
                        for effect, label in labels.items()
                    ],
                    "mode": selector.SelectSelectorMode.DROPDOWN,
                }
            )
            if saved.get(entity_id) in labels:
                defaults[field_name] = saved[entity_id]

        return self.async_show_form(
            step_id="effects",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(fields), defaults
            ),
            last_step=True,
        )

    async def _group_form(
        self,
        step_id: str,
        user_input: dict[str, Any] | None,
        subentry: ConfigSubentry | None = None,
        *,
        effects: dict[str, str] | None = None,
    ) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        defaults = (
            dict(subentry.data, name=subentry.title)
            if subentry
            else {COLOR: [255, 220, 180]}
        )
        saved = subentry.data.get(EFFECTS, {}) if subentry else {}
        if subentry:
            defaults.setdefault(CONFIGURE_EFFECTS, bool(saved))
            defaults.setdefault(GROUP_ENABLED, True)
            defaults[LIGHTS] = {"entity_id": subentry.data[LIGHTS]}
            sources = subentry.data.get(SOURCES, ())
            defaults[SOURCE_DEVICES] = [
                source.removeprefix(DEVICE_PREFIX)
                for source in sources
                if source.startswith(DEVICE_PREFIX)
            ]
            defaults[SOURCE_ENTITIES] = [
                source.removeprefix(ENTITY_PREFIX)
                for source in sources
                if source.startswith(ENTITY_PREFIX)
            ]

        if user_input is not None:
            defaults = user_input
            data = dict(user_input)
            title = data.pop(CONF_NAME, "").strip()
            try:
                data[LIGHTS] = _selected_lights(self.hass, data.get(LIGHTS, {}))
            except vol.Invalid:
                data[LIGHTS] = []
            if effects is None:
                registry = er.async_get(self.hass)
                devices = data.pop(SOURCE_DEVICES, ())
                entities = data.pop(SOURCE_ENTITIES, ())
                data[SOURCES] = list(
                    dict.fromkeys(
                        [f"{DEVICE_PREFIX}{device}" for device in devices]
                        + [
                            f"{ENTITY_PREFIX}{er.async_resolve_entity_id(registry, entity) or entity}"
                            for entity in entities
                        ]
                    )
                )

            if not title:
                errors[CONF_NAME] = "empty_name"
            if not data[LIGHTS]:
                errors[LIGHTS] = "select_lights"
            if 0 < data.get(TEMPERATURE, 0) < 1000:
                errors[TEMPERATURE] = "invalid_temperature"

            lights = set(data[LIGHTS])
            entry = self._get_entry()
            for existing in entry.subentries.values():
                if existing.subentry_type != GROUP or existing is subentry:
                    continue
                if existing.title.casefold() == title.casefold():
                    errors[CONF_NAME] = "duplicate_name"
                if lights.intersection(existing.data[LIGHTS]):
                    errors[LIGHTS] = "light_already_grouped"

            if not errors:
                if effects is None:
                    if data[CONFIGURE_EFFECTS]:
                        available = _selected_effects(self.hass, data[LIGHTS], saved)
                        if available:
                            self._draft = {
                                **data,
                                CONF_NAME: title,
                                LIGHTS: {"entity_id": data[LIGHTS]},
                            }
                            self._effect_choices = available
                            return await self._effects_form(available, subentry)
                    effects = {
                        entity_id: effect
                        for entity_id, effect in saved.items()
                        if entity_id in lights
                    }
                data[EFFECTS] = effects
                if subentry is None:
                    used = {
                        item.data[SWITCH_CODE]
                        for item in entry.subentries.values()
                        if item.subentry_type == GROUP
                    }
                    data[SWITCH_CODE] = _new_switch_code(
                        self.hass, used, entry.data[SWITCH_CODE]
                    )
                    return self.async_create_entry(title=title, data=data)
                data[SWITCH_CODE] = subentry.data[SWITCH_CODE]
                return self.async_update_and_abort(
                    entry, subentry, title=title, data=data
                )

        fields = {
            vol.Optional(CONF_NAME): selector.TextSelector(),
            vol.Optional(GROUP_ENABLED, default=True): selector.BooleanSelector(),
            vol.Optional(GROUP_AREA): selector.AreaSelector(),
            vol.Optional(LIGHTS): selector.TargetSelector(
                {"entity": {"domain": "light"}, "primary_entities_only": False}
            ),
            vol.Optional(CONFIGURE_EFFECTS, default=False): selector.BooleanSelector(),
        }
        schema = vol.Schema(
            {
                **fields,
                **PRESET_FIELDS,
                vol.Optional(SOURCE_DEVICES): selector.DeviceSelector(
                    {"multiple": True}
                ),
                vol.Optional(SOURCE_ENTITIES): selector.EntitySelector(
                    {"multiple": True}
                ),
                vol.Optional(INCLUDE_UI, default=False): selector.BooleanSelector(),
            }
        )
        placeholders = None
        if subentry is not None:
            entry = self._get_entry()
            placeholders = {
                IDENTIFIER: _switch_identifier(
                    self.hass,
                    group_switch_id(entry.entry_id, subentry.subentry_id),
                    switch_object_id(
                        entry.data[SWITCH_CODE], subentry.data[SWITCH_CODE]
                    ),
                )
            }
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(schema, defaults),
            errors=errors,
            description_placeholders=placeholders,
            last_step=False,
        )


@callback
def _selected_effects(
    hass: HomeAssistant, lights: list[str], saved: dict[str, str]
) -> dict[str, tuple[str, ...]]:
    """Read effects only from the lights selected for this group."""
    available = {}
    for entity_id in lights:
        effects = available_effects(hass.states.get(entity_id))
        if effects is None and entity_id in saved:
            effects = (saved[entity_id],)
        if effects:
            available[entity_id] = effects
    return available


async def _effect_language(hass: HomeAssistant) -> str:
    """Follow the authenticated user's frontend language when it is saved."""
    request = current_request.get()
    if request is not None and (user := request.get(KEY_HASS_USER)) is not None:
        from homeassistant.components.frontend.storage import async_user_store

        store = await async_user_store(hass, user.id)
        locale = store.data.get("language")
        if isinstance(locale, dict) and isinstance(
            language := locale.get("language"), str
        ):
            if re.fullmatch(r"[a-zA-Z]{2,3}(?:-[a-zA-Z0-9]{2,8})*", language):
                return language
    return hass.config.language


async def _effect_options(
    hass: HomeAssistant, available: dict[str, tuple[str, ...]]
) -> dict[str, tuple[str, dict[str, str]]]:
    registry = er.async_get(hass)
    entries = {entity_id: registry.async_get(entity_id) for entity_id in available}
    platforms = {
        entry.platform for entry in entries.values() if entry and entry.translation_key
    }
    language = await _effect_language(hass)
    entity_labels, light_labels = await asyncio.gather(
        async_get_translations(hass, language, "entity", platforms),
        async_get_translations(hass, language, "entity_component", {"light"}),
    )

    options = {}
    for entity_id, effects in available.items():
        state = hass.states.get(entity_id)
        entry = entries[entity_id]
        name = state.name if state else (entity_id.split(".", 1)[1].replace("_", " "))
        if not state and entry:
            name = entry.name or entry.original_name or name
        labels = {}
        for effect in effects:
            suffix = f"state_attributes.effect.state.{effect}"
            translated = None
            if entry and entry.translation_key:
                translated = entity_labels.get(
                    f"component.{entry.platform}.entity.light.{entry.translation_key}.{suffix}"
                )
            label = translated or light_labels.get(
                f"component.light.entity_component._.{suffix}", effect
            )
            labels[effect] = label
        options[entity_id] = (name, labels)
    return options
