"""TS内のEIT[p/f]から「実際に放送されていた番組」をサンプリングで調べる。

EDCBの EpgDataCap3.dll を ctypes で直接呼ぶ（EDCB付属 Tools/tspgtxt と同じ手順）。
DLL内で異常が起きても呼び出し元（ワーカー）を巻き込まないよう、このファイルは
サブプロセスとして実行し、結果をJSONで標準出力に返す:

    python -m src.eitscan <EpgDataCap3.dll> <ts_path> <points>

TSを等間隔に <points> か所シークし、各位置で
  - EIT[p/f] の present（その時点で放送中の番組）
  - TOT（その位置の放送時刻）
を読み取る。
"""
import ctypes
import json
import math
import subprocess
import sys
from ctypes import POINTER, Structure, byref, c_ubyte, c_uint16, c_uint32, c_void_p, c_wchar_p
from datetime import datetime, timedelta
from pathlib import Path

WORD, BYTE, DWORD = c_uint16, c_ubyte, c_uint32
NO_ERR = 1  # EDCBのErrDef.h: NO_ERR=TRUE, ERR_FALSE=FALSE
TS = 188
CHUNK = 48128  # tspgtxtと同じ読み取り単位
BON_SELECTIVE_PID = 0x0030  # これ未満のPID(PAT/NIT/SDT/EIT/TOTなど)だけDLLに渡せばよい
PID_TOT = 0x0014


class SYSTEMTIME(Structure):
    _fields_ = [("wYear", WORD), ("wMonth", WORD), ("wDayOfWeek", WORD), ("wDay", WORD),
                ("wHour", WORD), ("wMinute", WORD), ("wSecond", WORD), ("wMilliseconds", WORD)]


class EPG_SHORT_EVENT_INFO(Structure):
    _fields_ = [("event_nameLength", WORD), ("event_name", c_wchar_p),
                ("text_charLength", WORD), ("text_char", c_wchar_p)]


class EPG_EVENT_INFO(Structure):
    _fields_ = [("event_id", WORD),
                ("StartTimeFlag", BYTE), ("start_time", SYSTEMTIME),
                ("DurationFlag", BYTE), ("durationSec", DWORD),
                ("shortInfo", POINTER(EPG_SHORT_EVENT_INFO)),
                ("extInfo", c_void_p), ("contentInfo", c_void_p), ("componentInfo", c_void_p),
                ("audioInfo", c_void_p), ("eventGroupInfo", c_void_p), ("eventRelayInfo", c_void_p),
                ("freeCAFlag", BYTE)]


class SERVICE_EXT_INFO(Structure):
    _fields_ = [("service_type", BYTE), ("partialReceptionFlag", BYTE),
                ("service_provider_name", c_wchar_p), ("service_name", c_wchar_p),
                ("network_name", c_wchar_p), ("ts_name", c_wchar_p),
                ("remote_control_key_id", BYTE)]


class SERVICE_INFO(Structure):
    _fields_ = [("original_network_id", WORD), ("transport_stream_id", WORD),
                ("service_id", WORD), ("extInfo", POINTER(SERVICE_EXT_INFO))]


# NITは送出頻度が低くGetServiceListActual()の成功が遅れるので、tspgtxtと同じくダミーを送る
_DUMMY_NIT = bytes([0x40, 0xF0, 13, 0x00, 0x00, 0xC1, 0x00, 0x00, 0xF0, 0, 0xF0, 0,
                    0x60, 0x24, 0x58, 0xC8])


class EpgDataCap3:
    def __init__(self, dll_path: str):
        self.dll = ctypes.WinDLL(dll_path)
        d = self.dll
        d.InitializeEP.argtypes = [ctypes.c_int, POINTER(DWORD)]
        d.UnInitializeEP.argtypes = [DWORD]
        d.AddTSPacketEP.argtypes = [DWORD, ctypes.c_char_p, DWORD]
        d.GetServiceListActualEP.argtypes = [DWORD, POINTER(DWORD), POINTER(POINTER(SERVICE_INFO))]
        d.GetEpgInfoEP.argtypes = [DWORD, WORD, WORD, WORD, ctypes.c_int, POINTER(POINTER(EPG_EVENT_INFO))]
        for f in ("InitializeEP", "UnInitializeEP", "AddTSPacketEP", "GetServiceListActualEP", "GetEpgInfoEP"):
            getattr(d, f).restype = DWORD

    def new_session(self) -> "Session":
        return Session(self.dll)


