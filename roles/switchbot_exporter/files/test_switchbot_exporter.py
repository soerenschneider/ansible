"""Tests for switchbot_exporter.

Replaces the old --selftest mode. Everything here runs without a Bluetooth
adapter: the BLE scanner is replaced with a fake that emits advertisements on a
schedule, and metrics go to a fresh registry per test.

    pip install pytest
    pytest -q
    pytest -q -k config          # just the configuration cases
    pytest -q -m slow            # the timing-sensitive duty-cycle cases

Every case here exists because something actually broke. Notably:
  * test_defaults_when_config_is_empty        — slots=True made Config.listen a
                                                descriptor, not the default
  * test_scanner_up_is_false_on_silent_window — v1 reported up=1 while receiving
                                                nothing at all
  * test_unconfigured_switchbot_is_still_seen  — a mistyped MAC was
                                                indistinguishable from a dead radio
  * test_no_pressure_metric                   — no SwitchBot has a barometer
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Sequence

import pytest
from prometheus_client import CollectorRegistry

# The exporter ships as a standalone script rather than a package, so load it by
# path. It must be registered in sys.modules before execution or dataclasses
# cannot resolve __module__.
_MODULE_PATH = Path(__file__).with_name("switchbot_exporter_v3.py")
_spec = importlib.util.spec_from_file_location("switchbot_exporter", _MODULE_PATH)
assert _spec and _spec.loader
sb = importlib.util.module_from_spec(_spec)
sys.modules["switchbot_exporter"] = sb
_spec.loader.exec_module(sb)


# --------------------------------------------------------------------------- #
# Fixtures and fakes
# --------------------------------------------------------------------------- #

VENDOR_MFR = "aabbccddeeff360302963700"  # documented W3400010 sample
VENDOR_SVC = "770064"

GARDEN = "AA:BB:CC:DD:EE:FF"
SHED = "11:22:33:44:55:66"
STRANGER = "EB:6B:01:C6:2B:2C"


def advertisement(mfr_hex: str = VENDOR_MFR, svc_hex: str = VENDOR_SVC,
                  rssi: int = -58) -> Any:
    """An AdvertisementData shaped like a real SwitchBot frame.

    service_uuids is empty on purpose: these devices advertise Service DATA for
    0xFD3D and no Service Class UUID list, which is why passing service_uuids to
    BleakScanner silently drops every frame.
    """
    from bleak.backends.scanner import AdvertisementData
    return AdvertisementData(
        local_name="Meter",
        manufacturer_data={sb.SWITCHBOT_COMPANY_ID: bytes.fromhex(mfr_hex)},
        service_data={sb.SWITCHBOT_SERVICE_UUID: bytes.fromhex(svc_hex)},
        service_uuids=[], tx_power=None, rssi=rssi, platform_data=())


class FakeScanner:
    """Stands in for BleakScanner, emitting advertisements on a schedule."""

    def __init__(self, callback: Any, schedule: Sequence[tuple[float, str]]) -> None:
        self._callback = callback
        self._schedule = schedule
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> "FakeScanner":
        self._task = asyncio.create_task(self._emit())
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _emit(self) -> None:
        from bleak.backends.device import BLEDevice
        for delay, mac in self._schedule:
            await asyncio.sleep(delay)
            self._callback(BLEDevice(address=mac, name="Meter", details={}),
                           advertisement())


def factory(schedule: Sequence[tuple[float, str]]) -> Any:
    return lambda callback: FakeScanner(callback, schedule)


@pytest.fixture
def registry() -> CollectorRegistry:
    """A clean registry per test, so cases cannot leak state into each other."""
    return CollectorRegistry()


@pytest.fixture
def metrics(registry: CollectorRegistry) -> Any:
    return sb.Metrics(registry)


def value(registry: CollectorRegistry, metric: str, **labels: str) -> Any:
    return registry.get_sample_value(metric, labels or None)


def run(coro: Any) -> Any:
    """Run an async test body without needing pytest-asyncio."""
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Decoder
# --------------------------------------------------------------------------- #

class TestDecoder:
    def test_vendor_sample_frame(self) -> None:
        reading = sb.decode_advertisement(GARDEN, advertisement(), now=1_700_000_000.0)
        assert reading is not None
        assert reading.temperature_c == 22.2
        assert reading.humidity_pct == 55
        assert reading.battery_pct == 100
        assert reading.model == "outdoor_meter"

    @pytest.mark.parametrize("mfr, expected", [
        ("aabbccddeeff360302963700", 22.2),   # sign bit set -> positive
        ("aabbccddeeff36030503 2a00".replace(" ", ""), -3.5),  # sign bit clear
        ("aabbccddeeff36030080630b", 0.0),    # zero, sign bit set
    ])
    def test_temperature_sign_and_fraction(self, mfr: str, expected: float) -> None:
        reading = sb.decode_advertisement(GARDEN, advertisement(mfr_hex=mfr))
        assert reading is not None
        assert reading.temperature_c == expected

    def test_humidity_high_bit_is_masked(self) -> None:
        reading = sb.decode_advertisement(
            GARDEN, advertisement(mfr_hex="aabbccddeeff36030296b700"))
        assert reading is not None
        assert reading.humidity_pct == 55

    @pytest.mark.parametrize("svc, model", [
        ("540064029637", "meter"),
        ("690064029637", "meter_plus"),
        ("340064029637", "meter_pro"),
        ("350064029637", "meter_pro_co2"),
    ])
    def test_service_data_models(self, svc: str, model: str) -> None:
        from bleak.backends.scanner import AdvertisementData
        adv = AdvertisementData(
            local_name=None, manufacturer_data={},
            service_data={sb.SWITCHBOT_SERVICE_UUID: bytes.fromhex(svc)},
            service_uuids=[], tx_power=None, rssi=-60, platform_data=())
        reading = sb.decode_advertisement(GARDEN, adv)
        assert reading is not None
        assert reading.model == model
        assert (reading.temperature_c, reading.humidity_pct) == (22.2, 55)

    def test_non_switchbot_is_ignored_not_an_error(self) -> None:
        from bleak.backends.scanner import AdvertisementData
        adv = AdvertisementData(
            local_name="something else", manufacturer_data={0x004C: b"\x02\x15"},
            service_data={}, service_uuids=[], tx_power=None, rssi=-70,
            platform_data=())
        assert sb.decode_advertisement(GARDEN, adv) is None

    @pytest.mark.parametrize("svc", [
        "540064",              # truncated
        "54006400ff37",        # +127 C
        "54006402967a",        # 122 %rh
    ])
    def test_implausible_frames_are_rejected(self, svc: str) -> None:
        from bleak.backends.scanner import AdvertisementData
        adv = AdvertisementData(
            local_name=None, manufacturer_data={},
            service_data={sb.SWITCHBOT_SERVICE_UUID: bytes.fromhex(svc)},
            service_uuids=[], tx_power=None, rssi=-60, platform_data=())
        with pytest.raises(sb.DecodeError):
            sb.decode_advertisement(GARDEN, adv)

    def test_derived_quantities(self) -> None:
        reading = sb.Reading(GARDEN, "outdoor_meter", 22.2, 55, 100, -58, 0.0)
        assert round(reading.dew_point_c, 1) == 12.7
        assert round(reading.absolute_humidity_g_m3, 1) == 10.8

    def test_saturated_air_dew_point_equals_temperature(self) -> None:
        reading = sb.Reading(GARDEN, "meter", 15.0, 100, None, None, 0.0)
        assert abs(reading.dew_point_c - 15.0) < 0.05

    def test_zero_humidity_does_not_produce_nan(self) -> None:
        import math
        reading = sb.Reading(GARDEN, "meter", 30.0, 0, None, None, 0.0)
        assert math.isfinite(reading.dew_point_c)
        assert reading.absolute_humidity_g_m3 == 0.0


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

class TestConfig:
    def test_shipped_example_is_valid(self) -> None:
        config, warnings = sb.parse_config(sb.EXAMPLE_CONFIG)
        assert config.port == 9865
        assert config.host == "0.0.0.0"
        assert len(config.devices) == 2
        assert config.scan.interval_seconds == 60
        assert warnings == []

    def test_defaults_when_config_is_empty(self) -> None:
        # Regression: with slots=True, Config.listen is a descriptor rather than
        # the default string, so an empty config used to be rejected outright.
        config, _ = sb.parse_config({})
        assert config.listen == sb.DEFAULT_LISTEN
        assert config.log_level == sb.DEFAULT_LOG_LEVEL
        assert config.scan.mode == "active"
        assert config.scan.interval_seconds == 0.0
        assert config.devices == ()

    def test_underscore_keys_are_ignored(self) -> None:
        config, _ = sb.parse_config(
            {"_note": "JSON has no comments", "listen": ":9999",
             "scan": {"_why": "note", "interval_seconds": 30}})
        assert config.port == 9999
        assert config.host == "0.0.0.0"

    @pytest.mark.parametrize("raw_mac", [
        "eb-6b-01-c6-2b-2c", "EB:6B:01:C6:2B:2C", "  eb:6b:01:c6:2b:2c  "])
    def test_mac_normalisation(self, raw_mac: str) -> None:
        config, _ = sb.parse_config({"devices": [{"mac": raw_mac, "name": "garden"}]})
        assert config.devices[0].mac == "EB:6B:01:C6:2B:2C"

    def test_name_defaults_to_mac(self) -> None:
        config, _ = sb.parse_config({"devices": [{"mac": GARDEN}]})
        assert config.devices[0].name == GARDEN

    def test_all_problems_reported_at_once(self) -> None:
        # Fixing one typo per restart is miserable, so validation accumulates.
        with pytest.raises(sb.ConfigError) as excinfo:
            sb.parse_config({"lisen": ":9865", "scan": {"window_seconds": -5},
                             "devices": [{"mac": "nope", "name": "x"}]})
        assert len(excinfo.value.problems) == 3

    @pytest.mark.parametrize("raw, fragment", [
        ({"scanning": {}}, "unknown key 'scanning'"),
        ({"listen": "9865"}, "listen must look like"),
        ({"listen": ":99999"}, "out of range"),
        ({"log": {"level": "verbose"}}, "log.level must be one of"),
        ({"log": {"lvl": "info"}}, "unknown key log."),
        ({"scan": {"mode": "sniff"}}, "scan.mode must be one of"),
        ({"scan": {"window_seconds": 0}}, "must be > 0"),
        ({"scan": {"interval_seconds": -1}}, "must be >= 0"),
        ({"scan": {"window_seconds": "25"}}, "must be a number"),
        ({"scan": {"max_empty_windows": 0}}, "integer >= 1"),
        ({"devices": {}}, "devices must be an array"),
        ({"devices": ["nope"]}, "must be an object"),
        ({"devices": [{"name": "x"}]}, "mac is required"),
        ({"devices": [{"mac": "zz:zz:zz:zz:zz:zz"}]}, "is not a MAC address"),
        ({"devices": [{"mac": GARDEN, "nme": "typo"}]}, "unknown key devices[0]"),
        ({"devices": [{"mac": GARDEN, "name": ""}]}, "non-empty string"),
    ])
    def test_rejects_bad_config(self, raw: dict, fragment: str) -> None:
        with pytest.raises(sb.ConfigError) as excinfo:
            sb.parse_config(raw)
        assert any(fragment in p for p in excinfo.value.problems), excinfo.value.problems

    def test_duplicate_mac_rejected(self) -> None:
        with pytest.raises(sb.ConfigError, match="configuration problem"):
            sb.parse_config({"devices": [{"mac": GARDEN, "name": "a"},
                                         {"mac": GARDEN.lower(), "name": "b"}]})

    def test_duplicate_name_rejected(self) -> None:
        # Two sensors sharing a name makes every legend and annotation ambiguous.
        with pytest.raises(sb.ConfigError):
            sb.parse_config({"devices": [{"mac": GARDEN, "name": "garden"},
                                         {"mac": SHED, "name": "garden"}]})

    @pytest.mark.parametrize("mode", sb.SCAN_MODES)
    def test_all_scan_modes_accepted(self, mode: str) -> None:
        config, _ = sb.parse_config({"scan": {"mode": mode}})
        assert config.scan.mode == mode

    def test_scan_mode_is_case_insensitive(self) -> None:
        config, _ = sb.parse_config({"scan": {"mode": "PASSIVE"}})
        assert config.scan.mode == "passive"

    def test_warnings_do_not_block_startup(self) -> None:
        _, warnings = sb.parse_config(
            {"scan": {"interval_seconds": 60, "window_seconds": 90,
                      "stale_after_seconds": 100}})
        joined = " | ".join(warnings)
        assert "will be clamped" in joined
        assert "three scan intervals" in joined
        assert "no devices listed" in joined

    def test_json_syntax_error_names_the_line(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text('{"listen": ":9865",}\n')
        with pytest.raises(sb.ConfigError) as excinfo:
            sb.load_config(path)
        problem = excinfo.value.problems[0]
        assert "invalid JSON at line 1" in problem
        assert "no comments" in problem      # nudge toward the _key convention

    def test_missing_file_is_an_error_when_explicitly_named(self, tmp_path: Path) -> None:
        with pytest.raises(sb.ConfigError, match="configuration problem"):
            sb.load_config(tmp_path / "absent.json")

    def test_roundtrip_of_the_example_through_json(self, tmp_path: Path) -> None:
        path = tmp_path / "config.json"
        path.write_text(json.dumps(sb.EXAMPLE_CONFIG))
        config, loaded_from, warnings = sb.load_config(path)
        assert loaded_from == path
        assert len(config.devices) == 2
        assert warnings == []


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #

class TestMetrics:
    def test_registries_are_isolated(self) -> None:
        first, second = sb.Metrics(CollectorRegistry()), sb.Metrics(CollectorRegistry())
        first.scanner_up.set(1)
        assert value(first.registry, "switchbot_scanner_up") == 1.0
        assert value(second.registry, "switchbot_scanner_up") == 0.0

    def test_no_pressure_metric(self, metrics: Any, registry: CollectorRegistry) -> None:
        # No SwitchBot meter has a barometer. Removed in v3; this stops it
        # reappearing by accident.
        names = {m.name for m in registry.collect()}
        assert "switchbot_pressure_hpa" not in names

    def test_publish_sets_expected_series(self, metrics: Any,
                                          registry: CollectorRegistry) -> None:
        reading = sb.Reading(GARDEN, "outdoor_meter", 22.2, 55, 100, -58, 1_700_000_000.0)
        metrics.publish(reading, "garden")
        labels = {"mac": GARDEN, "name": "garden", "model": "outdoor_meter"}
        assert value(registry, "switchbot_temperature_celsius", **labels) == 22.2
        assert value(registry, "switchbot_humidity_percent", **labels) == 55.0
        assert value(registry, "switchbot_battery_percent", **labels) == 100.0
        assert value(registry, "switchbot_rssi_dbm", **labels) == -58.0
        assert value(registry, "switchbot_advertisements_total", **labels) == 1.0

    def test_battery_omitted_when_absent(self, metrics: Any,
                                         registry: CollectorRegistry) -> None:
        metrics.publish(sb.Reading(GARDEN, "meter", 20.0, 50, None, None, 0.0), "garden")
        labels = {"mac": GARDEN, "name": "garden", "model": "meter"}
        assert value(registry, "switchbot_battery_percent", **labels) is None

    def test_expire_drops_gauges_but_keeps_counters(
            self, metrics: Any, registry: CollectorRegistry) -> None:
        # A frozen last value is worse than a missing series: it makes absent()
        # and staleness alerting impossible.
        reading = sb.Reading(GARDEN, "outdoor_meter", 22.2, 55, 100, -58, 0.0)
        metrics.publish(reading, "garden")
        labels = {"mac": GARDEN, "name": "garden", "model": "outdoor_meter"}
        metrics.expire((GARDEN, "garden", "outdoor_meter"))
        assert value(registry, "switchbot_temperature_celsius", **labels) is None
        assert value(registry, "switchbot_advertisements_total", **labels) == 1.0

    def test_expire_is_idempotent(self, metrics: Any) -> None:
        metrics.publish(sb.Reading(GARDEN, "meter", 20.0, 50, 90, -58, 0.0), "garden")
        metrics.expire((GARDEN, "garden", "meter"))
        metrics.expire((GARDEN, "garden", "meter"))      # must not raise


# --------------------------------------------------------------------------- #
# Scan windows
# --------------------------------------------------------------------------- #

def two_device_config(**scan: Any) -> Any:
    defaults = {"interval_seconds": 2, "window_seconds": 1.5, "stale_after_seconds": 999}
    defaults.update(scan)
    config, _ = sb.parse_config({
        "scan": defaults,
        "devices": [{"mac": GARDEN, "name": "garden"}, {"mac": SHED, "name": "shed"}]})
    return config


class TestScanWindow:
    def test_window_stops_early_once_all_devices_report(
            self, metrics: Any, registry: CollectorRegistry) -> None:
        exporter = sb.Exporter(two_device_config(), metrics,
                               factory([(0.05, GARDEN), (0.05, SHED)]))
        result = run(exporter._scan_window(1.5))
        assert result.complete is True
        assert result.devices == frozenset({GARDEN, SHED})
        assert result.duration < 0.5, "should not have waited for the 1.5s cap"
        assert value(registry, "switchbot_scanner_up") == 1.0
        assert value(registry, "switchbot_scan_windows_incomplete_total") == 0.0

    def test_window_runs_to_cap_when_a_device_is_missing(
            self, metrics: Any, registry: CollectorRegistry) -> None:
        exporter = sb.Exporter(two_device_config(), metrics, factory([(0.05, GARDEN)]))
        result = run(exporter._scan_window(0.6))
        assert result.complete is False
        assert result.duration >= 0.55
        assert value(registry, "switchbot_scan_windows_incomplete_total") == 1.0
        # We heard something, so the scanner is genuinely up.
        assert value(registry, "switchbot_scanner_up") == 1.0

    def test_scanner_up_is_false_on_silent_window(
            self, metrics: Any, registry: CollectorRegistry) -> None:
        # v1 reported scanner_up=1 whenever a scanner object existed, even while
        # receiving nothing, which is exactly the case that needed alerting.
        exporter = sb.Exporter(two_device_config(), metrics, factory([]))
        result = run(exporter._scan_window(0.3))
        assert result.adverts == 0
        assert value(registry, "switchbot_scanner_up") == 0.0
        assert value(registry, "switchbot_scan_windows_empty_total") == 1.0

    def test_unconfigured_switchbot_is_still_seen(
            self, metrics: Any, registry: CollectorRegistry) -> None:
        # A mistyped MAC used to look identical to a dead radio in the logs.
        exporter = sb.Exporter(two_device_config(), metrics, factory([(0.05, STRANGER)]))
        result = run(exporter._scan_window(0.3))
        assert result.adverts == 0, "must not export an unconfigured device"
        assert result.switchbot_seen == frozenset({STRANGER})
        assert result.all_adverts == 1
        assert value(registry, "switchbot_devices_in_range") == 1.0

    def test_per_window_advert_count(self, metrics: Any,
                                     registry: CollectorRegistry) -> None:
        exporter = sb.Exporter(two_device_config(), metrics,
                               factory([(0.02, GARDEN), (0.02, GARDEN), (0.02, SHED)]))
        run(exporter._scan_window(1.0))
        assert value(registry, "switchbot_advertisements_last_window",
                     mac=GARDEN, name="garden", model="outdoor_meter") == 2.0

    def test_no_device_filter_uses_the_full_window(
            self, metrics: Any, registry: CollectorRegistry) -> None:
        # Without a configured set there is no completion condition to detect.
        config, _ = sb.parse_config({"scan": {"interval_seconds": 2, "window_seconds": 0.4}})
        exporter = sb.Exporter(config, metrics, factory([(0.02, GARDEN)]))
        result = run(exporter._scan_window(0.4))
        assert result.duration >= 0.35
        assert result.adverts == 1


# --------------------------------------------------------------------------- #
# Duty cycling and scan modes
# --------------------------------------------------------------------------- #

async def drive(exporter: Any, seconds: float) -> None:
    """Run the duty-cycle loop for a while, then shut it down cleanly."""
    task = asyncio.create_task(exporter._scan_duty_cycled())
    await asyncio.sleep(seconds)
    exporter._stop.set()
    await asyncio.sleep(0.1)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


@pytest.mark.slow
class TestDutyCycle:
    def test_radio_is_off_most_of_the_time(self, metrics: Any,
                                           registry: CollectorRegistry) -> None:
        exporter = sb.Exporter(two_device_config(), metrics,
                               factory([(0.05, GARDEN), (0.05, SHED)]))
        run(drive(exporter, 4.4))
        # Windows begin at t=0, 2 and 4.
        assert value(registry, "switchbot_scan_windows_total") == 3.0
        radio_on = value(registry, "switchbot_scan_seconds_total")
        assert radio_on is not None
        duty = radio_on / 4.4
        assert duty < 0.25, f"duty cycle {duty:.1%} should be far below 25%"

    def test_auto_mode_falls_back_to_active(self, metrics: Any,
                                            registry: CollectorRegistry) -> None:
        config = two_device_config(mode="auto", interval_seconds=0.5,
                                   window_seconds=0.2, max_empty_windows=2)
        exporter = sb.Exporter(config, metrics, factory([]))
        assert exporter._passive is True
        assert value(registry, "switchbot_scan_passive") == 1.0
        run(drive(exporter, 1.6))
        assert exporter._passive is False
        assert value(registry, "switchbot_scan_passive") == 0.0
        assert value(registry, "switchbot_scan_mode_fallbacks_total") == 1.0

    def test_passive_mode_does_not_fall_back(self, metrics: Any,
                                             registry: CollectorRegistry) -> None:
        # Explicitly asking for no transmissions must be honoured; silently
        # switching to active would violate that request.
        config = two_device_config(mode="passive", interval_seconds=0.5,
                                   window_seconds=0.2, max_empty_windows=2)
        exporter = sb.Exporter(config, metrics, factory([]))
        run(drive(exporter, 1.6))
        assert exporter._passive is True
        assert value(registry, "switchbot_scan_mode_fallbacks_total") == 0.0

    def test_active_mode_never_reports_passive(self, metrics: Any,
                                               registry: CollectorRegistry) -> None:
        exporter = sb.Exporter(two_device_config(mode="active"), metrics, factory([]))
        assert exporter._passive is False
        assert value(registry, "switchbot_scan_passive") == 0.0

    def test_auto_mode_recovers_and_keeps_reading(
            self, metrics: Any, registry: CollectorRegistry) -> None:
        # Falling back must not stop the loop; readings should keep flowing.
        config = two_device_config(mode="auto", interval_seconds=0.4,
                                   window_seconds=0.15, max_empty_windows=1)
        exporter = sb.Exporter(config, metrics, factory([(0.02, GARDEN), (0.02, SHED)]))
        run(drive(exporter, 1.3))
        assert value(registry, "switchbot_temperature_celsius",
                     mac=GARDEN, name="garden", model="outdoor_meter") == 22.2


# --------------------------------------------------------------------------- #
# Scanner construction
# --------------------------------------------------------------------------- #

class TestScannerArguments:
    def test_service_uuids_is_never_passed(self, metrics: Any,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
        # The regression that cost real debugging time: service_uuids matches the
        # Service Class UUID list, which these devices do not advertise, so bleak
        # drops every frame. It must never be set.
        captured: dict[str, Any] = {}

        def fake_scanner(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return FakeScanner(kwargs.get("detection_callback"), [])

        monkeypatch.setattr(sb, "BleakScanner", fake_scanner)
        config, _ = sb.parse_config({"scan": {"mode": "active"}})
        sb.Exporter(config, metrics)._default_scanner(lambda *a: None)
        assert "service_uuids" not in captured

    def test_passive_mode_sets_or_patterns(self, metrics: Any,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        def fake_scanner(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return FakeScanner(kwargs.get("detection_callback"), [])

        monkeypatch.setattr(sb, "BleakScanner", fake_scanner)
        config, _ = sb.parse_config({"scan": {"mode": "passive"}})
        sb.Exporter(config, metrics)._default_scanner(lambda *a: None)
        assert captured["scanning_mode"] == "passive"
        patterns = captured["bluez"]["or_patterns"]
        assert len(patterns) == 1
        # Offset 0 of AD type 0x16 (service data), matching 0xFD3D little-endian.
        start, ad_type, content = patterns[0]
        assert (start, int(ad_type), content) == (0, 0x16, b"\x3d\xfd")

    def test_adapter_goes_into_the_bluez_dict(self, metrics: Any,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
        # bleak 3.x deprecated the top-level adapter= keyword.
        captured: dict[str, Any] = {}

        def fake_scanner(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return FakeScanner(kwargs.get("detection_callback"), [])

        monkeypatch.setattr(sb, "BleakScanner", fake_scanner)
        config, _ = sb.parse_config({"adapter": "hci1"})
        sb.Exporter(config, metrics)._default_scanner(lambda *a: None)
        assert "adapter" not in captured
        assert captured["bluez"]["adapter"] == "hci1"


# --------------------------------------------------------------------------- #
# Passive support probe
# --------------------------------------------------------------------------- #

class TestPassiveSupport:
    def test_reports_supported_on_this_bleak(self) -> None:
        supported, detail = sb.passive_support()
        assert supported is True
        assert "bleak" in detail

    def test_passive_mode_rejected_when_unsupported(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Asking for something impossible must fail loudly at config time rather
        # than degrade silently, which is how the original passive problem hid.
        monkeypatch.setattr(sb, "passive_support", lambda: (False, "simulated"))
        with pytest.raises(sb.ConfigError) as excinfo:
            sb.parse_config({"scan": {"mode": "passive"}})
        assert any("cannot request passive" in p for p in excinfo.value.problems)

    def test_auto_mode_only_warns_when_unsupported(
            self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sb, "passive_support", lambda: (False, "simulated"))
        config, warnings = sb.parse_config({"scan": {"mode": "auto"}})
        assert config.scan.mode == "auto"
        assert any("cannot try passive" in w for w in warnings)

    def test_auto_starts_active_when_unsupported(
            self, metrics: Any, registry: CollectorRegistry,
            monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sb, "passive_support", lambda: (False, "simulated"))
        config, _ = sb.parse_config({"scan": {"mode": "auto"}})
        exporter = sb.Exporter(config, metrics)
        assert exporter._passive is False
        assert value(registry, "switchbot_scan_passive") == 0.0


# --------------------------------------------------------------------------- #
# Discovery filters
# --------------------------------------------------------------------------- #

class TestDiscoveryFilters:
    """bleak defaults DuplicateData to False, which in BlueZ terms enables
    duplicate suppression: PropertiesChanged stops firing once a device's payload
    stops changing. A thermometer in stable conditions sends byte-identical
    advertisements for minutes, so the exporter would publish one reading at
    startup and then go quiet while unrelated BLE traffic kept arriving. It looks
    identical to a range problem and is not one."""

    def _filters(self, **bluez: Any) -> dict[str, Any]:
        from bleak import BleakScanner
        scanner = BleakScanner(detection_callback=lambda d, a: None, bluez=bluez or {})
        return {k: v.value for k, v in scanner._backend._filters.items()}

    def test_bleak_default_would_suppress_duplicates(self) -> None:
        # Documents the upstream default this works around. If bleak ever changes
        # it, this test fails and the override can be reconsidered.
        assert self._filters()["DuplicateData"] is False

    def test_exporter_forces_duplicate_data_on(
            self, metrics: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        def fake_scanner(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return FakeScanner(kwargs.get("detection_callback"), [])

        monkeypatch.setattr(sb, "BleakScanner", fake_scanner)
        config, _ = sb.parse_config({})
        sb.Exporter(config, metrics)._default_scanner(lambda *a: None)
        assert captured["bluez"]["filters"]["DuplicateData"] is True

    def test_override_preserves_le_transport(self) -> None:
        filters = self._filters(filters={"DuplicateData": True})
        assert filters["DuplicateData"] is True
        assert filters["Transport"] == "le"

    def test_duplicate_data_survives_an_adapter_setting(
            self, metrics: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        def fake_scanner(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return FakeScanner(kwargs.get("detection_callback"), [])

        monkeypatch.setattr(sb, "BleakScanner", fake_scanner)
        config, _ = sb.parse_config({"adapter": "hci0"})
        sb.Exporter(config, metrics)._default_scanner(lambda *a: None)
        assert captured["bluez"]["adapter"] == "hci0"
        assert captured["bluez"]["filters"]["DuplicateData"] is True

    def test_duplicate_data_set_in_passive_mode_too(
            self, metrics: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        def fake_scanner(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return FakeScanner(kwargs.get("detection_callback"), [])

        monkeypatch.setattr(sb, "BleakScanner", fake_scanner)
        config, _ = sb.parse_config({"scan": {"mode": "passive"}})
        sb.Exporter(config, metrics)._default_scanner(lambda *a: None)
        assert captured["scanning_mode"] == "passive"
        assert captured["bluez"]["filters"]["DuplicateData"] is True


class TestLastHeardHint:
    def test_reports_nothing_ever_heard(self, metrics: Any) -> None:
        exporter = sb.Exporter(two_device_config(), metrics, factory([]))
        assert "ever been heard" in exporter._last_heard_hint()

    def test_reports_age_and_signal_strength(self, metrics: Any) -> None:
        exporter = sb.Exporter(two_device_config(), metrics,
                               factory([(0.02, GARDEN)]))
        run(exporter._scan_window(0.3))
        hint = exporter._last_heard_hint()
        assert GARDEN in hint
        assert "-58 dBm" in hint

    def test_flags_a_marginal_link(self, metrics: Any) -> None:
        exporter = sb.Exporter(two_device_config(), metrics, factory([]))
        exporter._last_reading = (GARDEN, -97, __import__("time").monotonic())
        assert "expect frequent loss" in exporter._last_heard_hint()

    def test_does_not_flag_a_healthy_link(self, metrics: Any) -> None:
        exporter = sb.Exporter(two_device_config(), metrics, factory([]))
        exporter._last_reading = (GARDEN, -62, __import__("time").monotonic())
        assert "expect frequent loss" not in exporter._last_heard_hint()


# --------------------------------------------------------------------------- #
# MQTT
# --------------------------------------------------------------------------- #

class TestMqttConfig:
    def test_disabled_by_default(self) -> None:
        config, _ = sb.parse_config({})
        assert config.mqtt.enabled is False
        assert config.mqtt.host == "localhost"
        assert config.mqtt.port == 1883

    def test_example_config_includes_mqtt(self) -> None:
        config, warnings = sb.parse_config(sb.EXAMPLE_CONFIG)
        assert config.mqtt.enabled is False
        assert config.mqtt.discovery is True
        assert warnings == []

    @pytest.mark.parametrize("raw, fragment", [
        ({"mqtt": {"prt": 1883}}, "unknown key mqtt."),
        ({"mqtt": {"port": 0}}, "mqtt.port must be 1-65535"),
        ({"mqtt": {"port": 99999}}, "mqtt.port must be 1-65535"),
        ({"mqtt": {"qos": 3}}, "mqtt.qos must be 0, 1 or 2"),
        ({"mqtt": {"enabled": "yes"}}, "must be true or false"),
        ({"mqtt": {"host": ""}}, "non-empty string"),
        # Wildcards in a publish topic would be silently wrong rather than an error.
        ({"mqtt": {"topic_prefix": "switchbot/#"}}, "must not contain MQTT wildcards"),
        ({"mqtt": {"topic_prefix": "+/switchbot"}}, "must not contain MQTT wildcards"),
        ({"mqtt": {"topic_prefix": "/switchbot"}}, "start with '/'"),
        ({"mqtt": {"password": "s3cret", "password_file": "/tmp/x"}},
         "mutually exclusive"),
        ({"mqtt": {"password_file": "/nonexistent/secret"}}, "does not exist"),
    ])
    def test_rejects_bad_mqtt_config(self, raw: dict, fragment: str) -> None:
        with pytest.raises(sb.ConfigError) as excinfo:
            sb.parse_config(raw)
        assert any(fragment in p for p in excinfo.value.problems), excinfo.value.problems

    def test_username_without_password_warns(self) -> None:
        _, warnings = sb.parse_config(
            {"mqtt": {"enabled": True, "username": "homeassistant"}})
        assert any("without a password" in w for w in warnings)

    def test_password_file_is_read_lazily(self, tmp_path: Path) -> None:
        # Kept out of the config so the secret need not sit in a file the service
        # group can read.
        secret = tmp_path / "mqtt.pass"
        secret.write_text("hunter2\n")
        config, _ = sb.parse_config({"mqtt": {"password_file": str(secret)}})
        assert config.mqtt.password is None
        assert config.mqtt.resolve_password() == "hunter2"

    def test_qos_and_retain_round_trip(self) -> None:
        config, _ = sb.parse_config({"mqtt": {"qos": 1, "retain": False}})
        assert config.mqtt.qos == 1
        assert config.mqtt.retain is False


class TestTopicSlug:
    @pytest.mark.parametrize("name, expected", [
        ("balcony", "balcony"),
        ("Balcony / North", "balcony_north"),      # / is the topic separator
        ("Küche", "k_che"),
        ("sensor#1", "sensor_1"),                  # # is a wildcard
        ("a+b", "a_b"),                            # + is a wildcard
        ("  spaced  out  ", "spaced_out"),
        ("EB:6B:01:C6:2B:2C", "eb_6b_01_c6_2b_2c"),
        ("///", "unnamed"),
    ])
    def test_slugs_are_topic_safe(self, name: str, expected: str) -> None:
        slug = sb.topic_slug(name)
        assert slug == expected
        assert not set("+#/") & set(slug)
        assert slug


class FakeMqttClient:
    """Records publishes instead of talking to a broker."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str, int, bool]] = []
        self.will: tuple | None = None
        self.rc = 0

    def publish(self, topic: str, payload: Any = None, qos: int = 0,
                retain: bool = False, properties: Any = None) -> Any:
        self.published.append((topic, payload, qos, retain))
        return type("Info", (), {"rc": self.rc})()

    def will_set(self, topic: str, payload: Any = None, qos: int = 0,
                 retain: bool = False, properties: Any = None) -> None:
        self.will = (topic, payload, qos, retain)

    def disconnect(self) -> None: ...
    def loop_stop(self) -> None: ...


