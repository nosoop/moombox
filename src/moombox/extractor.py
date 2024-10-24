#!/usr/bin/python3

import asyncio
import base64
import datetime
import functools
import pathlib
import urllib.parse
from urllib.parse import ParseResult as URLParseResult

import httpx
import msgspec
import tldextract
from tldextract.tldextract import ExtractResult as DomainParseResult

# this currently duplicates a decent amount of the logic from moonarchive; not sure if we want
# to merge the implementations in one way or another, or rely on an innertube module in the
# future

# the android key here is base64 encoded (urlsafe) to lightly obfuscate from secret scanning
# this is a publicly known key
INNERTUBE_ANDROID_KEY_ENC = b"QUl6YVN5QU9fRkoyU2xxVThRNFNURUhMR0NpbHdfWTlfMTFxY1c4"


@functools.total_ordering
class YouTubeVideoThumbnail(msgspec.Struct, rename="camel"):
    width: int
    height: int
    url: str

    def __eq__(self, other: object):
        if not isinstance(other, YouTubeVideoThumbnail):
            raise NotImplementedError
        return (self.width, self.height) == (other.width, other.height)

    def __lt__(self, other: object):
        if not isinstance(other, YouTubeVideoThumbnail):
            raise NotImplementedError
        return (self.width, self.height) < (other.width, other.height)


class YouTubeVideoThumbnailList(msgspec.Struct, rename="camel"):
    thumbnails: list[YouTubeVideoThumbnail]


class YouTubeVideoDetails(msgspec.Struct, rename="camel"):
    video_id: str
    title: str
    author: str
    channel_id: str
    thumbnail: YouTubeVideoThumbnailList
    is_live: bool | None = (
        False  # whether or not the content is currently being shown in real-time
    )
    is_live_content: bool | None = False  # whether or not this video was a live broadcast
    is_upcoming: bool | None = False  # if a future stream

    @property
    def thumbnails(self) -> list[YouTubeVideoThumbnail]:
        return self.thumbnail.thumbnails


class YouTubeLiveStreamOfflineSlateRenderer(msgspec.Struct, rename="camel"):
    scheduled_start_time: str | None = None

    @property
    def scheduled_start_datetime(self) -> datetime.datetime | None:
        if not self.scheduled_start_time:
            return None
        try:
            return datetime.datetime.fromtimestamp(
                int(self.scheduled_start_time), tz=datetime.UTC
            )
        except ValueError:
            pass
        return None


class YouTubeOfflineSlate(msgspec.Struct, rename="camel"):
    live_stream_offline_slate_renderer: YouTubeLiveStreamOfflineSlateRenderer | None = None


class YouTubeLiveStreamabilityRenderer(msgspec.Struct, rename="camel"):
    offline_slate: YouTubeOfflineSlate | None = None


class YouTubeLiveStreamability(msgspec.Struct, rename="camel"):
    live_streamability_renderer: YouTubeLiveStreamabilityRenderer | None = None


class YouTubePlayabilityStatus(msgspec.Struct, rename="camel"):
    live_streamability: YouTubeLiveStreamability | None = None

    @property
    def scheduled_start_datetime(self) -> datetime.datetime | None:
        if not self.live_streamability:
            return None
        if not self.live_streamability.live_streamability_renderer:
            return None
        if not self.live_streamability.live_streamability_renderer.offline_slate:
            return None
        if not self.live_streamability.live_streamability_renderer.offline_slate.live_stream_offline_slate_renderer:
            return None
        return self.live_streamability.live_streamability_renderer.offline_slate.live_stream_offline_slate_renderer.scheduled_start_datetime


class YouTubePlayerResponse(msgspec.Struct, rename="camel"):
    video_details: YouTubeVideoDetails | None = None
    playability_status: YouTubePlayabilityStatus | None = None


def extract_video_id_from_string(url_or_id: str) -> str | None:
    # extracts the YouTube video ID from a URL (either string or raw ID)
    # we do this to try and avoid having to scrape a page for the result

    if len(url_or_id) == 11:
        # a video ID is a base64-encoded representation of a 64-bit integer,
        # so return the input as-is if it decodes to one
        try:
            uid = int.from_bytes(base64.urlsafe_b64decode(f"{url_or_id}="))
            if uid < (1 << 64):
                return url_or_id
        except ValueError:
            pass

    results = (tldextract.extract(url_or_id), urllib.parse.urlparse(url_or_id))
    match results:
        case (_, URLParseResult(netloc="youtu.be", path=videoid)):
            # https://youtu.be/dQw4w9WgXcQ
            return videoid[1:]
        case (DomainParseResult(domain="youtube"), URLParseResult(path=p)) if any(
            p.startswith(x) for x in ("/shorts/", "/live/")
        ):
            # https://youtube.com/live/dQw4w9WgXcQ
            return pathlib.Path(urllib.request.url2pathname(p)).name
        case (DomainParseResult(domain="youtube"), URLParseResult(path="/watch", query=q)):
            # https://youtube.com/watch?v=dQw4w9WgXcQ
            return next(iter(urllib.parse.parse_qs(q).get("v", [])), None)
    return None


async def fetch_youtube_player_response(video_id: str) -> YouTubePlayerResponse | None:
    params = {
        "key": base64.urlsafe_b64decode(INNERTUBE_ANDROID_KEY_ENC),
    }

    headers = {
        "X-YouTube-Client-Name": "3",
        "X-YouTube-Client-Version": "19.09.37",
        "Origin": "https://www.youtube.com",
        "content-type": "application/json",
    }

    payload = {
        "context": {
            "client": {
                "clientName": "ANDROID",
                "clientVersion": "19.09.37",
                "hl": "en",
            }
        },
        "videoId": video_id,
        "params": "CgIQBg==",
        "playbackContext": {
            "contentPlaybackContext": {
                "html5Preference": "HTML5_PREF_WANTS",
            }
        },
        "contentCheckOk": True,
        "racyCheckOk": True,
    }

    async with httpx.AsyncClient() as client:
        # we may occasionally get null responses out of this for some reason, so try a few times
        for _ in range(10):
            r = await client.post(
                "https://www.youtube.com/youtubei/v1/player",
                params=params,
                headers=headers,
                json=payload,
            )
            response = msgspec.convert(r.json(), type=YouTubePlayerResponse)
            if response.video_details and response.video_details.video_id == video_id:
                return response
            await asyncio.sleep(10)
    return None
