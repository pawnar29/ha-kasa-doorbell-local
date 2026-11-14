"""Local control client for Kasa cameras and doorbells.

Everything here talks to the camera on the LAN — there is no cloud path anywhere in this
integration.

  * ``secure_password`` / ``build_basic_auth_header`` — the device's auth scheme: HTTP Basic
    with the account email and the account password run through the device's cipher.
  * ``LocalLinkieClient`` — the JSON control channel on port 10443 (XOR-obfuscated body over
    TLS): sysinfo, capabilities, motion state, recorded-clip listings, switch setters.
  * URL/port helpers for the video ports; the video itself lives in ``stream.py``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import ssl
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    from aiohttp import ClientSession

from .const import (
    CIPHER_BASE64,
    ERR_UNKNOWN_METHOD,
    DEFAULT_AUDIO_CODEC,
    DEFAULT_RESOLUTION,
    DEFAULT_VIDEO_CODEC,
    LINKIE_DETECT_METHOD,
    LINKIE_MOTION_METHOD,
    LINKIE_PATH,
    LINKIE_SYSINFO_METHOD,
    LINKIE_SYSTEM_MODULE,
    LINKIE_PORT,
    LINKIE_RECORD_RANGE_METHOD,
    LINKIE_VOD_MODULE,
    LINKIE_XOR_SEED,
    STREAM_PATH,
    STREAM_PORT,
)

_LOGGER = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Local video — SOLVED, no cloud dependency
# ─────────────────────────────────────────────────────────────────────────────
def secure_password(password: str, cipher: str = "md5") -> str:
    """Apply the device cipher to the raw password.

    Mirrors DeviceCipherHelper.secureContent (DeviceCipherType):
      * MD5   -> lowercase hex md5 digest
      * BASE64-> base64 of the raw password
    The KD110 uses MD5 (its stream secret is a 32-hex md5 digest).
    """
    if cipher == "md5":
        return hashlib.md5(password.encode()).hexdigest()  # noqa: S324 (device protocol)
    return base64.b64encode(password.encode()).decode()


def build_basic_auth_header(email: str, secured_password: str) -> str:
    """Return the exact ``Authorization: Basic …`` value the KD110 requires.

    Recipe recovered from DeviceCipherHelper.getBasicAuthorizationHeaderCredentials:
    ``Basic base64( username ":" secured_password )``
      * username = the account email, used **raw** — the literal '@', not %40
      * secured_password = the password already run through the device cipher (for the
        KD110 that is the 32-hex md5 digest)
      * URL-embedded ``user:pass@host`` does NOT work — the header is required
    """
    userinfo = f"{email}:{secured_password}"
    token = base64.b64encode(userinfo.encode()).decode()
    return f"Basic {token}"


async def async_probe_stream_port(host: str, candidates: tuple[int, ...]) -> int:
    """Return the first stream port the camera accepts a TCP connection on.

    The app selects among 17443/18443/19443 by connection type, and which one a given model
    listens on varies. A plain TCP connect is cheap and reliable; falls back to the first
    candidate if none answer (so behaviour is unchanged when the probe can't run).
    """
    for port in candidates:
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=3
            )
        except Exception:  # noqa: BLE001 — closed/filtered port, try the next
            continue
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return port
    return candidates[0]


def build_stream_url(
    host: str,
    *,
    port: int = STREAM_PORT,
    video: str = DEFAULT_VIDEO_CODEC,
    audio: str = DEFAULT_AUDIO_CODEC,
    resolution: str = DEFAULT_RESOLUTION,
) -> str:
    """Return the raw HTTPS multipart stream URL."""
    return (
        f"https://{host}:{port}{STREAM_PATH}"
        f"?video={video}&audio={audio}&resolution={resolution}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Local motion — Linkie :10443, real-time, no cloud (SOLVED)
# ─────────────────────────────────────────────────────────────────────────────
def _linkie_xor(data: bytes) -> bytes:
    """Classic Kasa autokey XOR, seed 0xAB, keyless — symmetric for encrypt/decrypt input."""
    key, out = LINKIE_XOR_SEED, bytearray()
    for b in data:
        key = b ^ key
        out.append(key)
    return bytes(out)


def _linkie_unxor(data: bytes) -> bytes:
    """Inverse autokey XOR (the previous ciphertext byte is the next key)."""
    key, out = LINKIE_XOR_SEED, bytearray()
    for c in data:
        out.append(c ^ key)
        key = c
    return bytes(out)


def build_linkie_ssl_context() -> ssl.SSLContext:
    """SSL context for the camera's self-signed cert + legacy TLS ciphers.

    Blocking (loads system certs) — build once in an executor, never in the event loop.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.set_ciphers("DEFAULT@SECLEVEL=0")
    except ssl.SSLError:  # pragma: no cover - depends on the OpenSSL build
        ctx.set_ciphers("DEFAULT@SECLEVEL=1")
    return ctx


@dataclass(slots=True)
class KasaCameraInfo:
    """Identity + capabilities of a Kasa camera, read locally from ``system.get_sysinfo``.

    This is everything setup needs — model, deviceId, alias, MAC, firmware, the supported
    cipher list and the feature bitset — so the integration never has to touch the cloud.
    Verified against the KD110: ``new_feature`` decodes to exactly the same capability set the
    cloud reports in ``deviceContext.deviceNewFeature``.
    """

    device_id: str
    model: str  # e.g. "KD110(US)"
    alias: str  # user's name for it, e.g. "Doorbell"
    dev_name: str  # marketing name, e.g. "Kasa Doorbell Cam"
    mac: str
    hw_ver: str
    sw_ver: str
    ciphers: tuple[int, ...]  # c_opt — DeviceCipherType values (0=BASE64, 1=MD5)
    features: tuple[int, ...]  # new_feature — SysInfoNewFeatureMode values
    led_on: bool | None
    camera_on: bool | None  # camera_switch — privacy off/on
    dnd_on: bool | None
    rssi: int | None
    resolution: str
    updating: bool
    last_activity: int | None
    raw: dict

    @property
    def model_family(self) -> str:
        """Bare model without the region suffix — "KD110(US)" -> "KD110"."""
        return self.model.split("(", 1)[0].strip() or self.model

    @property
    def cipher(self) -> str:
        """The cipher to secure the local password with.

        Mirrors ``DeviceCipherHelper.resolveCipherType``: sort the supported types and take
        the strongest (MD5 > BASE64). Defaults to md5, which every modern model supports.
        """
        if self.ciphers and max(self.ciphers) == CIPHER_BASE64:
            return "base64"
        return "md5"

    def supports(self, feature: int) -> bool:
        """True if the device advertises a SysInfoNewFeatureMode value."""
        return feature in self.features


def camera_info_from_sysinfo(result: dict) -> KasaCameraInfo:
    """Parse a ``system.get_sysinfo`` response into KasaCameraInfo.

    The payload nests the interesting part under a second ``system`` key.
    """
    sysinfo = result.get("system") if isinstance(result.get("system"), dict) else result

    def _flag(value) -> bool | None:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() == "on"
        return None

    def _ints(value) -> tuple[int, ...]:
        if not isinstance(value, list):
            return ()
        return tuple(v for v in value if isinstance(v, int))

    return KasaCameraInfo(
        device_id=str(sysinfo.get("deviceId") or ""),
        model=str(sysinfo.get("model") or ""),
        alias=str(sysinfo.get("alias") or ""),
        dev_name=str(sysinfo.get("dev_name") or ""),
        mac=str(sysinfo.get("mac") or sysinfo.get("mic_mac") or ""),
        hw_ver=str(sysinfo.get("hw_ver") or ""),
        sw_ver=str(sysinfo.get("sw_ver") or ""),
        ciphers=_ints(sysinfo.get("c_opt")),
        features=_ints(sysinfo.get("new_feature")),
        led_on=_flag(sysinfo.get("led_status")),
        camera_on=_flag(sysinfo.get("camera_switch")),
        dnd_on=_flag(sysinfo.get("dnd_switch")),
        rssi=sysinfo.get("rssi") if isinstance(sysinfo.get("rssi"), int) else None,
        resolution=str(sysinfo.get("resolution") or ""),
        updating=bool(sysinfo.get("updating")),
        last_activity=(
            sysinfo.get("last_activity_timestamp")
            if isinstance(sysinfo.get("last_activity_timestamp"), int)
            else None
        ),
        raw=sysinfo,
    )


class LocalLinkieClient:
    """Talk to a Kasa camera's LAN Linkie channel (fully local, no cloud).

    Auth = HTTP Basic (email : secured password), where the password is secured with the
    cipher the device advertises in ``get_sysinfo.c_opt`` (md5 on modern models, base64 on
    older ones) — the same credential the local video stream uses.
    """

    def __init__(
        self, host: str, email: str, secret: str, session: ClientSession,
        ssl_context: ssl.SSLContext,
    ) -> None:
        self._host = host
        self._email = email
        self._secret = secret
        self._session = session
        self._ssl = ssl_context
        self._url = f"https://{host}:{LINKIE_PORT}{LINKIE_PATH}"

    def _auth_header(self) -> str:
        token = base64.b64encode(f"{self._email}:{self._secret}".encode()).decode()
        return f"Basic {token}"

    async def _async_call(
        self, module: str, method: str, params: dict | None = None
    ) -> dict:
        """Send one XOR-encrypted Linkie command and return its result dict.

        Works for any module — note ``system.get_sysinfo`` uses the bare module key
        ``"system"``, not ``smartlife.cam.ipcamera.system``.

        Raises LinkieError on transport failure, bad auth, or a device err_code.
        """
        payload = {
            "context": {"source": str(uuid.uuid4())},
            module: {method: params if params is not None else {}},
        }
        body = json.dumps(payload, separators=(",", ":")).encode()
        content = base64.b64encode(_linkie_xor(body)).decode()
        headers = {
            "Authorization": self._auth_header(),
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "Accept": "*/*",
        }
        try:
            async with self._session.post(
                self._url, data={"content": content}, headers=headers, ssl=self._ssl
            ) as resp:
                if resp.status != 200:
                    raise LinkieAuthError(f"HTTP {resp.status}")
                raw = await resp.read()
        except LinkieError:
            raise
        except Exception as err:  # noqa: BLE001 — network/TLS
            raise LinkieError(f"request failed: {type(err).__name__}") from err

        try:
            clear = _linkie_unxor(base64.b64decode(raw))
            data = json.loads(clear.decode("utf-8", "replace"))
            result = data[module][method]
        except Exception as err:  # noqa: BLE001 — malformed body
            raise LinkieError(f"bad response: {type(err).__name__}") from err
        ec = result.get("err_code")
        if ec != 0:
            raise LinkieError(
                f"device err_code {ec}: {result.get('err_msg', '')}", err_code=ec
            )
        return result

    async def _async_vod(self, method: str, params: dict) -> dict:
        """Shorthand for a call against the VOD module."""
        return await self._async_call(LINKIE_VOD_MODULE, method, params)

    async def async_get_sysinfo(self) -> KasaCameraInfo:
        """Identity + capabilities, read locally. The basis for model-agnostic setup."""
        result = await self._async_call(LINKIE_SYSTEM_MODULE, LINKIE_SYSINFO_METHOD)
        return camera_info_from_sysinfo(result)

    async def async_supports(self, module: str, method: str) -> bool:
        """Probe whether a module/method exists on this model.

        We deliberately call with NO params, so a supported method often replies with a
        parameter error (e.g. get_detect_event_state needs a `timestamp`). That still proves
        the method EXISTS — only ERR_UNKNOWN_METHOD means the model lacks it. Getting this
        wrong silently drops entities, which is exactly what happened to the motion sensor.
        """
        try:
            await self._async_call(module, method)
        except LinkieAuthError:
            raise
        except LinkieError as err:
            return err.err_code not in (None, ERR_UNKNOWN_METHOD)
        return True

    async def async_get(self, module: str, method: str) -> dict:
        """Read any module getter (battery status, siren state, …)."""
        return await self._async_call(module, method)

    async def async_set(self, module: str, method: str, params: dict) -> None:
        """Fire a setter (LED, privacy, DND, …) and discard the empty result."""
        await self._async_call(module, method, params)

    async def async_get_motion_state(self) -> dict:
        """Real-time motion state (is_moving/is_event_finish/is_video_ready) — instant."""
        return await self._async_vod(LINKIE_MOTION_METHOD, {"timestamp": int(time.time())})

    async def async_get_detections(self, start_ts: int, end_ts: int) -> list[dict]:
        """Typed detection clips in [start_ts, end_ts]: {start_time, end_time, alarm_type}.

        alarm_type: RING (doorbell press), PD (person), MD (motion). A clip only appears
        once it finalizes (~1-2 min after the event), so these events are local + typed but
        delayed, unlike the instant get_detect_event_state motion state.
        """
        result = await self._async_vod(
            LINKIE_DETECT_METHOD, {"start_time": start_ts, "end_time": end_ts}
        )
        return result.get("list") or []

    async def async_get_record_range(self) -> tuple[int, int] | None:
        """(oldest, newest) recorded epoch second on the SD card, or None if there is none.

        The card is a ring buffer, so this window slides — always re-read it rather than
        caching, and clamp any seek into it.
        """
        result = await self._async_vod(LINKIE_RECORD_RANGE_METHOD, {})
        start, end = result.get("start_time"), result.get("end_time")
        if not isinstance(start, int) or not isinstance(end, int) or end <= start:
            return None
        return start, end


class LinkieError(RuntimeError):
    """A local Linkie request failed (transport, auth, or device error).

    ``err_code`` carries the device's own code when there was one, so callers can tell an
    unknown module/method apart from a valid one that was simply called wrong.
    """

    def __init__(self, message: str, err_code: int | None = None) -> None:
        super().__init__(message)
        self.err_code = err_code


class LinkieAuthError(LinkieError):
    """The device rejected the credentials (HTTP 401) — wrong password or wrong cipher.

    Distinguished from a plain LinkieError so the config flow can retry with the other
    cipher instead of treating it as "device unreachable", and so capability probing never
    mistakes an auth failure for an unsupported module.
    """


