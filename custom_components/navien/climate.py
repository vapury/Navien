"""Climate platform for Navien Smart."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import NavienDevice
from .const import DOMAIN
from .coordinator import NavienSmartDataUpdateCoordinator
from .mat_climate import async_setup_entry as async_setup_mat_climate


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Navien Smart climate entities."""
    coordinator: NavienSmartDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        NavienSmartClimate(coordinator, device)
        for device in coordinator.devices
        if device.type == "climate"
    )
    
    # Load mat climate entities
    await async_setup_mat_climate(hass, entry, async_add_entities)


class NavienSmartClimate(CoordinatorEntity[NavienSmartDataUpdateCoordinator], ClimateEntity):
    """Representation of a Navien heating device."""

    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(
        self,
        coordinator: NavienSmartDataUpdateCoordinator,
        device: NavienDevice,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device.id
        self._attr_unique_id = f"{device.id}_climate"
        self._attr_name = device.name

    @property
    def device(self) -> NavienDevice | None:
        """Return the latest device snapshot."""
        return self.coordinator.device_by_id(self._device_id)

    @property
    def current_temperature(self) -> float | None:
        """Return current room temperature."""
        return self.device.current_temperature if self.device else None

    @property
    def target_temperature(self) -> float | None:
        """Return target temperature."""
        return self.device.target_temperature if self.device else None

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return current HVAC mode."""
        if self.device is None or self.device.power is None:
            return None
        return HVACMode.HEAT if self.device.power else HVACMode.OFF

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        await self.coordinator.client.async_set_temperature(
            self._device_id,
            float(temperature),
        )
        await self.coordinator.async_request_refresh()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set HVAC mode."""
        await self.coordinator.client.async_set_power(
            self._device_id,
            hvac_mode != HVACMode.OFF,
        )
        await self.coordinator.async_request_refresh()

