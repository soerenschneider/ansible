#!/usr/bin/env python3
"""
switchbot_exporter v3 — Prometheus exporter for SwitchBot BLE thermo-hygrometers.

Configured from a JSON file rather than a pile of flags:

    ./switchbot_exporter.py --example-config > /etc/switchbot-exporter/config.json
    ./switchbot_exporter.py --check-config           # validate, then exit
    ./switchbot_exporter.py --config /etc/switchbot-exporter/config.json

With no --config it looks for ./switchbot-exporter.json then
/etc/switchbot-exporter/config.json, and runs on defaults if neither exists.

Scanning
--------
Passive scanning needs BlueZ's Advertisement Monitor API, which many builds do
not expose. This exporter instead bounds radio time with duty cycling: set
scan.interval_seconds to your Prometheus scrape interval and it scans in short
windows, stopping the radio entirely in between. With devices listed, a window
ends the moment they have all reported, so a healthy sensor at close range needs
a few seconds rather than the full window.

    "scan": {"interval_seconds": 60, "window_seconds": 25}

Two things that look like optimisations and are not
---------------------------------------------------
1. service_uuids must never be passed to BleakScanner for these devices. It
   filters on the Service Class UUID list (AD types 0x02/0x03), which SwitchBot
   meters do not advertise at all -- they carry Service DATA for 0xFD3D and
   nothing else. BlueZ reports UUIDs=[] and bleak's is_allowed_uuid() then
   silently drops every advertisement. Filtering happens in the callback.
2. There is no pressure metric. No SwitchBot meter has a barometer; earlier
   versions exposed switchbot_pressure_hpa fed from an external sensor, which
   only invited confusion about where the number came from. Removed in v3.

    pip install 'bleak>=3.0' 'prometheus-client>=0.19'
    sudo setcap 'cap_net_raw,cap_net_admin+eip' "$(readlink -f "$(which python3)")"

Tests live alongside in test_switchbot_exporter.py and need no Bluetooth adapter:

    pip install pytest
    pytest -q                    # everything
    pytest -q -m "not slow"      # skip the timing-sensitive duty-cycle cases
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import math
import os
import re
import signal
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

try:
    from bleak import BleakScanner
    from bleak.backends.device import BLEDevice
    from bleak.backends.scanner import AdvertisementData
except ImportError:  # pragma: no cover
    print("missing dependency: pip install 'bleak>=3.0'", file=sys.stderr)
    raise

try:
    from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Info,
    start_http_server,
)
except ImportError:  # pragma: no cover
    print("missing dependency: pip install 'prometheus-client>=0.19'", file=sys.stderr)
    raise

__version__ = "3.0.0"

log = logging.getLogger("switchbot_exporter")

SWITCHBOT_COMPANY_ID = 0x0969
SWITCHBOT_SERVICE_UUID = "0000fd3d-0000-1000-8000-00805f9b34fb"

DEVICE_TYPE_OUTDOOR_METER = 0x77  # 'w', model W3400010
MODEL_NAMES = {
    0x77: "outdoor_meter",
    0x54: "meter",
    0x69: "meter_plus",
    0x34: "meter_pro",
    0x35: "meter_pro_co2",
}

LABELS = ("mac", "name", "model")

DEFAULT_CONFIG_PATHS = (
    Path("switchbot-exporter.json"),
    Path("/etc/switchbot-exporter/config.json"),
)


class DecodeError(ValueError):
    """Advertisement came from a SwitchBot device but did not decode cleanly."""


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

MAC_RE = re.compile(r"^(?:[0-9A-F]{2}:){5}[0-9A-F]{2}$")
LISTEN_RE = re.compile(r"^(?P<host>.*):(?P<port>\d+)$")
LOG_LEVELS = ("debug", "info", "warning", "error")

# Defined as constants rather than read back off the dataclass: with slots=True,
# Config.listen is a slot descriptor, not the default value.
DEFAULT_LISTEN = "0.0.0.0:9865"
DEFAULT_LOG_LEVEL = "info"

# active  : always scan actively. Works everywhere. Solicits a scan response
#           from the sensor for every advertisement it hears.
# passive : receive only, never transmit. Needs BlueZ's Advertisement Monitor
#           API, which many builds only expose with experimental features on.
# auto    : start passive, fall back to active if it produces nothing. The
#           duty-cycle loop can detect that, which is why this mode is possible.
SCAN_MODES = ("active", "passive", "auto")
DEFAULT_SCAN_MODE = "active"


def passive_support() -> tuple[bool, str]:
    """Whether this bleak build can request passive scanning.

    Probes by module rather than by importing names, so the two supported bleak
    layouts do not shadow each other. Only covers the client library: BlueZ may
    still accept an advertisement monitor and then deliver nothing, which is the
    whole reason 'auto' mode exists.
    """
    import importlib

    try:
        numbers = importlib.import_module("bleak.assigned_numbers")
        if not hasattr(numbers, "AdvertisementDataType"):
            return False, "bleak.assigned_numbers lacks AdvertisementDataType"
    except ImportError as exc:
        return False, f"bleak.assigned_numbers unavailable: {exc}"

    # bleak 3.x moved OrPattern to bleak.args.bluez; older builds kept it under
    # the BlueZ backend.
    for module_name in ("bleak.args.bluez",
                        "bleak.backends.bluezdbus.advertisement_monitor"):
        try:
            if hasattr(importlib.import_module(module_name), "OrPattern"):
                return True, module_name
        except ImportError:
            continue
    return False, "no bleak module provides OrPattern"


class ConfigError(Exception):
    """One or more problems in the configuration file."""

    def __init__(self, problems: Sequence[str], path: Path | None = None) -> None:
        self.problems = list(problems)
        self.path = path
        location = f" in {path}" if path else ""
        super().__init__(f"{len(self.problems)} configuration problem(s){location}")

    def report(self, stream: Any = sys.stderr) -> None:
        print(str(self), file=stream)
        for problem in self.problems:
            print(f"  - {problem}", file=stream)


@dataclass(frozen=True, slots=True)
class Device:
    mac: str
    name: str


@dataclass(frozen=True, slots=True)
class ScanConfig:
    mode: str = DEFAULT_SCAN_MODE
    # 0 scans continuously, which is the pre-v2 behaviour.
    interval_seconds: float = 0.0
    window_seconds: float = 25.0
    max_empty_windows: int = 3
    stale_after_seconds: float = 300.0
    watchdog_timeout_seconds: float = 120.0


@dataclass(frozen=True, slots=True)
class MqttConfig:
    enabled: bool = False
    host: str = "localhost"
    port: int = 1883
    username: str | None = None
    password: str | None = None
    # Preferred over `password` so the secret need not live in the config file,
    # which is world-readable to the service group.
    password_file: str | None = None
    tls: bool = False
    client_id: str = "switchbot-exporter"
    topic_prefix: str = "switchbot"
    qos: int = 0
    # Retained state means a subscriber that starts later still sees the last
    # reading. Paired with expire_after in Home Assistant discovery so a retained
    # value cannot masquerade as current forever.
    retain: bool = True
    individual_topics: bool = False
    discovery: bool = True
    discovery_prefix: str = "homeassistant"

    def resolve_password(self) -> str | None:
        if self.password_file:
            return Path(self.password_file).read_text().strip()
        return self.password


@dataclass(frozen=True, slots=True)
class Config:
    listen: str = DEFAULT_LISTEN
    adapter: str | None = None
    log_level: str = DEFAULT_LOG_LEVEL
    scan: ScanConfig = field(default_factory=ScanConfig)
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    devices: tuple[Device, ...] = ()

    @property
    def macs(self) -> frozenset[str]:
        return frozenset(d.mac for d in self.devices)

    @property
    def names(self) -> Mapping[str, str]:
        return {d.mac: d.name for d in self.devices}

    @property
    def host(self) -> str:
        match = LISTEN_RE.match(self.listen)
        assert match, "validated at load time"
        return match.group("host") or "0.0.0.0"

    @property
    def port(self) -> int:
        match = LISTEN_RE.match(self.listen)
        assert match, "validated at load time"
        return int(match.group("port"))


def _public_keys(obj: Mapping[str, Any]) -> set[str]:
    """Keys ignoring the _-prefixed convention, since JSON has no comments."""
    return {k for k in obj if not k.startswith("_")}


def parse_config(raw: Any) -> tuple[Config, list[str]]:
    """Build a Config from parsed JSON.

    Collects every problem before raising rather than failing on the first, so a
    config with three typos reports three typos.
    """
    problems: list[str] = []
    warnings: list[str] = []

    if not isinstance(raw, dict):
        raise ConfigError(["top level must be a JSON object"])

    known = {"listen", "adapter", "log", "scan", "mqtt", "devices"}
    for key in sorted(_public_keys(raw) - known):
        problems.append(f"unknown key {key!r} (expected one of {', '.join(sorted(known))})")

    # -- listen ---------------------------------------------------------- #
    listen = raw.get("listen", DEFAULT_LISTEN)
    if not isinstance(listen, str) or not LISTEN_RE.match(listen):
        problems.append(f"listen must look like 'host:port', got {listen!r}")
        listen = DEFAULT_LISTEN
    else:
        port = int(LISTEN_RE.match(listen).group("port"))  # type: ignore[union-attr]
        if not 1 <= port <= 65535:
            problems.append(f"listen port {port} is out of range")

    # -- adapter --------------------------------------------------------- #
    adapter = raw.get("adapter")
    if adapter is not None:
        if not isinstance(adapter, str) or not adapter:
            problems.append(f"adapter must be a string such as 'hci0' or null, got {adapter!r}")
            adapter = None
        elif not re.match(r"^hci\d+$", adapter):
            warnings.append(f"adapter {adapter!r} does not look like 'hciN'")

    # -- log ------------------------------------------------------------- #
    log_level = DEFAULT_LOG_LEVEL
    log_section = raw.get("log", {})
    if not isinstance(log_section, dict):
        problems.append("log must be an object")
    else:
        for key in sorted(_public_keys(log_section) - {"level"}):
            problems.append(f"unknown key log.{key!r}")
        level = log_section.get("level", log_level)
        if not isinstance(level, str) or level.lower() not in LOG_LEVELS:
            problems.append(f"log.level must be one of {', '.join(LOG_LEVELS)}, got {level!r}")
        else:
            log_level = level.lower()

    # -- scan ------------------------------------------------------------ #
    scan = ScanConfig()
    scan_section = raw.get("scan", {})
    if not isinstance(scan_section, dict):
        problems.append("scan must be an object")
    else:
        numeric = {
            "interval_seconds": (0.0, True),      # 0 means continuous
            "window_seconds": (0.0, False),
            "stale_after_seconds": (0.0, False),
            "watchdog_timeout_seconds": (0.0, False),
        }
        allowed = set(numeric) | {"max_empty_windows", "mode"}
        for key in sorted(_public_keys(scan_section) - allowed):
            problems.append(f"unknown key scan.{key!r}")

        mode = scan_section.get("mode", DEFAULT_SCAN_MODE)
        if not isinstance(mode, str) or mode.lower() not in SCAN_MODES:
            problems.append(
                f"scan.mode must be one of {', '.join(SCAN_MODES)}, got {mode!r}")
        else:
            mode = mode.lower()
            supported, detail = passive_support()
            if mode == "passive" and not supported:
                problems.append(
                    f"scan.mode is 'passive' but this bleak build cannot request "
                    f"passive scanning ({detail}). Upgrade bleak, or use 'auto'.")
            elif mode == "auto" and not supported:
                warnings.append(
                    f"scan.mode 'auto' cannot try passive on this bleak build "
                    f"({detail}); active scanning will be used")

        values: dict[str, Any] = {}
        for key, (floor, allow_floor) in numeric.items():
            if key not in scan_section:
                continue
            value = scan_section[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                problems.append(f"scan.{key} must be a number, got {value!r}")
                continue
            if value < floor or (value == floor and not allow_floor):
                comparison = ">=" if allow_floor else ">"
                problems.append(
                    f"scan.{key} must be {comparison} {floor:g}, got {value}")
                continue
            values[key] = float(value)

        if "max_empty_windows" in scan_section:
            value = scan_section["max_empty_windows"]
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                problems.append(f"scan.max_empty_windows must be an integer >= 1, got {value!r}")
            else:
                values["max_empty_windows"] = value

        if isinstance(mode, str) and mode.lower() in SCAN_MODES:
            values["mode"] = mode.lower()
        scan = replace(ScanConfig(), **values)

    # -- mqtt ------------------------------------------------------------ #
    mqtt = MqttConfig()
    mqtt_section = raw.get("mqtt", {})
    if not isinstance(mqtt_section, dict):
        problems.append("mqtt must be an object")
    else:
        strings = ("host", "username", "password", "password_file", "client_id",
                   "topic_prefix", "discovery_prefix")
        booleans = ("enabled", "tls", "retain", "individual_topics", "discovery")
        allowed = set(strings) | set(booleans) | {"port", "qos"}
        for key in sorted(_public_keys(mqtt_section) - allowed):
            problems.append(f"unknown key mqtt.{key!r}")

        values: dict[str, Any] = {}
        for key in strings:
            if key not in mqtt_section:
                continue
            value = mqtt_section[key]
            if value is None and key in ("username", "password", "password_file"):
                continue
            if not isinstance(value, str) or not value:
                problems.append(f"mqtt.{key} must be a non-empty string, got {value!r}")
            else:
                values[key] = value
        for key in booleans:
            if key not in mqtt_section:
                continue
            value = mqtt_section[key]
            if not isinstance(value, bool):
                problems.append(f"mqtt.{key} must be true or false, got {value!r}")
            else:
                values[key] = value
        if "port" in mqtt_section:
            value = mqtt_section["port"]
            if isinstance(value, bool) or not isinstance(value, int) \
                    or not 1 <= value <= 65535:
                problems.append(f"mqtt.port must be 1-65535, got {value!r}")
            else:
                values["port"] = value
        if "qos" in mqtt_section:
            value = mqtt_section["qos"]
            if isinstance(value, bool) or value not in (0, 1, 2):
                problems.append(f"mqtt.qos must be 0, 1 or 2, got {value!r}")
            else:
                values["qos"] = value

        for key in ("topic_prefix", "discovery_prefix"):
            prefix = values.get(key)
            if isinstance(prefix, str) and (set("+#") & set(prefix) or
                                            prefix.startswith("/")):
                problems.append(f"mqtt.{key} must not contain MQTT wildcards "
                                f"(+ #) or start with '/', got {prefix!r}")

        if values.get("password") and values.get("password_file"):
            problems.append("mqtt.password and mqtt.password_file are mutually "
                            "exclusive; prefer password_file")
        if values.get("password_file") and not Path(values["password_file"]).is_file():
            problems.append(f"mqtt.password_file {values['password_file']!r} "
                            "does not exist")

        mqtt = replace(MqttConfig(), **values)
        if mqtt.enabled:
            if _paho_import_error() is not None:
                problems.append(
                    f"mqtt.enabled is true but paho-mqtt is not importable "
                    f"({_paho_import_error()}). Install it with "
                    f"'pip install paho-mqtt>=2.0'.")
            if mqtt.username and not (mqtt.password or mqtt.password_file):
                warnings.append("mqtt.username is set without a password")
            if not mqtt.tls and mqtt.password_file:
                warnings.append("mqtt credentials will cross the network in "
                                "cleartext; consider mqtt.tls")

    # -- devices --------------------------------------------------------- #
    devices: list[Device] = []
    device_section = raw.get("devices", [])
    if not isinstance(device_section, list):
        problems.append("devices must be an array")
    else:
        seen_macs: dict[str, int] = {}
        seen_names: dict[str, int] = {}
        for index, entry in enumerate(device_section):
            where = f"devices[{index}]"
            if not isinstance(entry, dict):
                problems.append(f"{where} must be an object with 'mac' and 'name'")
                continue
            for key in sorted(_public_keys(entry) - {"mac", "name"}):
                problems.append(f"unknown key {where}.{key!r}")

            mac = entry.get("mac")
            if not isinstance(mac, str):
                problems.append(f"{where}.mac is required and must be a string")
                continue
            normalised = mac.strip().upper().replace("-", ":")
            if not MAC_RE.match(normalised):
                problems.append(f"{where}.mac {mac!r} is not a MAC address")
                continue
            if normalised in seen_macs:
                problems.append(f"{where}.mac {normalised} duplicates devices"
                                f"[{seen_macs[normalised]}]")
                continue
            seen_macs[normalised] = index

            name = entry.get("name", normalised)
            if not isinstance(name, str) or not name.strip():
                problems.append(f"{where}.name must be a non-empty string")
                continue
            name = name.strip()
            if name in seen_names:
                # Two devices sharing a name makes every dashboard legend and
                # alert annotation ambiguous, so treat it as an error.
                problems.append(f"{where}.name {name!r} duplicates devices"
                                f"[{seen_names[name]}]")
                continue
            seen_names[name] = index

            devices.append(Device(mac=normalised, name=name))

    # -- cross-field sanity ---------------------------------------------- #
    if scan.interval_seconds > 0:
        if scan.window_seconds > scan.interval_seconds:
            warnings.append(
                f"scan.window_seconds ({scan.window_seconds:g}) exceeds "
                f"scan.interval_seconds ({scan.interval_seconds:g}); it will be clamped")
        if scan.stale_after_seconds < 3 * scan.interval_seconds:
            warnings.append(
                f"scan.stale_after_seconds ({scan.stale_after_seconds:g}) is less than "
                f"three scan intervals; one missed window will expire your metrics")
        if not devices:
            warnings.append(
                "no devices listed, so a scan window has no completion condition and "
                "will always run for the full window_seconds. Listing the sensors you "
                "care about is what makes duty cycling efficient")

    if problems:
        raise ConfigError(problems)

    return Config(listen=listen, adapter=adapter, log_level=log_level,
                  scan=scan, mqtt=mqtt, devices=tuple(devices)), warnings


def load_config(path: Path | None) -> tuple[Config, Path | None, list[str]]:
    """Load config from path, or from the default locations, or use defaults."""
    if path is None:
        for candidate in DEFAULT_CONFIG_PATHS:
            if candidate.is_file():
                path = candidate
                break
        else:
            return Config(), None, ["no config file found, running on defaults"]

    try:
        text = path.read_text()
    except OSError as exc:
        raise ConfigError([f"cannot read config: {exc}"], path) from exc

    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            [f"invalid JSON at line {exc.lineno} column {exc.colno}: {exc.msg}. "
             f"Note that JSON has no comments; use keys beginning with '_' instead."],
            path) from exc

    try:
        config, warnings = parse_config(raw)
    except ConfigError as exc:
        raise ConfigError(exc.problems, path) from exc
    return config, path, warnings


EXAMPLE_CONFIG = {
    "_about": "SwitchBot BLE exporter. JSON has no comments, so keys starting "
              "with '_' are ignored and can be used for notes.",
    "listen": "0.0.0.0:9865",
    "_adapter": "BlueZ adapter such as 'hci0'. null picks the first one found.",
    "adapter": None,
    "log": {"level": "info"},
    "scan": {
        "_mode": "active | passive | auto. Passive never transmits, so it does "
                 "not solicit a scan response from the sensor, and it filters in "
                 "the controller. It needs BlueZ >= 5.56 with experimental "
                 "features enabled and silently delivers nothing otherwise, so "
                 "'auto' tries passive and falls back to active if it sees no "
                 "data. Passive and duty cycling combine; they are independent.",
        "mode": "active",
        "_interval_seconds": "Seconds between scan windows; set this to your "
                             "Prometheus scrape interval. 0 scans continuously.",
        "interval_seconds": 60,
        "_window_seconds": "Maximum radio-on time per window. The sensor "
                           "advertises every 5-10s and frames get lost, so allow "
                           "several intervals. Windows end early once every "
                           "listed device has reported.",
        "window_seconds": 25,
        "max_empty_windows": 3,
        "_stale_after_seconds": "Drop a device's metrics after this long without "
                                "a reading. Keep it above three scan intervals.",
        "stale_after_seconds": 300,
        "_watchdog_timeout_seconds": "Continuous mode only.",
        "watchdog_timeout_seconds": 120,
    },
    "mqtt": {
        "_about": "Publishes every decoded reading as JSON. A broker outage never "
                  "affects Prometheus scraping; failures are counted in "
                  "switchbot_mqtt_errors_total.",
        "enabled": False,
        "host": "localhost",
        "port": 1883,
        "username": None,
        "_password_file": "Preferred over 'password' so the secret stays out of "
                          "this file, which the service group can read.",
        "password_file": None,
        "tls": False,
        "client_id": "switchbot-exporter",
        "_topic_prefix": "State is published to {topic_prefix}/{device}/state, "
                         "and availability to {topic_prefix}/status via a last "
                         "will so subscribers notice an ungraceful exit.",
        "topic_prefix": "switchbot",
        "qos": 0,
        "retain": True,
        "_individual_topics": "Also publish each field to its own topic, for "
                              "subscribers that would rather not parse JSON.",
        "individual_topics": False,
        "_discovery": "Publish Home Assistant MQTT discovery so the sensors "
                      "appear automatically with correct units and device "
                      "classes. expire_after is derived from "
                      "scan.stale_after_seconds.",
        "discovery": True,
        "discovery_prefix": "homeassistant",
    },
    "_devices": "Omit or leave empty to export every SwitchBot meter in range, "
                "at the cost of full-length scan windows.",
    "devices": [
        {"mac": "EB:6B:01:C6:2B:2C", "name": "garden"},
        {"mac": "AA:BB:CC:DD:EE:FF", "name": "greenhouse"},
    ],
}


# --------------------------------------------------------------------------- #
# Decoding (unchanged, validated against the vendor sample frame)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class Reading:
    mac: str
    model: str
    temperature_c: float
    humidity_pct: int
    battery_pct: int | None
    rssi_dbm: int | None
    timestamp: float

    @property
    def dew_point_c(self) -> float:
        rh = max(self.humidity_pct, 1) / 100.0
        gamma = math.log(rh) + (17.62 * self.temperature_c) / (243.12 + self.temperature_c)
        return (243.12 * gamma) / (17.62 - gamma)

    @property
    def absolute_humidity_g_m3(self) -> float:
        t = self.temperature_c
        svp = 6.112 * math.exp((17.62 * t) / (243.12 + t))
        return 216.7 * svp * (self.humidity_pct / 100.0) / (273.15 + t)


def _decode_temperature(frac_byte: int, int_byte: int) -> float:
    tenths = frac_byte & 0x0F
    integer = int_byte & 0x7F
    sign = 1 if int_byte & 0x80 else -1
    return round(sign * (integer + tenths / 10.0), 1)


def decode_advertisement(mac: str, adv: AdvertisementData, *,
                         now: float | None = None) -> Reading | None:
    service_data = adv.service_data.get(SWITCHBOT_SERVICE_UUID)
    if not service_data:
        return None

    device_type = service_data[0] & 0x7F
    mfr_data = adv.manufacturer_data.get(SWITCHBOT_COMPANY_ID, b"")

    if device_type == DEVICE_TYPE_OUTDOOR_METER and len(mfr_data) >= 11:
        temperature = _decode_temperature(mfr_data[8], mfr_data[9])
        humidity = mfr_data[10] & 0x7F
        battery = (service_data[2] & 0x7F) if len(service_data) >= 3 else None
        model = "outdoor_meter"
    elif device_type in MODEL_NAMES:
        if len(service_data) < 6:
            raise DecodeError(f"service data is {len(service_data)} bytes, need 6")
        battery = service_data[2] & 0x7F
        temperature = _decode_temperature(service_data[3], service_data[4])
        humidity = service_data[5] & 0x7F
        model = MODEL_NAMES[device_type]
    else:
        return None

    if not -30.0 <= temperature <= 100.0:
        raise DecodeError(f"temperature out of range: {temperature}")
    if not 0 <= humidity <= 100:
        raise DecodeError(f"humidity out of range: {humidity}")
    if battery is not None and not 0 <= battery <= 100:
        raise DecodeError(f"battery out of range: {battery}")

    return Reading(mac.upper(), model, temperature, humidity, battery,
                   adv.rssi, now if now is not None else time.time())


# --------------------------------------------------------------------------- #
# MQTT
# --------------------------------------------------------------------------- #

def _paho_import_error() -> str | None:
    """None when paho-mqtt is importable, otherwise the reason.

    Probed by module rather than by importing a name, so the check does not leave
    an unused binding behind.
    """
    import importlib.util
    try:
        if importlib.util.find_spec("paho.mqtt.client") is None:
            return "no module named 'paho.mqtt.client'"
    except (ImportError, ValueError) as exc:
        return str(exc)
    return None


def topic_slug(name: str) -> str:
    """Make a name safe for an MQTT topic level.

    MQTT reserves + and # as wildcards and / as a separator, so a device called
    "Balcony / North" cannot appear verbatim in a topic.
    """
    slug = re.sub(r"[^a-z0-9_-]+", "_", name.strip().lower()).strip("_")
    return slug or "unnamed"


# Home Assistant discovery descriptors: reading attribute, HA device class,
# unit, and how many decimals are meaningful.
HA_SENSORS: tuple[tuple[str, str, str, str, int], ...] = (
    ("temperature_c", "Temperature", "temperature", "°C", 1),
    ("humidity_pct", "Humidity", "humidity", "%", 0),
    ("battery_pct", "Battery", "battery", "%", 0),
    ("dew_point_c", "Dew point", "temperature", "°C", 1),
    ("absolute_humidity_g_m3", "Absolute humidity", "humidity", "g/m³", 2),
    ("rssi_dbm", "Signal strength", "signal_strength", "dBm", 0),
)


class MqttPublisher:
    """Publishes each reading to MQTT, with optional Home Assistant discovery.

    A broker outage must never affect Prometheus scraping, so every failure here
    is counted and logged rather than raised. paho runs its own network thread
    and reconnects on its own; publish() only enqueues.
    """

    def __init__(self, config: MqttConfig, metrics: Metrics,
                 expire_after: float) -> None:
        self._cfg = config
        self._m = metrics
        self._expire_after = int(expire_after)
        self._client: Any = None
        self._announced: set[str] = set()
        self._stopping = False
        self._availability = f"{config.topic_prefix}/status"

    # -- lifecycle --------------------------------------------------------- #

    def start(self) -> None:
        import paho.mqtt.client as mqtt

        try:
            # paho 2.x requires an explicit callback API version; 1.x has no such
            # parameter.
            self._client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2, client_id=self._cfg.client_id)
            self._v2_callbacks = True
        except (AttributeError, TypeError):  # pragma: no cover - paho 1.x
            self._client = mqtt.Client(client_id=self._cfg.client_id)
            self._v2_callbacks = False

        if self._cfg.username:
            self._client.username_pw_set(self._cfg.username,
                                         self._cfg.resolve_password())
        if self._cfg.tls:
            self._client.tls_set()

        # Last will, so subscribers learn about an ungraceful exit. Home Assistant
        # marks the entities unavailable rather than showing a frozen value.
        self._client.will_set(self._availability, "offline", qos=1, retain=True)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect

        self._client.connect_async(self._cfg.host, self._cfg.port, keepalive=60)
        self._client.loop_start()
        log.info("mqtt: connecting to %s:%d as %r",
                 self._cfg.host, self._cfg.port, self._cfg.client_id)

    def stop(self) -> None:
        if self._client is None:
            return
        # So the disconnect callback does not warn about an expected disconnect.
        self._stopping = True
        with contextlib.suppress(Exception):
            # Publish availability explicitly: a clean shutdown does not trigger
            # the last will.
            self._client.publish(self._availability, "offline", qos=1, retain=True)
            self._client.disconnect()
        with contextlib.suppress(Exception):
            self._client.loop_stop()
        self._m.mqtt_connected.set(0)
        log.info("mqtt: disconnected")

    # -- paho callbacks (run on paho's network thread) --------------------- #

    def _on_connect(self, client: Any, userdata: Any, flags: Any,
                    reason_code: Any = 0, properties: Any = None) -> None:
        failed = getattr(reason_code, "is_failure", None)
        if failed is None:
            failed = reason_code != 0
        if failed:
            self._m.mqtt_errors.inc()
            log.error("mqtt: connection refused: %s", reason_code)
            return

        self._m.mqtt_connected.set(1)
        log.info("mqtt: connected to %s:%d", self._cfg.host, self._cfg.port)
        client.publish(self._availability, "online", qos=1, retain=True)
        # Re-announce after a reconnect: the broker may have been restarted
        # without persistence, losing the retained discovery messages.
        self._announced.clear()

    def _on_disconnect(self, client: Any, userdata: Any, *args: Any) -> None:
        self._m.mqtt_connected.set(0)
        if self._stopping:
            return
        log.warning("mqtt: disconnected, paho will retry")

    # -- publishing -------------------------------------------------------- #

    def publish(self, reading: Reading, name: str) -> None:
        if self._client is None:
            return
        slug = topic_slug(name)
        payload = {
            "mac": reading.mac,
            "name": name,
            "model": reading.model,
            "temperature_c": reading.temperature_c,
            "humidity_pct": reading.humidity_pct,
            "dew_point_c": round(reading.dew_point_c, 2),
            "absolute_humidity_g_m3": round(reading.absolute_humidity_g_m3, 2),
            "rssi_dbm": reading.rssi_dbm,
            "timestamp": _isoformat(reading.timestamp),
        }
        if reading.battery_pct is not None:
            payload["battery_pct"] = reading.battery_pct

        if self._cfg.discovery and reading.mac not in self._announced:
            self._announce(reading, name, slug)

        base = f"{self._cfg.topic_prefix}/{slug}"
        self._send(f"{base}/state", json.dumps(payload, ensure_ascii=False))

        if self._cfg.individual_topics:
            # For subscribers that would rather not parse JSON, e.g. simple
            # Node-RED flows or an ESPHome display.
            for key, value in payload.items():
                if isinstance(value, (int, float)):
                    self._send(f"{base}/{key}", str(value))

    def _send(self, topic: str, payload: str, retain: bool | None = None) -> None:
        try:
            info = self._client.publish(
                topic, payload, qos=self._cfg.qos,
                retain=self._cfg.retain if retain is None else retain)
        except Exception as exc:  # paho raises on a bad topic or oversized payload
            self._m.mqtt_errors.inc()
            log.warning("mqtt: publish to %s failed: %s", topic, exc)
            return
        if info.rc != 0:
            # rc != 0 usually means the client is offline. paho queues QoS>0 and
            # drops QoS 0, so this is expected during an outage.
            self._m.mqtt_errors.inc()
            log.debug("mqtt: publish to %s returned rc=%s", topic, info.rc)
            return
        self._m.mqtt_published.inc()

    def _announce(self, reading: Reading, name: str, slug: str) -> None:
        """Publish Home Assistant MQTT discovery for one device."""
        device_id = f"switchbot_{reading.mac.replace(':', '').lower()}"
        device = {
            "identifiers": [device_id],
            "connections": [["mac", reading.mac]],
            "name": name,
            "manufacturer": "SwitchBot",
            "model": reading.model,
        }
        state_topic = f"{self._cfg.topic_prefix}/{slug}/state"

        for attribute, label, device_class, unit, decimals in HA_SENSORS:
            if attribute == "battery_pct" and reading.battery_pct is None:
                continue
            config = {
                "name": label,
                "unique_id": f"{device_id}_{attribute}",
                "object_id": f"{slug}_{attribute}",
                "state_topic": state_topic,
                "value_template": (
                    "{{ value_json.%s | round(%d) }}" % (attribute, decimals)),
                "availability_topic": self._availability,
                "device_class": device_class,
                "unit_of_measurement": unit,
                "state_class": "measurement",
                # Without this a retained reading would look current forever.
                "expire_after": self._expire_after,
                "device": device,
            }
            if device_class == "signal_strength":
                config["entity_category"] = "diagnostic"
            topic = (f"{self._cfg.discovery_prefix}/sensor/"
                     f"{device_id}/{attribute}/config")
            # Discovery is always retained: Home Assistant reads it at startup.
            self._send(topic, json.dumps(config, ensure_ascii=False), retain=True)

        self._announced.add(reading.mac)
        log.info("mqtt: announced %s to Home Assistant discovery as %s",
                 name, device_id)


def _isoformat(timestamp: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #

class Metrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        # None means prometheus_client's default global registry, which is what
        # the running exporter wants. Passing a fresh CollectorRegistry lets a
        # caller build more than one Metrics without a duplicate-timeseries
        # error, which is how the tests keep cases independent.
        self.registry = registry
        target: dict[str, Any] = {} if registry is None else {"registry": registry}

        def gauge(name: str, help_: str, labels: Sequence[str] = LABELS) -> Gauge:
            return Gauge(name, help_, labels, **target)

        def counter(name: str, help_: str, labels: Sequence[str] = ()) -> Counter:
            return Counter(name, help_, labels, **target)

        self.info = Info("switchbot_exporter_build", "Exporter build information",
                         **target)
        self.info.info({"version": __version__})

        self.temperature = gauge("switchbot_temperature_celsius",
                                 "Temperature reported by the sensor.")
        self.humidity = gauge("switchbot_humidity_percent",
                              "Relative humidity reported by the sensor.")
        self.battery = gauge("switchbot_battery_percent", "Battery charge remaining.")
        self.dew_point = gauge("switchbot_dew_point_celsius",
                               "Dew point derived from temperature and humidity.")
        self.absolute_humidity = gauge(
            "switchbot_absolute_humidity_grams_per_cubic_meter",
            "Absolute humidity derived from temperature and relative humidity.")
        self.rssi = gauge("switchbot_rssi_dbm",
                          "Signal strength of the most recent advertisement.")
        self.last_seen = gauge("switchbot_last_seen_timestamp_seconds",
                               "Unix time of the last decoded advertisement.")
        self.advertisements = counter("switchbot_advertisements_total",
                                      "Advertisements decoded successfully.", LABELS)
        # Every BLE frame reaching this process, from any device, SwitchBot or
        # not. This is the receiver-health signal: it distinguishes "the radio is
        # deaf" from "my sensor is quiet", which no per-device counter can.
        self.ble_seen = counter(
            "switchbot_ble_advertisements_seen_total",
            "BLE advertisements received from any device, before any filtering.")
        self.ble_window = gauge(
            "switchbot_ble_advertisements_last_window",
            "BLE advertisements from any device during the last scan window. Use "
            "this rather than a rate() over the counter when duty cycling: a rate "
            "is per second of wall clock, and the radio is off most of that.",
            labels=())
        self.unmatched = counter(
            "switchbot_advertisements_unmatched_total",
            "Advertisements decoded as SwitchBot meters that are not listed in the "
            "config. A steady non-zero rate with no readings means a mistyped MAC.")

        self.window_adverts = gauge(
            "switchbot_advertisements_last_window",
            "Advertisements heard from this device during the last scan window. A "
            "decline means the link is degrading even while readings still arrive.")

        self.scan_windows = counter("switchbot_scan_windows_total", "Scan windows started.")
        self.scan_seconds = counter("switchbot_scan_seconds_total",
                                    "Cumulative seconds spent with the radio scanning.")
        self.empty_windows = counter("switchbot_scan_windows_empty_total",
                                     "Scan windows that heard nothing at all.")
        self.incomplete_windows = counter(
            "switchbot_scan_windows_incomplete_total",
            "Scan windows that expired before every configured device reported.")
        self.window_duration = gauge("switchbot_scan_window_duration_seconds",
                                     "Duration of the most recent scan window.", labels=())
        self.scan_in_progress = gauge("switchbot_scan_in_progress",
                                      "1 while a scan window is open.", labels=())
        self.devices_in_range = gauge(
            "switchbot_devices_in_range",
            "SwitchBot devices decoded during the last window, including ones not "
            "listed in the config. Compare against your configured device count to "
            "catch a mistyped MAC.", labels=())
        self.scanner_up = gauge(
            "switchbot_scanner_up",
            "1 when the most recent scan window produced at least one reading.", labels=())
        self.scan_passive = gauge(
            "switchbot_scan_passive",
            "1 when scanning passively (receive only, no scan requests), 0 when "
            "scanning actively. In 'auto' mode this flips to 0 on fallback.", labels=())
        self.mode_fallbacks = counter(
            "switchbot_scan_mode_fallbacks_total",
            "Times 'auto' mode gave up on passive scanning and fell back to active.")

        self.mqtt_connected = gauge("switchbot_mqtt_connected",
                                    "1 when the MQTT broker connection is up.",
                                    labels=())
        self.mqtt_published = counter("switchbot_mqtt_messages_published_total",
                                      "MQTT messages accepted by the client.")
        self.mqtt_errors = counter("switchbot_mqtt_errors_total",
                                   "MQTT publish or connection failures.")

        self.decode_errors = counter("switchbot_decode_errors_total",
                                     "Advertisements that failed to decode.")
        self.scanner_restarts = counter("switchbot_scanner_restarts_total",
                                        "BLE scanner restarts.")

        self._per_device = (self.temperature, self.humidity, self.battery,
                            self.dew_point, self.absolute_humidity, self.rssi,
                            self.last_seen, self.window_adverts)

    def publish(self, reading: Reading, name: str) -> None:
        labels = (reading.mac, name, reading.model)
        self.temperature.labels(*labels).set(reading.temperature_c)
        self.humidity.labels(*labels).set(reading.humidity_pct)
        self.dew_point.labels(*labels).set(reading.dew_point_c)
        self.absolute_humidity.labels(*labels).set(reading.absolute_humidity_g_m3)
        self.last_seen.labels(*labels).set(reading.timestamp)
        self.advertisements.labels(*labels).inc()
        if reading.battery_pct is not None:
            self.battery.labels(*labels).set(reading.battery_pct)
        if reading.rssi_dbm is not None:
            self.rssi.labels(*labels).set(reading.rssi_dbm)

    def expire(self, labels: tuple[str, str, str]) -> None:
        for g in self._per_device:
            with contextlib.suppress(KeyError):
                g.remove(*labels)


@dataclass
class WindowResult:
    duration: float
    adverts: int
    devices: frozenset[str]
    complete: bool
    all_adverts: int = 0
    switchbot_seen: frozenset[str] = frozenset()


# --------------------------------------------------------------------------- #
# Exporter
# --------------------------------------------------------------------------- #

ScannerFactory = Callable[[Callable[[BLEDevice, AdvertisementData], None]], Any]


class Exporter:
    def __init__(self, config: Config, metrics: Metrics,
                 scanner_factory: ScannerFactory | None = None,
                 publisher: "MqttPublisher | None" = None) -> None:
        self._cfg = config
        self._m = metrics
        self._scanner_factory = scanner_factory or self._default_scanner
        # Constructed here but not connected until run(), so building an Exporter
        # stays side-effect free and testable.
        if publisher is not None:
            self._mqtt: MqttPublisher | None = publisher
        elif config.mqtt.enabled:
            self._mqtt = MqttPublisher(config.mqtt, metrics,
                                       config.scan.stale_after_seconds)
        else:
            self._mqtt = None
        self._devices: dict[str, tuple[tuple[str, str, str], float]] = {}
        self._stop = asyncio.Event()

        self._window_devices: set[str] = set()
        self._window_adverts: dict[str, int] = {}
        self._window_total = 0
        self._window_all_adverts = 0
        self._window_switchbot: set[str] = set()
        self._window_complete = asyncio.Event()
        self._last_advert = 0.0
        # (mac, rssi, monotonic time) of the most recent successful decode.
        self._last_reading: tuple[str, int | None, float] | None = None
        # Three separate clocks, because "nothing is arriving" has three very
        # different causes and only these together tell them apart:
        #   _last_advert       last reading from a CONFIGURED device
        #   _last_switchbot    last frame from ANY SwitchBot, configured or not
        #   _last_any          last BLE frame from ANY device at all
        self._last_switchbot = 0.0
        self._last_any = 0.0
        self._silence_frames = 0
        self._silence_switchbot = 0

        # Passive is attempted for both 'passive' and 'auto'; only 'auto' is
        # allowed to give up on it.
        self._passive = config.scan.mode in ("passive", "auto")
        if self._passive and not passive_support()[0]:
            # Validation rejects mode 'passive' in this case, so this can only be
            # 'auto', which is permitted to degrade.
            self._passive = False
        self._m.scan_passive.set(1 if self._passive else 0)

    def _default_scanner(self, callback: Callable[[BLEDevice, AdvertisementData], None]) -> Any:
        # Deliberately no service_uuids: see the module docstring. It matches the
        # service CLASS uuid list, which these devices do not advertise, and would
        # drop every advertisement.
        kwargs: dict[str, Any] = {"detection_callback": callback}

        # DuplicateData must be forced on. bleak defaults it to False, and in
        # BlueZ's wording that means duplicate *suppression* is enabled: the
        # daemon stops emitting PropertiesChanged once a device's ServiceData and
        # ManufacturerData stop changing. A thermometer in stable conditions
        # broadcasts a byte-identical payload for minutes at a time, so the
        # symptom is one reading at startup and then silence, while unrelated BLE
        # traffic keeps arriving. Not a range problem, though it looks exactly
        # like one.
        bluez: dict[str, Any] = {"filters": {"DuplicateData": True}}
        if self._cfg.adapter:
            # bleak 3.x moved adapter into the bluez dict.
            bluez["adapter"] = self._cfg.adapter

        if self._passive:
            from bleak.assigned_numbers import AdvertisementDataType
            try:
                from bleak.args.bluez import OrPattern
            except ImportError:  # pragma: no cover - older bleak layout
                from bleak.backends.bluezdbus.advertisement_monitor import OrPattern

            # Matches AD type 0x16 (service data, 16-bit uuid) beginning with
            # 0xFD3D little-endian. This is the filter service_uuids could not
            # express: it matches service DATA, which is what SwitchBot sends.
            # Unlike service_uuids it also runs in the controller, so it is the
            # only genuine below-the-process filtering available here.
            bluez["or_patterns"] = [
                OrPattern(0, AdvertisementDataType.SERVICE_DATA_UUID16, b"\x3d\xfd")
            ]
            kwargs["scanning_mode"] = "passive"

        if bluez:
            kwargs["bluez"] = bluez
        return BleakScanner(**kwargs)

    def _fall_back_to_active(self, reason: str) -> bool:
        """Give up on passive scanning. Returns True if a switch happened."""
        if not self._passive or self._cfg.scan.mode != "auto":
            return False
        self._passive = False
        self._m.scan_passive.set(0)
        self._m.mode_fallbacks.inc()
        log.error(
            "passive scanning produced nothing (%s); falling back to active. "
            "BlueZ accepted the advertisement monitor but delivered no data, which "
            "usually means experimental features are disabled. Try "
            "'Experimental = true' in /etc/bluetooth/main.conf, then set "
            "scan.mode back to 'passive'.", reason)
        return True

    def _on_advert(self, device: BLEDevice, adv: AdvertisementData) -> None:
        self._window_all_adverts += 1
        self._m.ble_seen.inc()
        self._last_any = time.monotonic()
        self._silence_frames += 1
        mac = device.address.upper()
        try:
            reading = decode_advertisement(mac, adv)
        except DecodeError as exc:
            self._m.decode_errors.inc()
            log.warning("decode error from %s: %s", mac, exc)
            return
        if reading is None:
            return

        # Recorded before the device filter so a silent window can tell
        # "no sensor in range" apart from "wrong MAC in the config".
        self._window_switchbot.add(mac)
        self._last_switchbot = time.monotonic()
        self._silence_switchbot += 1
        configured = self._cfg.macs
        if configured and mac not in configured:
            # Deliberately not labelled by MAC: a block of flats can contain an
            # unbounded number of other people's SwitchBots, and each would
            # become a permanent time series. The MAC appears in the log and in
            # switchbot_devices_in_range instead.
            self._m.unmatched.inc()
            return

        name = self._cfg.names.get(mac, mac)
        labels = (reading.mac, name, reading.model)
        self._devices[mac] = (labels, time.monotonic())
        self._m.publish(reading, name)
        if self._mqtt is not None:
            # Never let a broker problem interfere with Prometheus export.
            try:
                self._mqtt.publish(reading, name)
            except Exception as exc:
                self._m.mqtt_errors.inc()
                log.warning("mqtt: publishing %s failed: %s", name, exc)

        self._window_devices.add(mac)
        self._window_adverts[mac] = self._window_adverts.get(mac, 0) + 1
        self._window_total += 1
        self._last_advert = time.monotonic()
        self._last_reading = (reading.mac, reading.rssi_dbm, self._last_advert)

        log.debug("%s (%s) %.1f°C %d%% rh battery=%s rssi=%s",
                  name, reading.model, reading.temperature_c, reading.humidity_pct,
                  reading.battery_pct, reading.rssi_dbm)

        if configured and configured <= self._window_devices:
            self._window_complete.set()

    async def _scan_window(self, window: float) -> WindowResult:
        self._window_devices = set()
        self._window_adverts = {}
        self._window_total = 0
        self._window_all_adverts = 0
        self._window_switchbot = set()
        self._window_complete.clear()

        started = time.monotonic()
        self._m.scan_windows.inc()
        self._m.scan_in_progress.set(1)
        try:
            async with self._scanner_factory(self._on_advert):
                if self._cfg.macs:
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self._window_complete.wait(), timeout=window)
                else:
                    await self._sleep(window)
        finally:
            self._m.scan_in_progress.set(0)

        duration = time.monotonic() - started
        complete = bool(self._cfg.macs) and self._cfg.macs <= self._window_devices

        self._m.scan_seconds.inc(duration)
        self._m.window_duration.set(duration)
        self._m.scanner_up.set(1 if self._window_total else 0)
        self._m.devices_in_range.set(len(self._window_switchbot))
        self._m.ble_window.set(self._window_all_adverts)
        if not self._window_total:
            self._m.empty_windows.inc()
        if self._cfg.macs and not complete:
            self._m.incomplete_windows.inc()
            missing = sorted(self._cfg.macs - self._window_devices)
            log.warning("window expired after %.1fs without hearing from %s",
                        duration, ", ".join(missing))

        for mac, count in self._window_adverts.items():
            self._m.window_adverts.labels(*self._devices[mac][0]).set(count)

        return WindowResult(duration, self._window_total,
                            frozenset(self._window_devices), complete,
                            self._window_all_adverts, frozenset(self._window_switchbot))

    def _diagnose_silence(self, silence: float) -> str:
        """Explain why no configured device has been heard for `silence` seconds.

        The distinction that matters is what else arrived in the meantime. Without
        it, a deaf adapter and an out-of-range sensor produce identical logs, and
        the operator has no way to choose between checking the Pi and walking
        outside with a ladder.
        """
        frames = self._silence_frames
        switchbots = self._silence_switchbot
        now = time.monotonic()

        if frames == 0:
            return (f"no BLE advertisements of ANY kind in {silence:.0f}s. The "
                    f"adapter is not receiving, so this is not your sensor: check "
                    f"'dmesg | grep -i blue' for HCI errors, 'rfkill list', and "
                    f"whether bluetoothd is alive.")

        detail = (f"{frames} BLE advertisement(s) from other devices arrived in "
                  f"that time, so the radio and the exporter are both working.")
        if switchbots:
            return (f"{detail} {switchbots} were from a SwitchBot but none from a "
                    f"configured device — check the MAC addresses in the config.")

        last = ""
        if self._last_reading:
            mac, rssi, when = self._last_reading
            age = now - when
            last = (f" {mac} was last heard {age / 60:.0f} min ago"
                    + (f" at {rssi} dBm" if rssi is not None else "") + ".")
            if rssi is not None and rssi < -85:
                last += " That was already weak, so a small change could tip it."
        return (f"{detail} Nothing at all from a SwitchBot, so this is the sensor: "
                f"range, obstruction, or batteries.{last}")

    def _reset_silence_counters(self) -> None:
        self._silence_frames = 0
        self._silence_switchbot = 0

    def _last_heard_hint(self) -> str:
        """Describe the last successful contact, to gauge how marginal the link is."""
        if not self._last_reading:
            return ". No sensor has ever been heard in this process"
        mac, rssi, when = self._last_reading
        age = time.monotonic() - when
        strength = "" if rssi is None else f" at {rssi} dBm"
        return (f". {mac} was last heard {age:.0f}s ago{strength}"
                + (" — below about -90 dBm expect frequent loss"
                   if rssi is not None and rssi < -90 else ""))

    def _explain_empty_window(self, result: WindowResult, streak: int) -> None:
        if result.switchbot_seen:
            log.warning(
                "window %.1fs produced nothing (%d in a row): heard %d BLE "
                "advertisement(s) including SwitchBot device(s) %s, none of which are "
                "in the config. Check the MAC addresses you configured.",
                result.duration, streak, result.all_adverts,
                ", ".join(sorted(result.switchbot_seen)))
        elif result.all_adverts:
            log.warning(
                "window %.1fs produced nothing (%d in a row): heard %d BLE "
                "advertisement(s) but none from a SwitchBot. The radio works, so "
                "this is range, obstruction or batteries%s.",
                result.duration, streak, result.all_adverts,
                self._last_heard_hint())
        elif self._passive:
            log.warning(
                "window %.1fs produced nothing (%d in a row): no BLE advertisements "
                "of any kind, and scanning is PASSIVE. That is the most likely "
                "cause: BlueZ can accept an advertisement monitor and still deliver "
                "nothing when experimental features are off. Set scan.mode to "
                "'auto' or 'active', or enable 'Experimental = true' in "
                "/etc/bluetooth/main.conf.", result.duration, streak)
        else:
            log.warning(
                "window %.1fs produced nothing (%d in a row): no BLE advertisements "
                "of any kind. The adapter is not receiving; check 'rfkill list' and "
                "'systemctl status bluetooth'.", result.duration, streak)

    async def _scan_duty_cycled(self) -> None:
        interval = self._cfg.scan.interval_seconds
        window = min(self._cfg.scan.window_seconds, interval)
        empty_streak = 0
        backoff = 1.0

        while not self._stop.is_set():
            cycle_started = time.monotonic()
            try:
                result = await self._scan_window(window)
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._m.scanner_up.set(0)
                self._m.scanner_restarts.inc()
                log.error("scan window failed: %s", exc)
                if not await self._sleep_interruptible(backoff):
                    return
                backoff = min(backoff * 2, 60.0)
                continue

            if result.adverts:
                empty_streak = 0
                log.info("window %.1fs: %d beacon(s) from %d configured device(s), "
                         "%d BLE advertisement(s) seen in total%s",
                         result.duration, result.adverts, len(result.devices),
                         result.all_adverts,
                         "" if result.complete or not self._cfg.macs else " (incomplete)")
            else:
                empty_streak += 1
                self._explain_empty_window(result, empty_streak)
                if empty_streak >= self._cfg.scan.max_empty_windows:
                    if self._fall_back_to_active(f"{empty_streak} empty windows"):
                        empty_streak = 0
                    else:
                        self._m.scanner_restarts.inc()
                        log.error("%d empty windows in a row; rebuilding the scanner",
                                  empty_streak)
                        empty_streak = 0

            idle = max(0.0, interval - (time.monotonic() - cycle_started))
            log.debug("radio off for %.1fs (duty cycle %.1f%%)",
                      idle, 100.0 * result.duration / max(interval, 1e-9))
            if not await self._sleep_interruptible(idle):
                return

    async def _scan_continuous(self) -> None:
        timeout = self._cfg.scan.watchdog_timeout_seconds
        backoff = 1.0
        silent_cycles = 0
        while not self._stop.is_set():
            try:
                async with self._scanner_factory(self._on_advert):
                    self._m.scan_in_progress.set(1)
                    started = time.monotonic()
                    self._last_advert = started
                    self._reset_silence_counters()
                    log.info("scanning continuously")
                    while not self._stop.is_set():
                        await self._sleep(1.0)
                        silence = time.monotonic() - self._last_advert
                        self._m.scanner_up.set(1 if silence < timeout else 0)
                        if silence > timeout:
                            if not self._fall_back_to_active(f"{silence:.0f}s of silence"):
                                log.warning(
                                    "no readings from a configured device for "
                                    "%.0fs, cycling scanner. %s",
                                    silence, self._diagnose_silence(silence))
                            silent_cycles += 1
                            break

                    # A session counts as healthy only if it actually produced a
                    # reading. Resetting on elapsed time alone is what let this
                    # stop/start the controller every ~120s for an hour straight:
                    # each silent session looked "long enough to be fine".
                    if self._last_advert > started:
                        silent_cycles = 0
                        backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("scanner failed: %s", exc)
            finally:
                self._m.scan_in_progress.set(0)
                self._m.scanner_up.set(0)

            if self._stop.is_set():
                return
            self._m.scanner_restarts.inc()
            if silent_cycles >= 3:
                log.warning("%d consecutive silent scanner sessions; backing off to "
                            "%.0fs between restarts rather than cycling the "
                            "controller every %.0fs",
                            silent_cycles, backoff, timeout)
            log.info("restarting scanner in %.0fs", backoff)
            if not await self._sleep_interruptible(backoff):
                return
            backoff = min(backoff * 2, 60.0)

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    async def _sleep_interruptible(self, seconds: float) -> bool:
        await self._sleep(seconds)
        return not self._stop.is_set()

    async def _reap_stale(self) -> None:
        stale_after = self._cfg.scan.stale_after_seconds
        while not self._stop.is_set():
            now = time.monotonic()
            for mac, (labels, seen) in list(self._devices.items()):
                if now - seen > stale_after:
                    log.warning("%s stale for %.0fs, expiring metrics",
                                labels[1], now - seen)
                    self._m.expire(labels)
                    del self._devices[mac]
            await self._sleep(1.0)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, self._stop.set)

        start_http_server(self._cfg.port, addr=self._cfg.host)
        log.info("metrics on http://%s:%d/metrics", self._cfg.host, self._cfg.port)

        if self._mqtt is not None:
            self._mqtt.start()
        else:
            log.info("mqtt: disabled")
        if self._cfg.devices:
            log.info("configured devices: %s",
                     ", ".join(f"{d.name} ({d.mac})" for d in self._cfg.devices))
        else:
            log.info("no devices configured; exporting every SwitchBot meter in range")

        log.info("scan mode: %s (currently %s)", self._cfg.scan.mode,
                 "passive" if self._passive else "active")
        if self._cfg.scan.interval_seconds > 0:
            log.info("duty-cycled scanning: up to %.0fs every %.0fs",
                     min(self._cfg.scan.window_seconds, self._cfg.scan.interval_seconds),
                     self._cfg.scan.interval_seconds)
            scan_task = asyncio.create_task(self._scan_duty_cycled())
        else:
            scan_task = asyncio.create_task(self._scan_continuous())

        tasks = [scan_task, asyncio.create_task(self._reap_stale())]
        try:
            await self._stop.wait()
        finally:
            log.info("shutting down")
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self._mqtt is not None:
                self._mqtt.stop()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Prometheus exporter for SwitchBot BLE thermo-hygrometers.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-c", "--config", type=Path,
                   default=(Path(os.environ["SWITCHBOT_CONFIG"])
                            if os.environ.get("SWITCHBOT_CONFIG") else None),
                   help="path to the JSON config file. Defaults to "
                        f"{' then '.join(str(p) for p in DEFAULT_CONFIG_PATHS)}.")
    p.add_argument("--check-config", action="store_true",
                   help="validate the config and exit. Useful as an "
                        "ExecStartPre= guard in a systemd unit.")
    p.add_argument("--example-config", action="store_true",
                   help="print a documented example config and exit")
    p.add_argument("--log-level", choices=LOG_LEVELS,
                   help="override log.level from the config")
    args = p.parse_args(argv)

    if args.example_config:
        # Tolerate being piped into head/less, which closes the pipe early.
        with contextlib.suppress(BrokenPipeError):
            print(json.dumps(EXAMPLE_CONFIG, indent=2))
            sys.stdout.flush()
        return 0

    # Logging is configured before loading so that config errors are formatted
    # consistently; the level is corrected once the config is known.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z", stream=sys.stderr)

    try:
        config, path, warnings = load_config(args.config)
    except ConfigError as exc:
        exc.report()
        return 2

    if args.log_level:
        config = replace(config, log_level=args.log_level)
    logging.getLogger().setLevel(config.log_level.upper())

    if args.check_config:
        print(f"config OK: {path or 'defaults'}")
        for warning in warnings:
            print(f"  warning: {warning}")
        print(f"  listen  : {config.host}:{config.port}")
        print(f"  adapter : {config.adapter or 'first available'}")
        supported, detail = passive_support()
        print(f"  mode    : {config.scan.mode}"
              + ("" if supported else f"  (passive unavailable: {detail})"))
        print("  scanning: " + (
            f"duty-cycled, up to {min(config.scan.window_seconds, config.scan.interval_seconds):g}s "
            f"every {config.scan.interval_seconds:g}s"
            if config.scan.interval_seconds > 0 else "continuous"))
        if config.mqtt.enabled:
            print(f"  mqtt    : {config.mqtt.host}:{config.mqtt.port} "
                  f"topic {config.mqtt.topic_prefix}/+/state"
                  f"{', HA discovery' if config.mqtt.discovery else ''}")
        else:
            print("  mqtt    : disabled")
        print(f"  devices : {len(config.devices)}")
        for device in config.devices:
            print(f"      {device.mac}  {device.name}")
        return 0

    log.info("loaded config from %s", path or "defaults")
    for warning in warnings:
        log.warning("config: %s", warning)

    try:
        asyncio.run(Exporter(config, Metrics()).run())
    except KeyboardInterrupt:  # pragma: no cover
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())