import json
import shutil
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from . import config as cfg_mod
from . import meta as meta_mod
from . import caption as caption_mod
from . import audio as audio_mod
from . import video as video_mod

JST = timezone(timedelta(hours=9))


def _now() -> str:
    return datetime.now(JST).isoformat()


def _update_job(job_path: Path, updates: dict) -> None:
    data = json.loads(job_path.read_text(encoding="utf-8"))
    data.update(updates)
    job_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _step_start(job_path: Path, step: str) -> None:
    data = json.loads(job_path.read_text(encoding="utf-8"))
    data["current_step"] = step
    data.setdefault("steps", {})[step] = {"status": "running", "started_at": _now()}
    job_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _step_done(job_path: Path, step: str, t0: float, **extra) -> None:
    data = json.loads(job_path.read_text(encoding="utf-8"))
    data["steps"][step].update({"status": "done", "duration_sec": round(time.time() - t0, 1), **extra})
    job_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def process_job(job_file: Path, cfg: cfg_mod.Config) -> None:
    """Full processing pipeline for one job. Raises on failure."""
    job = json.loads(job_file.read_text(encoding="utf-8"))
    ts_path = Path(job["ts_path"])
    program_txt = Path(job["program_txt"]) if job.get("program_txt") else None
    err_path = Path(job["err_path"]) if job.get("err_path") else None

    # ── Step 1: meta ──────────────────────────────────────────────
    _step_start(job_file, "meta")
    t0 = time.time()
    meta = meta_mod.build_meta(ts_path, program_txt, err_path, cfg.ffprobe)
    base_id = meta["base_id"]

    dt_str = meta.get("scheduled_start", "")
    if dt_str:
        dt = datetime.fromisoformat(dt_str)
        out_dir = cfg.archive_derived / dt.strftime("%Y/%m/%d") / base_id
    else:
        out_dir = cfg.archive_derived / "unknown" / base_id
    out_dir.mkdir(parents=True, exist_ok=True)

    meta_path = out_dir / f"{base_id}.meta.json"
    meta["derived"]["processed_at"] = _now()
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    _update_job(job_file, {"base_id": base_id, "out_dir": str(out_dir)})
    _step_done(job_file, "meta", t0)

    # ── Step 2: captions ──────────────────────────────────────────
    _step_start(job_file, "caption")
    t0 = time.time()
    cap_result = caption_mod.extract_captions(ts_path, out_dir, base_id, cfg.tsreadex, cfg.caption2ass)
    # Store pcr_start back into meta.json
    if cap_result["pcr_start"] is not None:
        meta["derived"]["pcr_start"] = cap_result["pcr_start"]
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    _step_done(job_file, "caption", t0,
               caption_count=cap_result["caption_count"],
               super_count=cap_result["super_count"])

    # ── Step 3: audio ─────────────────────────────────────────────
    _step_start(job_file, "audio")
    t0 = time.time()
    audio_paths, actual_audio_mode = audio_mod.extract_audio(
        ts_path, out_dir, base_id,
        meta["audio"]["mode"],
        cfg.tsreadex, cfg.ffmpeg, cfg.ffprobe, cfg.opus_bitrate,
    )
    if actual_audio_mode != meta["audio"]["mode"]:
        meta["audio"]["mode"] = actual_audio_mode
        meta["audio"]["languages"] = meta["audio"]["languages"][:1] or ["jpn"]
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    _step_done(job_file, "audio", t0, tracks=len(audio_paths))

    # ── Step 4: phash ─────────────────────────────────────────────
    _step_start(job_file, "phash")
    t0 = time.time()
    phash_path = video_mod.compute_phash(
        ts_path, out_dir, base_id,
        cfg.tsreadex, cfg.ffmpeg, cfg.phash_fps,
    )
    frame_count = _read_phash_frame_count(phash_path)
    _step_done(job_file, "phash", t0, frame_count=frame_count)

    # ── Step 5: thumbs ────────────────────────────────────────────
    _step_start(job_file, "thumbs")
    t0 = time.time()
    video_mod.generate_thumbs(
        ts_path, out_dir, base_id,
        cfg.tsreadex, cfg.ffmpeg,
        cfg.thumb_interval_sec, cfg.thumb_width, cfg.thumb_height,
    )
    _step_done(job_file, "thumbs", t0)

    # ── Step 6: verify ────────────────────────────────────────────
    _step_start(job_file, "verify")
    t0 = time.time()
    _verify_outputs(out_dir, base_id, actual_audio_mode)
    _step_done(job_file, "verify", t0)

    # ── Step 7: archive raw (処理完了後にのみ移動) ──────────────────
    _step_start(job_file, "archive_raw")
    t0 = time.time()
    _archive_raw(job_file, cfg, ts_path, program_txt, err_path, meta, meta_path)
    _step_done(job_file, "archive_raw", t0, moved=cfg.move_raw)

    _update_job(job_file, {"current_step": "done", "completed_at": _now()})


