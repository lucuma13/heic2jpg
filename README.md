# heic2jpg

`heic2jpg` is a fast HEIC to JPEG converter designed to resolve the compatibility gap between iPhone image files and common desktop workflows. Conversion runs in parallel across worker threads using [pillow-heif](https://pypi.org/project/pillow-heif/) for fast, in-process decoding in macOS, Linux or Windows.


### 🚀 Installation

1. Install the `uv` package manager with the [official installer](https://docs.astral.sh/uv/getting-started/installation/) (or `brew install uv` on macOS / Linux).

2. Install the tool:

```
uv tool install heic2jpg
```

### 📖 Usage

Convert a single file:

```bash
heic2jpg path/to/photo.HEIC
```

Convert all files in directory:
```bash
heic2jpg path/to/photo/album
```

Convert all files in current directory:
```bash
heic2jpg
```

More options:
```
  -q, --quality [1-100]] : Target quality (default: 30)
  -k, --keep             : Keep originals (default: delete after conversion)
  -f, --force            : Overwrite existing .jpg

```

### 📊 Performance

pillow-heif's C code releases the Python's GIL during HEIF decode and JPEG encode. The default `ThreadPoolExecutor` therefore gets full CPU parallelism with no per-worker process startup cost. That offers almost double the speed of traditional ImageMagick workflows.

Version          | Backend          | 100 files   |
-----------------|------------------|------------:|
heic2jpg 2.0.0   | pillow-heif      |       ~2.3s |
heic2jpg 1.1.0   | ImageMagick      |      ~15.5s |

Note: absolute throughput will vary with CPU and disk speed.