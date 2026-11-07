import json
import subprocess
from pathlib import Path

# A high-bitrate 1440x1080 video PID can dominate ffmpeg/ffprobe's default probing
# window (~5MB / ~5s) badly enough that a second audio PID starting a few seconds in
# (observed ~7.6s) never gets discovered. Force a generous window on every ffmpeg/ffprobe
# invocation here so stream discovery is reliable regardless of default heuristics.
_PROBE_ARGS = ["-analyzeduration", "15000000", "-probesize", "50000000"]


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
        streams, video_start = _probe_audio_streams(ts_path, tsreadex_bin, ffprobe_bin)
        stream_channels = [ch for ch, _ in streams]

        if len(stream_channels) >= 2:
            delays = _aligned_delays_ms(ts_path, streams, video_start, tsreadex_bin, ffprobe_bin)
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
                    delay_ms=delays[ch_idx - 1],
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

    if audio_mode == "multi_audio":
        # Separate audio components per the EPG (e.g. [二] 主音声=日本語 / 副音声=英語 as two
        # stereo PIDs 0x110/0x111). Keeping only 0:a:0 would silently drop the second language.
        streams, video_start = _probe_audio_streams(ts_path, tsreadex_bin, ffprobe_bin)
        if len(streams) >= 2:
            delays = _aligned_delays_ms(ts_path, streams, video_start, tsreadex_bin, ffprobe_bin)
            for idx, ch_name in ((0, "1"), (1, "2")):
                out_path = out_dir / f"{base_id}.{ch_name}.opus"
                _run_audio_extraction(
                    ts_path, out_path,
                    tsreadex_args=["-n", "-1", "-a", "8"],
                    ffmpeg_map=f"0:a:{idx}",
                    tsreadex_bin=tsreadex_bin,
                    ffmpeg_bin=ffmpeg_bin,
                    opus_bitrate=opus_bitrate,
                    delay_ms=delays[idx],
                )
                outputs.append(out_path)
            return outputs, "multi_audio"

        # The EPG listed a second component but the stream doesn't carry it.
        audio_mode = "stereo"

    if audio_mode != "dual_mono":
        # "-a 8" is included even here: omitting it changed tsreadex's output for a
        # multi-PID source in testing (see module docstring context), so always pass it
        # rather than relying on it being a no-op for non-dual-mono sources.
        out_path = out_dir / f"{base_id}.1.opus"
        _run_audio_extraction(
            ts_path, out_path,
            tsreadex_args=["-n", "-1", "-a", "8"],
            ffmpeg_map="0:a:0",
            tsreadex_bin=tsreadex_bin,
            ffmpeg_bin=ffmpeg_bin,
            opus_bitrate=opus_bitrate,
        )
        outputs.append(out_path)

    return outputs, audio_mode


