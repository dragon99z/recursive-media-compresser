#!/usr/bin/env python3
"""
media_compress.py

Recursively walks a directory tree, detects every image / video / audio file,
and compresses each one using lossless or visually-lossless techniques.

A persistent cache records processed files so that re-running the script on
the same directory automatically skips already-compressed files.

Modes
─────
  near-lossless  (default)
    Images : JPEG q=95+optimize  |  PNG compress_level=9  |  WebP q=90
    Video  : libx265 CRF 18 slow preset (CPU)
             or HEVC/AV1-NVENC CQ 18 + -b:v 0 (GPU, if available)  → always .mkv
    Audio  : AAC 256 kbps — transparent to most listeners

  lossless
    Images : PNG optimize  |  WebP lossless
    Video  : ffv1_vulkan GPU pipeline (recent FFmpeg + Vulkan required)
             or FFV1 level 3 CPU fallback + FLAC  → always .mkv  (bit-perfect)
    Audio  : FLAC compression=8 → .flac  (bit-perfect)

Usage:
    python media_compress.py <path> <thread_count> [options]

Examples:
    python media_compress.py ./photos 8
    python media_compress.py ./media  4  --mode lossless
    python media_compress.py ./media  4  --replace
    python media_compress.py video.mp4 1 -o out.mp4
    python media_compress.py ./media  4  --dry-run --verbose
    python media_compress.py ./media  8  --no-cache
    python media_compress.py ./media  8  --cache-file /tmp/progress.json

Windows notes:
  - Read-only files are automatically un-flagged before deletion.
  - Paths are prefixed with \\\\?\\ to bypass the 260-character MAX_PATH limit.
  - The temp file is always created next to the source file (same drive/share)
    so that atomic rename succeeds even when the script runs from a different
    drive (e.g. script on C:, files on Z: network share).
"""

import json
import os
import sys
import stat
import shutil
import tempfile
import argparse
import logging
import platform
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

try:
    from PIL import Image
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

# ---------------------------------------------------------------------------
# Logging  (mirrors cleanup_dirs.py)
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

IS_WINDOWS = platform.system() == "Windows"

stats = {"compressed": 0, "reverted": 0, "skipped": 0, "errors": 0, "bytes_saved": 0}
stats_lock = Lock()

# ---------------------------------------------------------------------------
# Supported media extensions
# ---------------------------------------------------------------------------

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".tif"}
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".wmv", ".flv", ".webm", ".m4v", ".ts"}
AUDIO_EXTS = {".wav", ".mp3", ".aac", ".m4a", ".ogg", ".flac", ".opus", ".wma", ".aiff"}
ALL_MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS | AUDIO_EXTS


# ---------------------------------------------------------------------------
# Windows helpers  (mirrors cleanup_dirs.py)
# ---------------------------------------------------------------------------

def to_long_path(path: str) -> str:
    """
    Prefix an absolute Windows path with the long-path prefix to bypass
    the 260-character MAX_PATH limit. No-op on non-Windows systems.
    """
    if not IS_WINDOWS:
        return path
    if path.startswith("\\\\?\\"):   # already prefixed — check FIRST
        return path
    if path.startswith("\\\\"):       # UNC path  \\server\share
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path           # regular  C:\...