@pytest.fixture
def publisher(metrics: Any) -> Any:
    config, _ = sb.parse_config(
        {"mqtt": {"enabled": True, "individual_topics": True},
         "scan": {"stale_after_seconds": 240}})
    pub = sb.MqttPublisher(config.mqtt, metrics, config.scan.stale_after_seconds)
    pub._client = FakeMqttClient()
    return pub


def a_reading(**kw: Any) -> Any:
    defaults = dict(mac=GARDEN, model="outdoor_meter", temperature_c=22.2,
                    humidity_pct=55, battery_pct=100, rssi_dbm=-63,
                    timestamp=1_700_000_000.0)
    defaults.update(kw)
    return sb.Reading(**defaults)


class TestMqttPublish:
    def _topics(self, publisher: Any) -> list[str]:
        return [t for t, _, _, _ in publisher._client.published]

    def test_state_payload_is_json_with_expected_fields(self, publisher: Any) -> None:
        publisher.publish(a_reading(), "garden")
        state = next(p for t, p, _, _ in publisher._client.published
                     if t == "switchbot/garden/state")
        payload = json.loads(state)
        assert payload["mac"] == GARDEN
        assert payload["temperature_c"] == 22.2
        assert payload["humidity_pct"] == 55
        assert payload["battery_pct"] == 100
        assert payload["dew_point_c"] == 12.73
        assert payload["rssi_dbm"] == -63
        assert payload["timestamp"].startswith("2023-11-14T")

    def test_battery_omitted_when_device_does_not_report_it(self, publisher: Any) -> None:
        publisher.publish(a_reading(battery_pct=None), "garden")
        state = next(p for t, p, _, _ in publisher._client.published
                     if t.endswith("/state"))
        assert "battery_pct" not in json.loads(state)

    def test_individual_topics_when_enabled(self, publisher: Any) -> None:
        publisher.publish(a_reading(), "garden")
        topics = self._topics(publisher)
        assert "switchbot/garden/temperature_c" in topics
        assert "switchbot/garden/humidity_pct" in topics
        # Strings such as mac and model must not become their own topics.
        assert "switchbot/garden/mac" not in topics

    def test_discovery_published_once_per_device(self, publisher: Any) -> None:
        publisher.publish(a_reading(), "garden")
        first = sum(1 for t in self._topics(publisher) if t.startswith("homeassistant/"))
        publisher.publish(a_reading(temperature_c=22.4), "garden")
        second = sum(1 for t in self._topics(publisher) if t.startswith("homeassistant/"))
        assert first == len(sb.HA_SENSORS)
        assert second == first, "discovery must not repeat on every reading"

    def test_discovery_is_retained_even_when_state_is_not(self, metrics: Any) -> None:
        # Home Assistant reads discovery at startup, so it must survive; state
        # retention is a separate choice.
        config, _ = sb.parse_config({"mqtt": {"enabled": True, "retain": False}})
        pub = sb.MqttPublisher(config.mqtt, metrics, 300)
        pub._client = FakeMqttClient()
        pub.publish(a_reading(), "garden")
        for topic, _, _, retain in pub._client.published:
            if topic.startswith("homeassistant/"):
                assert retain is True, topic
            elif topic.endswith("/state"):
                assert retain is False, topic

    def test_discovery_payload_shape(self, publisher: Any) -> None:
        publisher.publish(a_reading(), "garden")
        raw = next(p for t, p, _, _ in publisher._client.published
                   if t.endswith("/temperature_c/config"))
        config = json.loads(raw)
        assert config["device_class"] == "temperature"
        assert config["unit_of_measurement"] == "°C"
        assert config["state_class"] == "measurement"
        assert config["state_topic"] == "switchbot/garden/state"
        assert config["availability_topic"] == "switchbot/status"
        # Derived from stale_after_seconds so a retained value cannot look
        # current indefinitely.
        assert config["expire_after"] == 240
        assert config["device"]["identifiers"] == ["switchbot_aabbccddeeff"]
        assert config["device"]["manufacturer"] == "SwitchBot"

    def test_no_battery_discovery_without_battery(self, publisher: Any) -> None:
        publisher.publish(a_reading(battery_pct=None), "garden")
        assert not any(t.endswith("/battery_pct/config")
                       for t in self._topics(publisher))

    def test_rssi_is_a_diagnostic_entity(self, publisher: Any) -> None:
        publisher.publish(a_reading(), "garden")
        raw = next(p for t, p, _, _ in publisher._client.published
                   if t.endswith("/rssi_dbm/config"))
        assert json.loads(raw)["entity_category"] == "diagnostic"

    def test_publish_counts_success(self, publisher: Any,
                                    registry: CollectorRegistry) -> None:
        publisher.publish(a_reading(), "garden")
        published = value(registry, "switchbot_mqtt_messages_published_total")
        assert published == len(publisher._client.published)
        assert value(registry, "switchbot_mqtt_errors_total") == 0.0

    def test_publish_failure_is_counted_not_raised(
            self, publisher: Any, registry: CollectorRegistry) -> None:
        publisher._client.rc = 4          # not connected
        publisher.publish(a_reading(), "garden")
        assert value(registry, "switchbot_mqtt_errors_total") > 0
        assert value(registry, "switchbot_mqtt_messages_published_total") == 0.0

    def test_reconnect_re_announces_discovery(self, publisher: Any) -> None:
        # A broker restarted without persistence loses retained discovery, so it
        # has to be resent.
        publisher.publish(a_reading(), "garden")
        assert publisher._announced
        publisher._on_connect(publisher._client, None, None, 0, None)
        assert not publisher._announced

    def test_stop_publishes_offline(self, publisher: Any) -> None:
        publisher.stop()
        assert ("switchbot/status", "offline", 1, True) in publisher._client.published


