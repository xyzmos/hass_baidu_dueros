"""HASS Baidu DuerOS - Home Assistant voice control for Xiaodu/DuerOS HTTP self-built skill."""

import logging

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.entity_registry import EVENT_ENTITY_REGISTRY_UPDATED
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
from homeassistant.helpers.network import get_url
from homeassistant.helpers.typing import ConfigType

from . import util as havcs_util
from .const import (
    ATTR_DEVICE_ENTITY_ID,
    CONF_BOT_ID,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_ENTITY_KEY,
    CONF_HA_URL,
    DATA_HAVCS_CONFIG,
    DATA_HAVCS_HANDLER,
    DATA_HAVCS_OPEN_UIDS,
    HAVCS_SUPPORTED_DOMAINS,
    INTEGRATION,
)
from .dueros import VoiceControlDueros
from .http import HavcsHttpManager

_LOGGER = logging.getLogger(__name__)

DOMAIN = INTEGRATION

SERVICE_RELOAD = 'reload'
SERVICE_DEBUG_DISCOVERY = 'debug_discovery'

# 这些字段变化会影响暴露设备/分组，需要重建缓存
_REGISTRY_REBUILD_FIELDS = {'options', 'area_id', 'device_id', 'hidden_by', 'disabled_by'}
# registry 变更防抖：批量修改曝光时只同步一次
_REGISTRY_DEBOUNCE_SECONDS = 2.0


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the HASS Baidu DuerOS component from configuration.yaml (legacy)."""
    hass.data.setdefault(DOMAIN, {})
    return bool(hass.config_entries.async_entries(DOMAIN))


async def _async_update_listener(hass: HomeAssistant, config_entry) -> None:
    """Reload the entry when options change (e.g. entity_key)."""
    await hass.config_entries.async_reload(config_entry.entry_id)


async def async_setup_entry(hass, config_entry):
    """Set up HASS Baidu DuerOS from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(DATA_HAVCS_HANDLER, {})
    hass.data[DOMAIN].setdefault(DATA_HAVCS_OPEN_UIDS, {})

    data = config_entry.data
    client_id = data[CONF_CLIENT_ID]
    client_secret = data[CONF_CLIENT_SECRET]
    ha_url = data.get(CONF_HA_URL) or get_url(hass, allow_external=False)
    bot_id = data[CONF_BOT_ID]
    entity_key = config_entry.options.get(CONF_ENTITY_KEY) or data.get(CONF_ENTITY_KEY, "")

    havcs_util.ENTITY_KEY = entity_key

    conf = {
        'http': {
            'clients': {client_id: client_secret},
            'ha_url': ha_url,
        },
        'platform': ['dueros'],
    }
    hass.data[DOMAIN][DATA_HAVCS_CONFIG] = conf

    config_entry.async_on_unload(config_entry.add_update_listener(_async_update_listener))

    http_manager = HavcsHttpManager(hass, ha_url, client_id, client_secret)
    http_manager.register_views()

    @callback
    def _on_state_changed(event: Event) -> None:
        """状态变化回调：显式传入 hass（Event 对象不携带 hass）。"""
        _async_on_state_changed(hass, event)

    async def start_integration(event: Event = None):
        """Start the integration after HA is ready."""
        handler = VoiceControlDueros(hass, ['handler'], config_entry, bot_id)
        hass.data[DOMAIN][DATA_HAVCS_HANDLER]['dueros'] = handler

        devices = handler.vcdm.all(hass, init_flag=True)
        _LOGGER.info("[init] loaded %d exposed devices", len(devices))
        await handler.async_load_open_uids()
        await handler.async_load_timers()

        remove_state_listener = async_track_state_change_event(
            hass, _get_exposed_entity_ids(hass), _on_state_changed
        )
        config_entry.async_on_unload(remove_state_listener)

        def _resubscribe_state_listener():
            """刷新状态监听实体列表（新增/移除曝光实体后调用）。"""
            nonlocal remove_state_listener
            remove_state_listener()
            remove_state_listener = async_track_state_change_event(
                hass, _get_exposed_entity_ids(hass), _on_state_changed
            )
            config_entry.async_on_unload(remove_state_listener)

        debounce = {'cancel': None}

        async def _apply_registry_change():
            debounce['cancel'] = None
            handler.vcdm.all(hass, init_flag=True)
            _resubscribe_state_listener()
            await handler.sync_devices(hass)

        @callback
        def _on_entity_registry_changed(event: Event) -> None:
            if not _should_handle_registry_event(event):
                return
            if debounce['cancel'] is not None:
                debounce['cancel']()
            debounce['cancel'] = async_call_later(
                hass, _REGISTRY_DEBOUNCE_SECONDS, lambda now: hass.async_create_task(_apply_registry_change())
            )

        remove_entity_reg_listener = hass.bus.async_listen(
            EVENT_ENTITY_REGISTRY_UPDATED, _on_entity_registry_changed
        )
        config_entry.async_on_unload(remove_entity_reg_listener)
        config_entry.async_on_unload(lambda: debounce['cancel'] and debounce['cancel']())

        async def async_handler_service(service):
            if service.service == SERVICE_RELOAD:
                handler.vcdm.all(hass, init_flag=True)
                _LOGGER.info("[service] reloaded device info")
                _resubscribe_state_listener()
                await handler.sync_devices(hass)

            elif service.service == SERVICE_DEBUG_DISCOVERY:
                err_result, discovery_devices, entity_ids, zone_map = handler.process_discovery_command("service_call")
                _LOGGER.info("[service] discovery result: %s", discovery_devices)

        if hass.services.has_service(DOMAIN, SERVICE_RELOAD):
            hass.services.async_remove(DOMAIN, SERVICE_RELOAD)
            hass.services.async_remove(DOMAIN, SERVICE_DEBUG_DISCOVERY)
        hass.services.async_register(DOMAIN, SERVICE_RELOAD, async_handler_service)
        hass.services.async_register(DOMAIN, SERVICE_DEBUG_DISCOVERY, async_handler_service)

        await handler.sync_devices(hass)

    if not hass.is_running:
        unsub = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, start_integration)
        config_entry.async_on_unload(unsub)
    else:
        config_entry.async_create_background_task(hass, start_integration(None), f"{DOMAIN} start")

    _LOGGER.info("[init] %s initialization finished", DOMAIN)
    return True


