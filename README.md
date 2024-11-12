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
pip install -e git+https://github.com/nosoop/moombox

# run the application with a single worker since all of the state is within the process
hypercorn moombox.app:create_app() -w 1
```

### via Docker / Podman

moombox is also available as a containerized application that can be run via Docker or Podman.

The current iteration of the container uses `ffmpeg` as it's available in Debian Bookworm.

```sh
# the working directory is set to '/data' in the application
podman run -p 5000:5000 -v /opt/moombox:/data ghcr.io/nosoop/moombox:latest
```

You must mount a writable path on the host to `/data` so moombox can generate `/data/staging`
and `/data/output` directories.  The `staging` directory should be attached to a fast storage
device as it'll be used while downloading and muxing the final file before being moved to
`/data/output`.

You can also pass `--user` to run moombox as a different user if executing `docker` or `podman`
as root.  Make sure that, if it already exists, `/data/config/database.db3` is writable.

## License

Released under the MIT license.
