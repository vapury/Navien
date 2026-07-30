"""Number platform for Navien Smart."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import NavienDevice, NavienMode
from .const import DOMAIN
from .coordinator import NavienSmartDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Navien Smart number entities."""
    coordinator: NavienSmartDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        NavienSmartTargetHumidityNumber(coordinator, device)
        for device in coordinator.devices
        if _dry_mode(device) is not None
    )


class NavienSmartTargetHumidityNumber(
    CoordinatorEntity[NavienSmartDataUpdateCoordinator],
    NumberEntity,
):
    """Target humidity selector for dehumidification mode."""

    _attr_name = "Target Humidity"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_native_step = 5
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self,
        coordinator: NavienSmartDataUpdateCoordinator,
        device: NavienDevice,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device.id
        self._attr_unique_id = f"{device.id}_target_humidity"

    @property
    def device(self) -> NavienDevice | None:
        """Return the latest device snapshot."""
        return self.coordinator.device_by_id(self._device_id)

    @property
    def available(self) -> bool:
        """Return whether humidity control can be used now."""
        device = self.device
        if device is None:
            return False
        raw = device.raw or {}
        return bool(raw.get("connected", True)) and device.current_mode_key in (None, "dry")

    @property
    def native_min_value(self) -> float:
        """Return minimum supported humidity."""
        mode = _dry_mode(self.device)
        return float(mode.humidity_min if mode and mode.humidity_min is not None else 40)

    @property
    def native_max_value(self) -> float:
        """Return maximum supported humidity."""
        mode = _dry_mode(self.device)
        return float(mode.humidity_max if mode and mode.humidity_max is not None else 65)

    @property
    def native_value(self) -> float | None:
        """Return target humidity."""
        device = self.device
        if device is None:
            return None
        if device.target_humidity is not None:
            return _snap_humidity(device.target_humidity)
        return device.target_humidity

    async def async_set_native_value(self, value: float) -> None:
        """Set target humidity."""
        humidity = _snap_humidity(value)
        await self.coordinator.client.async_set_target_humidity(self._device_id, humidity)
        await self.coordinator.async_request_refresh()

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return device registry information."""
        if self.device is None:
            return None
        raw = self.device.raw or {}
        return DeviceInfo(
            identifiers={(DOMAIN, self.device.id)},
            manufacturer="KyungDong Navien",
            name=self.device.name,
            model=str(raw.get("modelDisplayName") or raw.get("modelCode"))
            if raw.get("modelDisplayName") or raw.get("modelCode")
            else None,
            serial_number=str(raw.get("deviceId")) if raw.get("deviceId") else None,
        )


def _dry_mode(device: NavienDevice | None) -> NavienMode | None:
    """Return the dehumidification mode if supported."""
    if device is None:
        return None
    return next((mode for mode in device.modes if mode.key == "dry"), None)


def _snap_humidity(value: float) -> int:
    """Snap humidity to the device's 5 percent increments."""
    return int(round(float(value) / 5) * 5)
