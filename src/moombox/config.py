#!/usr/bin/python3

import asyncio
import collections
import dataclasses
import datetime
import logging
import os
import pathlib
import re
import shutil
import typing
from contextvars import ContextVar

import msgspec
import quart
from moonarchive.util.paths import OutputPathTemplate, OutputPathTemplateVars

cfgmgr_ctx: ContextVar["ConfigManager"] = ContextVar("cfgmgr")


PositiveInt = typing.Annotated[int, msgspec.Meta(gt=0)]
NonNegativeInt = typing.Annotated[int, msgspec.Meta(ge=0)]


# map of 'terms' to expressions that match them
PatternMap = dict[str, typing.Pattern]

# type signature for msgspec decode hook
TypeConversionMap = dict[typing.Type, typing.Callable]
MsgspecDecodeHookCallable = typing.Callable[[typing.Type, typing.Any], typing.Any]

# resolutions used in the resolution selection dropdown
VALID_RESOLUTION_VALUES = {144, 240, 360, 480, 720, 1080, 1440, 2160, 4320}


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
        OutputPathTemplate: OutputPathTemplate,
    }
)

_sample_vars = OutputPathTemplateVars(
    title="stream title",
    id="xxxxxxxxxxx.00",
    video_id="xxxxxxxxxxx",
    channel_id="UCxxxxxxxxxxxxxxxxxxxxxx",
    channel="youtube channel",
)
_sample_vars.start_datetime = datetime.datetime.fromtimestamp(0, tz=datetime.UTC)

_bcompat_format_match = re.compile(r"\%(?:\((?P<named>[_a-z][_a-z0-9]*)\)|(?P<invalid>))s?")
"""
Pattern to match legacy string interpolation format.
We disallow this here in favor of the nicer, simpler template string syntax.
"""


def _validate_output_template(output_template: OutputPathTemplate) -> None:
    """
    Ensures that the output template is permitted.
    """
    bcompat_items = {
        bcmatch.group(0): bcmatch.group("named")
        for bcmatch in _bcompat_format_match.finditer(output_template.template)
    }
    if bcompat_items:
        interpolated_format = ", ".join(bcompat_items.keys())
        template_format = ", ".join(f"${{{key}}}" for key in bcompat_items.values())
        raise ValueError(
            f"Found keys using interpolation syntax {interpolated_format}; "
            f"expected keys in template syntax {template_format}"
        )

    var_names = set(field.name for field in dataclasses.fields(OutputPathTemplateVars))
    template_idents = set(output_template.get_identifiers())
    undefined_names = template_idents - var_names
    if undefined_names:
        undefined_name_list = ", ".join(undefined_names)
        raise ValueError(f"Undefined key(s) {undefined_name_list} in template")

    p = output_template.to_path(_sample_vars, suffix=".description")
    if p.is_absolute():
        raise ValueError(
            f"Output template '{output_template}' should not specify an absolute path"
        )


class NotificationConfig(msgspec.Struct):
    url: str
    tags: list[str] = msgspec.field(default_factory=list)


class YouTubeChannelMonitorConfig(msgspec.Struct):
    id: str
    num_desc_lookbehind: NonNegativeInt = 2
    name: str | None = None
    terms: PatternMap = msgspec.field(default_factory=PatternMap)
    output_directory: pathlib.Path | None = None
    include_non_live_content: bool = False

    def __post_init__(self) -> None:
        if not self.id.startswith("UC"):
            raise ValueError(f"Expected 'UC' prefix for YouTube channel ID '{self.id}'")


class TaskListConfig(msgspec.Struct):
    hide_finished_age_days: NonNegativeInt = 0

    @property
    def hide_finished_age(self) -> datetime.timedelta | None:
        if self.hide_finished_age_days:
            return datetime.timedelta(days=self.hide_finished_age_days)
        return None


class HealthcheckConfig(msgspec.Struct):
    enable_scheduled: bool = False


