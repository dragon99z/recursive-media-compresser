# recursive-media-compresser

A powerful, single-script Python utility that **recursively walks a directory tree**, detects every image, video, and audio file, and compresses each one using lossless or visually-lossless techniques — without ever sacrificing data you care about.

> **Full documentation:** [deepwiki.com/dragon99z/recursive-media-compresser](https://deepwiki.com/dragon99z/recursive-media-compresser)

---

## Features

- **Two compression modes** — *near-lossless* (default) or *lossless*, switchable per run
- **GPU acceleration** — auto-detects NVENC (AV1/HEVC) for video and Vulkan FFV1 for lossless encoding
- **Persistent cache** — a JSON-based registry skips already-processed files on re-runs
- **Multi-threaded** — parallel compression via `ThreadPoolExecutor` (configurable thread count)
- **Data-safe** — uses a "compress-then-swap" strategy; the original is only replaced after the new file is verified smaller and written successfully
- **Failsafe revert** — if the compressed output is larger than the source, the temp file is discarded and the original is left untouched
- **Windows MAX_PATH support** — automatically applies the `\\?\` long-path prefix and handles read-only file permissions
- **Dry-run mode** — preview what would happen without touching any files
- **Single-file or batch** — point at a single file or an entire directory tree

---

## Supported Formats

| Type   | Extensions |
|--------|------------|
| Images | `.jpg` `.jpeg` `.png` `.webp` `.gif` `.bmp` `.tiff` `.tif` |
| Video  | `.mp4` `.mkv` `.mov` `.avi` `.wmv` `.flv` `.webm` `.m4v` `.ts` |
| Audio  | `.wav` `.mp3` `.aac` `.m4a` `.ogg` `.flac` `.opus` `.wma` `.aiff` |

---

## Compression Modes

### Near-Lossless (default)
Maximises space savings with no perceptible quality loss.

| Media  | Codec / Settings |
|--------|-----------------|
| Images | JPEG q=95 + optimize · PNG compress_level=9 · WebP q=90 |
| Video  | libx265 CRF 18 slow (CPU) **or** HEVC/AV1-NVENC CQ 18 (GPU) → always `.mkv` |
| Audio  | AAC 256 kbps → `.m4a` |

### Lossless
Bit-perfect reproduction — what goes in comes out identical.

| Media  | Codec / Settings |
|--------|-----------------|
| Images | PNG optimize · WebP lossless |
| Video  | ffv1_vulkan GPU (recent FFmpeg + Vulkan) **or** FFV1 level 3 CPU + FLAC → `.mkv` |
| Audio  | FLAC compression=8 → `.flac` |

> **Why always `.mkv` for video?** MKV supports every codec without container restrictions, no MP4-specific flags needed, and works transparently on all platforms.

---

## Getting Started

### Prerequisites

- **Python 3.10+**
- **FFmpeg** — required for video and audio compression
  - Linux: `sudo apt install ffmpeg`
  - macOS: `brew install ffmpeg`
  - Windows: Download from [ffmpeg.org](https://ffmpeg.org/download.html) and add to `PATH`
- **Pillow** — required for image compression (installed via `requirements.txt`)

### Installation

```bash
git clone https://github.com/dragon99z/recursive-media-compresser.git
cd recursive-media-compresser

# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate      # Linux / macOS
# .venv\Scripts\activate       # Windows

pip install -r requirements.txt
```

---

## Usage

```
python media_compress.py <path> [thread_count] [options]
```

### Arguments

| Argument       | Description |
|----------------|-------------|
| `path`         | File or root directory to scan recursively |
| `thread_count` | Number of worker threads (default: half of CPU count) |

### Options

| Flag | Description |
|------|-------------|
| `--mode {near-lossless,lossless}` | Compression mode (default: `near-lossless`) |
| `--replace` | Replace originals in-place instead of creating `_compressed` copies |
| `-o / --output PATH` | Output path — single-file mode only |
| `--min-size SIZE` | Skip files smaller than SIZE (e.g. `500`, `1KB`, `2.5MB`, `1GB`) |
| `--min-size-div SIZE` | Revert if the space savings are less than SIZE |
| `--dry-run` | Preview what would be compressed without making changes |
| `--verbose` | Verbose logging — shows every skipped file |
| `--no-cache` | Disable the persistent cache and force-reprocess everything |
| `--cache-file PATH` | Custom path for the cache JSON file |

---

## Examples

```bash
# Compress all media in a folder with 8 threads (keeps originals as _compressed copies)
python media_compress.py ./media 8

# Replace originals in-place (safe for network drives)
python media_compress.py ./photos 4 --replace

# Lossless mode — preview first with dry-run
python media_compress.py ./media 4 --mode lossless --dry-run

# Single file with an explicit output path
python media_compress.py video.mp4 1 -o out.mp4

# Skip files smaller than 1 MB
python media_compress.py ./media 8 --min-size 1MB

# Revert output if savings are less than 100 KB (incompressible files)
python media_compress.py ./media 8 --min-size-div 100KB

# Re-run the same folder — already-done files are skipped automatically
python media_compress.py ./media 8

# Use a custom cache location
python media_compress.py ./media 8 --cache-file /tmp/progress.json

# Verbose output showing every skipped file
python media_compress.py ./media 4 --dry-run --verbose
```

---

## Architecture

The tool is contained in a single script (`media_compress.py`) built around a clean pipeline:

```
CLI args
   └─► run()
         ├─► collect_media_files()   — recursive directory walk
         ├─► probe_gpu_encoders()    — one-time NVENC / Vulkan probe
         ├─► ThreadPoolExecutor      — parallel dispatch
         │     └─► compress_file()  — per-file orchestrator
         │           ├─► compress_image()   (Pillow)
         │           ├─► compress_video()   (FFmpeg)
         │           └─► compress_audio()   (FFmpeg)
         └─► CompressionCache.save() — flush JSON cache
```

### Key Components

| Component | Role |
|-----------|------|
| `CompressionCache` | Persistent JSON registry. Skips files whose path, size, mtime, and mode all match a previous run. |
| `safe_replace()` | Writes a temp file in `src.parent` (same filesystem), then atomically renames it. Falls back to `shutil.copy2` for genuine cross-filesystem scenarios. The original is only deleted after the new file is confirmed on disk. |
| `probe_gpu_encoders()` | Spawns FFmpeg once to list encoders, then test-encodes a tiny 256x256 clip to verify each NVENC codec actually works on the current GPU — catches cases like `av1_nvenc` listed on RTX 3000 hardware that can't run it. |
| `to_long_path()` | Prepends `\\?\` (or `\\?\UNC\` for network shares) on Windows to bypass the 260-character MAX_PATH limit. |

---

## Platform Notes

**Windows:**
- Read-only files are automatically un-flagged before deletion
- Paths are prefixed with `\\?\` to bypass the 260-character MAX_PATH limit
- The temp file is always created in the same directory as the source so atomic rename succeeds even across network shares (e.g. script on `C:`, files on `Z:`)

**GPU (NVIDIA):**
- Near-lossless: `av1_nvenc` (Ada Lovelace / RTX 4000+) → `hevc_nvenc` (Turing / RTX 2000+) → CPU libx265 fallback
- Lossless: `ffv1_vulkan` (requires FFmpeg with Vulkan support) → CPU FFV1 fallback

---

## Project Structure

```
recursive-media-compresser/
├── media_compress.py   # Main script — all logic lives here
└── requirements.txt    # Python dependencies (Pillow)
```

---

## License

Recursive Media Compresser is licensed under the **Recursive Media Compresser Copyleft Named User License (RMCNUL) v1.0**.

#### You May

- View the source code
- Download and use the software
- Modify the software for personal, educational, or internal use
- Share unmodified copies with attribution

#### You Must

- Keep this license attached to all copies
- Document modifications
- License derivative works under the same license
- Provide attribution to the original author

#### You May Not

- Redistribute modified versions without permission from dragon99z
- Use the software commercially without permission from dragon99z
- Re-license the project under another license

See the `LICENSE` file for the complete terms.

Copyright © 2026 dragon99z.

---

## Links

- **Repository:** [github.com/dragon99z/recursive-media-compresser](https://github.com/dragon99z/recursive-media-compresser)
- **DeepWiki docs:** [deepwiki.com/dragon99z/recursive-media-compresser](https://deepwiki.com/dragon99z/recursive-media-compresser)
- **FFmpeg downloads:** [ffmpeg.org/download.html](https://ffmpeg.org/download.html)
