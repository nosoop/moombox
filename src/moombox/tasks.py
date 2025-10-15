#!/usr/bin/python3

import asyncio
import collections
import dataclasses
import datetime
import enum
import functools
import pathlib
import secrets
import traceback
from contextvars import ContextVar
from typing import Any, AsyncGenerator, NamedTuple

import aiolimiter
import moonarchive.models.messages as msgtypes
import msgspec
import quart
from moonarchive.downloaders.youtube import YouTubeDownloader
from moonarchive.downloaders.youtube.player import YTPlayerAdaptiveFormats, YTPlayerMediaType
from moonarchive.models.ffmpeg import FFMPEGProgress
from moonarchive.output import BaseMessageHandler

from .config import build_decode_hook, cfgmgr_ctx
from .database import database_ctx
from .extractor import fetch_youtube_player_response
from .notifications import apobj_ctx

downloadjob_decode_hook = build_decode_hook(
    {
        pathlib.Path: pathlib.Path,
    }
)


def _downloadjob_encode_hook(obj: Any) -> str:
    match obj:
        case pathlib.Path():
            return str(obj.resolve())
        case _:
            raise TypeError(f"Unsupported type {type(obj)}")


@dataclasses.dataclass
class DownloadManager:
    """
    Keeps track of download jobs and passes messages to connected clients.
    """

    jobs: dict[str, "DownloadJob"] = dataclasses.field(default_factory=dict)
    active_tasks: dict[str, asyncio.Task] = dataclasses.field(default_factory=dict)
    connections: set[asyncio.Queue] = dataclasses.field(default_factory=set)
    detail_connections: dict[str, set[asyncio.Queue]] = dataclasses.field(
        default_factory=functools.partial(collections.defaultdict, set)
    )

    def create_job(self, downloader: YouTubeDownloader) -> "DownloadJob":
        jobid = secrets.token_urlsafe(8)
        while jobid in self.jobs:
            # we should never get duplicates, but just in case
            jobid = secrets.token_urlsafe(8)

        cfgmgr = cfgmgr_ctx.get(None)
        if cfgmgr:
            if not downloader.ffmpeg_path:
                downloader.ffmpeg_path = cfgmgr.config.downloader.ffmpeg_path
            if not downloader.po_token:
                downloader.po_token = cfgmgr.config.downloader.po_token
            if not downloader.visitor_data:
                downloader.visitor_data = cfgmgr.config.downloader.visitor_data
            if not downloader.staging_directory and cfgmgr.config.downloader.staging_directory:
                downloader.staging_directory = (
                    cfgmgr.config.downloader.staging_directory / jobid
                )
            if not downloader.output_directory:
                downloader.output_directory = cfgmgr.config.downloader.output_directory
            if not downloader.cookie_file:
                downloader.cookie_file = cfgmgr.config.downloader.cookie_file
            if not downloader.output_template:
                downloader.output_template = cfgmgr.config.downloader.output_template
            if not downloader.max_video_resolution:
                downloader.max_video_resolution = cfgmgr.config.downloader.max_video_resolution
            downloader.unstable_bgutil_pot_provider_url = (
                cfgmgr.config.downloader.unstable_bgutil_pot_provider_url
            )
        if not downloader.staging_directory:
            downloader.staging_directory = pathlib.Path("staging") / jobid

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

    @property
    def visible_jobs(self) -> list["DownloadJob"]:
        cfgmgr = cfgmgr_ctx.get()
        now = datetime.datetime.now(tz=datetime.UTC)
        age = cfgmgr.config.tasklist.hide_finished_age
        return sorted(
            (
                job
                for job in self.jobs.values()
                if not job.download_finish_datetime
                or not age
                or now - job.download_finish_datetime < age
            ),
            key=DownloadJob.sort_key,
        )


manager_ctx: ContextVar[DownloadManager | None] = ContextVar("manager", default=None)


class DownloadStatus(enum.StrEnum):
    UNKNOWN = "Unknown"
    UNAVAILABLE = "Unavailable"
    WAITING = "Waiting"
    DOWNLOADING = "Downloading"
    MUXING = "Muxing"
    FINISHED = "Finished"
    ERROR = "Error"
    CANCELLED = "Cancelled"

    @property
    def can_delete_tempfiles(self) -> bool:
        """
        Returns whether or not temporary files can be deleted at the current status.
        DOWNLOADING and MUXING states do have temporary files, but they are restricted from
        deletion during those states.
        """
        return self in (DownloadStatus.CANCELLED, DownloadStatus.FINISHED, DownloadStatus.ERROR)

    @property
    def sort_key(self) -> int:
        match self:
            case DownloadStatus.UNKNOWN:
                # tasks are initially put in the unknown state, so we can treat that as
                # effectively being "we just added this" and giving it sorting priority
                return 3
            case DownloadStatus.DOWNLOADING:
                return 2
            case DownloadStatus.WAITING:
                return 1
        return 0