def _probe_audio_streams(
    ts_path: Path, tsreadex_bin: Path, ffprobe_bin: Path,
) -> tuple[list[tuple[int, float | None]], float | None]:
    """((channel count, start_time) of each audio stream, video start_time) in the
    tsreadex(-a 8)-demuxed output, audio streams in stream order.

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
        [str(ffprobe_bin), "-v", "quiet", *_PROBE_ARGS, "-print_format", "json", "-show_streams", "pipe:0"],
        stdin=proc_ts.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    proc_ts.stdout.close()
    proc_ts.wait()
    try:
        streams = json.loads(proc_probe.stdout.decode(errors="replace")).get("streams", [])
    except json.JSONDecodeError:
        return [], None
    audio = [
        (int(s.get("channels", 0)), _to_float(s.get("start_time")))
        for s in streams if s.get("codec_type") == "audio"
    ]
    video_start = next(
        (_to_float(s.get("start_time")) for s in streams if s.get("codec_type") == "video"), None
    )
    return audio, video_start


# How far an audio stream's start may legitimately sit from the video's start. 副音声 0x111
# normally begins ~8s after the video; a stale leftover packet is off by tens of minutes to hours.
_MAX_START_SKEW_SEC = 60.0


def _first_pts_near(
    ts_path: Path, audio_idx: int, ref: float, tsreadex_bin: Path, ffprobe_bin: Path,
) -> float | None:
    """PTS (sec) of the first packet of audio stream `audio_idx` that lies within
    _MAX_START_SKEW_SEC of `ref`, skipping stale leading packets. None if never found."""
    proc_ts = subprocess.Popen(
        [str(tsreadex_bin), "-n", "-1", "-a", "8", str(ts_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    proc_probe = subprocess.Popen(
        [str(ffprobe_bin), "-v", "quiet", *_PROBE_ARGS,
         "-select_streams", f"a:{audio_idx}",
         "-show_entries", "packet=pts_time", "-of", "csv=p=0", "pipe:0"],
        stdin=proc_ts.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    proc_ts.stdout.close()
    found = None
    try:
        for line in proc_probe.stdout:
            pts = _to_float(line.strip().rstrip(","))
            if pts is not None and abs(pts - ref) <= _MAX_START_SKEW_SEC:
                found = pts
                break
    finally:
        proc_probe.kill()
        proc_ts.kill()
        proc_probe.wait()
        proc_ts.wait()
    return found


def _aligned_delays_ms(
    ts_path: Path,
    streams: list[tuple[int, float | None]],
    video_start: float | None,
    tsreadex_bin: Path,
    ffprobe_bin: Path,
) -> list[int]:
    """_start_delays_ms, after replacing bogus stream start_times.

    A [二] broadcast's 副音声 PID can carry a stale leftover packet at the head of the recording
    whose PTS is hours away from the programme (observed: video at 4925s, 0x111's first packet at
    1212s, real 0x111 audio from 4933s). ffprobe reports that stale PTS as the stream's start_time,
    and trusting it padded the *other* track's head with hours of silence. Any start_time that
    lies too far from the video's start is re-derived from the first plausible packet.
    """
    starts = [st for _, st in streams]
    if video_start is not None:
        for i, st in enumerate(starts):
            if st is not None and abs(st - video_start) > _MAX_START_SKEW_SEC:
                fixed = _first_pts_near(ts_path, i, video_start, tsreadex_bin, ffprobe_bin)
                print(f"[audio] a:{i} start_time {st:.3f} is far from video start "
                      f"{video_start:.3f}; using first nearby packet {fixed}")
                starts[i] = fixed
    return _start_delays_ms(starts)


def _to_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _start_delays_ms(starts: list[float | None]) -> list[int]:
    """How late each audio stream starts relative to the earliest one, in ms.

    Separate audio PIDs don't start together (副音声 0x111 observed ~7s after 0x110), and each
    opus file would otherwise begin at its own PID's first packet. Padding each track's head by
    this much keeps all tracks on a shared t=0.
    """
    known = [st for st in starts if st is not None]
    if not known:
        return [0] * len(starts)
    base = min(known)
    return [round((st - base) * 1000) if st is not None else 0 for st in starts]


def _run_audio_extraction(
    ts_path: Path,
    out_path: Path,
    tsreadex_args: list[str],
    ffmpeg_map: str,
    tsreadex_bin: Path,
    ffmpeg_bin: Path,
    opus_bitrate: str,
    select_channel: int | None = None,
    delay_ms: int = 0,
) -> None:
    # select_channel picks that channel verbatim (for a single PID carrying two languages
    # as separate channels); otherwise "-ac 1" downmixes normally (fine for a PID that's
    # already single-language, or genuine mono/stereo sources).
    audio_args = ["-af", f"pan=mono|c0=c{select_channel}"] if select_channel is not None else ["-ac", "1"]
    if delay_ms > 0:
        # Explicit head padding (see _start_delays_ms). aresample=async:first_pts=0 was tried
        # and does not pad here: the encoded opus still starts at the PID's first packet.
        audio_args = ["-af", f"adelay=delays={delay_ms}:all=1", *audio_args]

    proc_ts = subprocess.Popen(
        [str(tsreadex_bin)] + tsreadex_args + [str(ts_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    proc_ff = subprocess.Popen(
        [
            str(ffmpeg_bin), "-y",
            *_PROBE_ARGS,
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
