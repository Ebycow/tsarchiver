"""watch_dir をポーリングし、EDCBが書き終えたTSファイルを検出してキューに積む。"""
import json
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from . import config as cfg_mod

JST = timezone(timedelta(hours=9))


def _is_file_stable(ts_path: Path) -> bool:
    """EDCBは録画中ファイルを書き込みロックしたまま保持する。
    排他書き込みオープンが成功すれば、EDCBが手放した（録画完了した）とみなせる。
    """
    try:
        with open(ts_path, "r+b"):
            return True
    except OSError:
        return False


def _already_queued(ts_path: Path, cfg: cfg_mod.Config) -> bool:
    for queue_dir in (cfg.queue_pending, cfg.queue_processing, cfg.queue_done, cfg.queue_failed):
        for job_file in queue_dir.glob("*.json"):
            try:
                job = json.loads(job_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if job.get("ts_path") == str(ts_path):
                return True
    return False


def _enqueue(ts_path: Path, cfg: cfg_mod.Config) -> None:
    program_txt = Path(str(ts_path) + ".program.txt")
    err_path = Path(str(ts_path) + ".err")
    job_id = uuid.uuid4().hex[:12]
    job = {
        "job_id": job_id,
        "queued_at": datetime.now(JST).isoformat(),
        "ts_path": str(ts_path),
        "program_txt": str(program_txt) if program_txt.exists() else None,
        "err_path": str(err_path) if err_path.exists() else None,
    }
    out = cfg.queue_pending / f"{job_id}.json"
    out.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[watcher] queued: {ts_path.name}")


def run_loop(cfg: cfg_mod.Config) -> None:
    cfg.watch_dir.mkdir(parents=True, exist_ok=True)
    print(f"[watcher] watching {cfg.watch_dir}")

    seen: set[str] = set()
    last_size: dict[str, int] = {}

    while True:
        for ts_path in sorted(cfg.watch_dir.glob("*.ts")):
            key = str(ts_path)
            if key in seen:
                continue
            if _already_queued(ts_path, cfg):
                seen.add(key)
                continue

            # EDCB(EpgDataCap_BonMin.cpp: CMD2_VIEW_APP_REC_STOP_CTRL)は録画停止時、
            # SaveErrCount()で.errを書き出した"後に"EndSave()でTSファイルを閉じる。
            # つまり.errの出現は録画停止の合図だが、その瞬間はまだ.tsが閉じきって
            # いない可能性があるため、書き込みロック解放も併せて確認する。
            err_path = Path(str(ts_path) + ".err")
            if err_path.exists():
                if not _is_file_stable(ts_path):
                    continue
            else:
                # .err がまだ無い場合のフォールバック: サイズが前回ポーリング時から
                # 変わっておらず、かつ書き込みロックが解放されていれば完了とみなす
                try:
                    size = ts_path.stat().st_size
                except OSError:
                    continue
                if last_size.get(key) != size:
                    last_size[key] = size
                    continue
                if not _is_file_stable(ts_path):
                    continue

            _enqueue(ts_path, cfg)
            seen.add(key)
            last_size.pop(key, None)

        time.sleep(cfg.poll_interval_sec)
