import asyncio
import json
import logging
import re
import time
import traceback
import uuid

import aiohttp

from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from homeassistant.core import callback

from .const import (
    ATTR_DEVICE_ZONE,
    DATA_HAVCS_OPEN_UIDS,
    DUEROS_CHANGE_REPORT_URL,
    DUEROS_DEVICE_SYNC_URL,
    INTEGRATION,
)
from .helper import VoiceControlDeviceManager, VoiceControlProcessor
from .timer import HavcsTimerManager
from .util import decrypt_device_id, encrypt_device_id, mask_tokens

_LOGGER = logging.getLogger(__name__)

DOMAIN = 'dueros'
LOGGER_NAME = 'dueros'

STORAGE_VERSION = 1
MAX_ATTRIBUTES = 10

_TOKEN_PATTERN = re.compile(r'("(?:accessToken|token)"\s*:\s*")([^"]+)"', re.I)

# 定时动作与实体域的映射：(turn_on, turn_off)
_TIMING_SERVICE_MAP = {
    'light': ('turn_on', 'turn_off'),
    'switch': ('turn_on', 'turn_off'),
    'input_boolean': ('turn_on', 'turn_off'),
    'fan': ('turn_on', 'turn_off'),
    'humidifier': ('turn_on', 'turn_off'),
    'media_player': ('turn_on', 'turn_off'),
    'cover': ('open_cover', 'close_cover'),
    'vacuum': ('start', 'return_to_base'),
    'scene': ('turn_on', 'turn_on'),
}

# 发现设备数量上限（协议限制 300）
_MAX_APPLIANCES = 300
# 发现分组数量上限（协议限制 10）
_MAX_GROUPS = 10

# DuerOS AIR_CONDITION mode → HA hvac_mode
_DUEROS_TO_HA_CLIMATE_MODE = {
    'COOL': 'cool',
    'HEAT': 'heat',
    'AUTO': 'auto',
    'FAN': 'fan_only',
    'DEHUMIDIFICATION': 'dry',
}

# GetLocationResponse 允许的区域枚举（其它区域回退为明文）
_LOCATION_ENUM = {
    '主卧': 'MASTER_BEDROOM',
    '次卧': 'SECOND_BEDROOM',
    '客厅': 'LIVING_ROOM',
    '厨房': 'KITCHEN',
    '书房': 'STUDY',
    '餐厅': 'RESTAURANT',
}

# DuerOS 扫地机 mode → HA vacuum state
_SWEEPING_MODE_HA_STATE = {
    'AUTO': 'cleaning',
    'AUTO_CLEAN': 'cleaning',
    'FOCUS': 'cleaning',
    'FOCUS_CLEAN': 'cleaning',
    'ALONG_EDGE': 'cleaning',
    'EDGE': 'cleaning',
    'AUTO_MOP': 'cleaning',
    'FOCUS_MOP': 'cleaning',
    'ALONG_EDGE_MOP': 'cleaning',
    'STANDARD': 'cleaning',
    'POWERFUL': 'cleaning',
    'MAX': 'cleaning',
    'QUIET': 'cleaning',
    'ANTI_DISTURB': 'cleaning',
    'CHARGE': 'returning',
    'PAUSE': 'paused',
}

# DuerOS 水量档位 → HA 百分比
_WATER_LEVEL_PERCENT = {'LOW': 33, 'MIDDLE': 66, 'HIGH': 100}
_WATER_LEVEL_KEYS = ('water', 'mop', '水量', '水箱')

# 风扇模式中表示摆风的取值
_SWING_MODES = ('SWING', 'OSCILLATE', 'SWING_UP_DOWN', 'SWING_LEFT_RIGHT',
                'SWING_LEFT_RIGHT_SWING', '摆风', '摆动')
# 风扇模式中表示停止摆风的取值
_NON_SWING_FAN_MODES = ('STOP', 'CANCEL', 'OFF', 'NONE', 'FIXED', 'STATIC')


_REPORT_WARN_INTERVAL = 3600.0

# 色温上下界缺省值（设备未上报能力时使用）
_DEFAULT_MIN_KELVIN = 2000.0
_DEFAULT_MAX_KELVIN = 6500.0

# 同一属性两次主动上报的最小间隔（小度限制 60 秒，21207）
_CHANGE_REPORT_INTERVAL = 60.0
# 设备同步限频（applianceId 失效时触发）
_SYNC_MIN_INTERVAL = 600.0
# 小度返回码：同属性 60 秒内重复同步 / 未收到 ReportStateResponse
_SYNC_STATUS_RATE_LIMITED = 21207
_SYNC_STATUS_NAME_MISMATCH = 21096


def _shorten(text, limit=200):
    """压缩空白并截断，便于在日志中查看上游响应摘要。"""
    if not text:
        return ''
    collapsed = ' '.join(str(text).split())
    return collapsed[:limit] + ('...' if len(collapsed) > limit else '')


class _ReportDeliveryError(Exception):
    """上报请求未送达（超时/网络错误，重试后仍失败）。"""


# 上报请求超时与重试：小度云端偶发慢响应，短超时会产生大量瞬时失败
_REPORT_TIMEOUT = 15.0
_REPORT_ATTEMPTS = 2
_REPORT_RETRY_DELAY = 1.0


async def _async_post_report(session, url, payload) -> tuple:
    """上报类 POST：尽力把响应体解析为 JSON。

    实测百度侧会用 text/html 的 Content-Type 承载 JSON 结果，
    因此不依赖 Content-Type 判断，一律尝试解析；空响应体按成功处理。
    超时/网络错误重试一次，仍失败抛 _ReportDeliveryError。
    """
    for attempt in range(1, _REPORT_ATTEMPTS + 1):
        try:
            async with asyncio.timeout(_REPORT_TIMEOUT):
                response = await session.post(url, json=payload, headers={"Content-Type": "application/json"})
                text = (await response.text()).strip()
            break
        except (TimeoutError, aiohttp.ClientError) as err:
            if attempt >= _REPORT_ATTEMPTS:
                raise _ReportDeliveryError(f'请求失败（{_REPORT_ATTEMPTS} 次尝试）: {err}') from err
            await asyncio.sleep(_REPORT_RETRY_DELAY)
    if not text:
        return response.status, {}
    try:
        return response.status, json.loads(text)
    except json.JSONDecodeError:
        return response.status, text


def _clamp(value, lower, upper):
    return max(lower, min(upper, value))


def _clamp_percent(value):
    return _clamp(float(value), 0.0, 100.0)


def _as_float(value):
    """尽量转换为 float，失败返回 None。"""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _payload_value(payload, key, default=None):
    if not isinstance(payload, dict):
        return default
    section = payload.get(key)
    if section is None:
        return default
    if isinstance(section, dict):
        return section.get('value', default)
    return section


def _kelvin_bounds(state, hass=None, entity_id=None):
    """色温上下界：优先取状态能力属性，其次读实体对象，最后用通用默认值。"""
    min_k = _as_float(state.attributes.get('min_color_temp_kelvin')) if state else None
    max_k = _as_float(state.attributes.get('max_color_temp_kelvin')) if state else None
    if (min_k is None or max_k is None) and hass is not None and entity_id:
        entity = _get_light_entity(hass, entity_id)
        if entity is not None:
            if min_k is None:
                min_k = _as_float(getattr(entity, 'min_color_temp_kelvin', None))
            if max_k is None:
                max_k = _as_float(getattr(entity, 'max_color_temp_kelvin', None))
    return (min_k if min_k is not None else _DEFAULT_MIN_KELVIN,
            max_k if max_k is not None else _DEFAULT_MAX_KELVIN)


def _get_light_entity(hass, entity_id):
    try:
        from homeassistant.helpers import entity_component

        component = entity_component.get_component(hass, 'light')
        return component.get_entity(entity_id) if component is not None else None
    except Exception:
        return None


def _clamp_kelvin(hass, state, entity_id, value):
    if value is None:
        raise ValueError('colorTemperatureInKelvin is required')
    min_k, max_k = _kelvin_bounds(state, hass, entity_id)
    return int(_clamp(float(value), min_k, max_k))


def _resolve_kelvin_delta(hass, state, entity_id, payload, sign):
    delta_pct = _as_float(_payload_value(payload, 'deltaPercentage', 10.0)) or 10.0
    min_k, max_k = _kelvin_bounds(state, hass, entity_id)
    current = _as_float(state.attributes.get('color_temp_kelvin')) if state else None
    current = current if current is not None else (min_k + max_k) / 2
    return _clamp_kelvin(hass, state, entity_id, current + sign * delta_pct / 100 * (max_k - min_k))


def _current_brightness_pct(state):
    if state is None:
        return 0.0
    raw = _as_float(state.attributes.get('brightness'))
    if raw is None:
        return 100.0 if state.state == 'on' else 0.0
    return raw / 255 * 100


def _resolve_brightness_set(state, payload):
    value = _as_float(_payload_value(payload, 'brightness'))
    if value is None:
        raise ValueError('brightness is required')
    return _clamp_percent(value)


def _resolve_brightness_delta(state, payload, sign):
    delta = _as_float(_payload_value(payload, 'deltaPercentage', 10.0)) or 10.0
    if state is not None and state.state != 'on':
        if sign < 0:
            # 灯未打开时无法“调暗”
            raise ValueError('device is off')
        # 关闭状态下“调亮”按打开并给一个起始亮度
        return _clamp_percent(delta)
    return _clamp_percent(_current_brightness_pct(state) + sign * abs(delta))


