#!/usr/bin/python3

import asyncio
import collections
import pathlib
import re
import shutil
import typing
from contextvars import ContextVar

import msgspec
import quart

cfgmgr_ctx: ContextVar["ConfigManager"] = ContextVar("cfgmgr")


PositiveInt = typing.Annotated[int, msgspec.Meta(gt=0)]
NonNegativeInt = typing.Annotated[int, msgspec.Meta(ge=0)]


# map of 'terms' to expressions that match them
PatternMap = dict[str, typing.Pattern]

# type signature for msgspec decode hook
TypeConversionMap = dict[typing.Type, typing.Callable]
MsgspecDecodeHookCallable = typing.Callable[[typing.Type, typing.Any], typing.Any]


# builds a function that takes a mapping of types to callables that can build them
def build_decode_hook(conversions: TypeConversionMap) -> MsgspecDecodeHookCallable:
    def dec_hook(type: typing.Type, obj: typing.Any) -> typing.Any:
        if type in conversions:
            return conversions[type](obj)
        raise NotImplementedError(f"Objects of type {type(obj)} are not supported")

    return dec_hook


_config_decode_hook = build_decode_hook(
    {
        typing.Pattern: re.compile,
        pathlib.Path: pathlib.Path,
    }
)


class NotificationConfig(msgspec.Struct):
    url: str
    tags: list[str] = msgspec.field(default_factory=list)


class YouTubeChannelMonitorConfig(msgspec.Struct):
    id: str
    num_desc_lookbehind: NonNegativeInt = 2
    name: str | None = None
    terms: PatternMap = msgspec.field(default_factory=PatternMap)


class DownloaderConfig(msgspec.Struct, kw_only=True):
    num_parallel_downloads: PositiveInt = 1
    ffmpeg_path: pathlib.Path | None = None
    po_token: str | None = None
    visitor_data: str | None = None

    def __post_init__(self) -> None:
        if self.ffmpeg_path:
            if not self.ffmpeg_path.exists():
                raise ValueError(f"ffmpeg does not exist at {self.ffmpeg_path}")
            elif not shutil.which("ffmpeg", path=self.ffmpeg_path.parent):
                raise ValueError(f"ffmpeg at {self.ffmpeg_path} is not executable")
        elif not shutil.which("ffmpeg"):
            raise ValueError("Could not find a working installation of ffmpeg")


class AppConfig(msgspec.Struct):
    log_level: NonNegativeInt = 30
    downloader: DownloaderConfig = msgspec.field(default_factory=DownloaderConfig)
    notifications: list[NotificationConfig] = msgspec.field(default_factory=list)
    channels: list[YouTubeChannelMonitorConfig] = msgspec.field(default_factory=list)

    def __post_init__(self) -> None:
        channel_dupes = set(
            c for c, n in collections.Counter(c.id for c in self.channels).items() if n > 1
        )
        if channel_dupes:
            raise ValueError(f"Duplicate YouTube channels in config: {channel_dupes}")


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
                quart.current_app.logger.info("Configuration file modified; parsing")
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
        self.config = msgspec.toml.decode(
            self.config_path.read_bytes(), type=AppConfig, dec_hook=_config_decode_hook
        )
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
