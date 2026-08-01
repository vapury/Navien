"""Sensor platform for Navien Smart."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import NavienDevice
from .const import DOMAIN
from .coordinator import NavienSmartDataUpdateCoordinator


@dataclass(frozen=True, slots=True)
class NavienSmartSensorDescription:
    """Describe one air sensor value."""

    key: str
    name: str
    native_unit: str | None = None
    device_class: SensorDeviceClass | None = None


AIR_SENSOR_DESCRIPTIONS: tuple[NavienSmartSensorDescription, ...] = (
    NavienSmartSensorDescription(
        key="temperature",
        name="Temperature",
        native_unit=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
    ),
    NavienSmartSensorDescription(
        key="humidity",
        name="Humidity",
        native_unit=PERCENTAGE,
        device_class=SensorDeviceClass.HUMIDITY,
    ),
    NavienSmartSensorDescription(key="pm1Dot0", name="PM1.0", native_unit="ug/m3"),
    NavienSmartSensorDescription(key="pm2Dot5", name="PM2.5", native_unit="ug/m3"),
    NavienSmartSensorDescription(key="pm10", name="PM10", native_unit="ug/m3"),
    NavienSmartSensorDescription(key="co2", name="CO2", native_unit="ppm"),
    NavienSmartSensorDescription(key="tvoc", name="TVOC"),
    NavienSmartSensorDescription(key="total", name="Air Quality Score"),
    NavienSmartSensorDescription(key="radon", name="Radon", native_unit="Bq/m3"),
)




async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Navien Smart sensor entities."""
    coordinator: NavienSmartDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[NavienSmartAirSensor] = []
    for device in coordinator.devices:
        air_sensors = device.air_sensors or {}
        entities.extend(
            NavienSmartAirSensor(coordinator, device, description)
            for description in AIR_SENSOR_DESCRIPTIONS
            if description.key in air_sensors
        )
    async_add_entities(entities)


class NavienSmartAirSensor(
    CoordinatorEntity[NavienSmartDataUpdateCoordinator],
    SensorEntity,
):
    """Air quality sensor for a Navien device."""

    def __init__(
        self,
        coordinator: NavienSmartDataUpdateCoordinator,
        device: NavienDevice,
        description: NavienSmartSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device.id
        self._description = description
        self._attr_unique_id = f"{device.id}_{description.key}"
        self._attr_name = description.name
        self._attr_native_unit_of_measurement = description.native_unit
        self._attr_device_class = description.device_class

    @property
    def device(self) -> NavienDevice | None:
        """Return the latest device snapshot."""
        return self.coordinator.device_by_id(self._device_id)

    @property
    def native_value(self) -> float | str | None:
        """Return the air sensor value."""
        if self.device is None or self.device.air_sensors is None:
            return None
        value = (self.device.air_sensors.get(self._description.key) or {}).get("value")
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return str(value)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return Navien metadata for the sensor value."""
        if self.device is None or self.device.air_sensors is None:
            return None
        data = self.device.air_sensors.get(self._description.key) or {}
        attrs = {
            key: data.get(key)
            for key in (
                "level",
                "zone_id",
                "update_time",
                "source",
                "air_monitor_support",
                "air_monitor_paired",
                "air_monitor_connected",
                "air_monitor_model_code",
            )
            if data.get(key) not in (None, "")
        }
        profile = self._sensor_profile
        for key, attr_key in (
            ("sourceName", "sensor_configuration"),
            ("modelCode", "sensor_model_code"),
            ("modelName", "sensor_model_name"),
            ("deviceId", "sensor_device_id"),
        ):
            value = profile.get(key)
            if value not in (None, ""):
                attrs[attr_key] = value
        if profile.get("radonSupported") is True:
            attrs["radon_supported"] = True
        return attrs or None

    @property
    def device_info(self) -> DeviceInfo | None:
        """Group all air values under one Home Assistant device."""
        if self.device is None:
            return None
        raw = self.device.raw or {}
        
        identifiers = {(DOMAIN, self.device.id)}
        name = self.device.name
        model = raw.get("modelDisplayName") or raw.get("modelCode")
        serial_number = str(raw.get("deviceId")) if raw.get("deviceId") else None
        
        return DeviceInfo(
            identifiers=identifiers,
            manufacturer="KyungDong Navien",
            name=name,
            model=str(model) if model else None,
            serial_number=serial_number,
        )

    @property
    def _sensor_profile(self) -> dict[str, Any]:
        """Return the latest classified sensor profile."""
        if self.device is not None and self.device.sensor_profile:
            return self.device.sensor_profile
        if self.device is not None and self.device.raw:
            profile = self.device.raw.get("sensorProfile")
            if isinstance(profile, dict):
                return profile
        return {}
