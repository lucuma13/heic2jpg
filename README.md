# heic2jpg

`heic2jpg` converts HEIC images to JPG at desired compression quality.

### 🛠 Dependencies

* [ImageMagick](https://github.com/ImageMagick/ImageMagick) (v7+) © 1999-2026 ImageMagick Studio LLC (Apache 2.0)
* [libheif](https://github.com/strukturag/libheif) (for HEIC/HEIF support in ImageMagick) © 2017-2026 Julea, GmbH (GPL-3.0)

### 🚀 Installation

##### macOS and Linux

1. Install [Homebrew](https://brew.sh/) (if not already installed):
```
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

2. Tap and install:
```
brew tap lucuma13/dit
brew install heic2jpg
```

### 📖 Usage

`heic2jpg [options] <path>`

| Option | Argument | Description |
| :---: | :---: | :--- |
| `-q` | `[1-100]` | Compression quality (default: 30) |
| `-v` | | Verbose |
| `-h` | | Show help message |
| `--version` | | Print version |

Note: `<path>` can be a single file or a directory, or the current directory if left blank.
