"""Exercise the config flows and native light dispatch against Home Assistant Core."""

import asyncio
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import probatio as vol
import pytest
import pytest_asyncio
from aiohttp.test_utils import make_mocked_request
from homeassistant import bootstrap, config_entries, data_entry_flow, loader
from homeassistant.components.conversation import async_converse
from homeassistant.components.frontend.storage import async_user_store
from homeassistant.components.http import KEY_HASS_USER
from homeassistant.components.light import ColorMode, LightEntity, LightEntityFeature
from homeassistant.components.light.const import DATA_COMPONENT
from homeassistant.components.websocket_api.commands import handle_call_service
from homeassistant.components.websocket_api.connection import current_connection
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import Context, HomeAssistant, State
from homeassistant.helpers import (
    area_registry,
    device_registry,
    entity_registry,
    label_registry,
)
from homeassistant.helpers.http import current_request
from homeassistant.helpers.translation import async_get_translations

from custom_components.light_profile.const import SWITCH_CODE, group_switch_id
from custom_components.light_profile.daylight import (
    available_effects,
    reported_settings,
)

DOMAIN = "light_profile"
GROUP = "light_group"
MIRROR = "light.mirror"
SHOWER = "light.shower"
HALL = "light.hall"


class TestLight(LightEntity):
    """A dimmable light that records the commands actually dispatched."""

    __test__ = False
    _attr_should_poll = False
    _attr_is_on = False
    _attr_supported_color_modes = {ColorMode.COLOR_TEMP, ColorMode.RGB}
    _attr_supported_features = LightEntityFeature.TRANSITION
    _attr_color_mode = ColorMode.RGB
    _attr_rgb_color = (255, 255, 255)

    def __init__(self, entity_id: str) -> None:
        self.entity_id = entity_id
        self._attr_name = entity_id.split(".", 1)[1].title()
        self._attr_unique_id = entity_id
        self.calls = []
        self.off_calls = []
        self.called = asyncio.Event()

    async def async_turn_on(self, **kwargs):
        self.calls.append(kwargs)
        self._attr_is_on = True
        if "brightness" in kwargs:
            self._attr_brightness = kwargs["brightness"]
        if "rgb_color" in kwargs:
            self._attr_color_mode = ColorMode.RGB
            self._attr_rgb_color = kwargs["rgb_color"]
        elif "color_temp_kelvin" in kwargs:
            self._attr_color_mode = ColorMode.COLOR_TEMP
            self._attr_color_temp_kelvin = kwargs["color_temp_kelvin"]
        if "effect" in kwargs:
            self._attr_effect = kwargs["effect"]
        self.async_write_ha_state()
        self.called.set()

    async def async_turn_off(self, **kwargs):
        self.off_calls.append(kwargs)
        self._attr_is_on = False
        self.async_write_ha_state()


@pytest_asyncio.fixture
async def ha(tmp_path: Path, monkeypatch):
    """Start a local Home Assistant and register three test lights."""
    integration = Path(__file__).parents[1] / "custom_components" / DOMAIN
    shutil.copytree(integration, tmp_path / "custom_components" / DOMAIN)
    hass = HomeAssistant(str(tmp_path))
    hass.config.skip_pip = True
    loader.async_setup(hass)
    monkeypatch.setattr(
        "homeassistant.components.network.network.async_load_adapters",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        "homeassistant.components.network.util.async_get_source_ip",
        lambda target: "127.0.0.1",
    )
    for method in ("async_bind", "start"):
        monkeypatch.setattr(
            f"homeassistant.components.http.server.HomeAssistantHTTP.{method}",
            AsyncMock(),
        )
    config = {
        "light": {},
        "conversation": {},
        "script": {
            "outer": {
                "sequence": [
                    {
                        "action": "script.turn_on",
                        "target": {"entity_id": "script.inner"},
                    },
                ],
            },
            "inner": {
                "sequence": [
                    {
                        "action": "light.turn_on",
                        "target": {"entity_id": SHOWER},
                        "data": {"brightness": 200},
                    },
                ],
            },
        },
        "automation": [
            {
                "id": "button",
                "alias": "Bathroom button",
                "triggers": [
                    {"trigger": "event", "event_type": "light_profile_button"}
                ],
                "actions": [{"event": "light_profile_relay"}],
            },
            {
                "id": "relay",
                "alias": "Bathroom relay",
                "triggers": [{"trigger": "event", "event_type": "light_profile_relay"}],
                "actions": [
                    {
                        "action": "light.turn_on",
                        "target": {"entity_id": [MIRROR, HALL]},
                        "data": {"brightness_pct": 100, "rgb_color": [0, 0, 255]},
                    }
                ],
            },
            {
                "id": "script_test",
                "alias": "Bathroom script test",
                "triggers": [
                    {"trigger": "event", "event_type": "light_profile_script"}
                ],
                "actions": [
                    {"action": "script.outer"},
                    {
                        "action": "light.turn_on",
                        "target": {"entity_id": HALL},
                        "data": {"brightness": 200},
                    },
                ],
            },
            {
                "id": "motion",
                "alias": "Bathroom motion",
                "triggers": [
                    {
                        "trigger": "state",
                        "entity_id": "binary_sensor.motion",
                        "to": "on",
                    }
                ],
                "actions": [
                    {
                        "action": "light.turn_on",
                        "target": {"entity_id": SHOWER},
                        "data": {"brightness": 200},
                    },
                ],
            },
        ],
    }
    assert await bootstrap.async_from_config_dict(config, hass) is hass
    lights = {entity_id: TestLight(entity_id) for entity_id in (MIRROR, SHOWER, HALL)}
    await hass.data[DATA_COMPONENT].async_add_entities(lights.values())
    hass.states.async_set("binary_sensor.motion", "off")
    await hass.async_start()
    await hass.async_block_till_done()
    try:
        yield hass, lights
    finally:
        await hass.async_stop()


async def install(hass, title="Light Profile", priority=10):
    flow = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"name": title, "priority": priority}
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    entry = result["result"]
    assert entry.state == config_entries.ConfigEntryState.LOADED
    return entry


def profile_switch(hass, entry):
    return entity_registry.async_get(hass).async_get_entity_id(
        "switch", DOMAIN, entry.entry_id
    )


async def configure_group(hass, flow_id, data):
    if isinstance(data.get("lights"), list):
        data = {**data, "lights": {"entity_id": data["lights"]}}
    return await hass.config_entries.subentries.async_configure(flow_id, data)


async def add_group(hass, entry, data):
    flow = await hass.config_entries.subentries.async_init(
        (entry.entry_id, GROUP), context={"source": "user"}
    )
    result = await configure_group(hass, flow["flow_id"], data)
    await hass.async_block_till_done()
    return result


async def action(hass, name, target, data=None, *, domain="light"):
    await hass.services.async_call(domain, name, data, target=target, blocking=True)
    await hass.async_block_till_done()


