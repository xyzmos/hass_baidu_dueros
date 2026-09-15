"""Config flow for HAVCS (Xiaodu/DuerOS, HTTP self-built skill mode)."""

import logging

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_BOT_ID,
    CONF_CLIENT_ID,
    CONF_CLIENT_SECRET,
    CONF_ENTITY_KEY,
    CONF_HA_URL,
    INTEGRATION,
)

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema({
    vol.Required(CONF_CLIENT_ID): cv.string,
    vol.Required(CONF_CLIENT_SECRET): cv.string,
    vol.Required(CONF_HA_URL): cv.string,
    vol.Required(CONF_BOT_ID): cv.string,
    vol.Optional(CONF_ENTITY_KEY): cv.string,
})


def _validate_client_id(client_id: str) -> bool:
    """Client ID 必须以 dueros 开头（与授权页/令牌端点的校验保持一致）。"""
    return isinstance(client_id, str) and client_id.startswith('dueros')


class HavcsConfigFlow(config_entries.ConfigFlow, domain=INTEGRATION):
    """Handle a config flow for HAVCS."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors = {}

        if self._async_current_entries():
            # 仅支持单个实例：服务地址与 client 绑定均为全局
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            entity_key = user_input.get(CONF_ENTITY_KEY, "")
            if entity_key and len(entity_key) != 16:
                errors[CONF_ENTITY_KEY] = "entity_key_validation"
            elif not _validate_client_id(user_input[CONF_CLIENT_ID]):
                errors[CONF_CLIENT_ID] = "client_id_validation"
            else:
                await self.async_set_unique_id(user_input[CONF_CLIENT_ID])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"小度音箱 ({user_input[CONF_CLIENT_ID]})",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input or {}),
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Handle reconfiguration."""
        errors = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            entity_key = user_input.get(CONF_ENTITY_KEY, "")
            if entity_key and len(entity_key) != 16:
                errors[CONF_ENTITY_KEY] = "entity_key_validation"
            elif not _validate_client_id(user_input[CONF_CLIENT_ID]):
                errors[CONF_CLIENT_ID] = "client_id_validation"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates=user_input,
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, entry.data),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        """Get options flow（HA 2024.11+ 推荐无参构造）。"""
        return HavcsOptionsFlow()


class HavcsOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for HAVCS."""

    async def async_step_init(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Manage the options."""
        errors = {}

        if user_input is not None:
            entity_key = user_input.get(CONF_ENTITY_KEY, "")
            if entity_key and len(entity_key) != 16:
                errors[CONF_ENTITY_KEY] = "entity_key_validation"
            else:
                return self.async_create_entry(data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema({
                    vol.Optional(CONF_ENTITY_KEY): cv.string,
                }),
                self.config_entry.options,
            ),
            errors=errors,
        )
