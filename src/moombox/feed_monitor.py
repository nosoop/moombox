#!/usr/bin/python3

import asyncio
import collections
import itertools
import pathlib
import re
import sqlite3
import typing
import unicodedata
from contextvars import ContextVar

import aiolimiter
import feedparser  # type: ignore
import httpx
import quart
import unidecode as unidec
from moonarchive.downloaders.youtube import YouTubeDownloader

from .config import PatternMap, YouTubeChannelMonitorConfig, cfgmgr_ctx
from .database import database_ctx
from .extractor import fetch_youtube_player_response
from .notifications import apobj_ctx
from .tasks import DownloadStatus, manager_ctx

# used to ensure single characters that are spaced out are merged
# https://stackoverflow.com/a/24200646
_compress_spaces = re.compile(r"(?i)(?<=\b[a-z])\s+(?=[a-z]\b)")

_db_cursor_ctx: ContextVar[sqlite3.Cursor] = ContextVar("db_cursor")


def strip_marks(text: str) -> str:
    """
    Strips combining characters and accent marks commonly used as part of 'zalgo' text.
    Note that as the focus is on locating keywords in user-defined text, this function makes no
    attempt at preserving internationalization.

    https://www.npmjs.com/package/unzalgo#user-content-how-does-it-work
    """
    return "".join(c for c in text if unicodedata.category(c) not in ("Mn", "Me"))


def get_pattern_matches(pattern_map: PatternMap, input: str) -> set[str]:
    # returns any matched terms in the given input
    # the matcher here also tries and match exotic character substitutions
    haystacks = (
        input,
        unidec.unidecode(input),
        _compress_spaces.sub("", unidec.unidecode(input)),
        strip_marks(input),
    )
    return {
        term
        for term, pattern in pattern_map.items()
        if any(pattern.search(haystack) for haystack in haystacks)
    }


def _sliding_window(
    iterable: typing.Iterable, n: int = 2
) -> typing.Iterable[tuple[typing.Any, ...]]:
    it = iter(iterable)
    window = collections.deque(itertools.islice(it, n), maxlen=n)
    if len(window) == n:
        yield tuple(window)
    for x in it:
        window.append(x)
        yield tuple(window)


class FeedItemMatch(typing.NamedTuple):
    channel_config: YouTubeChannelMonitorConfig
    url: str
    video_id: str
    author: str
    matching_terms: set[str]

    @property
    def display_author(self) -> str:
        return self.channel_config.name or self.author


# limit the number of simultaneous download processes
download_sem = asyncio.Semaphore(3)

# limit the rate at which we make player requests
_player_request_limiter = aiolimiter.AsyncLimiter(1, 20)


async def get_channel_matches(channel: YouTubeChannelMonitorConfig) -> list[FeedItemMatch]:
    matches = []
    async with download_sem, httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://www.youtube.com/feeds/videos.xml?channel_id={channel.id}"
        )
        feed = feedparser.parse(resp.text)

    for entry, *older_entries in _sliding_window(feed.entries, channel.num_desc_lookbehind):
        # filter out lines that are present in the next item in the feed
        # intended to reduce false positives if a match shows up as part of the 'template'
        # descriptions may have trailing spaces at times; compact those
        older_item_lines = set()
        for it in older_entries:
            older_item_lines |= {line.rstrip() for line in it.summary.splitlines()}

        # we don't do set subtraction here since we want to preserve order in the original description
        summary_unique_lines = "\n".join(
            line.rstrip()
            for line in entry.summary.splitlines()
            if line.rstrip() not in older_item_lines
        )

        matching_terms = set()
        for haystack in (entry.title, summary_unique_lines):
            matching_terms |= get_pattern_matches(channel.terms, haystack)

        if matching_terms:
            matches.append(
                FeedItemMatch(
                    channel, entry.link, entry.yt_videoid, entry.author, matching_terms
                )
            )
        # recheck negative results in case they match later on
    return matches