async def async_unload_entry(hass, config_entry):
    """Unload a config entry."""
    for service in (SERVICE_RELOAD, SERVICE_DEBUG_DISCOVERY):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)

    hass.data[DOMAIN].pop(DATA_HAVCS_CONFIG, None)
    hass.data[DOMAIN].pop(DATA_HAVCS_HANDLER, None)
    hass.data[DOMAIN].pop(DATA_HAVCS_OPEN_UIDS, None)

    if not hass.data[DOMAIN]:
        hass.data.pop(DOMAIN)

    return True


def _get_exposed_entity_ids(hass: HomeAssistant) -> list:
    """Get entity ids that are exposed via conversation."""
    from .helper import VoiceControlDeviceManager

    exposed = VoiceControlDeviceManager.get_exposed_entities(hass)
    entity_ids = []
    for device_attrs in exposed.values():
        entity_ids.extend(device_attrs.get(ATTR_DEVICE_ENTITY_ID, []))
    return entity_ids


def _should_handle_registry_event(event: Event) -> bool:
    """判断实体注册表事件是否需要重建缓存并同步。"""
    action = event.data.get("action")
    if action not in ("create", "remove", "update"):
        return False

    entity_id = event.data.get("entity_id", "")
    domain = entity_id.split('.', 1)[0] if '.' in entity_id else ""
    if domain not in HAVCS_SUPPORTED_DOMAINS:
        return False

    if action == "update":
        changes = set((event.data.get("changes") or {}).keys())
        if not changes & _REGISTRY_REBUILD_FIELDS:
            return False

    return True


@callback
def _async_on_state_changed(hass: HomeAssistant, event: Event) -> None:
    """Handle entity state changes for active reporting to Xiaodu."""
    entity_id = event.data.get("entity_id")
    old_state = event.data.get("old_state")
    new_state = event.data.get("new_state")

    if old_state is None or new_state is None:
        return

    if old_state.state == new_state.state and old_state.attributes == new_state.attributes:
        return

    handler = hass.data.get(DOMAIN, {}).get(DATA_HAVCS_HANDLER, {}).get('dueros')
    if handler is None:
        return

    device_ids = handler.vcdm.get_entity_related_device_ids(hass, entity_id)
    if not device_ids:
        return

    changed_attributes = _diff_attributes(old_state, new_state)
    if not changed_attributes:
        return

    for device_id in device_ids:
        for attribute in changed_attributes:
            if attribute not in handler.ha_attribute_to_dueros:
                continue
            hass.async_create_task(handler.report_device(hass, device_id, attribute))


def _diff_attributes(old_state, new_state) -> list:
    """Return changed HA attribute keys (含被删除的属性)。"""
    if old_state.state != new_state.state:
        return ['state']
    changed = []
    for key, new_val in new_state.attributes.items():
        if old_state.attributes.get(key) != new_val:
            changed.append(key)
    for key in old_state.attributes:
        if key not in new_state.attributes:
            changed.append(key)
    return changed
