"""Apply group presets inside the native Home Assistant light dispatch."""

import asyncio
import logging
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from functools import partial
from typing import Any

from homeassistant.components.light import DOMAIN as LIGHT_DOMAIN
from homeassistant.components.light import LightEntity
from homeassistant.components.light.const import LightServiceArgument as Arg
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import (
    MATCH_ALL,
    SERVICE_TOGGLE,
    SERVICE_TURN_ON,
    STATE_ON,
)
from homeassistant.core import (
    Context,
    Event,
    EventStateChangedData,
    HassJob,
    HomeAssistant,
    ServiceCall,
    State,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.service import entity_service_call
from homeassistant.helpers.storage import Store

from .const import (
    BRIGHTNESS,
    COLOR,
    CONFIGURE_EFFECTS,
    DOMAIN,
    EFFECTS,
    GROUP,
    INCLUDE_UI,
    LIGHTS,
    SOURCES,
    TEMPERATURE,
    TRANSITION_MS,
)
from .daylight import (
    COLOR_PARAMS,
    available_effects,
    effect_off,
    reported_settings,
    restore_missing,
)
from .sources import BROWSER_SOURCE, SourceTracker

_LOGGER = logging.getLogger(__name__)
_BROWSER = frozenset({BROWSER_SOURCE})
_APPEARANCE = COLOR_PARAMS | {Arg.EFFECT}


@dataclass(frozen=True, slots=True)
class Group:
    """Light preset and the sources allowed to activate it."""

    lights: frozenset[str]
    preset: dict[str, Any]
    sources: frozenset[str]
    include_ui: bool
    effects: Mapping[str, str]

    @classmethod
    def from_config(cls, data: Mapping[str, Any]) -> "Group":
        preset = {Arg.BRIGHTNESS: round(255 * data[BRIGHTNESS] / 100)}
        if temperature := data.get(TEMPERATURE):
            preset[Arg.COLOR_TEMP_KELVIN] = int(temperature)
        elif COLOR in data:
            preset[Arg.RGB_COLOR] = tuple(data[COLOR])
        preset[Arg.TRANSITION] = data.get(TRANSITION_MS, 0) / 1000
        effects = data.get(EFFECTS, {})
        return cls(
            frozenset(data[LIGHTS]),
            preset,
            frozenset(data.get(SOURCES, ())),
            data[INCLUDE_UI],
            effects if data.get(CONFIGURE_EFFECTS, bool(effects)) else {},
        )

    def preset_for(self, state: State | None, entity_id: str) -> dict[str, Any]:
        """Send a device effect only while the device still advertises it."""
        effect = self.effects.get(entity_id)
        if effect is None or effect not in (available_effects(state) or ()):
            return self.preset

        # A static color sent together with an effect can cancel the effect.
        return {
            key: value for key, value in self.preset.items() if key not in COLOR_PARAMS
        } | {Arg.EFFECT: effect}


@dataclass(frozen=True, slots=True)
class Profile:
    priority: int
    lights: dict[str, Group]


class LightProfiles:
    """Resolve active profiles and preserve one baseline per light."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._profiles: dict[str, Profile] = {}
        self._enabled: set[str] = set()
        self._lights: dict[str, Group] = {}
        self._tracked: set[str] = set()
        self._sources = SourceTracker(hass)
        self._unsubscribe: Callable[[], None] | None = None
        self._untrack: Callable[[], None] | None = None
        self._store = Store[dict[str, Any]](hass, 1, DOMAIN)
        self._remove_interception: Callable[[], None] | None = None
        self._last_day: dict[str, dict[str, Any]] = {}
        self._baseline: dict[str, dict[str, Any]] = {}
        self._pending: dict[str, dict[str, Any]] = {}
        self._applied: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._only_if_on: ContextVar[str | None] = ContextVar(
            "light_profile_only_if_on", default=None
        )

    async def async_load(self) -> None:
        if data := await self._store.async_load():
            self._enabled = set(data["enabled"])
            self._last_day = data["last_day"]
            self._baseline = data["baseline"]
            self._pending = data["pending"]
            self._applied = data["applied"]

    @callback
    def _storage_data(self) -> dict[str, Any]:
        return {
            "enabled": sorted(self._enabled),
            "last_day": dict(self._last_day),
            "baseline": dict(self._baseline),
            "pending": dict(self._pending),
            "applied": dict(self._applied),
        }

    async def async_save(self) -> None:
        await self._store.async_save(self._storage_data())

    @callback
    def is_enabled(self, entry_id: str) -> bool:
        return entry_id in self._enabled

    async def async_set_profile(
        self, entry_id: str, priority: int, subentries: Mapping[str, ConfigSubentry]
    ) -> None:
        groups = [
            Group.from_config(entry.data)
            for entry in subentries.values()
            if entry.subentry_type == GROUP
        ]
        profile = Profile(
            priority,
            {entity_id: group for group in groups for entity_id in group.lights},
        )
        async with self._lock:
            previous = self._profiles.get(entry_id)
            if profile == previous:
                return
            self._profiles[entry_id] = profile
            try:
                await self._reconcile(apply=self.hass.is_running)
            except HomeAssistantError:
                if previous is None:
                    self._profiles.pop(entry_id)
                else:
                    self._profiles[entry_id] = previous
                raise

    async def async_unload_profile(self, entry_id: str) -> None:
        async with self._lock:
            self._profiles.pop(entry_id, None)
            await self._reconcile()

    async def async_forget_profile(self, entry_id: str) -> None:
        async with self._lock:
            self._enabled.discard(entry_id)
            if not self.hass.config_entries.async_entries(DOMAIN):
                self._last_day.clear()
                self._baseline.clear()
                self._pending.clear()
                self._applied.clear()
                await self._store.async_remove()
            else:
                await self.async_save()

    async def async_set_enabled(self, entry_id: str, enabled: bool) -> None:
        async with self._lock:
            if self.is_enabled(entry_id) == enabled:
                return
            if enabled:
                self._enabled.add(entry_id)
            else:
                self._enabled.discard(entry_id)
            try:
                await self._reconcile()
            except HomeAssistantError:
                if enabled:
                    self._enabled.discard(entry_id)
                else:
                    self._enabled.add(entry_id)
                raise

    async def _reconcile(self, *, apply: bool = True) -> None:
        active = sorted(
            (
                profile
                for key, profile in self._profiles.items()
                if key in self._enabled
            ),
            key=lambda profile: profile.priority,
        )
        next_lights = {
            entity_id: group
            for profile in active
            for entity_id, group in profile.lights.items()
        }
        if self._profiles and (next_lights or self._pending):
            try:
                self._ensure_interception()
            except RuntimeError as err:
                raise HomeAssistantError(str(err)) from err

        previous = self._lights
        for entity_id in next_lights.keys() - previous.keys():
            state = self.hass.states.get(entity_id)
            current = reported_settings(state)
            saved = self._pending.pop(entity_id, None) or self._baseline.get(entity_id)
            if not saved:
                if state is not None and state.state == STATE_ON:
                    saved = current or self._last_day.get(entity_id)
                else:
                    saved = self._last_day.get(entity_id) or current
            if saved:
                self._baseline[entity_id] = saved

        removed = previous.keys() - next_lights.keys()
        for entity_id in removed:
            saved = self._baseline.pop(entity_id, None)
            if self._applied.pop(entity_id, None) is not None and saved:
                self._pending[entity_id] = saved

        self._lights = next_lights
        self._update_tracking()
        self._update_listener()
        self._remove_interception_if_idle()
        await self.async_save()
        if apply:
            targets = {
                entity_id: self._with_handoff(
                    entity_id,
                    group.preset_for(self.hass.states.get(entity_id), entity_id),
                )
                for entity_id, group in next_lights.items()
                if group != previous.get(entity_id)
            }
            targets.update(
                (entity_id, self._pending[entity_id])
                for entity_id in removed
                if entity_id in self._pending
            )
            if targets:
                await self._update_lit(targets)
                await self.async_save()
        if not self._profiles:
            self._detach_interception()

    @callback
    def _ensure_interception(self) -> None:
        if self._remove_interception is None:
            self._remove_interception = self._install_interception()

    @callback
    def _remove_interception_if_idle(self) -> None:
        if not self._lights and not self._pending:
            self._detach_interception()

    @callback
    def _detach_interception(self) -> None:
        if self._remove_interception is not None:
            self._remove_interception()
            self._remove_interception = None

    async def _update_lit(self, targets: Mapping[str, dict[str, Any]]) -> None:
        context = Context()
        token = self._only_if_on.set(context.id)
        try:
            actions = [
                self.hass.services.async_call(
                    LIGHT_DOMAIN,
                    SERVICE_TURN_ON,
                    {"entity_id": entity_id, **settings},
                    blocking=True,
                    context=context,
                )
                for entity_id, settings in targets.items()
                if self.hass.states.is_state(entity_id, STATE_ON)
            ]
            for result in await asyncio.gather(*actions, return_exceptions=True):
                if isinstance(result, Exception):
                    _LOGGER.warning(
                        "Could not update a light during the Light Profiles mode change: %s",
                        result,
                    )
        finally:
            self._only_if_on.reset(token)

    @callback
    def _remember_day(self, entity_id: str) -> None:
        state = self.hass.states.get(entity_id)
        if state is None or (state.state != STATE_ON and entity_id in self._last_day):
            return
        settings = reported_settings(state)
        if settings and self._last_day.get(entity_id) != settings:
            self._last_day[entity_id] = settings
            self._store.async_delay_save(self._storage_data, 5)

    @callback
    def _observe_light(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        if entity_id not in self._lights and entity_id not in self._pending:
            self._remember_day(entity_id)

    @callback
    def _update_tracking(self) -> None:
        tracked = {
            entity_id
            for profile in self._profiles.values()
            for entity_id in profile.lights
        }
        if tracked == self._tracked:
            return
        if self._untrack is not None:
            self._untrack()
        self._untrack = async_track_state_change_event(
            self.hass, tracked, self._observe_light
        )
        for entity_id in tracked - self._tracked:
            if entity_id not in self._lights and entity_id not in self._pending:
                self._remember_day(entity_id)
        self._tracked = tracked

    @callback
    def _update_listener(self) -> None:
        if self._lights and self._unsubscribe is None:
            self._unsubscribe = self.hass.bus.async_listen(
                MATCH_ALL, self._sources.observe
            )
        elif not self._lights and self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
            self._sources.clear()

    @callback
    def _mark_applied(self, entity_id: str, preset: Mapping[str, Any]) -> None:
        appearance = ""
        if Arg.EFFECT in preset:
            appearance = Arg.EFFECT
        elif COLOR_PARAMS.intersection(preset):
            appearance = "color"
        if self._applied.get(entity_id) != appearance:
            self._applied[entity_id] = appearance
            self._store.async_delay_save(self._storage_data, 5)

    def _with_handoff(self, entity_id: str, preset: dict[str, Any]) -> dict[str, Any]:
        """Clear the previous profile's appearance when the winner changes it."""
        previous = self._applied.get(entity_id)
        if not previous or Arg.EFFECT in preset:
            return preset
        effects = available_effects(self.hass.states.get(entity_id)) or ()
        if not COLOR_PARAMS.intersection(preset):
            baseline = {
                key: value
                for key, value in self._baseline.get(entity_id, {}).items()
                if key in _APPEARANCE and (key != Arg.EFFECT or value in effects)
            }
            preset = restore_missing(preset, baseline)
        if (
            previous == Arg.EFFECT
            and Arg.EFFECT not in preset
            and (off := effect_off(effects))
        ):
            preset = preset | {Arg.EFFECT: off}
        return preset

    async def _apply_preset(
        self, original: HassJob, light: LightEntity, call: ServiceCall
    ) -> Any:
        entity_id = light.entity_id
        editing_lit = self._only_if_on.get() == call.context.id
        saved = None
        if editing_lit:
            if not light.is_on:
                return None
            if group := self._lights.get(entity_id):
                self._mark_applied(
                    entity_id,
                    group.preset_for(self.hass.states.get(entity_id), entity_id),
                )
            else:
                saved = self._pending.get(entity_id)
        elif (
            (entity_id in self._lights or entity_id in self._pending)
            and not light.is_on
            and isinstance(params := call.data.get("params"), dict)
            and not _requests_off(params)
        ):
            if entity_id in self._lights:
                # Finish dispatching the source event if the action began eagerly.
                await asyncio.sleep(0)
                if not light.is_on and (group := self._lights.get(entity_id)):
                    browser = self._sources.matches(call.context, _BROWSER)
                    if (browser and group.include_ui) or (
                        not browser
                        and (
                            not group.sources
                            or self._sources.matches(call.context, group.sources)
                        )
                    ):
                        preset = group.preset_for(
                            self.hass.states.get(entity_id), entity_id
                        )
                        settings = (
                            preset
                            if _APPEARANCE.intersection(params)
                            and not _APPEARANCE.intersection(preset)
                            else self._with_handoff(entity_id, preset)
                        )
                        self._mark_applied(entity_id, preset)
                        call = _with_preset(call, params, settings)
            if (
                entity_id not in self._lights
                and not light.is_on
                and (saved := self._pending.get(entity_id))
            ):
                call = _with_params(call, restore_missing(params, saved))

        previous = self.hass.states.get(entity_id) if saved is not None else None
        task = self.hass.async_run_hass_job(original, light, call)
        result = await task if task is not None else None
        if saved is not None and self._pending.get(entity_id) is saved:
            self._pending.pop(entity_id)
            self._remove_interception_if_idle()
            self._last_day[entity_id] = saved
            if self.hass.states.get(entity_id) is not previous:
                self._remember_day(entity_id)
            self._store.async_delay_save(self._storage_data, 5)
        return result

    @callback
    def _install_interception(self) -> Callable[[], None]:
        """Wrap the per-light job while keeping Core's target and access checks."""
        registered = self.hass.services.async_services_for_domain(LIGHT_DOMAIN)
        replacements = []
        for action in (SERVICE_TURN_ON, SERVICE_TOGGLE):
            service = registered.get(action)
            if service is None:
                raise RuntimeError(f"Missing Home Assistant light.{action} action")

            old_job = service.job
            dispatch = old_job.target
            # Core registers entity actions as a partial over its dispatcher.
            # Check the shape before modifying either action.
            if (
                not isinstance(dispatch, partial)
                or dispatch.func is not entity_service_call
                or len(dispatch.args) != 3
                or not isinstance(dispatch.args[2], HassJob)
            ):
                raise RuntimeError(f"Unsupported handler for light.{action}")
            hass, entities, entity_job = dispatch.args
            patched = partial(
                dispatch.func,
                hass,
                entities,
                HassJob(partial(self._apply_preset, entity_job)),
                **(dispatch.keywords or {}),
            )
            replacements.append((service, old_job, HassJob(patched, old_job.name)))

        for service, _, new_job in replacements:
            service.job = new_job

        @callback
        def remove() -> None:
            for service, old_job, new_job in replacements:
                if service.job is new_job:
                    service.job = old_job

        return remove


def _requests_off(params: Mapping[str, Any]) -> bool:
    """Preserve light parameters that mean off even in a turn-on action."""
    return (
        params.get(Arg.BRIGHTNESS) == 0
        or params.get(Arg.WHITE) == 0
        or params.get(Arg.BRIGHTNESS_STEP, 1) <= 0
        or params.get(Arg.BRIGHTNESS_STEP_PCT, 1) <= 0
    )


def _with_preset(
    call: ServiceCall, params: dict[str, Any], preset: dict[str, Any]
) -> ServiceCall:
    new_params = dict(params)
    for key in (Arg.BRIGHTNESS_PCT, Arg.BRIGHTNESS_STEP, Arg.BRIGHTNESS_STEP_PCT):
        new_params.pop(key, None)
    if Arg.EFFECT in preset or COLOR_PARAMS.intersection(preset):
        for key in COLOR_PARAMS:
            new_params.pop(key, None)
        new_params.pop(Arg.EFFECT, None)
    elif Arg.WHITE in new_params:
        # The white argument also carries a brightness value.
        new_params[Arg.WHITE] = preset[Arg.BRIGHTNESS]
        preset = {key: value for key, value in preset.items() if key != Arg.BRIGHTNESS}

    new_params.update(preset)
    return _with_params(call, new_params)


def _with_params(call: ServiceCall, params: dict[str, Any]) -> ServiceCall:
    return ServiceCall(
        call.hass,
        call.domain,
        call.service,
        dict(call.data, params=params),
        call.context,
        call.return_response,
    )