def _resolve_color(state, payload):
    color = payload.get('color') if isinstance(payload, dict) else None
    if not color:
        raise ValueError('color is required')
    hs_color = [float(color.get('hue', 0)), float(color.get('saturation', 1)) * 100]
    brightness = _as_float(color.get('brightness'))
    data = {'hs_color': hs_color}
    if brightness is not None:
        data['brightness_pct'] = _clamp_percent(brightness * 100)
    return data


def _temperature_bounds(state):
    min_t = _as_float(state.attributes.get('min_temp')) if state else None
    max_t = _as_float(state.attributes.get('max_temp')) if state else None
    return min_t, max_t


def _clamp_temperature(state, value):
    min_t, max_t = _temperature_bounds(state)
    if min_t is not None:
        value = max(value, min_t)
    if max_t is not None:
        value = min(value, max_t)
    return round(value, 1)


def _resolve_target_temperature(state, payload):
    value = _as_float(_payload_value(payload, 'targetTemperature'))
    if value is None:
        raise ValueError('targetTemperature is required')
    return _clamp_temperature(state, value)


def _resolve_temperature_delta(state, payload, sign):
    delta = _as_float(_payload_value(payload, 'deltaValue', 1.0)) or 1.0
    current = _as_float(state.attributes.get('temperature')) if state else None
    if current is None:
        raise ValueError('current target temperature unknown')
    return _clamp_temperature(state, current + sign * abs(delta))


def _resolve_vacuum_fan_speed(state, payload):
    level = str(_payload_value(payload, 'suction', '')).upper()
    speeds = list((state.attributes.get('fan_speed_list') if state else None) or [])
    if not speeds:
        raise ValueError('device does not support fan speed')
    lowered = [str(speed).lower() for speed in speeds]
    strong_keys = ('strong', 'turbo', 'max', 'high', 'powerful', 'boost')
    standard_keys = ('standard', 'normal', 'medium', 'mid', 'balanced', 'default', 'quiet', 'low', 'min')
    if level == 'STRONG':
        for key in strong_keys:
            for index, speed in enumerate(lowered):
                if key in speed:
                    return speeds[index]
        return speeds[-1]
    for key in standard_keys:
        for index, speed in enumerate(lowered):
            if key in speed:
                return speeds[index]
    return speeds[0]


def _resolve_volume_level(payload):
    value = _as_float(_payload_value(payload, 'deltaValue'))
    if value is None:
        raise ValueError('deltaValue is required')
    return _clamp(value / 100, 0.0, 1.0)


def _resolve_volume_mute(payload):
    value = str(_payload_value(payload, 'deltaValue', '')).lower()
    if value not in ('on', 'off'):
        raise ValueError('deltaValue must be on/off')
    return value == 'on'


def _resolve_humidity(state, payload):
    value = _as_float(_payload_value(payload, 'deltaValue'))
    if value is None:
        raise ValueError('deltaValue is required')
    return int(_clamp(value, 0, 100))


def _resolve_mode_value(payload, key='mode'):
    value = _payload_value(payload, key)
    if value is None:
        raise ValueError(f'{key} is required')
    return str(value)


def _resolve_climate_fan_mode(state, payload):
    """Resolve DuerOS fanSpeed to a HA climate fan_mode string."""
    fan_speed = payload.get('fanSpeed') if isinstance(payload, dict) else None
    if not fan_speed:
        raise ValueError('fanSpeed is required')
    fan_modes = list((state.attributes.get('fan_modes') if state else None) or [])
    current = (state.attributes.get('fan_mode') if state else None) or ''
    if fan_speed.get('value') is not None:
        value = int(fan_speed['value'])
        if fan_modes:
            index = min(len(fan_modes) - 1, max(0, int((value - 1) * len(fan_modes) / 10)))
            return fan_modes[index]
        return current
    level_map = {
        'min': ['low', 'quiet', 'min'],
        'low': ['low'],
        'middle': ['medium', 'mid', 'middle'],
        'high': ['high'],
        'max': ['max', 'turbo', 'powerful', 'high'],
        'auto': ['auto'],
    }
    candidates = level_map.get(str(fan_speed.get('level', '')).lower(), [])
    if fan_modes:
        for candidate in candidates:
            for mode in fan_modes:
                if candidate.lower() == mode.lower():
                    return mode
    return candidates[0] if candidates else current


def _resolve_fan_percentage(payload):
    """DuerOS fanSpeed(value/level) → HA fan percentage。"""
    fan_speed = payload.get('fanSpeed') if isinstance(payload, dict) else None
    if not fan_speed:
        raise ValueError('fanSpeed is required')
    if fan_speed.get('value') is not None:
        return _clamp_percent(float(fan_speed['value']) * 10)
    level_map = {'min': 20, 'low': 30, 'middle': 50, 'high': 80, 'max': 100, 'auto': 50}
    level = str(fan_speed.get('level', '')).lower()
    if level in level_map:
        return float(level_map[level])
    raise ValueError('unknown fanSpeed level')


def _resolve_turn_on_state(entity_id, state):
    """按设备域判断 DuerOS turnOnState。"""
    domain = entity_id.split('.', 1)[0]
    if domain == 'scene':
        # 场景是"执行"语义，没有可读状态，统一按 ON 上报
        return 'ON'
    value = state.state
    if value in ('unavailable', 'unknown'):
        return 'OFF'
    if domain == 'climate':
        return 'OFF' if value == 'off' else 'ON'
    if domain == 'cover':
        return 'ON' if value in ('open', 'opening') else 'OFF'
    if domain == 'media_player':
        return 'ON' if value in ('on', 'playing', 'paused', 'idle', 'buffering') else 'OFF'
    if domain == 'vacuum':
        return 'ON' if value in ('cleaning', 'returning') else 'OFF'
    return 'ON' if value == 'on' else 'OFF'


def _resolve_work_state(value):
    """扫地机等设备的 state 属性（GetStateResponse 取值）。"""
    mapping = {
        'cleaning': 'CLEANING',
        'returning': 'RECHARGING',
        'docked': 'CHARGING',
        'idle': 'STAND_BY',
        'paused': 'PAUSED',
        'error': 'REPORT_ERROR',
        'unavailable': 'STAND_BY',
    }
    return mapping.get(str(value).lower(), str(value).upper())


def _resolve_suction(value):
    """HA vacuum fan_speed → DuerOS suction 枚举。"""
    if value is None:
        return None
    text = str(value).lower()
    strong_keys = ('strong', 'turbo', 'max', 'high', 'powerful', 'boost')
    return 'STRONG' if any(key in text for key in strong_keys) else 'STANDARD'


def _is_swing_mode(value):
    """判断 DuerOS 模式值是否为风扇摆风；非摆风值返回 False（如 NORMAL/QUIET）。"""
    text = str(value).upper()
    if text in _SWING_MODES:
        return True
    if text in _NON_SWING_FAN_MODES:
        return False
    raise ValueError(f'unsupported fan mode: {value}')


def _resolve_fan_mode(state, payload):
    """风扇 SetModeRequest → (服务名, 数据)：优先匹配 preset_mode，否则按摆风处理。"""
    mode = _resolve_mode_value(payload)
    text = str(mode).upper()
    state = state or None
    presets = list((state.attributes.get('preset_modes') if state else None) or [])
    for preset in presets:
        if str(preset).upper() == text:
            return (['fan'], ['set_preset_mode'], [{'preset_mode': preset}])
    return (['fan'], ['oscillate'], [{'oscillating': _is_swing_mode(mode)}])


def _resolve_work_state_from_mode(payload):
    """扫地机 SetModeRequest → HA vacuum 状态。"""
    mode = str(_payload_value(payload, 'mode', '')).upper()
    if not mode:
        raise ValueError('mode is required')
    ha_state = _SWEEPING_MODE_HA_STATE.get(mode)
    if not ha_state:
        raise ValueError(f'unsupported sweeping mode: {mode}')
    return ha_state


def _resolve_water_level(payload, key='waterLevel'):
    """扫地机水量档位 → HA 百分比；未指定档位时返回 None。"""
    level = _payload_value(payload, key)
    if level is None:
        return None
    text = str(level).upper()
    if text not in _WATER_LEVEL_PERCENT:
        raise ValueError(f'unsupported water level: {level}')
    return _WATER_LEVEL_PERCENT[text]


def _mode_to_swing(value):
    """HA oscillating 布尔值 → DuerOS 风扇模式值。"""
    if value is True:
        return 'SWING'
    if value is False:
        return 'STOP'
    return None


def _first_number(state, keys):
    for key in keys:
        value = _as_float(state.attributes.get(key)) if state and state.attributes else None
        if value is not None:
            return value
    return None


def _clamp_percent_value(value):
    if value is None:
        return None
    return int(_clamp_percent(value))


def _resolve_electricity_capacity(state):
    """电量：优先百分比实体（battery_level），其次数值实体，再退回 state。"""
    if state is None:
        return None
    value = _first_number(state, ('battery_level', 'battery', 'electricity_capacity'))
    if value is None:
        value = _as_float(state.state)
    return _clamp_percent_value(value)


