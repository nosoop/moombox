#!/usr/bin/python3

import os
import pathlib
import sqlite3

import msgspec
import quart
from hypercorn.middleware import ProxyFixMiddleware
from hypercorn.typing import ASGIFramework
from moonarchive.downloaders.youtube import YouTubeDownloader

from . import extractor
from .config import ConfigManager
from .database import database_ctx
from .feed_monitor import monitor_daemon
from .notifications import NotificationManager
from .tasks import (
    DownloadJob,
    DownloadManager,
    DownloadStatus,
    downloadjob_decode_hook,
    manager_ctx,
)


async def _update_job_details(job: DownloadJob, video_id: str) -> None:
    video_response = await extractor.fetch_youtube_player_response(video_id)
    if not video_response:
        return
    if video_response.video_details:
        job.video_id = video_response.video_details.video_id
        job.author = video_response.video_details.author
        job.channel_id = video_response.video_details.channel_id
        job.thumbnail_url = next(
            (
                thumb.url
                for thumb in sorted(video_response.video_details.thumbnails, reverse=True)
            ),
            None,
        )
    if video_response.playability_status:
        job.scheduled_start_datetime = (
            video_response.playability_status.scheduled_start_datetime
        )


def create_quart_app(test_config: dict | None = None) -> quart.Quart:
    """
    Creates the Quart app.  This exposes additional methods that are not available under
    ASGIFramework.
    """
    app = quart.Quart(
        __name__,
        instance_path=os.getenv("MOOMBOX_INSTANCE_PATH"),
        instance_relative_config=True,
    )
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

    cfgmgr = ConfigManager(pathlib.Path(app.instance_path) / "config.toml")
    app.logger.setLevel(cfgmgr.config.log_level)

    notificationmgr = NotificationManager()

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
            manager.jobs[id] = msgspec.json.decode(
                previous_job, type=DownloadJob, dec_hook=downloadjob_decode_hook
            )
            app.logger.debug(f"Loaded job {id} from cache.")
        except msgspec.DecodeError as exc:
            app.logger.warning(f"Error loading job {id} from cache: {exc}")

    @app.before_serving
    async def startup() -> None:
        app.add_background_task(monitor_daemon)
        app.add_background_task(notificationmgr.run)
        app.add_background_task(cfgmgr.monitor_path)

    @app.route("/")
    async def main() -> str:
        return await quart.render_template(
            "index.html",
            download_manager=manager.visible_jobs,
            cfgmgr=cfgmgr,
        )

    @app.post("/add")
    async def add_video() -> str:
        form = await quart.request.form

        target = form["url"]

        video_id = extractor.extract_video_id_from_string(target)
        if not video_id:
            # we should probably raise a validation error here...
            pass
        else:
            # rewrite the target so we don't fetch it with unnecessary tracking params
            target = f"https://youtu.be/{video_id}"

        output_directory = cfgmgr.config.downloader.output_directory or pathlib.Path("output")
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
            staging_directory=None,
            output_directory=output_directory,
            prioritize_vp9=form.get("prefer_vp9", False, type=bool),
            cookie_file=None,
            num_parallel_downloads=form.get("num_jobs", 1, type=int),
        )

        job = manager.create_job(downloader)

        if video_id:
            quart.current_app.add_background_task(_update_job_details, job, video_id)

        quart.current_app.add_background_task(job.run)

        return await quart.render_template(
            "video_table.html",
            download_manager=manager.visible_jobs,
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

    @app.post("/job/<id>/healthcheck")
    async def request_status_update(id: str) -> str:
        if id not in manager.jobs:
            quart.abort(404, "Task not found")
        job = manager.jobs[id]
        quart.current_app.add_background_task(job.run_healthcheck)
        return ""

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

    @app.get("/status/<id>")
    async def get_status_of_job(id: str) -> dict:
        if id not in manager.jobs:
            quart.abort(404, "Task not found")
        return manager.jobs[id].get_status()

    @app.websocket("/ws/overview")
    async def stream_overview() -> None:
        await quart.render_template(
            "video_table.html",
            download_manager=manager.visible_jobs,
        )
        async for message in manager.subscribe():
            await quart.websocket.send(
                await quart.render_template("video_item.html", video_item=message)
            )

    @app.put("/config")
    async def update_config() -> str:
        try:
            form = await quart.request.form
            cfgmgr.save_config(form["config"])
            message = "Changes saved"
        except (msgspec.ValidationError, msgspec.DecodeError) as exc:
            quart.current_app.logger.error(exc)
            message = str(exc)
        return await quart.render_template(
            "panel_config_apply.html", cfgmgr=cfgmgr, config_message=message
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
