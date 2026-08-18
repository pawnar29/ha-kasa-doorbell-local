"""Unit tests for the local protocol: auth encoding, the Linkie cipher, sysinfo parsing.

No Home Assistant and no camera needed — ``api.py`` is loaded standalone with its relative
import stubbed out. It does import aiohttp, so that must be installed.

Run directly (``python3 tests/test_api.py``) or under pytest.
"""

import base64
import hashlib
import importlib.util
import sys
from pathlib import Path

# Load api.py standalone so the test runs without Home Assistant installed.
_API = Path(__file__).resolve().parents[1] / "custom_components" / "kasa_doorbell" / "api.py"
_CONST = _API.parent / "const.py"

# Stub the relative import (`from .const import …`) that api.py does.
_pkg = "kasa_doorbell_stub"
sys.modules[_pkg] = type(sys)(_pkg)
sys.modules[_pkg].__path__ = [str(_API.parent)]
spec_c = importlib.util.spec_from_file_location(f"{_pkg}.const", _CONST)
const = importlib.util.module_from_spec(spec_c)
sys.modules[f"{_pkg}.const"] = const
spec_c.loader.exec_module(const)
spec_a = importlib.util.spec_from_file_location(f"{_pkg}.api", _API)
api = importlib.util.module_from_spec(spec_a)
api.__package__ = _pkg
sys.modules[f"{_pkg}.api"] = api  # required so @dataclass(slots=True) can resolve types
spec_a.loader.exec_module(api)

EMAIL = "user@example.com"
SECRET = hashlib.md5(b"example-password").hexdigest()  # noqa: S324 — device protocol


# ── Auth ─────────────────────────────────────────────────────────────────────
def test_secure_password_applies_the_device_cipher():
    assert api.secure_password("example-password", "md5") == SECRET
    assert api.secure_password("example-password", "base64") == "ZXhhbXBsZS1wYXNzd29yZA=="


def test_basic_auth_uses_the_raw_email_and_the_secured_password():
    # The header carries the literal '@' — the %40 form is only for URL userinfo, and the
    # camera rejects it. This is the single most fragile detail of the local auth.
    header = api.build_basic_auth_header(EMAIL, SECRET)
    assert header.startswith("Basic ")
    decoded = base64.b64decode(header.removeprefix("Basic ")).decode()
    assert decoded == f"{EMAIL}:{SECRET}"


def test_basic_auth_does_not_url_encode():
    assert "%40" not in base64.b64decode(
        api.build_basic_auth_header(EMAIL, SECRET).removeprefix("Basic ")
    ).decode()


# ── Stream URL ───────────────────────────────────────────────────────────────
def test_stream_url_shape():
    assert api.build_stream_url("10.0.0.5") == (
        "https://10.0.0.5:19443/https/stream/mixed"
        "?video=h264&audio=g711&resolution=hd"
    )


# ── Linkie control channel ───────────────────────────────────────────────────
def test_linkie_xor_round_trips():
    payload = b'{"system":{"get_sysinfo":{}}}'
    assert api._linkie_unxor(api._linkie_xor(payload)) == payload


def test_linkie_xor_is_the_autokey_variant_seeded_0xab():
    # Each output byte keys the next one; a plain repeating-key XOR would not match.
    assert api._linkie_xor(b"\x00\x00\x00") == bytes([0xAB, 0xAB, 0xAB])
    assert api._linkie_xor(b"AB") == bytes([0x41 ^ 0xAB, 0x42 ^ (0x41 ^ 0xAB)])


# ── sysinfo parsing ──────────────────────────────────────────────────────────
_SYSINFO = {
    "system": {
        "deviceId": "0" * 40,
        "model": "KD110D(US)",
        "alias": "Front door",
        "dev_name": "Kasa Smart Video Doorbell",
        "mac": "AA:BB:CC:DD:EE:FF",
        "hw_ver": "1.0",
        "sw_ver": "1.2.3 Build 000000 Rel.000000",
        "c_opt": [0, 1],
        "new_feature": [2, 3, 4, 5, 9],
        "led_status": "on",
        "camera_switch": "on",
        "dnd_switch": "off",
        "rssi": -52,
        "resolution": "hd",
        "updating": 0,
        "last_activity_timestamp": 1700000000,
    }
}


def test_sysinfo_is_read_from_the_nested_system_key():
    info = api.camera_info_from_sysinfo(_SYSINFO)
    assert info.device_id == "0" * 40
    assert info.model == "KD110D(US)"
    assert info.model_family == "KD110D"
    assert info.alias == "Front door"
    assert info.rssi == -52


def test_switch_states_decode_from_on_off_strings():
    info = api.camera_info_from_sysinfo(_SYSINFO)
    assert info.led_on is True
    assert info.camera_on is True
    assert info.dnd_on is False


def test_missing_switch_state_is_unknown_not_false():
    # None means "this model has no such switch"; False would create a dead entity.
    info = api.camera_info_from_sysinfo({"system": {"model": "KC400"}})
    assert info.led_on is None and info.dnd_on is None


def test_cipher_picks_the_strongest_the_device_offers():
    # c_opt [0,1] = BASE64 + MD5 supported -> md5, mirroring DeviceCipherHelper.
    assert api.camera_info_from_sysinfo(_SYSINFO).cipher == "md5"
    base64_only = {"system": {"c_opt": [0]}}
    assert api.camera_info_from_sysinfo(base64_only).cipher == "base64"
    assert api.camera_info_from_sysinfo({"system": {}}).cipher == "md5"


def test_features_decode_to_the_capability_set():
    info = api.camera_info_from_sysinfo(_SYSINFO)
    assert info.supports(5) and not info.supports(1)


# ── Error semantics ──────────────────────────────────────────────────────────
def test_only_unknown_method_means_unsupported():
    # -40700 is "parameter error": the method EXISTS, we just called it bare. Treating any
    # error as unsupported once silently deleted the motion sensor.
    assert const.ERR_UNKNOWN_METHOD == -10008
    assert api.LinkieError("nope", -40700).err_code == -40700
    assert issubclass(api.LinkieAuthError, api.LinkieError)


def test_clip_labels_name_the_trigger_for_every_known_alarm_type():
    assert const.ALARM_LABELS["RING"] == "Doorbell ring"
    assert set(const.ALARM_LABELS) == {"RING", "PD", "MD", "AD", "BCD"}


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as err:
                failures += 1
                print(f"FAIL {name}: {err}")
            else:
                print(f"PASS {name}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
