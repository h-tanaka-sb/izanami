"""同梱テロップフォント（assets/fonts）のユーザーレベル自動インストール。

管理者権限なしで %LOCALAPPDATA%\\Microsoft\\Windows\\Fonts へコピーし、
HKCU の Fonts レジストリに登録する（GDI/libass の両方から見えるようになる）。
アプリ起動時に ensure_fonts_installed() を呼ぶ（済みならスキップ・高速）。
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FONTS_DIR = ROOT / "assets" / "fonts"

# テロッププリセットのフォント名 → 同梱ファイル（グリフ欠落検査用・2026-08-19）
FAMILY_TO_FILE = {
    "Keifont": "keifont.ttf",
    "GenEi POPle": "GenEiPOPle-Bk.ttf",
    "GenEi M Gothic v2": "GenEiMGothic2-Black.ttf",
    "GenEi Chikugo Mincho v3": "GenEiChikugoMin3-R.ttf",
    "TogeMaru Gothic": "TogeMaruGothic-900-Black.ttf",
    "Rounded-X M+ 1c": "rounded-x-mplus-1c-black.ttf",
    "mouhituP": "毛筆太文字.ttf",
    "GenEi Antique v6": "GenEiAntiqueNv6-M.ttf",
}


def missing_glyphs(family: str, text: str) -> str:
    """同梱フォントが表示できない文字を返す（歯抜けテロップの事前検知・2026-08-19）。

    同梱フォント以外（Yu Gothic UI等のシステムフォント）や検査不能時は空文字＝
    検査しない（フェイルオープン。fontsdir直指定が主対策なのでここは二重の保険）。"""
    fname = FAMILY_TO_FILE.get((family or "").strip())
    if not fname:
        return ""
    path = FONTS_DIR / fname
    if not path.exists():
        return ""
    try:
        from fontTools.ttLib import TTFont
        cmap = TTFont(str(path), fontNumber=0, lazy=True).getBestCmap()
    except Exception:
        return ""
    seen: set[str] = set()
    miss: list[str] = []
    for ch in text or "":
        if ch in seen or ch.isspace():
            continue
        seen.add(ch)
        if ord(ch) not in cmap:
            miss.append(ch)
    return "".join(miss)


# ファイル名 → レジストリ表示名（family + style (TrueType)）
BUNDLED = {
    "keifont.ttf": "Keifont (TrueType)",
    "GenEiPOPle-Bk.ttf": "GenEi POPle Black (TrueType)",
    "GenEiMGothic2-Black.ttf": "GenEi M Gothic v2 Black (TrueType)",
    "GenEiChikugoMin3-R.ttf": "GenEi Chikugo Mincho v3 (TrueType)",
    "TogeMaruGothic-900-Black.ttf": "TogeMaru Gothic Black (TrueType)",
    "rounded-x-mplus-1c-black.ttf": "Rounded-X M+ 1c black (TrueType)",
    "毛筆太文字.ttf": "mouhituP (TrueType)",
    "GenEiAntiqueNv6-M.ttf": "GenEi Antique v6 Medium (TrueType)",
}


def ensure_fonts_installed(log=print) -> int:
    """未導入の同梱フォントをユーザーフォントとして登録する。戻り値=新規導入数。"""
    if os.name != "nt" or not FONTS_DIR.exists():
        return 0
    try:
        import winreg
    except Exception:
        return 0
    user_fonts = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts"
    user_fonts.mkdir(parents=True, exist_ok=True)
    installed = 0
    try:
        key = winreg.CreateKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows NT\CurrentVersion\Fonts")
    except Exception as e:
        log(f"    フォント登録キーを開けません: {e}")
        return 0
    try:
        for fname, regname in BUNDLED.items():
            src = FONTS_DIR / fname
            if not src.exists():
                continue
            dst = user_fonts / fname
            try:
                if not dst.exists() or dst.stat().st_size != src.stat().st_size:
                    shutil.copy2(src, dst)
                try:
                    cur, _ = winreg.QueryValueEx(key, regname)
                except FileNotFoundError:
                    cur = None
                if cur != str(dst):
                    winreg.SetValueEx(key, regname, 0, winreg.REG_SZ, str(dst))
                    installed += 1
            except Exception as e:
                log(f"    フォント {fname} の登録に失敗: {e}")
    finally:
        winreg.CloseKey(key)
    if installed:
        # 起動中アプリへフォント追加を通知（GDI）。失敗しても再起動すれば有効
        try:
            import ctypes
            for fname in BUNDLED:
                p = str(user_fonts / fname)
                if Path(p).exists():
                    ctypes.windll.gdi32.AddFontResourceW(p)
            ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001D, 0, 0, 0, 1000, None)
        except Exception:
            pass
        log(f"    テロップフォント {installed} 種を登録しました。")
    return installed