def _resolve_water_level_value(state):
    """水量控制实体数值 → 水量档位（LOW/MIDDLE/HIGH）。"""
    if state is None:
        return None
    value = _as_float(state.state)
    if value is None:
        return None
    percent = _clamp_percent(value)
    if percent <= 34:
        return 'LOW'
    if percent <= 67:
        return 'MIDDLE'
    return 'HIGH'


def _vacuum_set_mode(payload):
    """扫地机 SetModeRequest → HA 服务调用（HA 无清扫模式服务，按模式语义落到启停）。"""
    ha_state = _resolve_work_state_from_mode(payload)
    service = {'cleaning': 'start', 'returning': 'return_to_base', 'paused': 'pause'}.get(ha_state, 'start')
    return (['vacuum'], [service], [{}])


def _vacuum_water_level(payload):
    """扫地机水量档位 → 水量控制实体的 set_value。"""
    percent = _resolve_water_level(payload)
    if percent is None:
        raise ValueError('waterLevel is required')
    return (['number'], ['set_value'], [{'value': percent}])


def _vacuum_direction(payload):
    """扫地机方向指令 → 清扫/暂停；其余方向 HA 无对应能力，如实报错。"""
    direction = str(_payload_value(payload, 'direction', '')).upper()
    if not direction:
        raise ValueError('direction is required')
    if direction in ('STOP', 'PAUSE'):
        return (['vacuum'], ['pause'], [{}])
    if direction in ('FORWARD', 'BACKWARD', 'LEFT', 'RIGHT', 'TURN_LEFT', 'TURN_RIGHT'):
        raise ValueError('manual direction control is not supported by Home Assistant')
    raise ValueError(f'unsupported direction: {direction}')


def _kelvin_light_handlers() -> dict:
    """灯色温指令处理器：light.turn_on 只接受 color_temp_kelvin 参数。

    state 对象自带 hass 与 entity_id，可据此读取该灯真实的色温上下界。
    """
    def resolve_state(state):
        return (getattr(state, 'hass', None), state.entity_id if state else None)

    def set_handler(state, attributes, payload):
        hass, entity_id = resolve_state(state)
        value = _payload_value(payload, 'colorTemperatureInKelvin')
        return (['light'], ['turn_on'],
                [{'color_temp_kelvin': _clamp_kelvin(hass, state, entity_id, value)}])

    def delta_handler(sign):
        def handler(state, attributes, payload):
            hass, entity_id = resolve_state(state)
            value = _resolve_kelvin_delta(hass, state, entity_id, payload, sign)
            return (['light'], ['turn_on'], [{'color_temp_kelvin': value}])
        return handler

    return {
        'SetColorTemperatureRequest': set_handler,
        'IncrementColorTemperatureRequest': delta_handler(1),
        'DecrementColorTemperatureRequest': delta_handler(-1),
    }


