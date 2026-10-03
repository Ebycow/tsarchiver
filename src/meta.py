import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

JST = timezone(timedelta(hours=9))


def _read_text(path: Path) -> str:
    for enc in ("utf-16", "utf-8-sig", "cp932"):
        try:
            return path.read_text(encoding=enc)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def parse_program_txt(path: Path) -> dict:
    lines = [l.rstrip() for l in _read_text(path).splitlines()]
    if not lines:
        return {}

    result: dict = {}

    # Line 0: "2026/07/11(土) 22:55～23:00"
    m = re.match(r"(\d{4}/\d{2}/\d{2})(?:\(.+?\))?\s+(\d{2}:\d{2})～(\d{2}:\d{2})", lines[0])
    if m:
        date = m.group(1).replace("/", "-")
        result["scheduled_start"] = f"{date}T{m.group(2)}:00+09:00"
        result["scheduled_end"] = f"{date}T{m.group(3)}:00+09:00"

    result["channel_name"] = lines[1] if len(lines) > 1 else ""

    raw_title = lines[2] if len(lines) > 2 else ""
    result["flags"] = re.findall(r"\[(.+?)\]", raw_title)
    result["title"] = re.sub(r"\[.+?\]", "", raw_title).strip()

    genres: list[str] = []
    audio_mode = "mono"
    audio_languages: list[str] = []
    in_genre = False
    in_audio_lang = False
    # 音声欄には音声コンポーネントが複数並ぶことがある（[二]の二か国語放送、[解]の解説音声など）。
    # 例: "音声 : 2/0モード（ステレオ）" / "日本語" / "サンプリングレート : 48kHz" /
    #     "2/0モード（ステレオ）" / "英語" / "サンプリングレート : 48kHz"
    in_audio_block = False
    want_component_lang = False
    audio_components = 0
    component_languages: list[str] = []

    for line in lines[3:]:
        if line.startswith("ジャンル"):
            in_genre, in_audio_lang, in_audio_block = True, False, False
            continue
        if line.startswith("映像"):
            in_genre, in_audio_lang, in_audio_block = False, False, False
            m2 = re.search(r"(\d{3,4}[ip]?)", line)
            result["video_resolution"] = m2.group(1) if m2 else ""
            continue
        if line.startswith("音声"):
            in_genre, in_audio_block, want_component_lang = False, True, True
            audio_components = 1
            audio_line = line.split(":", 1)[-1]
            if "デュアルモノ" in audio_line:
                audio_mode, in_audio_lang = "dual_mono", True
            elif "ステレオ" in audio_line:
                audio_mode = "stereo"
            else:
                audio_mode = "mono"
            continue
        if in_audio_block and "モード" in line:
            audio_components += 1
            want_component_lang = True
            continue
        if line.startswith("サンプリングレート"):
            in_audio_lang = want_component_lang = False
            continue
        if in_audio_block and re.match(r"\w+ID:", line):
            in_audio_block = False
        if want_component_lang and line.strip():
            component_languages.append(line.strip())
            want_component_lang = False
        if in_audio_lang and line.strip():
            audio_languages.append(line.strip())
            continue
        if in_audio_block:
            continue
        if in_genre and line.strip():
            genres.append(line.strip())
            continue

        for key, attr in (
            ("OriginalNetworkID", "onid"),
            ("TransportStreamID", "tsid"),
            ("ServiceID", "sid"),
            ("EventID", "eid"),
        ):
            m3 = re.match(rf"{key}:(\d+)", line)
            if m3:
                result[attr] = int(m3.group(1))

    if audio_mode != "dual_mono" and audio_components >= 2:
        # 別PIDの音声が複数ある（主音声・副音声）。audio.py で両方保存する
        audio_mode = "multi_audio"
        audio_languages = component_languages

    result["genres"] = genres
    result["audio_mode"] = audio_mode
    result["audio_languages"] = audio_languages or (["jpn"] if audio_mode != "dual_mono" else [])
    return result


def parse_err_file(path: Path) -> dict:
    text = _read_text(path)
    pid_stats = []
    bondriver = ""
    drop_total = scramble_total = 0

    for line in text.splitlines():
        m = re.match(
            r"PID:\s*(0x[0-9A-Fa-f]+)\s+Total:\s*(\d+)\s+Drop:\s*(\d+)\s+Scramble:\s*(\d+)\s+(.*)",
            line,
        )
        if m:
            drop, scramble = int(m.group(3)), int(m.group(4))
            drop_total += drop
            scramble_total += scramble
            pid_stats.append({
                "pid": m.group(1),
                "label": m.group(5).strip(),
                "total": int(m.group(2)),
                "drop": drop,
                "scramble": scramble,
            })
        m2 = re.match(r"使用BonDriver\s*:\s*(.+)", line)
        if m2:
            bondriver = m2.group(1).strip()

    return {
        "pid_stats": pid_stats,
        "drop_total": drop_total,
        "scramble_total": scramble_total,
        "bondriver": bondriver,
    }


