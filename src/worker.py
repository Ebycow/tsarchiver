import json
import shutil
import time
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

from . import config as cfg_mod
from . import notify
from . import pipeline

JST = timezone(timedelta(hours=9))


def _now() -> str:
    return datetime.now(JST).isoformat()


def _move_with_retry(src: Path, dst: Path, retries: int = 10, delay_sec: float = 0.5) -> None:
    """shutil.move のリトライ版。
    Windowsではウイルス対策ソフトやインデクサーが作成/移動直後のファイルを
    一瞬だけロックすることがあり、PermissionError (WinError 32) が一過性に発生する。
    """
    for attempt in range(retries):
        try:
            shutil.move(str(src), str(dst))
            return
        except PermissionError:
            if attempt == retries - 1:
                raise
            time.sleep(delay_sec)


def _recover_processing(cfg: cfg_mod.Config) -> None:
    """Move any leftover processing jobs back to pending on startup."""
    for job_file in cfg.queue_processing.glob("*.json"):
        dest = cfg.queue_pending / job_file.name
        _move_with_retry(job_file, dest)
        print(f"[worker] recovered: {job_file.name} → pending")


def run_loop(cfg: cfg_mod.Config) -> None:
    cfg.queue_pending.mkdir(parents=True, exist_ok=True)
    cfg.queue_processing.mkdir(parents=True, exist_ok=True)
    cfg.queue_done.mkdir(parents=True, exist_ok=True)
    cfg.queue_failed.mkdir(parents=True, exist_ok=True)
    cfg.archive_raw.mkdir(parents=True, exist_ok=True)
    cfg.archive_derived.mkdir(parents=True, exist_ok=True)

    _recover_processing(cfg)
    print(f"[worker] started, polling every {cfg.poll_interval_sec}s")

    while True:
        jobs = sorted(cfg.queue_pending.glob("*.json"))
        if jobs:
            try:
                _process_one(jobs[0], cfg)
            except Exception:
                # 想定外の例外でスレッド自体を落とさない（例: リトライしても解消しない
                # ファイルロック）。ログだけ出してポーリングを継続する。
                print(f"[worker] unexpected error, will retry:\n{traceback.format_exc()}")
                time.sleep(cfg.poll_interval_sec)
        else:
            time.sleep(cfg.poll_interval_sec)


def _process_one(job_file: Path, cfg: cfg_mod.Config) -> None:
    # Move to processing
    proc_file = cfg.queue_processing / job_file.name
    _move_with_retry(job_file, proc_file)

    job = json.loads(proc_file.read_text(encoding="utf-8"))
    ts_name = Path(job["ts_path"]).name
    print(f"[worker] start: {ts_name}")

    # Initialize step tracking
    data = json.loads(proc_file.read_text(encoding="utf-8"))
    data["started_at"] = _now()
    data["current_step"] = "meta"
    data["steps"] = {s: {"status": "pending"} for s in
                     ["meta", "caption", "audio", "phash", "thumbs", "verify", "archive_raw"]}
    proc_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    try:
        pipeline.process_job(proc_file, cfg)
        dest = cfg.queue_done / proc_file.name
        _move_with_retry(proc_file, dest)
        print(f"[worker] done: {ts_name}")
    except Exception as e:
        tb = traceback.format_exc()
        print(f"[worker] FAILED: {ts_name}\n{tb}")
        # Write failure info
        data = json.loads(proc_file.read_text(encoding="utf-8"))
        data["failed_at"] = _now()
        data["error"] = str(e)
        data["traceback"] = tb
        proc_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        dest = cfg.queue_failed / proc_file.name
        _move_with_retry(proc_file, dest)
        notify.notify_discord_failure(cfg.discord_webhook_url, ts_name, str(e), tb)