@pytest.mark.asyncio
async def test_sources_match_through_real_automations(ha):
    hass, lights = ha
    entry = await install(hass)
    device = device_registry.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "bathroom_button")},
        name="Bathroom button",
    )
    result = await add_group(
        hass,
        entry,
        {
            "name": "Bathroom",
            "lights": [MIRROR, SHOWER],
            "brightness": 8,
            "temperature": 2200,
            "transition_ms": 0,
            "sources": [f"device:{device.id}"],
            "include_ui": True,
        },
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    await action(
        hass, "turn_on", {"entity_id": profile_switch(hass, entry)}, domain="switch"
    )

    for event_data in (
        {"device_id": "different_button", "command": "on"},
        {"device_id": device.id, "command": "hold"},
        {"device_id": device.id, "command": "on"},
    ):
        hass.bus.async_fire("light_profile_button", event_data)
        await asyncio.wait_for(lights[MIRROR].called.wait(), 2)
        await asyncio.wait_for(lights[HALL].called.wait(), 2)
        lights[MIRROR].called.clear()
        lights[HALL].called.clear()
        await hass.async_block_till_done()
        expected = 20 if event_data["device_id"] == device.id else 255
        assert lights[MIRROR].calls[-1]["brightness"] == expected
        assert lights[HALL].calls[-1]["brightness"] == 255
        assert not lights[SHOWER].calls
        await action(hass, "turn_off", {"entity_id": [MIRROR, HALL]})
    assert len(lights[MIRROR].calls) == 3
    assert lights[MIRROR].calls[-1] == {
        "brightness": 20,
        "color_temp_kelvin": 2200,
        "transition": 0,
    }

    hass.states.async_set("binary_sensor.motion", "on")
    await hass.async_block_till_done()
    assert lights[SHOWER].calls[-1]["brightness"] == 200

    user = await hass.auth.async_create_user("Dashboard user")
    await websocket_light(hass, user, MIRROR, origin=None)
    assert lights[MIRROR].calls[-1]["brightness"] == 180
    await action(hass, "turn_off", {"entity_id": MIRROR})
    await websocket_light(hass, user, MIRROR, origin="http://homeassistant.local:8123")
    assert lights[MIRROR].calls[-1]["brightness"] == 20


@pytest.mark.asyncio
async def test_mixed_area_toggle_edit_and_remove(ha):
    hass, lights = ha
    native_jobs = {
        name: service.job
        for name, service in hass.services.async_services_for_domain("light").items()
    }
    entry = await install(hass)
    result = await add_group(
        hass, entry, {"name": "Mirror", "lights": [MIRROR], "brightness": 5}
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    result = await add_group(
        hass, entry, {"name": "Hall", "lights": [HALL], "brightness": 20}
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    duplicate = await add_group(hass, entry, {"name": "Duplicate", "lights": [MIRROR]})
    assert duplicate["errors"]["lights"] == "light_already_grouped"

    area = area_registry.async_get(hass).async_create("Test area")
    registry = entity_registry.async_get(hass)
    for entity_id in lights:
        registry.async_update_entity(entity_id, area_id=area.id)
    await hass.async_block_till_done()

    await action(hass, "turn_on", {"entity_id": MIRROR}, {"brightness": 180})
    assert lights[MIRROR].calls[-1]["brightness"] == 180
    await action(hass, "turn_off", {"entity_id": MIRROR})

    await action(
        hass, "turn_on", {"entity_id": profile_switch(hass, entry)}, domain="switch"
    )
    await action(hass, "toggle", {"area_id": area.id}, {"brightness": 180})
    assert lights[MIRROR].calls[-1]["brightness"] == 13
    assert lights[HALL].calls[-1]["brightness"] == 51
    assert lights[SHOWER].calls[-1]["brightness"] == 180
    await action(hass, "toggle", {"area_id": area.id})
    assert all(hass.states.is_state(entity_id, STATE_OFF) for entity_id in lights)
    await action(hass, "turn_on", {"entity_id": MIRROR}, {"brightness_pct": 0})
    assert len(lights[MIRROR].calls) == 2

    mirror = next(
        subentry for subentry in entry.subentries.values() if subentry.title == "Mirror"
    )
    flow = await hass.config_entries.subentries.async_init(
        (entry.entry_id, GROUP),
        context={"source": "reconfigure", "subentry_id": mirror.subentry_id},
    )
    result = await configure_group(
        hass,
        flow["flow_id"],
        {"name": "Bathroom", "lights": [MIRROR, SHOWER], "brightness": 12},
    )
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert hass.states.is_state(profile_switch(hass, entry), STATE_ON)
    await action(hass, "turn_on", {"entity_id": SHOWER}, {"brightness": 180})
    assert lights[SHOWER].calls[-1]["brightness"] == 31
    await action(hass, "turn_on", {"entity_id": SHOWER}, {"brightness": 200})
    assert lights[SHOWER].calls[-1]["brightness"] == 200

    hass.config_entries.async_remove_subentry(entry, mirror.subentry_id)
    await hass.async_block_till_done()
    await action(hass, "turn_on", {"entity_id": MIRROR}, {"brightness": 180})
    assert lights[MIRROR].calls[-1]["brightness"] == 180

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.is_state(profile_switch(hass, entry), STATE_ON)
    await action(hass, "turn_on", {"entity_id": HALL}, domain="homeassistant")
    assert lights[HALL].calls[-1]["brightness"] == 51

    assert await hass.config_entries.async_unload(entry.entry_id)
    for name, service in hass.services.async_services_for_domain("light").items():
        assert service.job is native_jobs[name]


async def websocket_light(hass, user, entity_id, *, origin):
    """Run the real WebSocket service handler in its connection context."""
    done = asyncio.Event()
    connection = SimpleNamespace(
        hass=hass,
        user=user,
        context=lambda msg: Context(user_id=user.id),
        send_result=Mock(side_effect=lambda *_: done.set()),
    )
    headers = {"Origin": origin} if origin else {}
    request_token = current_request.set(
        make_mocked_request("GET", "/api/websocket", headers=headers)
    )
    connection_token = current_connection.set(connection)
    try:
        handle_call_service(
            hass,
            connection,
            {
                "id": 1,
                "domain": "light",
                "service": "turn_on",
                "service_data": {"brightness": 180},
                "target": {"entity_id": entity_id},
                "return_response": False,
            },
        )
        await asyncio.wait_for(done.wait(), 2)
        connection.send_result.assert_called_once()
    finally:
        current_connection.reset(connection_token)
        current_request.reset(request_token)


@pytest.mark.asyncio
async def test_script_and_automation_sources_match_only_their_branch(ha):
    hass, lights = ha
    entry = await install(hass)
    data = {"name": "Bathroom", "lights": [SHOWER, HALL], "brightness": 10}
    result = await add_group(hass, entry, {**data, "sources": ["entity:script.outer"]})
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    await action(
        hass, "turn_on", {"entity_id": profile_switch(hass, entry)}, domain="switch"
    )
    hass.bus.async_fire("light_profile_script")
    await hass.async_block_till_done()
    assert lights[SHOWER].calls[-1]["brightness"] == 26
    assert lights[HALL].calls[-1]["brightness"] == 200

    await action(hass, "turn_off", {"entity_id": [SHOWER, HALL]})
    await hass.services.async_call("script", "outer", blocking=True)
    await hass.async_block_till_done()
    assert lights[SHOWER].calls[-1]["brightness"] == 26
    assert hass.states.is_state(HALL, STATE_OFF)

    await action(hass, "turn_off", {"entity_id": SHOWER})
    subentry = next(iter(entry.subentries.values()))
    flow = await hass.config_entries.subentries.async_init(
        (entry.entry_id, GROUP),
        context={"source": "reconfigure", "subentry_id": subentry.subentry_id},
    )
    result = await configure_group(
        hass,
        flow["flow_id"],
        {
            **data,
            "sources": [
                "entity:automation.bathroom_script_test",
                "entity:binary_sensor.motion",
            ],
        },
    )
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    await action(
        hass,
        "trigger",
        {"entity_id": "automation.bathroom_script_test"},
        domain="automation",
    )
    assert lights[SHOWER].calls[-1]["brightness"] == 26
    assert lights[HALL].calls[-1]["brightness"] == 26

    await action(hass, "turn_off", {"entity_id": SHOWER})
    lights[SHOWER].called.clear()
    hass.states.async_set("binary_sensor.motion", "on")
    await asyncio.wait_for(lights[SHOWER].called.wait(), 2)
    assert lights[SHOWER].calls[-1]["brightness"] == 26


@pytest.mark.asyncio
async def test_daylight_is_restored_without_turning_off_lights_on(ha):
    hass, lights = ha
    services = hass.services.async_services_for_domain("light")
    names = ("turn_on", "turn_off", "toggle")
    original = tuple(services[name].job for name in names)

    def current_jobs():
        return tuple(services[name].job for name in names)

    entry = await install(hass)
    assert current_jobs() == original
    result = await add_group(
        hass,
        entry,
        {
            "name": "Night",
            "lights": [MIRROR, SHOWER, HALL],
            "brightness": 10,
            "temperature": 2200,
            "transition_ms": 350,
        },
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert current_jobs() == original
    await action(
        hass,
        "turn_on",
        {"entity_id": MIRROR},
        {"brightness": 190, "rgb_color": [90, 20, 30]},
    )
    lights[MIRROR]._attr_brightness = 175
    lights[MIRROR].async_write_ha_state()
    await action(
        hass,
        "turn_on",
        {"entity_id": SHOWER},
        {"brightness": 140, "rgb_color": [40, 80, 90]},
    )
    await action(hass, "turn_off", {"entity_id": SHOWER})

    await action(
        hass, "turn_on", {"entity_id": profile_switch(hass, entry)}, domain="switch"
    )
    assert current_jobs() != original
    assert lights[MIRROR].calls[-1] == {
        "brightness": 26,
        "color_temp_kelvin": 2200,
        "transition": 0.35,
    }
    assert hass.states.is_state(SHOWER, STATE_OFF)
    assert not lights[HALL].calls
    await action(hass, "turn_on", {"entity_id": SHOWER})
    assert lights[SHOWER].calls[-1]["brightness"] == 26
    await action(hass, "turn_off", {"entity_id": SHOWER})

    await action(
        hass, "turn_off", {"entity_id": profile_switch(hass, entry)}, domain="switch"
    )
    assert lights[MIRROR].calls[-1] == {"brightness": 175, "rgb_color": (90, 20, 30)}
    assert hass.states.is_state(MIRROR, STATE_ON)
    assert hass.states.is_state(SHOWER, STATE_OFF)
    assert len(lights[SHOWER].calls) == 2
    assert not lights[HALL].calls
    assert current_jobs() != original

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.is_state(profile_switch(hass, entry), STATE_OFF)
    assert current_jobs() != original
    await action(hass, "turn_on", {"entity_id": SHOWER}, {"brightness": 99})
    assert lights[SHOWER].calls[-1] == {"brightness": 99, "rgb_color": (40, 80, 90)}
    assert current_jobs() == original
    await action(hass, "turn_off", {"entity_id": SHOWER})
    await action(hass, "turn_on", {"entity_id": SHOWER})
    assert lights[SHOWER].calls[-1] == {}


@pytest.mark.asyncio
async def test_interception_stops_when_no_active_profile_or_restore_remains(ha):
    hass, lights = ha
    service = hass.services.async_services_for_domain("light")["turn_on"]
    original = service.job
    entry = await install(hass)
    result = await add_group(
        hass, entry, {"name": "Room", "lights": [HALL], "brightness": 10}
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert service.job is original

    await action(
        hass, "turn_on", {"entity_id": profile_switch(hass, entry)}, domain="switch"
    )
    assert service.job is not original
    await action(hass, "turn_on", {"entity_id": HALL})
    assert lights[HALL].calls[-1]["brightness"] == 26
    await action(hass, "turn_off", {"entity_id": HALL})
    await action(
        hass, "turn_off", {"entity_id": profile_switch(hass, entry)}, domain="switch"
    )
    assert service.job is original
    assert hass.states.is_state(HALL, STATE_OFF)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert service.job is original


@pytest.mark.asyncio
async def test_russian_form_labels_and_independent_interface_option(ha):
    hass, lights = ha
    entry = await install(hass)
    labels = await async_get_translations(hass, "ru", "config_subentries", [DOMAIN])
    form = await hass.config_entries.subentries.async_init(
        (entry.entry_id, GROUP), context={"source": "user"}
    )
    schema = form["data_schema"].schema
    marker = next(field for field in schema if field == "color")
    assert marker.description["suggested_value"] == [255, 220, 180]
    assert isinstance(
        next(field for field in schema if field == "brightness"), vol.Optional
    )
    prefix = "component.light_profile.config_subentries.light_group.step.user"
    assert labels[f"{prefix}.data.sources"] == "Фильтр по источнику включения"
    assert "интерфейса" in labels[f"{prefix}.data.include_ui"]
    assert labels[f"{prefix}.title"] == "Настройка группы освещения"
    assert f"{prefix}.description" not in labels
    assert labels[f"{prefix}.data.temperature"] == "Цветовая температура (K)"
    assert labels[f"{prefix}.data.transition_ms"] == "Длительность перехода (мсек)"
    assert all(isinstance(field, vol.Optional) for field in schema)
    missing = await configure_group(hass, form["flow_id"], {})
    assert missing["errors"] == {"name": "empty_name", "lights": "select_lights"}

    data = {"name": "Room", "lights": [HALL], "brightness": 10, "include_ui": False}
    assert (await add_group(hass, entry, data))[
        "type"
    ] == data_entry_flow.FlowResultType.CREATE_ENTRY
    await action(
        hass, "turn_on", {"entity_id": profile_switch(hass, entry)}, domain="switch"
    )
    user = await hass.auth.async_create_user("Dashboard user")
    await websocket_light(hass, user, HALL, origin="http://homeassistant.local:8123")
    assert lights[HALL].calls[-1]["brightness"] == 180
    await action(hass, "turn_off", {"entity_id": HALL})
    await action(hass, "turn_on", {"entity_id": HALL}, {"transition": 2})
    assert lights[HALL].calls[-1]["brightness"] == 26
    assert lights[HALL].calls[-1]["transition"] == 0


@pytest.mark.asyncio
async def test_per_light_effects_and_daylight_restoration(ha):
    hass, lights = ha
    entry = await install(hass)
    for name, effects in ((MIRROR, ["off", "Rainbow"]), (SHOWER, ["Calm", "Stars"])):
        light = lights[name]
        light._attr_supported_features |= LightEntityFeature.EFFECT
        light._attr_effect_list = effects
        light.async_write_ha_state()

    config = {
        "name": "Bath",
        "lights": [MIRROR, SHOWER, HALL],
        "brightness": 10,
        "temperature": 2200,
        "color": [255, 220, 180],
        "transition_ms": 300,
    }
    step = await add_group(hass, entry, config | {"configure_effects": True})
    assert step["step_id"] == "effects"
    fields = step["data_schema"].schema
    assert list(fields) == ["Mirror", "Shower"]
    for name, expected in (
        ("Mirror", ["", "off", "Rainbow"]),
        ("Shower", ["", "Calm", "Stars"]),
    ):
        select = fields[name].serialize()["selector"]["select"]
        assert not select["multiple"]
        assert [option["value"] for option in select["options"]] == expected
    result = await configure_group(
        hass, step["flow_id"], {"Mirror": "Rainbow", "Shower": "Stars"}
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    group = next(iter(entry.subentries.values()))
    assert group.data["effects"] == {MIRROR: "Rainbow", SHOWER: "Stars"}
    assert group.data["configure_effects"] is True
    await hass.async_block_till_done()

    await action(hass, "turn_on", {"entity_id": MIRROR}, {"brightness": 130})
    await action(
        hass, "turn_on", {"entity_id": SHOWER}, {"effect": "Calm", "brightness": 150}
    )
    await action(hass, "turn_off", {"entity_id": SHOWER})
    await action(
        hass, "turn_on", {"entity_id": profile_switch(hass, entry)}, domain="switch"
    )
    assert lights[MIRROR].calls[-1] == {
        "brightness": 26,
        "effect": "Rainbow",
        "transition": 0.3,
    }
    await action(
        hass, "turn_on", {"entity_id": [SHOWER, HALL]}, {"rgb_color": [30, 40, 50]}
    )
    assert lights[SHOWER].calls[-1] == {
        "brightness": 26,
        "effect": "Stars",
        "transition": 0.3,
    }
    assert lights[HALL].calls[-1] == {
        "brightness": 26,
        "color_temp_kelvin": 2200,
        "transition": 0.3,
    }
    await action(hass, "turn_off", {"entity_id": SHOWER})
    await action(
        hass, "turn_off", {"entity_id": profile_switch(hass, entry)}, domain="switch"
    )
    assert lights[MIRROR].calls[-1]["effect"] == "off"
    await action(hass, "turn_on", {"entity_id": SHOWER})
    assert lights[SHOWER].calls[-1] == {"brightness": 150, "rgb_color": (30, 40, 50)}

    await action(hass, "turn_off", {"entity_id": MIRROR})
    lights[MIRROR]._attr_effect_list = ["off"]
    lights[MIRROR].async_write_ha_state()
    await action(
        hass, "turn_on", {"entity_id": profile_switch(hass, entry)}, domain="switch"
    )
    await action(hass, "turn_on", {"entity_id": MIRROR})
    assert lights[MIRROR].calls[-1] == {
        "brightness": 26,
        "color_temp_kelvin": 2200,
        "transition": 0.3,
    }

    context = {"source": "reconfigure", "subentry_id": group.subentry_id}
    step = await hass.config_entries.subentries.async_init(
        (entry.entry_id, GROUP), context=context
    )
    marker = next(
        field for field in step["data_schema"].schema if field == "configure_effects"
    )
    assert marker.description["suggested_value"] is True
    result = await configure_group(hass, step["flow_id"], config)
    assert result["reason"] == "reconfigure_successful"
    assert entry.subentries[group.subentry_id].data["effects"] == {
        MIRROR: "Rainbow",
        SHOWER: "Stars",
    }

    assert entry.subentries[group.subentry_id].data["configure_effects"] is False
    await hass.async_block_till_done()
    await action(hass, "turn_off", {"entity_id": SHOWER})
    await action(hass, "turn_on", {"entity_id": SHOWER})
    assert lights[SHOWER].calls[-1] == {
        "brightness": 26,
        "color_temp_kelvin": 2200,
        "transition": 0.3,
    }

    step = await hass.config_entries.subentries.async_init(
        (entry.entry_id, GROUP), context=context
    )
    marker = next(
        field for field in step["data_schema"].schema if field == "configure_effects"
    )
    assert marker.description["suggested_value"] is False
    step = await configure_group(
        hass, step["flow_id"], config | {"configure_effects": True, "temperature": 0}
    )
    assert step["step_id"] == "effects"
    marker = next(field for field in step["data_schema"].schema if field == "Shower")
    assert marker.description["suggested_value"] == "Stars"
    result = await configure_group(hass, step["flow_id"], {"Shower": ""})
    assert result["reason"] == "reconfigure_successful"
    assert entry.subentries[group.subentry_id].data["effects"] == {}
    await hass.async_block_till_done()
    await action(hass, "turn_off", {"entity_id": SHOWER})
    await action(hass, "turn_on", {"entity_id": SHOWER})
    assert lights[SHOWER].calls[-1] == {
        "brightness": 26,
        "rgb_color": (255, 220, 180),
        "transition": 0.3,
    }


@pytest.mark.asyncio
async def test_profile_priority_handoff_and_baseline(ha):
    hass, lights = ha
    low = await install(hass, title="Evening", priority=10)
    high = await install(hass, title="Night", priority=30)
    for entry, brightness in ((low, 40), (high, 10)):
        result = await add_group(
            hass,
            entry,
            {"name": "Shared", "lights": [MIRROR, SHOWER], "brightness": brightness},
        )
        assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY

    await action(hass, "turn_on", {"entity_id": [MIRROR, SHOWER]}, {"brightness": 185})
    await action(hass, "turn_off", {"entity_id": SHOWER})
    await action(
        hass, "turn_on", {"entity_id": profile_switch(hass, high)}, domain="switch"
    )
    assert lights[MIRROR].calls[-1]["brightness"] == 26
    assert not lights[SHOWER].is_on

    count = len(lights[MIRROR].calls)
    await action(
        hass, "turn_on", {"entity_id": profile_switch(hass, low)}, domain="switch"
    )
    assert len(lights[MIRROR].calls) == count
    assert hass.states.is_state(profile_switch(hass, low), STATE_ON)
    assert hass.states.is_state(profile_switch(hass, high), STATE_ON)
    await action(hass, "turn_on", {"entity_id": SHOWER})
    assert lights[SHOWER].calls[-1]["brightness"] == 26
    await action(hass, "turn_off", {"entity_id": SHOWER})

    await action(
        hass, "turn_off", {"entity_id": profile_switch(hass, high)}, domain="switch"
    )
    assert lights[MIRROR].calls[-1]["brightness"] == 102
    assert not lights[SHOWER].is_on
    await action(hass, "turn_on", {"entity_id": SHOWER})
    assert lights[SHOWER].calls[-1]["brightness"] == 102
    await action(hass, "turn_off", {"entity_id": SHOWER})

    await action(
        hass, "turn_off", {"entity_id": profile_switch(hass, low)}, domain="switch"
    )
    assert lights[MIRROR].calls[-1]["brightness"] == 185
    assert not lights[SHOWER].is_on
    await action(hass, "turn_on", {"entity_id": SHOWER})
    assert lights[SHOWER].calls[-1]["brightness"] == 185


@pytest.mark.asyncio
async def test_unique_priorities_and_reorder_active_profiles(ha):
    hass, lights = ha
    low = await install(hass, title="Evening", priority=10)
    high = await install(hass, title="Night", priority=30)

    step = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    duplicate = await hass.config_entries.flow.async_configure(
        step["flow_id"], {"name": "Duplicate", "priority": 10}
    )
    assert duplicate["errors"] == {"priority": "duplicate_priority"}
    assert len(hass.config_entries.async_entries(DOMAIN)) == 2

    for entry, brightness in ((low, 40), (high, 10)):
        result = await add_group(
            hass,
            entry,
            {"name": "Shared", "lights": [MIRROR], "brightness": brightness},
        )
        assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY

    await action(
        hass,
        "turn_on",
        {"entity_id": [profile_switch(hass, low), profile_switch(hass, high)]},
        domain="switch",
    )
    await action(hass, "turn_on", {"entity_id": MIRROR})
    assert lights[MIRROR].calls[-1]["brightness"] == 26

    assert low.supports_options
    assert not low.supports_reconfigure
    step = await hass.config_entries.options.async_init(low.entry_id)
    duplicate = await hass.config_entries.options.async_configure(
        step["flow_id"], {"name": "Evening", "priority": 30}
    )
    assert duplicate["errors"] == {"priority": "duplicate_priority"}
    assert low.data["priority"] == 10

    result = await hass.config_entries.options.async_configure(
        step["flow_id"], {"name": "Late Evening", "priority": 50}
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert low.data["priority"] == 50
    assert hass.states.get(profile_switch(hass, low)).name == "Late Evening"
    assert lights[MIRROR].calls[-1]["brightness"] == 102

    assert await hass.config_entries.async_unload(low.entry_id)
    assert lights[MIRROR].calls[-1]["brightness"] == 26
    assert await hass.config_entries.async_setup(low.entry_id)
    await hass.async_block_till_done()
    assert hass.states.is_state(profile_switch(hass, low), STATE_ON)
    assert lights[MIRROR].calls[-1]["brightness"] == 102


@pytest.mark.asyncio
async def test_only_the_winning_profiles_source_filter_is_used(ha):
    hass, lights = ha
    low = await install(hass, title="Evening", priority=10)
    high = await install(hass, title="Night", priority=30)
    base = {"name": "Shared", "lights": [MIRROR]}
    assert (await add_group(hass, low, base | {"brightness": 40}))[
        "type"
    ] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert (
        await add_group(
            hass,
            high,
            base | {"brightness": 10, "sources": ["entity:binary_sensor.motion"]},
        )
    )["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY

    await action(
        hass,
        "turn_on",
        {"entity_id": [profile_switch(hass, low), profile_switch(hass, high)]},
        domain="switch",
    )
    await action(hass, "turn_on", {"entity_id": MIRROR}, {"brightness": 180})
    assert lights[MIRROR].calls[-1]["brightness"] == 180
    await action(hass, "turn_off", {"entity_id": MIRROR})
    await action(
        hass, "turn_off", {"entity_id": profile_switch(hass, high)}, domain="switch"
    )
    await action(hass, "turn_on", {"entity_id": MIRROR}, {"brightness": 180})
    assert lights[MIRROR].calls[-1]["brightness"] == 102


@pytest.mark.asyncio
async def test_profile_handoff_replaces_previous_effect(ha):
    hass, lights = ha
    low = await install(hass, title="Evening", priority=10)
    high = await install(hass, title="Night", priority=30)
    light = lights[MIRROR]
    light._attr_supported_features |= LightEntityFeature.EFFECT
    light._attr_effect_list = ["off", "Rainbow"]
    light.async_write_ha_state()

    for entry, brightness, effect in ((low, 40, "Rainbow"), (high, 10, None)):
        result = await add_group(
            hass,
            entry,
            {
                "name": "Shared",
                "lights": [MIRROR],
                "brightness": brightness,
                "configure_effects": effect is not None,
            },
        )
        if effect is not None:
            assert result["step_id"] == "effects"
            result = await configure_group(hass, result["flow_id"], {"Mirror": effect})
        assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()

    await action(
        hass,
        "turn_on",
        {"entity_id": MIRROR},
        {"brightness": 185, "rgb_color": [20, 30, 40]},
    )
    await action(
        hass, "turn_on", {"entity_id": profile_switch(hass, low)}, domain="switch"
    )
    assert light.calls[-1] == {"brightness": 102, "effect": "Rainbow", "transition": 0}
    await action(hass, "turn_off", {"entity_id": MIRROR})
    await action(
        hass, "turn_on", {"entity_id": profile_switch(hass, high)}, domain="switch"
    )
    assert not light.is_on
    await action(hass, "turn_on", {"entity_id": MIRROR})
    assert light.calls[-1] == {
        "brightness": 26,
        "effect": "off",
        "rgb_color": (20, 30, 40),
        "transition": 0,
    }
    await action(
        hass, "turn_off", {"entity_id": profile_switch(hass, high)}, domain="switch"
    )
    assert light.calls[-1] == {"brightness": 102, "effect": "Rainbow", "transition": 0}


@pytest.mark.asyncio
async def test_effect_options_only_show_group_lights_with_translated_labels(ha):
    hass, _ = ha
    entry = await install(hass)
    registry = entity_registry.async_get(hass)
    hue = registry.async_get_or_create(
        "light",
        "hue",
        "fixture-mirror",
        suggested_object_id="hue_example",
        translation_key="hue_light",
    )
    hass.states.async_set(
        hue.entity_id,
        STATE_OFF,
        {"friendly_name": "Зеркало", "effect_list": ["candle", "off", "Experimental"]},
    )
    hass.states.async_set(
        "light.another_room",
        STATE_OFF,
        {"friendly_name": "Другая комната", "effect_list": ["candle"]},
    )
    user = await hass.auth.async_create_user("Dashboard user")
    store = await async_user_store(hass, user.id)
    await store.async_set_item("language", {"language": "ru"})
    request = make_mocked_request("POST", "/api/config/config_entries/subentries/flow")
    request[KEY_HASS_USER] = user
    token = current_request.set(request)
    try:
        step = await add_group(
            hass,
            entry,
            {
                "name": "Ванная",
                "lights": [hue.entity_id],
                "brightness": 20,
                "configure_effects": True,
            },
        )
    finally:
        current_request.reset(token)

    assert step["step_id"] == "effects"
    assert list(step["data_schema"].schema) == ["Зеркало"]
    schema = step["data_schema"].schema["Зеркало"].serialize()
    options = schema["selector"]["select"]["options"]
    assert {option["value"]: option["label"] for option in options} == {
        "": "—",
        "candle": "Свеча",
        "off": "Выключено",
        "Experimental": "Experimental",
    }
    result = await configure_group(hass, step["flow_id"], {"Зеркало": "candle"})
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert next(iter(entry.subentries.values())).data["effects"] == {
        hue.entity_id: "candle"
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("leave_light_on", "mesh_brightness", "restored"),
    [(True, 50000, 195), (False, 50000, 195), (True, 1, 1)],
)
async def test_restore_fractional_mesh_brightness(
    ha, leave_light_on, mesh_brightness, restored
):
    hass, lights = ha
    light = lights[MIRROR]
    entry = await install(hass)
    result = await add_group(
        hass,
        entry,
        {"name": "Night", "lights": [MIRROR], "brightness": 10, "temperature": 2200},
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY

    await action(
        hass,
        "turn_on",
        {"entity_id": MIRROR},
        {"brightness": 195, "color_temp_kelvin": 4000},
    )
    # Xiaomi Gateway 3 decodes this model's 16-bit MIoT brightness to a float.
    original = mesh_brightness / 65535 * 255.0
    light._attr_brightness = original
    light.async_write_ha_state()
    assert hass.states.get(MIRROR).attributes["brightness"] == original

    await action(
        hass, "turn_on", {"entity_id": profile_switch(hass, entry)}, domain="switch"
    )
    assert light.calls[-1]["brightness"] == 26
    if not leave_light_on:
        await action(hass, "turn_off", {"entity_id": MIRROR})

    await action(
        hass, "turn_off", {"entity_id": profile_switch(hass, entry)}, domain="switch"
    )
    if not leave_light_on:
        assert hass.states.is_state(MIRROR, STATE_OFF)
        await action(hass, "turn_on", {"entity_id": MIRROR})

    assert light.calls[-1]["brightness"] == restored
    assert light.calls[-1]["color_temp_kelvin"] == 4000


@pytest.mark.parametrize(
    ("attributes", "expected"),
    [
        ({"brightness": "195.5"}, {"brightness": 196}),
        ({"brightness": True, "color_mode": ["rgb"]}, {}),
        (
            {"brightness": 0, "color_mode": "rgb", "rgb_color": [0, 0, 0]},
            {"rgb_color": (0, 0, 0)},
        ),
        ({"brightness": "NaN", "color_mode": "white"}, {}),
        ({"brightness": 256, "color_mode": "white"}, {}),
        ({"brightness": "0.1", "color_mode": "white"}, {"white": 1}),
        (
            {"color_mode": "color_temp", "color_temp_kelvin": "3999.7"},
            {"color_temp_kelvin": 4000},
        ),
        ({"color_mode": "color_temp", "color_temp_kelvin": 0}, {}),
        (
            {"color_mode": "rgbw", "rgbw_color": [1, "2", 3.6, 255]},
            {"rgbw_color": (1, 2, 4, 255)},
        ),
        (
            {"color_mode": "rgbww", "rgbww_color": (1, 2, 3, 4, 5)},
            {"rgbww_color": (1, 2, 3, 4, 5)},
        ),
        ({"color_mode": "rgbww", "rgbww_color": (1, 2, 3, 4)}, {}),
        ({"color_mode": "hs", "hs_color": ["360", 10.5]}, {"hs_color": (360, 10.5)}),
        ({"color_mode": "hs", "hs_color": [361, 0]}, {}),
        ({"color_mode": "xy", "xy_color": (0.15, "0.44")}, {"xy_color": (0.15, 0.44)}),
        ({"color_mode": "xy", "xy_color": (float("inf"), 0.44)}, {}),
        ({"effect": "Rainbow", "effect_list": "Rainbow"}, {}),
        (
            {"effect": "Rainbow", "effect_list": ["Rainbow", {}, False]},
            {"effect": "Rainbow"},
        ),
    ],
)
def test_reported_settings_normalizes_light_attributes(attributes, expected):
    assert reported_settings(State(MIRROR, STATE_ON, attributes)) == expected


def test_available_effects_keeps_only_unique_nonempty_names():
    state = State(
        MIRROR, STATE_ON, {"effect_list": ["Off", "", [], " ", "Off", "Color"]}
    )
    assert available_effects(state) == ("Off", "Color")


@pytest.mark.asyncio
async def test_restore_valid_brightness_when_reported_color_is_invalid(ha):
    hass, lights = ha
    light = lights[MIRROR]
    entry = await install(hass)
    await add_group(
        hass, entry, {"name": "Night", "lights": [MIRROR], "brightness": 10}
    )
    await action(hass, "turn_on", {"entity_id": MIRROR}, {"brightness": 195})
    hass.states.async_set(
        MIRROR,
        STATE_ON,
        {"color_mode": "rgb", "rgb_color": [999, 0, 0], "brightness": "195"},
    )
    await action(
        hass, "turn_on", {"entity_id": profile_switch(hass, entry)}, domain="switch"
    )
    assert light.calls[-1]["brightness"] == 26

    await action(
        hass, "turn_off", {"entity_id": profile_switch(hass, entry)}, domain="switch"
    )

    assert light.calls[-1] == {"brightness": 195}


@pytest.mark.asyncio
async def test_zero_brightness_turns_off_only_its_group_and_restores_latest_request(
    ha,
):
    hass, lights = ha
    entry = await install(hass)
    result = await add_group(
        hass,
        entry,
        {
            "name": "Off",
            "lights": [MIRROR, HALL],
            "brightness": 0,
            "color": [255, 0, 0],
            "transition_ms": 1500,
        },
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert next(iter(entry.subentries.values())).data["brightness"] == 0
    await add_group(hass, entry, {"name": "Dim", "lights": [SHOWER], "brightness": 10})
    await action(hass, "turn_on", {"entity_id": MIRROR}, {"brightness": 195})
    await action(
        hass, "turn_on", {"entity_id": profile_switch(hass, entry)}, domain="switch"
    )
    assert hass.states.is_state(MIRROR, STATE_OFF)
    assert lights[MIRROR].off_calls == [{"transition": 1.5}]
    assert lights[HALL].off_calls == []

    for command in ("turn_on", "toggle"):
        await action(
            hass,
            command,
            {"entity_id": [MIRROR, SHOWER] if command == "turn_on" else MIRROR},
            {"brightness_pct": 75, "rgb_color": [0, 0, 255]},
        )
        assert hass.states.is_state(MIRROR, STATE_OFF)
    assert len(lights[MIRROR].calls) == 1
    assert lights[SHOWER].calls[-1]["brightness"] == 26

    await action(
        hass, "turn_off", {"entity_id": profile_switch(hass, entry)}, domain="switch"
    )
    assert hass.states.is_state(MIRROR, STATE_ON)
    assert lights[MIRROR].calls[-1] == {"brightness": 191, "rgb_color": (0, 0, 255)}
    assert hass.states.is_state(HALL, STATE_OFF)


@pytest.mark.asyncio
@pytest.mark.parametrize("last_command", [None, "turn_off", "brightness_zero", "step"])
async def test_zero_brightness_restores_power_and_remembers_partial_requests(
    ha, last_command
):
    hass, lights = ha
    entry = await install(hass)
    await add_group(hass, entry, {"name": "Off", "lights": [MIRROR], "brightness": 0})
    target = {"entity_id": MIRROR}
    await action(hass, "turn_on", target, {"brightness": 180, "rgb_color": [1, 2, 3]})
    await action(
        hass, "turn_on", {"entity_id": profile_switch(hass, entry)}, domain="switch"
    )
    assert hass.states.is_state(MIRROR, STATE_OFF)

    if last_command is not None:
        await action(hass, "turn_on", target, {"brightness": 100})
        await action(hass, "turn_on", target, {"rgb_color": [4, 5, 6]})
        if last_command == "turn_off":
            await action(hass, "turn_off", target)
        elif last_command == "brightness_zero":
            await action(hass, "turn_on", target, {"brightness_pct": 0})
        else:
            await action(hass, "turn_on", target, {"brightness_step": 25})
        assert hass.states.is_state(MIRROR, STATE_OFF)

    await action(
        hass, "turn_off", {"entity_id": profile_switch(hass, entry)}, domain="switch"
    )
    if last_command in ("turn_off", "brightness_zero"):
        assert hass.states.is_state(MIRROR, STATE_OFF)
        await action(hass, "turn_on", target)
        expected = {"brightness": 100, "rgb_color": (4, 5, 6)}
    elif last_command == "step":
        expected = {"brightness": 125, "rgb_color": (4, 5, 6)}
    else:
        expected = {"brightness": 180, "rgb_color": (1, 2, 3)}
    assert hass.states.is_state(MIRROR, STATE_ON)
    assert lights[MIRROR].calls[-1] == expected


@pytest.mark.asyncio
async def test_turn_on_intent_survives_handoff_from_zero_brightness_profile(ha):
    hass, lights = ha
    low = await install(hass, title="Evening", priority=10)
    high = await install(hass, title="Night", priority=30)
    for entry, brightness in ((low, 40), (high, 0)):
        await add_group(
            hass,
            entry,
            {"name": "Shared", "lights": [MIRROR], "brightness": brightness},
        )
    await action(
        hass,
        "turn_on",
        {"entity_id": [profile_switch(hass, low), profile_switch(hass, high)]},
        domain="switch",
    )
    await action(hass, "turn_on", {"entity_id": MIRROR}, {"brightness": 150})
    assert hass.states.is_state(MIRROR, STATE_OFF)
    await action(
        hass, "turn_off", {"entity_id": profile_switch(hass, high)}, domain="switch"
    )
    assert hass.states.is_state(MIRROR, STATE_ON)
    assert lights[MIRROR].calls[-1]["brightness"] == 102
    await action(
        hass, "turn_off", {"entity_id": profile_switch(hass, low)}, domain="switch"
    )
    assert lights[MIRROR].calls[-1]["brightness"] == 150


@pytest.mark.asyncio
async def test_disabling_one_group_preserves_its_settings_and_other_groups(ha):
    hass, lights = ha
    low = await install(hass, title="Evening", priority=10)
    high = await install(hass, title="Night", priority=30)
    group_data = {"name": "Mirror", "lights": [MIRROR], "brightness": 10}
    await add_group(hass, low, group_data | {"brightness": 40})
    await add_group(hass, high, group_data)
    await add_group(
        hass, high, {"name": "Shower", "lights": [SHOWER], "brightness": 20}
    )
    group = next(item for item in high.subentries.values() if item.title == "Mirror")
    context = {"source": "reconfigure", "subentry_id": group.subentry_id}
    await action(hass, "turn_on", {"entity_id": [MIRROR, SHOWER]}, {"brightness": 185})
    await action(
        hass,
        "turn_on",
        {"entity_id": [profile_switch(hass, low), profile_switch(hass, high)]},
        domain="switch",
    )
    assert lights[MIRROR].calls[-1]["brightness"] == 26

    for enabled, expected in ((False, 102), (True, 26)):
        form = await hass.config_entries.subentries.async_init(
            (high.entry_id, GROUP), context=context
        )
        marker = next(
            field for field in form["data_schema"].schema if field == "enabled"
        )
        assert marker.description["suggested_value"] is not enabled
        await configure_group(hass, form["flow_id"], group_data | {"enabled": enabled})
        await hass.async_block_till_done()
        assert high.subentries[group.subentry_id].data["enabled"] is enabled
        assert high.subentries[group.subentry_id].data["brightness"] == 10
        assert hass.states.is_state(profile_switch(hass, high), STATE_ON)
        assert lights[MIRROR].calls[-1]["brightness"] == expected
        assert lights[SHOWER].calls[-1]["brightness"] == 51


@pytest.mark.asyncio
async def test_local_assist_controls_rooms_and_the_whole_profile(ha):
    hass, lights = ha
    areas = area_registry.async_get(hass)
    living = areas.async_create("Гостиная")
    office = areas.async_create("Кабинет", aliases={"кабинете"})
    entry = await install(hass, title="Ночной режим")
    for name, entity, area, level in (
        ("Mirror", MIRROR, living, 10),
        ("Shower", SHOWER, living, 20),
        ("Hall", HALL, office, 30),
    ):
        result = await add_group(
            hass,
            entry,
            {
                "name": name,
                "lights": [entity],
                "area": area.id,
                "brightness": level,
            },
        )
        assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY

    registry = entity_registry.async_get(hass)
    overall = registry.async_get_entity_id("switch", DOMAIN, entry.entry_id)
    switches = {
        group.title: registry.async_get_entity_id(
            "switch", DOMAIN, group_switch_id(entry.entry_id, group.subentry_id)
        )
        for group in entry.subentries.values()
    }
    assert len(set(switches.values())) == 3
    assert hass.states.get(switches["Mirror"]).name == "Ночной режим · Mirror"
    await action(
        hass, "turn_on", {"entity_id": [MIRROR, SHOWER, HALL]}, {"brightness": 200}
    )

    device = device_registry.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "test-voice-device")},
    )
    device_registry.async_get(hass).async_update_device(device.id, area_id=office.id)

    async def say(sentence, language="ru"):
        result = await async_converse(
            hass, sentence, None, Context(), language=language, device_id=device.id
        )
        await hass.async_block_till_done()
        assert result.response.response_type.value == "action_done"

    await say("Включи ночной режим в гостиной")
    assert [
        lights[entity].calls[-1]["brightness"] for entity in (MIRROR, SHOWER, HALL)
    ] == [26, 51, 200]
    assert hass.states.is_state(switches["Mirror"], STATE_ON)
    assert hass.states.is_state(switches["Hall"], STATE_OFF)
    assert hass.states.is_state(overall, STATE_OFF)

    await say("Включи ночной режим")
    assert lights[HALL].calls[-1]["brightness"] == 76
    assert hass.states.is_state(overall, STATE_ON)

    await say("Выключи ночной режим в кабинете")
    assert lights[HALL].calls[-1]["brightness"] == 200
    assert hass.states.is_state(switches["Mirror"], STATE_ON)
    assert hass.states.is_state(switches["Hall"], STATE_OFF)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.is_state(switches["Mirror"], STATE_ON)
    assert hass.states.is_state(switches["Hall"], STATE_OFF)

    await say("Выключи ночной режим")
    assert all(hass.states.is_state(entity, STATE_OFF) for entity in switches.values())
    assert all(lights[entity].calls[-1]["brightness"] == 200 for entity in lights)


@pytest.mark.asyncio
async def test_switch_codes_and_ids_survive_renaming_and_moving_a_group(ha):
    hass, _ = ha
    registry = entity_registry.async_get(hass)
    areas = area_registry.async_get(hass)
    living = areas.async_create("Гостиная")
    bedroom = areas.async_create("Спальня")
    entry = await install(hass, title="Ночной режим")
    data = {"name": "Mirror", "lights": [MIRROR], "area": living.id}
    await add_group(hass, entry, data)
    group = next(iter(entry.subentries.values()))
    overall = profile_switch(hass, entry)
    group_id = registry.async_get_entity_id(
        "switch", DOMAIN, group_switch_id(entry.entry_id, group.subentry_id)
    )
    profile_code = entry.data[SWITCH_CODE]
    group_code = group.data[SWITCH_CODE]
    for code in (profile_code, group_code):
        assert len(code) == 4
        assert code.isascii() and code.isalnum() and code == code.lower()
    assert overall == f"switch.light_profile_{profile_code}"
    assert group_id == f"{overall}_group_{group_code}"

    options = await hass.config_entries.options.async_init(entry.entry_id)
    fields = options["data_schema"].schema
    assert "identifier" not in fields
    assert options["description_placeholders"] == {"identifier": overall}
    await hass.config_entries.options.async_configure(
        options["flow_id"], {"name": "Тихий режим", "priority": 50}
    )
    await hass.async_block_till_done()
    await action(hass, "turn_on", {"entity_id": group_id}, domain="switch")

    form = await hass.config_entries.subentries.async_init(
        (entry.entry_id, GROUP),
        context={"source": "reconfigure", "subentry_id": group.subentry_id},
    )
    fields = form["data_schema"].schema
    assert "identifier" not in fields
    assert form["description_placeholders"] == {"identifier": group_id}
    await configure_group(
        hass, form["flow_id"], data | {"name": "Bedside", "area": bedroom.id}
    )
    await hass.async_block_till_done()
    assert profile_switch(hass, entry) == overall
    assert registry.async_get(group_id).area_id == bedroom.id
    assert hass.states.get(group_id).name == "Тихий режим · Bedside"
    assert hass.states.is_state(group_id, STATE_ON)
    reply = await async_converse(
        hass, "Выключи тихий режим в спальне", None, Context(), language="ru"
    )
    assert reply.response.response_type.value == "action_done"
    await hass.async_block_till_done()
    assert hass.states.is_state(group_id, STATE_OFF)


@pytest.mark.asyncio
async def test_switch_code_allocation_retries_collisions(ha, monkeypatch):
    hass, _ = ha
    choices = iter("abcdabcdwxyzabcdabcd1234")
    monkeypatch.setattr(
        "custom_components.light_profile.config_flow.choice",
        lambda _: next(choices),
    )
    first = await install(hass, priority=10)
    second = await install(hass, priority=20)
    assert first.data[SWITCH_CODE] == "abcd"
    assert second.data[SWITCH_CODE] == "wxyz"

    await add_group(hass, first, {"name": "Mirror", "lights": [MIRROR]})
    await add_group(hass, first, {"name": "Shower", "lights": [SHOWER]})
    assert {group.data[SWITCH_CODE] for group in first.subentries.values()} == {
        "abcd",
        "1234",
    }


@pytest.mark.asyncio
async def test_group_target_picker_expands_lights_and_saves_a_snapshot(ha):
    hass, _ = ha
    entry = await install(hass)
    room = area_registry.async_get(hass).async_create("Bedroom")
    label = label_registry.async_get(hass).async_create("Track lights")
    device = device_registry.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, "multi-light")}
    )
    registry = entity_registry.async_get(hass)
    in_room = registry.async_get_or_create("light", DOMAIN, "room-light")
    registry.async_update_entity(in_room.entity_id, area_id=room.id)
    registry.async_update_entity(MIRROR, area_id=room.id)
    registry.async_update_entity(SHOWER, device_id=device.id)
    registry.async_update_entity(HALL, labels={label.label_id})
    second = registry.async_get_or_create(
        "light", DOMAIN, "second-light", device_id=device.id
    )
    registry.async_get_or_create("sensor", DOMAIN, "battery", device_id=device.id)

    form = await hass.config_entries.subentries.async_init(
        (entry.entry_id, GROUP), context={"source": "user"}
    )
    config = form["data_schema"].schema["lights"].serialize()["selector"]["target"]
    assert config["entity"] == [{"domain": ["light"]}]
    assert form["description_placeholders"] is None
    target = {
        "area_id": [room.id],
        "device_id": [device.id],
        "label_id": [label.label_id],
        "entity_id": [registry.async_get(MIRROR).id],
    }
    result = await configure_group(
        hass, form["flow_id"], {"name": "Room", "lights": target}
    )
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    group = next(iter(entry.subentries.values()))
    expected = sorted([MIRROR, SHOWER, HALL, second.entity_id, in_room.entity_id])
    assert group.data["lights"] == expected

    registry.async_update_entity(in_room.entity_id, area_id=None)
    form = await hass.config_entries.subentries.async_init(
        (entry.entry_id, GROUP),
        context={"source": "reconfigure", "subentry_id": group.subentry_id},
    )
    field = next(field for field in form["data_schema"].schema if field == "lights")
    assert field.description["suggested_value"] == {"entity_id": expected}
