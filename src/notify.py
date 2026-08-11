import json
import urllib.request
import urllib.error


def notify_discord_failure(webhook_url: str, ts_name: str, error: str, traceback_str: str) -> None:
    """Post a job-failure notification to a Discord webhook. Never raises."""
    if not webhook_url:
        return

    tb_snippet = traceback_str[-1500:] if traceback_str else ""
    content = (
        f"⚠️ **録画データ処理に失敗しました**\n"
        f"ファイル: `{ts_name}`\n"
        f"エラー: {error}\n"
        + (f"```\n{tb_snippet}\n```" if tb_snippet else "")
    )
    # Discord content is capped at 2000 chars
    if len(content) > 2000:
        content = content[:1997] + "..."

    payload = json.dumps({"content": content}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "tsarchiver/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except urllib.error.URLError as e:
        print(f"[notify] Discord通知に失敗しました: {e}")
