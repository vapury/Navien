"""Climate platform for Navien Smart Mat."""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ClimateEntityFeature,
    HVACMode,
    HVACAction,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
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
    """Set up Navien Smart Mat climate entities."""
    coordinator: NavienSmartDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
    
    entities = []
    # getattr를 통해 mat_devices 접근 (초기화 전일 수도 있으므로 방어적 코드)
    mat_devices = getattr(coordinator, "mat_devices", {})
    
    for device in mat_devices.values():
        if not device.heat_control or not device.heat_control.is_temperature:
            continue
            
        for zone in device.zones:
            entities.append(NavienSmartMatClimate(coordinator, device, zone))
            
    async_add_entities(entities)


class NavienSmartMatClimate(CoordinatorEntity[NavienSmartDataUpdateCoordinator], ClimateEntity):
    """Representation of a Navien Mat climate device (Temperature)."""

    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
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
        
        # 존 이름 맵핑
        zone_names = {"left": "좌측", "right": "우측", "center": "매트"}
        self._attr_name = f"{zone_names.get(zone, zone)} 온도"
        self._attr_unique_id = f"{device.device_id}_{zone}_climate"
        
        if device.heat_control:
            if device.heat_control.unit == "0.5C":
                self._attr_target_temperature_step = 0.5
            else:
                self._attr_target_temperature_step = 1.0

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
    def min_temp(self) -> float:
        """Return the minimum temperature."""
        if self.device and self.device.heat_control and self.device.heat_control.range_min is not None:
            return self.device.heat_control.range_min
        return 20.0

    @property
    def max_temp(self) -> float:
        """Return the maximum temperature."""
        if self.device and self.device.heat_control and self.device.heat_control.range_max is not None:
            return self.device.heat_control.range_max
        return 45.0

    @property
    def current_temperature(self) -> float | None:
        """Return current temperature."""
        if self.device is None:
            return None
        return self.device.zone_current(self._zone)

    @property
    def target_temperature(self) -> float | None:
        """Return target temperature."""
        if self.device is None:
            return None
        return self.device.zone_setting(self._zone)

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return current HVAC mode."""
        if self.device is None or self.device.power is None:
            return None
        return HVACMode.HEAT if self.device.power else HVACMode.OFF

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return current HVAC action."""
        if self.device is None or not self.device.power:
            return HVACAction.OFF
            
        op_mode = self.device.operation_mode
        # 1: 운전중, 5: 빠른난방 -> HEATING
        if op_mode in (1, 5):
            return HVACAction.HEATING
        # 2: 대기, 3: 슬립 -> IDLE
        return HVACAction.IDLE

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None or self.device is None:
            return
            
        # 기기가 꺼져있으면 먼저 켭니다.
        if self.hvac_mode == HVACMode.OFF:
            power_desired = self.device.build_power_desired(True)
            await self.coordinator.client.async_mat_control(self.device, power_desired)
            
            # 낙관적 업데이트 (UI 튕김 방지)
            if "heater" not in self.device.reported:
                self.device.reported["heater"] = {}
            self.device.reported["heater"]["status"] = 1
            self.async_write_ha_state()
            
            # 1초 대기 (다중 명령 충돌 방지)
            await asyncio.sleep(1.0)
            
        desired = self.device.build_heater_desired(self._zone, temperature)
        await self.coordinator.client.async_mat_control(self.device, desired)
        
        # 낙관적 업데이트 (목표 온도 즉시 갱신)
        if "heater" not in self.device.reported:
            self.device.reported["heater"] = {}
        if self._zone not in self.device.reported["heater"]:
            self.device.reported["heater"][self._zone] = {}
        self.device.reported["heater"][self._zone]["setting"] = temperature
        self.async_write_ha_state()
        
        # 실제 서버 반영 확인을 위해 백그라운드 업데이트 예약
        self.coordinator.async_schedule_mqtt_update()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set HVAC mode."""
        if self.device is None:
            return
            
        turn_on = hvac_mode != HVACMode.OFF
        desired = self.device.build_power_desired(turn_on)
        await self.coordinator.client.async_mat_control(self.device, desired)
        
        # 낙관적 업데이트
        if "heater" not in self.device.reported:
            self.device.reported["heater"] = {}
        self.device.reported["heater"]["status"] = 1 if turn_on else 0
        self.async_write_ha_state()
        
        self.coordinator.async_schedule_mqtt_update()