def run_ffprobe(ts_path: Path, ffprobe_bin: Path) -> dict:
    # 録画中にPIDが切り替わる(番組冒頭のごく短い断片が別PID構成で入る等の)TSでは、
    # デフォルトの探索窓だと切り替え後の本編PIDまで見つけきれず、コンテナ全体の
    # format.durationが取れずに0になることがある。audio.pyの_PROBE_ARGSと同じ値で
    # 探索窓を広げ、確実に本編PIDまで見つける。
    r = subprocess.run(
        [str(ffprobe_bin), "-v", "quiet",
         "-analyzeduration", "15000000", "-probesize", "50000000",
         "-print_format", "json",
         "-show_streams", "-show_format", str(ts_path)],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    if r.returncode != 0 or not r.stdout:
        return {}
    try:
        return json.loads(r.stdout.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


def generate_base_id(meta: dict) -> str:
    if not meta.get("scheduled_start"):
        raise ValueError("scheduled_start missing from meta")
    dt = datetime.fromisoformat(meta["scheduled_start"])
    ts_part = dt.strftime("%Y%m%dT%H%M%S")
    onid = meta.get("onid") or 0
    tsid = meta.get("tsid") or 0
    sid = meta.get("sid") or 0
    eid = meta.get("eid")
    eid_str = f"e{eid:04x}" if eid is not None else "e0000"
    base_id = f"{ts_part}_{onid}-{tsid}-{sid}_{eid_str}"
    if not (onid and sid) or eid is None:
        # .program.txt が無いとサービス/イベントを識別できず、同時刻に別チューナーで
        # 録った番組同士が同じbase_idになって出力を上書きし合う。TSファイル名
        # (EDCBが録画ごとに一意に付ける)のハッシュを付けて衝突を防ぐ。
        name = Path(meta["source"]["original_path"]).name
        base_id += "_" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
    return base_id


_FNAME_DT_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})\d*-")


def _parse_datetime_from_filename(ts_path: Path) -> str | None:
    """Fallback for when .program.txt is missing: EDCB's default filename starts
    with the recording start time as YYYYMMDDHHMMSS (e.g. 202607281800010103-...).
    """
    m = _FNAME_DT_RE.match(ts_path.name)
    if not m:
        return None
    y, mo, d, h, mi, s = m.groups()
    return f"{y}-{mo}-{d}T{h}:{mi}:{s}+09:00"


def build_meta(
    ts_path: Path,
    program_txt: Path | None,
    err_path: Path | None,
    ffprobe_bin: Path,
) -> dict:
    prog = parse_program_txt(program_txt) if program_txt and program_txt.exists() else {}
    err = parse_err_file(err_path) if err_path and err_path.exists() else {}
    probe = run_ffprobe(ts_path, ffprobe_bin)

    scheduled_start = prog.get("scheduled_start") or _parse_datetime_from_filename(ts_path)

    fmt = probe.get("format", {})
    streams = probe.get("streams", [])
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), {})
    sub_streams = [s for s in streams if s.get("codec_type") == "subtitle"]

    meta = {
        "title": prog.get("title", ""),
        "flags": prog.get("flags", []),
        "channel_name": prog.get("channel_name", ""),
        "onid": prog.get("onid"),
        "tsid": prog.get("tsid"),
        "sid": prog.get("sid"),
        "eid": prog.get("eid"),
        "eid_available": prog.get("eid") is not None,
        "scheduled_start": scheduled_start,
        "scheduled_end": prog.get("scheduled_end"),
        "actual_start": None,
        "genres": prog.get("genres", []),
        "video": {
            "codec": video_stream.get("codec_name", ""),
            "resolution": f"{video_stream.get('width', '')}x{video_stream.get('height', '')}",
            "display_aspect": video_stream.get("display_aspect_ratio", ""),
            "frame_rate": video_stream.get("r_frame_rate", ""),
            "field_order": video_stream.get("field_order", ""),
        },
        "audio": {
            "mode": prog.get("audio_mode", "mono"),
            "languages": prog.get("audio_languages", ["jpn"]),
            "sample_rate": int(audio_stream.get("sample_rate", 48000)),
            "pid": audio_stream.get("id", ""),
        },
        "subtitles": {
            "caption_pid": sub_streams[0].get("id", "") if len(sub_streams) > 0 else "",
            "superimpose_pid": sub_streams[1].get("id", "") if len(sub_streams) > 1 else "",
        },
        "source": {
            "original_path": str(ts_path),
            "file_size_bytes": int(fmt.get("size", 0)),
            "duration_sec": float(fmt.get("duration", 0)),
            "bondriver": err.get("bondriver", ""),
        },
        "quality": {
            "drop_total": err.get("drop_total", 0),
            "scramble_total": err.get("scramble_total", 0),
            "pid_stats": err.get("pid_stats", []),
        },
        "derived": {
            "processed_at": None,
            "raw_path": None,
            "pcr_start": None,
        },
    }

    meta["base_id"] = generate_base_id(meta)
    return meta
