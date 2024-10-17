#!/usr/bin/python3

import asyncio
import pathlib
from contextvars import ContextVar

import msgspec
import quart

cfgmgr_ctx: ContextVar["ConfigManager"] = ContextVar("cfgmgr")


class NotificationConfig(msgspec.Struct):
    url: str
    tags: list[str] = msgspec.field(default_factory=list)


class AppConfig(msgspec.Struct):
    notifications: list[NotificationConfig] = msgspec.field(default_factory=list)


class ConfigManager(msgspec.Struct):
    config_path: pathlib.Path
    config: AppConfig = msgspec.field(default_factory=AppConfig)
    update_events: set[asyncio.Event] = msgspec.field(default_factory=set)

    def __post_init__(self) -> None:
        self.update_config()
        if cfgmgr_ctx.get(None):
            raise RuntimeError("Configuration manager already exists in current context")
        cfgmgr_ctx.set(self)

    async def monitor_path(self) -> None:
        config_mtime = self.config_path.stat().st_mtime
        self.update_config()

        # hot reload config
        while True:
            new_config_mtime = self.config_path.stat().st_mtime
            if config_mtime != new_config_mtime:
                quart.current_app.logger.debug("Configuration file modified; parsing")
                config_mtime = new_config_mtime
                try:
                    self.update_config()
                    quart.current_app.logger.info("Updated configuration")
                except Exception as e:
                    quart.current_app.logger.error(
                        f"Failed to parse updated configuration file: {e}"
                    )
            await asyncio.sleep(10)

    def update_config(self) -> None:
        self.config = msgspec.toml.decode(self.config_path.read_bytes(), type=AppConfig)
        # mark config as updated for tasks
        self.notify()

    def get_modified_flag(self) -> asyncio.Event:
        """
        Returns an event object that a coroutine can use to track changes in the configuration.
        This is intended for handling stateful operations.
        """
        event = asyncio.Event()
        self.update_events.add(event)
        event.set()
        return event

    def notify(self) -> None:
        for event in self.update_events:
            event.set()
