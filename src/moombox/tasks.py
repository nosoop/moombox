#!/usr/bin/python3

import asyncio
import collections
import dataclasses
import datetime
import enum
import functools
import pathlib
import secrets
from contextvars import ContextVar
from typing import Any, AsyncGenerator, NamedTuple

import moonarchive.models.messages as msgtypes
import msgspec
import quart
from moonarchive.downloaders.youtube import YouTubeDownloader
from moonarchive.models.youtube_player import YTPlayerMediaType
from moonarchive.output import BaseMessageHandler

from .config import cfgmgr_ctx
from .database import database_ctx
from .notifications import apobj_ctx


@dataclasses.dataclass
class DownloadManager:
    """
    Keeps track of download jobs and passes messages to connected clients.
    """

    jobs: dict[str, "DownloadJob"] = dataclasses.field(default_factory=dict)
    connections: set[asyncio.Queue] = dataclasses.field(default_factory=set)
    detail_connections: dict[str, set[asyncio.Queue]] = dataclasses.field(
        default_factory=functools.partial(collections.defaultdict, set)
    )

    def create_job(self, downloader: YouTubeDownloader) -> "DownloadJob":
        jobid = secrets.token_urlsafe(8)
        while jobid in self.jobs:
            # we should never get duplicates, but just in case
            jobid = secrets.token_urlsafe(8)
        if not downloader.staging_directory:
            downloader.staging_directory = pathlib.Path("staging") / jobid

        cfgmgr = cfgmgr_ctx.get(None)
        if cfgmgr:
            if not downloader.ffmpeg_path:
                downloader.ffmpeg_path = cfgmgr.config.downloader.ffmpeg_path

        self.jobs[jobid] = DownloadJob(jobid, downloader=downloader)
        return self.jobs[jobid]

    async def publish(self, message: Any) -> None:
        for connection in self.connections:
            await connection.put(message)

    async def subscribe(self) -> AsyncGenerator:
        connection: asyncio.Queue = asyncio.Queue()
        self.connections.add(connection)
        try:
            while True:
                yield await connection.get()
        finally:
            self.connections.remove(connection)

    async def publish_detail(self, job: str, message: Any) -> None:
        for connection in self.detail_connections[job]:
            await connection.put(message)

    async def subscribe_detail(self, job: str) -> AsyncGenerator:
        connection: asyncio.Queue = asyncio.Queue()
        self.detail_connections[job].add(connection)
        try:
            while True:
                yield await connection.get()
        finally:
            self.detail_connections[job].remove(connection)


manager_ctx: ContextVar[DownloadManager | None] = ContextVar("manager", default=None)


class DownloadStatus(enum.StrEnum):
    UNKNOWN = "Unknown"
    UNAVAILABLE = "Unavailable"
    WAITING = "Waiting"
    DOWNLOADING = "Downloading"
    MUXING = "Muxing"
    FINISHED = "Finished"
    ERROR = "Error"


class DownloadLogMessage(NamedTuple):
    event_datetime: datetime.datetime
    message: str


class DownloadManifestProgress(msgspec.Struct):
    video_seq: int = 0
    audio_seq: int = 0
    max_seq: int = 0
    total_downloaded: int = 0


