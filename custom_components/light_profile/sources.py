"""Identify the sources in the Home Assistant action chain."""

from collections import OrderedDict
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from time import monotonic

from homeassistant.components.automation.const import EVENT_AUTOMATION_TRIGGERED
from homeassistant.components.script.const import DOMAIN as SCRIPT_DOMAIN
from homeassistant.components.script.const import EVENT_SCRIPT_STARTED
from homeassistant.components.websocket_api.connection import current_connection
from homeassistant.const import (
    EVENT_CALL_SERVICE,
    EVENT_STATE_CHANGED,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Context, Event, HomeAssistant, callback, valid_entity_id
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.http import current_request
from homeassistant.helpers.script import script_stack_cv

from .const import DEVICE_PREFIX, ENTITY_PREFIX

CONTEXT_TTL = 600
CONTEXT_LIMIT = 4096
BROWSER_SOURCE = "browser"


@dataclass(frozen=True, slots=True)
class Cause:
    """Sources associated with a Home Assistant context."""

    parent_id: str | None
    sources: frozenset[str]
    recorded_at: float


class SourceTracker:
    """Follow context parents and the scripts active in the current branch."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._registry = er.async_get(hass)
        self._causes: OrderedDict[str, Cause] = OrderedDict()
        self._calling_scripts: ContextVar[tuple[str, frozenset[str]] | None] = (
            ContextVar("light_profile_calling_scripts", default=None)
        )

    @callback
    def clear(self) -> None:
        self._causes.clear()

    @callback
    def _entity_sources(self, entity_id: str) -> set[str]:
        sources = {f"{ENTITY_PREFIX}{entity_id}"}
        if (entry := self._registry.async_get(entity_id)) and entry.device_id:
            sources.add(f"{DEVICE_PREFIX}{entry.device_id}")
        return sources

    @callback
    def _script_sources(self, context: Context) -> frozenset[str]:
        stack = script_stack_cv.get()
        if not stack:
            return frozenset()

        sources = set()
        if (calling := self._calling_scripts.get()) and calling[0] in (
            context.id,
            context.parent_id,
        ):
            sources.update(calling[1])

        if (component := self._hass.data.get(SCRIPT_DOMAIN)) and any(
            name.startswith("script.") for name in stack
        ):
            active_scripts = set(stack)
            for entity in component.entities:
                script = getattr(entity, "script", None)
                if script is not None and script.unique_id in active_scripts:
                    sources.update(self._entity_sources(entity.entity_id))
        return frozenset(sources)

    @callback
    def _browser_context(self, context: Context) -> bool:
        connection = current_connection.get()
        request = current_request.get()
        return (
            connection is not None
            and connection.hass is self._hass
            and context.user_id == connection.user.id
            and request is not None
            and "Origin" in request.headers
        )

    @callback
    def observe(self, event: Event) -> None:
        """Record what produced an event without inspecting button payloads."""
        context = event.context
        if event.event_type == EVENT_CALL_SERVICE:
            if scripts := self._script_sources(context):
                # Core clears its execution stack for a fire-and-forget script.
                # The new task still inherits the ancestry of this service call.
                self._calling_scripts.set((context.id, scripts))
            if self._browser_context(context):
                self._remember(context, {BROWSER_SOURCE})
            return

        # A script shares the caller's context. Its start is only attributable
        # to the script branch, which is tracked on the execution stack.
        if event.event_type == EVENT_SCRIPT_STARTED:
            return

        sources = set()
        if event.event_type == EVENT_STATE_CHANGED:
            old = event.data.get("old_state")
            new = event.data.get("new_state")
            if (
                old is None
                or new is None
                or new.state == old.state
                or new.state in (STATE_UNKNOWN, STATE_UNAVAILABLE)
                or new.domain in ("automation", SCRIPT_DOMAIN)
            ):
                return
            sources.update(self._entity_sources(new.entity_id))
        else:
            device_id = event.data.get("device_id")
            if isinstance(device_id, str):
                sources.add(f"{DEVICE_PREFIX}{device_id}")
            entity_id = event.data.get("entity_id")
            if isinstance(entity_id, str) and valid_entity_id(entity_id):
                sources.update(self._entity_sources(entity_id))
            if event.event_type == EVENT_AUTOMATION_TRIGGERED:
                sources.update(self._script_sources(context))

        if sources:
            self._remember(context, sources)

    @callback
    def _remember(self, context: Context, sources: set[str]) -> None:
        now = monotonic()
        old = self._causes.pop(context.id, None)
        if old is not None and now - old.recorded_at <= CONTEXT_TTL:
            sources.update(old.sources)
        self._causes[context.id] = Cause(context.parent_id, frozenset(sources), now)
        while len(self._causes) > CONTEXT_LIMIT:
            self._causes.popitem(last=False)

    @callback
    def _ancestors(self, context: Context) -> Iterator[Cause]:
        now = monotonic()
        current = context.id
        parent = context.parent_id
        seen = set()
        while current is not None and current not in seen:
            seen.add(current)
            cause = self._causes.get(current)
            if cause is not None and now - cause.recorded_at <= CONTEXT_TTL:
                yield cause
                parent = parent or cause.parent_id
            elif current != context.id:
                return
            current, parent = parent, None

    @callback
    def matches(self, context: Context, selected: frozenset[str]) -> bool:
        """Match any selected source in the command's causal chain."""
        return (
            (BROWSER_SOURCE in selected and self._browser_context(context))
            or not selected.isdisjoint(self._script_sources(context))
            or any(
                not selected.isdisjoint(cause.sources)
                for cause in self._ancestors(context)
            )
        )