def _archive_raw(
    job_file: Path, cfg: cfg_mod.Config,
    ts_path: Path, program_txt: Path | None, err_path: Path | None,
    meta: dict, meta_path: Path,
) -> tuple[Path, Path | None, Path | None]:
    """Move the raw TS (+ EDCB sidecar files) into archive_raw, flat, then apply
    count-based rolling deletion of the oldest files there.
    No-op (files stay at their original EDCB location) when cfg.move_raw is False.
    """
    if not cfg.move_raw:
        return ts_path, program_txt, err_path

    cfg.archive_raw.mkdir(parents=True, exist_ok=True)

    dest_ts = cfg.archive_raw / ts_path.name
    shutil.move(str(ts_path), str(dest_ts))

    dest_program = None
    if program_txt and program_txt.exists():
        dest_program = cfg.archive_raw / program_txt.name
        shutil.move(str(program_txt), str(dest_program))

    dest_err = None
    if err_path and err_path.exists():
        dest_err = cfg.archive_raw / err_path.name
        shutil.move(str(err_path), str(dest_err))

    meta["derived"]["raw_path"] = str(dest_ts)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    _update_job(job_file, {
        "ts_path": str(dest_ts),
        "program_txt": str(dest_program) if dest_program else None,
        "err_path": str(dest_err) if dest_err else None,
    })

    _rolling_cleanup_raw(cfg)

    return dest_ts, dest_program, dest_err


def _rolling_cleanup_raw(cfg: cfg_mod.Config) -> None:
    """Delete the oldest TS files (+ sidecars) in archive_raw beyond keep_raw_count.
    EDCB filenames are timestamp-prefixed, so filename sort order == recording order.
    """
    if cfg.keep_raw_count <= 0:
        return

    ts_files = sorted(cfg.archive_raw.glob("*.ts"))
    excess = len(ts_files) - cfg.keep_raw_count
    if excess <= 0:
        return

    for old_ts in ts_files[:excess]:
        old_ts.unlink(missing_ok=True)
        Path(str(old_ts) + ".program.txt").unlink(missing_ok=True)
        Path(str(old_ts) + ".err").unlink(missing_ok=True)


def _read_phash_frame_count(path: Path) -> int:
    try:
        import struct
        with open(path, "rb") as f:
            f.seek(8)
            return struct.unpack("<I", f.read(4))[0]
    except Exception:
        return -1


def _verify_outputs(out_dir: Path, base_id: str, audio_mode: str) -> None:
    # ファイルが存在しかつサイズ > 0 であることを必須とするもの
    required_nonempty = [
        out_dir / f"{base_id}.meta.json",
        out_dir / f"{base_id}.1.opus",
        out_dir / f"{base_id}.phash.bin",
        out_dir / f"{base_id}.thumbs.zip",
    ]
    if audio_mode == "dual_mono":
        required_nonempty.append(out_dir / f"{base_id}.2.opus")

    # 存在さえすれば空でも可（イベントがなかった場合）
    required_exist = [
        out_dir / f"{base_id}.caption.jsonl",
        out_dir / f"{base_id}.super.jsonl",
    ]

    missing = [p for p in required_nonempty if not p.exists() or p.stat().st_size == 0]
    missing += [p for p in required_exist if not p.exists()]
    if missing:
        raise RuntimeError(f"Output verification failed, missing/empty: {[p.name for p in missing]}")