class DownloadLogMessage(NamedTuple):
    event_datetime: datetime.datetime
    message: str


class DownloadManifestProgress(msgspec.Struct):
    video_seq: int = 0
    audio_seq: int = 0
    max_seq: int = 0
    video_format: YTPlayerAdaptiveFormats | None = None
    audio_format: YTPlayerAdaptiveFormats | None = None
    total_downloaded: int = 0
    download_start_dt: datetime.datetime | None = None
    download_last_update_dt: datetime.datetime | None = None
    output: FFMPEGProgress = msgspec.field(default_factory=FFMPEGProgress)

    @property
    def estimated_download_time_remaining(self) -> datetime.timedelta | None:
        """
        Returns an estimate of the remaining download time for the given manifest.
        This estimate is produced with granularity in seconds.
        """
        if not self.download_start_dt or not self.download_last_update_dt:
            return None
        min_current_seq = max(min(self.video_seq, self.audio_seq), 1)
        remaining_fragments = self.max_seq - min_current_seq
        elapsed_seconds = max(
            1, (self.download_last_update_dt - self.download_start_dt).total_seconds()
        )
        estimated_remaining_seconds = remaining_fragments / (min_current_seq / elapsed_seconds)
        return datetime.timedelta(seconds=int(estimated_remaining_seconds))


class HealthCheckResult(enum.StrEnum):
    # no problems
    OK = "OK"

    # general failure to perform health check
    HEALTHCHECK_FAILURE = "HEALTHCHECK_FAILURE"

    # video was made private
    VIDEO_UNAVAILABLE = "VIDEO_UNAVAILABLE"

    # stream duration differs significantly (content was likely cut)
    STREAM_LENGTH_DIFFERS = "STREAM_LENGTH_DIFFERS"

    # stream duration is indeterminate (multi-manifest stream)
    STREAM_LENGTH_INDETERMINATE = "STREAM_LENGTH_INDETERMINATE"


_healthcheck_rate_limiter = aiolimiter.AsyncLimiter(1, 45)


