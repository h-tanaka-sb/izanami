"""共通ユーティリティ: ffmpeg探索・コンソール窓抑止・ファイル名整形・JSON保存。"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

# Windowsでffmpeg/ffprobeを呼ぶ際にコンソール窓を出さない（ムービーデスク踏襲）
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_ROOT = Path(__file__).resolve().parent.parent


def _winget_ffmpeg() -> str:
    """winget版ffmpegの最終フォールバック（PATHに無い環境向け）。

    旧実装は開発機の絶対パス（ユーザー名入り）を焼き込んでいた＝配布物への
    PC固有情報の混入＋配布先では絶対に当たらないパスだった（漏洩監査2026-08-19）。
    → 実行PCのLOCALAPPDATAからglobで探す＝どのPC・どのバージョンでも当たる。"""
    try:
        base = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
        for p in sorted(base.glob("Gyan.FFmpeg*/ffmpeg-*/bin/ffmpeg.exe"), reverse=True):
            if p.exists():
                return str(p)
    except Exception:
        pass
    return "ffmpeg"   # 最後はPATH任せ（見つからなければ実行時エラーで気づける）


def _tools_exe(name: str) -> str:
    """プロジェクト同梱の実行ファイル（<ROOT>/_tools/）。配布先PCの生命線なので最優先。"""
    p = _ROOT / "_tools" / name
    return str(p) if p.exists() else ""


def find_ffmpeg() -> str:
    """ffmpeg のパスを返す。同梱_tools → PATH → imageio-ffmpeg → winget の順で探す。"""
    p = _tools_exe("ffmpeg.exe")
    if p:
        return p
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            return exe
    except Exception:
        pass
    return _winget_ffmpeg()


def find_ffprobe() -> str:
    """ffprobe のパスを返す。同梱_tools → PATH → ffmpegの隣 の順で探す。

    注意: imageio-ffmpeg は ffprobe を同梱しない。配布先PCでは _tools/ffprobe.exe が頼り
    （合成のタイムライン計算と完成検証に必須）。"""
    p = _tools_exe("ffprobe.exe")
    if p:
        return p
    p = shutil.which("ffprobe")
    if p:
        return p
    cand = Path(find_ffmpeg()).with_name("ffprobe.exe")
    return str(cand)


def run(cmd, **kwargs):
    """コンソール窓を出さずに外部コマンドを実行する。"""
    kwargs.setdefault("creationflags", _NO_WINDOW)
    return subprocess.run(cmd, **kwargs)


def popen(cmd, **kwargs):
    kwargs.setdefault("creationflags", _NO_WINDOW)
    return subprocess.Popen(cmd, **kwargs)


def prevent_sleep(on: bool) -> None:
    """量産の完走中はWindowsのスリープを抑止する（「お題を入れて寝るだけ」の生命線）。

    SetThreadExecutionState はスレッド単位。呼んだスレッドが生きている間だけ有効で、
    そのスレッドが終われば自動で解除される。量産ワーカースレッドの先頭でTrue、
    finally でFalse（＝ES_CONTINUOUSのみに戻す）を呼ぶ。画面は消えてよいので
    ES_DISPLAY_REQUIRED は付けない（システムのスリープだけ止める）。"""
    try:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        ES_SYSTEM_REQUIRED = 0x00000001
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
        ctypes.windll.kernel32.SetThreadExecutionState(flags)
    except Exception:
        pass


def run_watched(cmd, timeout_sec: float, stop=None, **kwargs) -> int:
    """ウォッチドッグ付き実行: timeout超過か stop() が真になったら kill する。

    夜間無人運用で「NVENCハング1本で朝まで全停止」を防ぐための基盤。
    返り値は returncode（kill時は -9）。
    """
    import time as _time
    proc = popen(cmd, **kwargs)
    end = _time.monotonic() + max(10, float(timeout_sec))
    while True:
        rc = proc.poll()
        if rc is not None:
            return rc
        if _time.monotonic() > end or (stop and stop()):
            try:
                proc.kill()
                proc.wait(timeout=10)
            except Exception:
                pass
            return -9
        _time.sleep(1)


def encoder_ok(ffmpeg: str, name: str) -> bool:
    """指定エンコーダが実際に使えるか短いテストエンコードで確認する。"""
    cmd = [ffmpeg, "-hide_banner", "-f", "lavfi", "-i", "nullsrc=s=256x256:d=0.1",
           "-c:v", name, "-f", "null", "-"]
    try:
        return run(cmd, capture_output=True, timeout=30).returncode == 0
    except Exception:
        return False


def pick_encoder(ffmpeg: str, settings: dict | None = None) -> str:
    """利用可能な最良のH.264エンコーダを返す（NVENC優先・libx264は必ず保証）。

    settings["force_encoder"] があればそれを優先（compose失敗時のlibx264縮退用）。
    """
    forced = (settings or {}).get("force_encoder")
    if forced:
        return forced
    for enc in ("h264_nvenc", "h264_qsv", "h264_amf", "libx264"):
        if encoder_ok(ffmpeg, enc):
            return enc
    return "libx264"


def enc_args(enc: str, quality: int = 20) -> list:
    """エンコーダ別の品質引数（ムービーデスク export.py 準拠）。"""
    if enc == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", str(quality), "-b:v", "0"]
    if enc == "h264_qsv":
        return ["-c:v", "h264_qsv", "-global_quality", str(quality)]
    if enc == "h264_amf":
        return ["-c:v", "h264_amf", "-rc", "cqp", "-qp_i", str(quality), "-qp_p", str(quality)]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", str(quality)]


def probe_ok(path) -> dict:
    """出力動画を ffprobe で検証する。"""
    path = Path(path)
    if not path.exists():
        return {"ok": False, "error": "file missing"}
    try:
        pr = run(
            [find_ffprobe(), "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,codec_name,width,height",
             "-of", "default=nw=1", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
        out = {"ok": pr.returncode == 0}
        for line in (pr.stdout or "").strip().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out.setdefault(k, v)
        return out
    except Exception as e:
        return {"ok": False, "error": str(e)}


def safe_name(name: str, default: str = "izanagi", maxlen: int = 60) -> str:
    """ファイル/フォルダ名に使えない文字を除去する。"""
    name = re.sub(r'[<>:"/\\|?*\n\r\t]', "", str(name)).strip()
    name = name[:maxlen].strip()
    return name or default


def resize_cover(path, w: int, h: int) -> None:
    """画像を w×h に中央クロップ＋リサイズ（16:9統一など）。失敗しても落とさない。"""
    try:
        from PIL import Image
        im = Image.open(path).convert("RGB")
        sw, sh = im.size
        tr, sr = w / h, sw / sh
        if sr > tr:
            nw = int(sh * tr); x = (sw - nw) // 2
            im = im.crop((x, 0, x + nw, sh))
        elif sr < tr:
            nh = int(sw / tr); y = (sh - nh) // 2
            im = im.crop((0, y, sw, y + nh))
        im = im.resize((w, h), Image.LANCZOS)
        im.save(str(path), "PNG")
    except Exception:
        pass


def save_json_atomic(path, data) -> None:
    """JSONをクラッシュ安全に保存する（tmp→fsync→os.replace＋.bak）。

    ムービーデスク project.py の atomic save 思想。書き込み途中の電源断等でも
    旧データを壊さない。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if path.exists():
            try:
                shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
            except Exception:
                pass
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass


