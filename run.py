"""メインエントリポイント。ワーカースレッド + WebサーバーをひとつのプロセスでS起動する。"""
import sys
import threading
from pathlib import Path

# プロジェクトルートをパスに追加
sys.path.insert(0, str(Path(__file__).parent))

import uvicorn
from src import config as cfg_mod
from src import watcher
from src import worker
from src.web import create_app


def main() -> None:
    cfg = cfg_mod.load()

    # フォルダ監視・ワーカーをバックグラウンドスレッドで起動
    threading.Thread(target=watcher.run_loop, args=(cfg,), daemon=True).start()
    threading.Thread(target=worker.run_loop, args=(cfg,), daemon=True).start()

    # WebUI を起動（メインスレッド）
    app = create_app(cfg)
    print(f"[web] http://{cfg.web_host}:{cfg.web_port}")
    uvicorn.run(app, host=cfg.web_host, port=cfg.web_port, log_level="warning")


if __name__ == "__main__":
    main()
