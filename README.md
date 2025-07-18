# moombox

Web frontend for [moonarchive][] to manage downloads of multiple YouTube livestreams and
premieres.

Design shamelessly ripped off from [hoshinova][], an equivalent frontend for [ytarchive][].

![image](https://github.com/user-attachments/assets/4f268e8d-f553-4b14-afd8-8d3a51b5911a)

[moonarchive]: https://github.com/nosoop/moonarchive
[hoshinova]: https://github.com/HoloArchivists/hoshinova
[ytarchive]: https://github.com/Kethsar/ytarchive

## Installation

### via Python

Python 3.11 or newer is required.

```sh
python -m venv .venv
source .venv/bin/activate # or .venv\Scripts\activate.bat on Windows
pip install git+https://github.com/nosoop/moombox

# run the application with a single worker since all of the state is within the process
hypercorn moombox.app:create_app() -w 1
```

### via Docker / Podman

moombox is also available as a containerized application that can be run via Docker or Podman.
This method isn't as extensively tested, so please let me know whether or not you have problems
configuring moombox this way.

The current iteration of the container uses `ffmpeg` as it's available in Debian Bookworm.

```sh
# the working directory is set to '/data' in the application
podman run -p 5000:5000 -v /opt/moombox:/data ghcr.io/nosoop/moombox
```

You must mount a writable path on the host to `/data` so moombox can generate `/data/staging`
and `/data/output` directories.  The `staging` directory should be attached to a fast storage
device as it'll be used while downloading and muxing the final file before being moved to
`/data/output`.

You can also pass `--user` to run moombox as a different user if executing `docker` or `podman`
as root.  Make sure that, if it already exists, `/data/config/database.db3` is writable.

#### via Docker Compose

You may also run moombox using Docker Compose instead.

1. Copy [config.container.toml](config.container.toml) to `./data/config.toml` and make the
necessary user-specific modifications.
2. Start the container using the provided [docker-compose.yml][] file:
   ```sh
   docker compose up -d
   ```

[docker-compose.yml]: docker-compose.yml

## Configuration

Configuration is controlled by a `config.toml` in the instance path.  See `config.example.toml`
for documentation on the features.

If you installed moombox via Python, see [Flask's documentation on it][instance-path]
(we use Quart, but same thing).

You can set the `MOOMBOX_INSTANCE_PATH` environment variable to override the location.
The Docker / Podman releases set the instance path to `/data/config` this way, and you should
mount a folder to that location when running it (as described in the installation instructions).

[instance-path]: https://flask.palletsprojects.com/en/stable/config/#instance-folders

> [!IMPORTANT]
> These days, a proof-of-origin token is practically a requirement when downloading from
> YouTube.  [moonarchive's section on proof-of-origin downloads][pot-dl] covers this
> extensively.
> 
> It is strongly recommended to set both `downloader.po_token` and either
> `downloader.visitor_data` or `downloader.cookie_file` before downloading a file; otherwise
> YouTube may block your connection from accessing videos.  To obtain that information:
>
> - `po_token`: See [yt-dlp &rarr; PO Token for GVS][pot-gvs].
> - `visitor_data`: See the above; the value you want can be retrieved using
>   `ytcfg.get('VISITOR_DATA')` in the browser console.
> - `cookie_file`: See [yt-dlp &rarr; Exporting YouTube cookies][yt-cookies].

[pot-dl]: https://github.com/nosoop/moonarchive?tab=readme-ov-file#proof-of-origin-downloads
[pot-gvs]: https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide#po-token-for-gvs
[yt-cookies]: https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies

## License

Released under the MIT license.
