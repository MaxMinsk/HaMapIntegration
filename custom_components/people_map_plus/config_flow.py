"""Config flow for People Map Plus integration."""

from __future__ import annotations

from typing import Any

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
import voluptuous as vol

from .const import (
    CONF_DEFAULT_PHOTO_DAYS,
    CONF_DEFAULT_PHOTO_LIMIT,
    CONF_DEFAULT_TRACK_DAYS,
    CONF_INDEX_INTERVAL_MINUTES,
    CONF_MAX_SCAN_FILES_PER_RUN,
    CONF_PHOTO_ROOTS,
    CONF_THUMB_PREFERRED,
    DEFAULT_DEFAULT_PHOTO_DAYS,
    DEFAULT_DEFAULT_PHOTO_LIMIT,
    DEFAULT_DEFAULT_TRACK_DAYS,
    DEFAULT_INDEX_INTERVAL_MINUTES,
    DEFAULT_MAX_SCAN_FILES_PER_RUN,
    DEFAULT_PHOTO_ROOTS,
    DEFAULT_THUMB_PREFERRED,
    DOMAIN,
)


def _options_schema(user_input: dict[str, Any] | None = None) -> vol.Schema:
    current = user_input or {}
    return vol.Schema(
        {
            vol.Required(CONF_NAME, default=current.get(CONF_NAME, "People Map Plus")): str,
            vol.Required(
                CONF_PHOTO_ROOTS,
                default=current.get(CONF_PHOTO_ROOTS, ",".join(DEFAULT_PHOTO_ROOTS)),
            ): str,
            vol.Required(
                CONF_INDEX_INTERVAL_MINUTES,
                default=current.get(CONF_INDEX_INTERVAL_MINUTES, DEFAULT_INDEX_INTERVAL_MINUTES),
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=1440)),
            vol.Required(
                CONF_MAX_SCAN_FILES_PER_RUN,
                default=current.get(CONF_MAX_SCAN_FILES_PER_RUN, DEFAULT_MAX_SCAN_FILES_PER_RUN),
            ): vol.All(vol.Coerce(int), vol.Range(min=100, max=50000)),
            vol.Required(
                CONF_DEFAULT_PHOTO_DAYS,
                default=current.get(CONF_DEFAULT_PHOTO_DAYS, DEFAULT_DEFAULT_PHOTO_DAYS),
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=30)),
            vol.Required(
                CONF_DEFAULT_TRACK_DAYS,
                default=current.get(CONF_DEFAULT_TRACK_DAYS, DEFAULT_DEFAULT_TRACK_DAYS),
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=30)),
            vol.Required(
                CONF_DEFAULT_PHOTO_LIMIT,
                default=current.get(CONF_DEFAULT_PHOTO_LIMIT, DEFAULT_DEFAULT_PHOTO_LIMIT),
            ): vol.All(vol.Coerce(int), vol.Range(min=0, max=50000)),
            vol.Required(
                CONF_THUMB_PREFERRED,
                default=current.get(CONF_THUMB_PREFERRED, DEFAULT_THUMB_PREFERRED),
            ): bool,
        }
    )


def _normalize_options(user_input: dict[str, Any]) -> dict[str, Any]:
    roots_raw = str(user_input.get(CONF_PHOTO_ROOTS, ""))
    roots = [
        root.strip().replace("\\", "/").strip("/")
        for root in roots_raw.split(",")
        if root.strip()
    ]
    roots = sorted(set(roots)) or DEFAULT_PHOTO_ROOTS.copy()

    return {
        CONF_NAME: str(user_input.get(CONF_NAME, "People Map Plus")).strip() or "People Map Plus",
        CONF_PHOTO_ROOTS: roots,
        CONF_INDEX_INTERVAL_MINUTES: int(user_input.get(CONF_INDEX_INTERVAL_MINUTES, DEFAULT_INDEX_INTERVAL_MINUTES)),
        CONF_MAX_SCAN_FILES_PER_RUN: int(user_input.get(CONF_MAX_SCAN_FILES_PER_RUN, DEFAULT_MAX_SCAN_FILES_PER_RUN)),
        CONF_DEFAULT_PHOTO_DAYS: int(user_input.get(CONF_DEFAULT_PHOTO_DAYS, DEFAULT_DEFAULT_PHOTO_DAYS)),
        CONF_DEFAULT_TRACK_DAYS: int(user_input.get(CONF_DEFAULT_TRACK_DAYS, DEFAULT_DEFAULT_TRACK_DAYS)),
        CONF_DEFAULT_PHOTO_LIMIT: int(user_input.get(CONF_DEFAULT_PHOTO_LIMIT, DEFAULT_DEFAULT_PHOTO_LIMIT)),
        CONF_THUMB_PREFERRED: bool(user_input.get(CONF_THUMB_PREFERRED, DEFAULT_THUMB_PREFERRED)),
    }


class PeopleMapPlusConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for People Map Plus."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            normalized = _normalize_options(user_input)
            return self.async_create_entry(
                title=normalized[CONF_NAME],
                data=normalized,
            )

        return self.async_show_form(
            step_id="user",
            data_schema=_options_schema(),
        )

    @staticmethod
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return PeopleMapPlusOptionsFlow(config_entry)


class PeopleMapPlusOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for People Map Plus."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        current = {
            **self._config_entry.data,
            **self._config_entry.options,
        }
        current[CONF_PHOTO_ROOTS] = ",".join(current.get(CONF_PHOTO_ROOTS, DEFAULT_PHOTO_ROOTS))

        if user_input is not None:
            normalized = _normalize_options(user_input)
            return self.async_create_entry(title="", data=normalized)

        return self.async_show_form(
            step_id="init",
            data_schema=_options_schema(current),
        )
