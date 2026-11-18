"""Wifi Commands configuration carried in, and restored from, the hub backup."""

import asyncio
import importlib
from types import SimpleNamespace

from custom_components.sofabaton_x1s.command_config import CommandConfigStore, default_commands
from custom_components.sofabaton_x1s.wifi_config_backup import (
    build_wifi_commands_section,
    plan_wifi_commands_restore,
    restore_id_maps,
    wifi_commands_section,
)

integration = importlib.import_module("custom_components.sofabaton_x1s.__init__")
runtime_module = importlib.import_module("custom_components.sofabaton_x1s.runtime")
operations_module = importlib.import_module("custom_components.sofabaton_x1s.operations")
ws_backup = importlib.import_module("custom_components.sofabaton_x1s.ws_backup")

ACTION = {"action": "perform-action", "perform_action": "light.toggle"}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _record(key, name, deployed_id, *, activity="101"):
    commands = default_commands()
    commands[0] = {
        "name": "Lights",
        "add_as_favorite": True,
        "hard_button": "182",
        "long_press_enabled": False,
        "input_activity_id": activity,
        "activities": [activity],
        "action": dict(ACTION),
        "long_press_action": {"action": "none"},
    }
    return {
        "device_key": key,
        "device_name": name,
        "slot_count": 10,
        "commands": commands,
        "activity_labels": {activity: "Watch TV"},
        "deployed_device_id": deployed_id,
        "deployed_commands_hash": "abc123" if deployed_id is not None else "",
        "deployed_commands": [{"name": "Lights"}] if deployed_id is not None else [],
        "deployed_request_port": 8060,
        "delivery": {"status": "delivered"},
    }


def _hub_section():
    return {
        "devices": [
            _record("aa11bb22", "Shield Apps", 7),
            _record("cc33dd44", "Draft", None),
        ],
        "event_actions": {"power_off": dict(ACTION)},
        "activity_event_actions": {"101": {"start": dict(ACTION)}},
        "matter_event_actions": {"1": {"long_press": dict(ACTION)}},
        "matter_device_id": "matter-dev",
    }


# ── export ───────────────────────────────────────────────────────────────


def test_whole_hub_section_carries_every_record_and_hub_actions():
    section = build_wifi_commands_section(_hub_section(), device_ids=None)
    assert section["whole_hub"] is True
    assert [d["device_key"] for d in section["devices"]] == ["aa11bb22", "cc33dd44"]
    assert section["event_actions"] == {"power_off": ACTION}
    assert section["matter_device_id"] == "matter-dev"
    assert section["devices"][0]["commands"][0]["action"] == ACTION


def test_device_scoped_section_carries_only_records_deployed_in_scope():
    section = build_wifi_commands_section(_hub_section(), device_ids=[7])
    assert section["whole_hub"] is False
    assert [d["device_key"] for d in section["devices"]] == ["aa11bb22"]
    assert "event_actions" not in section
    assert build_wifi_commands_section(_hub_section(), device_ids=[9])["devices"] == []


def test_section_reader_ignores_bundles_without_one():
    assert wifi_commands_section({"kind": "hub_bundle"}) is None
    assert wifi_commands_section({"ha_config": {"wifi_commands": {"version": 99, "devices": []}}}) is None


# ── restore planning ─────────────────────────────────────────────────────


def test_replace_restore_remaps_ids_and_keeps_drafts():
    section = build_wifi_commands_section(_hub_section(), device_ids=None)
    plan = plan_wifi_commands_restore(
        section,
        device_id_map={7: 3},
        activity_id_map={101: 105},
        replace_mode=True,
        same_hub=True,
    )
    assert plan["replace_devices"] is True
    shield, draft = plan["devices"]
    assert shield["deployed_device_id"] == 3
    assert shield["deployed_commands_hash"] == "abc123"
    assert shield["delivery"] is None
    slot = shield["commands"][0]
    assert slot["activities"] == ["105"]
    assert slot["input_activity_id"] == "105"
    assert slot["action"] == ACTION
    assert shield["activity_labels"] == {"105": "Watch TV"}
    assert draft["deployed_device_id"] is None
    assert plan["activity_event_actions"] == {"105": {"start": ACTION}}
    assert plan["hub_actions"]["event_actions"] == {"power_off": ACTION}


def test_replace_restore_drops_activities_it_did_not_bring_back():
    section = build_wifi_commands_section(_hub_section(), device_ids=None)
    plan = plan_wifi_commands_restore(
        section, device_id_map={7: 3}, activity_id_map={}, replace_mode=True, same_hub=True
    )
    slot = plan["devices"][0]["commands"][0]
    assert slot["activities"] == []
    assert slot["input_activity_id"] == ""
    assert plan["activity_event_actions"] == {}


def test_merge_onto_same_hub_keeps_unrestored_activity_ids():
    section = build_wifi_commands_section(_hub_section(), device_ids=None)
    plan = plan_wifi_commands_restore(
        section, device_id_map={7: 7}, activity_id_map={}, replace_mode=False, same_hub=True
    )
    assert plan["replace_devices"] is False
    assert plan["hub_actions"] is None
    # Only records whose device came back; drafts stay out of a merge.
    assert [d["device_key"] for d in plan["devices"]] == ["aa11bb22"]
    assert plan["devices"][0]["commands"][0]["activities"] == ["101"]


def test_merge_onto_other_hub_drops_unrestored_activity_ids():
    section = build_wifi_commands_section(_hub_section(), device_ids=None)
    plan = plan_wifi_commands_restore(
        section, device_id_map={7: 12}, activity_id_map={}, replace_mode=False, same_hub=False
    )
    assert plan["devices"][0]["deployed_device_id"] == 12
    assert plan["devices"][0]["commands"][0]["activities"] == []


