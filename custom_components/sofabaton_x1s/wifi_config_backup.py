"""Wifi Commands configuration inside the integration's hub backup.

The hub backup (``hub_bundle``) only holds what lives on the hub. A Wifi
Commands device also has a Home Assistant side that exists nowhere else:
the per-button actions, the activity choices and the hub / activity /
Matter event actions in the ``command_config`` store. This module carries
that store section in the bundle's ``ha_config.wifi_commands`` block and
maps it back onto the store after a restore.

Ids are the only translation. A restore assigns new hub device and
activity ids, so every stored reference is rewritten through the restore
result's maps. A reference the restore did not bring back is kept only
when the bundle came from this same hub and the restore did not erase it
(a merge onto the source hub); otherwise it is dropped.

Pure functions only: no I/O and no Home Assistant imports, so the logic is
testable on its own. :class:`CommandConfigStore` applies the result.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable, Mapping

HA_CONFIG_KEY = "ha_config"
WIFI_COMMANDS_KEY = "wifi_commands"
WIFI_COMMANDS_SECTION_VERSION = 1


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def build_wifi_commands_section(
    hub_section: Mapping[str, Any],
    *,
    device_ids: Iterable[int] | None,
) -> dict[str, Any]:
    """The ``ha_config.wifi_commands`` block for one hub's store section.

    ``hub_section`` is the store's per-hub record (``devices``,
    ``event_actions``, ...). ``device_ids`` is the backup's device scope:
    ``None`` for a whole-hub backup, which carries every record and the
    hub-level actions; a device-scoped backup carries only the records
    deployed as one of those devices, and no hub-level actions.
    """

    devices = [deepcopy(d) for d in hub_section.get("devices") or [] if isinstance(d, Mapping)]
    whole_hub = device_ids is None
    if not whole_hub:
        scope = {int(value) for value in device_ids or []}
        devices = [d for d in devices if _int_or_none(d.get("deployed_device_id")) in scope]
    section: dict[str, Any] = {
        "version": WIFI_COMMANDS_SECTION_VERSION,
        "whole_hub": whole_hub,
        "devices": devices,
    }
    if whole_hub:
        for key in ("event_actions", "activity_event_actions", "matter_event_actions"):
            value = hub_section.get(key)
            if isinstance(value, Mapping):
                section[key] = deepcopy(dict(value))
        matter_device_id = hub_section.get("matter_device_id")
        if isinstance(matter_device_id, str) and matter_device_id:
            section["matter_device_id"] = matter_device_id
    return section


def wifi_commands_section(bundle: Mapping[str, Any]) -> dict[str, Any] | None:
    """The bundle's Wifi Commands block, or None when it carries none."""

    ha_config = bundle.get(HA_CONFIG_KEY) if isinstance(bundle, Mapping) else None
    section = ha_config.get(WIFI_COMMANDS_KEY) if isinstance(ha_config, Mapping) else None
    if not isinstance(section, Mapping):
        return None
    if _int_or_none(section.get("version")) != WIFI_COMMANDS_SECTION_VERSION:
        return None
    if not isinstance(section.get("devices"), list):
        return None
    return dict(section)


class _IdMap:
    """Source id -> restored id, with the keep-unmapped rule."""

    def __init__(self, mapping: Mapping[int, int], *, keep_unmapped: bool) -> None:
        self._mapping = {int(k): int(v) for k, v in mapping.items()}
        self._keep_unmapped = keep_unmapped

    def map(self, value: Any) -> int | None:
        source = _int_or_none(value)
        if source is None:
            return None
        if source in self._mapping:
            return self._mapping[source]
        return source if self._keep_unmapped else None


