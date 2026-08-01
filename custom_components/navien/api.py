"""API client for Navien Smart."""

from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
from dataclasses import dataclass
import hashlib
import hmac
import http.client
import json
import logging
import os
import re
import ssl
import time
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from aiohttp import ClientResponse, ClientSession

from .const import API_BASE_URL, MEMBER_BASE_URL
from .mat_models import MatDevice

LOGGER = logging.getLogger(__name__)


class NavienSmartApiError(Exception):
    """Base exception for Navien Smart API errors."""


class NavienSmartAuthError(NavienSmartApiError):
    """Raised when authentication fails."""


class NavienSmartUnsupportedError(NavienSmartApiError):
    """Raised for endpoints that have not been mapped yet."""


@dataclass(frozen=True, slots=True)
class NavienFanOption:
    """A fan option supported by one operation mode."""

    key: str
    name: str
    option: int
    air_volume: int
    configurable: bool


@dataclass(frozen=True, slots=True)
class NavienMode:
    """An operation mode supported by a Navien ventilation device."""

    key: str
    name: str
    mode: int
    option: int
    air_volume: int
    configurable: bool
    fan_options: tuple[NavienFanOption, ...] = ()
    humidity_min: int | None = None
    humidity_max: int | None = None


@dataclass(slots=True)
class NavienDevice:
    """A normalized Navien device returned by the API client."""

    id: str
    name: str
    type: str
    power: bool | None = None
    current_temperature: float | None = None
    target_temperature: float | None = None
    target_humidity: int | None = None
    current_mode_key: str | None = None
    current_fan_key: str | None = None
    modes: tuple[NavienMode, ...] = ()
    air_sensors: dict[str, dict[str, Any]] | None = None
    sensor_profile: dict[str, Any] | None = None
    raw: dict[str, Any] | None = None


LOGIN_MESSAGE_RE = re.compile(r"var\s+message\s*=\s*(\{.*?\})\s*;", re.DOTALL)

MODE_LABELS = {
    "auto": "자동",
    "vent_dry": "환기제습",
    "vent": "환기",
    "dry": "제습",
    "clean": "청정",
    "cook": "요리",
    "sleep": "숙면",
    "bypass": "바이패스",
}
MODE_BY_CODE_OPTION = {
    (12, 1): ("auto", MODE_LABELS["auto"]),
    (10, 1): ("vent_dry", MODE_LABELS["vent_dry"]),
    (4, 1): ("vent", MODE_LABELS["vent"]),
    (9, 1): ("dry", MODE_LABELS["dry"]),
    (8, 1): ("clean", MODE_LABELS["clean"]),
    (6, 1): ("cook", MODE_LABELS["cook"]),
    (4, 4): ("sleep", MODE_LABELS["sleep"]),
    (17, 1): ("bypass", MODE_LABELS["bypass"]),
}
FAN_LABELS = {
    "gentle": "미풍",
    "low": "약풍",
    "high": "강풍",
    "auto": "자동",
    "turbo": "터보",
    "saving": "절전",
    "basal": "기저",
}
FAN_BY_AIR_VOLUME = {
    1: ("gentle", FAN_LABELS["gentle"]),
    2: ("low", FAN_LABELS["low"]),
    3: ("high", FAN_LABELS["high"]),
    4: ("auto", FAN_LABELS["auto"]),
}

AWS_IOT_ENDPOINTS = (
    "nskr-iot.naviensmartcontrol.com",
    "a1o5esupplsltq-ats.iot.ap-northeast-2.amazonaws.com",
)
AWS_IOT_REGION = "ap-northeast-2"
AWS_IOT_SERVICE = "iotdata"
AWS_IOT_SDK_USER_META = "?SDK=Android&Version=2.77.1"
MQTT_STATUS_TIMEOUT = 5
TARGET_HUMIDITY_STEP = 5
OPTIMISTIC_STATE_TTL = 120
SUPPORTED_SERVICE_CODE = "300"
SUPPORTED_MODEL_CODES = {"1901", "1102"}
SUPPORTED_MODEL_NAMES = {"1901": "NRT-530Z3", "1102": "NRT-530S"}
NRT530_AIR_MONITOR_MODEL_NAMES = {
    "35": "NAA-21DM",
}
AIR_SENSOR_KEY_ALIASES = {
    "radonvalue": "radon",
    "radon_value": "radon",
    "radonbq": "radon",
    "radonbqm3": "radon",
}
RADON_SENSOR_KEYS = {"radon"}