class HealthCheckStatus(msgspec.Struct):
    result: HealthCheckResult | None = None
    last_update: datetime.datetime | None = None


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
    download_finish_datetime: datetime.datetime | None = None
    message_log: list[DownloadLogMessage] = msgspec.field(default_factory=list)
    manifest_progress: dict[str, DownloadManifestProgress] = msgspec.field(
        default_factory=functools.partial(collections.defaultdict, DownloadManifestProgress)
    )
    healthcheck: HealthCheckStatus = msgspec.field(default_factory=HealthCheckStatus)
    output_paths: set[pathlib.Path] = msgspec.field(default_factory=set)

    def __post_init__(self):
        if self.download_finish_datetime is None and self.status == DownloadStatus.FINISHED:
            # fall back to the last event entry for completed tasks
            self.download_finish_datetime = self.message_log[-1].event_datetime

    @dataclasses.dataclass
    class SortKey:
        status: DownloadStatus = DownloadStatus.UNKNOWN
        event_datetime: datetime.datetime | None = None

        def __lt__(self, other: "DownloadJob.SortKey") -> bool:
            if self.status.sort_key != other.status.sort_key:
                return self.status.sort_key < other.status.sort_key
            elif not self.event_datetime or not other.event_datetime:
                # if either has no time, fall back to insertion order
                return False
            if self.status == DownloadStatus.WAITING:
                # items with upcoming times closer to now should appear closer to the top
                return self.event_datetime > other.event_datetime
            return self.event_datetime < other.event_datetime

    def sort_key(self) -> "DownloadJob.SortKey":
        event_datetime = self.message_log[-1].event_datetime if len(self.message_log) else None
        match self.status:
            case DownloadStatus.FINISHED:
                event_datetime = self.download_finish_datetime
            case DownloadStatus.WAITING:
                event_datetime = self.scheduled_start_datetime
            case _:
                pass
        return DownloadJob.SortKey(self.status, event_datetime)

    async def handle_message(self, msg: msgtypes.BaseMessage) -> None:
        prev_status = self.status
        match msg:
            case msgtypes.StreamInfoMessage():
                self.title = msg.video_title
                self.status = DownloadStatus.WAITING
                if self.scheduled_start_datetime != msg.start_datetime:
                    self.scheduled_start_datetime = msg.start_datetime
            case msgtypes.FragmentMessage():
                self.status = DownloadStatus.DOWNLOADING

                manifest_progress = self.manifest_progress[msg.manifest_id]
                manifest_progress.max_seq = max(manifest_progress.max_seq, msg.max_fragments)
                if not manifest_progress.download_start_dt:
                    manifest_progress.download_start_dt = datetime.datetime.now(tz=datetime.UTC)
                manifest_progress.download_last_update_dt = datetime.datetime.now(
                    tz=datetime.UTC
                )
                if msg.media_type == "audio":
                    manifest_progress.audio_seq = msg.current_fragment
                elif msg.media_type == "video":
                    manifest_progress.video_seq = msg.current_fragment
                manifest_progress.total_downloaded += msg.fragment_size
                self.current_manifest = msg.manifest_id
                self.video_id, *_ = msg.manifest_id.split(".")
            case msgtypes.DownloadJobFinishedMessage():
                self.status = DownloadStatus.FINISHED
                self.append_message("Finished downloading")
                self.output_paths = set(msg.output_paths)
                self.download_finish_datetime = datetime.datetime.now(tz=datetime.UTC)
                self.persist_to_database()
                quart.current_app.add_background_task(self.run_scheduled_healthchecks)
            case msgtypes.DownloadJobFailedOutputMoveMessage():
                self.status = DownloadStatus.ERROR
            case msgtypes.StreamMuxMessage():
                self.status = DownloadStatus.MUXING
                self.append_message("Started remux process")
            case msgtypes.StreamUnavailableMessage():
                self.status = DownloadStatus.UNAVAILABLE
            case msgtypes.FormatSelectionMessage():
                major_type_str = str(msg.major_type).capitalize()
                display_media_type = msg.format.media_type.codec_primary or "unknown codec"
                manifest_progress = self.manifest_progress[msg.manifest_id]
                if msg.major_type == YTPlayerMediaType.VIDEO:
                    if display_media_type.startswith("avc1"):
                        display_media_type = "h264"
                    self.append_message(
                        f"{major_type_str} format: {msg.format.quality_label} "
                        f"{display_media_type} (itag {msg.format.itag}, manifest "
                        f"{msg.manifest_id}, duration {msg.format.target_duration_sec})"
                    )
                    manifest_progress.video_format = msg.format
                    manifest_progress.video_format.url = None
                elif msg.format.bitrate:
                    self.append_message(
                        f"{major_type_str} format: {msg.format.bitrate // 1000}k "
                        f"{display_media_type} (itag {msg.format.itag}, manifest "
                        f"{msg.manifest_id}, duration {msg.format.target_duration_sec})"
                    )
                    manifest_progress.audio_format = msg.format
                    manifest_progress.audio_format.url = None
                else:
                    self.append_message(
                        f"{major_type_str} format selected (manifest "
                        f"{msg.manifest_id}, duration {msg.format.target_duration_sec})"
                    )
            case msgtypes.StringMessage():
                self.append_message(msg.text)
            case msgtypes.StreamMuxProgressMessage():
                self.manifest_progress[msg.manifest_id].output = msg.progress
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
            task = asyncio.create_task(self.downloader.async_run())
            manager = manager_ctx.get()
            if manager:
                manager.active_tasks[self.id] = task
            try:
                await task
            except asyncio.CancelledError as exc:
                self.status = DownloadStatus.CANCELLED
                self.append_message(f"Cancelled: {str(exc)}")
                self.broadcast_status_update()
                manager = manager_ctx.get()
                if manager:
                    quart.current_app.add_background_task(manager.publish, self)
                    quart.current_app.add_background_task(manager.publish_detail, self.id, self)
            except Exception as exc:
                self.status = DownloadStatus.ERROR
                self.append_message(f"Exception: {exc=}")
                self.append_message(traceback.format_exc())
                self.broadcast_status_update()
                self.persist_to_database()
                manager = manager_ctx.get()
                if manager:
                    quart.current_app.add_background_task(manager.publish, self)
                    quart.current_app.add_background_task(manager.publish_detail, self.id, self)

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

    async def run_healthcheck(self) -> None:
        """
        Performs a health check on the source video to see if it was modified since the stream
        was downloaded.
        """
        last_healthcheck_result = self.healthcheck.result
        self.healthcheck.result = await self._fetch_health_status()
        self.healthcheck.last_update = datetime.datetime.now(tz=datetime.UTC)
        if last_healthcheck_result != self.healthcheck.result:
            # TODO: notify on status change
            pass
        manager = manager_ctx.get()
        if manager:
            quart.current_app.add_background_task(manager.publish, self)
            quart.current_app.add_background_task(manager.publish_detail, self.id, self)
        if self.status == DownloadStatus.FINISHED:
            self.persist_to_database()

    async def _fetch_health_status(self) -> HealthCheckResult:
        if not self.video_id:
            return HealthCheckResult.HEALTHCHECK_FAILURE
        elif self.downloaded_duration is None:
            return HealthCheckResult.STREAM_LENGTH_INDETERMINATE

        async with _healthcheck_rate_limiter:
            # stagger healthcheck requests to avoid tripping YouTube
            quart.current_app.logger.debug(
                f"Performing healthcheck for video ID {self.video_id}"
            )
            response = await fetch_youtube_player_response(self.video_id, False)
        if not response or not response.playability_status:
            return HealthCheckResult.HEALTHCHECK_FAILURE
        if response.playability_status.status in ("LOGIN_REQUIRED",):
            return HealthCheckResult.VIDEO_UNAVAILABLE
        if response.video_details and response.video_details.is_live_content:
            # skip checking duration on premieres
            upstream_duration = response.video_details.video_duration
            if abs(self.downloaded_duration - upstream_duration) > 1:
                estimated_duration = None
                if response.microformat and response.microformat.live_broadcast_details:
                    estimated_duration = (
                        response.microformat.live_broadcast_details.estimated_duration
                    )
                if estimated_duration and upstream_duration < estimated_duration:
                    # if response's upstream is equal to estimated, then video may not be
                    # finished processing
                    return HealthCheckResult.STREAM_LENGTH_DIFFERS
        return HealthCheckResult.OK

    async def run_scheduled_healthchecks(self) -> None:
        """
        Task to run healthchecks on a schedule.  No healthchecks will be run if the task isn't
        finished downloading.
        """
        while sleep_interval := self._get_next_healthcheck_interval():
            await asyncio.sleep(sleep_interval.total_seconds())

            cfgmgr = cfgmgr_ctx.get()
            if not cfgmgr.config.healthchecks.enable_scheduled:
                continue

            await self.run_healthcheck()

    def persist_to_database(self) -> None:
        database = database_ctx.get()
        if database:
            database.execute(
                "INSERT INTO jobs (id, payload) VALUES (?, ?) "
                "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (
                    self.id,
                    msgspec.json.encode(
                        msgspec.structs.replace(self, downloader=None),
                        enc_hook=_downloadjob_encode_hook,
                    ),
                ),
            )
            database.commit()

    def get_status(self) -> dict:
        return msgspec.to_builtins(
            msgspec.structs.replace(self, downloader=None), enc_hook=_downloadjob_encode_hook
        )

    def _get_next_healthcheck_interval(self) -> datetime.timedelta | None:
        """
        Returns the amount of time to wait between healthcheck requests, or None if no future
        healthcheck should be performed.
        """
        if not self.download_finish_datetime:
            return None
        current_time = datetime.datetime.now(tz=datetime.UTC)
        duration_since_complete = current_time - self.download_finish_datetime
        duration_map = {
            # this produces at best around 12 to 18 checks at each interval stage
            # realistically there may be fewer since checks are throttled
            datetime.timedelta(hours=1): datetime.timedelta(minutes=5),
            datetime.timedelta(hours=6): datetime.timedelta(minutes=30),
            datetime.timedelta(days=1): datetime.timedelta(hours=1),
            datetime.timedelta(days=3): datetime.timedelta(hours=4),
        }
        for offset, sleep_time in duration_map.items():
            if offset > duration_since_complete:
                return sleep_time
        return None

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
    def downloaded_duration(self) -> int | None:
        """
        Returns the downloaded stream's final muxed duration in seconds, or None if the value
        could not be determined.
        """
        if len(self.manifest_progress) != 1:
            # Multi-manifest broadcasts have overlapping segments, so we can't reliably identify
            # if an affected stream was cut with this alone.
            #
            # We *could* possibly determine this by performing analysis on the segment metadata
            # and obtaining the union of segment ingest times, but that's far too much effort
            # at this time.
            return None
        return (
            int(sum(prog.output.out_time_us or 0 for prog in self.manifest_progress.values()))
            // 1_000_000
        )

    @property
    def total_duration_timedelta(self) -> datetime.timedelta | None:
        if not len(self.manifest_progress):
            return None
        return datetime.timedelta(
            microseconds=sum(
                prog.output.out_time_us or 0 for prog in self.manifest_progress.values()
            )
        )

    @property
    def total_downloaded(self) -> int:
        """
        Returns the total number of bytes transferred.
        """
        return sum(prog.total_downloaded for prog in self.manifest_progress.values())

    @property
    def total_muxed(self) -> int:
        """
        Returns the total number of bytes in the final output.
        """
        return sum(prog.output.total_size or 0 for prog in self.manifest_progress.values())

    @property
    def has_active_task(self) -> bool:
        manager = manager_ctx.get()
        if manager and self.id in manager.active_tasks:
            return not manager.active_tasks[self.id].done()
        return False

    @property
    def can_delete_tempfiles(self) -> bool:
        if self.video_id is None:
            return False
        elif self.status == DownloadStatus.FINISHED and any(
            prog.output.total_size is None for prog in self.manifest_progress.values()
        ):
            # if any manifests have skipped outputs then we don't consider it safe for removal
            return False
        return self.status.can_delete_tempfiles
