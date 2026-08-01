"""Number platform for Navien Smart Mat."""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN
from .coordinator import NavienSmartDataUpdateCoordinator
from .mat_models import MatDevice


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Navien Smart Mat number entities."""
    coordinator: NavienSmartDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    
    entities = []
    mat_devices = getattr(coordinator, "mat_devices", {})
    
    for device in mat_devices.values():
        if not device.heat_control or not device.heat_control.is_step:
            continue
            
        for zone in device.zones:
            entities.append(NavienSmartMatNumber(coordinator, device, zone))
            
    async_add_entities(entities)


class NavienSmartMatNumber(CoordinatorEntity[NavienSmartDataUpdateCoordinator], NumberEntity):
    """Representation of a Navien Mat step device."""

    _attr_mode = NumberMode.SLIDER
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: NavienSmartDataUpdateCoordinator,
        device: MatDevice,
        zone: str,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device.device_id
        self._zone = zone
        
        zone_names = {"left": "좌측", "right": "우측", "center": "매트"}
        self._attr_name = f"{zone_names.get(zone, zone)} 단계"
        self._attr_unique_id = f"{device.device_id}_{zone}_number"
        
        self._attr_native_step = 1.0
        self._attr_native_min_value = 1.0
        self._attr_native_max_value = 8.0
        
        if device.heat_control:
            if device.heat_control.range_min is not None:
                self._attr_native_min_value = device.heat_control.range_min
            if device.heat_control.range_max is not None:
                self._attr_native_max_value = device.heat_control.range_max

    @property
    def device(self) -> MatDevice | None:
        """Return the latest device snapshot."""
        return getattr(self.coordinator, "mat_devices", {}).get(self._device_id)

    @property
    def device_info(self) -> DeviceInfo | None:
        if self.device is None:
            return None
        return DeviceInfo(
            identifiers={(DOMAIN, self.device.device_id)},
            manufacturer="KyungDong Navien",
            name=self.device.nickname,
            model=self.device.model_name or self.device.model_code,
        )

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.device is not None and self.device.available

    @property
    def native_value(self) -> float | None:
        """Return current setting step."""
        if self.device is None:
            return None
        return self.device.zone_setting(self._zone)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return entity specific state attributes."""
        if self.device is None:
            return {}
        
        attrs = {}
        error_code = self.device.error_code
        if error_code is not None:
            attrs["error_code"] = error_code
            if error_code == 5:
                attrs["error_description"] = "E5: 코드 연결 안됨 (좌/우 분리 코드 확인 요망)"
                
        op_mode = self.device.operation_mode
        if op_mode is not None:
            attrs["operation_mode"] = op_mode
            
        return attrs

    async def async_set_native_value(self, value: float) -> None:
        """Set target step."""
        if self.device is None:
            return
            
        # 기기가 꺼져있으면 먼저 켭니다.
        if not self.device.power:
            power_desired = self.device.build_power_desired(True)
            await self.coordinator.client.async_mat_control(self.device, power_desired)
            
            # 낙관적 업데이트
            if "heater" not in self.device.reported:
                self.device.reported["heater"] = {}
            self.device.reported["heater"]["status"] = 1
            self.async_write_ha_state()
            
            await asyncio.sleep(1.0)
            
        desired = self.device.build_heater_desired(self._zone, value)
        await self.coordinator.client.async_mat_control(self.device, desired)
        
        # 낙관적 업데이트
        if "heater" not in self.device.reported:
            self.device.reported["heater"] = {}
        if self._zone not in self.device.reported["heater"]:
            self.device.reported["heater"][self._zone] = {}
        self.device.reported["heater"][self._zone]["setting"] = value
        self.async_write_ha_state()
        
        self.coordinator.async_schedule_mqtt_update()
