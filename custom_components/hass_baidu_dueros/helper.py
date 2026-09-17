import asyncio
import logging
import traceback

import voluptuous as vol

from homeassistant.components.homeassistant.exposed_entities import async_should_expose
from homeassistant.core import Context, HomeAssistant, callback
from homeassistant.exceptions import ServiceNotFound
from homeassistant.helpers import area_registry, device_registry, entity_registry
from homeassistant.helpers.event import async_track_state_change_event

from .const import (
    ASSISTANT_CONVERSATION,
    ATTR_DEVICE_ACTIONS,
    ATTR_DEVICE_ENTITY_ID,
    ATTR_DEVICE_ID,
    ATTR_DEVICE_NAME,
    ATTR_DEVICE_PROPERTIES,
    ATTR_DEVICE_TYPE,
    ATTR_DEVICE_ZONE,
    DEVICE_ID_PREFIX,
    HAVCS_SUPPORTED_DOMAINS,
)
from .device import VoiceControllDevice
from .util import encrypt_device_id

_LOGGER = logging.getLogger(__name__)
LOGGER_NAME = 'helper'

DOMAIN_SERVICE_WITH_ENTITY_ID = ['climate']
CONTEXT = Context()

# 设备的调色/调色温能力判定所用色模式
_COLOR_MODES = ('hs', 'rgb', 'rgbw', 'rgbww', 'xy')
# 水量/水箱控制实体的名称关键字（扫地机拖地水量）
_WATER_WORDS = ('水量', '水箱', 'water', 'mop')
_NAME_FORBIDDEN = str.maketrans({c: None for c in "!@#$%^&*()_+=~`[]{}\\|;:'\"<>,.?/-"
                                              "（）【】《》「」『』、，。；：！？·…—～＃＠￥％＆＊＋＝｜"})


