"""WebSocket API backing the recordings card.

The Media Source browser exists for HA's generic Media panel, but a purpose-built card wants
plain data — days, and clips with their type — not a `media-source://` tree it has to walk and
re-parse. These commands return exactly that, plus a ready-to-play URL per clip so the card
never has to know how playback is wired.

    kasa_doorbell_local/recordings/cameras            -> [{entry_id, name, entity_id}]
    kasa_doorbell_local/recordings/days   {entry_id}  -> {days: [...], start, end}
    kasa_doorbell_local/recordings/clips  {entry_id, day}
                                                -> [{start, end, duration, type, label}]
    kasa_doorbell_local/recordings/prepare {entry_id, start, duration}
                                                -> {ready, progress, url, error}

`prepare` is how a clip is played: it captures the recording to a real MP4 and the card polls
until it is ready, then points <video> at `url`. Streaming the recording live is what caused
every playback problem this integration has had — see clips.py.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import ALARM_LABELS, DOMAIN, PLAYBACK_PREROLL_SECONDS
from .clips import async_get_cache, clip_seconds
from .view import async_clip_path

_LOGGER = logging.getLogger(__name__)

# A day is fetched in slices; `get_detect_zone_list` was measured uncapped over 6 h, so this
# is about bounding one response, not working around a limit.
DETECT_CHUNK_SECONDS = 6 * 3600
MAX_ARCHIVE_DAYS = 60

_REGISTERED = f"{__name__}_registered"


@callback
def async_register(hass: HomeAssistant) -> None:
    """Register the recordings commands once."""
    if hass.data.get(_REGISTERED):
        return
    websocket_api.async_register_command(hass, ws_cameras)
    websocket_api.async_register_command(hass, ws_days)
    websocket_api.async_register_command(hass, ws_clips)
    websocket_api.async_register_command(hass, ws_prepare)
    hass.data[_REGISTERED] = True


def _entries(hass: HomeAssistant) -> list[ConfigEntry]:
    """Loaded entries whose camera can actually serve recordings."""
    return [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
        and getattr(entry, "runtime_data", None) is not None
        and entry.runtime_data.has_recordings
    ]


def _entry(hass: HomeAssistant, entry_id: str) -> ConfigEntry | None:
    """Look up by config-entry id, or by camera entity_id so the card can be configured
    the way every other HA card is — with an entity."""
    for entry in _entries(hass):
        if entry.entry_id == entry_id:
            return entry
    if "." in entry_id:
        entity = er.async_get(hass).async_get(entry_id)
        if entity is not None:
            for entry in _entries(hass):
                if entry.entry_id == entity.config_entry_id:
                    return entry
    return None


def _camera_entity(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.domain == "camera":
            return entity.entity_id
    return None


@websocket_api.websocket_command(
    {vol.Required("type"): "kasa_doorbell_local/recordings/cameras"}
)
@callback
def ws_cameras(hass: HomeAssistant, connection, msg: dict) -> None:
    """Cameras that can serve recordings, so the card can offer a picker."""
    connection.send_result(
        msg["id"],
        [
            {
                "entry_id": entry.entry_id,
                "name": entry.runtime_data.device_info.get("name") or entry.title,
                "entity_id": _camera_entity(hass, entry),
            }
            for entry in _entries(hass)
        ],
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "kasa_doorbell_local/recordings/days",
        vol.Required("entry_id"): str,
    }
)
@websocket_api.async_response
async def ws_days(hass: HomeAssistant, connection, msg: dict) -> None:
    """Which days the SD card still holds, newest first."""
    entry = _entry(hass, msg["entry_id"])
    if entry is None:
        connection.send_error(msg["id"], "not_found", "Unknown camera")
        return
    recorded = await entry.runtime_data.async_get_record_range()
    if recorded is None:
        connection.send_error(
            msg["id"], "no_recordings", "The camera reports no SD card recordings"
        )
        return
    first, last = (_local(ts).date() for ts in recorded)
    first = max(first, last - timedelta(days=MAX_ARCHIVE_DAYS))
    days = []
    day = last
    while day >= first:
        days.append(day.isoformat())
        day -= timedelta(days=1)
    connection.send_result(
        msg["id"], {"days": days, "start": recorded[0], "end": recorded[1]}
    )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "kasa_doorbell_local/recordings/clips",
        vol.Required("entry_id"): str,
        vol.Required("day"): str,
    }
)
@websocket_api.async_response
async def ws_clips(hass: HomeAssistant, connection, msg: dict) -> None:
    """Every finalized clip on one local day, newest first.

    Types are returned raw AND labelled: the card filters on the raw code, and shows the
    label. Note the code names the RECORDING TRIGGER, not an AI classification.
    """
    entry = _entry(hass, msg["entry_id"])
    if entry is None:
        connection.send_error(msg["id"], "not_found", "Unknown camera")
        return
    try:
        day = date.fromisoformat(msg["day"])
    except ValueError:
        connection.send_error(msg["id"], "invalid_format", f"Bad day: {msg['day']}")
        return

    coordinator = entry.runtime_data
    start = int(dt_util.start_of_local_day(day).timestamp())
    end = int(dt_util.start_of_local_day(day + timedelta(days=1)).timestamp())

    clips: dict[tuple[int, str], dict] = {}
    for window_start in range(start, end, DETECT_CHUNK_SECONDS):
        window_end = min(window_start + DETECT_CHUNK_SECONDS, end)
        for clip in await coordinator.async_get_detections(window_start, window_end):
            when = clip.get("start_time")
            if isinstance(when, int) and start <= when < end:
                clips[(when, str(clip.get("alarm_type") or ""))] = clip

    result = []
    for (when, alarm), clip in sorted(clips.items(), reverse=True):
        finish = clip.get("end_time")
        duration = finish - when if isinstance(finish, int) and finish > when else None
        seek = max(0, when - PLAYBACK_PREROLL_SECONDS)
        result.append(
            {
                "start": when,
                "end": finish if isinstance(finish, int) else None,
                "duration": duration,
                "type": alarm,
                "label": ALARM_LABELS.get(alarm, alarm or "Recording"),
                "time": f"{_local(when):%H:%M:%S}",
                "seek": seek,
            }
        )
    connection.send_result(msg["id"], result)


def _local(timestamp: int):
    return dt_util.as_local(dt_util.utc_from_timestamp(timestamp))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "kasa_doorbell_local/recordings/prepare",
        vol.Required("entry_id"): str,
        vol.Required("seek"): int,
        vol.Optional("duration"): vol.Any(int, None),
    }
)
@websocket_api.async_response
async def ws_prepare(hass: HomeAssistant, connection, msg: dict) -> None:
    """Capture a clip to a file (or report on a capture already running).

    Returns immediately; the card polls this until `ready`, showing `progress`. The capture
    is shared, so polling — or two people opening the same clip — never starts a second one.
    """
    entry = _entry(hass, msg["entry_id"])
    if entry is None:
        connection.send_error(msg["id"], "not_found", "Unknown camera")
        return
    clip = await async_get_cache(hass).async_request(
        entry, msg["seek"], clip_seconds(msg.get("duration"))
    )
    connection.send_result(
        msg["id"],
        {
            "ready": clip.ready,
            "progress": clip.progress,
            "error": clip.error,
            "url": async_clip_path(hass, entry.entry_id, msg["seek"]) if clip.ready else None,
        },
    )
