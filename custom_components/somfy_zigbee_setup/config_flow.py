"""Config flow: one instance, one setting (the Zigbee2MQTT base topic)."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import CONF_BASE_TOPIC, DEFAULT_BASE_TOPIC, DOMAIN


class SomfyZigbeeSetupConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            return self.async_create_entry(title="Somfy Zigbee Setup", data=user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Optional(CONF_BASE_TOPIC, default=DEFAULT_BASE_TOPIC): str}),
        )