class VoiceControlProcessor:
    def _discovery_process_propertites(self, device_properties, device=None) -> None:
        raise NotImplementedError()

    def _discovery_process_actions(self, device_properties, raw_actions) -> None:
        raise NotImplementedError()

    def _discovery_process_device_type(self, raw_device_type) -> None:
        raise NotImplementedError()

    def _discovery_process_device_info(self, device_id, device_type, device_name, zone, properties, actions, device=None) -> None:
        raise NotImplementedError()

    def _control_process_propertites(self, device_properties, action, device=None) -> None:
        raise NotImplementedError()

    def _query_process_propertites(self, device_properties, action, device=None) -> None:
        raise NotImplementedError()

    def _prase_action_p2h(self, action) -> None:
        for k, v in self.vcdm.device_action_map_h2p.items():
            if v == action:
                return k
        i = 0
        service = ''
        for c in action.split('Request')[0]:
            service += (('_' if i else '') + c.lower()) if c.isupper() else c
            i += 1
        return service

    def _decrypt_device_id(self, device_id) -> None:
        raise NotImplementedError()

    def _prase_command(self, command, arg) -> None:
        raise NotImplementedError()

    def _errorResult(self, errorCode, messsage=None) -> None:
        raise NotImplementedError()

    async def _async_pre_process_action(self, device, entity_ids, action, payload) -> tuple | None:
        """子类拦截特殊动作（如定时），返回非 None 表示已处理完毕。"""
        return None

    vcdm = None
    _hass = None
    _service_map_p2h = None

    def process_discovery_command(self, request_from) -> tuple:
        devices = []
        entity_ids = []
        zone_map = {}  # {zone_name: [applianceId, ...]}
        for vc_device in self.vcdm.all(self._hass):
            device_id, raw_device_type, device_name, zone, device_properties, raw_actions = self.vcdm.get_device_attrs(vc_device.attributes)
            properties = self._discovery_process_propertites(device_properties, vc_device)
            actions = self._discovery_process_actions(device_properties, raw_actions)
            device_type = self._discovery_process_device_type(raw_device_type)
            if None in (device_type, device_name) or [] in (properties, actions):
                _LOGGER.debug("[%s] discovery: incomplete info for %s (type=%s, name=%s)", LOGGER_NAME, device_id, device_type, device_name)
            else:
                encrypted_id = encrypt_device_id(device_id)
                devices.append(self._discovery_process_device_info(encrypted_id, device_type, device_name, properties, actions, vc_device))
                entity_ids.append(device_id)
                if zone and zone != '未指定':
                    zone_map.setdefault(zone, []).append(encrypted_id)
        return None, devices, entity_ids, zone_map

    async def process_control_command(self, command) -> tuple:
        device_id = self._prase_command(command, 'device_id')
        device_id = self._decrypt_device_id(device_id)
        device = self.vcdm.get(device_id)
        if device_id is None or device is None:
            return self._errorResult('DEVICE_IS_NOT_EXIST'), None
        entity_ids = device.entity_id
        action = self._prase_command(command, 'action')
        payload = self._prase_command(command, 'payload')
        _LOGGER.debug("[%s] control: device_id=%s, entity_ids=%s, action=%s", LOGGER_NAME, device_id, entity_ids, action)

        pre_result = await self._async_pre_process_action(device, entity_ids, action, payload)
        if pre_result is not None:
            return pre_result

        success_task = []
        error_code = None
        device_type = device.attributes.get(ATTR_DEVICE_TYPE)

        for entity_id in entity_ids:
            domain = entity_id[:entity_id.find('.')]
            data = {"entity_id": entity_id}
            domain_list = [domain]
            data_list = [data]
            service_list = ['']

            service_domain = 'YUBA' if (device_type == 'YUBA' and 'YUBA' in self._service_map_p2h) else domain

            if action in self._service_map_p2h.get(service_domain, []):
                translation = self._service_map_p2h[service_domain][action]
                if callable(translation):
                    state = self._hass.states.get(entity_id)
                    try:
                        domain_list, service_list, data_list = translation(state, device.raw_attributes, payload)
                    except (vol.Invalid, ValueError, KeyError, TypeError) as ex:
                        error_code = error_code or 'INVALIDATE_PARAMS'
                        _LOGGER.error("[%s] invalid params for %s of %s: %s", LOGGER_NAME, action, entity_id, ex)
                        continue
                    for i, d in enumerate(data_list):
                        if 'entity_id' not in d and (domain_list[i] in DOMAIN_SERVICE_WITH_ENTITY_ID or entity_id.startswith(domain_list[i] + '.')):
                            d.update(data)
                else:
                    service_list[0] = translation
            else:
                service_list[0] = self._prase_action_p2h(action)

            for i in range(len(domain_list)):
                try:
                    await self._hass.services.async_call(domain_list[i], service_list[i], data_list[i], blocking=True, context=CONTEXT)
                    success_task.append({entity_id: [domain_list[i], service_list[i], data_list[i]]})
                except (vol.Invalid, ValueError, KeyError) as ex:
                    error_code = error_code or 'INVALIDATE_PARAMS'
                    _LOGGER.error("[%s] invalid service data %s.%s: %s", LOGGER_NAME, domain_list[i], service_list[i], ex)
                except ServiceNotFound:
                    error_code = error_code or 'DEVICE_NOT_SUPPORT_FUNCTION'
                    _LOGGER.error("[%s] service not found: %s.%s", LOGGER_NAME, domain_list[i], service_list[i])
                except Exception:  # noqa: BLE001
                    error_code = error_code or 'SERVICE_ERROR'
                    _LOGGER.error("[%s] failed to call service: %s", LOGGER_NAME, traceback.format_exc())

        if not success_task:
            return self._errorResult(error_code or 'IOT_DEVICE_OFFLINE'), None

        await self._async_wait_state_change(entity_ids)
        device_properties = self.vcdm.get(device_id).properties
        properties = self._control_process_propertites(device_properties, action, device)
        return None, properties

    async def _async_wait_state_change(self, entity_ids, timeout=1.0):
        """等待设备状态生效（事件驱动，超时兜底）。"""
        if not entity_ids:
            return
        done = asyncio.Event()

        @callback
        def _changed(event):
            done.set()

        unsub = async_track_state_change_event(self._hass, entity_ids, _changed)
        try:
            async with asyncio.timeout(timeout):
                await done.wait()
        except TimeoutError:
            _LOGGER.debug("[%s] state of %s not settled in %ss", LOGGER_NAME, entity_ids, timeout)
        finally:
            unsub()

    def process_query_command(self, command) -> tuple:
        device_id = self._prase_command(command, 'device_id')
        device_id = self._decrypt_device_id(device_id)
        device = self.vcdm.get(device_id) if device_id is not None else None
        if device is None:
            return self._errorResult('DEVICE_IS_NOT_EXIST'), None
        action = self._prase_command(command, 'action')
        properties = self._query_process_propertites(device.properties, action, device)
        return (None, properties) if properties else (self._errorResult('DEVICE_NOT_SUPPORT_FUNCTION'), None)


