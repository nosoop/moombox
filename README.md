# moombox

Web frontend for [moonarchive][] to manage downloads of multiple YouTube livestreams and
premieres.

Design shamelessly ripped off from [hoshinova][], an equivalent frontend for [ytarchive][].

![image](https://github.com/user-attachments/assets/4f268e8d-f553-4b14-afd8-8d3a51b5911a)

[moonarchive]: https://github.com/nosoop/moonarchive
[hoshinova]: https://github.com/HoloArchivists/hoshinova
[ytarchive]: https://github.com/Kethsar/ytarchive

## Installation

Right now only source installation is provided.

Python 3.11 or newer is required.

```sh
python -m venv .venv
source .venv/bin/activate # or .venv\Scripts\activate.bat on Windows
pip install git+https://github.com/nosoop/moombox

# run the application with a single worker since all of the state is within the process
hypercorn moombox.app:create_app() -w 1
```

## License

Released under the MIT license.