class DownloadJob(BaseMessageHandler):
    id: str

    # downloader may be omitted if this job is pulled from cache or mocked for visual testing
    downloader: YouTubeDownloader | None = None

    author: str | None = None
    channel_id: str | None = None
    video_id: str | None = None
    scheduled_start_datetime: datetime.datetime | None = None
    thumbnail_url: str | None = None
    current_manifest: str | None = None
    title: str | None = None
    status: DownloadStatus = DownloadStatus.UNKNOWN
    message_log: list[DownloadLogMessage] = msgspec.field(default_factory=list)
    manifest_progress: dict[str, DownloadManifestProgress] = msgspec.field(
        default_factory=functools.partial(collections.defaultdict, DownloadManifestProgress)
    )

    async def handle_message(self, msg: msgtypes.BaseMessage) -> None:
        prev_status = self.status
        match msg:
            case msg if isinstance(msg, msgtypes.StreamInfoMessage):
                self.title = msg.video_title
                self.status = DownloadStatus.WAITING
            case msg if isinstance(msg, msgtypes.FragmentMessage):
                self.status = DownloadStatus.DOWNLOADING

                manifest_progress = self.manifest_progress[msg.manifest_id]
                manifest_progress.max_seq = max(manifest_progress.max_seq, msg.max_fragments)
                if msg.media_type == "audio":
                    manifest_progress.audio_seq = msg.current_fragment
                elif msg.media_type == "video":
                    manifest_progress.video_seq = msg.current_fragment
                manifest_progress.total_downloaded += msg.fragment_size
                self.current_manifest = msg.manifest_id
                self.video_id, *_ = msg.manifest_id.split(".")
            case msg if isinstance(msg, msgtypes.DownloadJobFinishedMessage):
                self.status = DownloadStatus.FINISHED
                self.append_message("Finished downloading")

                database = database_ctx.get()
                if database:
                    database.execute(
                        "INSERT OR IGNORE INTO jobs (id, payload) VALUES (?, ?)",
                        (
                            self.id,
                            msgspec.json.encode(msgspec.structs.replace(self, downloader=None)),
                        ),
                    )
                    database.commit()
            case msg if isinstance(msg, msgtypes.DownloadJobFailedOutputMoveMessage):
                self.status = DownloadStatus.ERROR
            case msg if isinstance(msg, msgtypes.StreamMuxMessage):
                self.status = DownloadStatus.MUXING
                self.append_message("Started remux process")
            case msg if isinstance(msg, msgtypes.StreamUnavailableMessage):
                self.status = DownloadStatus.UNAVAILABLE
            case msg if isinstance(msg, msgtypes.FormatSelectionMessage):
                major_type_str = str(msg.major_type).capitalize()
                display_media_type = msg.format.media_type.codec_primary or "unknown codec"
                if msg.major_type == YTPlayerMediaType.VIDEO:
                    if display_media_type.startswith("avc1"):
                        display_media_type = "h264"
                    self.append_message(
                        f"{major_type_str} format: {msg.format.quality_label} "
                        f"{display_media_type} (itag {msg.format.itag}, manifest "
                        f"{msg.manifest_id}, duration {msg.format.target_duration_sec})"
                    )
                elif msg.format.bitrate:
                    self.append_message(
                        f"{major_type_str} format: {msg.format.bitrate // 1000}k "
                        f"{display_media_type} (itag {msg.format.itag}, manifest "
                        f"{msg.manifest_id}, duration {msg.format.target_duration_sec})"
                    )
                else:
                    self.append_message(
                        f"{major_type_str} format selected (manifest "
                        f"{msg.manifest_id}, duration {msg.format.target_duration_sec})"
                    )
            case msg if isinstance(msg, msgtypes.StringMessage):
                self.append_message(msg.text)
            case _:
                pass

        if prev_status != self.status:
            self.broadcast_status_update()

        manager = manager_ctx.get()
        if manager:
            quart.current_app.add_background_task(manager.publish, self)
            quart.current_app.add_background_task(manager.publish_detail, self.id, self)

    async def run(self) -> None:
        if self.downloader:
            self.downloader.handlers = [self]
            self.append_message("Started download task")
            try:
                await self.downloader.async_run()
            except Exception as exc:
                self.status = DownloadStatus.ERROR
                self.broadcast_status_update()
                self.append_message(f"Exception: {exc=}")

    def append_message(self, message: str) -> None:
        self.message_log.append(
            DownloadLogMessage(datetime.datetime.now(tz=datetime.UTC), message)
        )

    def broadcast_status_update(self) -> None:
        apobj = apobj_ctx.get()
        if not apobj:
            return
        quart.current_app.add_background_task(
            apobj.async_notify,
            title=f"Archive status: {str(self.status).capitalize()}",
            body=f"{self.title} from {self.author} @ https://youtu.be/{self.video_id}",
            tag=f"status:{self.status.lower()}",
        )

    def get_status(self) -> dict:
        return msgspec.to_builtins(msgspec.structs.replace(self, downloader=None))

    @property
    def video_seq(self) -> int:
        return sum(prog.video_seq for prog in self.manifest_progress.values())

    @property
    def audio_seq(self) -> int:
        return sum(prog.audio_seq for prog in self.manifest_progress.values())

    @property
    def max_seq(self) -> int:
        return sum(prog.max_seq for prog in self.manifest_progress.values())

    @property
    def total_downloaded(self) -> int:
        return sum(prog.total_downloaded for prog in self.manifest_progress.values())

    @property
    def can_delete_tempfiles(self) -> bool:
        return self.video_id is not None and self.status == DownloadStatus.FINISHED
