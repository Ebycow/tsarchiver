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

    "デュアルモノ" broadcasts come in two different transport layouts, and which one a
    given recording uses is not predictable from the EPG text alone:
      - two separate audio PIDs (0x110 主音声 + 0x111 副音声), each already carrying a
        single language duplicated across its two AAC channels (L≈R) -- downmixing each
        PID with "-ac 1" is safe.
      - a single PID whose one AAC stream packs both languages as distinct channels
        (ch0=主音声, ch1=副音声) -- downmixing this with "-ac 1" averages the two
        languages together into an audibly blended mess. Each channel must be pulled out
        individually instead.
    Probe the actual PID/channel layout tsreadex hands back and pick the matching strategy,
    so both languages are always archived either way.
    """
    outputs: list[Path] = []

    if audio_mode == "dual_mono":
        stream_channels = _probe_audio_stream_channels(ts_path, tsreadex_bin, ffprobe_bin)

        if len(stream_channels) >= 2:
            # Two separate PIDs already present -- each is single-language, plain downmix is safe.
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
            return outputs, "dual_mono"

        if len(stream_channels) == 1 and stream_channels[0] >= 2:
            # One PID, two languages interleaved as its channels -- extract each channel
            # on its own rather than downmixing (which would blend both languages).
            for channel, ch_name in ((0, "1"), (1, "2")):
                out_path = out_dir / f"{base_id}.{ch_name}.opus"
                _run_audio_extraction(
                    ts_path, out_path,
                    tsreadex_args=["-n", "-1", "-a", "8"],
                    ffmpeg_map="0:a:0",
                    tsreadex_bin=tsreadex_bin,
                    ffmpeg_bin=ffmpeg_bin,
                    opus_bitrate=opus_bitrate,
                    select_channel=channel,
                )
                outputs.append(out_path)
            return outputs, "dual_mono"

        # Genuinely single-channel despite the EPG dual-mono flag -- nothing to split.
        audio_mode = "mono"

    if audio_mode != "dual_mono":
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


def _probe_audio_stream_channels(ts_path: Path, tsreadex_bin: Path, ffprobe_bin: Path) -> list[int]:
    """Channel count of each audio stream in the tsreadex(-a 8)-demuxed output, in stream order.

    Uses -show_streams (flat "streams" list) rather than -select_streams a with a CSV writer:
    the latter emits each stream once per program grouping *and* once in the flat list, so it
    double-counts and always reports >=2 streams even for single-audio sources.
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
        return []
    return [int(s.get("channels", 0)) for s in streams if s.get("codec_type") == "audio"]


def _run_audio_extraction(
    ts_path: Path,
    out_path: Path,
    tsreadex_args: list[str],
    ffmpeg_map: str,
    tsreadex_bin: Path,
    ffmpeg_bin: Path,
    opus_bitrate: str,
    select_channel: int | None = None,
) -> None:
    # select_channel picks that channel verbatim (for a single PID carrying two languages
    # as separate channels); otherwise "-ac 1" downmixes normally (fine for a PID that's
    # already single-language, or genuine mono/stereo sources).
    audio_args = ["-af", f"pan=mono|c0=c{select_channel}"] if select_channel is not None else ["-ac", "1"]

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
