#!/usr/bin/python3

import asyncio
import base64
import datetime
import functools
import html.parser
import json
import pathlib
import urllib.parse
from typing import Type
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
    length_seconds: str
    thumbnail: YouTubeVideoThumbnailList
    is_live: bool | None = (
        False  # whether or not the content is currently being shown in real-time
    )
    is_live_content: bool = True
    """
    Whether or not this video was a live broadcast.  Default to True if not present just to
    ensure we don't run into false negatives if the field no longer exists upstream.
    """

    is_upcoming: bool | None = False  # if a future stream (including waiting for streamer)
    is_post_live_dvr: bool | None = False  # if recently finished (fragments available)

    @property
    def video_duration(self) -> int:
        return int(self.length_seconds)

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
    status: str
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


class YouTubeVideoBroadcastDetails(msgspec.Struct, rename="camel"):
    start_timestamp: datetime.datetime | None = None
    end_timestamp: datetime.datetime | None = None
    is_live_now: bool = False

    @property
    def estimated_duration(self) -> int | None:
        if not self.start_timestamp:
            return None
        elif not self.end_timestamp:
            return None
        return int((self.end_timestamp - self.start_timestamp).total_seconds())


class YouTubeVideoMicroformatRenderer(msgspec.Struct, rename="camel"):
    live_broadcast_details: YouTubeVideoBroadcastDetails | None = None


class YouTubeVideoMicroformat(msgspec.Struct, rename="camel"):
    player_microformat_renderer: YouTubeVideoMicroformatRenderer | None = None

    def __getattr__(self, name: str):
        # proxy attribute accesses to the renderer
        return getattr(self.player_microformat_renderer, name)

    def __bool__(self) -> bool:
        return bool(self.player_microformat_renderer)


class YouTubePlayerResponse(msgspec.Struct, rename="camel"):
    video_details: YouTubeVideoDetails | None = None
    playability_status: YouTubePlayabilityStatus | None = None
    microformat: YouTubeVideoMicroformat | None = None


class YouTubeClientConfig(msgspec.Struct, rename="camel", kw_only=True):
    delegated_session_id: str | None = msgspec.field(name="DELEGATED_SESSION_ID", default=None)
    id_token: str | None = msgspec.field(name="ID_TOKEN", default=None)
    hl: str = msgspec.field(name="HL")
    innertube_api_key: str = msgspec.field(name="INNERTUBE_API_KEY")
    innertube_client_name: str = msgspec.field(name="INNERTUBE_CLIENT_NAME")
    innertube_client_version: str = msgspec.field(name="INNERTUBE_CLIENT_VERSION")
    innertube_ctx_client_name: int = msgspec.field(name="INNERTUBE_CONTEXT_CLIENT_NAME")
    innertube_ctx_client_version: str = msgspec.field(name="INNERTUBE_CONTEXT_CLIENT_VERSION")
    session_index: str | None = msgspec.field(name="SESSION_INDEX", default=None)
    visitor_data: str = msgspec.field(name="VISITOR_DATA")

    def to_headers(self) -> dict[str, str]:
        headers = {
            "X-YouTube-Client-Name": str(self.innertube_ctx_client_name),
            "X-YouTube-Client-Version": self.innertube_client_version,
        }
        if self.visitor_data:
            headers["X-Goog-Visitor-Id"] = self.visitor_data
        if self.session_index:
            headers["X-Goog-AuthUser"] = self.visitor_data
        if self.delegated_session_id:
            headers["X-Goog-PageId"] = self.delegated_session_id
        if self.id_token:
            headers["X-Youtube-Identity-Token"] = self.id_token
        return headers

    def to_post_context(self) -> dict[str, str]:
        post_context = {
            "clientName": self.innertube_client_name,
            "clientVersion": self.innertube_client_version,
        }
        if self.visitor_data:
            post_context["visitorData"] = self.visitor_data
        return post_context


def create_json_object_extractor(decl: str) -> Type[html.parser.HTMLParser]:
    class InternalHTMLParser(html.parser.HTMLParser):
        in_script: bool = False
        result = None

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            self.in_script = tag == "script"

        def handle_endtag(self, tag: str) -> None:
            self.in_script = False

        def handle_data(self, data: str) -> None:
            if not self.in_script:
                return

            decl_pos = data.find(decl)
            if decl_pos == -1:
                return

            # we'll just let the decoder throw to determine where the data ends
            start_pos = data[decl_pos:].find("{") + decl_pos
            try:
                self.result = json.loads(data[start_pos:])
            except json.JSONDecodeError as e:
                self.result = json.loads(data[start_pos : start_pos + e.pos])

    return InternalHTMLParser


PlayerResponseExtractor = create_json_object_extractor("var ytInitialPlayerResponse =")
YTCFGExtractor = create_json_object_extractor('ytcfg.set({"CLIENT')

_ytcfg_cache: YouTubeClientConfig | None = None
_ytcfg_cache_dt: datetime.datetime | None = None


async def _extract_yt_cfg() -> YouTubeClientConfig:
    # scrapes the home page and returns a current YouTubeClientConfig
    response_extractor = YTCFGExtractor()
    async with httpx.AsyncClient(follow_redirects=True) as client:
        for n in range(5):
            try:
                r = await client.get("https://youtube.com/")
                response_extractor.feed(r.text)
                break
            except httpx.HTTPError:
                await asyncio.sleep(6)

        if not response_extractor.result:  # type: ignore
            raise ValueError("Could not extract YouTubeClientConfig response")
        return msgspec.convert(response_extractor.result, type=YouTubeClientConfig)  # type: ignore


async def _get_yt_cfg() -> YouTubeClientConfig:
    # attempts to retrieve the latest available web client information
    # here we don't care about proof-of-origin data since YouTube will still offer video info
    # without it
    global _ytcfg_cache
    global _ytcfg_cache_dt

    max_age = datetime.timedelta(hours=4)
    now = datetime.datetime.now(tz=datetime.UTC)
    if _ytcfg_cache and _ytcfg_cache_dt and now - _ytcfg_cache_dt < max_age:
        return _ytcfg_cache
    _ytcfg_cache = await _extract_yt_cfg()
    _ytcfg_cache_dt = now  # off by however long extraction takes but relatively small
    return _ytcfg_cache


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


async def fetch_youtube_player_response(
    video_id: str, validate: bool = True
) -> YouTubePlayerResponse | None:
    ytcfg = await _get_yt_cfg()
    params = {
        "key": ytcfg.innertube_api_key or base64.urlsafe_b64decode(INNERTUBE_ANDROID_KEY_ENC),
    }

    headers = {
        "X-YouTube-Client-Name": "1",
        "X-YouTube-Client-Version": "2.20241121.01.00",
        "Origin": "https://www.youtube.com",
        "content-type": "application/json",
    } | ytcfg.to_headers()

    payload = {
        "context": {
            "client": {
                "clientName": "WEB",
                "clientVersion": "2.20241121.01.00",
                "hl": "en",
            }
            | ytcfg.to_post_context()
        },
        "videoId": video_id,
        "playbackContext": {"contentPlaybackContext": {"html5Preference": "HTML5_PREF_WANTS"}},
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
            if not validate:
                # normally we fetch the response for upcoming or currently live streams
                # bypassing validation means we return a response for private videos too
                return response
            if response.video_details and response.video_details.video_id == video_id:
                return response
            await asyncio.sleep(10)
    return None