def _remove_readonly(func, path, _exc_info):
    """Error handler for shutil operations on Windows read-only files."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def file_size(path: Path) -> int:
    return path.stat().st_size


def human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def parse_size(value: str) -> int:
    """
    Convert a human-readable size string to bytes.  Used as the argparse
    type for --min-size so the shell validates it at startup.

    Accepted (case-insensitive): 500  500B  1KB  1K  2.5MB  2.5M  1GB  1G  1TB  1T
    """
    _UNITS = {
        "tb": 1024 ** 4, "t": 1024 ** 4,
        "gb": 1024 ** 3, "g": 1024 ** 3,
        "mb": 1024 ** 2, "m": 1024 ** 2,
        "kb": 1024,      "k": 1024,
        "b":  1,         "":  1,
    }
    lower = value.strip().lower()
    # Try longest suffix first so "mb" matches before bare "b"
    for suffix, multiplier in sorted(_UNITS.items(), key=lambda x: -len(x[0])):
        if lower.endswith(suffix):
            numeric = lower[: len(lower) - len(suffix)].strip()
            try:
                return int(float(numeric) * multiplier)
            except ValueError:
                break
    raise argparse.ArgumentTypeError(
        f"Invalid size '{value}'. Examples: 500, 1KB, 2.5MB, 1GB"
    )


def check_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


def detect_type(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:  return "image"
    if ext in VIDEO_EXTS:  return "video"
    if ext in AUDIO_EXTS:  return "audio"
    return "unknown"


def output_ext(src: Path, mode: str) -> str:
    """
    Output file extension for a given source and mode.

    Video is always .mkv — it supports every codec, has no container
    limitations, and works with both FFV1 (lossless) and HEVC/AV1
    (near-lossless) without needing MP4-specific flags like faststart
    or QuickTime tags.

    Images keep their original format; audio follows the mode.
    """
    kind = detect_type(src)
    if kind == "image":  return src.suffix.lower()
    if kind == "video":  return ".mkv"   # always MKV for both modes
    if kind == "audio":  return ".flac" if mode == "lossless" else ".m4a"
    return src.suffix.lower()


def ffmpeg_run(args: list[str], verbose: bool = False) -> bool:
    """Run an FFmpeg command and return True on success."""
    cmd = ["ffmpeg", "-y", "-hide_banner"] + args
    if not verbose:
        cmd += ["-loglevel", "error"]
    return subprocess.run(cmd).returncode == 0


# ---------------------------------------------------------------------------
# GPU detection — single probe, results cached for the entire process lifetime
# ---------------------------------------------------------------------------

def _test_encoder(codec: str) -> bool:
    """
    Verify that a video encoder actually works on THIS hardware.

    Being listed in 'ffmpeg -encoders' is not enough — a codec can be
    compiled into FFmpeg but still fail at runtime if the GPU doesn't
    support it.  Classic example: av1_nvenc appears on Ampere (RTX 3000)
    even though AV1 encoding requires Ada Lovelace (RTX 4000+).

    Why not '-f null -'
    ───────────────────
    When subprocess captures stdout (capture_output=True), '-' as the
    output target points at that pipe.  On Windows this can cause NVENC to
    fail even when the encoder itself is healthy, producing a false negative.
    Writing to a real temp file avoids the issue entirely.

    Other hardening
    ───────────────
    • '-pix_fmt yuv420p'  NVENC rejects other formats unless a scale filter
                           is inserted; this avoids needing one.
    • '256x256'           128×128 is on the edge of NVENC's minimum dimensions
                           depending on codec profile; 256×256 is always safe.
    • '-t 1'             At 24 fps that's 24 frames — enough for the encoder
                           to fully initialise and produce at least one output packet.
    """
    tmp_path: Path | None = None
    try:
        tmp_fd, tmp_str = tempfile.mkstemp(suffix=".mkv")
        os.close(tmp_fd)
        tmp_path = Path(tmp_str)

        r = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi",
                "-i", "color=c=black:s=256x256:r=24",
                "-pix_fmt", "yuv420p",
                "-t", "1",
                "-c:v", codec,
                "-y", str(tmp_path),
            ],
            capture_output=True,
            timeout=30,
        )
        if r.returncode != 0 and r.stderr:
            # Log at INFO so the user sees the real reason without needing --verbose
            log.info(
                "GPU: %s test-encode failed: %s",
                codec,
                r.stderr.decode(errors="replace").strip().splitlines()[-1],
            )
        return r.returncode == 0
    except Exception as exc:
        log.info("GPU: %s test-encode raised %s", codec, exc)
        return False
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)

_gpu_probed:      bool = False
_gpu_nvenc:       str | None = None   # "av1_nvenc" | "hevc_nvenc" | None
_gpu_ffv1_vulkan: bool = False
_gpu_probe_lock = Lock()


def probe_gpu_encoders() -> tuple[str | None, bool]:
    """
    Probe all GPU encoders in one FFmpeg call and return cached results.

    Returns
    ───────
    (nvenc_codec, has_ffv1_vulkan)
      nvenc_codec      "av1_nvenc" | "hevc_nvenc" | None
                       Used for near-lossless video encoding.
                       Priority: av1_nvenc (Ada Lovelace/Blackwell) > hevc_nvenc (Turing+)
      has_ffv1_vulkan  True when ffv1_vulkan is listed in FFmpeg encoders.
                       Requires a recent FFmpeg build compiled with Vulkan
                       support (--enable-vulkan).  Used for lossless video.

    Thread-safe; FFmpeg is spawned exactly once per process.
    """
    global _gpu_probed, _gpu_nvenc, _gpu_ffv1_vulkan
    with _gpu_probe_lock:
        if _gpu_probed:
            return _gpu_nvenc, _gpu_ffv1_vulkan
        try:
            result = subprocess.run(
                ["ffmpeg", "-hide_banner", "-encoders"],
                capture_output=True, text=True, timeout=15,
            )
            stdout = result.stdout
            _gpu_ffv1_vulkan = "ffv1_vulkan" in stdout

            # Being in -encoders is necessary but not sufficient — test each
            # NVENC codec with a real (tiny) encode to catch hardware mismatches
            # (e.g. av1_nvenc listed on RTX 3000 but only RTX 4000+ can run it).
            for candidate in ("av1_nvenc", "hevc_nvenc"):
                if candidate in stdout:
                    if _test_encoder(candidate):
                        _gpu_nvenc = candidate
                        break
                    log.info("GPU: %s listed but hardware test failed — skipping", candidate)
        except Exception:
            pass
        finally:
            _gpu_probed = True

        if _gpu_nvenc:
            log.info("GPU: %s detected — near-lossless video will use NVENC", _gpu_nvenc)
        else:
            log.info("GPU: no NVENC encoder found — near-lossless video will use CPU libx265")
        if _gpu_ffv1_vulkan:
            log.info("GPU: ffv1_vulkan detected — lossless video will use Vulkan-accelerated FFV1")
        else:
            log.info("GPU: ffv1_vulkan not available — lossless video will use CPU FFV1")

        return _gpu_nvenc, _gpu_ffv1_vulkan


# ---------------------------------------------------------------------------
# Compression cache
# ---------------------------------------------------------------------------

class CompressionCache:
    """
    Persistent JSON record of files that have already been processed.

    An entry is stored after every successful outcome (compressed or
    reverted).  On re-run a file is skipped when ALL of these match:
      • its absolute path is in the cache
      • its current size & mtime match the recorded values  (± 2 s for
        FAT/NTFS 2-second timestamp granularity)
      • the current --mode matches the recorded mode

    If any condition fails (file modified, mode changed) the file is
    reprocessed and its cache entry is updated automatically.
    """
    _SCHEMA = 1

    def __init__(self, cache_path: Path) -> None:
        self._path = cache_path
        self._write_lock = Lock()
        self._entries: dict[str, dict] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if data.get("schema_version") == self._SCHEMA:
                self._entries = data.get("entries", {})
                log.info(
                    "Cache: loaded %d entr%s from %s",
                    len(self._entries),
                    "y" if len(self._entries) == 1 else "ies",
                    self._path,
                )
        except Exception as exc:
            log.warning("Cache: could not read %s — %s (starting fresh)", self._path, exc)

    def save(self) -> None:
        """Flush pending changes to disk — call once after all workers finish."""
        with self._write_lock:
            if not self._dirty:
                return
            try:
                self._path.write_text(
                    json.dumps(
                        {"schema_version": self._SCHEMA, "entries": self._entries},
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                log.info(
                    "Cache: saved %d entr%s → %s",
                    len(self._entries),
                    "y" if len(self._entries) == 1 else "ies",
                    self._path,
                )
            except Exception as exc:
                log.warning("Cache: could not write %s — %s", self._path, exc)

    def is_done(self, path: Path, mode: str) -> bool:
        """True if path was processed with this mode and is unchanged on disk."""
        entry_path = str(path.resolve())
        entry = self._entries.get(entry_path)
        if entry is None or entry.get("mode") != mode:
            return False
        try:
            st = path.stat()
            return (
                st.st_size == entry["size"]
                and abs(st.st_mtime - entry["mtime"]) < 2.0
            )
        except OSError:
            return False

    def mark_done(self, path: Path, *, size: int, mtime: float, mode: str) -> None:
        """Record path as processed (thread-safe)."""
        with self._write_lock:
            self._entries[str(path.resolve())] = {
                "size":          size,
                "mtime":         round(mtime, 3),
                "mode":          mode,
                "compressed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }
            self._dirty = True


# ---------------------------------------------------------------------------
# Cross-filesystem safe replacement
# ---------------------------------------------------------------------------

def safe_replace(tmp: Path, final_dst: Path, original_src: Path) -> None:
    """
    Install tmp at final_dst, then remove original_src.

    Strategy (order matters for data safety):
    1. Try Path.replace() — atomic on the same filesystem, also handles the
       case where final_dst == original_src (same extension, same path).
    2. On OSError (genuine cross-filesystem edge case) fall back to
       shutil.copy2() which streams across filesystem boundaries.
       The original is only removed AFTER the copy is confirmed, so no
       data is ever lost if the copy fails mid-way.

    Root cause of the original cross-drive bug: the temp file was written to
    a hardcoded local "./temp" directory on the C: drive, then the code tried
    to rename it to Z: (network drive) — which is always an error.
    Fix: always create the temp file in src.parent (same directory = same
    drive/share as the source), making Path.replace() always succeed.
    """
    try:
        tmp.replace(final_dst)          # atomic; handles final_dst == original_src
    except OSError:
        # True cross-filesystem fallback (rare after the above fix)
        try:
            shutil.copy2(str(tmp), str(final_dst))
        except Exception:
            tmp.unlink(missing_ok=True)
            raise                       # let caller clean up and log
        tmp.unlink(missing_ok=True)

    # Remove original only after new file is confirmed in place
    if original_src.resolve() != final_dst.resolve():
        original_src.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Per-type compressors
# ---------------------------------------------------------------------------

def compress_image(src: Path, dst: Path, mode: str) -> bool:
    if not HAS_PILLOW:
        log.error("Pillow not installed — run: pip install Pillow")
        return False
    try:
        img = Image.open(to_long_path(str(src)))
        ext = dst.suffix.lower()

        # JPEG cannot carry an alpha channel — convert before saving
        if ext in (".jpg", ".jpeg") and img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGB")

        if mode == "lossless":
            if ext in (".jpg", ".jpeg"):
                log.warning("JPEG is inherently lossy — using quality=100 for %s", src.name)
                img.save(str(dst), format="JPEG", quality=100, optimize=True)
            elif ext == ".png":
                img.save(str(dst), format="PNG", optimize=True, compress_level=9)
            elif ext == ".webp":
                img.save(str(dst), format="WEBP", lossless=True, method=6)
            else:
                img.save(str(dst), optimize=True)
        else:  # near-lossless
            if ext in (".jpg", ".jpeg"):
                img.save(str(dst), format="JPEG", quality=95, optimize=True, subsampling=0)
            elif ext == ".png":
                img.save(str(dst), format="PNG", optimize=True, compress_level=9)
            elif ext == ".webp":
                img.save(str(dst), format="WEBP", lossless=False, quality=90, method=6)
            else:
                img.save(str(dst), optimize=True)
        return True
    except Exception as exc:
        log.error("Image compression failed for %s — %s", src.name, exc)
        return False


def compress_video(src: Path, dst: Path, mode: str, verbose: bool) -> bool:
    """
    All video output is .mkv.

    Lossless
    ────────
    ffv1_vulkan available → Vulkan-accelerated FFV1 (GPU, bit-perfect).
                            Requires a recent FFmpeg build with Vulkan support.
    CPU fallback          → FFV1 level 3 with multithreaded slices (bit-perfect).

    Near-lossless
    ─────────────
    NVENC available → HEVC-NVENC or AV1-NVENC with VBR CQ 18.

      KEY: -b:v 0 is mandatory.  Without it NVENC VBR ignores -cq and
      uses an internal bitrate target that routinely makes output 20 %+
      larger than the source.  Setting -b:v 0 puts the encoder into pure
      quality mode — the exact NVENC equivalent of x265 CRF.

    CPU fallback    → libx265 CRF 18, "slow" preset.
                      ~10 % better compression than "medium" at the same CRF.
                      No -tag:v hvc1 or -movflags needed — output is MKV.
    """
    if not check_ffmpeg():
        log.error("FFmpeg not found — install from https://ffmpeg.org/download.html")
        return False

    nvenc, has_ffv1_vulkan = probe_gpu_encoders()

    if mode == "lossless":
        if has_ffv1_vulkan:
            # Vulkan pipeline: init device → hwaccel decode → GPU FFV1 encode
            # -strict experimental may be required depending on the FFmpeg build.
            args = [
                "-init_hw_device", "vulkan=vk:0",    # init Vulkan device, name it "vk"
                "-hwaccel",               "vulkan",
                "-hwaccel_output_format", "vulkan",   # keep decoded frames on GPU
                "-hwaccel_device",        "vk",
                "-i", str(src),
                "-map", "0",
                "-c:v", "ffv1_vulkan",
                "-strict",   "experimental",
                "-level",    "3",
                "-coder",    "1",
                "-context",  "1",
                "-g",        "1",
                "-slices",   "16",
                "-slicecrc", "1",
                "-c:a", "flac",
                "-c:s", "copy",
                str(dst),
            ]
        else:
            # CPU FFV1: level 3 enables slice-based multithreading
            args = [
                "-i", str(src),
                "-map", "0",
                "-c:v", "ffv1",
                "-level",    "3",
                "-coder",    "1",
                "-context",  "1",
                "-g",        "1",
                "-slices",   "16",
                "-slicecrc", "1",
                "-c:a", "flac",
                "-c:s", "copy",
                str(dst),
            ]
    else:  # near-lossless
        if nvenc:
            args = [
                "-hwaccel",               "cuda",
                "-hwaccel_output_format", "cuda",   # keep decoded frames on GPU
                "-i", str(src),
                "-map", "0",
                "-c:v",         nvenc,
                "-rc",          "vbr",
                "-cq",          "24",     # quality target (0 = lossless, 51 = worst)
                "-b:v",         "0",      # FIX: pure quality mode — disables internal bitrate floor
                "-qmin",        "1",
                "-qmax",        "51",
                "-preset",      "p7",     # slowest = best quality
                "-tune",        "hq",
                "-spatial_aq",  "1",
                "-temporal_aq", "1",
                "-aq-strength", "8",
                "-b_ref_mode",  "middle",
                "-c:a", "aac",
                "-b:a", "256k",
                "-c:s", "copy",
                str(dst),
            ]
        else:
            # CPU x265 — -tag:v hvc1 and -movflags are MP4-only; not used for MKV
            args = [
                "-i", str(src),
                "-map", "0",
                "-c:v", "libx265",
                "-crf",    "24",     # quality target (0 = lossless, 51 = worst)
                "-preset", "slow",
                "-c:a", "aac", "-b:a", "256k", "-c:s", "copy",
                str(dst),
            ]

    return ffmpeg_run(args, verbose)


def compress_audio(src: Path, dst: Path, mode: str, verbose: bool) -> bool:
    """
    near-lossless : AAC 256 kbps (transparent to most listeners)  → .m4a
    lossless      : FLAC level 8 (bit-perfect)                     → .flac

    FLAC level 8 vs 12: level 12 adds ~3× CPU cost for roughly 0.5% extra
    compression.  Level 8 is the real-world sweet spot for archival use.
    """
    if not check_ffmpeg():
        log.error("FFmpeg not found — install from https://ffmpeg.org/download.html")
        return False

    if mode == "lossless":
        args = ["-i", str(src), "-c:a", "flac", "-compression_level", "8", str(dst)]
    else:
        args = ["-i", str(src), "-c:a", "aac", "-b:a", "256k", "-ar", "48000", str(dst)]
    return ffmpeg_run(args, verbose)


# ---------------------------------------------------------------------------
# Core logic  (mirrors cleanup_dirs.py → delete_directory)
# ---------------------------------------------------------------------------

def compress_file(
    src: Path,
    explicit_dst: Path | None,
    mode: str,
    replace: bool,
    dry_run: bool,
    verbose: bool,
    min_size: int = 0,
    min_size_div: int = 0,
    cache: "CompressionCache | None" = None,
) -> bool:
    """
    Compress a single media file.

    --replace  Compress into a sibling temp file (same directory = same
               filesystem).  On success the original is removed and the temp
               is renamed to the final path via safe_replace(), which uses an
               atomic rename on the same FS or shutil.copy2() as a fallback.
               The original is only deleted AFTER the new file is in place.

    default    Write to <stem>_compressed<ext> beside the original.
               Skip if that output already exists.

    failsafe   If the compressed output is not smaller than the original the
               temp is discarded and the original is left untouched.

    cache      If a CompressionCache is provided, files already recorded in
               it (same mode, file unchanged) are skipped.  Every successful
               outcome (compressed or reverted) is written back to the cache.
    """
    kind = detect_type(src)
    if kind == "unknown":
        if verbose:
            log.info("SKIP (unsupported type %s): %s", src.suffix, src.name)
        with stats_lock:
            stats["skipped"] += 1
        return False

    ext    = output_ext(src, mode)
    before = file_size(src)

    # ── Cache check ───────────────────────────────────────────────────────
    if cache is not None and cache.is_done(src, mode):
        if verbose:
            log.info("SKIP (cache hit — already compressed): %s", src.name)
        with stats_lock:
            stats["skipped"] += 1
        return True

    # ── Min-size guard ────────────────────────────────────────────────────
    if min_size > 0 and before < min_size:
        if verbose:
            log.info(
                "SKIP (below --min-size %s, file is %s): %s",
                human(min_size), human(before), src.name,
            )
        with stats_lock:
            stats["skipped"] += 1
        return False

    # ── Resolve working path & final destination ──────────────────────────
    if replace:
        # FIX: always use src.parent so temp and source are on the same
        # filesystem — this prevents the cross-drive OSError on network shares.
        try:
            tmp_fd, tmp_str = tempfile.mkstemp(suffix=f"_cmptmp{ext}", dir=src.parent)
            os.close(tmp_fd)
        except OSError as exc:
            log.error("Cannot create temp file in %s — %s", src.parent, exc)
            with stats_lock:
                stats["errors"] += 1
            return False
        work_dst  = Path(tmp_str)
        final_dst = src.parent / (src.stem + ext)
    else:
        if explicit_dst is not None:
            work_dst = final_dst = explicit_dst
        else:
            work_dst = final_dst = src.parent / f"{src.stem}_compressed{ext}"
        if final_dst.exists():
            if verbose:
                log.info("SKIP (output exists): %s", final_dst.name)
            with stats_lock:
                stats["skipped"] += 1
            return False

    # ── Dry-run short-circuit ─────────────────────────────────────────────
    if dry_run:
        action = f"replace in-place → {final_dst.name}" if replace else f"→ {final_dst.name}"
        log.info("[DRY RUN] Would compress (%s): %s  %s", kind, src.name, action)
        if replace:
            work_dst.unlink(missing_ok=True)
        with stats_lock:
            stats["skipped"] += 1
        return True

    # ── Run the appropriate compressor ────────────────────────────────────
    log.info("Compressing (%s): %s", kind, src.name)
    t0 = time.monotonic()
    try:
        if kind   == "image": ok = compress_image(src, work_dst, mode)
        elif kind == "video": ok = compress_video(src, work_dst, mode, verbose)
        else:                 ok = compress_audio(src, work_dst, mode, verbose)
    except Exception as exc:
        log.error("Unexpected error on %s — %s", src.name, exc)
        ok = False

    elapsed = time.monotonic() - t0

    # ── Finalise ──────────────────────────────────────────────────────────
    if ok and work_dst.exists():
        after = file_size(work_dst)
        saved = before - after
        pct   = (saved / before * 100) if before else 0

        # ── Failsafe: discard output if compressed file is not smaller ────
        # The original is still intact here in both modes because
        # safe_replace() / src.unlink() only happen further below.
        if after + min_size_div >= before:
            work_dst.unlink(missing_ok=True)
            if min_size_div > 0:
                log.warning(
                    "REVERTED %s → %s (size with --min-size-div %s) (+%s, +%.1f%%) — output was larger with --min-size-div %s, keeping original: %s",
                    human(before), human(after), human(after + min_size_div), human(after - before), abs(pct), human(min_size_div), src.name,
                )
            else:
                log.warning(
                    "REVERTED %s → %s (+%s, +%.1f%%) — output was larger, keeping original: %s",
                    human(before), human(after), human(after - before), abs(pct), src.name,
                )
            # Cache the original so we don't keep retrying an incompressible file
            if cache is not None:
                try:
                    st = src.stat()
                    cache.mark_done(src, size=st.st_size, mtime=st.st_mtime, mode=mode)
                except OSError:
                    pass
            with stats_lock:
                stats["reverted"] += 1
            return True

        # ── Commit (saved > 0 guaranteed here) ───────────────────────────
        if replace:
            try:
                safe_replace(work_dst, final_dst, src)
            except Exception as exc:
                log.error("Replace failed for %s — %s", src.name, exc)
                work_dst.unlink(missing_ok=True)
                with stats_lock:
                    stats["errors"] += 1
                return False

        log.info(
            "  ▼ %s → %s  (%s, %.1f%%)  %.1fs",
            human(before), human(after), human(saved), pct, elapsed,
        )

        # Cache the file that now lives on disk after the operation:
        #   replace mode → final_dst (the surviving compressed file)
        #   default mode → src       (the untouched original)
        if cache is not None:
            record = final_dst if replace else src
            try:
                st = record.stat()
                cache.mark_done(record, size=st.st_size, mtime=st.st_mtime, mode=mode)
            except OSError:
                pass  # non-fatal: cache miss on next run is fine

        with stats_lock:
            stats["compressed"] += 1
            stats["bytes_saved"] += saved
        return True

    else:
        if replace:
            work_dst.unlink(missing_ok=True)
        log.error("Compression failed: %s", src.name)
        with stats_lock:
            stats["errors"] += 1
        return False


# ---------------------------------------------------------------------------
# Collection  (mirrors cleanup_dirs.py → collect_candidates)
# ---------------------------------------------------------------------------

def collect_media_files(root: str, verbose: bool) -> list[str]:
    """
    Walk the tree and return the absolute path of every recognised media file.
    Non-media files are silently skipped (or logged when verbose=True).
    """
    lp_root = to_long_path(root)
    found: list[str] = []

    for dirpath, _dirnames, filenames in os.walk(lp_root):
        for name in filenames:
            ext = Path(name).suffix.lower()
            if ext in ALL_MEDIA_EXTS:
                found.append(os.path.join(dirpath, name))
            elif verbose:
                log.info("SKIP (not media): %s", os.path.join(dirpath, name))

    return found


# ---------------------------------------------------------------------------
# Orchestration  (mirrors cleanup_dirs.py → run)
# ---------------------------------------------------------------------------

def run(
    root: str,
    thread_count: int,
    mode: str,
    replace: bool,
    dry_run: bool,
    verbose: bool,
    output: str | None,
    min_size: int,
    min_size_div: int,
    cache_path: Path | None,
) -> None:
    lp_root   = to_long_path(root)
    root_path = Path(root)

    # ── Load cache ────────────────────────────────────────────────────────
    cache: CompressionCache | None = None
    if cache_path is not None and not dry_run:
        cache = CompressionCache(cache_path)

    # ── Single-file mode ──────────────────────────────────────────────────
    if root_path.is_file():
        dst = Path(output) if output else None
        compress_file(root_path, dst, mode, replace, dry_run, verbose, min_size, min_size_div, cache)
        if cache is not None:
            cache.save()
        _print_summary()
        return

    # ── Directory mode ────────────────────────────────────────────────────
    if not os.path.isdir(lp_root):
        log.error("Path does not exist or is not a directory: %s", root)
        sys.exit(1)

    log.info("Scanning : %s", root)
    log.info("Platform : %s%s", platform.system(), " (long-path prefix active)" if IS_WINDOWS else "")
    log.info("Mode     : %s", mode)
    log.info("Replace  : %s | Min-size: %s | Min-size-div: %s | Threads: %d | Dry run: %s | Verbose: %s",
             replace, human(min_size) if min_size else "none", human(min_size_div) if min_size_div else "none", thread_count, dry_run, verbose)
    if cache_path:
        log.info("Cache    : %s", cache_path)
    print()

    # Probe GPU once before spawning worker threads (result is then cached)
    if check_ffmpeg():
        probe_gpu_encoders()

    candidates = collect_media_files(root, verbose=verbose)

    if not candidates:
        log.info("No media files found under '%s'.", root)
        if not verbose:
            log.info("Tip: re-run with --verbose to see every skipped file.")
        return

    log.info(
        "Found %d media file%s to compress.",
        len(candidates),
        "s" if len(candidates) != 1 else "",
    )
    print()

    with ThreadPoolExecutor(max_workers=thread_count) as executor:
        futures = {
            executor.submit(
                compress_file,
                Path(f), None, mode, replace, dry_run, verbose, min_size, min_size_div, cache,
            ): f
            for f in candidates
        }
        for future in as_completed(futures):
            future.result()   # exceptions are caught and logged inside compress_file

    if cache is not None:
        cache.save()

    print()
    _print_summary()


def _print_summary() -> None:
    log.info(
        "Done. Compressed: %d | Reverted: %d | Skipped: %d | Errors: %d | Total saved: %s",
        stats["compressed"],
        stats["reverted"],
        stats["skipped"],
        stats["errors"],
        human(stats["bytes_saved"]),
    )


# ---------------------------------------------------------------------------
# CLI  (mirrors cleanup_dirs.py → parse_args)
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Recursively detect and compress every image, video, and audio file "
            "in a directory tree without reducing quality."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Modes\n"
            "─────\n"
            "  near-lossless  (default)\n"
            "    Images : JPEG q=95+optimize  |  PNG compress_level=9  |  WebP q=90\n"
            "    Video  : libx265 CRF 18 slow (CPU) or HEVC/AV1-NVENC CQ 18 (GPU) → .mkv\n"
            "    Audio  : AAC 256 kbps  (transparent to most listeners)\n\n"
            "  lossless\n"
            "    Images : PNG optimize  |  WebP lossless\n"
            "    Video  : ffv1_vulkan GPU (recent FFmpeg) or FFV1 CPU → .mkv  (bit-perfect)\n"
            "    Audio  : FLAC compression=8 → .flac  (bit-perfect)\n\n"
            "Examples\n"
            "────────\n"
            "  # Compress all media in a folder, 8 threads, keep originals\n"
            "  python media_compress.py ./media 8\n\n"
            "  # Replace originals in-place (works on network drives)\n"
            "  python media_compress.py ./photos 4 --replace\n\n"
            "  # Lossless mode — preview first with dry-run\n"
            "  python media_compress.py ./media 4 --mode lossless --dry-run\n\n"
            "  # Single file with an explicit output path\n"
            "  python media_compress.py video.mp4 1 -o out.mp4\n\n"
            "  # Skip files smaller than 1 MB\n"
            "  python media_compress.py ./media 8 --min-size 1MB\n\n"
            "  # Revert files that aren't significantly smaller\n"
            "  python media_compress.py ./media 8 --min-size-div 100KB\n\n"
            "  # Rerun same folder — already-done files are skipped automatically\n"
            "  python media_compress.py ./media 8\n\n"
            "  # Custom cache file location\n"
            "  python media_compress.py ./media 8 --cache-file /tmp/progress.json\n\n"
            "  # Disable cache (force reprocess everything)\n"
            "  python media_compress.py ./media 8 --no-cache\n"
        ),
    )
    parser.add_argument("path", help="File or root directory to scan recursively")
    parser.add_argument(
        "thread_count",
        type=int,
        nargs="?",
        default=max(1, (os.cpu_count() or 4) // 2),
        help="Worker threads for parallel compression (default: half of CPU count)",
    )
    parser.add_argument(
        "--mode",
        choices=["near-lossless", "lossless"],
        default="near-lossless",
        help="Compression mode (default: near-lossless)",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help=(
            "Replace the original file instead of creating a '_compressed' copy. "
            "Temp file is always written in the same directory as the source "
            "to avoid cross-drive errors on network shares."
        ),
    )
    parser.add_argument(
        "-o", "--output",
        help="Output path — single-file mode only; mutually exclusive with --replace",
    )
    parser.add_argument(
        "--min-size",
        metavar="SIZE",
        type=parse_size,
        default=0,
        help="Skip files smaller than SIZE (e.g. 500, 1KB, 2.5MB, 1GB). Default: 0 (no limit).",
    )
    parser.add_argument(
        "--min-size-div",
        metavar="SIZE",
        type=parse_size,
        default=0,
        help="Reverts to the original file if the compressed output is not at least SIZE smaller than the original. Default: 0 (no limit).",
    )
    parser.add_argument(
        "--cache-file",
        metavar="PATH",
        help=(
            "Path to the JSON progress cache (default: <root>/.media_compress_cache.json). "
            "Files already in the cache are skipped on re-runs."
        ),
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Disable the cache — reprocess every file regardless of prior runs.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making any changes",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Log every skipped file and the reason it was passed over",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if args.replace and args.output:
        log.error("--replace and --output are mutually exclusive.")
        sys.exit(1)

    root_abs = os.path.abspath(args.path)

    # Resolve cache path (None disables the cache entirely)
    cache_path: Path | None = None
    if not args.no_cache:
        if args.cache_file:
            cache_path = Path(args.cache_file)
        else:
            root_p   = Path(root_abs)
            cache_dir = root_p if root_p.is_dir() else root_p.parent
            cache_path = cache_dir / ".media_compress_cache.json"

    run(
        root=root_abs,
        thread_count=max(1, args.thread_count),
        mode=args.mode,
        replace=args.replace,
        dry_run=args.dry_run,
        verbose=args.verbose,
        output=args.output,
        min_size=args.min_size,
        min_size_div=args.min_size_div,
        cache_path=cache_path,
    )