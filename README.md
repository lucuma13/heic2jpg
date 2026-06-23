# heic2jpg

[![PyPI Version](https://img.shields.io/pypi/v/heic2jpg.svg)](https://pypi.org/project/heic2jpg/)
![OS](https://img.shields.io/badge/OS-macOS%20%7C%20Windows%20%7C%20Linux-lightgrey)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![ty](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ty/main/assets/badge/v0.json)](https://github.com/astral-sh/ty)
[![CI](https://github.com/lucuma13/heic2jpg/actions/workflows/ci.yml/badge.svg)](https://github.com/lucuma13/heic2jpg/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/lucuma13/heic2jpg/graph/badge.svg?token=X3YA4CA9E3)](https://codecov.io/gh/lucuma13/heic2jpg)

`heic2jpg` is a fast HEIC to JPEG converter designed to resolve the compatibility gap between iPhone image files and common desktop workflows. Conversion runs in parallel across worker threads using [pillow-heif](https://pypi.org/project/pillow-heif/) for fast, in-process decoding in macOS, Linux or Windows.

### 🚀 Installation

1. Install the `uv` package manager with the [official installer](https://docs.astral.sh/uv/getting-started/installation/), or:
* macOS: `brew install uv`
* Windows: `winget install astral-sh.uv`
* Linux (Debian): `apt-get install uv`
<!--
* Linux (RHEL): `yum install uv`
* Linux (SUSE): `zypper install python-uv`
* Linux (Arch): `pacman -S muv`
-->

2. Install the tool:

```
uv tool install heic2jpg
```

3. Test the installation (if the command is not recognised try `uv tool update-shell` and restart your terminal):

```
heic2jpg --version
```

### 📖 Usage examples

Convert a single file:

```bash
heic2jpg path/to/photo.HEIC
```

Convert all files in directory:
```bash
heic2jpg path/to/photo/album
heic2jpg                        # current directory
```

Convert all files in current directory recursively, preserving metadata and timestamps:
```bash
heic2jpg -Rmt
```

Other options:
```
  -q, --quality [1-100] Target quality (default: 30)
  -m, --metadata        Preserve EXIF metadata
  -t, --times           Preserve source file timestamps
  -k, --keep            Keep originals (default: delete after conversion)
  -R, --recursive       Recurse into subdirectories when a directory is given
  -f, --force           Overwrite existing .jpg outputs
```

### 📊 Performance

pillow-heif's C code releases the Python's GIL during HEIF decode and JPEG encode. The default `ThreadPoolExecutor` therefore gets full CPU parallelism with no per-worker process startup cost. That offers over 6x the speed of traditional ImageMagick workflows.

Version          | Backend          | 100 files   |
-----------------|------------------|------------:|
heic2jpg 2.0.0   | pillow-heif      |       ~2.5s |
heic2jpg 1.1.0   | ImageMagick      |      ~15.5s |

Note: absolute throughput will vary with CPU and disk speed.
