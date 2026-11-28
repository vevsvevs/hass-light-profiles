"""Register local Assist sentences for profile and group switches."""

import re

from hassil.recognize import RecognizeResult
from hassil.util import normalize_for_matching
from homeassistant.components.conversation import ConversationInput
from homeassistant.components.conversation.agent_manager import get_agent_manager
from homeassistant.const import SERVICE_TURN_OFF, SERVICE_TURN_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, group_switch_id


def _key(text: str) -> str:
    return normalize_for_matching(text).text.casefold()


def _ru_locations(name: str) -> set[str]:
    # Common room names such as «Гостиная», «Ванная» and «Кухня».
    if name.startswith(("в ", "во ", "на ")):
        return {name}
    locations = {f"в {name}"}
    for suffix, ending in (("ая", "ой"), ("яя", "ей"), ("ня", "не")):
        if name.endswith(suffix):
            locations.add(f"в {name[: -len(suffix)]}{ending}")
            break
    return locations


class VoiceCommands:
    """Route a spoken name to the profile or the explicitly named room."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._profiles: dict[str, tuple[str, dict[str, set[str]]]] = {}
        self._unregister = None
        self._targets: dict[str, set[tuple[str, str | None, str]]] = {}

    @callback
    def update(self, entry_id: str, name: str, areas: dict[str, set[str]]) -> None:
        self._profiles[entry_id] = (name, areas)
        self._refresh()

    @callback
    def remove(self, entry_id: str) -> None:
        self._profiles.pop(entry_id, None)
        self._refresh()

    @callback
    def _refresh(self) -> None:
        targets: dict[str, set[tuple[str, str | None, str]]] = {}
        registry = ar.async_get(self.hass)
        for entry_id, (title, areas) in self._profiles.items():
            name = _key(title)
            if not name:
                continue
            for enabled, ru, en in (
                (True, "включи", "turn on"),
                (False, "выключи", "turn off"),
            ):
                action = SERVICE_TURN_ON if enabled else SERVICE_TURN_OFF
                for sentence in (f"{ru} {name}", f"{en} {name}"):
                    targets.setdefault(sentence, set()).add((entry_id, None, action))
                for area_id in areas:
                    if not (area := registry.async_get_area(area_id)):
                        continue
                    target = (entry_id, area_id, action)
                    for label in (area.name, *area.aliases):
                        label = _key(label)
                        if not label:
                            continue
                        for location in _ru_locations(label):
                            targets.setdefault(f"{ru} {name} {location}", set()).add(
                                target
                            )
                        targets.setdefault(f"{en} {name} in {label}", set()).add(target)

        if targets == self._targets:
            return
        self._targets = targets
        if self._unregister is not None:
            self._unregister()
            self._unregister = None
        if targets:
            self._unregister = get_agent_manager(self.hass).register_trigger(
                sentences=[
                    re.sub(r"([\\()[\]{}<>|;])", r"\\\1", sentence)
                    for sentence in targets
                ],
                trigger_callback=self._handle,
            )

    async def _handle(
        self, user_input: ConversationInput, _result: RecognizeResult
    ) -> str | None:
        russian = user_input.language.split("-", 1)[0] == "ru"
        targets = self._targets.get(_key(user_input.text), set())
        if len(targets) != 1:
            return (
                "Уточните название режима или комнаты"
                if russian
                else "Specify the profile or room name"
            )

        ((entry_id, area_id, action),) = targets
        unique_ids = (
            [entry_id]
            if area_id is None
            else [
                group_switch_id(entry_id, group_id)
                for group_id in self._profiles[entry_id][1][area_id]
            ]
        )
        registry = er.async_get(self.hass)
        entity_ids = [
            entity_id
            for unique_id in unique_ids
            if (entity_id := registry.async_get_entity_id("switch", DOMAIN, unique_id))
            and self.hass.states.get(entity_id)
        ]
        if not entity_ids:
            return (
                "Режим недоступен" if russian else "The lighting profile is unavailable"
            )

        await self.hass.services.async_call(
            "switch",
            action,
            {"entity_id": entity_ids},
            blocking=True,
            context=user_input.context,
        )
        return None
