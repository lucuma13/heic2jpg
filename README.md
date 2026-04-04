# heic2jpg

A quick way to convert HEIC images to JPG.

#### 📋 Description

`heic2jpg` converts HEIC images to JPG at desired compression quality.

#### 💻 Compatibility

* macOS
* Linux

#### 🛠 Dependencies

* [ImageMagick](https://github.com/ImageMagick/ImageMagick) v7 or higher.
* [libheif](https://github.com/strukturag/libheif) (required for HEIC support in ImageMagick)

#### 🚀 Installation

1. Install [Homebrew](https://brew.sh/) (if not already installed):
```
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

2. Tap and install:
```
brew tap lucuma13/homebrew-dit
brew install heic2jpg
```

#### 📖 Usage

`heic2jpg [options] <path>`

| Option | Argument | Description |
| :---: | :---: | :--- |
| `-q` | `[1-100]` | Compression quality (default: 30) |
| `-v` | | Verbose |
| `-h` | | Show help message |
| `--version` | | Print version |

The `<path>` can be a file, a directory, or left blank to process the current folder. All HEIC files in the target location will be batch-converted.
