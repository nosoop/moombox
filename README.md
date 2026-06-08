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

The current iteration of the container uses `ffmpeg` as it's available in Debian Trixie.

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

1. Copy [config.container.toml](config.container.toml) to `./data/config/config.toml` and make
the necessary user-specific modifications.
2. Start the container using the provided [docker-compose.yml][] file:
   ```sh
   docker compose up -d
   ```

[docker-compose.yml]: docker-compose.yml

## Configuration

Configuration is controlled by a `config.toml` in
[the instance path, as described by Flask][instance-path].  See `config.example.toml` for
documentation on the features.

If you launch moombox without a configuration file, the "Configuration" tab in the web interface
will tell you which location it expects one in.

You can set the `MOOMBOX_INSTANCE_PATH` environment variable to override the location.
The Docker / Podman releases set the instance path to `/data/config` this way, and you should
mount a folder to that location when running it (as described in the installation instructions).

[instance-path]: https://flask.palletsprojects.com/en/stable/config/#instance-folders

> [!IMPORTANT]
> You will need additional software configured under `$.downloader`.  Specifcally, the following
> options are required:
>
> - `unstable_bgutil_pot_provider_url`: See [content-based tokens].
> - `unstable_cipher_solver_url`: See [n-parameter solving].
>
> Note that (still) the options may change.  It's likely that backwards compatibility will be
> maintained such that those options will work in the future.
>
> The following value is optional; mainly only relevant if you are interested in downloading
> members-only content.
>
> - `cookie_file`: See [yt-dlp &rarr; Exporting YouTube cookies][yt-cookies].

[content-based tokens]: https://github.com/nosoop/moonarchive#content-based-tokens
[n-parameter solving]: https://github.com/nosoop/moonarchive#n-parameter-solving
[yt-cookies]: https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies

## License

Released under the MIT license.