class TestMqttIntegrationWithExporter:
    def test_mqtt_absent_when_disabled(self, metrics: Any) -> None:
        config, _ = sb.parse_config({})
        assert sb.Exporter(config, metrics)._mqtt is None

    def test_mqtt_constructed_when_enabled(self, metrics: Any) -> None:
        config, _ = sb.parse_config({"mqtt": {"enabled": True}})
        assert sb.Exporter(config, metrics)._mqtt is not None

    def test_reading_goes_to_both_sinks(self, metrics: Any,
                                        registry: CollectorRegistry) -> None:
        config, _ = sb.parse_config(
            {"mqtt": {"enabled": True},
             "devices": [{"mac": GARDEN, "name": "garden"}]})
        pub = sb.MqttPublisher(config.mqtt, metrics, 300)
        pub._client = FakeMqttClient()
        exporter = sb.Exporter(config, metrics, factory([]), publisher=pub)

        from bleak.backends.device import BLEDevice
        exporter._on_advert(
            BLEDevice(address=GARDEN, name="Meter", details={}), advertisement())
        labels = {"mac": GARDEN, "name": "garden", "model": "outdoor_meter"}
        assert value(registry, "switchbot_temperature_celsius", **labels) == 22.2
        assert any(t.endswith("/state") for t, _, _, _ in pub._client.published)

    def test_broker_failure_does_not_break_prometheus(
            self, metrics: Any, registry: CollectorRegistry) -> None:
        # The whole point: MQTT is best effort, Prometheus is not.
        class Exploding(FakeMqttClient):
            def publish(self, *a: Any, **kw: Any) -> Any:
                raise OSError("broker unreachable")

        config, _ = sb.parse_config(
            {"mqtt": {"enabled": True}, "devices": [{"mac": GARDEN, "name": "garden"}]})
        pub = sb.MqttPublisher(config.mqtt, metrics, 300)
        pub._client = Exploding()
        exporter = sb.Exporter(config, metrics, factory([]), publisher=pub)

        from bleak.backends.device import BLEDevice
        exporter._on_advert(
            BLEDevice(address=GARDEN, name="Meter", details={}), advertisement())
        labels = {"mac": GARDEN, "name": "garden", "model": "outdoor_meter"}
        assert value(registry, "switchbot_temperature_celsius", **labels) == 22.2
        assert value(registry, "switchbot_mqtt_errors_total") > 0