class PlatformParameter:
    """DuerOS 协议参数与映射表。"""

    # Map internal attribute names (from helper.py get_device_properties) to DuerOS attribute names
    device_attribute_map_h2p = {
        'turnonstate': 'turnOnState',
        'temperature': 'temperatureReading',
        'targettemperature': 'targetTemperature',
        'brightness': 'brightness',
        'color_temperature': 'colorTemperatureInKelvin',
        'color': 'color',
        'humidity': 'humidity',
        'targethumidity': 'targetHumidity',
        'pm25': 'pm2.5',
        'pm10': 'PM10',
        'co2': 'co2',
        'hcho': 'formaldehyde',
        'aqi': 'airQuality',
        'hvac_mode': 'mode',
        'fan_mode': 'fanSpeed',
        'fanspeed': 'fanSpeed',
        'mode': 'mode',
        'percentage': 'percentage',
        'state': 'state',
        'illumination': 'illumination',
        'warmthlevel': 'warmthLevel',
        'volume': 'volume',
        'suction': 'suction',
        'electricitycapacity': 'electricityCapacity',
        'waterlevel': 'waterLevel',
        'location': 'location',
    }

    # Map HA state attribute keys (from __init__.py state listener) to DuerOS attribute names
    ha_attribute_to_dueros = {
        'state': 'turnOnState',
        'brightness': 'brightness',
        'color_temp': 'colorTemperatureInKelvin',
        'color_temp_kelvin': 'colorTemperatureInKelvin',
        'rgb_color': 'color',
        'xy_color': 'color',
        'hs_color': 'color',
        'temperature': 'targetTemperature',
        'current_temperature': 'temperatureReading',
        'hvac_mode': 'mode',
        'fan_mode': 'fanSpeed',
        'preset_mode': 'mode',
        'percentage': 'fanSpeed',
        'humidity': 'targetHumidity',
        'current_humidity': 'humidity',
        'oscillating': 'mode',
        'current_position': 'percentage',
        'volume_level': 'volume',
        'is_volume_muted': 'muteState',
        'fan_speed': 'suction',
        'activity': 'state',
        'battery_level': 'electricityCapacity',
        'cleaning_mode': 'mode',
    }

    # DuerOS attribute metadata: scale, legalValue
    _dueros_attr_meta = {
        'turnOnState': {'scale': '', 'legalValue': '(ON, OFF)'},
        'brightness': {'scale': '%', 'legalValue': '[0, 100]'},
        'color': {'scale': '', 'legalValue': 'OBJECT'},
        'colorTemperatureInKelvin': {'scale': 'K', 'legalValue': '[1000, 10000]'},
        'temperatureReading': {'scale': 'CELSIUS', 'legalValue': 'DOUBLE'},
        'targetTemperature': {'scale': 'CELSIUS', 'legalValue': 'DOUBLE'},
        'mode': {'scale': '', 'legalValue': 'STRING'},
        'fanSpeed': {'scale': '', 'legalValue': '[0, 10]'},
        'humidity': {'scale': '%', 'legalValue': '[0.0, 100.0]'},
        'targetHumidity': {'scale': '%', 'legalValue': '[0.0, 100.0]'},
        'percentage': {'scale': '%', 'legalValue': '[0, 100]'},
        'pm2.5': {'scale': 'ug/m3', 'legalValue': '[0.0, 1000.0]'},
        'PM10': {'scale': 'ug/m3', 'legalValue': 'DOUBLE'},
        'co2': {'scale': 'ppm', 'legalValue': 'INTEGER'},
        'formaldehyde': {'scale': 'mg/m3', 'legalValue': 'DOUBLE'},
        'airQuality': {'scale': '', 'legalValue': 'STRING'},
        'state': {'scale': '', 'legalValue': '(CLEANING, CHARGING, RECHARGING, SLEEPING, STAND_BY, REPORT_ERROR, SHUT_DOWN, REMOTE_CONTROLING, PAUSED)'},
        'location': {'scale': '', 'legalValue': 'STRING'},
        'volume': {'scale': '', 'legalValue': '[0, 100]'},
        'muteState': {'scale': '', 'legalValue': 'BOOLEAN'},
        'suction': {'scale': '', 'legalValue': '(STANDARD, STRONG)'},
        'electricityCapacity': {'scale': '%', 'legalValue': '[0, 100]'},
        'waterLevel': {'scale': '', 'legalValue': '(LOW, MIDDLE, HIGH)'},
        'connectivity': {'scale': '', 'legalValue': '(UNREACHABLE, REACHABLE)'},
        'illumination': {'scale': 'lx', 'legalValue': 'DOUBLE'},
        'warmthLevel': {'scale': '', 'legalValue': '(LOW, MIDDLE, HIGH)'},
    }

    device_action_map_h2p = {
        'turn_on': 'turnOn',
        'turn_off': 'turnOff',
        'timing_turn_on': 'timingTurnOn',
        'timing_turn_off': 'timingTurnOff',
        'increase_brightness': 'incrementBrightnessPercentage',
        'decrease_brightness': 'decrementBrightnessPercentage',
        'set_brightness': 'setBrightnessPercentage',
        'set_color': 'setColor',
        'increase_temperature': 'incrementTemperature',
        'decrease_temperature': 'decrementTemperature',
        'set_temperature': 'setTemperature',
        'increase_speed': 'incrementFanSpeed',
        'decrease_speed': 'decrementFanSpeed',
        'set_percentage': 'setFanSpeed',
        'pause': 'pause',
        'set_humidity': 'setHumidity',
        'set_hvac_mode': 'setMode',
        'set_oscillate': 'setMode',
        'unset_oscillate': 'unSetMode',
        'volume_up': 'incrementVolume',
        'volume_down': 'decrementVolume',
        'volume_set': 'setVolume',
        'volume_mute': 'setVolumeMute',
        'media_pause': 'pause',
        'media_play': 'continue',
        'query_temperature': 'getTemperatureReading',
        'query_humidity': 'getHumidity',
        'query_targettemperature': 'getTargetTemperature',
        'query_targethumidity': 'getTargetHumidity',
        'query_state': 'getState',
        'query_electricitycapacity': 'getElectricityCapacity',
        'query_waterlevel': 'getWaterLevel',
        'query_pm25': 'getAirPM25',
        'query_pm10': 'getAirPM10',
        'query_co2': 'getCO2Quantity',
        'query_aqi': 'getAirQualityIndex',
        'query_location': 'getLocation',
        'query_turnonstate': 'getTurnOnState',
        'query_fanspeed': 'getFanSpeed',
        'set_colortemperature': 'setColorTemperature',
        'increment_colortemperature': 'incrementColorTemperature',
        'decrement_colortemperature': 'decrementColorTemperature',
        'set_mode': 'setMode',
        'set_gear': 'setGear',
        'set_suction': 'setSuction',
        'set_waterlevel': 'setWaterLevel',
        'activate': 'turnOn',
    }

    _device_type_alias = {
        "LIGHT": "电灯",
        "AIR_CONDITION": "空调",
        "CURTAIN": "窗帘",
        "CURT_SIMP": "窗纱",
        "SOCKET": "插座",
        "SWITCH": "开关",
        "FRIDGE": "冰箱",
        "WATER_PURIFIER": "净水器",
        "HUMIDIFIER": "加湿器",
        "DEHUMIDIFIER": "除湿器",
        "INDUCTION_COOKER": "电磁炉",
        "AIR_PURIFIER": "空气净化器",
        "WASHING_MACHINE": "洗衣机",
        "WATER_HEATER": "热水器",
        "GAS_STOVE": "燃气灶",
        "TV_SET": "电视机",
        "OTT_BOX": "网络盒子",
        "RANGE_HOOD": "油烟机",
        "FAN": "电风扇",
        "PROJECTOR": "投影仪",
        "SWEEPING_ROBOT": "扫地机器人",
        "KETTLE": "热水壶",
        "MICROWAVE_OVEN": "微波炉",
        "PRESSURE_COOKER": "压力锅",
        "RICE_COOKER": "电饭煲",
        "HIGH_SPEED_BLENDER": "破壁机",
        "AIR_FRESHER": "新风机",
        "CLOTHES_RACK": "晾衣架",
        "OVEN": "烤箱设备",
        "STEAM_OVEN": "蒸烤箱",
        "STEAM_BOX": "蒸箱",
        "HEATER": "电暖器",
        "WINDOW_OPENER": "开窗器",
        "WEBCAM": "摄像头",
        "CAMERA": "相机",
        "ROBOT": "机器人",
        "PRINTER": "打印机",
        "WATER_COOLER": "饮水机",
        "FISH_TANK": "鱼缸",
        "WATERING_DEVICE": "浇花器",
        "SET_TOP_BOX": "机顶盒",
        "AROMATHERAPY_MACHINE": "香薰机",
        "DVD": "DVD",
        "SHOE_CABINET": "鞋柜",
        "WALKING_MACHINE": "走步机",
        "TREADMILL": "跑步机",
        "BED": "床",
        "YUBA": "浴霸",
        "SHOWER": "花洒",
        "BATHTUB": "浴缸",
        "DISINFECTION_CABINET": "消毒柜",
        "DISHWASHER": "洗碗机",
        "SOFA": "沙发品类",
        "DOOR_BELL": "门铃",
        "ELEVATOR": "电梯",
        "WEIGHT_SCALE": "体重秤",
        "BODY_FAT_SCALE": "体脂秤",
        "WALL_HUNG_GAS_BOILER": "壁挂炉",
        "SCENE_TRIGGER": "场景",
        "ACTIVITY_TRIGGER": "活动场景",
    }

    # DuerOS AIR_CONDITION mode → HA hvac_mode
    dueros_to_ha_climate_mode = _DUEROS_TO_HA_CLIMATE_MODE

    # HA hvac_mode → DuerOS AIR_CONDITION mode
    ha_to_dueros_climate_mode = {
        'off': 'OFF',
        'cool': 'COOL',
        'heat': 'HEAT',
        'auto': 'AUTO',
        'heat_cool': 'AUTO',
        'fan_only': 'FAN',
        'dry': 'DEHUMIDIFICATION',
    }

    device_type_map_h2p = {
        'climate': 'AIR_CONDITION',
        'fan': 'FAN',
        'light': 'LIGHT',
        'media_player': 'TV_SET',
        'switch': 'SWITCH',
        'sensor': 'SENSOR',
        'cover': 'CURTAIN',
        'vacuum': 'SWEEPING_ROBOT',
        'humidifier': 'HUMIDIFIER',
        'scene': 'SCENE_TRIGGER',
    }

    _service_map_p2h = {
        'fan': {
            'IncrementFanSpeedRequest': lambda state, attributes, payload: (['fan'], ['set_percentage'], [{'percentage': _clamp_percent((_as_float(state.attributes.get('percentage')) or 0) + 20)}]),
            'DecrementFanSpeedRequest': lambda state, attributes, payload: (['fan'], ['set_percentage'], [{'percentage': _clamp_percent((_as_float(state.attributes.get('percentage')) or 0) - 20)}]),
            'SetFanSpeedRequest': lambda state, attributes, payload: (['fan'], ['set_percentage'], [{'percentage': _resolve_fan_percentage(payload)}]),
            'SetModeRequest': lambda state, attributes, payload: _resolve_fan_mode(state, payload),
            'UnsetModeRequest': lambda state, attributes, payload: (['fan'], ['oscillate'], [{'oscillating': False}]),
        },
        'YUBA': {
            'TurnOnRequest': 'turn_on',
            'TurnOffRequest': 'turn_off',
            'IncrementFanSpeedRequest': lambda state, attributes, payload: (['fan'], ['set_percentage'], [{'percentage': _clamp_percent((_as_float(state.attributes.get('percentage')) or 0) + 20)}]),
            'DecrementFanSpeedRequest': lambda state, attributes, payload: (['fan'], ['set_percentage'], [{'percentage': _clamp_percent((_as_float(state.attributes.get('percentage')) or 0) - 20)}]),
            'SetFanSpeedRequest': lambda state, attributes, payload: (['fan'], ['set_percentage'], [{'percentage': _resolve_fan_percentage(payload)}]),
            'SetModeRequest': lambda state, attributes, payload: (['fan'], ['set_preset_mode'], [{'preset_mode': _resolve_mode_value(payload).lower()}]),
            'UnsetModeRequest': lambda state, attributes, payload: (['fan'], ['set_preset_mode'], [{'preset_mode': 'off'}]),
            'SetGearRequest': lambda state, attributes, payload: (['fan'], ['set_preset_mode'], [{'preset_mode': {'MIN': 'low', 'LOW': 'low', 'MIDDLE_LOW': 'medium', 'MIDDLE': 'medium', 'MIDDLE_HIGH': 'medium', 'HIGH': 'high', 'MAX': 'high', 'AUTO': 'auto', 'RANDOM': 'auto'}.get(str(_payload_value(payload, 'gear', '')).upper(), str(_payload_value(payload, 'gear', '')).lower())}]),
        },
        'climate': {
            'TurnOnRequest': lambda state, attributes, payload: (['climate'], ['set_hvac_mode'], [{'hvac_mode': _resolve_climate_turn_on(state)}]),
            'TurnOffRequest': lambda state, attributes, payload: (['climate'], ['set_hvac_mode'], [{'hvac_mode': 'off'}]),
            'SetTemperatureRequest': lambda state, attributes, payload: (['climate'], ['set_temperature'], [{'temperature': _resolve_target_temperature(state, payload)}]),
            'IncrementTemperatureRequest': lambda state, attributes, payload: (['climate'], ['set_temperature'], [{'temperature': _resolve_temperature_delta(state, payload, 1)}]),
            'DecrementTemperatureRequest': lambda state, attributes, payload: (['climate'], ['set_temperature'], [{'temperature': _resolve_temperature_delta(state, payload, -1)}]),
            'SetModeRequest': lambda state, attributes, payload: _climate_set_mode(payload),
            'IncrementFanSpeedRequest': lambda state, attributes, payload: (['climate'], ['set_fan_mode'], [{'fan_mode': _resolve_climate_fan_mode_step(state, 1)}]),
            'DecrementFanSpeedRequest': lambda state, attributes, payload: (['climate'], ['set_fan_mode'], [{'fan_mode': _resolve_climate_fan_mode_step(state, -1)}]),
            'SetFanSpeedRequest': lambda state, attributes, payload: (['climate'], ['set_fan_mode'], [{'fan_mode': _resolve_climate_fan_mode(state, payload)}]),
        },
        'media_player': {
            'TurnOnRequest': 'turn_on',
            'TurnOffRequest': 'turn_off',
            'PauseRequest': 'media_pause',
            'ContinueRequest': 'media_play',
            'IncrementVolumeRequest': 'volume_up',
            'DecrementVolumeRequest': 'volume_down',
            'SetVolumeRequest': lambda state, attributes, payload: (['media_player'], ['volume_set'], [{'volume_level': _resolve_volume_level(payload)}]),
            'SetVolumeMuteRequest': lambda state, attributes, payload: (['media_player'], ['volume_mute'], [{'is_volume_muted': _resolve_volume_mute(payload)}]),
        },
        'humidifier': {
            'TurnOnRequest': 'turn_on',
            'TurnOffRequest': 'turn_off',
            'SetHumidityRequest': lambda state, attributes, payload: (['humidifier'], ['set_humidity'], [{'humidity': _resolve_humidity(state, payload)}]),
        },
        'cover': {
            'TurnOnRequest': 'open_cover',
            'TurnOffRequest': 'close_cover',
            'PauseRequest': 'stop_cover',
        },
        'vacuum': {
            'TurnOnRequest': 'start',
            'TurnOffRequest': 'return_to_base',
            'PauseRequest': 'pause',
            'ContinueRequest': 'start',
            'SetSuctionRequest': lambda state, attributes, payload: (['vacuum'], ['set_fan_speed'], [{'fan_speed': _resolve_vacuum_fan_speed(state, payload)}]),
            'SetModeRequest': lambda state, attributes, payload: _vacuum_set_mode(payload),
            'ChargeRequest': lambda state, attributes, payload: (['vacuum'], ['return_to_base'], [{}]),
            'DischargeRequest': lambda state, attributes, payload: (['vacuum'], ['start'], [{}]),
            'SetWaterLevelRequest': lambda state, attributes, payload: _vacuum_water_level(payload),
            'SetDirectionRequest': lambda state, attributes, payload: _vacuum_direction(payload),
        },
        'switch': {
            'TurnOnRequest': 'turn_on',
            'TurnOffRequest': 'turn_off',
        },
        'light': {
            'TurnOnRequest': 'turn_on',
            'TurnOffRequest': 'turn_off',
            'SetBrightnessPercentageRequest': lambda state, attributes, payload: (['light'], ['turn_on'], [{'brightness_pct': _resolve_brightness_set(state, payload)}]),
            'IncrementBrightnessPercentageRequest': lambda state, attributes, payload: (['light'], ['turn_on'], [{'brightness_pct': _resolve_brightness_delta(state, payload, 1)}]),
            'DecrementBrightnessPercentageRequest': lambda state, attributes, payload: (['light'], ['turn_on'], [{'brightness_pct': _resolve_brightness_delta(state, payload, -1)}]),
            'SetColorRequest': lambda state, attributes, payload: (['light'], ['turn_on'], [_resolve_color(state, payload)]),
            **_kelvin_light_handlers(),
        },
        'scene': {
            'TurnOnRequest': 'turn_on',
            'TurnOffRequest': 'turn_on',
        },
    }

    # 查询请求 → (读取属性名, 响应构造方式)
    _query_map = {
        'TurnOnState': {'format': 'attribute', 'read': 'turnOnState', 'emit': 'turnOnState'},
        'TemperatureReading': {'format': 'temperature', 'read': 'temperatureReading', 'emit': 'temperatureReading'},
        'TargetTemperature': {'format': 'target_temperature', 'read': 'targetTemperature', 'emit': 'targetTemperature'},
        'Humidity': {'format': 'attribute', 'read': 'humidity', 'emit': 'humidity'},
        'TargetHumidity': {'format': 'attribute', 'read': 'targetHumidity', 'emit': 'humidity'},
        'FanSpeed': {'format': 'attribute', 'read': 'fanSpeed', 'emit': 'fanSpeed'},
        'AirPM25': {'format': 'value_scale', 'read': 'pm2.5', 'emit': 'PM25'},
        'AirPM10': {'format': 'value_scale', 'read': 'PM10', 'emit': 'PM10'},
        'CO2Quantity': {'format': 'value_only', 'read': 'co2', 'emit': 'ppm'},
        'AirQualityIndex': {'format': 'aqi', 'read': 'airQuality', 'emit': 'AQI'},
        'State': {'format': 'attribute', 'read': 'state', 'emit': 'state'},
        'Location': {'format': 'location', 'read': 'location', 'emit': 'location'},
        'ElectricityCapacity': {'format': 'value_scale', 'read': 'electricityCapacity', 'emit': 'electricityCapacity'},
        'WaterLevel': {'format': 'attribute', 'read': 'waterLevel', 'emit': 'waterLevel'},
    }