async def schedule_feed_match(match: FeedItemMatch) -> None:
    manager = manager_ctx.get()
    if not manager:
        return

    # skip scheduling if it's already active
    # this should catch all downloads that aren't manually cleared
    if any(
        job.video_id == match.video_id and job.status not in (DownloadStatus.UNAVAILABLE,)
        for job in manager.jobs.values()
    ):
        return

    cur = _db_cursor_ctx.get()
    cur.execute("SELECT EXISTS(SELECT 1 FROM video_history WHERE id = ?)", (match.video_id,))
    (video_in_history,) = cur.fetchone()
    if video_in_history:
        return

    # throttle matches that involve player requests
    async with _player_request_limiter:
        resp = await fetch_youtube_player_response(match.video_id)
    if not resp or not resp.video_details:
        # video is unavailable; skip adding but allow for rechecks
        return
    elif not (
        (
            resp.video_details.is_post_live_dvr
            or resp.video_details.is_upcoming
            or resp.video_details.is_live
        )
        and (
            resp.video_details.is_live_content or match.channel_config.include_non_live_content
        )
    ):
        # add IDs that can no longer be downloaded into history so we know not to recheck them
        cur.execute("INSERT OR IGNORE INTO video_history VALUES (?);", (match.video_id,))
        return

    cfgmgr = cfgmgr_ctx.get()

    output_directory = (
        match.channel_config.output_directory
        or cfgmgr.config.downloader.output_directory
        or pathlib.Path("output")
    )
    try:
        output_directory.mkdir(parents=True, exist_ok=True)
    except (ValueError, OSError):
        pass

    # many of these parameters are modified in tasks.py @ DownloadManager.create_job
    downloader = YouTubeDownloader(
        url=match.url,
        poll_interval=300,
        ffmpeg_path=None,
        write_description=True,
        write_thumbnail=True,
        output_directory=output_directory,
        staging_directory=None,
        prioritize_vp9=True,
        cookie_file=None,
        num_parallel_downloads=cfgmgr.config.downloader.num_parallel_downloads,
    )

    job = manager.create_job(downloader)

    job.video_id = resp.video_details.video_id
    job.author = resp.video_details.author
    job.channel_id = resp.video_details.channel_id
    job.thumbnail_url = next(
        (thumb.url for thumb in sorted(resp.video_details.thumbnails, reverse=True)),
        None,
    )
    if resp.playability_status:
        job.scheduled_start_datetime = resp.playability_status.scheduled_start_datetime

    job.append_message(f"Found stream with matching terms: {', '.join(match.matching_terms)}")
    quart.current_app.add_background_task(job.run)

    apobj = apobj_ctx.get()
    if not apobj:
        return
    await apobj.async_notify(
        body=f"{match.display_author} is doing a stream matching: "
        f"{', '.join(match.matching_terms)} @ https://youtu.be/{resp.video_details.video_id}",
        tag="monitor-feed:found",
    )


async def monitor_daemon() -> None:
    quart.current_app.logger.info("Monitoring task started")
    cfgmgr = cfgmgr_ctx.get()
    if not cfgmgr:
        raise RuntimeError("Configuration manager is unavailable in current context")

    modified_flag = cfgmgr.get_modified_flag()
    modified_flag.clear()

    database = database_ctx.get()
    if not database:
        raise RuntimeError("Database is unavailable in current context")

    database.execute("CREATE TABLE IF NOT EXISTS video_history (id TEXT UNIQUE);")
    database.commit()

    _db_cursor_ctx.set(database.cursor())

    while True:
        while not cfgmgr.config.channels:
            # suspend feed monitoring while we don't have any channels to monitor
            quart.current_app.logger.warning(
                "No channels for monitoring; feed polling suspended - add channels by specifying '[[channels]]' sections"
            )
            await modified_flag.wait()
            modified_flag.clear()

        tasks = [get_channel_matches(c) for c in cfgmgr.config.channels]

        for task in asyncio.as_completed(tasks):
            try:
                matches = await task
            except httpx.HTTPError:
                # TODO: we need to retry this
                pass
            else:
                for match in matches:
                    await schedule_feed_match(match)

        database.commit()
        await asyncio.sleep(600)
