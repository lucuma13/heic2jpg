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

`heic2jpg [options] <source>`

Options:<br>
`-q` [1-100] : Compression quality (default = 30)<br>
`-h` : Show help message<br>
`-v` : Verbose<br>
`--version` : Print version<br>

The `<source>` can be a single file or an entire directory. If a directory is provided, all HEIC files within it will be batch-converted.