class VoiceControlDeviceManager:

    def __init__(self, entry, platform, device_action_map_h2p, device_attribute_map_h2p, service_map_p2h, device_type_map_h2p, device_type_alias, device_name_constraints={}, zone_constraints=[]):
        self._entry = entry
        self._platform = platform
        self.device_action_map_h2p = device_action_map_h2p
        self.device_attribute_map_h2p = device_attribute_map_h2p
        self._service_map_p2h = service_map_p2h
        self.device_type_map_h2p = device_type_map_h2p
        self._device_type_alias = device_type_alias
        self._device_name_constraints = device_name_constraints
        self._zone_constraints = zone_constraints
        self._devices_cache = {}
        self._places = ["门口", "客厅", "卧室", "客房", "主卧", "次卧", "书房", "餐厅", "厨房", "洗手间", "浴室", "阳台",
                        "宠物房", "老人房", "儿童房", "婴儿房", "保姆房", "玄关", "一楼", "二楼", "三楼", "四楼", "楼梯", "走廊",
                        "过道", "楼上", "楼下", "影音室", "娱乐室", "工作间", "杂物间", "衣帽间", "吧台", "花园", "温室", "车库", "休息室", "办公室", "起居室"]

    @staticmethod
    def get_exposed_entities(hass: HomeAssistant) -> dict:
        """Build device entries from entities exposed to the voice assistant."""
        exposed_items = {}

        for state in hass.states.async_all():
            entity_id = state.entity_id
            domain = entity_id[:entity_id.find('.')]

            if domain not in HAVCS_SUPPORTED_DOMAINS:
                continue

            if not async_should_expose(hass, ASSISTANT_CONVERSATION, entity_id):
                continue

            object_id = entity_id.split('.', 1)[1]
            device_id = f"{DEVICE_ID_PREFIX}_{domain}_{object_id}"
            exposed_items[device_id] = {
                ATTR_DEVICE_ENTITY_ID: [entity_id],
            }

        return exposed_items

    def all(self, hass: HomeAssistant = None, init_flag: bool = False) -> list:
        if not self._devices_cache or init_flag:
            self._devices_cache.clear()

            exposed_items = self.get_exposed_entities(hass)

            for device_id, device_attributes in exposed_items.items():
                if isinstance(device_attributes.get(ATTR_DEVICE_ENTITY_ID), str):
                    device_attributes[ATTR_DEVICE_ENTITY_ID] = [device_attributes.get(ATTR_DEVICE_ENTITY_ID)]
                self._devices_cache.update(self.get(device_id, hass, device_attributes))

        return list(self._devices_cache.values())

    def get(self, device_id: str, hass: HomeAssistant = None, raw_attributes: dict = None) -> dict:
        if raw_attributes is None:
            return self._devices_cache.get(device_id)

        device_name = None
        device_type = None
        zone = None
        entity_ids = self.get_device_related_entities(hass, raw_attributes)
        actions = []
        properties = []

        for entity_id in entity_ids:
            if device_name is None:
                device_name = self.get_device_name(hass, entity_id, self._device_name_constraints)
            if device_type is None:
                device_type = self.get_device_type(hass, entity_id, device_name, self.device_type_map_h2p)
            if zone is None:
                zone = self.get_device_zone(hass, entity_id, self._places, self._zone_constraints)
            properties += self.get_device_properties(hass, entity_id)
            actions += self.get_device_actions(hass, entity_id, device_type)

        actions = list(set(actions))

        attributes = {
            ATTR_DEVICE_ID: device_id,
            ATTR_DEVICE_ENTITY_ID: entity_ids,
            ATTR_DEVICE_TYPE: device_type,
            ATTR_DEVICE_NAME: device_name,
            ATTR_DEVICE_ZONE: zone,
            ATTR_DEVICE_PROPERTIES: properties,
            ATTR_DEVICE_ACTIONS: actions,
        }
        device = VoiceControllDevice(hass, self._entry, attributes, raw_attributes)
        return {device_id: device}

    def get_entity_related_device_ids(self, hass, entity_id):
        ids = []
        for vc_device in self.all(hass):
            if entity_id in vc_device.entity_id:
                ids.append(vc_device.device_id)
        return ids

    async def async_reregister_devices(self, hass=None):
        devreg = device_registry.async_get(hass)
        devreg.async_clear_config_entry(self._entry.entry_id)
        for device in self._devices_cache.values():
            await device.async_update_device_registry()

    def get_device_attrs(self, device_attributes) -> list:
        return (
            device_attributes.get(ATTR_DEVICE_ID),
            device_attributes.get(ATTR_DEVICE_TYPE),
            device_attributes.get(ATTR_DEVICE_NAME),
            device_attributes.get(ATTR_DEVICE_ZONE),
            device_attributes.get(ATTR_DEVICE_PROPERTIES),
            device_attributes.get(ATTR_DEVICE_ACTIONS),
        )

    def get_device_related_entities(self, hass, raw_attributes: dict, device_type: str = None) -> list:
        entity_ids = []
        for entity_id in raw_attributes.get(ATTR_DEVICE_ENTITY_ID, []):
            if entity_id.startswith('group.'):
                for entity_in_group_id in hass.states.get(entity_id).attributes.get(ATTR_DEVICE_ENTITY_ID):
                    if device_type is None or entity_in_group_id.startswith(device_type + '.'):
                        entity_ids.append(entity_in_group_id)
            else:
                entity_ids.append(entity_id)
        return entity_ids

    def get_device_type(self, hass, entity_id, device_name, domain_map=None) -> str:
        """解析设备类型：先按实体域映射，再按名称别名（取最长匹配）。

        名称别名必须在域映射之后判断，否则 vacuum 会被别名 ROBOT（SWEEPING_ROBOT 的子串）截胡。
        """
        domain = entity_id[:entity_id.find('.')] if '.' in entity_id else entity_id
        if domain_map and domain in domain_map:
            return domain

        names = [device_name]
        state = hass.states.get(entity_id)
        if state:
            names.append(state.attributes.get('friendly_name'))
        best = None
        for name in names:
            if not name:
                continue
            for device_type, alias in self._device_type_alias.items():
                if alias in name and (best is None or len(alias) > len(self._device_type_alias[best])):
                    best = device_type
        return best or domain

    def get_device_name(self, hass, entity_id, device_name_constraints=[]) -> str:
        state = hass.states.get(entity_id)
        device_name = state.attributes.get('friendly_name') if state else None
        if device_name_constraints and device_name:
            probably_device_names = []
            for device_name_constraint in device_name_constraints:
                aliases = [device_name_constraint['key']] + device_name_constraint['value']
                aliases.reverse()
                for alias in aliases:
                    if alias in device_name:
                        probably_device_names += [alias]
            return max(probably_device_names) if probably_device_names else None
        return self.clean_device_name(device_name)

    @staticmethod
    def clean_device_name(device_name) -> str:
        """清洗 friendlyName：去除标点与多余空白，并限制在 128 字符内。"""
        if not device_name:
            return device_name
        cleaned = device_name.translate(_NAME_FORBIDDEN)
        cleaned = ' '.join(cleaned.split())
        return cleaned[:128]

    def get_device_zone(self, hass, entity_id, places=[], zone_constraints=[]) -> str:
        zone = '未指定'
        er = entity_registry.async_get(hass)
        entry = er.async_get(entity_id)
        if entry and entry.area_id:
            ar = area_registry.async_get(hass)
            area = ar.async_get_area(entry.area_id)
            if area:
                zone = area.name
        if zone == '未指定' and entry and entry.device_id:
            dr = device_registry.async_get(hass)
            device = dr.async_get(entry.device_id)
            if device and device.area_id:
                ar = area_registry.async_get(hass)
                area = ar.async_get_area(device.area_id)
                if area:
                    zone = area.name
        if zone == '未指定':
            state = hass.states.get(entity_id)
            device_name = state.attributes.get('friendly_name') if state else None
            if device_name:
                for place in places:
                    if device_name.startswith(place):
                        zone = place
                        break
        if zone == '未指定':
            for state in hass.states.async_all():
                group_entity_id = state.entity_id
                if group_entity_id.startswith('group.') and not group_entity_id.startswith('group.all_') and group_entity_id != 'group.default_view':
                    if entity_id in (state.attributes.get(ATTR_DEVICE_ENTITY_ID) or []):
                        for place in places:
                            if place in (state.attributes.get('friendly_name') or ''):
                                zone = place
                                break
        if zone_constraints:
            return zone if zone in zone_constraints else None
        return zone

    def get_device_properties(self, hass, entity_id) -> list:
        properties = []
        if entity_id.startswith('sensor.'):
            state = hass.states.get(entity_id)
            if state is None:
                return []
            unit = state.attributes.get('unit_of_measurement', '')
            friendly_name = state.attributes.get('friendly_name', '')
            if unit == '°C' or unit == '℃' or 'temperature' in entity_id or '温度' in friendly_name:
                attribute = 'temperature'
            elif unit == 'lx' or unit == 'lm' or 'illumination' in entity_id or '光照' in friendly_name:
                attribute = 'illumination'
            elif 'humidity' in entity_id or '湿度' in friendly_name:
                attribute = 'humidity'
            elif 'pm25' in entity_id or 'pm2.5' in friendly_name:
                attribute = 'pm25'
            elif 'pm10' in entity_id or 'pm10' in friendly_name:
                attribute = 'pm10'
            elif 'co2' in entity_id or '二氧化碳' in friendly_name:
                attribute = 'co2'
            elif 'hcho' in entity_id or '甲醛' in friendly_name:
                attribute = 'hcho'
            elif 'aqi' in entity_id or '空气质量' in friendly_name:
                attribute = 'aqi'
            else:
                attribute = None
            if attribute:
                properties = [{'entity_id': entity_id, 'attribute': attribute}]
        elif entity_id.startswith('fan.'):
            state = hass.states.get(entity_id)
            friendly_name = state.attributes.get('friendly_name', '') if state else ''
            if '浴霸' in friendly_name:
                properties = [{'entity_id': entity_id, 'attribute': 'turnonstate'}, {'entity_id': entity_id, 'attribute': 'fanspeed'}, {'entity_id': entity_id, 'attribute': 'mode'}, {'entity_id': entity_id, 'attribute': 'warmthlevel'}]
                if state and state.attributes.get('current_temperature') is not None:
                    properties.append({'entity_id': entity_id, 'attribute': 'temperature'})
                    properties.append({'entity_id': entity_id, 'attribute': 'targettemperature'})
            else:
                properties = [{'entity_id': entity_id, 'attribute': 'turnonstate'}, {'entity_id': entity_id, 'attribute': 'fanspeed'}, {'entity_id': entity_id, 'attribute': 'mode'}]
        elif entity_id.startswith('climate.'):
            properties = [{'entity_id': entity_id, 'attribute': 'turnonstate'}, {'entity_id': entity_id, 'attribute': 'hvac_mode'}, {'entity_id': entity_id, 'attribute': 'targettemperature'}, {'entity_id': entity_id, 'attribute': 'temperature'}]
            state = hass.states.get(entity_id)
            if state and state.attributes.get('fan_modes'):
                properties.append({'entity_id': entity_id, 'attribute': 'fan_mode'})
            if state and state.attributes.get('current_humidity') is not None:
                properties.append({'entity_id': entity_id, 'attribute': 'humidity'})
        elif entity_id.startswith('humidifier.'):
            properties = [{'entity_id': entity_id, 'attribute': 'turnonstate'}, {'entity_id': entity_id, 'attribute': 'targethumidity'}, {'entity_id': entity_id, 'attribute': 'humidity'}]
        elif entity_id.startswith('cover.'):
            properties = [{'entity_id': entity_id, 'attribute': 'turnonstate'}, {'entity_id': entity_id, 'attribute': 'percentage'}]
        elif entity_id.startswith('light.'):
            properties = [{'entity_id': entity_id, 'attribute': 'turnonstate'}]
            state = hass.states.get(entity_id)
            if state:
                supported = state.attributes.get('supported_color_modes') or set()
                if any(m in supported for m in ('brightness', *_COLOR_MODES, 'color_temp')):
                    properties.append({'entity_id': entity_id, 'attribute': 'brightness'})
                if 'color_temp' in supported:
                    properties.append({'entity_id': entity_id, 'attribute': 'color_temperature'})
        elif entity_id.startswith('media_player.'):
            properties = [{'entity_id': entity_id, 'attribute': 'turnonstate'}]
            state = hass.states.get(entity_id)
            if state and state.attributes.get('volume_level') is not None:
                properties.append({'entity_id': entity_id, 'attribute': 'volume'})
        elif entity_id.startswith('vacuum.'):
            properties = [{'entity_id': entity_id, 'attribute': 'turnonstate'}, {'entity_id': entity_id, 'attribute': 'state'}]
            state = hass.states.get(entity_id)
            if state and state.attributes.get('fan_speed'):
                properties.append({'entity_id': entity_id, 'attribute': 'suction'})
            if state and state.attributes.get('battery_level') is not None:
                properties.append({'entity_id': entity_id, 'attribute': 'electricitycapacity'})
            water_attribute = self._get_water_property(hass, entity_id)
            if water_attribute:
                properties.append(water_attribute)
        elif entity_id.startswith('scene.'):
            properties = [{'entity_id': entity_id, 'attribute': 'turnonstate'}]
        else:
            properties = [{'entity_id': entity_id, 'attribute': 'turnonstate'}]
        return properties

    def get_device_actions(self, hass, entity_id, device_type) -> list:
        actions = self._get_default_actions(hass, entity_id, device_type)
        return self._filter_actions_by_capability(hass, entity_id, actions)

    def _get_default_actions(self, hass, entity_id, device_type) -> list:
        if device_type == 'switch':
            actions = ["turn_on", "turn_off", "timing_turn_on", "timing_turn_off", "query_turnonstate"]
        elif device_type == 'light':
            actions = ["turn_on", "turn_off", "timing_turn_on", "timing_turn_off", "query_turnonstate", "set_brightness", "increase_brightness", "decrease_brightness", "set_color", "set_colortemperature", "increment_colortemperature", "decrement_colortemperature"]
        elif device_type == 'climate':
            actions = ["turn_on", "turn_off", "timing_turn_on", "timing_turn_off", "query_turnonstate", "set_temperature", "increase_temperature", "decrease_temperature", "query_targettemperature", "query_temperature", "set_hvac_mode", "set_percentage", "increase_speed", "decrease_speed"]
        elif device_type == 'cover':
            actions = ["turn_on", "turn_off", "timing_turn_on", "timing_turn_off", "query_turnonstate", "pause"]
        elif device_type == 'media_player':
            actions = ["turn_on", "turn_off", "timing_turn_on", "timing_turn_off", "query_turnonstate", "media_pause", "media_play", "volume_up", "volume_down", "volume_set", "volume_mute"]
        elif device_type == 'humidifier':
            actions = ["turn_on", "turn_off", "timing_turn_on", "timing_turn_off", "query_turnonstate", "set_humidity", "query_humidity", "query_targethumidity"]
        elif device_type == 'vacuum':
            actions = ["turn_on", "turn_off", "timing_turn_on", "timing_turn_off", "query_turnonstate", "query_state",
                       "set_suction", "set_mode", "query_electricitycapacity", "set_waterlevel", "query_waterlevel"]
        elif device_type == 'fan':
            actions = ["turn_on", "turn_off", "timing_turn_on", "timing_turn_off", "query_turnonstate", "set_percentage", "increase_speed", "decrease_speed", "set_oscillate", "unset_oscillate", "query_fanspeed"]
        elif device_type == 'YUBA':
            actions = ["turn_on", "turn_off", "timing_turn_on", "timing_turn_off", "query_turnonstate", "set_percentage", "increase_speed", "decrease_speed", "set_mode", "set_gear", "query_fanspeed"]
        elif device_type == 'scene':
            actions = ["turn_on", "query_turnonstate"]
        elif device_type == 'sensor':
            actions = self.get_sensor_actions_from_properties(self.get_device_properties(hass, entity_id))
        elif entity_id.startswith('switch.'):
            actions = ["turn_on", "turn_off", "timing_turn_on", "timing_turn_off", "query_turnonstate"]
        elif entity_id.startswith('light.'):
            actions = ["turn_on", "turn_off", "timing_turn_on", "timing_turn_off", "query_turnonstate", "set_brightness", "increase_brightness", "decrease_brightness", "set_color", "set_colortemperature", "increment_colortemperature", "decrement_colortemperature"]
        elif entity_id.startswith('climate.'):
            actions = ["turn_on", "turn_off", "timing_turn_on", "timing_turn_off", "query_turnonstate", "set_temperature", "increase_temperature", "decrease_temperature", "query_targettemperature", "query_temperature", "set_hvac_mode", "set_percentage", "increase_speed", "decrease_speed"]
        elif entity_id.startswith('cover.'):
            actions = ["turn_on", "turn_off", "timing_turn_on", "timing_turn_off", "query_turnonstate", "pause"]
        elif entity_id.startswith('media_player.'):
            actions = ["turn_on", "turn_off", "timing_turn_on", "timing_turn_off", "query_turnonstate", "media_pause", "media_play", "volume_up", "volume_down", "volume_set", "volume_mute"]
        elif entity_id.startswith('humidifier.'):
            actions = ["turn_on", "turn_off", "timing_turn_on", "timing_turn_off", "query_turnonstate", "set_humidity", "query_humidity", "query_targethumidity"]
        elif entity_id.startswith('vacuum.'):
            actions = ["turn_on", "turn_off", "timing_turn_on", "timing_turn_off", "query_turnonstate", "query_state",
                       "set_suction", "set_mode", "query_electricitycapacity", "set_waterlevel", "query_waterlevel"]
        elif entity_id.startswith('fan.'):
            state = hass.states.get(entity_id)
            friendly_name = state.attributes.get('friendly_name', '') if state else ''
            if '浴霸' in friendly_name:
                actions = ["turn_on", "turn_off", "timing_turn_on", "timing_turn_off", "query_turnonstate", "set_percentage", "increase_speed", "decrease_speed", "set_mode", "set_gear", "query_fanspeed"]
            else:
                actions = ["turn_on", "turn_off", "timing_turn_on", "timing_turn_off", "query_turnonstate", "set_percentage", "increase_speed", "decrease_speed", "set_oscillate", "unset_oscillate", "query_fanspeed"]
        elif entity_id.startswith('scene.'):
            actions = ["turn_on", "query_turnonstate"]
        elif entity_id.startswith('sensor.'):
            actions = self.get_sensor_actions_from_properties(self.get_device_properties(hass, entity_id))
        else:
            actions = ["turn_on", "turn_off", "timing_turn_on", "timing_turn_off", "query_turnonstate"]
        return actions

    def _filter_actions_by_capability(self, hass, entity_id, actions) -> list:
        """按设备实际能力裁剪动作，避免声明无法执行的能力。"""
        state = hass.states.get(entity_id)
        if state is None:
            return actions
        attrs = state.attributes
        removed = set()
        if entity_id.startswith('light.'):
            modes = attrs.get('supported_color_modes') or set()
            if not any(m in modes for m in _COLOR_MODES):
                removed |= {'set_color'}
            if 'color_temp' not in modes:
                removed |= {'set_colortemperature', 'increment_colortemperature', 'decrement_colortemperature'}
            if not modes or modes == {'onoff'}:
                removed |= {'set_brightness', 'increase_brightness', 'decrease_brightness', 'set_color',
                            'set_colortemperature', 'increment_colortemperature', 'decrement_colortemperature'}
        elif entity_id.startswith('media_player.'):
            from homeassistant.components.media_player import MediaPlayerEntityFeature
            features = int(attrs.get('supported_features', 0))
            if not features & MediaPlayerEntityFeature.VOLUME_SET:
                removed |= {'volume_set'}
            if not features & MediaPlayerEntityFeature.VOLUME_STEP:
                removed |= {'volume_up', 'volume_down'}
            if not features & MediaPlayerEntityFeature.VOLUME_MUTE:
                removed |= {'volume_mute'}
            if not features & MediaPlayerEntityFeature.PAUSE:
                removed |= {'media_pause'}
            if not features & MediaPlayerEntityFeature.PLAY:
                removed |= {'media_play'}
        elif entity_id.startswith('cover.'):
            from homeassistant.components.cover import CoverEntityFeature
            if not int(attrs.get('supported_features', 0)) & CoverEntityFeature.STOP:
                removed |= {'pause'}
        elif entity_id.startswith('fan.'):
            from homeassistant.components.fan import FanEntityFeature
            if not int(attrs.get('supported_features', 0)) & FanEntityFeature.OSCILLATE:
                removed |= {'set_oscillate', 'unset_oscillate'}
        elif entity_id.startswith('climate.'):
            if not attrs.get('fan_modes'):
                removed |= {'query_fanspeed'}
        elif entity_id.startswith('vacuum.'):
            from homeassistant.components.vacuum import VacuumEntityFeature
            features = int(attrs.get('supported_features', 0))
            if not attrs.get('fan_speed_list'):
                removed |= {'set_suction'}
            if not features & VacuumEntityFeature.START:
                removed |= {'set_mode'}
            if not any('waterlevel' == p.get('attribute') for p in self.get_device_properties(hass, entity_id)):
                removed |= {'set_waterlevel', 'query_waterlevel'}
        return [a for a in actions if a not in removed]

    def get_sensor_actions_from_properties(self, properties) -> list:
        return ['query_' + device_property.get('attribute') for device_property in properties if device_property.get('attribute')]

    def _get_water_property(self, hass, vacuum_entity_id) -> dict | None:
        """查找与扫地机同区域的水量控制实体（number 域），用于 waterLevel 属性。"""
        key = vacuum_entity_id.split('.', 1)[1]
        for state in hass.states.async_all():
            if not state.entity_id.startswith('number.'):
                continue
            name = str(state.attributes.get('friendly_name') or '')
            if not any(word in name for word in _WATER_WORDS):
                continue
            if key in state.entity_id or key in name:
                return {'entity_id': state.entity_id, 'attribute': 'waterlevel'}
        return None
