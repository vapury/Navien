"""Fan platform for Navien Smart."""

from __future__ import annotations

import math
from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
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
    """Set up Navien Smart fan entities."""
    coordinator: NavienSmartDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        NavienSmartFan(coordinator, device)
        for device in coordinator.devices
        if device.modes
    ]
    async_add_entities(entities)


class NavienSmartFan(
    CoordinatorEntity[NavienSmartDataUpdateCoordinator],
    FanEntity,
):
    """Fan entity for a Navien device."""

    _attr_has_entity_name = True
    _attr_name = None

    def __init__(
        self,
        coordinator: NavienSmartDataUpdateCoordinator,
        device: NavienDevice,
    ) -> None:
        """Initialize the fan entity."""
        super().__init__(coordinator)
        self._device_id = device.id
        self._attr_unique_id = f"{device.id}_fan"
        self._attr_supported_features = (
            FanEntityFeature.SET_SPEED
            | FanEntityFeature.TURN_ON
            | FanEntityFeature.TURN_OFF
            | FanEntityFeature.PRESET_MODE
        )

    @property
    def device(self) -> NavienDevice | None:
        """Return the latest device snapshot."""
        return self.coordinator.device_by_id(self._device_id)

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return device information."""
        if self.device is None:
            return None
        raw = self.device.raw or {}
        model = raw.get("modelDisplayName") or raw.get("modelCode")
        serial_number = str(raw.get("deviceId")) if raw.get("deviceId") else None
        return DeviceInfo(
            identifiers={(DOMAIN, self.device.id)},
            manufacturer="KyungDong Navien",
            name=self.device.name,
            model=str(model) if model else None,
            serial_number=serial_number,
        )

    @property
    def is_on(self) -> bool | None:
        """Return True if entity is on."""
        if self.device is None:
            return None
        return self.device.power

    @property
    def percentage(self) -> int | None:
        """Return the current speed percentage."""
        if self.device is None:
            return None
        if not self.device.power:
            return 0
        
        fan_key = self.device.current_fan_key
        if fan_key == "gentle":
            return 20
        elif fan_key == "low":
            return 40
        elif fan_key == "high":
            return 60
        elif fan_key == "turbo":
            return 80
        elif fan_key == "auto":
            return 100
        
        return 0

    @property
    def percentage_step(self) -> float:
        """Return the step size for speed percentage."""
        return 20.0

    @property
    def preset_modes(self) -> list[str] | None:
        """Return a list of available preset modes."""
        if self.device is None or not self.device.modes:
            return None
        return [mode.name for mode in self.device.modes]

    @property
    def preset_mode(self) -> str | None:
        """Return the current preset mode."""
        if self.device is None or self.device.current_mode_key is None:
            return None
        mode = next((m for m in self.device.modes if m.key == self.device.current_mode_key), None)
        return mode.name if mode else None

    async def async_set_percentage(self, percentage: int) -> None:
        """Set the speed percentage of the fan."""
        if percentage == 0:
            await self.async_turn_off()
            return

        fan_key = "auto"
        if percentage <= 20:
            fan_key = "gentle"
        elif percentage <= 40:
            fan_key = "low"
        elif percentage <= 60:
            fan_key = "high"
        elif percentage <= 80:
            fan_key = "turbo"
        else:
            fan_key = "auto"

        if not self.is_on:
            await self.coordinator.client.async_set_power(self._device_id, True)

        await self.coordinator.client.async_set_fan(self._device_id, fan_key)
        await self.coordinator.async_request_refresh()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set the preset mode of the fan."""
        if self.device is None:
            return
        mode = next((m for m in self.device.modes if m.name == preset_mode), None)
        if mode is None:
            return

        if not self.is_on:
            await self.coordinator.client.async_set_power(self._device_id, True)

        await self.coordinator.client.async_set_mode(self._device_id, mode.key)
        await self.coordinator.async_request_refresh()

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Turn on the fan."""
        if preset_mode is not None:
            await self.async_set_preset_mode(preset_mode)
        
        if percentage is not None:
            await self.async_set_percentage(percentage)
            return
            
        if preset_mode is None and percentage is None:
            await self.coordinator.client.async_set_power(self._device_id, True)
            await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the fan."""
        await self.coordinator.client.async_set_power(self._device_id, False)
        await self.coordinator.async_request_refresh()