def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def recycle_supported(path) -> bool:
    """このパスの削除が「ごみ箱に入る＝復元できる」見込みかを判定する。

    FO_DELETE+FOF_ALLOWUNDOは**ごみ箱が使える場所でだけ**ごみ箱移動になり、
    UNC/ネットワークドライブ/USBメモリ等では**無警告の完全削除**にフォールバックする
    （2026-08-04敵対レビューでUNC実験により実証）。削除確認の文言を切り替えるための判定。
    DRIVE_FIXED(3)のみ確実にごみ箱がある。判定できない時は安全側（完全削除の警告）に倒す。
    """
    try:
        p = str(Path(path).resolve())
        if p.startswith("\\\\"):
            return False  # UNC/ネットワークパスにごみ箱は無い
        drive = Path(p).drive
        if not drive:
            return False
        import ctypes
        return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 3  # DRIVE_FIXED
    except Exception:
        return False


def send_to_recycle(paths) -> str | None:
    """ファイル/フォルダを**Windowsのごみ箱へ移動**する（完全削除はしない＝復元できる）。

    投稿タブの動画削除用（2026-08-04ユーザー要望）。shutil.rmtreeだと誤操作で
    動画が即消滅するため、SHFileOperationW(FO_DELETE+FOF_ALLOWUNDO)でごみ箱経由にする。
    成功=None / 失敗=人に見せる理由文字列（例: 再生中・フォルダを開いたまま）。
    ※pFromは「パス\\0パス\\0\\0」の二重NUL終端リスト。ctypesのc_wchar_pは埋め込み
    NULごと渡すのでこの形式がそのまま使える。相対パスは失敗するので絶対化する。
    """
    items = [str(Path(p).resolve()) for p in (paths or [])]
    if not items:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                        ("pFrom", ctypes.c_wchar_p), ("pTo", ctypes.c_wchar_p),
                        ("fFlags", ctypes.c_ushort),
                        ("fAnyOperationsAborted", wintypes.BOOL),
                        ("hNameMappings", ctypes.c_void_p),
                        ("lpszProgressTitle", ctypes.c_wchar_p)]

        FO_DELETE = 3
        FOF_SILENT, FOF_NOCONFIRMATION = 0x0004, 0x0010
        FOF_ALLOWUNDO, FOF_NOERRORUI = 0x0040, 0x0400
        op = _SHFILEOPSTRUCTW(
            None, FO_DELETE, "\0".join(items) + "\0\0", None,
            FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI,
            False, None, None)
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    except Exception as e:
        return f"ごみ箱APIを呼べませんでした: {e}"
    if rc != 0:
        return (f"ごみ箱への移動に失敗しました（コード{rc}）。動画を再生中だったり、"
                "フォルダをエクスプローラーで開いたままだと失敗します。閉じてからもう一度どうぞ。")
    if op.fAnyOperationsAborted:
        return "ごみ箱への移動が途中で中止されました（一部は移動済みの可能性があります）。"
    return None
