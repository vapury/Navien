"""Diagnostics support for Navien Smart."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .const import DOMAIN


REDACTED_KEYS = {
    "deviceId",
    "deviceSeq",
    "serial_number",
    "sensor_device_id",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    return {
        "entry": {
            **entry.as_dict(),
            "data": {
                **entry.data,
                CONF_PASSWORD: "**REDACTED**",
            },
        },
        "devices": [
            _device_diagnostics(device)
            for device in getattr(coordinator, "devices", [])
        ],
    }


def _device_diagnostics(device: Any) -> dict[str, Any]:
    """Return non-sensitive device details useful for model support."""
    raw = device.raw or {}
    return {
        "id": "**REDACTED**",
        "name": device.name,
        "type": device.type,
        "power": device.power,
        "current_mode_key": device.current_mode_key,
        "current_fan_key": device.current_fan_key,
        "target_humidity": device.target_humidity,
        "model": {
            "serviceCode": raw.get("serviceCode"),
            "modelCode": raw.get("modelCode"),
            "modelName": raw.get("modelName"),
            "modelDisplayName": raw.get("modelDisplayName"),
            "connected": raw.get("connected"),
        },
        "sensor_profile": _redact(raw.get("sensorProfile") or device.sensor_profile or {}),
        "air_sensor_keys": sorted((device.air_sensors or {}).keys()),
        "modes": [
            {
                "key": mode.key,
                "name": mode.name,
                "mode": mode.mode,
                "option": mode.option,
                "air_volume": mode.air_volume,
                "configurable": mode.configurable,
                "humidity_min": mode.humidity_min,
                "humidity_max": mode.humidity_max,
                "fan_options": [
                    {
                        "key": fan.key,
                        "name": fan.name,
                        "option": fan.option,
                        "air_volume": fan.air_volume,
                        "configurable": fan.configurable,
                    }
                    for fan in mode.fan_options
                ],
            }
            for mode in device.modes
        ],
    }


def _redact(value: Any) -> Any:
    """Redact identifiers from diagnostics."""
    if isinstance(value, dict):
        return {
            key: "**REDACTED**" if key in REDACTED_KEYS else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value
