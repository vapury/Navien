"""Switch platform for Navien Smart."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import NavienDevice
from .const import DOMAIN
from .coordinator import NavienSmartDataUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Navien Smart switch entities."""
    coordinator: NavienSmartDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        NavienSmartPowerSwitch(coordinator, device)
        for device in coordinator.devices
        if device.modes
    )


class NavienSmartPowerSwitch(
    CoordinatorEntity[NavienSmartDataUpdateCoordinator],
    SwitchEntity,
):
    """Power switch for a Navien room controller device."""

    def __init__(
        self,
        coordinator: NavienSmartDataUpdateCoordinator,
        device: NavienDevice,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device.id
        self._attr_unique_id = f"{device.id}_power"
        self._attr_name = "Power"

    @property
    def device(self) -> NavienDevice | None:
        """Return the latest device snapshot."""
        return self.coordinator.device_by_id(self._device_id)

    @property
    def available(self) -> bool:
        """Return whether the entity is available."""
        raw = (self.device.raw if self.device else {}) or {}
        return bool(raw.get("connected", True))

    @property
    def is_on(self) -> bool | None:
        """Return the current power state."""
        return self.device.power if self.device else None

    async def async_turn_on(self, **kwargs: object) -> None:
        """Turn the device on."""
        await self.coordinator.client.async_set_power(self._device_id, True)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: object) -> None:
        """Turn the device off."""
        await self.coordinator.client.async_set_power(self._device_id, False)
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
