#!/usr/bin/python3

import asyncio
import collections
import dataclasses
import datetime
import enum
import functools
import os
import pathlib
import secrets
import sqlite3
from contextvars import ContextVar
from typing import Any, AsyncGenerator, NamedTuple

import moonarchive.models.messages as msgtypes
import msgspec
import quart
from hypercorn.middleware import ProxyFixMiddleware
from hypercorn.typing import ASGIFramework
from moonarchive.downloaders.youtube import YouTubeDownloader
from moonarchive.models.youtube_player import YTPlayerMediaType
from moonarchive.output import BaseMessageHandler

from . import extractor
from .database import database_ctx


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
                self.append_message(f"Exception: {exc=}")

    def append_message(self, message: str) -> None:
        self.message_log.append(
            DownloadLogMessage(datetime.datetime.now(tz=datetime.UTC), message)
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


def create_quart_app(test_config: dict | None = None) -> quart.Quart:
    """
    Creates the Quart app.  This exposes additional methods that are not available under
    ASGIFramework.
    """
    app = quart.Quart(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY="dev",
    )

    if test_config is None:
        # load the instance config, if it exists, when not testing
        app.config.from_pyfile("config.py", silent=True)
    else:
        # load the test config if passed in
        app.config.from_mapping(test_config)

    # ensure the instance folder exists
    try:
        os.makedirs(app.instance_path)
    except OSError:
        pass

    # report location of instance path in debug mode
    if app.config.get("DEBUG"):
        print(f" * Application instance path set to {os.path.abspath(app.instance_path)}")

    manager = DownloadManager()
    manager_ctx.set(manager)

    database = sqlite3.connect(pathlib.Path(app.instance_path) / "database.db3")
    database.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, payload TEXT)")
    database.commit()
    database_ctx.set(database)

    cur = database.cursor()
    cur.execute("SELECT id, payload FROM jobs")
    for id, previous_job in cur.fetchall():
        try:
            # the format is currently unstable and may change in the future
            #
            # we do not provide any compatibility guarantees across versions, but we never clear
            # out the jobs from the database so they effectively will just be hidden
            manager.jobs[id] = msgspec.json.decode(previous_job, type=DownloadJob)
            app.logger.info(f"Loaded job {id} from cache.")
        except msgspec.DecodeError as exc:
            app.logger.warning(f"Error loading job {id} from cache: {exc}")

    @app.route("/")
    async def main() -> str:
        return await quart.render_template(
            "index.html",
            download_manager=manager.jobs.values(),
        )

    @app.post("/add")
    async def add_video() -> str:
        form = await quart.request.form

        jobid = secrets.token_urlsafe(8)

        target = form["url"]

        video_id = extractor.extract_video_id_from_string(target)
        if not video_id:
            # we should probably raise a validation error here...
            pass

        output_directory = pathlib.Path("output")
        try:
            requested_output_directory = form.get("path", pathlib.Path(), type=pathlib.Path)
            if not requested_output_directory.is_absolute():
                # root relative directories relative to our base output
                # absolute paths are currently permitted
                requested_output_directory = output_directory / requested_output_directory

            # test the validity of the directory by creating it if it doesn't exist
            requested_output_directory.mkdir(parents=True, exist_ok=True)
            output_directory = requested_output_directory
        except (ValueError, OSError):
            pass

        downloader = YouTubeDownloader(
            url=target,
            poll_interval=300,
            ffmpeg_path=None,
            write_description=form.get("download_description", False, type=bool),
            write_thumbnail=form.get("download_thumbnail", False, type=bool),
            staging_directory=pathlib.Path("staging") / jobid,
            output_directory=output_directory,
            prioritize_vp9=form.get("prefer_vp9", False, type=bool),
            cookie_file=None,
            num_parallel_downloads=form.get("num_jobs", 1, type=int),
        )

        job = DownloadJob(jobid, downloader=downloader)

        if video_id:
            video_response = await extractor.fetch_youtube_player_response(video_id)
            if video_response:
                if video_response and video_response.video_details:
                    job.video_id = video_response.video_details.video_id
                    job.author = video_response.video_details.author
                    job.channel_id = video_response.video_details.channel_id
                    job.thumbnail_url = next(
                        (
                            thumb.url
                            for thumb in sorted(
                                video_response.video_details.thumbnails, reverse=True
                            )
                        ),
                        None,
                    )
                if video_response.playability_status:
                    job.scheduled_start_datetime = (
                        video_response.playability_status.scheduled_start_datetime
                    )

        manager.jobs[jobid] = job
        quart.current_app.add_background_task(job.run)

        return await quart.render_template(
            "video_table.html",
            download_manager=manager.jobs.values(),
        )

    @app.get("/job/<id>")
    async def view_job_info(id: str) -> str:
        if id not in manager.jobs:
            quart.abort(404, "Task not found")
        return await quart.render_template(
            "video_job.html",
            video_item=manager.jobs[id],
        )

    @app.delete("/job/<id>/tempfiles")
    async def delete_job_tempfiles(id: str) -> str:
        if id not in manager.jobs:
            quart.abort(404, "Task not found")
        job = manager.jobs[id]
        if not job.downloader:
            quart.abort(404, "Downloader not present")
        if not job.can_delete_tempfiles:
            quart.abort(400, "Cannot delete temporary files on an unfinished job")
        assert job.video_id
        if job.downloader.staging_directory and job.downloader.staging_directory.exists():
            # avoid touching files not related to the job
            # TODO: get the actual list of downloaded files from moonarchive
            # TODO: disable functionality if files need to be manually processed
            for f in job.downloader.staging_directory.iterdir():
                if f.name.startswith(job.video_id):
                    f.unlink()
            try:
                job.downloader.staging_directory.rmdir()
            except OSError:
                pass
        return await quart.render_template(
            "video_job.html",
            video_item=manager.jobs[id],
        )

    @app.websocket("/ws/job/<id>")
    async def stream_job_info(id: str) -> None:
        if id not in manager.jobs:
            quart.abort(404, "Task not found")
        await quart.websocket.send(
            await quart.render_template("video_job_details.html", video_item=manager.jobs[id])
        )
        async for message in manager.subscribe_detail(id):
            await quart.websocket.send(
                await quart.render_template("video_job_details.html", video_item=message)
            )

    @app.get("/status")
    async def get_status() -> list[dict]:
        return [job.get_status() for job in manager.jobs.values()]

    @app.websocket("/ws/overview")
    async def stream_overview() -> None:
        await quart.render_template(
            "video_table.html",
            download_manager=manager.jobs.values(),
        )
        async for message in manager.subscribe():
            await quart.websocket.send(
                await quart.render_template("video_item.html", video_item=message)
            )

    @app.template_filter("human_size")
    def _sizeof_fmt(num: int | float, suffix: str = "B") -> str:
        # https://stackoverflow.com/a/1094933
        for unit in ("", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"):
            if abs(num) < 1024.0:
                return f"{num:3.2f}{unit}{suffix}"
            num /= 1024.0
        return f"{num:.2f}Yi{suffix}"

    return app


def create_app(test_config: dict | None = None) -> ASGIFramework:
    """
    Creates an app configured for production use via Hypercorn.
    """
    app = create_quart_app(test_config)

    if app.config.get("PROXY_FIX_OPTS") is not None:
        # apply proxy fixing middleware if PROXY_FIX_OPTS is present
        # this may be an empty dict
        return ProxyFixMiddleware(app, **app.config["PROXY_FIX_OPTS"])

    return app


def main() -> None:
    app = create_quart_app()
    app.run()


if __name__ == "__main__":
    main()
