# Credits and licences

ShackLog stands on the work of other people. Thank you.

## OFFgrid-SDR

The **SDR monitor** is built around
[OFFgrid-SDR](https://github.com/diddy-boy/OFFgrid-SDR) by **diddy-boy**: a complete
RTL-SDR receiver (FM, AM, SSB, CW, airband, shortwave, RDS, Morse and weather-fax decoders)
in a single offline web page, driven over WebUSB.

ShackLog **does not include or modify OFFgrid-SDR**. You download it from its author and
ShackLog serves your copy from your own computer, beside its logbook. All credit for the
receiver goes to its author. If you find it useful, consider supporting them through the
link on the project page.

OFFgrid-SDR itself credits:

| Component | Author | Licence |
|---|---|---|
| @jtarrio/webrtlsdr: WebUSB driver for the RTL2832U and its tuners | Jacobo Tarrío | Apache 2.0 |
| @jtarrio/signals: signal processing and demodulators | Jacobo Tarrío | Apache 2.0 |
| Radio Receiver: the Chrome app these libraries come from | Google | |
| rtl-sdr: the research that made these sticks SDRs | Osmocom | |
| V4 hardware and open driver work | RTL-SDR Blog | |
| lamejs: MP3 encoder | LAME port | LGPL |
| Barlow Semi Condensed, Share Tech Mono fonts | | SIL Open Font License |
| esbuild: bundling into one offline file | | |
| Zadig: WinUSB driver installer for Windows | Pete Batard | |
| Shortwave schedules (Identify menu) | Shortwave.Live | |
| Mystery-station frequencies | Priyom | |

See the OFFgrid-SDR repository and its `licenses` folder for the full texts.

## Included in ShackLog

| Component | Use | Licence |
|---|---|---|
| [htmx](https://htmx.org) 2 | partial page updates | BSD Zero-Clause |
| [Leaflet](https://leafletjs.com) 1.9 | the map | BSD 2-Clause |
| [Atkinson Hyperlegible](https://www.brailleinstitute.org/freefont/) | typeface (tells 0/O and 1/l/I apart) | SIL Open Font License 1.1 |
| [Flask](https://flask.palletsprojects.com), Werkzeug, Jinja | web framework | BSD 3-Clause |
| [SQLAlchemy](https://www.sqlalchemy.org) | database layer | MIT |
| [SQLite](https://sqlite.org) | the database | public domain |
| [waitress](https://docs.pylonsproject.org/projects/waitress/) | web server | ZPL 2.1 |
| [platformdirs](https://github.com/tox-dev/platformdirs) | data folder per OS | MIT |
| [pypdf](https://github.com/py-pdf/pypdf) | text inside PDF manuals | BSD 3-Clause |
| [Python-Markdown](https://python-markdown.github.io) | notes | BSD 3-Clause |
| [pystray](https://github.com/moses-palmer/pystray) (Windows) | tray icon | LGPL 3 |
| [Pillow](https://python-pillow.org) (Windows) | tray icon image | MIT-CMU |
| [PyInstaller](https://pyinstaller.org) | builds the executable | GPL 2 with bootloader exception |

Licence texts of the vendored web files are in `shacklog/web/static/vendor/`.

Map tiles © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors, loaded
from the internet when you open the map.

## Data formats

ADIF is maintained by the [ADIF Developers Group](https://adif.org). CHIRP channel lists
follow [CHIRP](https://chirpmyradio.com)'s CSV layout; ShackLog reads CHIRP files but
does not include CHIRP. Touchstone (`.s1p`) is the format NanoVNA and other analysers
export.
