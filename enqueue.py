"""EDCBの録画後実行BATから呼び出す。引数にTSファイルパスを受け取りキューに積む。"""
import json
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).parent
JST = timezone(timedelta(hours=9))


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: enqueue.py <ts_file_path>")
        sys.exit(1)

    ts_path = Path(sys.argv[1])
    if not ts_path.exists():
        print(f"Error: file not found: {ts_path}", file=sys.stderr)
        sys.exit(1)

    # EDCB が生成する付属ファイル
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

    pending_dir = ROOT / "queue" / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)

    out = pending_dir / f"{job_id}.json"
    out.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Enqueued {job_id}: {ts_path.name}")


if __name__ == "__main__":
    main()
