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
    def _valid_fan_options(self) -> list[str]:
        """Return a sorted list of valid fan keys for the current mode."""
        device = self.device
        if device is None:
            return []
        
        state = self.coordinator.client._optimistic_state.get(self._device_id, {})
        current_mode_key = state.get("current_mode_key") or device.current_mode_key
        mode = next((m for m in device.modes if m.key == current_mode_key), None)
        if not mode:
            return []
            
        valid_order = ["gentle", "low", "high", "auto"]
        supported = [f.key for f in mode.fan_options]
        return [k for k in valid_order if k in supported]

    @property
    def percentage_step(self) -> float:
        """Return the dynamic step size based on valid fan options."""
        count = len(self._valid_fan_options)
        if count == 0:
            return 100.0
        return 100.0 / count

    @property
    def percentage(self) -> int | None:
        """Return the current speed percentage."""
        if self.device is None:
            return None
        if not self.device.power:
            return 0
        
        fan_key = self.device.current_fan_key
        valid_fans = self._valid_fan_options
        if fan_key not in valid_fans:
            return 0
            
        index = valid_fans.index(fan_key)
        return int(round((index + 1) * self.percentage_step))

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

        valid_fans = self._valid_fan_options
        if not valid_fans:
            return

        step = self.percentage_step
        index = int(round(percentage / step)) - 1
        index = max(0, min(len(valid_fans) - 1, index))
        fan_key = valid_fans[index]

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