# --------------------------------------------------------------------------- #
# Beacon counting
# --------------------------------------------------------------------------- #

def other_vendor_advertisement() -> Any:
    """A non-SwitchBot frame, e.g. an iBeacon."""
    from bleak.backends.scanner import AdvertisementData
    return AdvertisementData(
        local_name="something else", manufacturer_data={0x004C: b"\x02\x15" + bytes(21)},
        service_data={}, service_uuids=[], tx_power=None, rssi=-80, platform_data=())


class FakeMixedScanner:
    """Emits a mix of SwitchBot and unrelated advertisements."""

    def __init__(self, callback: Any, schedule: Sequence[tuple[float, str, str]]) -> None:
        self._callback = callback
        self._schedule = schedule
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> "FakeMixedScanner":
        self._task = asyncio.create_task(self._emit())
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _emit(self) -> None:
        from bleak.backends.device import BLEDevice
        for delay, mac, kind in self._schedule:
            await asyncio.sleep(delay)
            adv = advertisement() if kind == "switchbot" else other_vendor_advertisement()
            self._callback(BLEDevice(address=mac, name=None, details={}), adv)


def mixed_factory(schedule: Sequence[tuple[float, str, str]]) -> Any:
    return lambda callback: FakeMixedScanner(callback, schedule)


