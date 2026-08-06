import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).parent.parent


@dataclass
class Config:
    watch_dir: Path
    archive_raw: Path
    archive_derived: Path
    queue_pending: Path
    queue_processing: Path
    queue_done: Path
    queue_failed: Path
    poll_interval_sec: int
    move_raw: bool
    keep_raw_count: int
    web_host: str
    web_port: int
    phash_fps: int
    thumb_interval_sec: int
    thumb_width: int
    thumb_height: int
    opus_bitrate: str

    @property
    def bin(self) -> Path:
        return ROOT / "bin"

    @property
    def ffmpeg(self) -> Path:
        return self.bin / "ffmpeg.exe"

    @property
    def ffprobe(self) -> Path:
        return self.bin / "ffprobe.exe"

    @property
    def tsreadex(self) -> Path:
        return self.bin / "tsreadex.exe"

    @property
    def caption2ass(self) -> Path:
        return self.bin / "Caption2AssC_x64.exe"


def load(path: Path | None = None) -> Config:
    path = path or ROOT / "config.toml"
    with open(path, "rb") as f:
        d = tomllib.load(f)

    def p(s: str) -> Path:
        q = Path(s)
        return q if q.is_absolute() else ROOT / q

    return Config(
        watch_dir=p(d["paths"]["watch_dir"]),
        archive_raw=p(d["paths"]["archive_raw"]),
        archive_derived=p(d["paths"]["archive_derived"]),
        queue_pending=p(d["queue"]["pending"]),
        queue_processing=p(d["queue"]["processing"]),
        queue_done=p(d["queue"]["done"]),
        queue_failed=p(d["queue"]["failed"]),
        poll_interval_sec=d["worker"]["poll_interval_sec"],
        move_raw=d["worker"]["move_raw"],
        keep_raw_count=d["worker"]["keep_raw_count"],
        web_host=d["web"]["host"],
        web_port=d["web"]["port"],
        phash_fps=d["processing"]["phash_fps"],
        thumb_interval_sec=d["processing"]["thumb_interval_sec"],
        thumb_width=d["processing"]["thumb_width"],
        thumb_height=d["processing"]["thumb_height"],
        opus_bitrate=d["processing"]["opus_bitrate"],
    )
