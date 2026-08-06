import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader

from . import config as cfg_mod

ROOT = Path(__file__).parent.parent
TEMPLATES = ROOT / "templates"


def create_app(cfg: cfg_mod.Config) -> FastAPI:
    app = FastAPI(title="tsarchiver")
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=True)

    def _load_jobs(directory: Path) -> list[dict]:
        jobs = []
        for f in sorted(directory.glob("*.json"), reverse=True)[:100]:
            try:
                jobs.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                pass
        return jobs

    def _queue_counts() -> dict:
        return {
            "pending": len(list(cfg.queue_pending.glob("*.json"))),
            "processing": len(list(cfg.queue_processing.glob("*.json"))),
            "done": len(list(cfg.queue_done.glob("*.json"))),
            "failed": len(list(cfg.queue_failed.glob("*.json"))),
        }

    @app.get("/", response_class=HTMLResponse)
    def index():
        counts = _queue_counts()
        processing = _load_jobs(cfg.queue_processing)
        current_job = processing[0] if processing else None
        tmpl = env.get_template("index.html")
        return tmpl.render(counts=counts, current_job=current_job)

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs():
        done = _load_jobs(cfg.queue_done)
        failed = _load_jobs(cfg.queue_failed)
        counts = _queue_counts()
        tmpl = env.get_template("jobs.html")
        return tmpl.render(done=done, failed=failed, counts=counts)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_detail(job_id: str):
        for directory in (cfg.queue_done, cfg.queue_failed, cfg.queue_processing, cfg.queue_pending):
            job_file = directory / f"{job_id}.json"
            if job_file.exists():
                job = json.loads(job_file.read_text(encoding="utf-8"))
                tmpl = env.get_template("job_detail.html")
                return tmpl.render(job=job)
        return HTMLResponse("Job not found", status_code=404)

    @app.get("/api/status")
    def api_status():
        counts = _queue_counts()
        processing = _load_jobs(cfg.queue_processing)
        return {"counts": counts, "current": processing[0] if processing else None}

    return app