def _resolve_climate_turn_on(state):
    """空调“打开”：优先恢复当前模式，缺省取设备支持的模式。"""
    if state is None:
        return 'auto'
    if state.state not in ('off', 'unavailable', 'unknown'):
        return state.state
    modes = list(state.attributes.get('hvac_modes') or [])
    for candidate in ('auto', 'cool', 'heat', 'fan_only', 'dry'):
        if candidate in modes:
            return candidate
    return 'auto'


def _climate_set_mode(payload):
    """SetModeRequest → 服务调用；未知模式报错而非静默回退。"""
    mode = str(_payload_value(payload, 'mode', '')).upper()
    if not mode:
        raise ValueError('mode is required')
    if mode == 'SLEEP':
        return (['climate'], ['set_preset_mode'], [{'preset_mode': 'sleep'}])
    mapped = _DUEROS_TO_HA_CLIMATE_MODE.get(mode)
    if not mapped:
        raise ValueError(f'unsupported climate mode: {mode}')
    return (['climate'], ['set_hvac_mode'], [{'hvac_mode': mapped}])


def _resolve_climate_fan_mode_step(state, step):
    """空调风速加/减档，按设备档位列表循环。"""
    fan_modes = list((state.attributes.get('fan_modes') if state else None) or [])
    if not fan_modes:
        raise ValueError('device does not support fan_mode')
    current = (state.attributes.get('fan_mode') if state else None) or ''
    index = fan_modes.index(current) if current in fan_modes else 0
    return fan_modes[(index + step) % len(fan_modes)]


