"""Built-in scheduling for DuerOS timingTurnOn / timingTurnOff."""

import logging

from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import INTEGRATION

_LOGGER = logging.getLogger(__name__)
LOGGER_NAME = 'timer'

STORAGE_VERSION = 1
_KEY_SEP = '#'


class HavcsTimerManager:
    """Schedule delayed device actions and survive HA restarts.

    任务以 (entity_id, slot) 为键，slot 用于区分开/关方向，避免"5 分钟后开灯"与
    "10 分钟后关灯"互相覆盖；同一 entity+slot 重复设定时以最后一次为准。
    """

    def __init__(self, hass, entry_id):
        self._hass = hass
        self._store = Store(hass, STORAGE_VERSION, f"{INTEGRATION}_timers_{entry_id}")
        self._jobs = {}
        self._unsubs = {}

    async def async_load(self):
        """Restore pending jobs after startup."""
        stored = await self._store.async_load() or {}
        for key, job in stored.items():
            self._jobs[key] = job
            self._schedule(key, job)
        if stored:
            _LOGGER.info("[%s] restored %d pending timing job(s)", LOGGER_NAME, len(stored))

    async def async_schedule(self, entity_id, commands, timestamp, slot=''):
        """Schedule (or replace) a delayed action for one entity + direction."""
        key = self._make_key(entity_id, slot)
        self._async_cancel(key)
        job = {'entity_id': entity_id, 'commands': commands, 'timestamp': int(timestamp)}
        self._jobs[key] = job
        self._schedule(key, job)
        await self._async_save()
        _LOGGER.debug("[%s] scheduled job %s at %s", LOGGER_NAME, key, job['timestamp'])

    async def async_cancel(self, entity_id, slot=''):
        """Cancel a pending job if present."""
        key = self._make_key(entity_id, slot)
        if key in self._jobs:
            self._async_cancel(key)
            await self._async_save()

    @staticmethod
    def _make_key(entity_id, slot):
        return f"{entity_id}{_KEY_SEP}{slot}" if slot else entity_id

    def _async_cancel(self, key):
        unsub = self._unsubs.pop(key, None)
        if unsub:
            unsub()
        self._jobs.pop(key, None)

    def _schedule(self, key, job):
        point_in_time = dt_util.utc_from_timestamp(int(job['timestamp']))
        self._unsubs[key] = async_track_point_in_time(
            self._hass,
            lambda now, job_key=key: self._hass.async_create_task(self._async_fire(job_key)),
            point_in_time,
        )

    async def _async_fire(self, key):
        job = self._jobs.pop(key, None)
        self._unsubs.pop(key, None)
        if not job:
            return
        await self._async_save()
        entity_id = job.get('entity_id') or key
        for command in job.get('commands', []):
            domain, service, data = command[0], command[1], dict(command[2] or {})
            if 'entity_id' not in data:
                data['entity_id'] = entity_id
            try:
                await self._hass.services.async_call(domain, service, data, blocking=True)
                _LOGGER.info("[%s] executed scheduled %s.%s for %s", LOGGER_NAME, domain, service, entity_id)
            except Exception:  # noqa: BLE001 - 定时任务须记录失败且不影响其它任务
                _LOGGER.error("[%s] failed to run scheduled %s.%s for %s", LOGGER_NAME, domain, service, entity_id, exc_info=True)

    async def _async_save(self):
        await self._store.async_save(self._jobs)
