"""People Map Plus integration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeAlias

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant, ServiceCall

from .api import (
    PeopleMapPlusPhotoProxyView,
    PeopleMapPlusPhotosView,
    PeopleMapPlusStatusView,
    PeopleMapPlusTracksView,
)
from .const import (
    DB_FILE_NAME,
    DOMAIN,
    PLATFORMS,
    SCAN_SERVICE,
    STORAGE_SUBDIR,
)
from .scanner import PhotoIndexScanner
from .storage import PhotoIndexRepository


@dataclass(slots=True)
class RuntimeData:
    """Runtime objects for config entry."""

    repository: PhotoIndexRepository
    scanner: PhotoIndexScanner
    unsubscribe_update_listener: Any


PeopleMapConfigEntry: TypeAlias = ConfigEntry[RuntimeData]


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up People Map Plus component."""
    hass.data.setdefault(DOMAIN, {})

    hass.http.register_view(PeopleMapPlusStatusView())
    hass.http.register_view(PeopleMapPlusPhotosView())
    hass.http.register_view(PeopleMapPlusTracksView())
    hass.http.register_view(PeopleMapPlusPhotoProxyView())

    if not hass.services.has_service(DOMAIN, SCAN_SERVICE):
        async def _handle_scan_now(service_call: ServiceCall) -> None:
            target_entry_id = service_call.data.get("entry_id")
            entries = list(hass.data.get(DOMAIN, {}).items())
            for entry_id, runtime in entries:
                if target_entry_id and entry_id != target_entry_id:
                    continue
                await runtime.scanner.async_scan_once("service")

        hass.services.async_register(DOMAIN, SCAN_SERVICE, _handle_scan_now)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: PeopleMapConfigEntry) -> bool:
    """Set up People Map Plus from config entry."""
    options = _effective_options(entry)
    db_path = Path(hass.config.path(STORAGE_SUBDIR, DB_FILE_NAME))
    repository = PhotoIndexRepository(hass, db_path)
    await repository.async_initialize()

    scanner = PhotoIndexScanner(hass, repository, entry.entry_id, options)
    await scanner.async_start()

    unsubscribe = entry.add_update_listener(_async_update_listener)
    entry.runtime_data = RuntimeData(
        repository=repository,
        scanner=scanner,
        unsubscribe_update_listener=unsubscribe,
    )
    hass.data[DOMAIN][entry.entry_id] = entry.runtime_data

    if PLATFORMS:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: PeopleMapConfigEntry) -> bool:
    """Unload config entry."""
    runtime = hass.data[DOMAIN].get(entry.entry_id)
    if runtime is not None:
        await runtime.scanner.async_stop()
        if runtime.unsubscribe_update_listener is not None:
            runtime.unsubscribe_update_listener()

    unload_ok = True
    if PLATFORMS:
        unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: PeopleMapConfigEntry) -> None:
    runtime = hass.data[DOMAIN].get(entry.entry_id)
    if runtime is None:
        return

    options = _effective_options(entry)
    await runtime.scanner.async_update_options(options)


def _effective_options(entry: ConfigEntry) -> dict[str, Any]:
    merged = {**entry.data, **entry.options}
    if not merged.get(CONF_NAME):
        merged[CONF_NAME] = "People Map Plus"
    return merged