class DownloaderConfig(msgspec.Struct, kw_only=True):
    num_parallel_downloads: PositiveInt = 1
    max_video_resolution: PositiveInt = msgspec.field(default=max(VALID_RESOLUTION_VALUES))
    ffmpeg_path: pathlib.Path | None = None
    output_directory: pathlib.Path | None = None
    output_template: OutputPathTemplate | None = None
    staging_directory: pathlib.Path | None = None
    po_token: str | None = None
    visitor_data: str | None = None
    cookie_file: pathlib.Path | None = None

    # experimental option - design is not finalized
    unstable_bgutil_pot_provider_url: str | None = None

    def __post_init__(self) -> None:
        if self.ffmpeg_path:
            if not self.ffmpeg_path.exists():
                raise ValueError(f"ffmpeg does not exist at {self.ffmpeg_path}")
            elif not shutil.which("ffmpeg", path=self.ffmpeg_path.parent):
                raise ValueError(f"ffmpeg at {self.ffmpeg_path} is not executable")
        elif not shutil.which("ffmpeg"):
            raise ValueError("Could not find a working installation of ffmpeg")

        if self.staging_directory:
            if not self.staging_directory.exists():
                try:
                    self.staging_directory.mkdir(parents=True, exist_ok=True)
                except OSError:
                    raise ValueError(
                        f"Failed to create staging directory {self.staging_directory}"
                    )
            if not os.access(self.staging_directory, os.R_OK | os.W_OK | os.X_OK):
                raise ValueError(
                    f"Staging directory {self.staging_directory} is not accessible"
                )
        if self.output_directory:
            if not self.output_directory.exists():
                try:
                    self.output_directory.mkdir(parents=True, exist_ok=True)
                except OSError:
                    raise ValueError(
                        f"Failed to create download output directory {self.output_directory}"
                    )
            if not os.access(self.output_directory, os.R_OK | os.W_OK | os.X_OK):
                raise ValueError(
                    f"Download output directory {self.output_directory} is not accessible"
                )
        if self.output_template:
            _validate_output_template(self.output_template)

        if self.cookie_file and not self.cookie_file.exists():
            raise ValueError(f"Cookie file {self.cookie_file} does not exist")

        if (
            self.max_video_resolution is not None
            and self.max_video_resolution not in VALID_RESOLUTION_VALUES
        ):
            raise ValueError(
                f"Invalid resolution preset {self.max_video_resolution} "
                f"(expected one of {', '.join(map(str, sorted(VALID_RESOLUTION_VALUES)))})"
            )


class AppConfig(msgspec.Struct):
    log_level: NonNegativeInt | str = 30
    tasklist: TaskListConfig = msgspec.field(default_factory=TaskListConfig)
    downloader: DownloaderConfig = msgspec.field(default_factory=DownloaderConfig)
    notifications: list[NotificationConfig] = msgspec.field(default_factory=list)
    channels: list[YouTubeChannelMonitorConfig] = msgspec.field(default_factory=list)
    healthchecks: HealthcheckConfig = msgspec.field(default_factory=HealthcheckConfig)

    def __post_init__(self) -> None:
        if isinstance(self.log_level, str):
            available_levels = logging.getLevelNamesMapping()
            if self.log_level in available_levels:
                self.log_level = available_levels[self.log_level]
            else:
                raise ValueError(
                    f"Log level name {self.log_level} is not valid "
                    f"(expected one of {', '.join(available_levels)})"
                )
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
        config_mtime = self.config_path.stat().st_mtime if self.config_path.exists() else 0
        self.update_config()

        # hot reload config
        while True:
            new_config_mtime = (
                self.config_path.stat().st_mtime if self.config_path.exists() else 0
            )
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

    @property
    def config_text(self) -> str:
        if self.config_path.exists():
            return self.config_path.read_text("utf8")
        # TODO: add tomli_w as a dependency so we can show the default file
        return f"# No configuration file at '{self.config_path.resolve()}'; using defaults."

    def update_config(self) -> None:
        self.config = msgspec.toml.decode(
            self.config_path.read_bytes() if self.config_path.exists() else b"",
            type=AppConfig,
            dec_hook=_config_decode_hook,
        )
        # mark config as updated for tasks
        self.notify()

    def save_config(self, data: str) -> None:
        if self.read_only:
            raise ValueError("Configuration file is read-only.")
        try:
            msgspec.toml.decode(data, type=AppConfig, dec_hook=_config_decode_hook)
            self.config_path.write_text(data, "utf8")
        except (OSError, msgspec.ValidationError, msgspec.DecodeError) as exc:
            raise exc

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

    @property
    def read_only(self) -> bool:
        return not self.config_path.exists() or not os.access(self.config_path, os.W_OK)
