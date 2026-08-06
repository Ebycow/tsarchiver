import json
import subprocess
from pathlib import Path


def extract_audio(
    ts_path: Path,
    out_dir: Path,
    base_id: str,
    audio_mode: str,
    tsreadex_bin: Path,
    ffmpeg_bin: Path,
    ffprobe_bin: Path,
    opus_bitrate: str,
) -> tuple[list[Path], str]:
    """Extract audio as opus. Returns (output paths, actual audio_mode used).

    The EPG-reported audio_mode (program.txt "デュアルモノ") is sometimes wrong: some
    dual-mono broadcasts never get split into two PIDs by tsreadex -a 8 and stay as a
    single AAC stream with the two languages sitting in channels 0/1. Probe the actual
    stream layout and, when only one stream comes out, take channel 0 (the first-listed/
    main language) only -- a plain "-ac 1" downmix would average both channels together
    and audibly blend the two languages.
    """
    outputs: list[Path] = []

    if audio_mode == "dual_mono" and _count_audio_streams(ts_path, tsreadex_bin, ffprobe_bin) < 2:
        out_path = out_dir / f"{base_id}.1.opus"
        _run_audio_extraction(
            ts_path, out_path,
            tsreadex_args=["-n", "-1", "-a", "8"],
            ffmpeg_map="0:a:0",
            tsreadex_bin=tsreadex_bin,
            ffmpeg_bin=ffmpeg_bin,
            opus_bitrate=opus_bitrate,
            select_first_channel=True,
        )
        return [out_path], "mono"

    if audio_mode == "dual_mono":
        # tsreadex -a 8 splits dual-mono into two separate mono streams (PID 0x0110 + 0x0111)
        for ch_idx, ch_name in ((1, "1"), (2, "2")):
            out_path = out_dir / f"{base_id}.{ch_name}.opus"
            _run_audio_extraction(
                ts_path, out_path,
                tsreadex_args=["-n", "-1", "-a", "8"],
                ffmpeg_map=f"0:a:{ch_idx - 1}",
                tsreadex_bin=tsreadex_bin,
                ffmpeg_bin=ffmpeg_bin,
                opus_bitrate=opus_bitrate,
            )
            outputs.append(out_path)
    else:
        out_path = out_dir / f"{base_id}.1.opus"
        _run_audio_extraction(
            ts_path, out_path,
            tsreadex_args=["-n", "-1"],
            ffmpeg_map="0:a:0",
            tsreadex_bin=tsreadex_bin,
            ffmpeg_bin=ffmpeg_bin,
            opus_bitrate=opus_bitrate,
        )
        outputs.append(out_path)

    return outputs, audio_mode


def _count_audio_streams(ts_path: Path, tsreadex_bin: Path, ffprobe_bin: Path) -> int:
    """Count audio streams in the tsreadex(-a 8)-demuxed output, same pipe used for extraction.

    Uses -show_streams (flat "streams" list) rather than -select_streams a with a CSV writer:
    the latter emits each stream once per program grouping *and* once in the flat list, so it
    double-counts and always reports >=2 for single-audio sources.
    """
    proc_ts = subprocess.Popen(
        [str(tsreadex_bin), "-n", "-1", "-a", "8", str(ts_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    proc_probe = subprocess.run(
        [str(ffprobe_bin), "-v", "quiet", "-print_format", "json", "-show_streams", "pipe:0"],
        stdin=proc_ts.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    proc_ts.stdout.close()
    proc_ts.wait()
    try:
        streams = json.loads(proc_probe.stdout.decode(errors="replace")).get("streams", [])
    except json.JSONDecodeError:
        return 0
    return sum(1 for s in streams if s.get("codec_type") == "audio")


def _run_audio_extraction(
    ts_path: Path,
    out_path: Path,
    tsreadex_args: list[str],
    ffmpeg_map: str,
    tsreadex_bin: Path,
    ffmpeg_bin: Path,
    opus_bitrate: str,
    select_first_channel: bool = False,
) -> None:
    # select_first_channel picks channel 0 verbatim (for an unsplit dual-mono stream);
    # otherwise "-ac 1" downmixes normally (fine for genuine mono/stereo sources).
    audio_args = ["-af", "pan=mono|c0=c0"] if select_first_channel else ["-ac", "1"]

    proc_ts = subprocess.Popen(
        [str(tsreadex_bin)] + tsreadex_args + [str(ts_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    proc_ff = subprocess.Popen(
        [
            str(ffmpeg_bin), "-y",
            "-i", "pipe:0",
            "-map", ffmpeg_map,
            "-c:a", "libopus",
            "-b:a", opus_bitrate,
            *audio_args,
            "-vn",
            str(out_path),
        ],
        stdin=proc_ts.stdout,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    proc_ts.stdout.close()
    _, ff_err = proc_ff.communicate()
    proc_ts.wait()

    if proc_ff.returncode != 0:
        raise RuntimeError(f"ffmpeg audio failed: {ff_err.decode(errors='replace')[-500:]}")
