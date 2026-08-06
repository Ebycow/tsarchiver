import io
import struct
import subprocess
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image

PHASH_MAGIC = b"PHSH"
PHASH_VERSION = 1
# Per-frame record: pts_ms(4) + dhash(8) + phash(8) + hist_r(512) + hist_g(512) + hist_b(512) = 1556 bytes
FRAME_SIZE_HASH = 128 * 72 * 3  # RGB24 at 128x72 for hash/hist computation


def _dhash(gray: np.ndarray) -> int:
    """Difference hash: resize to 9x8 grayscale, compare adjacent pixels."""
    img = Image.fromarray(gray).resize((9, 8), Image.LANCZOS)
    arr = np.array(img)
    diff = arr[:, 1:] > arr[:, :-1]  # shape (8,8)
    bits = diff.flatten()
    result = 0
    for b in bits:
        result = (result << 1) | int(b)
    return result


def _phash(gray: np.ndarray) -> int:
    """Perceptual hash: 32x32 DCT, top-left 8x8 compared to median."""
    img = Image.fromarray(gray).resize((32, 32), Image.LANCZOS)
    arr = np.array(img, dtype=float)

    # 2D DCT-II via FFT
    def dct1d(x: np.ndarray) -> np.ndarray:
        n = x.shape[-1]
        v = np.concatenate([x, x[..., ::-1]], axis=-1)
        V = np.fft.rfft(v, axis=-1)
        k = np.arange(n)
        W = np.exp(-1j * np.pi * k / (2 * n))
        return (V[..., :n] * W).real * 2

    dct2d = dct1d(dct1d(arr).T).T
    low = dct2d[:8, :8]
    med = np.median(low)
    bits = (low.flatten() > med).astype(int)
    result = 0
    for b in bits:
        result = (result << 1) | int(b)
    return result


def _color_hist(rgb: np.ndarray) -> bytes:
    """256-bin histogram per channel, normalized to uint16."""
    total = rgb.shape[0] * rgb.shape[1]
    out = bytearray()
    for ch in range(3):
        counts, _ = np.histogram(rgb[:, :, ch], bins=256, range=(0, 256))
        normalized = (counts / total * 65535).astype(np.uint16)
        out += normalized.tobytes()
    return bytes(out)


def compute_phash(
    ts_path: Path,
    out_dir: Path,
    base_id: str,
    tsreadex_bin: Path,
    ffmpeg_bin: Path,
    phash_fps: int,
) -> Path:
    out_path = out_dir / f"{base_id}.phash.bin"

    proc_ts = subprocess.Popen(
        [str(tsreadex_bin), "-n", "-1", str(ts_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    proc_ff = subprocess.Popen(
        [
            str(ffmpeg_bin), "-y",
            "-i", "pipe:0",
            "-vf", f"yadif=0,fps={phash_fps},scale=128:72",
            "-f", "rawvideo",
            "-pix_fmt", "rgb24",
            "pipe:1",
        ],
        stdin=proc_ts.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    proc_ts.stdout.close()

    records: list[bytes] = []
    frame_idx = 0

    while True:
        chunk = proc_ff.stdout.read(FRAME_SIZE_HASH)
        if len(chunk) < FRAME_SIZE_HASH:
            break

        rgb = np.frombuffer(chunk, dtype=np.uint8).reshape(72, 128, 3)
        gray = np.array(Image.fromarray(rgb).convert("L"))

        pts_ms = frame_idx * (1000 // phash_fps)
        dh = _dhash(gray)
        ph = _phash(gray)
        hist = _color_hist(rgb)

        records.append(struct.pack("<IQQ", pts_ms, dh, ph) + hist)
        frame_idx += 1

    proc_ff.wait()
    proc_ts.wait()

    with open(out_path, "wb") as f:
        # Header: magic(4) + version(1) + reserved(3) + frame_count(4)
        f.write(PHASH_MAGIC)
        f.write(bytes([PHASH_VERSION, 0, 0, 0]))
        f.write(struct.pack("<I", len(records)))
        for rec in records:
            f.write(rec)

    return out_path


def generate_thumbs(
    ts_path: Path,
    out_dir: Path,
    base_id: str,
    tsreadex_bin: Path,
    ffmpeg_bin: Path,
    thumb_interval_sec: int,
    thumb_width: int,
    thumb_height: int,
) -> Path:
    out_path = out_dir / f"{base_id}.thumbs.zip"

    # Output individual JPEGs to a temp subdir, then zip
    tmp_dir = out_dir / f"_thumbs_tmp_{base_id}"
    tmp_dir.mkdir(exist_ok=True)

    proc_ts = subprocess.Popen(
        [str(tsreadex_bin), "-n", "-1", str(ts_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    proc_ff = subprocess.Popen(
        [
            str(ffmpeg_bin), "-y",
            "-i", "pipe:0",
            "-vf", f"yadif=0,fps=1/{thumb_interval_sec},scale={thumb_width}:{thumb_height}",
            "-q:v", "5",
            str(tmp_dir / "%09d.jpg"),
        ],
        stdin=proc_ts.stdout,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc_ts.stdout.close()
    proc_ff.wait()
    proc_ts.wait()

    # Pack into ZIP (stored, no compression)
    jpg_files = sorted(tmp_dir.glob("*.jpg"))
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_STORED) as zf:
        for jpg in jpg_files:
            # Entry name encodes pts_offset_ms: filename is frame index, convert to ms
            idx = int(jpg.stem)
            pts_ms = idx * thumb_interval_sec * 1000
            entry_name = f"{pts_ms:09d}.jpg"
            zf.write(jpg, entry_name)

    # Clean up temp dir
    for jpg in jpg_files:
        jpg.unlink()
    tmp_dir.rmdir()

    return out_path