class VoiceControlDueros(PlatformParameter, VoiceControlProcessor):
    def __init__(self, hass, mode, entry, bot_id):
        self._hass = hass
        self._mode = mode
        self._bot_id = bot_id
        self._store = Store(hass, STORAGE_VERSION, f"{INTEGRATION}_open_uids_{entry.entry_id}")
        self._uid_by_token = {}
        self._report_warn_at = 0.0
        self._report_fail_warn_at = 0.0
        self._name_mismatch_warn_at = 0.0
        self._report_at = {}
        self._report_timers = {}
        self._sync_at = 0.0
        self._timers = HavcsTimerManager(hass, entry.entry_id)
        self.vcdm = VoiceControlDeviceManager(entry, DOMAIN, self.device_action_map_h2p, self.device_attribute_map_h2p, self._service_map_p2h, self.device_type_map_h2p, self._device_type_alias)

    # ------------------------------------------------------------------ 持久化

    def _open_uids(self):
        return self._hass.data.setdefault(INTEGRATION, {}).setdefault(DATA_HAVCS_OPEN_UIDS, {}).setdefault(DOMAIN, set())

    async def async_load_open_uids(self):
        """恢复已授权用户的 openUid（HA 重启后仍可主动上报）。"""
        self._uid_by_token = await self._store.async_load() or {}
        self._open_uids().update(self._uid_by_token.values())
        if self._uid_by_token:
            _LOGGER.info("[%s] restored %d openUid(s)", LOGGER_NAME, len(self._uid_by_token))

    async def async_load_timers(self):
        await self._timers.async_load()

    @callback
    def _token_id(self, auth):
        return getattr(auth, 'id', None)

    async def _async_bind_open_uid(self, auth, open_uid):
        token_id = self._token_id(auth)
        if not open_uid or not token_id:
            return
        changed = self._uid_by_token.get(token_id) != open_uid
        self._uid_by_token[token_id] = open_uid
        self._open_uids().add(open_uid)
        if changed:
            await self._store.async_save(self._uid_by_token)

    async def _async_unbind_open_uid(self, auth):
        token_id = self._token_id(auth)
        open_uid = self._uid_by_token.pop(token_id, None)
        if open_uid:
            self._open_uids().discard(open_uid)
            await self._store.async_save(self._uid_by_token)
            _LOGGER.info("[%s] removed openUid for unbound user", LOGGER_NAME)

    # ------------------------------------------------------------- 请求入口

    def _errorResult(self, errorCode, messsage=None, payload=None):
        error_code_map = {
            'INVALIDATE_CONTROL_ORDER': 'UnexpectedInformationReceivedError',
            'INVALIDATE_PARAMS': 'UnexpectedInformationReceivedError',
            'SERVICE_ERROR': 'DriverInternalError',
            'DEVICE_NOT_SUPPORT_FUNCTION': 'UnsupportedOperationError',
            'DEVICE_IS_NOT_EXIST': 'UnsupportedTargetError',
            'IOT_DEVICE_OFFLINE': 'TargetOfflineError',
            'ACCESS_TOKEN_INVALIDATE': 'InvalidAccessTokenError',
            'ACCESS_TOKEN_EXPIRED': 'ExpiredAccessTokenError',
        }
        name = error_code_map.get(errorCode, 'DriverInternalError')
        result = {'errorCode': name}
        # UnexpectedInformationReceivedError 必须携带 faultingParameter
        if name == 'UnexpectedInformationReceivedError':
            payload = payload or {'faultingParameter': {'name': errorCode, 'value': messsage or ''}}
        if payload:
            result['payload'] = payload
        return result

    def _prase_command(self, command, arg):
        header = command.get('header') or {}
        payload = command.get('payload') or {}

        if arg == 'device_id':
            return (payload.get('appliance') or {}).get('applianceId', '')
        elif arg == 'action':
            return header.get('name', '')
        elif arg == 'user_uid':
            return payload.get('openUid', '')
        elif arg == 'header':
            return header
        else:
            return command.get(arg)

    def _decrypt_device_id(self, device_id) -> None:
        return decrypt_device_id(device_id)

    async def handleRequest(self, data, auth=False, request_from="http", token_expired=False):
        """Handle request from Xiaodu."""
        _LOGGER.info("[%s] Handle Request:\n%s", LOGGER_NAME, mask_tokens(json.dumps(data, ensure_ascii=False)))

        header = self._prase_command(data, 'header')
        action = self._prase_command(data, 'action')
        p_user_id = self._prase_command(data, 'user_uid')
        result = {}

        if auth:
            namespace = header['namespace']
            if namespace == 'DuerOS.ConnectedHome.Discovery':
                action = 'DiscoverAppliancesResponse'
                err_result, discovery_devices, entity_ids, zone_map = self.process_discovery_command(request_from)
                if len(discovery_devices) > _MAX_APPLIANCES:
                    _LOGGER.warning("[%s] discovery: %d appliances exceed the %d limit, truncated",
                                    LOGGER_NAME, len(discovery_devices), _MAX_APPLIANCES)
                    discovery_devices = discovery_devices[:_MAX_APPLIANCES]
                # 分组只能引用已发现的设备，否则小度侧同步失败
                kept_ids = {item['applianceId'] for item in discovery_devices}
                groups = []
                for zone_name, appliance_ids in zone_map.items():
                    group_ids = [appliance_id for appliance_id in appliance_ids[:50] if appliance_id in kept_ids]
                    if not group_ids:
                        continue
                    groups.append({
                        'groupName': zone_name[:20],
                        'applianceIds': group_ids,
                        'groupNotes': '',
                        'additionalGroupDetails': {},
                    })
                    if len(groups) >= _MAX_GROUPS:
                        break
                if len(zone_map) > _MAX_GROUPS:
                    _LOGGER.warning("[%s] discovery: %d groups exceed the %d limit, truncated",
                                    LOGGER_NAME, len(zone_map), _MAX_GROUPS)
                result = {'discoveredAppliances': discovery_devices, 'discoveredGroups': groups}
                await self._async_bind_open_uid(auth, p_user_id)

            elif namespace == 'DuerOS.ConnectedHome.Control':
                err_result, properties = await self.process_control_command(data)
                result = err_result if err_result else {'attributes': properties}
                action = action.replace('Request', 'Confirmation')

            elif namespace == 'DuerOS.ConnectedHome.Query':
                if action == 'ReportStateRequest':
                    result, action, report_detail = self._handle_report_state(data)
                    _LOGGER.debug("[%s] ReportState → %s (applianceId=%s, attribute=%s, 命中设备=%s)",
                                  LOGGER_NAME, action, report_detail['appliance_id'],
                                  report_detail['attribute'], report_detail['resolved'])
                    if report_detail['stale']:
                        # applianceId 已失效：保持 ReportStateResponse 名称以避免小度停止同步，
                        # 同时触发一次设备同步让平台重新发现设备
                        _LOGGER.warning(
                            "[%s] ReportStateRequest 的 applianceId 已失效（按明文或新 ID 均未命中设备），"
                            "已请求平台重新同步设备；如反复出现请在小度 APP 重新发现设备", LOGGER_NAME)
                        self._schedule_device_sync()
                else:
                    err_result, properties = self.process_query_command(data)
                    result = err_result if err_result else properties
                    action = action.replace('Request', 'Response')

            elif namespace == 'DuerOS.ConnectedHome.UnbindBot':
                action = 'UnbindBotResponse'
                result = {}
                await self._async_unbind_open_uid(auth)

            else:
                result = self._errorResult('INVALIDATE_CONTROL_ORDER')
        else:
            result = self._errorResult('ACCESS_TOKEN_EXPIRED' if token_expired else 'ACCESS_TOKEN_INVALIDATE')
            # ReportStateRequest 鉴权失败时仍保持 ReportStateResponse 名：
            # 否则小度认为未收到回执并返回 21096 停止该设备同步
            if action == 'ReportStateRequest':
                _LOGGER.warning(
                    "[%s] ReportStateRequest access token 校验失败（expired=%s），"
                    "回执将按 ReportStateResponse 返回空属性；请在小度 APP 重新绑定技能",
                    LOGGER_NAME, token_expired)
                return {
                    'header': {
                        'namespace': 'DuerOS.ConnectedHome.Query',
                        'name': 'ReportStateResponse',
                        'messageId': header.get('messageId'),
                        'payloadVersion': header.get('payloadVersion', '1'),
                    },
                    'payload': {'attributes': []},
                }

        response_header = {
            'namespace': header.get('namespace'),
            'name': action,
            'messageId': header.get('messageId'),
            'payloadVersion': header.get('payloadVersion', '1'),
        }
        if 'errorCode' in result:
            # 文档规定所有错误消息的 namespace 均为 DuerOS.ConnectedHome.Control
            response_header['namespace'] = 'DuerOS.ConnectedHome.Control'
            response_header['name'] = result['errorCode']
            result = result.get('payload') or {}

        response = {'header': response_header, 'payload': result}
        _LOGGER.info("[%s] Response: %s", LOGGER_NAME, response)
        return response

    # ------------------------------------------------------------- 发现设备

    def _discovery_process_device_type(self, raw_device_type):
        return raw_device_type if raw_device_type in self._device_type_alias else self.device_type_map_h2p.get(raw_device_type)

    def _discovery_process_actions(self, device_properties, raw_actions):
        actions = []
        for device_property in device_properties:
            attr_key = device_property.get('attribute')
            if attr_key is None:
                continue
            action = self.device_action_map_h2p.get('query_' + attr_key)
            if action:
                actions.append(action)
        for raw_action in raw_actions:
            action = self.device_action_map_h2p.get(raw_action)
            if action:
                actions.append(action)
        return list(set(actions))

    def _discovery_process_propertites(self, device_properties, device=None) -> None:
        return self._build_device_attributes(device_properties, device=device)

    def _control_process_propertites(self, device_properties, action, device=None) -> None:
        return self._build_device_attributes(device_properties, device=device)

    def _discovery_process_device_info(self, encrypted_id, device_type, device_name, properties, actions, device=None):
        reachable = True
        if device is not None:
            for entity_id in device.entity_id:
                state = self._hass.states.get(entity_id)
                if state is None or state.state in ('unavailable', 'unknown'):
                    reachable = False
                    break
        attributes = list(properties)
        if not any(item.get('name') == 'connectivity' for item in attributes) and len(attributes) < MAX_ATTRIBUTES:
            meta = self._dueros_attr_meta['connectivity']
            attributes.append(self._attribute_item('connectivity', 'REACHABLE' if reachable else 'UNREACHABLE', meta))
        return {
            'applianceId': encrypted_id,
            'friendlyName': device_name,
            'friendlyDescription': f"{device_name}（HomeAssistant 接入，云云对接）"[:128],
            'additionalApplianceDetails': {},
            'applianceTypes': [device_type],
            'isReachable': reachable,
            'manufacturerName': 'HomeAssistant',
            'modelName': 'HomeAssistant',
            'version': '1.0',
            'actions': actions,
            'attributes': attributes[:MAX_ATTRIBUTES],
        }

    # ------------------------------------------------------------- 属性读取

    @staticmethod
    def _attribute_item(name, value, meta):
        return {
            'name': name,
            'value': value,
            'scale': meta['scale'],
            'timestampOfSample': int(time.time()),
            'uncertaintyInMilliseconds': 1000,
            'legalValue': meta['legalValue'],
        }

    @staticmethod
    def _aqi_level(number):
        if number <= 50:
            return '优'
        if number <= 100:
            return '良'
        if number <= 150:
            return '轻度污染'
        if number <= 200:
            return '中度污染'
        if number <= 300:
            return '重度污染'
        return '严重污染'

    def _read_property(self, device_property) -> tuple:
        attr_key = device_property.get('attribute')
        name = self.device_attribute_map_h2p.get(attr_key)
        if not name:
            return None, None, None
        entity_id = device_property.get('entity_id')
        state = self._hass.states.get(entity_id)
        return name, self._read_attribute_value(name, state, attr_key, entity_id), entity_id

    def _build_device_attributes(self, device_properties, requested=None, limit=MAX_ATTRIBUTES,
                                 device=None) -> list:
        """构造属性列表；requested 可含未声明属性（ReportState 要求必含被请求项）。"""
        names = set()
        if isinstance(requested, str):
            names = {requested}
        elif isinstance(requested, (list, tuple, set)):
            names = {requested_name for requested_name in requested if requested_name}

        items = []
        seen = set()
        for device_property in device_properties:
            attr_name, value, _ = self._read_property(device_property)
            if not attr_name or attr_name in seen or value is None:
                continue
            seen.add(attr_name)
            items.append(self._scalar_attribute(attr_name, value))

        # 设备级属性（location）随发现与回读一并上报
        for attr_name in ('location',):
            if attr_name in seen or device is None:
                continue
            value = self._read_device_attribute(attr_name, device)
            if value is not None:
                seen.add(attr_name)
                items.append(self._scalar_attribute(attr_name, value))

        for attr_name in names - seen:
            value = self._read_device_attribute(attr_name, device) if device is not None else None
            if value is not None:
                items.append(self._scalar_attribute(attr_name, value))

        if names:
            items.sort(key=lambda item: item['name'] not in names)
        return items[:limit] or [self._scalar_attribute('turnOnState', 'OFF')]

    def _scalar_attribute(self, name, value) -> dict:
        meta = self._dueros_attr_meta.get(name, {'scale': '', 'legalValue': 'STRING'})
        return self._attribute_item(name, value, meta)

    def _read_device_attribute(self, dueros_name, device):
        """读取不依赖实体状态的设备级属性（location/waterLevel）。"""
        if dueros_name == 'location':
            return self._resolve_location(device)
        if dueros_name == 'waterLevel':
            for device_property in device.properties:
                if device_property.get('attribute') != 'waterlevel':
                    continue
                state = self._hass.states.get(device_property.get('entity_id'))
                return _resolve_water_level_value(state)
        return None

    def _resolve_location(self, device):
        zone = device.attributes.get(ATTR_DEVICE_ZONE) if device else None
        if not zone or zone == '未指定':
            return None
        return _LOCATION_ENUM.get(zone, zone)

    def _read_attribute_value(self, dueros_name, state, attr_key, entity_id):
        """读取 HA 状态并转换为 DuerOS 属性值。"""
        if state is None:
            return None
        domain = entity_id.split('.', 1)[0] if entity_id else ''
        if dueros_name == 'turnOnState':
            return _resolve_turn_on_state(entity_id, state)
        if dueros_name == 'connectivity':
            return 'REACHABLE' if state.state not in ('unavailable', 'unknown') else 'UNREACHABLE'
        if dueros_name == 'brightness':
            value = _as_float(state.attributes.get('brightness'))
            return round(value / 255 * 100, 1) if value is not None else None
        if dueros_name == 'colorTemperatureInKelvin':
            value = _as_float(state.attributes.get('color_temp_kelvin'))
            return int(value) if value is not None else None
        if dueros_name == 'color':
            hs_color = state.attributes.get('hs_color')
            if hs_color:
                brightness = _as_float(state.attributes.get('brightness')) or 255
                return {'hue': float(hs_color[0]), 'saturation': float(hs_color[1]) / 100, 'brightness': brightness / 255}
            return None
        if dueros_name == 'temperatureReading':
            value = _as_float(state.attributes.get('current_temperature')) if domain == 'climate' else _as_float(state.state)
            return round(value, 1) if value is not None else None
        if dueros_name == 'targetTemperature':
            value = _as_float(state.attributes.get('temperature')) if domain == 'climate' else _as_float(state.state)
            return round(value, 1) if value is not None else None
        if dueros_name == 'mode':
            if domain == 'climate':
                return self.ha_to_dueros_climate_mode.get(state.attributes.get('hvac_mode') or state.state, 'AUTO')
            if domain == 'fan':
                swing = _mode_to_swing(state.attributes.get('oscillating'))
                return swing or state.attributes.get('preset_mode')
            if domain == 'humidifier':
                return state.attributes.get('preset_mode') or state.attributes.get('mode') or None
            if domain == 'vacuum':
                return state.attributes.get('cleaning_mode') or None
            return None
        if dueros_name == 'fanSpeed':
            if domain == 'climate':
                return state.attributes.get('fan_mode') or None
            if domain == 'fan':
                percentage = _as_float(state.attributes.get('percentage'))
                return int(round(_clamp(percentage, 0, 100) / 10)) if percentage is not None else None
            return None
        if dueros_name == 'humidity':
            value = _as_float(state.attributes.get('current_humidity'))
            if value is None and domain == 'sensor':
                value = _as_float(state.state)
            return round(value, 1) if value is not None else None
        if dueros_name == 'targetHumidity':
            value = _as_float(state.attributes.get('humidity'))
            return round(value, 1) if value is not None else None
        if dueros_name in ('pm2.5', 'PM10', 'co2'):
            value = _as_float(state.state)
            return round(value, 1) if value is not None else None
        if dueros_name == 'formaldehyde':
            return _as_float(state.state)
        if dueros_name == 'airQuality':
            number = _as_float(state.state)
            return self._aqi_level(number) if number is not None else state.state
        if dueros_name == 'illumination':
            return _as_float(state.state)
        if dueros_name == 'percentage':
            value = _as_float(state.attributes.get('current_position'))
            return int(value) if value is not None else None
        if dueros_name == 'state':
            return _resolve_work_state(state.state)
        if dueros_name == 'volume':
            value = _as_float(state.attributes.get('volume_level'))
            return int(round(value * 100)) if value is not None else None
        if dueros_name == 'muteState':
            value = state.attributes.get('is_volume_muted')
            return value if isinstance(value, bool) else None
        if dueros_name == 'suction':
            return _resolve_suction(state.attributes.get('fan_speed'))
        if dueros_name == 'electricityCapacity':
            return _resolve_electricity_capacity(state)
        if dueros_name == 'waterLevel':
            return _resolve_water_level_value(state)
        if dueros_name == 'warmthLevel':
            preset = str(state.attributes.get('preset_mode') or '')
            if not preset:
                return None
            preset_map = {'low': 'LOW', 'quiet': 'LOW', 'min': 'LOW', 'medium': 'MIDDLE', 'mid': 'MIDDLE',
                          'middle': 'MIDDLE', 'high': 'HIGH', 'turbo': 'HIGH', 'max': 'HIGH', 'auto': 'AUTO'}
            return preset_map.get(preset.lower(), preset.upper())
        return None

    # ------------------------------------------------------------- 查询处理

    def _query_process_propertites(self, device_properties, action, device=None) -> None:
        action_key = action.replace('Request', '').replace('Get', '')
        spec = self._query_map.get(action_key)
        if not spec:
            return {}

        if spec['format'] == 'location':
            zone = device.attributes.get(ATTR_DEVICE_ZONE) if device else None
            if zone and zone != '未指定':
                value = _LOCATION_ENUM.get(zone, zone)
                return {'attributes': [self._attribute_item('location', value, self._dueros_attr_meta['location'])]}
            return {}

        read_name = spec['read']
        for device_property in device_properties:
            name, value, entity_id = self._read_property(device_property)
            if name != read_name or value is None:
                continue
            meta = self._dueros_attr_meta.get(name, {'scale': '', 'legalValue': 'STRING'})
            fmt = spec['format']
            if fmt == 'attribute':
                return {'attributes': [self._attribute_item(spec['emit'], value, meta)]}
            if fmt == 'value_scale':
                return {spec['emit']: {'value': value, 'scale': meta['scale']}}
            if fmt == 'value_only':
                return {spec['emit']: {'value': value}}
            if fmt in ('temperature', 'target_temperature'):
                return self._temperature_payload(spec, value, entity_id)
            if fmt == 'aqi':
                state = self._hass.states.get(entity_id)
                number = _as_float(state.state) if state else None
                if number is not None:
                    return {'AQI': {'value': number}, 'level': {'value': self._aqi_level(number)}}
                return {'level': {'value': str(value)}}
        return {}

    def _temperature_payload(self, spec, value, entity_id) -> dict:
        number = _as_float(value)
        if number is None:
            return {}
        mode = 'AUTO'
        if entity_id and entity_id.startswith('climate.'):
            state = self._hass.states.get(entity_id)
            if state:
                mode = self.ha_to_dueros_climate_mode.get(state.attributes.get('hvac_mode') or state.state, 'AUTO')
        payload = {
            spec['emit']: {'value': number, 'scale': 'CELSIUS'},
            'mode': {'value': mode},
            'applianceResponseTimestamp': dt_util.utcnow().isoformat(),
        }
        if spec['format'] == 'target_temperature':
            payload['temperatureMode'] = {'value': mode, 'friendlyName': mode}
        return payload

    def _handle_report_state(self, data) -> tuple:
        """处理 ReportStateRequest，返回 (payload, 响应名, 诊断信息)。

        无论成败都保留 ReportStateResponse 作为响应名：若被替换为错误名，
        小度会返回 21096 并停止该设备的属性同步。
        """
        raw_id = self._prase_command(data, 'device_id')
        device = self._get_device(raw_id)
        attribute = ((data.get('payload') or {}).get('appliance') or {}).get('attributeName')
        detail = {'appliance_id': raw_id, 'attribute': attribute,
                  'resolved': device.device_id if device else None,
                  'stale': device is None}
        if device is None:
            return {'attributes': []}, 'ReportStateResponse', detail
        return ({'attributes': self._build_device_attributes(device.properties, requested=attribute, device=device)},
                'ReportStateResponse', detail)

    def _get_device(self, raw_id):
        """按 applianceId 取设备：先按配置的加密方式解出，再回退明文 ID。"""
        if not raw_id:
            return None
        device = self.vcdm.get(self._decrypt_device_id(raw_id))
        if device is None:
            device = self.vcdm.get(raw_id)
        if device is None:
            self.vcdm.all(self._hass)
            device = self.vcdm.get(self._decrypt_device_id(raw_id)) or self.vcdm.get(raw_id)
        return device

    def _schedule_device_sync(self) -> None:
        """限频触发一次设备同步（applianceId 失效时让平台重新发现）。"""
        now = time.monotonic()
        if now - self._sync_at < _SYNC_MIN_INTERVAL:
            return
        self._sync_at = now
        self._hass.async_create_task(self.sync_devices(self._hass))

    # ------------------------------------------------------------- 控制处理

    async def _async_pre_process_action(self, device, entity_ids, action, payload) -> tuple | None:
        if action in ('TimingTurnOnRequest', 'TimingTurnOffRequest'):
            return await self._async_process_timing(device, entity_ids, action == 'TimingTurnOnRequest', payload)
        return None

    async def _async_process_timing(self, device, entity_ids, turn_on, payload) -> tuple:
        timestamp = _as_float(_payload_value(payload, 'timestamp'))
        if not timestamp or timestamp <= time.time():
            return self._errorResult('INVALIDATE_PARAMS'), None
        action_name = 'timing_turn_on' if turn_on else 'timing_turn_off'
        scheduled = []
        for entity_id in entity_ids:
            commands = self._build_timing_commands(entity_id, turn_on)
            if not commands:
                continue
            await self._timers.async_schedule(entity_id, commands, int(timestamp), slot='on' if turn_on else 'off')
            scheduled.append(entity_id)
        if not scheduled:
            return self._errorResult('DEVICE_NOT_SUPPORT_FUNCTION'), None
        _LOGGER.info("[%s] scheduled timing %s for %s", LOGGER_NAME, action_name, scheduled)
        properties = self._control_process_propertites(device.properties, 'TimingTurnOnRequest' if turn_on else 'TimingTurnOffRequest')
        return None, properties

    def _build_timing_commands(self, entity_id, turn_on) -> list:
        """构造定时执行的服务调用。"""
        domain = entity_id.split('.', 1)[0]
        if domain == 'climate':
            state = self._hass.states.get(entity_id)
            mode = 'off'
            if turn_on:
                modes = list((state.attributes.get('hvac_modes') if state else None) or [])
                current = state.state if state else None
                mode = current if current and current not in ('off', 'unavailable', 'unknown') else ('auto' if 'auto' in modes else (modes[0] if modes else 'auto'))
            return [('climate', 'set_hvac_mode', {'entity_id': entity_id, 'hvac_mode': mode})]
        services = _TIMING_SERVICE_MAP.get(domain)
        if not services:
            return []
        service = services[0] if turn_on else services[1]
        if not service:
            return []
        return [(domain, service, {'entity_id': entity_id})]

    # ------------------------------------------------------------- 主动上报

    def _log_delivery_failure(self, action, err):
        """上报未送达：限频告警，避免网络中断期间刷屏。"""
        now = time.monotonic()
        if now - self._report_fail_warn_at > _REPORT_WARN_INTERVAL:
            self._report_fail_warn_at = now
            _LOGGER.warning("[%s] %s delivery failed: %s", LOGGER_NAME, action, err)
        else:
            _LOGGER.debug("[%s] %s delivery failed: %s", LOGGER_NAME, action, err)

    def _log_upstream_result(self, action, status, result):
        """记录上报结果；无法解析为 JSON 的响应（网关/WAF 页面等）按小时限频告警。

        21096 由 report_device 单独处理（限频并附排查线索），此处跳过避免重复。
        """
        if isinstance(result, dict):
            if result.get('status', 0) != 0:
                if result.get('status') == _SYNC_STATUS_NAME_MISMATCH:
                    return
                _LOGGER.warning("[%s] %s rejected (HTTP %s): %s", LOGGER_NAME, action, status, result)
            else:
                _LOGGER.debug("[%s] %s response: %s", LOGGER_NAME, action, result)
            return
        message = "[%s] %s returned unparseable response (HTTP %s): %s"
        now = time.monotonic()
        if status >= 400 or now - self._report_warn_at > _REPORT_WARN_INTERVAL:
            self._report_warn_at = now
            _LOGGER.warning(message, LOGGER_NAME, action, status, _shorten(result))
        else:
            _LOGGER.debug(message, LOGGER_NAME, action, status, _shorten(result))

    async def report_device(self, hass, device_id, changed_attribute='turnOnState') -> bool:
        """Send ChangeReportRequest to Xiaodu when device state changes.

        小度对同一属性有 60 秒内仅同步 1 次的限制（status=21207），
        因此窗口内的变更合并为一次延迟补发，避免被拒后状态长期不同步。
        """
        open_uids = self._open_uids()
        if not open_uids:
            _LOGGER.debug("[%s] no openUids cached, skip report", LOGGER_NAME)
            return False

        device = self.vcdm.get(device_id)
        if changed_attribute not in self._device_reportable_attributes(device):
            _LOGGER.debug("[%s] %s is not reportable for %s", LOGGER_NAME, changed_attribute, device_id)
            return False

        dueros_attr = self.ha_attribute_to_dueros.get(changed_attribute, 'turnOnState')
        if changed_attribute == 'state' and device is not None and any(entity_id.startswith('vacuum.') for entity_id in device.entity_id):
            dueros_attr = 'state'

        key = (device_id, dueros_attr)
        now = time.time()
        elapsed = now - self._report_at.get(key, 0.0)
        if elapsed < _CHANGE_REPORT_INTERVAL:
            self._schedule_report_flush(hass, device_id, changed_attribute, dueros_attr, _CHANGE_REPORT_INTERVAL - elapsed)
            return False

        self._report_at[key] = now
        attempted = False
        for p_user_id in open_uids:
            report = {
                "header": {
                    "namespace": "DuerOS.ConnectedHome.Control",
                    "name": "ChangeReportRequest",
                    "messageId": str(uuid.uuid4()),
                    "payloadVersion": "1",
                },
                "payload": {
                    "botId": self._bot_id,
                    "openUid": p_user_id,
                    "appliance": {
                        "applianceId": encrypt_device_id(device_id),
                        "attributeName": dueros_attr,
                    },
                },
            }
            try:
                session = async_get_clientsession(hass)
                status, result = await _async_post_report(session, DUEROS_CHANGE_REPORT_URL, report)
            except _ReportDeliveryError as err:
                self._log_delivery_failure('ChangeReport', err)
                # 状态变化未送达：排程一次补发，避免上报长期缺失
                self._schedule_report_flush(hass, device_id, changed_attribute, dueros_attr, _CHANGE_REPORT_INTERVAL)
                continue
            except Exception:
                _LOGGER.error("[%s] failed to send ChangeReport: %s", LOGGER_NAME, traceback.format_exc())
                continue
            attempted = True
            self._log_upstream_result('ChangeReport', status, result)
            if isinstance(result, dict) and result.get('status') == _SYNC_STATUS_RATE_LIMITED:
                # 小度已按 60 秒窗口计数，重排一次补发而不是丢弃
                self._schedule_report_flush(hass, device_id, changed_attribute, dueros_attr, _CHANGE_REPORT_INTERVAL)
            elif isinstance(result, dict) and result.get('status') == _SYNC_STATUS_NAME_MISMATCH:
                # 小度回查属性未收到合法 ReportStateResponse；常见根因：回查携带的
                # access token 失效、applianceId 已失效、或处理超时/异常。限频告警。
                now_m = time.monotonic()
                if now_m - self._name_mismatch_warn_at > _REPORT_WARN_INTERVAL:
                    self._name_mismatch_warn_at = now_m
                    _LOGGER.warning(
                        "[%s] ChangeReport rejected (21096): 服务端未收到 ReportStateResponse。"
                        "请检索日志中 ReportState 相关记录确认：token 校验失败 / applianceId 失效 / handle fail"
                        "（device_id=%s, attribute=%s）",
                        LOGGER_NAME, device_id, dueros_attr)
                else:
                    _LOGGER.debug(
                        "[%s] ChangeReport rejected (21096): device_id=%s, attribute=%s",
                        LOGGER_NAME, device_id, dueros_attr)
        return attempted

    def _schedule_report_flush(self, hass, device_id, changed_attribute, dueros_attr, delay) -> None:
        """窗口内合并变更：到期后补发一次最新状态。"""
        key = (device_id, dueros_attr)
        if key in self._report_timers:
            return
        loop = hass.loop
        handle = loop.call_later(delay + 1.0, self._async_flush_report, hass, device_id, changed_attribute, dueros_attr)
        self._report_timers[key] = handle

    @callback
    def _async_flush_report(self, hass, device_id, changed_attribute, dueros_attr) -> None:
        self._report_timers.pop((device_id, dueros_attr), None)
        hass.async_create_task(self.report_device(hass, device_id, changed_attribute))

    def _device_reportable_attributes(self, device) -> set:
        """计算设备可上报的 HA 属性名（与发现/上报的映射保持一致）。"""
        if device is None:
            return set()
        reportable = set()
        for entity_id in device.entity_id:
            domain = entity_id.split('.', 1)[0]
            if domain == 'sensor':
                continue
            if domain == 'vacuum':
                reportable |= {'state', 'fan_speed', 'battery_level'}
                continue
            for device_property in device.properties:
                if device_property.get('entity_id') != entity_id:
                    continue
                dueros_name = self.device_attribute_map_h2p.get(device_property.get('attribute'))
                for key, name in self.ha_attribute_to_dueros.items():
                    if name == dueros_name:
                        reportable.add(key)
        return reportable

    async def sync_devices(self, hass):
        """Notify Xiaodu to re-discover devices via devicesync API."""
        open_uids = self._open_uids()
        if not open_uids:
            _LOGGER.debug("[%s] no openUids cached, skip device sync", LOGGER_NAME)
            return

        uid_list = list(open_uids)
        for i in range(0, len(uid_list), 5):
            batch = uid_list[i:i + 5]
            payload = {
                "botId": self._bot_id,
                "logId": str(uuid.uuid4()),
                "openUids": batch,
            }
            try:
                session = async_get_clientsession(hass)
                status, result = await _async_post_report(session, DUEROS_DEVICE_SYNC_URL, payload)
            except _ReportDeliveryError as err:
                self._log_delivery_failure('devicesync', err)
                continue
            except Exception:
                _LOGGER.error("[%s] failed to sync devices: %s", LOGGER_NAME, traceback.format_exc())
                continue
            self._log_upstream_result('devicesync', status, result)