class Session:
    """1サンプル分のDLLインスタンス。シーク位置ごとに作り直し、前の位置のEITを持ち越さない。"""

    def __init__(self, dll):
        self.dll = dll
        self.id = DWORD()
        if dll.InitializeEP(0, byref(self.id)) != NO_ERR:
            raise RuntimeError("InitializeEP failed")
        self.nit_pid = 0
        self.nit_counter = 0

    def close(self):
        self.dll.UnInitializeEP(self.id)

    def add(self, packet: bytes) -> None:
        self.dll.AddTSPacketEP(self.id, packet, TS)

    def add_dummy_nit(self) -> None:
        if not self.nit_pid:
            return
        self.nit_counter = (self.nit_counter + 1) & 0x0F
        pkt = bytes([0x47, (self.nit_pid >> 8) | 0x40, self.nit_pid & 0xFF, 0x10 | self.nit_counter, 0])
        pkt += _DUMMY_NIT
        self.add(pkt + b"\xFF" * (TS - len(pkt)))

    def service(self, sid: int) -> SERVICE_INFO | None:
        size = DWORD()
        lst = POINTER(SERVICE_INFO)()
        if self.dll.GetServiceListActualEP(self.id, byref(size), byref(lst)) != NO_ERR:
            return None
        for i in range(size.value):
            if lst[i].service_id == sid:
                return lst[i]
        return None

    def present(self, svc: SERVICE_INFO) -> dict | None:
        info = POINTER(EPG_EVENT_INFO)()
        if self.dll.GetEpgInfoEP(self.id, svc.original_network_id, svc.transport_stream_id,
                                 svc.service_id, 0, byref(info)) != NO_ERR:
            return None
        e = info.contents
        st = e.start_time
        return {
            "onid": svc.original_network_id,
            "tsid": svc.transport_stream_id,
            "sid": svc.service_id,
            "eid": e.event_id,
            "title": e.shortInfo.contents.event_name if e.shortInfo else None,
            "start": datetime(st.wYear, st.wMonth, st.wDay, st.wHour, st.wMinute, st.wSecond).isoformat()
                     if e.StartTimeFlag else None,
            "duration_sec": e.durationSec if e.DurationFlag else None,
        }


def _pid(p: bytes) -> int:
    return ((p[1] & 0x1F) << 8) | p[2]


def _parse_pat(p: bytes) -> tuple[int, list[int]] | None:
    """(NITのPID, program_numberの並び) — 1パケットに収まるPATのみ対応（サービス録画なら十分）。"""
    if not (p[1] & 0x40):
        return None
    off = 5 + p[4] if p[3] & 0x20 else 4  # adaptation fieldを飛ばす
    off += 1 + p[off]  # pointer_field
    if off + 8 > TS or p[off] != 0x00:
        return None
    sec_len = ((p[off + 1] & 0x0F) << 8) | p[off + 2]
    end = min(off + 3 + sec_len - 4, TS)
    nit, progs = 0, []
    for i in range(off + 8, end - 3, 4):
        num = (p[i] << 8) | p[i + 1]
        pid = ((p[i + 2] & 0x1F) << 8) | p[i + 3]
        if num == 0:
            nit = pid
        else:
            progs.append(num)
    return nit, progs


def _bcd(b: int) -> int:
    return (b >> 4) * 10 + (b & 0x0F)


def _parse_tot(p: bytes) -> datetime | None:
    """TDT/TOT(table_id 0x70/0x73)のJST時刻。"""
    if not (p[1] & 0x40):
        return None
    off = 5 + p[4] if p[3] & 0x20 else 4
    off += 1 + p[off]
    if off + 8 > TS or p[off] not in (0x70, 0x73):
        return None
    mjd = (p[off + 3] << 8) | p[off + 4]
    if mjd == 0xFFFF:
        return None
    try:
        return (datetime(1858, 11, 17) + timedelta(days=mjd)).replace(
            hour=_bcd(p[off + 5]), minute=_bcd(p[off + 6]), second=_bcd(p[off + 7]))
    except ValueError:
        return None


def _sync(buf: bytes) -> int:
    for o in range(TS):
        if all(o + k * TS < len(buf) and buf[o + k * TS] == 0x47 for k in range(4)):
            return o
    return -1


