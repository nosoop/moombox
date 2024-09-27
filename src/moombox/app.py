#!/usr/bin/python3

import asyncio
import dataclasses
import datetime
import enum
import os
import pathlib
import secrets
from typing import Any, AsyncGenerator

import moonarchive.models.messages as msgtypes
import quart
from hypercorn.middleware import ProxyFixMiddleware
from hypercorn.typing import ASGIFramework
from moonarchive.downloaders.youtube import YouTubeDownloader
from moonarchive.output import BaseMessageHandler

from . import extractor


@dataclasses.dataclass
class DownloadManager:
    """
    Keeps track of download jobs and passes messages to connected clients.
    """

    jobs: dict[str, "DownloadJob"] = dataclasses.field(default_factory=dict)
    connections: set[asyncio.Queue] = dataclasses.field(default_factory=set)

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


class DownloadStatus(enum.StrEnum):
    UNKNOWN = "Unknown"
    UNAVAILABLE = "Unavailable"
    WAITING = "Waiting"
    DOWNLOADING = "Downloading"
    MUXING = "Muxing"
    FINISHED = "Finished"
    ERROR = "Error"


class DownloadJob(BaseMessageHandler):
    id: str

    # downloader and manager may be omitted if this job is pulled from cache
    # or mocked for visual testing
    downloader: YouTubeDownloader | None = None
    manager: DownloadManager | None = None

    author: str | None = None
    channel_id: str | None = None
    video_id: str | None = None
    scheduled_start_datetime: datetime.datetime | None = None
    thumbnail_url: str | None = None
    current_manifest: str | None = None
    title: str | None = None
    video_seq: int = 0
    audio_seq: int = 0
    max_seq: int = 0
    total_downloaded: int = 0
    status: DownloadStatus = DownloadStatus.UNKNOWN

    async def handle_message(self, msg: msgtypes.BaseMessage) -> None:
        match msg:
            case msg if isinstance(msg, msgtypes.StreamInfoMessage):
                self.title = msg.video_title
                self.status = DownloadStatus.WAITING
            case msg if isinstance(msg, msgtypes.FragmentMessage):
                self.status = DownloadStatus.DOWNLOADING
                self.max_seq = max(self.max_seq, msg.max_fragments)
                if msg.media_type == "audio":
                    self.audio_seq = msg.current_fragment
                elif msg.media_type == "video":
                    self.video_seq = msg.current_fragment
                self.total_downloaded += msg.fragment_size
                self.current_manifest = msg.manifest_id
                self.video_id, *_ = msg.manifest_id.split(".")
            case msg if isinstance(msg, msgtypes.DownloadJobFinishedMessage):
                self.status = DownloadStatus.FINISHED
            case msg if isinstance(msg, msgtypes.DownloadJobFailedOutputMoveMessage):
                self.status = DownloadStatus.ERROR
            case msg if isinstance(msg, msgtypes.StreamMuxMessage):
                self.status = DownloadStatus.MUXING
            case msg if isinstance(msg, msgtypes.StreamUnavailableMessage):
                self.status = DownloadStatus.UNAVAILABLE
            case _:
                pass
        if self.manager:
            quart.current_app.add_background_task(self.manager.publish, self)

    async def run(self) -> None:
        if self.downloader:
            self.downloader.handlers = [self]
            await self.downloader.async_run()

    def get_status(self) -> dict:
        return {
            "id": self.video_id,
            "video_fragments": self.video_seq,
            "audio_fragments": self.audio_seq,
            "state": self.status,
        }


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

        job = DownloadJob(jobid, downloader=downloader, manager=manager)

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

    @app.get("/status")
    async def get_status() -> list[dict]:
        return [job.get_status() for job in manager.jobs.values()]

    @app.websocket("/ws")
    async def ws() -> None:
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