def _remap_record(record: Mapping[str, Any], activities: _IdMap, new_device_id: int | None) -> dict[str, Any]:
    out = deepcopy(dict(record))
    out["deployed_device_id"] = new_device_id
    if new_device_id is None:
        # Nothing on the hub carries this record: an undeployed draft.
        out["deployed_commands"] = []
        out["deployed_commands_hash"] = ""
        out["deployed_transport"] = None
    # Whether a press reaches Home Assistant is re-learned on the new hub.
    out["delivery"] = None

    commands = []
    for slot in record.get("commands") or []:
        if not isinstance(slot, Mapping):
            commands.append(slot)
            continue
        slot_out = deepcopy(dict(slot))
        mapped = [activities.map(a) for a in slot.get("activities") or []]
        slot_out["activities"] = [str(a) for a in mapped if a is not None]
        input_activity = str(slot.get("input_activity_id") or "").strip()
        if input_activity:
            new_input = activities.map(input_activity)
            slot_out["input_activity_id"] = "" if new_input is None else str(new_input)
        commands.append(slot_out)
    out["commands"] = commands

    labels = record.get("activity_labels")
    if isinstance(labels, Mapping):
        new_labels: dict[str, Any] = {}
        for key, name in labels.items():
            new_key = activities.map(key)
            if new_key is not None:
                new_labels[str(new_key)] = name
        out["activity_labels"] = new_labels
    return out


def plan_wifi_commands_restore(
    section: Mapping[str, Any],
    *,
    device_id_map: Mapping[int, int],
    activity_id_map: Mapping[int, int],
    replace_mode: bool,
    same_hub: bool,
) -> dict[str, Any]:
    """Translate a bundle's Wifi Commands block onto the restored hub.

    Returns ``{"devices": [...], "replace_devices": bool,
    "activity_event_actions": {...}, "hub_actions": {...} | None}``:

    * ``devices`` -- records to write, ids rewritten. A record deployed as
      a device this restore brought back points at that device's new id.
      Undeployed records (drafts) and records whose device was not
      restored come back only with a whole-hub replacing restore, as
      undeployed drafts.
    * ``replace_devices`` -- True for a whole-hub replacing restore: the
      hub was erased, so its old store records point at nothing and the
      restored set replaces them. Otherwise records upsert by key.
    * ``activity_event_actions`` -- per-activity actions keyed by the new
      activity ids, to merge over the store's.
    * ``hub_actions`` -- hub-level and Matter actions, only for a whole-hub
      replacing restore (a merge leaves the destination's own).
    """

    keep_unmapped = same_hub and not replace_mode
    activities = _IdMap(activity_id_map, keep_unmapped=keep_unmapped)
    devices_map = {int(k): int(v) for k, v in device_id_map.items()}
    whole_hub = bool(section.get("whole_hub"))
    full_replace = whole_hub and replace_mode

    devices: list[dict[str, Any]] = []
    for record in section.get("devices") or []:
        if not isinstance(record, Mapping) or not str(record.get("device_key") or "").strip():
            continue
        source_id = _int_or_none(record.get("deployed_device_id"))
        if source_id is not None and source_id in devices_map:
            devices.append(_remap_record(record, activities, devices_map[source_id]))
        elif full_replace:
            devices.append(_remap_record(record, activities, None))

    activity_actions: dict[str, Any] = {}
    raw_activity_actions = section.get("activity_event_actions")
    if isinstance(raw_activity_actions, Mapping):
        for key, entry in raw_activity_actions.items():
            new_key = activities.map(key)
            if new_key is not None:
                activity_actions[str(new_key)] = deepcopy(entry)

    hub_actions: dict[str, Any] | None = None
    if full_replace:
        hub_actions = {
            "event_actions": deepcopy(section.get("event_actions") or {}),
            "matter_event_actions": deepcopy(section.get("matter_event_actions") or {}),
            "matter_device_id": section.get("matter_device_id"),
        }

    return {
        "devices": devices,
        "replace_devices": full_replace,
        "activity_event_actions": activity_actions,
        "hub_actions": hub_actions,
    }


def restore_id_maps(result: Mapping[str, Any]) -> tuple[dict[int, int], dict[int, int]]:
    """(device map, activity map) from a ``restore_hub_bundle`` result."""

    device_map: dict[int, int] = {}
    for source, target in (result.get("device_id_map") or {}).items():
        source_id, target_id = _int_or_none(source), _int_or_none(target)
        if source_id is not None and target_id is not None:
            device_map[source_id] = target_id
    activity_map: dict[int, int] = {}
    for row in result.get("restored_activities") or []:
        if not isinstance(row, Mapping):
            continue
        source_id = _int_or_none(row.get("source_activity_id"))
        target_id = _int_or_none(row.get("activity_id"))
        if source_id is not None and target_id is not None:
            activity_map[source_id] = target_id
    return device_map, activity_map
