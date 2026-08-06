import json
import re
import subprocess
from pathlib import Path


def _parse_trace_line(line: str, pcr_start: int | None) -> tuple[str | None, dict | None]:
    """Return (stream_type, record) or (None, None) if not a superimpose line with text.

    Caption text is no longer extracted here (see Caption2AssC in _extract_caption_text) —
    this trace parser is kept only for the superimpose (文字スーパー) stream, which
    Caption2AssC does not cover.
    """
    m = re.match(r"pcrpid=0x[0-9A-Fa-f]+;pcr=(\d+)", line)
    if m:
        return ("pcr", {"pcr": int(m.group(1))})

    m = re.match(r"pts=(\d+);pcrrel=[^;]+;(?:text=[^;]+;)?b24(caption|superimpose)(\d+)=(.+)", line)
    if not m:
        return (None, None)

    stream = m.group(2)
    if stream != "superimpose":
        return (None, None)

    pts = int(m.group(1))
    lang_idx = int(m.group(3))
    raw = m.group(4)

    if lang_idx == 0:
        return (None, None)

    text = _extract_text(raw)
    if not text:
        return (None, None)

    pts_ms = (pts - pcr_start) // 90 if pcr_start is not None else None
    if pts_ms is not None and pts_ms < -10000:
        pts_ms = (pts - pcr_start + 2**33) // 90

    record = {
        "pts": pts,
        "pts_ms": pts_ms,
        "text": text,
    }
    return (stream, record)


def _extract_text(raw: str) -> str:
    parts = []
    segments = raw.split("%^G")
    for seg in segments[1:]:
        text_part = re.sub(r"%[^%\s]+", "", seg)
        text_part = text_part.strip()
        if text_part:
            parts.append(text_part)

    return "".join(parts)


_ASS_DIALOGUE_RE = re.compile(
    r"^Dialogue:\s*\d+,([\d:.]+),([\d:.]+),[^,]*,[^,]*,\d+,\d+,\d+,[^,]*,(.*)$"
)
_ASS_OVERRIDE_RE = re.compile(r"\{[^}]*\}")


def _ass_time_to_ms(t: str) -> int:
    h, m, s = t.split(":")
    sec, cs = s.split(".")
    return ((int(h) * 60 + int(m)) * 60 + int(sec)) * 1000 + int(cs) * 10


def _parse_ass(ass_path: Path) -> list[dict]:
    records: list[dict] = []
    for line in ass_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        m = _ASS_DIALOGUE_RE.match(line)
        if not m:
            continue
        start_s, end_s, raw_text = m.groups()
        text = _ASS_OVERRIDE_RE.sub("", raw_text).replace("\\N", "\n").replace("\\n", "\n").strip()
        if not text:
            continue
        records.append({
            "start_ms": _ass_time_to_ms(start_s),
            "end_ms": _ass_time_to_ms(end_s),
            "text": text,
        })
    return records


def _extract_caption_text(ts_path: Path, caption2ass_bin: Path, ass_path: Path) -> list[dict]:
    """Extract closed-caption (字幕, component_tag=0x30) text via Caption2AssC.

    Caption2AssC reuses EDCB's own Caption.dll, which properly interprets ARIB
    STD-B24 control-code parameter lengths — unlike a regex over tsreadex's raw
    trace output, which cannot tell control-code parameter bytes apart from
    literal caption text (both are unescaped printable bytes on the wire).

    Writes the raw .ass output to ass_path (kept alongside the jsonl, not discarded).
    """
    target_base = ass_path.with_suffix("")  # Caption2AssC appends ".ass" itself
    proc = subprocess.run(
        [str(caption2ass_bin), "-format", "ass", str(ts_path), str(target_base)],
        capture_output=True,
    )
    if not ass_path.exists():
        if proc.returncode != 0:
            print(f"[caption] Caption2AssC failed (rc={proc.returncode}): "
                  f"{proc.stderr.decode('utf-8', errors='replace')}")
        return []
    return _parse_ass(ass_path)


def extract_captions(ts_path: Path, out_dir: Path, base_id: str, tsreadex_bin: Path,
                      caption2ass_bin: Path) -> dict:
    """Extract captions (via Caption2AssC) and superimpose (via tsreadex trace),
    writing .caption.jsonl, .caption.ass, and .super.jsonl.
    Returns {"pcr_start": int, "caption_count": int, "super_count": int}.
    """
    caption_path = out_dir / f"{base_id}.caption.jsonl"
    ass_path = out_dir / f"{base_id}.caption.ass"
    super_path = out_dir / f"{base_id}.super.jsonl"

    caption_records = _extract_caption_text(ts_path, caption2ass_bin, ass_path)

    proc = subprocess.Popen(
        [str(tsreadex_bin), "-n", "-1", "-r", "-", "-t", "10", str(ts_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    pcr_start: int | None = None
    super_records: list[dict] = []

    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue

        stream_type, record = _parse_trace_line(line, pcr_start)
        if stream_type == "pcr":
            if pcr_start is None:
                pcr_start = record["pcr"]
        elif stream_type == "superimpose" and record:
            super_records.append(record)

    proc.wait()

    caption_path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in caption_records),
        encoding="utf-8",
    )
    super_path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in super_records),
        encoding="utf-8",
    )

    return {
        "pcr_start": pcr_start,
        "caption_count": len(caption_records),
        "super_count": len(super_records),
    }