class TestBeaconCounts:
    def test_per_device_beacons_are_counted(self, metrics: Any,
                                            registry: CollectorRegistry) -> None:
        exporter = sb.Exporter(two_device_config(), metrics,
                               mixed_factory([(0.02, GARDEN, "switchbot"),
                                              (0.02, GARDEN, "switchbot"),
                                              (0.02, SHED, "switchbot")]))
        run(exporter._scan_window(1.0))
        labels = {"mac": GARDEN, "name": "garden", "model": "outdoor_meter"}
        assert value(registry, "switchbot_advertisements_total", **labels) == 2.0
        assert value(registry, "switchbot_advertisements_last_window", **labels) == 2.0

    def test_all_ble_frames_are_counted_not_just_switchbot(
            self, metrics: Any, registry: CollectorRegistry) -> None:
        # The distinguishing metric: one SwitchBot beacon among four frames.
        exporter = sb.Exporter(two_device_config(), metrics,
                               mixed_factory([(0.02, GARDEN, "switchbot"),
                                              (0.02, "00:11:22:33:44:55", "other"),
                                              (0.02, "66:77:88:99:AA:BB", "other"),
                                              (0.02, "CC:DD:EE:FF:00:11", "other")]))
        result = run(exporter._scan_window(1.0))
        assert value(registry, "switchbot_ble_advertisements_seen_total") == 4.0
        assert value(registry, "switchbot_ble_advertisements_last_window") == 4.0
        assert result.all_adverts == 4
        # Only the SwitchBot one counts towards the per-device total.
        assert value(registry, "switchbot_advertisements_total",
                     mac=GARDEN, name="garden", model="outdoor_meter") == 1.0

    def test_a_deaf_radio_is_distinguishable_from_a_quiet_sensor(
            self, metrics: Any, registry: CollectorRegistry) -> None:
        # This is the case that cost real debugging time: no readings, but were
        # any frames arriving at all? The two situations must look different.
        deaf = sb.Exporter(two_device_config(), metrics, mixed_factory([]))
        run(deaf._scan_window(0.2))
        assert value(registry, "switchbot_ble_advertisements_seen_total") == 0.0

        quiet = sb.Exporter(two_device_config(), sb.Metrics(reg2 := CollectorRegistry()),
                            mixed_factory([(0.02, "00:11:22:33:44:55", "other")] * 3))
        run(quiet._scan_window(0.3))
        assert value(reg2, "switchbot_ble_advertisements_seen_total") == 3.0
        assert value(reg2, "switchbot_advertisements_unmatched_total") == 0.0

    def test_unconfigured_switchbot_increments_unmatched(
            self, metrics: Any, registry: CollectorRegistry) -> None:
        # A mistyped MAC: frames arrive, decode as SwitchBot, and match nothing.
        exporter = sb.Exporter(two_device_config(), metrics,
                               mixed_factory([(0.02, STRANGER, "switchbot"),
                                              (0.02, STRANGER, "switchbot")]))
        run(exporter._scan_window(0.4))
        assert value(registry, "switchbot_advertisements_unmatched_total") == 2.0
        assert value(registry, "switchbot_ble_advertisements_seen_total") == 2.0
        assert value(registry, "switchbot_devices_in_range") == 1.0

    def test_unmatched_is_unlabelled_to_bound_cardinality(self, metrics: Any) -> None:
        # Neighbours' devices must not each create a permanent time series.
        assert metrics.unmatched._labelnames == ()

    def test_counters_survive_staleness_expiry(self, metrics: Any,
                                               registry: CollectorRegistry) -> None:
        exporter = sb.Exporter(two_device_config(), metrics,
                               mixed_factory([(0.02, GARDEN, "switchbot")]))
        run(exporter._scan_window(0.3))
        metrics.expire((GARDEN, "garden", "outdoor_meter"))
        # Gauges go, counters are monotonic by contract and must remain.
        assert value(registry, "switchbot_advertisements_last_window",
                     mac=GARDEN, name="garden", model="outdoor_meter") is None
        assert value(registry, "switchbot_advertisements_total",
                     mac=GARDEN, name="garden", model="outdoor_meter") == 1.0
        assert value(registry, "switchbot_ble_advertisements_seen_total") == 1.0

    def test_window_gauge_resets_between_windows(self, metrics: Any,
                                                 registry: CollectorRegistry) -> None:
        exporter = sb.Exporter(two_device_config(), metrics,
                               mixed_factory([(0.02, "00:11:22:33:44:55", "other")] * 5))

        # Both windows run inside one event loop, as they do in production: the
        # exporter holds asyncio primitives that bind to the loop that first
        # touches them.
        async def two_windows() -> tuple[float | None, float | None]:
            await exporter._scan_window(0.3)
            first = value(registry, "switchbot_ble_advertisements_last_window")
            exporter._scanner_factory = mixed_factory([])
            await exporter._scan_window(0.2)
            return first, value(registry, "switchbot_ble_advertisements_last_window")

        first, second = run(two_windows())
        assert first == 5.0
        assert second == 0.0, "the per-window gauge must reset"
        # The cumulative counter must not reset.
        assert value(registry, "switchbot_ble_advertisements_seen_total") == 5.0