def test_restore_id_maps_read_the_restore_result():
    devices, activities = restore_id_maps(
        {
            "device_id_map": {"7": 3, "8": 4},
            "restored_activities": [{"source_activity_id": 101, "activity_id": 105}],
        }
    )
    assert devices == {7: 3, 8: 4}
    assert activities == {101: 105}


# ── store apply ──────────────────────────────────────────────────────────


def _store_with(section):
    store = CommandConfigStore(SimpleNamespace())
    _run(store.async_load())
    store._data = {"hubs": {"entry-1": section}}  # type: ignore[attr-defined]
    return store


def test_store_export_then_replace_restore_round_trips():
    source = _store_with(_hub_section())
    section = build_wifi_commands_section(source.export_hub_section("entry-1"), device_ids=None)

    target = _store_with({"devices": [_record("ee55ff66", "Stale", 2)]})
    plan = plan_wifi_commands_restore(
        section, device_id_map={7: 3}, activity_id_map={101: 105}, replace_mode=True, same_hub=False
    )
    assert _run(target.async_apply_restore_plan("entry-1", plan)) == 2

    devices = _run(target.async_list_hub_devices("entry-1"))
    assert [d["device_key"] for d in devices] == ["aa11bb22", "cc33dd44"]
    assert devices[0]["device_name"] == "Shield Apps"
    assert devices[0]["deployed_device_id"] == 3
    assert devices[0]["commands"][0]["action"] == ACTION
    assert target.get_hub_event_actions("entry-1")["power_off"] == ACTION
    activity_actions = target.get_activity_event_actions("entry-1")
    assert list(activity_actions) == ["105"]
    assert activity_actions["105"]["start"] == ACTION
    assert target.get_matter_device_choice("entry-1") == "matter-dev"


def test_store_merge_restore_upserts_and_releases_claimed_device_id():
    section = build_wifi_commands_section(_hub_section(), device_ids=[7])
    target = _store_with(
        {
            "devices": [_record("ee55ff66", "Other", 7), _record("aa11bb22", "Old Shield", 5)],
            "event_actions": {"power_off": {"action": "keep-me"}},
        }
    )
    plan = plan_wifi_commands_restore(
        section, device_id_map={7: 7}, activity_id_map={}, replace_mode=False, same_hub=True
    )
    _run(target.async_apply_restore_plan("entry-1", plan))

    devices = {d["device_key"]: d for d in _run(target.async_list_hub_devices("entry-1"))}
    assert devices["aa11bb22"]["device_name"] == "Shield Apps"
    assert devices["aa11bb22"]["deployed_device_id"] == 7
    # The other record claimed the same hub device; the restored one owns it now.
    assert devices["ee55ff66"]["deployed_device_id"] is None
    assert target.get_hub_event_actions("entry-1")["power_off"] == {"action": "keep-me"}


# ── backup operations ────────────────────────────────────────────────────


class _Hub:
    entry_id = "entry-1"
    name = "Living Room"

    async def async_backup_hub(self, *, device_ids=None, progress_callback=None, wait_timeout=10.0):
        return {
            "kind": "hub_bundle",
            "schema_version": 5,
            "captured_at": "2026-11-17T08:09:10+00:00",
            "hub": {"entry_id": "entry-1", "name": "Living Room"},
            "devices": [{"device": {"device_id": 7, "name": "Shield Apps"}}],
            "activities": [],
        }

    async def async_restore_backup(self, payload, *, replace_mode=None, progress_callback=None, wifi_commands_request_port=8060):
        return {
            "status": "success",
            "device_id_map": {"7": 3},
            "restored_devices": [{"source_device_id": 7, "device_id": 3}],
            "restored_activities": [{"source_activity_id": 101, "activity_id": 105}],
        }


def test_backup_operation_exports_and_restore_operation_rebuilds_store(monkeypatch):
    source = _store_with(_hub_section())
    target = _store_with({})
    current = {"store": source}

    async def fake_store(_hass):
        return current["store"]

    monkeypatch.setattr(runtime_module, "_async_get_command_config_store", fake_store)
    monkeypatch.setattr(ws_backup, "async_dispatcher_send", lambda *_args: None)

    loop = asyncio.new_event_loop()
    try:
        hass = SimpleNamespace(loop=loop, data={integration.DOMAIN: {}})
        registry = operations_module._backup_operation_registry(hass)
        export_id = registry.create(kind="backup_export", entry_id="entry-1", initial_state={})
        loop.run_until_complete(
            ws_backup._run_backup_export_operation(hass, export_id, hub=_Hub(), device_ids=None)
        )
        bundle = registry.get(export_id)["state"]["backup"]
        assert bundle["ha_config"]["wifi_commands"]["devices"][0]["device_name"] == "Shield Apps"

        current["store"] = target
        restore_id = registry.create(kind="backup_restore", entry_id="entry-1", initial_state={})
        loop.run_until_complete(
            ws_backup._run_backup_restore_operation(
                hass, restore_id, hub=_Hub(), payload=bundle, mode="replace"
            )
        )
        state = registry.get(restore_id)["state"]
    finally:
        loop.close()

    assert state["status"] == "success"
    assert state["result"]["wifi_commands_restored"] == 2
    restored = target.export_hub_section("entry-1")
    assert restored["devices"][0]["deployed_device_id"] == 3
    assert restored["devices"][0]["commands"][0]["activities"] == ["105"]
