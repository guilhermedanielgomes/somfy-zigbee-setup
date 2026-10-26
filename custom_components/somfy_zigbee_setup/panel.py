"""The sidebar panel: a page served by Home Assistant that does the Bluetooth part in the browser."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PANEL_ICON, PANEL_TITLE, PANEL_URL_PATH, STATIC_URL, VERSION

_STATIC_REGISTERED = f"{DOMAIN}_static_registered"


async def async_register_panel(hass: HomeAssistant, base_topic: str) -> None:
    frontend_dir = Path(__file__).parent / "frontend"
    if not hass.data.get(_STATIC_REGISTERED):
        try:
            from homeassistant.components.http import StaticPathConfig

            await hass.http.async_register_static_paths([StaticPathConfig(STATIC_URL, str(frontend_dir), False)])
        except ImportError:  # Home Assistant before 2024.6
            hass.http.register_static_path(STATIC_URL, str(frontend_dir), False)
        hass.data[_STATIC_REGISTERED] = True

    await panel_custom.async_register_panel(
        hass,
        webcomponent_name="somfy-zigbee-setup-panel",
        frontend_url_path=PANEL_URL_PATH,
        module_url=f"{STATIC_URL}/panel.js?v={VERSION}",
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        require_admin=True,
        config={"base_topic": base_topic, "version": VERSION},
    )


def async_remove_panel(hass: HomeAssistant) -> None:
    frontend.async_remove_panel(hass, PANEL_URL_PATH)