def sample_at(edc: EpgDataCap3, f, offset: int, limit_bytes: int) -> dict:
    """offsetから読み、presentとTOTの両方が取れるまで（最大limit_bytes）DLLに流し込む。"""
    f.seek(offset)
    sess = edc.new_session()
    try:
        sid, svc, ev, tot = None, None, None, None
        read = 0
        rest = b""
        aligned = False
        while read < limit_bytes:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            read += len(chunk)
            buf = rest + chunk
            pos = 0
            if not aligned:
                pos = _sync(buf)
                if pos < 0:
                    rest = buf[-3 * TS:]
                    continue
                aligned = True
            n = (len(buf) - pos) // TS
            for k in range(n):
                p = buf[pos + k * TS: pos + (k + 1) * TS]
                if p[0] != 0x47:
                    continue
                pid = _pid(p)
                if pid >= BON_SELECTIVE_PID:
                    continue
                if pid == 0:
                    pat = _parse_pat(p)
                    if pat:
                        sess.nit_pid = sess.nit_pid or pat[0]
                        if sid is None and pat[1]:
                            sid = pat[1][0]
                if pid == PID_TOT and tot is None:
                    tot = _parse_tot(p)
                if sess.nit_pid and pid != sess.nit_pid:
                    sess.add(p)
            rest = buf[pos + n * TS:]
            sess.add_dummy_nit()
            if svc is None and sid is not None:
                svc = sess.service(sid)
            if svc is not None and ev is None:
                ev = sess.present(svc)
            if ev is not None and tot is not None:
                break
        return {"offset": offset, "tot": tot.isoformat() if tot else None, "event": ev, "read_bytes": read}
    finally:
        sess.close()


def scan(dll_path: str, ts_path: str, points: int, limit_bytes: int = 16 * 1024 * 1024) -> list[dict]:
    edc = EpgDataCap3(dll_path)
    samples = []
    with open(ts_path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        for k in range(points):
            # 区間の中央を読む（先頭/末尾ぴったりは前後番組との境目になりやすい）
            offset = int(size * (k + 0.5) / points) // TS * TS
            samples.append(sample_at(edc, f, offset, limit_bytes))
    return samples


# ── ワーカー側から使う部分（DLLはロードしない） ─────────────────────────────

SAMPLE_INTERVAL_SEC = 30
MIN_POINTS, MAX_POINTS = 6, 240


def points_for(duration_sec: float) -> int:
    if not duration_sec or duration_sec <= 0:
        return 60
    return max(MIN_POINTS, min(MAX_POINTS, math.ceil(duration_sec / SAMPLE_INTERVAL_SEC)))


def summarize(samples: list[dict], reserved: tuple[int, int] | None) -> dict:
    """サンプルを番組(sid, eid)ごとに集計する。shareは番組情報が取れたサンプルに占める割合。

    reserved: program.txtから分かる予約番組の(sid, eid)。その番組が録画の何割を占めたかを
    reserved_shareとして返す（program.txtが無ければNone）。
    """
    valid = [s for s in samples if s.get("event")]
    groups: dict[tuple[int, int], dict] = {}
    for s in valid:
        e = s["event"]
        g = groups.setdefault((e["sid"], e["eid"]), {**e, "count": 0, "first_seen": None, "last_seen": None})
        g["count"] += 1
        if s.get("tot"):
            g["first_seen"] = min(filter(None, [g["first_seen"], s["tot"]]))
            g["last_seen"] = max(filter(None, [g["last_seen"], s["tot"]]))
    events = []
    for g in sorted(groups.values(), key=lambda g: -g["count"]):
        g["share"] = round(g.pop("count") / len(valid), 3)
        events.append(g)
    reserved_share = None
    if reserved is not None and valid:
        reserved_share = next((e["share"] for e in events if (e["sid"], e["eid"]) == reserved), 0.0)
    return {
        "samples": len(samples),
        "valid": len(valid),
        "events": events,
        "reserved_share": reserved_share,
    }


def scan_aired(
    dll_path: Path | None, ts_path: Path, duration_sec: float,
    reserved: tuple[int, int] | None, timeout_sec: int = 600,
) -> dict | None:
    """TSで実際に放送されていた番組を調べてmeta用のdictを返す。dll_pathがNoneなら無効(None)。

    失敗しても録画処理全体は止めない（{"error": ...} を返す）。
    """
    if dll_path is None:
        return None
    if not dll_path.exists():
        return {"error": f"EpgDataCap3.dll not found: {dll_path}"}
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "src.eitscan", str(dll_path), str(ts_path), str(points_for(duration_sec))],
            cwd=str(Path(__file__).parent.parent),
            capture_output=True, timeout=timeout_sec,
        )
        if proc.returncode != 0:
            return {"error": f"eitscan rc={proc.returncode}: {proc.stderr.decode('utf-8', 'replace')[-500:]}"}
        samples = json.loads(proc.stdout.decode("utf-8"))
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as e:
        return {"error": f"eitscan failed: {e!r}"}
    return summarize(samples, reserved)


if __name__ == "__main__":
    dll, ts, pts = sys.argv[1], sys.argv[2], int(sys.argv[3])
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(scan(dll, ts, pts), ensure_ascii=False))
