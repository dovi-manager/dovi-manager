# Third-party notices

The container image is derived from
[`cryptochrome/dovi_convert:8.2.0`](https://hub.docker.com/r/cryptochrome/dovi_convert),
which includes `dovi_convert` and its media-processing dependencies.

`dovi_convert` is Copyright (C) 2025-2026 cryptochrome and is licensed under
the GNU General Public License, version 3 or later. Its corresponding source,
license text, and release history are available at:

- <https://github.com/cryptochrome/dovi_convert/tree/v8.2.0>
- <https://github.com/cryptochrome/dovi_convert/releases/tag/v8.2.0>

The unmodified `dovi_convert.py` source and GPL license supplied by the
upstream image remain present inside the built image under `/app`.

Other installed software retains its respective upstream license.

## Bundled web fonts

The web UI includes subsetted copies of Inter and JetBrains Mono sourced from
the Google Fonts repository:

- Inter: <https://github.com/google/fonts/tree/main/ofl/inter>
- JetBrains Mono: <https://github.com/google/fonts/tree/main/ofl/jetbrainsmono>

Both font families are distributed under the SIL Open Font License, Version
1.1. The license text is available at
<https://openfontlicense.org/open-font-license-official-text/>.
