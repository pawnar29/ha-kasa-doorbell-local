"""Constants for the Kasa Camera integration.

The protocol values here — ports, module and method names, the XOR seed, the cipher enum —
were recovered by reverse-engineering the Kasa Smart Android app, then verified against a
real KD110. Everything is LAN-local; there are no cloud endpoints in this integration.
"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "kasa_doorbell"

# ── Config entry keys ────────────────────────────────────────────────────────
CONF_HOST: Final = "host"  # camera LAN IP — used for local video
CONF_EMAIL: Final = "email"  # TP-Link/Kasa account email
CONF_PASSWORD: Final = "password"  # TP-Link/Kasa account password
CONF_STREAM_PASSWORD: Final = "stream_password"  # optional explicit local-secret override
CONF_THING_NAME: Final = "thing_name"  # the camera's own deviceId (from sysinfo)

# ── Local motion (port 10443, Linkie) — fully local, real-time ───────────────
# POST https://<host>:10443/data/LINKIE2.json with the classic Kasa autokey-XOR cipher
# (seed 0xAB, keyless) + HTTP Basic auth (email : md5 device secret). The VOD module's
# get_detect_event_state returns {is_moving, is_event_finish, is_video_ready} — a live,
# bidirectional motion signal. Verified against the KD110: is_moving tracks real motion,
# ~1s latency, true off-state. Param MUST be {"timestamp": <epoch SECONDS int>}.
LINKIE_PORT: Final = 10443
LINKIE_PATH: Final = "/data/LINKIE2.json"
LINKIE_XOR_SEED: Final = 0xAB
LINKIE_VOD_MODULE: Final = "smartlife.cam.ipcamera.vod"
LINKIE_MOTION_METHOD: Final = "get_detect_event_state"

# ── Local identity + capabilities (system.get_sysinfo) ───────────────────────
# NOTE the module key is the bare "system", NOT "smartlife.cam.ipcamera.system".
# Returns deviceId/model/alias/mac/hw_ver/sw_ver + c_opt + new_feature + live switch states,
# so setup and capability detection need no cloud at all. Verified on the KD110: new_feature
# decodes to exactly the capability set the cloud reports in deviceContext.deviceNewFeature.
LINKIE_SYSTEM_MODULE: Final = "system"
LINKIE_SYSINFO_METHOD: Final = "get_sysinfo"

# DeviceCipherType values (c_opt). The app picks the STRONGEST supported (MD5 > BASE64).
CIPHER_BASE64: Final = 0
CIPHER_MD5: Final = 1
# Which cipher this camera accepted, stored on the entry so the secret can be RE-DERIVED
# from the account password on every start. Storing the derived secret instead would break
# self-healing when the user changes their TP-Link password.
CONF_CIPHER: Final = "cipher"
DEFAULT_CIPHER: Final = "md5"

# ── Optional per-model modules (probed once at setup; absent modules are skipped) ──
LINKIE_SWITCH_MODULE: Final = "smartlife.cam.ipcamera.switch"  # privacy / camera on-off
LINKIE_LED_MODULE: Final = "smartlife.cam.ipcamera.led"
LINKIE_DND_MODULE: Final = "smartlife.cam.ipcamera.dndSchedule"
LINKIE_BATTERY_MODULE: Final = "smartlife.cam.ipcamera.battery"
LINKIE_SIREN_MODULE: Final = "smartlife.cam.ipcamera.siren"
# Getter used to probe each of the above.
# Device error codes. -10008 = unknown module/method (genuinely unsupported). Anything else
# (e.g. -40700 "parameter error") means the method EXISTS but we called it without params,
# which still proves support — see LocalLinkieClient.async_supports.
ERR_UNKNOWN_METHOD: Final = -10008
LINKIE_PROBE_METHODS: Final = {
    LINKIE_SWITCH_MODULE: "get_is_enable",
    LINKIE_LED_MODULE: "get_status",
    LINKIE_DND_MODULE: "get_dnd_enable",
    LINKIE_BATTERY_MODULE: "get_status",
    LINKIE_SIREN_MODULE: "get_state",
}

# How often to refresh sysinfo (identity is static; the switch/rssi state is not).
DEFAULT_SYSINFO_INTERVAL: Final = 60  # seconds
# Local motion poll cadence (fast — it's LAN-local). Auto-enabled when a stream secret is set.
CONF_LOCAL_MOTION_INTERVAL: Final = "local_motion_interval"
DEFAULT_LOCAL_MOTION_INTERVAL: Final = 2  # seconds
MIN_LOCAL_MOTION_INTERVAL: Final = 1
MAX_LOCAL_MOTION_INTERVAL: Final = 30
# Consecutive local-poll failures before we declare local motion unavailable (e.g. the
# account password changed, or the camera went off-LAN).
LOCAL_MOTION_FAILURE_THRESHOLD: Final = 5

# ── Local TYPED detections (Linkie, delayed but local) ───────────────────────
# get_detect_zone_list returns finalized detection clips with an `alarm_type`: RING (doorbell
# press!), PD (person), MD (motion). A clip only appears once it finalizes (~1-2 min after the
# event ends), so these are local + correctly typed but delayed. The instant motion path stays
# on get_detect_event_state; this list is what the recordings card browses.
LINKIE_DETECT_METHOD: Final = "get_detect_zone_list"
ALARM_MOTION: Final = "MD"
ALARM_SOUND: Final = "AD"  # audio detection
ALARM_BABY_CRY: Final = "BCD"
# What the recordings browser calls each clip. NOTE these name the RECORDING TRIGGER, not an
# AI classification — a person in frame is often logged "MD". Labels stay literal for that
# reason ("Person detected" would overclaim).
ALARM_LABELS: Final = {
    "RING": "Doorbell ring",
    "PD": "Person",
    ALARM_MOTION: "Motion",
    ALARM_SOUND: "Sound",
    ALARM_BABY_CRY: "Baby cry",
}

# ── Local video (port 19443) — fully local ───────────────────────────────────
STREAM_PORT: Final = 19443
# The app picks among these by connection type; which one a model listens on varies, so the
# coordinator TCP-probes them in order at setup. 19443 is correct for the KD110.
STREAM_PORT_CANDIDATES: Final = (19443, 18443, 17443)
STREAM_PATH: Final = "/https/stream/mixed"

# ── Recorded playback (VOD) — verified against real footage ──────────────────
# Playback is a SEPARATE port + path, and the seek is just a query param
# (`start_time=<epoch seconds>`), plus an X-PLAYER-ID header. The Linkie
# vod.set_start_playback calls are NOT the seek mechanism — they are no-ops on their own.
VOD_PORT: Final = 17443
VOD_PATH: Final = "/vod/data/mixstream"
# Oldest/newest second still on the SD card ({start_time, end_time}); the card is a ring
# buffer, so this window slides. Takes no params. Bounds the recordings browser's day list.
LINKIE_RECORD_RANGE_METHOD: Final = "get_record_zone_range"
# Start each clip slightly early: the seek lands on the nearest keyframe, and a couple of
# seconds of lead-in means the event itself isn't already over when the picture appears.
PLAYBACK_PREROLL_SECONDS: Final = 5
DEFAULT_VIDEO_CODEC: Final = "h264"
DEFAULT_AUDIO_CODEC: Final = "g711"  # camera-side codec; transcoded to aac for browsers
DEFAULT_RESOLUTION: Final = "hd"  # the device wants it lowercase

# ── Motion sensor safety clear ───────────────────────────────────────────────
# The motion poll can stop answering mid-detection (camera busy, briefly off-LAN). The sensor
# then clears itself instead of latching on forever; the window re-arms on every reading, so
# continuous motion keeps it `on`. User-tunable via the options flow.
DEFAULT_MOTION_RESET_SECONDS: Final = 30
MIN_MOTION_RESET_SECONDS: Final = 5
MAX_MOTION_RESET_SECONDS: Final = 600
CONF_MOTION_RESET: Final = "motion_reset_seconds"
