"""매트 기기 데이터 모델."""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from typing import Any

_LOGGER = logging.getLogger(__name__)


def _dig(data: dict[str, Any] | None, *keys: str) -> Any:
    """Safely traverse a nested dictionary."""
    if data is None:
        return None
    for key in keys:
        if isinstance(data, dict):
            data = data.get(key)
        else:
            return None
    return data


@dataclass(frozen=True, slots=True)
class HeatControl:
    """난방 온도 제어 메타데이터 (0.5C 온도형 또는 STEP 단계형)."""
    unit: str
    range_min: float | None
    range_max: float | None
    configurable: bool

    @property
    def is_temperature(self) -> bool:
        """온도(Climate) 엔티티로 제어해야 하는 모델인지."""
        return self.unit in ("0.5C", "1C", "1.0C", "C")

    @property
    def is_step(self) -> bool:
        """단계(Number/Select) 엔티티로 제어해야 하는 모델인지."""
        return self.unit in ("STEP", "Step", "step")


class MatDevice:
    """숙면매트 기기 상태."""

    def __init__(self, raw: dict[str, Any]) -> None:
        self.raw = raw
        self.device_seq = str(raw.get("deviceSeq", ""))
        self.device_id = str(raw.get("deviceId", ""))
        self.model_code = str(raw.get("modelCode", ""))
        self.model_name = str(raw.get("modelName", ""))
        self.service_code = raw.get("serviceCode")
        
        # 실시간 상태 (MQTT reported)
        self.reported: dict[str, Any] = {}
        
        # 난방 제어 메타데이터 파싱
        self.heat_control = self._parse_heat_control(raw)
        
        # 양쪽 제어 여부 파싱 (isDouble)
        functions = _dig(raw, "Properties", "registry", "attributes", "functions") or {}
        self.is_double = bool(functions.get("isDouble", False))

    def _parse_heat_control(self, raw: dict[str, Any]) -> HeatControl | None:
        control = _dig(raw, "Properties", "registry", "attributes", "functions", "heatControl")
        if not isinstance(control, dict):
            return None
            
        unit = str(control.get("unit") or "").strip()
        if not unit:
            return None
            
        return HeatControl(
            unit=unit,
            range_min=float(control["rangeMin"]) if control.get("rangeMin") is not None else None,
            range_max=float(control["rangeMax"]) if control.get("rangeMax") is not None else None,
            configurable=bool(control.get("configurable", True)),
        )

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> MatDevice | None:
        """API 응답에서 매트 기기 객체를 생성한다."""
        if not raw.get("deviceSeq") or not raw.get("deviceId"):
            return None
        return cls(raw)

    def apply_reported(self, reported: dict[str, Any]) -> None:
        """MQTT로 들어온 상태를 병합한다."""
        if not reported:
            return
        
        # 깊은 복사 후 병합 (부분 업데이트 지원)
        merged = copy.deepcopy(self.reported)
        for key, value in reported.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key].update(value)
            else:
                merged[key] = copy.deepcopy(value)
        self.reported = merged

    @property
    def nickname(self) -> str:
        """기기 별칭."""
        name = self.raw.get("name")
        if isinstance(name, str) and name:
            return name
        return self.model_name or self.device_id

    @property
    def available(self) -> bool:
        """기기 접속 가능 상태."""
        return bool(self.raw.get("connected", True))

    @property
    def zones(self) -> list[str]:
        """조작 가능한 구역 목록."""
        if self.is_double:
            return ["left", "right"]
        return ["center"]

    @property
    def power(self) -> bool | None:
        """전원 상태."""
        status = _dig(self.reported, "heater", "status")
        if status is None:
            return None
        return status == 1

    @property
    def operation_mode(self) -> int | None:
        """운전 상태 코드 (1:운전중, 2:대기, 3:슬립, 4:살균 등)."""
        return _dig(self.reported, "heater", "operationMode")

    @property
    def error_code(self) -> int | None:
        """에러 코드 (0이면 정상)."""
        return _dig(self.reported, "heater", "errorCode")

    @property
    def child_lock(self) -> bool | None:
        """차일드락 설정 여부."""
        lock = _dig(self.reported, "heater", "childLock")
        if lock is None:
            return None
        return bool(lock)
        
    @property
    def is_heating_too_high(self) -> bool | None:
        """고온 경고(화상 위험) 선을 넘었는지 여부."""
        return bool(_dig(self.reported, "heater", "isHeatingTooHigh"))

    def zone_setting(self, zone: str) -> float | None:
        """구역의 설정 온도/단계."""
        val = _dig(self.reported, "heater", zone, "setting")
        if val is None:
            return None
        return float(val)

    def zone_current(self, zone: str) -> float | None:
        """구역의 현재 온도/단계."""
        val = _dig(self.reported, "heater", zone, "current")
        if val is None:
            return None
        return float(val)

    def build_power_desired(self, turn_on: bool) -> dict[str, Any]:
        """전원 제어 명령 생성."""
        return {"heater": {"status": 1 if turn_on else 0}}

    def build_lock_desired(self, lock: bool) -> dict[str, Any]:
        """잠금 제어 명령 생성."""
        return {"heater": {"childLock": 1 if lock else 0}}

    def build_heater_desired(self, zone: str, value: float) -> dict[str, Any]:
        """온도/단계 제어 명령 생성."""
        # 0.5C 단위형이라도 API에 보낼 땐 2배수(정수)가 아님 (문서 상 float 그대로 전송)
        # 하지만 기존 레포 등에서 0.5단위를 어떻게 보냈는지 확인하면, float 그대로 보낸다.
        if self.heat_control and self.heat_control.is_temperature:
            value = float(value)
        else:
            value = int(value)
            
        return {
            "heater": {
                zone: {"setting": value}
            }
        }