# --------------------------------------------------------------------------- #
# Silence diagnosis
# --------------------------------------------------------------------------- #

class TestSilenceDiagnosis:
    """The exporter went quiet for an hour and logged 'no advertisements for 120s'
    thirty times, which is compatible with three unrelated causes. These tests pin
    the message to the evidence so the log identifies the cause itself."""

    def test_no_frames_at_all_blames_the_adapter(self, metrics: Any) -> None:
        exporter = sb.Exporter(two_device_config(), metrics, factory([]))
        message = exporter._diagnose_silence(120.0)
        assert "ANY kind" in message
        assert "adapter is not receiving" in message
        assert "dmesg" in message

    def test_other_traffic_but_no_switchbot_blames_the_sensor(
            self, metrics: Any) -> None:
        exporter = sb.Exporter(two_device_config(), metrics, factory([]))
        exporter._silence_frames = 847
        message = exporter._diagnose_silence(120.0)
        assert "847" in message
        assert "radio and the exporter are both working" in message
        assert "this is the sensor" in message

    def test_other_switchbots_audible_blames_the_config(self, metrics: Any) -> None:
        exporter = sb.Exporter(two_device_config(), metrics, factory([]))
        exporter._silence_frames = 500
        exporter._silence_switchbot = 12
        message = exporter._diagnose_silence(120.0)
        assert "12 were from a SwitchBot" in message
        assert "MAC addresses in the config" in message

    def test_reports_when_the_sensor_was_last_heard(self, metrics: Any) -> None:
        exporter = sb.Exporter(two_device_config(), metrics, factory([]))
        exporter._silence_frames = 300
        exporter._last_reading = (GARDEN, -63, __import__("time").monotonic() - 3600)
        message = exporter._diagnose_silence(120.0)
        assert GARDEN in message
        assert "60 min ago" in message
        assert "-63 dBm" in message

    def test_flags_an_already_weak_link(self, metrics: Any) -> None:
        exporter = sb.Exporter(two_device_config(), metrics, factory([]))
        exporter._silence_frames = 300
        exporter._last_reading = (GARDEN, -88, __import__("time").monotonic() - 300)
        assert "already weak" in exporter._diagnose_silence(120.0)

    def test_does_not_flag_a_strong_link(self, metrics: Any) -> None:
        exporter = sb.Exporter(two_device_config(), metrics, factory([]))
        exporter._silence_frames = 300
        exporter._last_reading = (GARDEN, -55, __import__("time").monotonic() - 300)
        assert "already weak" not in exporter._diagnose_silence(120.0)

    def test_counters_track_traffic_during_silence(self, metrics: Any) -> None:
        # A configured device never reports, but plenty else does.
        exporter = sb.Exporter(two_device_config(), metrics,
                               mixed_factory([(0.02, "00:11:22:33:44:55", "other"),
                                              (0.02, "66:77:88:99:AA:BB", "other"),
                                              (0.02, STRANGER, "switchbot")]))
        run(exporter._scan_window(0.4))
        assert exporter._silence_frames == 3
        assert exporter._silence_switchbot == 1
        message = exporter._diagnose_silence(120.0)
        assert "1 were from a SwitchBot" in message

    def test_reset_clears_the_counters(self, metrics: Any) -> None:
        exporter = sb.Exporter(two_device_config(), metrics, factory([]))
        exporter._silence_frames = 99
        exporter._silence_switchbot = 9
        exporter._reset_silence_counters()
        assert (exporter._silence_frames, exporter._silence_switchbot) == (0, 0)