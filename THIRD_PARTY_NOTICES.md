# Third-party notices

VEXT is free software under the **GNU General Public License v3.0 or later** (see [LICENSE](LICENSE)).
The distributed builds (installer and portable folder) bundle the components below, each under its own
license. The builds are GPL-3.0 as a whole because they include `pedalboard` (GPL-3.0), and every license
below is compatible with that.

Versions are the ones pinned in `requirements.txt` / `requirements-x86.txt` and `desktop/src-tauri/Cargo.lock`
at the time of the 1.0.0 build. Common license texts are at <https://spdx.org/licenses/>; the full text of
each package is also inside its wheel's `dist-info/licenses` folder and, for the PyInstaller bundles, is
reproduced from there on request (open an issue).

## Python packages (backend sidecar `vext-backend.exe`)

| Package | Version | License | Notes |
|---|---|---|---|
| [pedalboard](https://github.com/spotify/pedalboard) | 0.9.26 | GPL-3.0 | Spotify. Hosts VST3 plugins. Source: link, and the sdist on PyPI. It is the reason the builds are GPL-3.0. |
| [PyAV](https://github.com/PyAV-Org/PyAV) (`av`) | 19.0.1 | BSD-3-Clause | Bindings only. Its wheel carries FFmpeg, see "FFmpeg" below. |
| [sounddevice](https://github.com/spatialaudio/python-sounddevice) | 0.5.6 | MIT | Plus PortAudio, see below. |
| [cffi](https://github.com/python-cffi/cffi) | 2.1.1 | MIT-0 | |
| [pycparser](https://github.com/eliben/pycparser) | 3.11 | BSD-3-Clause | |
| [numpy](https://github.com/numpy/numpy) | 2.5.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | |
| [Pillow](https://github.com/python-pillow/Pillow) | 12.3.0 (64-bit), 13.0.0 (32-bit host) | MIT-CMU | |
| [pefile](https://github.com/erocarrera/pefile) | 2023.2.7 | MIT | |
| [FastAPI](https://github.com/fastapi/fastapi) | 0.115.0 | MIT | |
| [Starlette](https://github.com/encode/starlette) | 0.38.6 | BSD-3-Clause | |
| [Uvicorn](https://github.com/encode/uvicorn) | 0.32.0 | BSD-3-Clause | |
| [websockets](https://github.com/python-websockets/websockets) | 17.2 | BSD-3-Clause | |
| [Pydantic](https://github.com/pydantic/pydantic) and pydantic-core | 2.13.5 / 2.46.5 | MIT | |
| [annotated-types](https://github.com/annotated-types/annotated-types) | 0.8.0 | MIT | |
| [typing-extensions](https://github.com/python/typing_extensions) | 4.16.0 | PSF-2.0 | |
| [typing-inspection](https://github.com/pydantic/typing-inspection) | 0.4.4 | MIT | |
| [AnyIO](https://github.com/agronholm/anyio) | 4.15.1 | MIT | |
| [idna](https://github.com/kjd/idna) | 3.20 | BSD-3-Clause | |
| [Click](https://github.com/pallets/click) | 8.5.0 | BSD-3-Clause | |
| [h11](https://github.com/python-hyper/h11) | 0.16.0 | MIT | |
| [certifi](https://github.com/certifi/python-certifi) | 2026.7.22 | MPL-2.0 | Mozilla's CA bundle, unmodified. Source: link. |
| [Python](https://www.python.org/) runtime | 3.14 (64-bit) | PSF-2.0 | Embedded by PyInstaller. |
| [PyInstaller](https://github.com/pyinstaller/pyinstaller) bootloader | 6.22.3 | GPL-2.0-or-later with the bootloader exception | The exception lets PyInstaller output carry any license; here it is GPL-3.0 anyway. |

## Python packages (32-bit plugin host `host32\vext-host32.exe`)

A subset of the above: pefile, sounddevice (+ PortAudio 32-bit), numpy, Pillow, cffi, pycparser, and the
32-bit Python runtime (PSF-2.0), bundled with PyInstaller. No pedalboard and no PyAV (neither has a 32-bit
Windows build).

## FFmpeg (inside PyAV's wheel)

The Windows wheel of PyAV 19.0.1 ships FFmpeg 9 shared libraries in `av.libs` (`avcodec`, `avformat`,
`avutil`, `avfilter`, `swresample`, `swscale`, `avdevice`) next to the codec libraries they use: libx264,
libx265, libdav1d, libvpx, libopus, libsvtav1, libvmaf, libwebp, libvpl, libwinpthread, libgcc, libstdc++,
zlib and libiconv.

- The FFmpeg libraries report **LGPL version 3 or later** (`avutil_license()`), built with `--enable-version3`.
- The wheel also ships **libx264 and libx265, which are GPL-2.0-or-later**. To be safe, VEXT treats the FFmpeg
  in the build as a **GPL-3.0 combination**, which matches VEXT's own license.
- The other codec libraries are BSD-2/3-Clause (dav1d, libvpx, SVT-AV1, libwebp, libvmaf), BSD-3-Clause (opus)
  and zlib/MIT-style (zlib1, libwinpthread); libvpl is MIT; libgcc and libstdc++ carry the GCC runtime
  library exception.
- **What the license requires:** the libraries are dynamically linked (separate DLLs inside the bundle), not
  statically compiled into VEXT, and you can replace them. FFmpeg's source and the build recipe are public:
  <https://ffmpeg.org/> and <https://github.com/PyAV-Org/pyav-ffmpeg> (the project that builds PyAV's FFmpeg
  libraries). VEXT's own source is in this repository. VEXT uses FFmpeg only to decode audio files and to
  record/convert browser audio; it does not modify FFmpeg.
- FFmpeg codecs may be covered by patents in some countries (for example H.264/H.265 and MP3). VEXT does not
  encode video.

## PortAudio (via sounddevice)

`_sounddevice_data` ships PortAudio DLLs (`libportaudio64bit(-asio).dll`, `libportaudio32bit(-asio).dll`):
[PortAudio](http://www.portaudio.com/), MIT-style license. The `-asio` builds contain host API code for
Steinberg ASIO; the ASIO SDK is licensed by Steinberg under **GPL-3.0** (dual-licensed with a proprietary
license), which fits VEXT's license. "ASIO is a trademark and software of Steinberg Media Technologies GmbH."

## Rust crates (Tauri shell `VEXT.exe`)

The shell is built on [Tauri 2](https://tauri.app/) (MIT OR Apache-2.0), wry, tao and the `windows` and
`webview2-com` crates. The 254 crates linked into the Windows build are listed below with their licenses
(generated by `python tools/gen_notices.py` from `cargo metadata`; build tools are not included). No crate is
GPL or LGPL; the five MPL-2.0 crates are used unmodified (source on crates.io).

<!-- CRATES:BEGIN -->
| Crate | Version | License |
|---|---|---|
| [adler2](https://crates.io/crates/adler2/2.0.1) | 2.0.1 | 0BSD OR MIT OR Apache-2.0 |
| [aho-corasick](https://crates.io/crates/aho-corasick/1.1.5) | 1.1.5 | Unlicense OR MIT |
| [alloc-no-stdlib](https://crates.io/crates/alloc-no-stdlib/3.0.0) | 3.0.0 | BSD-3-Clause |
| [alloc-stdlib](https://crates.io/crates/alloc-stdlib/0.3.0) | 0.3.0 | BSD-3-Clause |
| [anyhow](https://crates.io/crates/anyhow/1.0.104) | 1.0.104 | MIT OR Apache-2.0 |
| [base64](https://crates.io/crates/base64/0.22.1) | 0.22.1 | MIT OR Apache-2.0 |
| [base64](https://crates.io/crates/base64/0.23.1) | 0.23.1 | MIT OR Apache-2.0 |
| [bit-set](https://crates.io/crates/bit-set/0.8.0) | 0.8.0 | Apache-2.0 OR MIT |
| [bit-vec](https://crates.io/crates/bit-vec/0.8.0) | 0.8.0 | Apache-2.0 OR MIT |
| [bitflags](https://crates.io/crates/bitflags/1.3.2) | 1.3.2 | MIT/Apache-2.0 |
| [bitflags](https://crates.io/crates/bitflags/2.13.2) | 2.13.2 | MIT OR Apache-2.0 |
| [block-buffer](https://crates.io/crates/block-buffer/0.10.4) | 0.10.4 | MIT OR Apache-2.0 |
| [brotli](https://crates.io/crates/brotli/9.0.0) | 9.0.0 | BSD-3-Clause AND MIT |
| [brotli-decompressor](https://crates.io/crates/brotli-decompressor/6.0.1) | 6.0.1 | BSD-3-Clause/MIT |
| [bs58](https://crates.io/crates/bs58/0.5.1) | 0.5.1 | MIT/Apache-2.0 |
| [byteorder](https://crates.io/crates/byteorder/1.5.0) | 1.5.0 | Unlicense OR MIT |
| [bytes](https://crates.io/crates/bytes/1.12.1) | 1.12.1 | MIT |
| [camino](https://crates.io/crates/camino/1.2.6) | 1.2.6 | MIT OR Apache-2.0 |
| [cargo-platform](https://crates.io/crates/cargo-platform/0.1.9) | 0.1.9 | MIT OR Apache-2.0 |
| [cargo_metadata](https://crates.io/crates/cargo_metadata/0.19.2) | 0.19.2 | MIT |
| [cfb](https://crates.io/crates/cfb/0.14.0) | 0.14.0 | MIT |
| [cfg-if](https://crates.io/crates/cfg-if/1.0.5) | 1.0.5 | MIT OR Apache-2.0 |
| [chrono](https://crates.io/crates/chrono/0.4.45) | 0.4.45 | MIT OR Apache-2.0 |
| [cookie](https://crates.io/crates/cookie/0.18.2) | 0.18.2 | MIT OR Apache-2.0 |
| [core_detect](https://crates.io/crates/core_detect/1.0.0) | 1.0.0 | MIT/Apache-2.0 |
| [cpufeatures](https://crates.io/crates/cpufeatures/0.2.17) | 0.2.17 | MIT OR Apache-2.0 |
| [crc32fast](https://crates.io/crates/crc32fast/1.5.2) | 1.5.2 | MIT OR Apache-2.0 |
| [crossbeam-channel](https://crates.io/crates/crossbeam-channel/0.5.17) | 0.5.17 | MIT OR Apache-2.0 |
| [crossbeam-utils](https://crates.io/crates/crossbeam-utils/0.8.23) | 0.8.23 | MIT OR Apache-2.0 |
| [crypto-common](https://crates.io/crates/crypto-common/0.1.7) | 0.1.7 | MIT OR Apache-2.0 |
| [cssparser](https://crates.io/crates/cssparser/0.37.0) | 0.37.0 | MPL-2.0 |
| [cssparser-macros](https://crates.io/crates/cssparser-macros/0.7.1) | 0.7.1 | MPL-2.0 |
| [ctor](https://crates.io/crates/ctor/1.0.13) | 1.0.13 | Apache-2.0 OR MIT |
| [darling](https://crates.io/crates/darling/0.24.1) | 0.24.1 | MIT |
| [darling_core](https://crates.io/crates/darling_core/0.24.1) | 0.24.1 | MIT |
| [darling_macro](https://crates.io/crates/darling_macro/0.24.1) | 0.24.1 | MIT |
| [defmt](https://crates.io/crates/defmt/1.1.1) | 1.1.1 | MIT OR Apache-2.0 |
| [defmt-macros](https://crates.io/crates/defmt-macros/1.1.1) | 1.1.1 | MIT OR Apache-2.0 |
| [defmt-parser](https://crates.io/crates/defmt-parser/1.0.0) | 1.0.0 | MIT OR Apache-2.0 |
| [deranged](https://crates.io/crates/deranged/0.5.8) | 0.5.8 | MIT OR Apache-2.0 |
| [derive_more](https://crates.io/crates/derive_more/2.1.1) | 2.1.1 | MIT |
| [derive_more-impl](https://crates.io/crates/derive_more-impl/2.1.1) | 2.1.1 | MIT |
| [digest](https://crates.io/crates/digest/0.10.7) | 0.10.7 | MIT OR Apache-2.0 |
| [dirs](https://crates.io/crates/dirs/7.0.0) | 7.0.0 | MIT OR Apache-2.0 |
| [dirs-sys](https://crates.io/crates/dirs-sys/0.5.0) | 0.5.0 | MIT OR Apache-2.0 |
| [displaydoc](https://crates.io/crates/displaydoc/0.2.7) | 0.2.7 | MIT OR Apache-2.0 |
| [dom_query](https://crates.io/crates/dom_query/0.28.0) | 0.28.0 | MIT |
| [dpi](https://crates.io/crates/dpi/0.1.2) | 0.1.2 | Apache-2.0 AND MIT |
| [dtoa](https://crates.io/crates/dtoa/1.0.11) | 1.0.11 | MIT OR Apache-2.0 |
| [dtoa-short](https://crates.io/crates/dtoa-short/0.3.5) | 0.3.5 | MPL-2.0 |
| [dunce](https://crates.io/crates/dunce/1.0.5) | 1.0.5 | CC0-1.0 OR MIT-0 OR Apache-2.0 |
| [dyn-clone](https://crates.io/crates/dyn-clone/1.0.20) | 1.0.20 | MIT OR Apache-2.0 |
| [encoding_rs](https://crates.io/crates/encoding_rs/0.8.42) | 0.8.42 | (Apache-2.0 OR MIT) AND BSD-3-Clause |
| [equivalent](https://crates.io/crates/equivalent/1.0.2) | 1.0.2 | Apache-2.0 OR MIT |
| [erased-serde](https://crates.io/crates/erased-serde/0.4.10) | 0.4.10 | MIT OR Apache-2.0 |
| [fastrand](https://crates.io/crates/fastrand/2.5.0) | 2.5.0 | Apache-2.0 OR MIT |
| [fdeflate](https://crates.io/crates/fdeflate/0.3.7) | 0.3.7 | MIT OR Apache-2.0 |
| [flate2](https://crates.io/crates/flate2/1.1.10) | 1.1.10 | MIT OR Apache-2.0 |
| [fnv](https://crates.io/crates/fnv/1.0.7) | 1.0.7 | Apache-2.0 / MIT |
| [foldhash](https://crates.io/crates/foldhash/0.2.0) | 0.2.0 | Zlib |
| [form_urlencoded](https://crates.io/crates/form_urlencoded/1.2.2) | 1.2.2 | MIT OR Apache-2.0 |
| [generic-array](https://crates.io/crates/generic-array/0.14.7) | 0.14.7 | MIT |
| [getrandom](https://crates.io/crates/getrandom/0.3.4) | 0.3.4 | MIT OR Apache-2.0 |
| [getrandom](https://crates.io/crates/getrandom/0.4.3) | 0.4.3 | MIT OR Apache-2.0 |
| [glob](https://crates.io/crates/glob/0.3.4) | 0.3.4 | MIT OR Apache-2.0 |
| [hashbrown](https://crates.io/crates/hashbrown/0.12.3) | 0.12.3 | MIT OR Apache-2.0 |
| [hashbrown](https://crates.io/crates/hashbrown/0.17.1) | 0.17.1 | MIT OR Apache-2.0 |
| [heck](https://crates.io/crates/heck/0.5.0) | 0.5.0 | MIT OR Apache-2.0 |
| [hex](https://crates.io/crates/hex/0.4.3) | 0.4.3 | MIT OR Apache-2.0 |
| [html5ever](https://crates.io/crates/html5ever/0.39.0) | 0.39.0 | MIT OR Apache-2.0 |
| [http](https://crates.io/crates/http/1.5.0) | 1.5.0 | MIT OR Apache-2.0 |
| [ico](https://crates.io/crates/ico/0.5.0) | 0.5.0 | MIT |
| [icu_collections](https://crates.io/crates/icu_collections/2.3.0) | 2.3.0 | Unicode-3.0 |
| [icu_locale_core](https://crates.io/crates/icu_locale_core/2.3.0) | 2.3.0 | Unicode-3.0 |
| [icu_normalizer](https://crates.io/crates/icu_normalizer/2.3.0) | 2.3.0 | Unicode-3.0 |
| [icu_normalizer_data](https://crates.io/crates/icu_normalizer_data/2.3.0) | 2.3.0 | Unicode-3.0 |
| [icu_properties](https://crates.io/crates/icu_properties/2.3.0) | 2.3.0 | Unicode-3.0 |
| [icu_properties_data](https://crates.io/crates/icu_properties_data/2.3.0) | 2.3.0 | Unicode-3.0 |
| [icu_provider](https://crates.io/crates/icu_provider/2.3.1) | 2.3.1 | Unicode-3.0 |
| [ident_case](https://crates.io/crates/ident_case/1.0.1) | 1.0.1 | MIT/Apache-2.0 |
| [idna](https://crates.io/crates/idna/1.1.0) | 1.1.0 | MIT OR Apache-2.0 |
| [idna_adapter](https://crates.io/crates/idna_adapter/1.2.2) | 1.2.2 | Apache-2.0 OR MIT |
| [indexmap](https://crates.io/crates/indexmap/1.9.3) | 1.9.3 | Apache-2.0 OR MIT |
| [indexmap](https://crates.io/crates/indexmap/2.14.2) | 2.14.2 | Apache-2.0 OR MIT |
| [infer](https://crates.io/crates/infer/0.22.0) | 0.22.0 | MIT |
| [itoa](https://crates.io/crates/itoa/1.0.18) | 1.0.18 | MIT OR Apache-2.0 |
| [jiff](https://crates.io/crates/jiff/0.2.37) | 0.2.37 | Unlicense OR MIT |
| [jiff-core](https://crates.io/crates/jiff-core/0.1.1) | 0.1.1 | Unlicense OR MIT |
| [jiff-tzdb](https://crates.io/crates/jiff-tzdb/0.1.8) | 0.1.8 | Unlicense OR MIT |
| [jiff-tzdb-platform](https://crates.io/crates/jiff-tzdb-platform/0.1.3) | 0.1.3 | Unlicense OR MIT |
| [json-patch](https://crates.io/crates/json-patch/4.2.0) | 4.2.0 | MIT/Apache-2.0 |
| [jsonptr](https://crates.io/crates/jsonptr/0.7.1) | 0.7.1 | MIT OR Apache-2.0 |
| [keyboard-types](https://crates.io/crates/keyboard-types/0.8.3) | 0.8.3 | MIT OR Apache-2.0 |
| [libc](https://crates.io/crates/libc/0.2.189) | 0.2.189 | MIT OR Apache-2.0 |
| [litemap](https://crates.io/crates/litemap/0.8.3) | 0.8.3 | Unicode-3.0 |
| [lock_api](https://crates.io/crates/lock_api/0.4.14) | 0.4.14 | MIT OR Apache-2.0 |
| [log](https://crates.io/crates/log/0.4.34) | 0.4.34 | MIT OR Apache-2.0 |
| [markup5ever](https://crates.io/crates/markup5ever/0.39.0) | 0.39.0 | MIT OR Apache-2.0 |
| [memchr](https://crates.io/crates/memchr/2.8.3) | 2.8.3 | Unlicense OR MIT |
| [mime](https://crates.io/crates/mime/0.3.17) | 0.3.17 | MIT OR Apache-2.0 |
| [miniz_oxide](https://crates.io/crates/miniz_oxide/0.8.9) | 0.8.9 | MIT OR Zlib OR Apache-2.0 |
| [miniz_oxide](https://crates.io/crates/miniz_oxide/0.9.1) | 0.9.1 | MIT OR Zlib OR Apache-2.0 |
| [mio](https://crates.io/crates/mio/1.2.3) | 1.2.3 | MIT |
| [muda](https://crates.io/crates/muda/0.20.0) | 0.20.0 | Apache-2.0 OR MIT |
| [multiversion_no_op](https://crates.io/crates/multiversion_no_op/1.0.0) | 1.0.0 | Apache-2.0 OR MIT |
| [new_debug_unreachable](https://crates.io/crates/new_debug_unreachable/1.0.6) | 1.0.6 | MIT |
| [num-conv](https://crates.io/crates/num-conv/0.2.2) | 0.2.2 | MIT OR Apache-2.0 |
| [num-traits](https://crates.io/crates/num-traits/0.2.19) | 0.2.19 | MIT OR Apache-2.0 |
| [once_cell](https://crates.io/crates/once_cell/1.21.4) | 1.21.4 | MIT OR Apache-2.0 |
| [open](https://crates.io/crates/open/5.4.4) | 5.4.4 | MIT |
| [option-ext](https://crates.io/crates/option-ext/0.2.0) | 0.2.0 | MPL-2.0 |
| [os_pipe](https://crates.io/crates/os_pipe/1.2.3) | 1.2.3 | MIT |
| [parking_lot](https://crates.io/crates/parking_lot/0.12.5) | 0.12.5 | MIT OR Apache-2.0 |
| [parking_lot_core](https://crates.io/crates/parking_lot_core/0.9.12) | 0.9.12 | MIT OR Apache-2.0 |
| [percent-encoding](https://crates.io/crates/percent-encoding/2.3.2) | 2.3.2 | MIT OR Apache-2.0 |
| [phf](https://crates.io/crates/phf/0.13.1) | 0.13.1 | MIT |
| [phf_generator](https://crates.io/crates/phf_generator/0.13.1) | 0.13.1 | MIT |
| [phf_macros](https://crates.io/crates/phf_macros/0.13.1) | 0.13.1 | MIT |
| [phf_shared](https://crates.io/crates/phf_shared/0.13.1) | 0.13.1 | MIT |
| [pin-project-lite](https://crates.io/crates/pin-project-lite/0.2.17) | 0.2.17 | Apache-2.0 OR MIT |
| [plist](https://crates.io/crates/plist/1.10.1) | 1.10.1 | MIT |
| [png](https://crates.io/crates/png/0.17.16) | 0.17.16 | MIT OR Apache-2.0 |
| [png](https://crates.io/crates/png/0.18.1) | 0.18.1 | MIT OR Apache-2.0 |
| [potential_utf](https://crates.io/crates/potential_utf/0.1.6) | 0.1.6 | Unicode-3.0 |
| [powerfmt](https://crates.io/crates/powerfmt/0.2.0) | 0.2.0 | MIT OR Apache-2.0 |
| [precomputed-hash](https://crates.io/crates/precomputed-hash/0.1.1) | 0.1.1 | MIT |
| [proc-macro2](https://crates.io/crates/proc-macro2/1.0.107) | 1.0.107 | MIT OR Apache-2.0 |
| [quick-xml](https://crates.io/crates/quick-xml/0.42.0) | 0.42.0 | MIT |
| [quote](https://crates.io/crates/quote/1.0.47) | 1.0.47 | MIT OR Apache-2.0 |
| [raw-window-handle](https://crates.io/crates/raw-window-handle/0.6.2) | 0.6.2 | MIT OR Apache-2.0 OR Zlib |
| [ref-cast](https://crates.io/crates/ref-cast/1.0.27) | 1.0.27 | MIT OR Apache-2.0 |
| [ref-cast-impl](https://crates.io/crates/ref-cast-impl/1.0.27) | 1.0.27 | MIT OR Apache-2.0 |
| [regex](https://crates.io/crates/regex/1.13.1) | 1.13.1 | MIT OR Apache-2.0 |
| [regex-automata](https://crates.io/crates/regex-automata/0.4.18) | 0.4.18 | MIT OR Apache-2.0 |
| [regex-syntax](https://crates.io/crates/regex-syntax/0.8.11) | 0.8.11 | MIT OR Apache-2.0 |
| [rfd](https://crates.io/crates/rfd/0.16.0) | 0.16.0 | MIT |
| [rustc-hash](https://crates.io/crates/rustc-hash/2.1.3) | 2.1.3 | Apache-2.0 OR MIT |
| [same-file](https://crates.io/crates/same-file/1.0.6) | 1.0.6 | Unlicense/MIT |
| [schemars](https://crates.io/crates/schemars/0.8.22) | 0.8.22 | MIT |
| [schemars](https://crates.io/crates/schemars/0.9.0) | 0.9.0 | MIT |
| [schemars](https://crates.io/crates/schemars/1.2.2) | 1.2.2 | MIT |
| [schemars_derive](https://crates.io/crates/schemars_derive/0.8.22) | 0.8.22 | MIT |
| [scopeguard](https://crates.io/crates/scopeguard/1.2.0) | 1.2.0 | MIT OR Apache-2.0 |
| [selectors](https://crates.io/crates/selectors/0.38.0) | 0.38.0 | MPL-2.0 |
| [semver](https://crates.io/crates/semver/1.0.28) | 1.0.28 | MIT OR Apache-2.0 |
| [serde](https://crates.io/crates/serde/1.0.229) | 1.0.229 | MIT OR Apache-2.0 |
| [serde-untagged](https://crates.io/crates/serde-untagged/0.1.9) | 0.1.9 | MIT OR Apache-2.0 |
| [serde_core](https://crates.io/crates/serde_core/1.0.229) | 1.0.229 | MIT OR Apache-2.0 |
| [serde_derive](https://crates.io/crates/serde_derive/1.0.229) | 1.0.229 | MIT OR Apache-2.0 |
| [serde_derive_internals](https://crates.io/crates/serde_derive_internals/0.29.1) | 0.29.1 | MIT OR Apache-2.0 |
| [serde_json](https://crates.io/crates/serde_json/1.0.151) | 1.0.151 | MIT OR Apache-2.0 |
| [serde_repr](https://crates.io/crates/serde_repr/0.1.21) | 0.1.21 | MIT OR Apache-2.0 |
| [serde_spanned](https://crates.io/crates/serde_spanned/1.1.1) | 1.1.1 | MIT OR Apache-2.0 |
| [serde_with](https://crates.io/crates/serde_with/3.24.0) | 3.24.0 | MIT OR Apache-2.0 |
| [serde_with_macros](https://crates.io/crates/serde_with_macros/3.24.0) | 3.24.0 | MIT OR Apache-2.0 |
| [serialize-to-javascript](https://crates.io/crates/serialize-to-javascript/0.1.2) | 0.1.2 | MIT OR Apache-2.0 |
| [serialize-to-javascript-impl](https://crates.io/crates/serialize-to-javascript-impl/0.1.2) | 0.1.2 | MIT OR Apache-2.0 |
| [servo_arc](https://crates.io/crates/servo_arc/0.4.3) | 0.4.3 | MIT OR Apache-2.0 |
| [sha2](https://crates.io/crates/sha2/0.10.9) | 0.10.9 | MIT OR Apache-2.0 |
| [shared_child](https://crates.io/crates/shared_child/1.1.2) | 1.1.2 | MIT |
| [simd-adler32](https://crates.io/crates/simd-adler32/0.3.10) | 0.3.10 | MIT |
| [simdutf8](https://crates.io/crates/simdutf8/0.1.5) | 0.1.5 | MIT OR Apache-2.0 |
| [siphasher](https://crates.io/crates/siphasher/1.0.4) | 1.0.4 | MIT OR Apache-2.0 |
| [smallvec](https://crates.io/crates/smallvec/1.16.2) | 1.16.2 | MIT OR Apache-2.0 |
| [socket2](https://crates.io/crates/socket2/0.6.5) | 0.6.5 | MIT OR Apache-2.0 |
| [softbuffer](https://crates.io/crates/softbuffer/0.4.8) | 0.4.8 | MIT OR Apache-2.0 |
| [stable_deref_trait](https://crates.io/crates/stable_deref_trait/1.2.1) | 1.2.1 | MIT OR Apache-2.0 |
| [string_cache](https://crates.io/crates/string_cache/0.9.0) | 0.9.0 | MIT OR Apache-2.0 |
| [strsim](https://crates.io/crates/strsim/0.11.1) | 0.11.1 | MIT |
| [syn](https://crates.io/crates/syn/2.0.119) | 2.0.119 | MIT OR Apache-2.0 |
| [syn](https://crates.io/crates/syn/3.0.6) | 3.0.6 | MIT OR Apache-2.0 |
| [synstructure](https://crates.io/crates/synstructure/0.14.0) | 0.14.0 | MIT |
| [tao](https://crates.io/crates/tao/0.37.1) | 0.37.1 | Apache-2.0 |
| [tauri](https://crates.io/crates/tauri/2.12.0) | 2.12.0 | Apache-2.0 OR MIT |
| [tauri-codegen](https://crates.io/crates/tauri-codegen/2.7.0) | 2.7.0 | Apache-2.0 OR MIT |
| [tauri-macros](https://crates.io/crates/tauri-macros/2.7.0) | 2.7.0 | Apache-2.0 OR MIT |
| [tauri-plugin-dialog](https://crates.io/crates/tauri-plugin-dialog/2.8.0) | 2.8.0 | Apache-2.0 OR MIT |
| [tauri-plugin-fs](https://crates.io/crates/tauri-plugin-fs/2.6.0) | 2.6.0 | Apache-2.0 OR MIT |
| [tauri-plugin-opener](https://crates.io/crates/tauri-plugin-opener/2.6.0) | 2.6.0 | Apache-2.0 OR MIT |
| [tauri-plugin-shell](https://crates.io/crates/tauri-plugin-shell/2.4.0) | 2.4.0 | Apache-2.0 OR MIT |
| [tauri-plugin-single-instance](https://crates.io/crates/tauri-plugin-single-instance/2.5.4) | 2.5.4 | Apache-2.0 OR MIT |
| [tauri-runtime](https://crates.io/crates/tauri-runtime/2.12.0) | 2.12.0 | Apache-2.0 OR MIT |
| [tauri-runtime-wry](https://crates.io/crates/tauri-runtime-wry/2.12.0) | 2.12.0 | Apache-2.0 OR MIT |
| [tauri-utils](https://crates.io/crates/tauri-utils/2.10.0) | 2.10.0 | Apache-2.0 OR MIT |
| [tendril](https://crates.io/crates/tendril/0.5.1) | 0.5.1 | MIT OR Apache-2.0 |
| [thiserror](https://crates.io/crates/thiserror/2.0.21) | 2.0.21 | MIT OR Apache-2.0 |
| [thiserror-impl](https://crates.io/crates/thiserror-impl/2.0.21) | 2.0.21 | MIT OR Apache-2.0 |
| [time](https://crates.io/crates/time/0.3.55) | 0.3.55 | MIT OR Apache-2.0 |
| [time-core](https://crates.io/crates/time-core/0.1.9) | 0.1.9 | MIT OR Apache-2.0 |
| [time-macros](https://crates.io/crates/time-macros/0.2.32) | 0.2.32 | MIT OR Apache-2.0 |
| [tinystr](https://crates.io/crates/tinystr/0.8.4) | 0.8.4 | Unicode-3.0 |
| [tinyvec](https://crates.io/crates/tinyvec/1.13.3) | 1.13.3 | Zlib OR Apache-2.0 OR MIT |
| [tokio](https://crates.io/crates/tokio/1.53.1) | 1.53.1 | MIT |
| [toml](https://crates.io/crates/toml/1.1.6+spec-1.1.0) | 1.1.6+spec-1.1.0 | MIT OR Apache-2.0 |
| [toml_datetime](https://crates.io/crates/toml_datetime/1.1.1+spec-1.1.0) | 1.1.1+spec-1.1.0 | MIT OR Apache-2.0 |
| [toml_parser](https://crates.io/crates/toml_parser/1.1.3+spec-1.1.0) | 1.1.3+spec-1.1.0 | MIT OR Apache-2.0 |
| [toml_writer](https://crates.io/crates/toml_writer/1.1.2+spec-1.1.0) | 1.1.2+spec-1.1.0 | MIT OR Apache-2.0 |
| [tracing](https://crates.io/crates/tracing/0.1.44) | 0.1.44 | MIT |
| [tracing-attributes](https://crates.io/crates/tracing-attributes/0.1.31) | 0.1.31 | MIT |
| [tracing-core](https://crates.io/crates/tracing-core/0.1.36) | 0.1.36 | MIT |
| [tray-icon](https://crates.io/crates/tray-icon/0.25.1) | 0.25.1 | MIT OR Apache-2.0 |
| [typeid](https://crates.io/crates/typeid/1.0.3) | 1.0.3 | MIT OR Apache-2.0 |
| [typenum](https://crates.io/crates/typenum/1.20.1) | 1.20.1 | MIT OR Apache-2.0 |
| [unicode-ident](https://crates.io/crates/unicode-ident/1.0.26) | 1.0.26 | (MIT OR Apache-2.0) AND Unicode-3.0 |
| [unicode-segmentation](https://crates.io/crates/unicode-segmentation/1.13.3) | 1.13.3 | MIT OR Apache-2.0 |
| [url](https://crates.io/crates/url/2.5.8) | 2.5.8 | MIT OR Apache-2.0 |
| [urlpattern](https://crates.io/crates/urlpattern/0.6.0) | 0.6.0 | MIT |
| [utf8_iter](https://crates.io/crates/utf8_iter/1.0.4) | 1.0.4 | Apache-2.0 OR MIT |
| [uuid](https://crates.io/crates/uuid/1.26.1) | 1.26.1 | Apache-2.0 OR MIT |
| [walkdir](https://crates.io/crates/walkdir/2.5.0) | 2.5.0 | Unlicense/MIT |
| [web-time](https://crates.io/crates/web-time/1.1.0) | 1.1.0 | MIT OR Apache-2.0 |
| [web_atoms](https://crates.io/crates/web_atoms/0.2.6) | 0.2.6 | MIT OR Apache-2.0 |
| [webview2-com](https://crates.io/crates/webview2-com/0.39.1) | 0.39.1 | MIT |
| [webview2-com-macros](https://crates.io/crates/webview2-com-macros/0.8.1) | 0.8.1 | MIT |
| [webview2-com-sys](https://crates.io/crates/webview2-com-sys/0.39.1) | 0.39.1 | MIT |
| [winapi-util](https://crates.io/crates/winapi-util/0.1.11) | 0.1.11 | Unlicense OR MIT |
| [window-vibrancy](https://crates.io/crates/window-vibrancy/0.8.1) | 0.8.1 | Apache-2.0 OR MIT |
| [windows](https://crates.io/crates/windows/0.61.3) | 0.61.3 | MIT OR Apache-2.0 |
| [windows](https://crates.io/crates/windows/0.62.2) | 0.62.2 | MIT OR Apache-2.0 |
| [windows-collections](https://crates.io/crates/windows-collections/0.2.0) | 0.2.0 | MIT OR Apache-2.0 |
| [windows-collections](https://crates.io/crates/windows-collections/0.3.2) | 0.3.2 | MIT OR Apache-2.0 |
| [windows-core](https://crates.io/crates/windows-core/0.61.2) | 0.61.2 | MIT OR Apache-2.0 |
| [windows-core](https://crates.io/crates/windows-core/0.62.2) | 0.62.2 | MIT OR Apache-2.0 |
| [windows-future](https://crates.io/crates/windows-future/0.2.1) | 0.2.1 | MIT OR Apache-2.0 |
| [windows-future](https://crates.io/crates/windows-future/0.3.2) | 0.3.2 | MIT OR Apache-2.0 |
| [windows-implement](https://crates.io/crates/windows-implement/0.60.2) | 0.60.2 | MIT OR Apache-2.0 |
| [windows-interface](https://crates.io/crates/windows-interface/0.59.3) | 0.59.3 | MIT OR Apache-2.0 |
| [windows-link](https://crates.io/crates/windows-link/0.1.3) | 0.1.3 | MIT OR Apache-2.0 |
| [windows-link](https://crates.io/crates/windows-link/0.2.1) | 0.2.1 | MIT OR Apache-2.0 |
| [windows-numerics](https://crates.io/crates/windows-numerics/0.2.0) | 0.2.0 | MIT OR Apache-2.0 |
| [windows-numerics](https://crates.io/crates/windows-numerics/0.3.1) | 0.3.1 | MIT OR Apache-2.0 |
| [windows-result](https://crates.io/crates/windows-result/0.3.4) | 0.3.4 | MIT OR Apache-2.0 |
| [windows-result](https://crates.io/crates/windows-result/0.4.1) | 0.4.1 | MIT OR Apache-2.0 |
| [windows-strings](https://crates.io/crates/windows-strings/0.4.2) | 0.4.2 | MIT OR Apache-2.0 |
| [windows-strings](https://crates.io/crates/windows-strings/0.5.1) | 0.5.1 | MIT OR Apache-2.0 |
| [windows-sys](https://crates.io/crates/windows-sys/0.60.2) | 0.60.2 | MIT OR Apache-2.0 |
| [windows-sys](https://crates.io/crates/windows-sys/0.61.2) | 0.61.2 | MIT OR Apache-2.0 |
| [windows-targets](https://crates.io/crates/windows-targets/0.53.5) | 0.53.5 | MIT OR Apache-2.0 |
| [windows-threading](https://crates.io/crates/windows-threading/0.1.0) | 0.1.0 | MIT OR Apache-2.0 |
| [windows-threading](https://crates.io/crates/windows-threading/0.2.1) | 0.2.1 | MIT OR Apache-2.0 |
| [windows-version](https://crates.io/crates/windows-version/0.1.7) | 0.1.7 | MIT OR Apache-2.0 |
| [windows_x86_64_msvc](https://crates.io/crates/windows_x86_64_msvc/0.53.1) | 0.53.1 | MIT OR Apache-2.0 |
| [winnow](https://crates.io/crates/winnow/1.0.4) | 1.0.4 | MIT |
| [writeable](https://crates.io/crates/writeable/0.6.4) | 0.6.4 | Unicode-3.0 |
| [wry](https://crates.io/crates/wry/0.57.0) | 0.57.0 | Apache-2.0 OR MIT |
| [yoke](https://crates.io/crates/yoke/0.8.3) | 0.8.3 | Unicode-3.0 |
| [yoke-derive](https://crates.io/crates/yoke-derive/0.8.3) | 0.8.3 | Unicode-3.0 |
| [zerofrom](https://crates.io/crates/zerofrom/0.1.8) | 0.1.8 | Unicode-3.0 |
| [zerofrom-derive](https://crates.io/crates/zerofrom-derive/0.1.8) | 0.1.8 | Unicode-3.0 |
| [zerotrie](https://crates.io/crates/zerotrie/0.2.5) | 0.2.5 | Unicode-3.0 |
| [zerovec](https://crates.io/crates/zerovec/0.11.8) | 0.11.8 | Unicode-3.0 |
| [zerovec-derive](https://crates.io/crates/zerovec-derive/0.11.6) | 0.11.6 | Unicode-3.0 |
| [zlib-rs](https://crates.io/crates/zlib-rs/0.6.8) | 0.6.8 | Zlib |
| [zmij](https://crates.io/crates/zmij/1.0.23) | 1.0.23 | MIT |
<!-- CRATES:END -->

## Microsoft components

- **Microsoft Edge WebView2 Runtime** (offline installer, `MicrosoftEdgeWebView2RuntimeInstallerX64.exe`),
  carried by the setup.exe. Redistributed unmodified under the
  [WebView2 Runtime license terms](https://developer.microsoft.com/microsoft-edge/webview2/) (it may be
  redistributed with applications that use it; Microsoft's license terms are shown when it installs).
  VEXT does not modify it and every installer is signature-checked by `tools/fetch_redist.ps1`.
- **Microsoft Visual C++ Redistributable** (14.x x64 and x86, plus the 2013, 2012 and 2010 x64 packages)
  in `redist\`. Redistributed unmodified under Microsoft's
  [redistributable terms](https://learn.microsoft.com/cpp/windows/redistributing-visual-cpp-files) and each
  package's own license, shown when it installs. They are installed only when a PC lacks them or has an older
  version.

These are Microsoft's, not VEXT's: they are not covered by the GPL, and they sit next to VEXT as separate
programs (the installer runs them; VEXT does not link to their source).

## Trademarks

FL Studio and Image-Line are trademarks of Image-Line Software. VST is a trademark of Steinberg Media
Technologies GmbH. Waves, CLAP, AAX and every plugin name VEXT shows belong to their owners. VEXT is not
affiliated with or endorsed by any of them.