class NavienSmartApiClient:
    """Small async client around the Navien Smart mobile API."""

    def __init__(
        self,
        *,
        session: ClientSession,
        username: str,
        password: str,
        stored_target_humidities: dict[str, Any] | None = None,
        async_save_target_humidities: Callable[[dict[str, int]], Awaitable[None]] | None = None,
    ) -> None:
        self._session = session
        self._username = username
        self._password = password
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._account_seq: int | None = None
        self._user_seq: int | None = None
        self._home_seq: int | None = None
        self._auth_info: dict[str, Any] = {}
        self._mqtt_client_id: str | None = None
        self._mqtt_client: Any = None
        self._mqtt_connected = False
        self._mqtt_lock = asyncio.Lock()
        self._mqtt_loop: asyncio.AbstractEventLoop | None = None
        self._mqtt_waiters: dict[str, asyncio.Event] = {}
        self._mqtt_topic_device_ids: dict[str, str] = {}
        self._latest_status_by_device_id: dict[str, dict[str, Any]] = {}
        self._latest_air_sensors_by_device_id: dict[str, dict[str, dict[str, Any]]] = {}
        self._devices: dict[str, NavienDevice] = {}
        self.mat_devices: dict[str, MatDevice] = {}
        self._raw_devices: list[dict[str, Any]] = []
        self._logged_unsupported_devices: set[tuple[str, str, str]] = set()
        self._optimistic_state: dict[str, dict[str, Any]] = {}
        self._stored_target_humidities: dict[str, int] = {
            str(key): self._snap_target_humidity(value, None)
            for key, value in (stored_target_humidities or {}).items()
            if self._int_value(value) is not None
        }
        self._async_save_target_humidities = async_save_target_humidities
        self._status_update_callback: Any = None
        self._headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "User-Agent": (
                "Mozilla/5.0 (Linux; Android 13; NavienSmart) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
                "Chrome/120.0 Mobile Safari/537.36"
            ),
            "X-Requested-With": "kr.co.kdnavien.naviensmart",
        }

    async def async_login(self) -> None:
        """Authenticate with Navien Smart."""
        self._access_token = None
        self._refresh_token = None
        self._account_seq = None
        self._user_seq = None
        self._home_seq = None
        self._auth_info = {}
        await self.async_close()
        await self._login_member_web()
        await self._secured_sign_in()

    async def _login_member_web(self) -> None:
        """Run the WebView login flow and extract the token bridge payload."""
        async with self._session.get(
            f"{MEMBER_BASE_URL}/member/login",
            headers=self._headers,
        ) as response:
            await response.text()

        try:
            async with self._session.post(f"{MEMBER_BASE_URL}/pwchgLate", headers=self._headers) as response:
                await response.text()
        except Exception:
            pass

        async with self._session.post(
            f"{MEMBER_BASE_URL}/member/login",
            data={"username": self._username, "password": self._password},
            headers={
                **self._headers,
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": MEMBER_BASE_URL,
                "Referer": f"{MEMBER_BASE_URL}/member/login",
            },
            allow_redirects=True,
        ) as response:
            text = await response.text()
            if response.status in (401, 403):
                raise NavienSmartAuthError("Invalid Navien Smart credentials")
            await self._raise_for_api_status(response, text)

        match = LOGIN_MESSAGE_RE.search(text)
        if match is None:
            if "login" in str(response.url).lower():
                raise NavienSmartAuthError("Invalid Navien Smart credentials")
            raise NavienSmartApiError("Could not find login token in Navien response")

        try:
            message = json.loads(match.group(1))
        except json.JSONDecodeError as err:
            raise NavienSmartApiError("Could not parse Navien login token") from err

        access_token = message.get("accessToken")
        refresh_token = message.get("refreshToken")
        account_seq = message.get("userSeq")
        if not access_token or account_seq is None:
            raise NavienSmartAuthError("Navien Smart login did not return a token")

        self._access_token = str(access_token)
        self._refresh_token = str(refresh_token) if refresh_token else None
        self._account_seq = int(account_seq)

    async def _secured_sign_in(self) -> None:
        """Exchange the member token for Navien app account/home metadata."""
        if self._access_token is None or self._account_seq is None:
            raise NavienSmartAuthError("Navien Smart login has not completed")

        data = await self._request_json(
            "POST",
            "/api/v2.0/users/secured-sign-in",
            json_body={"accountSeq": self._account_seq, "userId": self._username},
            allow_reauth=False,
        )
        body = data.get("data") or {}
        user_info = body.get("userInfo") or {}
        homes = body.get("home") or []

        user_seq = user_info.get("userSeq")
        if user_seq is None or not homes:
            raise NavienSmartApiError("Navien Smart account has no usable home")

        current_home_seq = body.get("currentHomeSeq")
        home = next(
            (item for item in homes if item.get("homeSeq") == current_home_seq),
            homes[0],
        )
        self._user_seq = int(user_seq)
        self._home_seq = int(home["homeSeq"])
        self._auth_info = body.get("authInfo") or {}

    def export_auth_state(self) -> dict[str, Any]:
        """Return the current auth state for same-process setup reuse."""
        return {
            "access_token": self._access_token,
            "refresh_token": self._refresh_token,
            "account_seq": self._account_seq,
            "user_seq": self._user_seq,
            "home_seq": self._home_seq,
            "auth_info": self._auth_info,
        }

    def import_auth_state(self, state: dict[str, Any] | None) -> None:
        """Load auth state captured during config flow."""
        if not isinstance(state, dict):
            return
        self._access_token = str(state["access_token"]) if state.get("access_token") else None
        self._refresh_token = str(state["refresh_token"]) if state.get("refresh_token") else None
        self._account_seq = self._int_value(state.get("account_seq"))
        self._user_seq = self._int_value(state.get("user_seq"))
        self._home_seq = self._int_value(state.get("home_seq"))
        auth_info = state.get("auth_info")
        self._auth_info = auth_info if isinstance(auth_info, dict) else {}

    async def async_get_devices(self) -> list[NavienDevice]:
        """Return all devices attached to the account."""
        if self._access_token is None or self._user_seq is None or self._home_seq is None:
            await self.async_login()

        data = await self._request_json(
            "GET",
            "/api/v2.0/devices",
            params={"homeSeq": self._home_seq, "userSeq": self._user_seq},
        )
        devices = data.get("data", {}).get("devices", [])
        supported_devices = self._supported_devices(devices)
        self._raw_devices = supported_devices
        await self._async_refresh_mqtt_status(supported_devices)
        
        normalized = []
        mat_normalized = []
        for device in supported_devices:
            if str(device.get("serviceCode") or "") == "200":
                mat = MatDevice.parse(device)
                if mat:
                    mat_normalized.append(mat)
            else:
                normalized.append(await self._normalize_device(device))
                
        self._devices = {device.id: device for device in normalized}
        self.mat_devices = {mat.device_id: mat for mat in mat_normalized}
        return normalized

    def set_status_update_callback(self, callback: Any) -> None:
        """Set a callback fired when MQTT status changes."""
        self._status_update_callback = callback

    async def async_get_cached_devices(self) -> list[NavienDevice]:
        """Return devices using the latest cached raw devices and MQTT state."""
        if self._raw_devices and not self._mqtt_connected:
            try:
                LOGGER.info("Navien Smart MQTT disconnected, attempting to self-heal (re-login & reconnect)")
                await self.async_login()
                await self._async_ensure_mqtt_connected()
            except Exception as err:
                LOGGER.warning("Navien Smart MQTT self-healing failed: %s", err)

        normalized = []
        mat_normalized = []
        for device in self._raw_devices:
            if str(device.get("serviceCode") or "") == "200":
                mat = MatDevice.parse(device)
                if mat:
                    mat_normalized.append(mat)
            else:
                normalized.append(await self._normalize_device(device))
                
        self._devices = {device.id: device for device in normalized}
        self.mat_devices = {mat.device_id: mat for mat in mat_normalized}
        
        # 최신 상태(reported)를 매트 모델에 적용
        for device_id, status in self._latest_status_by_device_id.items():
            if device_id in self.mat_devices:
                self.mat_devices[device_id].apply_reported(status)
                
        return normalized

    def _supported_devices(self, raw_devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return devices this integration intentionally supports."""
        supported: list[dict[str, Any]] = []
        for raw_device in raw_devices:
            service_code = str(raw_device.get("serviceCode") or "")
            model_code = str(raw_device.get("modelCode") or "")
            if service_code == "200":
                supported.append(raw_device)
                continue
            if service_code == SUPPORTED_SERVICE_CODE and model_code in SUPPORTED_MODEL_CODES:
                supported.append(raw_device)
                continue
            self._log_unsupported_device(raw_device)
        return supported

    def _log_unsupported_device(self, raw_device: dict[str, Any]) -> None:
        """Log unsupported devices once so new model support can be added later."""
        service_code = str(raw_device.get("serviceCode") or "")
        model_code = str(raw_device.get("modelCode") or "")
        model_name = str(raw_device.get("modelName") or "")
        device_id = str(raw_device.get("deviceId") or raw_device.get("deviceSeq") or "")
        log_key = (service_code, model_code, model_name)
        if log_key in self._logged_unsupported_devices:
            return
        self._logged_unsupported_devices.add(log_key)
        LOGGER.warning(
            "Navien Smart unverified model skipped; serviceCode=%s modelCode=%s modelName=%s deviceId=%s",
            service_code or "<unknown>",
            model_code or "<unknown>",
            model_name or "<unknown>",
            device_id or "<unknown>",
        )

    async def async_close(self) -> None:
        """Close any background MQTT connection."""
        mqtt_client = self._mqtt_client
        self._mqtt_client = None
        self._mqtt_connected = False
        self._mqtt_waiters.clear()
        if mqtt_client is not None:
            await asyncio.to_thread(self._disconnect_mqtt_client, mqtt_client)

    async def async_set_temperature(self, device_id: str, temperature: float) -> None:
        """Set target temperature for a heating device."""
        raise NavienSmartUnsupportedError(
            "Temperature control has not been mapped for this Navien device yet."
        )

    async def async_set_power(self, device_id: str, power: bool) -> None:
        """Set device power state."""
        device = self._device_for_control(device_id)
        raw = device.raw or {}
        room_controller = (raw.get("data") or {}).get("roomController") or {}
        desired = {
            "roomController": {
                "deviceId": room_controller.get("deviceId") or raw.get("deviceId"),
                "zoneId": room_controller.get("zoneId") or 1,
                "running": 1 if power else 2,
            }
        }
        await self._async_send_control(device, "power", desired)
        state = self._optimistic_state.setdefault(device_id, {})
        state["power"] = power
        state["updated_at"] = time.time()

    async def async_mat_control(self, device: MatDevice, desired: dict[str, Any]) -> None:
        """Send a shadow update command to a Mat device."""
        if self._user_seq is None or self._home_seq is None:
            await self.async_login()
            
        await self._request_json(
            "POST",
            f"/api/v2.0/devices/{device.device_seq}/control",
            params={"homeSeq": self._home_seq, "userSeq": self._user_seq},
            json_body={
                "serviceCode": device.service_code,
                "payload": {
                    "clientId": self._client_id(),
                    "sessionId": str(int(time.time() * 1000)),
                    "requestTopic": f"$aws/things/{device.device_id}/shadow/name/status/update",
                    "responseTopic": f"$aws/things/{device.device_id}/shadow/name/status/update/accepted",
                    "state": {"desired": desired},
                },
            },
        )

    async def async_set_mode(
        self,
        device_id: str,
        mode_key: str,
        *,
        fan_key: str | None = None,
        target_humidity: int | None = None,
    ) -> None:
        """Set operation mode, optional fan speed, and optional target humidity."""
        device = self._device_for_control(device_id)
        mode = self._mode_by_key(device, mode_key)
        if target_humidity is not None and mode.key == "dry":
            target_humidity = self._snap_target_humidity(target_humidity, mode)
        fan = self._fan_for_mode(mode, fan_key or self._optimistic_state.get(device_id, {}).get("current_fan_key"))
        desired = self._build_mode_desired(mode, fan, target_humidity)
        await self._async_send_control(device, "change-mode", desired)
        state = self._optimistic_state.setdefault(device_id, {})
        state["current_mode_key"] = mode.key
        state["current_fan_key"] = fan.key
        state["updated_at"] = time.time()
        if target_humidity is not None:
            state["target_humidity"] = int(target_humidity)
            state["target_humidity_updated_at"] = time.time()
            await self._async_remember_target_humidity(device_id, target_humidity)

    async def async_set_fan(self, device_id: str, fan_key: str) -> None:
        """Set fan option for the currently selected operation mode."""
        device = self._device_for_control(device_id)
        state = self._optimistic_state.setdefault(device_id, {})
        mode_key = state.get("current_mode_key") or device.current_mode_key or (device.modes[0].key if device.modes else None)
        if mode_key is None:
            raise NavienSmartUnsupportedError("This Navien device has no mapped operation modes")
        await self.async_set_mode(
            device_id,
            str(mode_key),
            fan_key=fan_key,
            target_humidity=state.get("target_humidity"),
        )

    async def async_set_target_humidity(self, device_id: str, humidity: int) -> None:
        """Set the dehumidification target humidity."""
        device = self._device_for_control(device_id)
        dry_mode = self._mode_by_key(device, "dry")
        humidity = self._snap_target_humidity(humidity, dry_mode)
        if dry_mode.humidity_min is not None and humidity < dry_mode.humidity_min:
            raise NavienSmartApiError("Target humidity is below the supported range")
        if dry_mode.humidity_max is not None and humidity > dry_mode.humidity_max:
            raise NavienSmartApiError("Target humidity is above the supported range")
        state = self._optimistic_state.setdefault(device_id, {})
        await self.async_set_mode(
            device_id,
            "dry",
            fan_key=state.get("current_fan_key"),
            target_humidity=humidity,
        )

    async def _normalize_device(self, raw_device: dict[str, Any]) -> NavienDevice:
        """Convert a Navien device payload into a stable integration shape."""
        properties = raw_device.get("Properties") or {}
        device_seq = str(raw_device.get("deviceSeq") or raw_device.get("deviceId"))
        nickname = self._extract_device_name(properties.get("nickName"))
        name = (
            nickname
            or raw_device.get("modelName")
            or raw_device.get("deviceId")
            or device_seq
        )
        service_code = raw_device.get("serviceCode")
        model_code = str(raw_device.get("modelCode") or "")
        model_name = raw_device.get("modelName") or SUPPORTED_MODEL_NAMES.get(model_code)
        model_display_name = (
            f"{model_name} ({model_code})"
            if model_name and model_code
            else str(model_name or model_code or "")
        )
        device_type = "air_sensor" if str(service_code) == "300" else "unknown"
        air_sensors = await self._get_air_sensors(device_seq) if device_type == "air_sensor" else {}
        mqtt_air_sensors = self._latest_air_sensors_by_device_id.get(str(raw_device.get("deviceId"))) or {}
        if mqtt_air_sensors:
            air_sensors = {**air_sensors, **mqtt_air_sensors}
        sensor_profile = self._nrt530_sensor_profile(raw_device, air_sensors)
        modes = self._extract_modes(raw_device)
        current_state = self._extract_current_state(raw_device, modes)
        optimistic = self._optimistic_state.get(device_seq, {})
        
        optimistic_updated_at = optimistic.get("updated_at", 0)
        use_optimistic = bool(time.time() - optimistic_updated_at < OPTIMISTIC_STATE_TTL)
        
        power = optimistic.get("power") if use_optimistic and "power" in optimistic else current_state.get("power", optimistic.get("power"))
        current_mode_key = optimistic.get("current_mode_key") if use_optimistic and "current_mode_key" in optimistic else current_state.get("current_mode_key", optimistic.get("current_mode_key"))
        current_fan_key = optimistic.get("current_fan_key") if use_optimistic and "current_fan_key" in optimistic else current_state.get("current_fan_key", optimistic.get("current_fan_key"))

        target_humidity = current_state.get("target_humidity", optimistic.get("target_humidity"))
        optimistic_target = optimistic.get("target_humidity")
        optimistic_target_updated_at = optimistic.get("target_humidity_updated_at")
        if (
            optimistic_target is not None
            and isinstance(optimistic_target_updated_at, (int, float))
            and time.time() - optimistic_target_updated_at < OPTIMISTIC_STATE_TTL
        ):
            target_humidity = optimistic_target
        if target_humidity is None:
            target_humidity = self._stored_target_humidities.get(device_seq)
        elif current_state.get("target_humidity") is not None:
            await self._async_remember_target_humidity(device_seq, target_humidity)

        current_temperature = self._float_air_value(air_sensors, "temperature")
        return NavienDevice(
            id=device_seq,
            name=str(name),
            type=device_type,
            power=power,
            current_temperature=current_temperature,
            target_humidity=target_humidity,
            current_mode_key=current_mode_key,
            current_fan_key=current_fan_key,
            modes=modes,
            air_sensors=air_sensors,
            sensor_profile=sensor_profile,
            raw={
                "deviceSeq": raw_device.get("deviceSeq"),
                "serviceCode": service_code,
                "deviceId": raw_device.get("deviceId"),
                "modelCode": model_code,
                "modelName": model_name,
                "modelDisplayName": model_display_name,
                "sensorProfile": sensor_profile,
                "connected": raw_device.get("connected"),
                "data": (raw_device.get("Properties") or {}).get("data") or {},
            },
        )

    def _nrt530_sensor_profile(
        self,
        raw_device: dict[str, Any],
        air_sensors: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        """Classify the NRT530 sensor arrangement from device capability payloads."""
        properties = raw_device.get("Properties") or {}
        data = properties.get("data") or {}
        reported = ((data.get("did") or {}).get("reported") or {})
        room_controller = reported.get("roomController") or data.get("roomController") or {}
        sub_room_controllers = room_controller.get("subRoomController") or []
        if not isinstance(sub_room_controllers, list):
            sub_room_controllers = []

        room_sensors = self._list_value(room_controller.get("sensor"))
        sub_room_sensor_count = sum(
            len(self._list_value(controller.get("sensor")))
            for controller in sub_room_controllers
            if isinstance(controller, dict)
        )
        air_monitors = self._list_value(reported.get("airMonitor"))
        air_monitor = air_monitors[0] if air_monitors and isinstance(air_monitors[0], dict) else {}

        external_monitor = bool(air_monitor)
        integrated_sensor = bool(room_sensors or sub_room_sensor_count)
        if external_monitor:
            source = "external_air_monitor"
            source_name = "별도 에어모니터 연동형"
        elif integrated_sensor:
            source = "integrated_room_controller"
            source_name = "룸콘 센서 일체형"
        elif air_sensors:
            source = "air_sensor_values_only"
            source_name = "센서값 확인됨"
        else:
            source = "no_sensor_detected"
            source_name = "센서 미감지"

        monitor_model_code = self._string_value(air_monitor.get("modelCode"))
        monitor_model_name = (
            NRT530_AIR_MONITOR_MODEL_NAMES.get(monitor_model_code or "")
            if monitor_model_code is not None
            else None
        )
        sensor_keys = sorted(str(key) for key in air_sensors)
        radon_supported = self._has_radon_capability(reported, air_sensors)
        profile = {
            "source": source,
            "sourceName": source_name,
            "deviceId": air_monitor.get("deviceId") or raw_device.get("deviceId"),
            "modelCode": monitor_model_code,
            "modelName": monitor_model_name,
            "zoneId": air_monitor.get("zoneId") or room_controller.get("zoneId"),
            "airMonitorCount": len(air_monitors),
            "roomControllerSensorCount": len(room_sensors),
            "subRoomControllerSensorCount": sub_room_sensor_count,
            "sensorKeys": sensor_keys,
            "radonSupported": radon_supported,
        }
        return profile

    @classmethod
    def _has_radon_capability(
        cls,
        reported: dict[str, Any],
        air_sensors: dict[str, dict[str, Any]],
    ) -> bool:
        """Return whether the payload shows a radon-capable sensor."""
        if RADON_SENSOR_KEYS.intersection(air_sensors):
            return True
        for item in cls._walk_dicts(reported):
            for key, value in item.items():
                normalized_key = cls._normalize_air_sensor_key(key)
                if normalized_key in RADON_SENSOR_KEYS and value not in (None, "", []):
                    return True
                if str(key).lower() in {"radonstageuse", "radonstagevalue"}:
                    return True
        return False

    @classmethod
    def _walk_dicts(cls, value: Any) -> list[dict[str, Any]]:
        """Return dictionaries nested in a JSON-like value."""
        found: list[dict[str, Any]] = []
        if isinstance(value, dict):
            found.append(value)
            for child in value.values():
                found.extend(cls._walk_dicts(child))
        elif isinstance(value, list):
            for child in value:
                found.extend(cls._walk_dicts(child))
        return found

    @staticmethod
    def _list_value(value: Any) -> list[Any]:
        """Return a list for list-like payload fields."""
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            return [value]
        return []

    @staticmethod
    def _string_value(value: Any) -> str | None:
        """Return a non-empty string value."""
        if value in (None, ""):
            return None
        return str(value)

    def _extract_current_state(
        self,
        raw_device: dict[str, Any],
        modes: tuple[NavienMode, ...],
    ) -> dict[str, Any]:
        """Extract current state when the API payload contains status fields."""
        properties = raw_device.get("Properties") or {}
        data = properties.get("data") or {}
        room_controller = data.get("roomController") or {}
        physical_device_id = raw_device.get("deviceId")
        status = (
            self._latest_status_by_device_id.get(str(physical_device_id))
            if physical_device_id is not None
            else None
        )
        if status is None:
            status = self._find_status_room_controller(raw_device)
        state: dict[str, Any] = {}

        running = status.get("running")
        if running is None:
            running = room_controller.get("running")
        if running is None:
            running = room_controller.get("state")
        if running is not None:
            try:
                state["power"] = int(running) == 1
            except (TypeError, ValueError):
                pass

        mode_code = self._int_value(status.get("mode"))
        mode_option = self._int_value(status.get("option"), default=1)
        air_volume = status.get("airVolume")
        if mode_code is not None and mode_option is not None:
            mode = self._mode_from_current_values(modes, mode_code, mode_option, air_volume)
            if mode is not None:
                state["current_mode_key"] = mode.key
                fan = self._fan_from_current_values(mode, mode_option, air_volume)
                if fan is not None:
                    state["current_fan_key"] = fan.key

        humidity = self._target_humidity_from_status(
            status,
            allow_type1=state.get("current_mode_key") == "dry",
        )
        if humidity is not None:
            dry_mode = next((mode for mode in modes if mode.key == "dry"), None)
            state["target_humidity"] = self._snap_target_humidity(humidity, dry_mode)
        elif state.get("current_mode_key") == "dry":
            self._log_missing_target_humidity(raw_device, status)
        return state

    async def _async_remember_target_humidity(self, device_id: str, humidity: Any) -> None:
        """Persist the last valid target humidity for restart restore."""
        value = self._snap_target_humidity(humidity, None)
        if self._stored_target_humidities.get(str(device_id)) == value:
            return
        self._stored_target_humidities[str(device_id)] = value
        if self._async_save_target_humidities is not None:
            await self._async_save_target_humidities(dict(self._stored_target_humidities))

    def _log_missing_target_humidity(
        self,
        raw_device: dict[str, Any],
        status: dict[str, Any],
    ) -> None:
        """Log sanitized status shape when dry target humidity cannot be mapped."""
        physical_device_id = str(raw_device.get("deviceId") or "")
        marker = f"missing_target_humidity_logged:{physical_device_id}"
        optimistic = self._optimistic_state.setdefault(str(raw_device.get("deviceSeq") or ""), {})
        if optimistic.get(marker):
            return
        optimistic[marker] = True
        LOGGER.debug(
            "Navien Smart dry mode target humidity not found; deviceId=%s status_keys=%s additional_data_count=%s additional_data_paths=%s additional_data_summary=%s",
            physical_device_id,
            self._safe_keys(status),
            len(self._additional_data_items(status)),
            self._additional_data_paths(status),
            self._additional_data_summary(status),
        )

    @classmethod
    def _additional_data_summary(cls, value: Any) -> list[dict[str, Any]]:
        """Return non-sensitive additionalData fields for debugging."""
        summary: list[dict[str, Any]] = []
        for item in cls._additional_data_items(value)[:12]:
            if not isinstance(item, dict):
                continue
            summary.append(
                {
                    key: item.get(key)
                    for key in ("type", "value", "min", "max")
                    if key in item
                }
            )
        return summary

    @staticmethod
    def _find_status_room_controller(raw_device: dict[str, Any]) -> dict[str, Any]:
        """Find a roomController object that looks like live status."""
        properties = raw_device.get("Properties") or {}
        candidates = [
            raw_device.get("roomController"),
            (raw_device.get("eachRoomSd") or {}).get("roomController"),
            ((raw_device.get("state") or {}).get("reported") or {}).get("roomController"),
            ((properties.get("data") or {}).get("status") or {}).get("roomController"),
            (
                ((properties.get("data") or {}).get("did") or {})
                .get("reported", {})
                .get("roomController")
            ),
        ]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            mode_value = candidate.get("mode")
            if mode_value is None or self._int_value(mode_value) is not None:
                return candidate
        return {}

    @staticmethod
    def _mode_from_current_values(
        modes: tuple[NavienMode, ...],
        mode_code: int,
        mode_option: int,
        air_volume: Any,
    ) -> NavienMode | None:
        """Map current numeric mode values to a supported mode."""
        for mode in modes:
            if mode.mode == mode_code and mode.option == mode_option:
                return mode
        for mode in modes:
            if mode.mode == mode_code:
                try:
                    volume = int(air_volume)
                except (TypeError, ValueError):
                    volume = None
                if any(
                    fan.option == mode_option
                    and (
                        fan.key in {"turbo", "saving", "basal"}
                        or volume is None
                        or fan.air_volume == volume
                    )
                    for fan in mode.fan_options
                ):
                    return mode
        return next((mode for mode in modes if mode.mode == mode_code), None)

    @staticmethod
    def _fan_from_current_values(
        mode: NavienMode,
        mode_option: int,
        air_volume: Any,
    ) -> NavienFanOption | None:
        """Map current numeric fan values to a supported fan option."""
        try:
            volume = int(air_volume)
        except (TypeError, ValueError):
            volume = None
        for fan in mode.fan_options:
            if fan.key in {"turbo", "saving", "basal"} and fan.option == mode_option:
                return fan
            if fan.option == mode_option and (volume is None or fan.air_volume == volume):
                return fan
        return mode.fan_options[0] if mode.fan_options else None

    @staticmethod
    def _snap_target_humidity(humidity: Any, mode: NavienMode | None) -> int:
        """Snap target humidity to the device's supported 5 percent increments."""
        value = NavienSmartApiClient._int_value(humidity)
        if value is None:
            value = 40
        minimum = mode.humidity_min if mode and mode.humidity_min is not None else 40
        maximum = mode.humidity_max if mode and mode.humidity_max is not None else 65
        value = max(minimum, min(maximum, value))
        return int(round(value / TARGET_HUMIDITY_STEP) * TARGET_HUMIDITY_STEP)

    @staticmethod
    def _int_value(value: Any, *, default: int | None = None) -> int | None:
        """Return an int for numeric values encoded as int, float, or string."""
        if value is None:
            return default
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _target_humidity_from_status(
        status: dict[str, Any],
        *,
        allow_type1: bool = True,
    ) -> int | None:
        """Extract target humidity from current status additional data."""
        candidates: list[tuple[int, int]] = []
        for item in NavienSmartApiClient._additional_data_items(status):
            if not isinstance(item, dict):
                continue
            try:
                item_type = int(item.get("type"))
            except (TypeError, ValueError):
                continue
            if item_type not in (1, 3):
                continue
            if item_type == 1 and not allow_type1:
                continue
            if "min" in item or "max" in item:
                continue
            value = item.get("value")
            humidity = NavienSmartApiClient._int_value(value)
            if humidity is None:
                continue
            if humidity < 40 or humidity > 65 or humidity % TARGET_HUMIDITY_STEP != 0:
                continue
            priority = 0 if item_type == 3 else 1
            candidates.append((priority, humidity))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]

    @staticmethod
    def _additional_data_items(value: Any) -> list[dict[str, Any]]:
        """Collect additionalData items from common status shapes."""
        found: list[dict[str, Any]] = []

        def collect(item: Any) -> None:
            if isinstance(item, list):
                for child in item:
                    collect(child)
                return
            if not isinstance(item, dict):
                return
            if "type" in item and "value" in item:
                found.append(item)
                return
            additional = item.get("additionalData")
            if additional is not None:
                collect(additional)
            for key, child in item.items():
                if key == "additionalData":
                    continue
                if isinstance(child, (dict, list)):
                    collect(child)

        collect(value.get("additionalData") if isinstance(value, dict) else value)
        if not found:
            collect(value)
        return found

    @classmethod
    def _additional_data_paths(cls, value: Any) -> list[str]:
        """Return sanitized paths where additionalData appears."""
        paths: list[str] = []

        def collect(item: Any, path: str) -> None:
            if len(paths) >= 8:
                return
            if isinstance(item, list):
                for index, child in enumerate(item[:4]):
                    collect(child, f"{path}[{index}]")
                return
            if not isinstance(item, dict):
                return
            if "additionalData" in item:
                additional = item.get("additionalData")
                length = len(additional) if isinstance(additional, list) else None
                paths.append(f"{path}.additionalData:{type(additional).__name__}:{length}")
                collect(additional, f"{path}.additionalData")
            for key, child in item.items():
                if key == "additionalData":
                    continue
                if isinstance(child, (dict, list)):
                    collect(child, f"{path}.{key}")

        collect(value, "$")
        return paths

    def _extract_modes(self, raw_device: dict[str, Any]) -> tuple[NavienMode, ...]:
        """Extract supported room controller modes from the DID report."""
        properties = raw_device.get("Properties") or {}
        mode_items = (
            ((properties.get("data") or {}).get("did") or {})
            .get("reported", {})
            .get("roomController", {})
            .get("mode")
            or []
        )
        if not isinstance(mode_items, list):
            return ()

        by_name: dict[int, list[dict[str, Any]]] = {}
        for item in mode_items:
            if isinstance(item, dict) and item.get("name") is not None:
                by_name.setdefault(int(item["name"]), []).append(item)

        modes: list[NavienMode] = []
        seen: set[tuple[int, int]] = set()
        for item in mode_items:
            if not isinstance(item, dict):
                continue
            code = item.get("name")
            option = int(item.get("option") or 1)
            if code is None or (int(code), option) not in MODE_BY_CODE_OPTION:
                continue
            if (int(code), option) in seen:
                continue
            seen.add((int(code), option))
            key, label = MODE_BY_CODE_OPTION[(int(code), option)]
            humidity_min, humidity_max = self._humidity_range(item)
            modes.append(
                NavienMode(
                    key=key,
                    name=label,
                    mode=int(code),
                    option=option,
                    air_volume=int(item.get("airVolume") or 0),
                    configurable=bool(item.get("configurable")),
                    fan_options=self._fan_options_for_mode(item, by_name.get(int(code), [])),
                    humidity_min=humidity_min,
                    humidity_max=humidity_max,
                )
            )
        return tuple(modes)

    def _fan_options_for_mode(
        self,
        mode_item: dict[str, Any],
        same_code_items: list[dict[str, Any]],
    ) -> tuple[NavienFanOption, ...]:
        """Return selectable fan options for a mode, following app capability rules."""
        options: list[NavienFanOption] = []
        configurable = bool(mode_item.get("configurable"))
        supported = mode_item.get("supportedAirVolumes") or []
        
        # NRT-530S 등 일부 기기는 supportedAirVolumes가 없지만 additionalData type 1에 max=4 형태로 풍량을 줌
        if not supported and configurable:
            for item in mode_item.get("additionalData") or []:
                if isinstance(item, dict) and item.get("type") == 1:
                    max_val = item.get("max")
                    if max_val in (3, 4, 5):
                        supported = list(range(1, int(max_val) + 1))
                        break
            if not supported:
                supported = [1, 2, 3, 4]

        if configurable and isinstance(supported, list) and supported:
            for air_volume in supported:
                if int(air_volume) in FAN_BY_AIR_VOLUME:
                    key, name = FAN_BY_AIR_VOLUME[int(air_volume)]
                    options.append(
                        NavienFanOption(
                            key=key,
                            name=name,
                            option=int(mode_item.get("option") or 1),
                            air_volume=int(air_volume),
                            configurable=True,
                        )
                    )
        else:
            options.append(self._fan_option_from_fixed_mode(mode_item))

        if int(mode_item.get("option") or 1) == 4:
            return tuple(options)

        for item in same_code_items:
            option = int(item.get("option") or 1)
            if option == 2:
                options.append(NavienFanOption("turbo", FAN_LABELS["turbo"], option, int(item.get("airVolume") or 0), False))
            elif option == 3:
                options.append(NavienFanOption("saving", FAN_LABELS["saving"], option, int(item.get("airVolume") or 0), False))
            elif option == 5:
                options.append(NavienFanOption("basal", FAN_LABELS["basal"], option, int(item.get("airVolume") or 0), False))

        deduped: list[NavienFanOption] = []
        seen: set[str] = set()
        for option in options:
            if option.key not in seen:
                seen.add(option.key)
                deduped.append(option)
        return tuple(deduped)

    @staticmethod
    def _fan_option_from_fixed_mode(mode_item: dict[str, Any]) -> NavienFanOption:
        """Map a fixed mode's air volume to a fan label."""
        option = int(mode_item.get("option") or 1)
        air_volume = int(mode_item.get("airVolume") or 0)
        if option == 2:
            return NavienFanOption("turbo", FAN_LABELS["turbo"], option, air_volume, False)
        if option == 3:
            return NavienFanOption("saving", FAN_LABELS["saving"], option, air_volume, False)
        if option == 5:
            return NavienFanOption("basal", FAN_LABELS["basal"], option, air_volume, False)
        key, name = FAN_BY_AIR_VOLUME.get(air_volume, ("auto", FAN_LABELS["auto"]))
        return NavienFanOption(key, name, option, air_volume, False)

    @staticmethod
    def _humidity_range(mode_item: dict[str, Any]) -> tuple[int | None, int | None]:
        """Extract supported target humidity range."""
        for item in mode_item.get("additionalData") or []:
            if isinstance(item, dict) and item.get("type") == 1:
                minimum = item.get("min")
                maximum = item.get("max")
                return (
                    int(minimum) if minimum is not None else None,
                    int(maximum) if maximum is not None else None,
                )
        return None, None

    def _device_for_control(self, device_id: str) -> NavienDevice:
        """Return a known device for a control command."""
        device = self._devices.get(device_id)
        if device is None:
            raise NavienSmartApiError("Navien device has not been loaded yet")
        if not device.modes:
            raise NavienSmartUnsupportedError("This Navien device does not expose mapped room controller modes")
        return device

    @staticmethod
    def _mode_by_key(device: NavienDevice, mode_key: str) -> NavienMode:
        """Find a supported mode by key."""
        for mode in device.modes:
            if mode.key == mode_key:
                return mode
        raise NavienSmartUnsupportedError(f"Unsupported Navien operation mode: {mode_key}")

    @staticmethod
    def _fan_for_mode(mode: NavienMode, fan_key: str | None) -> NavienFanOption:
        """Find a fan option supported by the selected mode."""
        if not mode.fan_options:
            raise NavienSmartUnsupportedError(f"Navien mode {mode.name} has no fan options")
        if fan_key is not None:
            for fan in mode.fan_options:
                if fan.key == fan_key:
                    return fan
        return mode.fan_options[0]

    @staticmethod
    def _build_mode_desired(
        mode: NavienMode,
        fan: NavienFanOption,
        target_humidity: int | None,
    ) -> dict[str, Any]:
        """Build a V2 room controller mode command."""
        room_controller: dict[str, Any] = {
            "mode": mode.mode,
            "option": fan.option if fan.key in {"turbo", "saving", "basal"} else mode.option,
            "airVolume": fan.air_volume,
        }
        if mode.key == "dry" and target_humidity is not None:
            room_controller["additionalData"] = {"type": 1, "value": int(target_humidity)}
        return {"roomController": room_controller}

    async def _async_send_control(
        self,
        device: NavienDevice,
        command: str,
        desired: dict[str, Any],
    ) -> None:
        """Send a V2 room controller command through the Navien control endpoint."""
        if self._user_seq is None or self._home_seq is None:
            await self.async_login()
        raw = device.raw or {}
        model_code = raw.get("modelCode")
        physical_device_id = raw.get("deviceId")
        service_code = raw.get("serviceCode")
        if not model_code or not physical_device_id or service_code is None:
            raise NavienSmartApiError("Navien device is missing control metadata")

        await self._request_json(
            "POST",
            f"/api/v2.0/devices/{device.id}/control",
            params={"homeSeq": self._home_seq, "userSeq": self._user_seq},
            json_body={
                "serviceCode": service_code,
                "payload": {
                    "clientId": self._client_id(),
                    "sessionId": str(int(time.time() * 1000)),
                    "requestTopic": f"cmd/rc/v2/{model_code}/{physical_device_id}/remote/{command}",
                    "responseTopic": f"cmd/rc/v2/{model_code}/{physical_device_id}/remote/{command}/res",
                    "state": {"desired": desired},
                },
            },
        )

    def _client_id(self) -> str:
        """Return a stable MQTT-style client ID for HTTP control commands."""
        if self._user_seq is None:
            raise NavienSmartAuthError("Navien Smart login has not completed")
        if self._mqtt_client_id is None:
            self._mqtt_client_id = f"{uuid4()}-U{self._user_seq}"
        return self._mqtt_client_id

    async def _async_refresh_mqtt_status(self, raw_devices: list[dict[str, Any]]) -> None:
        """Request current room controller status over MQTT, then wait briefly."""
        control_devices = [
            item
            for item in raw_devices
            if item.get("deviceSeq")
            and item.get("deviceId")
            and item.get("modelCode")
            and item.get("serviceCode") is not None
        ]
        if not control_devices:
            return
        try:
            await self._async_ensure_mqtt_connected()
        except Exception as err:
            LOGGER.warning("Navien Smart MQTT status unavailable, falling back to HTTP polling: %s", err)
            return

        waiters: list[asyncio.Event] = []
        for raw_device in control_devices:
            topics = self._status_subscribe_topics(raw_device)
            event = asyncio.Event()
            if self._mqtt_client is not None:
                for topic in topics:
                    self._mqtt_client.subscribe(topic)
                    self._mqtt_topic_device_ids[topic] = str(raw_device.get("deviceId"))
                    self._mqtt_waiters[topic] = event
                    LOGGER.debug("Navien Smart MQTT subscribed topic=%s", topic)
            waiters.append(event)
            await self._async_send_status_request(raw_device)

        if not waiters:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*(event.wait() for event in waiters), return_exceptions=True),
                timeout=MQTT_STATUS_TIMEOUT,
            )
        except TimeoutError:
            pending = [
                str(raw_device.get("deviceId"))
                for raw_device, event in zip(control_devices, waiters, strict=False)
                if not event.is_set()
            ]
            LOGGER.warning("Navien Smart MQTT status timed out for deviceIds=%s", pending)
        finally:
            for raw_device in control_devices:
                for topic in self._status_subscribe_topics(raw_device):
                    self._mqtt_waiters.pop(topic, None)

    async def _async_send_status_request(self, raw_device: dict[str, Any]) -> None:
        """Ask the cloud bridge to publish current status on the MQTT response topic."""
        if self._user_seq is None or self._home_seq is None:
            await self.async_login()
            
        service_code = str(raw_device.get("serviceCode") or "")
        device_id = raw_device.get("deviceId")
        
        if service_code == "200":
            request_topic = f"$aws/things/{device_id}/shadow/name/status/get"
            response_topic = f"$aws/things/{device_id}/shadow/name/status/get/accepted"
        else:
            request_topic = self._status_request_topic(raw_device)
            response_topic = self._status_response_topic(raw_device)
            
        await self._request_json(
            "POST",
            f"/api/v2.0/devices/{raw_device['deviceSeq']}/control",
            params={"homeSeq": self._home_seq, "userSeq": self._user_seq},
            json_body={
                "serviceCode": service_code,
                "payload": {
                    "clientId": self._client_id(),
                    "sessionId": str(int(time.time() * 1000)),
                    "requestTopic": request_topic,
                    "responseTopic": response_topic,
                },
            },
        )

    async def _async_ensure_mqtt_connected(self) -> None:
        """Connect to AWS IoT MQTT over WebSockets using app credentials."""
        if self._mqtt_connected and self._mqtt_client is not None:
            return
        async with self._mqtt_lock:
            if self._mqtt_connected and self._mqtt_client is not None:
                return
            if not self._auth_info:
                raise NavienSmartApiError("Navien Smart MQTT credentials are unavailable")
            self._mqtt_loop = asyncio.get_running_loop()
            await asyncio.to_thread(self._connect_mqtt)

    def _connect_mqtt(self) -> None:
        """Blocking MQTT connect routine used in a worker thread."""
        try:
            import paho.mqtt.client as mqtt
        except ImportError as err:
            raise NavienSmartApiError("paho-mqtt is required for Navien Smart MQTT status") from err

        errors: list[str] = []
        for endpoint in AWS_IOT_ENDPOINTS:
            for signed_host in (endpoint, f"{endpoint}:443"):
                self._mqtt_connected = False
                client = mqtt.Client(
                    client_id=self._client_id(),
                    protocol=mqtt.MQTTv311,
                    transport="websockets",
                )
                client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
                client.username_pw_set(AWS_IOT_SDK_USER_META)
                client.ws_set_options(
                    path=self._aws_iot_websocket_path(signed_host),
                    headers={"User-Agent": AWS_IOT_SDK_USER_META},
                )
                client.on_connect = self._on_mqtt_connect
                client.on_disconnect = self._on_mqtt_disconnect
                client.on_message = self._on_mqtt_message
                try:
                    client.connect(endpoint, 443, keepalive=300)
                    client.loop_start()
                except Exception as err:
                    probe = self._probe_mqtt_websocket_handshake(endpoint, signed_host)
                    errors.append(f"{signed_host}: {err}; probe={probe}")
                    self._disconnect_mqtt_client(client)
                    continue

                deadline = time.time() + MQTT_STATUS_TIMEOUT
                while time.time() < deadline:
                    if self._mqtt_connected:
                        self._mqtt_client = client
                        LOGGER.debug("Navien Smart MQTT connected endpoint=%s signed_host=%s", endpoint, signed_host)
                        return
                    time.sleep(0.1)

                errors.append(f"{signed_host}: timed out waiting for CONNACK")
                self._disconnect_mqtt_client(client)

        raise NavienSmartApiError(
            "Navien Smart MQTT WebSocket connect failed: " + "; ".join(errors)
        )

    def _probe_mqtt_websocket_handshake(self, endpoint: str, signed_host: str) -> str:
        """Return sanitized HTTP details for a failed AWS IoT WebSocket handshake."""
        try:
            path = self._aws_iot_websocket_path(signed_host)
            key = base64.b64encode(os.urandom(16)).decode()
            connection = http.client.HTTPSConnection(endpoint, 443, timeout=MQTT_STATUS_TIMEOUT)
            connection.request(
                "GET",
                path,
                headers={
                    "Host": signed_host,
                    "User-Agent": AWS_IOT_SDK_USER_META,
                    "Upgrade": "websocket",
                    "Connection": "Upgrade",
                    "Sec-WebSocket-Key": key,
                    "Sec-WebSocket-Version": "13",
                    "Sec-WebSocket-Protocol": "mqtt",
                },
            )
            response = connection.getresponse()
            body = response.read(240).decode("utf-8", errors="replace")
            connection.close()
            body = re.sub(r"AKIA[0-9A-Z]+|ASIA[0-9A-Z]+", "**REDACTED_ACCESS_KEY**", body)
            return f"HTTP {response.status} {response.reason}: {body[:180]}"
        except Exception as err:
            return f"probe failed: {err}"

    @staticmethod
    def _disconnect_mqtt_client(client: Any) -> None:
        """Disconnect a paho MQTT client."""
        try:
            client.loop_stop()
        finally:
            client.disconnect()

    def _on_mqtt_connect(self, client: Any, userdata: Any, flags: Any, rc: int, *args: Any) -> None:
        """Handle MQTT connection."""
        self._mqtt_connected = self._mqtt_reason_is_success(rc)
        LOGGER.debug("Navien Smart MQTT connect result=%s connected=%s", rc, self._mqtt_connected)
        if not self._mqtt_connected:
            return
        for device in self._devices.values():
            raw = device.raw or {}
            if raw.get("modelCode") and raw.get("deviceId"):
                for topic in self._status_subscribe_topics(raw):
                    client.subscribe(topic)
                    self._mqtt_topic_device_ids[topic] = str(raw.get("deviceId"))

    def _on_mqtt_disconnect(self, client: Any, userdata: Any, rc: int, *args: Any) -> None:
        """Handle MQTT disconnection."""
        self._mqtt_connected = False

    def _on_mqtt_message(self, client: Any, userdata: Any, message: Any) -> None:
        """Store current status from MQTT."""
        topic = str(message.topic)
        payload = message.payload.decode("utf-8", errors="ignore")
        LOGGER.debug("Navien Smart MQTT message received topic=%s payload_length=%s", topic, len(payload))
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            data = {}
            
        # 매트(mate) 토픽인지 확인
        is_mat = "mate/" in topic
        
        status = None
        air_sensors = None
        changed = False
        
        physical_device_id = self._mqtt_topic_device_ids.get(topic) or self._physical_device_id_from_topic(topic)
        
        # 매트(mate) 또는 AWS IoT shadow 토픽인지 확인
        is_mat = "mate/" in topic or "$aws/things/" in topic or (physical_device_id and str(physical_device_id) in self.mat_devices)
        
        if is_mat:
            # 매트 shadow reported 추출
            reported = None
            if "state" in data and "reported" in data["state"]:
                reported = data["state"]["reported"]
            elif "heater" in data:
                reported = data
                
            if reported and physical_device_id:
                if str(physical_device_id) in self.mat_devices:
                    self.mat_devices[str(physical_device_id)].apply_reported(reported)
                    changed = True
        else:
            status = self._extract_mqtt_room_controller_status(payload)
            status = self._merge_mqtt_target_humidity(status, payload)
            if not physical_device_id and status:
                physical_device_id = status.get("deviceId")
                
            if physical_device_id is not None:
                if status:
                    self._latest_status_by_device_id[str(physical_device_id)] = status
                    changed = True
                air_sensors = self._extract_mqtt_air_sensors(payload, status)
                if air_sensors:
                    self._latest_air_sensors_by_device_id[str(physical_device_id)] = air_sensors
                    changed = True
                    
        if changed:
            self._notify_status_update()
            
        event = self._mqtt_waiters.get(topic)
        if event is not None and self._mqtt_loop is not None:
            self._mqtt_loop.call_soon_threadsafe(event.set)

    def _notify_status_update(self) -> None:
        """Notify Home Assistant that cached MQTT state changed."""
        if self._status_update_callback is None or self._mqtt_loop is None:
            return
        self._mqtt_loop.call_soon_threadsafe(self._status_update_callback)

    @classmethod
    def _extract_mqtt_room_controller_status(cls, payload: str) -> dict[str, Any]:
        """Extract room controller status from known AirOne MQTT payload shapes."""
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return {}
        return cls._find_room_controller_status(data)

    @classmethod
    def _find_room_controller_status(cls, value: Any) -> dict[str, Any]:
        """Find a live room controller status object in nested MQTT payloads."""
        if isinstance(value, list):
            for item in value:
                found = cls._find_room_controller_status(item)
                if found:
                    return found
            return {}
        if not isinstance(value, dict):
            return {}

        room_controller = value.get("roomController")
        if cls._looks_like_room_controller_status(room_controller):
            return room_controller
        if cls._looks_like_room_controller_status(value):
            return value

        for item in value.values():
            found = cls._find_room_controller_status(item)
            if found:
                return found
        return {}

    @staticmethod
    def _looks_like_room_controller_status(value: Any) -> bool:
        """Return whether a value looks like live room controller status."""
        if not isinstance(value, dict):
            return False
        mode = value.get("mode")
        if isinstance(mode, list):
            return False
        status_keys = {"running", "mode", "option", "airVolume", "additionalData"}
        return len(status_keys.intersection(value)) >= 2

    @classmethod
    def _merge_mqtt_target_humidity(
        cls,
        status: dict[str, Any],
        payload: str,
    ) -> dict[str, Any]:
        """Merge target humidity found outside the selected roomController object."""
        if not status:
            return status

        mode_code = cls._int_value(status.get("mode"))
        if cls._target_humidity_from_status(status, allow_type1=mode_code == 9) is not None:
            return status
        humidity = cls._target_humidity_from_payload(payload, allow_type1=mode_code == 9)
        if humidity is None:
            return status

        merged = dict(status)
        additional = merged.get("additionalData")
        if isinstance(additional, list):
            merged["additionalData"] = [*additional, {"type": 3, "value": humidity}]
        elif isinstance(additional, dict):
            merged["additionalData"] = [additional, {"type": 3, "value": humidity}]
        else:
            merged["additionalData"] = [{"type": 3, "value": humidity}]
        return merged

    @classmethod
    def _target_humidity_from_payload(cls, payload: str, *, allow_type1: bool) -> int | None:
        """Extract target humidity from the whole MQTT payload."""
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return None

        candidates: list[tuple[int, int]] = []
        for item in cls._additional_data_items(data):
            item_type = cls._int_value(item.get("type"))
            value = cls._int_value(item.get("value"))
            if item_type not in (1, 3) or value is None:
                continue
            if "min" in item or "max" in item:
                continue
            if item_type == 1 and not allow_type1:
                continue
            if value < 40 or value > 65 or value % TARGET_HUMIDITY_STEP != 0:
                continue
            priority = 0 if item_type == 3 else 1
            candidates.append((priority, value))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]

    @classmethod
    def _extract_mqtt_air_sensors(
        cls,
        payload: str,
        status: dict[str, Any],
    ) -> dict[str, dict[str, Any]]:
        """Extract air sensor values from likely MQTT payload shapes."""
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return {}

        candidates = [
            status.get("airSensorData"),
            status.get("airMonitor"),
            status.get("airSensors"),
            (((data.get("payload") or {}).get("reported") or {}).get("airMonitor")),
            (((data.get("payload") or {}).get("reported") or {}).get("airSensorData")),
            (((data.get("payload") or {}).get("reported") or {}).get("sensorList")),
            (((((data.get("payload") or {}).get("reported") or {}).get("eachRoomSd") or {}).get("airMonitor"))),
            (((((data.get("payload") or {}).get("reported") or {}).get("eachRoomSd") or {}).get("airSensorData"))),
            (((((data.get("payload") or {}).get("reported") or {}).get("eachRoomSd") or {}).get("sensorList"))),
            ((data.get("payload") or {}).get("airs")),
            data.get("airs"),
            data.get("sensorList"),
        ]

        values: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            cls._collect_air_sensor_values(candidate, values)
        return values

    @classmethod
    def _collect_air_sensor_values(cls, value: Any, values: dict[str, dict[str, Any]]) -> None:
        """Collect sensor values from common Navien air sensor shapes."""
        if isinstance(value, list):
            for item in value:
                cls._collect_air_sensor_values(item, values)
            return
        if not isinstance(value, dict):
            return

        if isinstance(value.get("airs"), list):
            cls._collect_air_sensor_values(value["airs"], values)

        sensor_type = cls._normalize_air_sensor_key(value.get("type"))
        if sensor_type is not None and "value" in value:
            values[str(sensor_type)] = {
                "value": value.get("value"),
                "level": value.get("level"),
                "zone_id": value.get("zoneId") or value.get("zone_id"),
                "update_time": value.get("updateTime") or value.get("update_time"),
                "source": "mqtt",
            }

        known_keys = {
            "temperature",
            "humidity",
            "pm1Dot0",
            "pm2Dot5",
            "pm10",
            "co2",
            "tvoc",
            "total",
            "radon",
            "radonValue",
            "radon_value",
            "radonBq",
            "radonBqm3",
        }
        for key in known_keys:
            if key not in value:
                continue
            normalized_key = cls._normalize_air_sensor_key(key)
            item = value[key]
            if isinstance(item, dict):
                values[normalized_key] = {
                    "value": item.get("value"),
                    "level": item.get("level"),
                    "zone_id": item.get("zoneId") or item.get("zone_id"),
                    "update_time": item.get("updateTime") or item.get("update_time"),
                    "source": "mqtt",
                }
            else:
                values[normalized_key] = {"value": item, "source": "mqtt"}

    @staticmethod
    def _normalize_air_sensor_key(value: Any) -> str | None:
        """Normalize air sensor key aliases from HTTP and MQTT payloads."""
        if value in (None, ""):
            return None
        key = str(value)
        return AIR_SENSOR_KEY_ALIASES.get(key.lower(), key)

    @classmethod
    def _log_mqtt_payload_shape(cls, topic: str, payload: str) -> None:
        """Log a sanitized MQTT payload shape for protocol mapping."""
        if not LOGGER.isEnabledFor(logging.DEBUG):
            return
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            LOGGER.debug(
                "Navien Smart MQTT payload topic=%s non_json_length=%s",
                topic,
                len(payload),
            )
            return

        room_controller = cls._extract_mqtt_room_controller_status(payload)
        air_candidates = cls._mqtt_air_candidate_shapes(data, room_controller)
        LOGGER.debug(
            "Navien Smart MQTT payload shape topic=%s top_keys=%s room_controller_keys=%s additional_data_count=%s air_candidates=%s",
            topic,
            cls._safe_keys(data),
            cls._safe_keys(room_controller),
            len(cls._additional_data_items(room_controller)),
            air_candidates,
        )

    @classmethod
    def _mqtt_air_candidate_shapes(
        cls,
        data: dict[str, Any],
        room_controller: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """Return sanitized descriptions of likely air sensor payload locations."""
        candidates = {
            "roomController.airMonitor": room_controller.get("airMonitor"),
            "roomController.airSensorData": room_controller.get("airSensorData"),
            "roomController.airSensors": room_controller.get("airSensors"),
            "payload.reported.airMonitor": (((data.get("payload") or {}).get("reported") or {}).get("airMonitor")),
            "payload.reported.airSensorData": (((data.get("payload") or {}).get("reported") or {}).get("airSensorData")),
            "payload.reported.sensorList": (((data.get("payload") or {}).get("reported") or {}).get("sensorList")),
            "payload.reported.eachRoomSd": (((data.get("payload") or {}).get("reported") or {}).get("eachRoomSd")),
            "payload.reported.eachRoomSd.roomController": (((((data.get("payload") or {}).get("reported") or {}).get("eachRoomSd") or {}).get("roomController"))),
            "state.reported.eachRoomSd": (((data.get("state") or {}).get("reported") or {}).get("eachRoomSd")),
            "payload.airs": ((data.get("payload") or {}).get("airs")),
            "airs": data.get("airs"),
            "sensorList": data.get("sensorList"),
        }
        result: list[dict[str, Any]] = []
        for path, value in candidates.items():
            if value in (None, {}, []):
                continue
            result.append(
                {
                    "path": path,
                    "type": type(value).__name__,
                    "keys": cls._safe_keys(value),
                    "length": len(value) if isinstance(value, list) else None,
                }
            )
        return result

    @staticmethod
    def _safe_keys(value: Any) -> list[str]:
        """Return sorted keys for debug logging without values."""
        if isinstance(value, dict):
            return sorted(str(key) for key in value)
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return sorted(str(key) for key in value[0])
        return []

    @staticmethod
    def _mqtt_reason_is_success(reason_code: Any) -> bool:
        """Return whether a paho MQTT reason code means success."""
        try:
            return int(reason_code) == 0
        except (TypeError, ValueError):
            return str(reason_code).lower() in {"0", "success", "normal disconnection"}

    @staticmethod
    def _status_request_topic(raw_device: dict[str, Any]) -> str:
        """Return the V2 status request topic for a raw device."""
        return f"cmd/rc/v2/{raw_device.get('modelCode')}/{raw_device.get('deviceId')}/remote/status"

    @staticmethod
    def _status_response_topic(raw_device: dict[str, Any]) -> str:
        """Return the V2 status response topic for a raw device."""
        return f"cmd/rc/v2/{raw_device.get('modelCode')}/{raw_device.get('deviceId')}/remote/status/res"

    @staticmethod
    def _control_response_topic(raw_device: dict[str, Any]) -> str:
        """Return the V2 wildcard response topic for app and HA control changes."""
        return f"cmd/rc/v2/{raw_device.get('modelCode')}/{raw_device.get('deviceId')}/remote/+/res"

    def _home_airone_topic(self, raw_device: dict[str, Any]) -> str | None:
        """Return the home AirOne topic observed in the mobile app logs."""
        if self._home_seq is None or raw_device.get("deviceId") is None:
            return None
        return f"{self._home_seq}/airone/{raw_device.get('deviceId')}"

    def _status_subscribe_topics(self, raw_device: dict[str, Any]) -> tuple[str, ...]:
        """Return all topics that may carry current status for a device."""
        service_code = str(raw_device.get("serviceCode") or "")
        
        if service_code == "200":
            device_id = raw_device.get("deviceId")
            return (
                f"{self._home_seq}/mate/{device_id}",
                f"$aws/things/{device_id}/shadow/name/status/update/accepted",
                f"$aws/things/{device_id}/shadow/name/status/update/documents",
                f"$aws/things/{device_id}/shadow/name/status/get/accepted",
            )
            
        topics = [
            self._status_response_topic(raw_device),
            self._control_response_topic(raw_device),
        ]
        home_topic = self._home_airone_topic(raw_device)
        if home_topic:
            topics.append(home_topic)
        return tuple(topics)

    @staticmethod
    def _physical_device_id_from_topic(topic: str) -> str | None:
        """Extract the physical device id from known AirOne MQTT topics."""
        match = re.search(r"cmd/rc/v2/[^/]+/([^/]+)/remote/", topic)
        if match:
            return match.group(1)
        match = re.search(r"/airone/([^/]+)$", topic)
        if match:
            return match.group(1)
        match = re.search(r"/mate/([^/]+)$", topic)
        if match:
            return match.group(1)
        match = re.search(r"\$aws/things/([^/]+)/shadow/", topic)
        if match:
            return match.group(1)
        return None

    def _aws_iot_websocket_path(self, host_header: str) -> str:
        """Build a SigV4 signed AWS IoT WebSocket path."""
        access_key = str(self._auth_info.get("accessKeyId") or "").strip()
        secret_key = str(self._auth_info.get("secretKey") or "").strip()
        session_token = str(self._auth_info.get("sessionToken") or "").strip()
        if not access_key or not secret_key or not session_token:
            raise NavienSmartApiError("Navien Smart MQTT credentials are incomplete")

        now = datetime.now(UTC)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        credential_scope = f"{date_stamp}/{AWS_IOT_REGION}/{AWS_IOT_SERVICE}/aws4_request"
        query: dict[str, str] = {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": f"{access_key}/{credential_scope}",
            "X-Amz-Date": amz_date,
            "X-Amz-SignedHeaders": "host",
        }
        canonical_query = self._canonical_query(query)
        canonical_request = "\n".join(
            [
                "GET",
                "/mqtt",
                canonical_query,
                f"host:{host_header}\n",
                "host",
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            ]
        )
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                credential_scope,
                hashlib.sha256(canonical_request.encode()).hexdigest(),
            ]
        )
        signing_key = self._aws_signing_key(secret_key, date_stamp)
        signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
        query["X-Amz-Signature"] = signature
        return f"/mqtt?{self._canonical_query(query)}&X-Amz-Security-Token={quote(session_token, safe='-_.~')}"

    @staticmethod
    def _canonical_query(query: dict[str, str]) -> str:
        """Return AWS SigV4 canonical query string."""
        return "&".join(
            f"{quote(str(key), safe='-_.~')}={quote(str(query[key]), safe='-_.~')}"
            for key in sorted(query)
        )

    @staticmethod
    def _aws_signing_key(secret_key: str, date_stamp: str) -> bytes:
        """Return AWS SigV4 signing key."""
        key_date = hmac.new(f"AWS4{secret_key}".encode(), date_stamp.encode(), hashlib.sha256).digest()
        key_region = hmac.new(key_date, AWS_IOT_REGION.encode(), hashlib.sha256).digest()
        key_service = hmac.new(key_region, AWS_IOT_SERVICE.encode(), hashlib.sha256).digest()
        return hmac.new(key_service, b"aws4_request", hashlib.sha256).digest()

    @staticmethod
    def _extract_device_name(value: Any) -> str | None:
        """Extract a clean device name from Navien's nickname payload."""
        if isinstance(value, str):
            return value or None
        if isinstance(value, dict):
            for key in ("mainItem", "name", "nickName"):
                item = value.get(key)
                if isinstance(item, str) and item:
                    return item
        return None

    async def _get_air_sensors(self, device_seq: str) -> dict[str, dict[str, Any]]:
        """Fetch air monitor values for a device."""
        if self._user_seq is None or self._home_seq is None:
            return {}

        data = await self._request_json(
            "GET",
            f"/api/v2.0/devices/{device_seq}/air-sensor",
            params={"homeSeq": self._home_seq, "userSeq": self._user_seq},
        )
        sensor_list = data.get("data", {}).get("sensorList") or []
        values: dict[str, dict[str, Any]] = {}
        for sensor in sensor_list:
            air_monitor = sensor.get("airMonitor") or {}
            for air in sensor.get("airs") or []:
                sensor_type = self._normalize_air_sensor_key(air.get("type"))
                if sensor_type:
                    values[str(sensor_type)] = {
                        "value": air.get("value"),
                        "level": air.get("level"),
                        "zone_id": sensor.get("zoneId"),
                        "update_time": sensor.get("updateTime"),
                        "air_monitor_support": air_monitor.get("support"),
                        "air_monitor_paired": air_monitor.get("paired"),
                        "air_monitor_connected": air_monitor.get("connected"),
                        "air_monitor_model_code": air_monitor.get("modelCode"),
                        "source": "http",
                    }
        return values

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        allow_reauth: bool = True,
    ) -> dict[str, Any]:
        """Call the native Navien Smart API."""
        try:
            return await self._request_json_once(
                method,
                path,
                params=params,
                json_body=json_body,
            )
        except NavienSmartAuthError:
            if not allow_reauth:
                raise
            await self.async_login()
            return await self._request_json_once(
                method,
                path,
                params=params,
                json_body=json_body,
            )

    async def _request_json_once(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call the native Navien Smart API once."""
        headers = {
            "Accept": "application/json",
            "User-Agent": self._headers["User-Agent"],
        }
        if self._access_token:
            headers["Authorization"] = self._access_token

        async with self._session.request(
            method,
            f"{API_BASE_URL}{path}",
            params=params,
            json=json_body,
            headers=headers,
        ) as response:
            text = await response.text()
            await self._raise_for_api_status(response, text)

        try:
            data = json.loads(text)
        except json.JSONDecodeError as err:
            raise NavienSmartApiError("Navien Smart API returned invalid JSON") from err

        if data.get("code") not in (None, 200):
            if data.get("code") in (401, 403):
                raise NavienSmartAuthError(data.get("msg") or "Navien Smart authentication failed")
            raise NavienSmartApiError(data.get("msg") or "Navien Smart API request failed")
        return data

    async def _raise_for_api_status(self, response: ClientResponse, text: str) -> None:
        """Convert HTTP failures into integration errors."""
        if response.status < 400:
            return
        if response.status in (401, 403):
            raise NavienSmartAuthError("Navien Smart authentication failed")
        raise NavienSmartApiError(
            f"Navien Smart request failed with HTTP {response.status}: {text[:120]}"
        )

    @staticmethod
    def _float_air_value(
        air_sensors: dict[str, dict[str, Any]],
        sensor_type: str,
    ) -> float | None:
        """Return a numeric air sensor value."""
        value = (air_sensors.get(sensor_type) or {}).get("value")
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
