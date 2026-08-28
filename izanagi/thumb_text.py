"""サムネのキャッチコピー焼き込み（PIL・決定論レイアウト）。

背景はAI・文字はPIL＝読み上げ系チャンネルの勝ち筋。
定石: 極太フォント・二重縁取り（黒縁＋外側白縁）・下部グラデ帯・アクセント1色・
左上〜上部配置（右下は再生時間バッジで潰れるため禁止）。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 極太優先の同梱フォント候補（assets/fonts）
_FONT_CANDIDATES = [
    "GenEiPOPle-Bk.ttf", "GenEiPOPle_bk.ttf", "rounded-x-mplus-1c-black.ttf",
    "GenEiChikugoMin3-B.ttf", "keifont.ttf",
]

# サムネ文字のフォント選択肢（表示名 → 同梱ファイル名）。""＝おまかせ（自動）
THUMB_FONTS = {
    "おまかせ（自動）": "",
    "ポップ体（極太）": "GenEiPOPle-Bk.ttf",
    "丸ゴシック（極太）": "rounded-x-mplus-1c-black.ttf",
    "ゴシック（極太）": "GenEiMGothic2-Black.ttf",
    "とげ丸ゴシック（極太）": "TogeMaruGothic-900-Black.ttf",
    "明朝体": "GenEiChikugoMin3-R.ttf",
    "アンティーク明朝": "GenEiAntiqueNv6-M.ttf",
    "毛筆（太）": "毛筆太文字.ttf",
    "手書き風": "keifont.ttf",
}


def _find_font() -> str | None:
    fdir = ROOT / "assets" / "fonts"
    low = {p.name.lower(): p for p in fdir.glob("*.tt[fc]")} if fdir.exists() else {}
    for name in _FONT_CANDIDATES:
        p = low.get(name.lower())
        if p:
            return str(p)
    return str(next(iter(low.values()))) if low else None


def _resolve_font(cfg) -> str | None:
    """cfg['thumb_font']（表示名でもファイル名でも可）を assets/fonts のパスへ。無指定は自動。"""
    fdir = ROOT / "assets" / "fonts"
    want = ((cfg or {}).get("thumb_font") or "").strip()
    if want:
        fname = THUMB_FONTS.get(want, want)  # 表示名なら実ファイル名へ
        if fname:
            p = fdir / fname
            if p.exists():
                return str(p)
    return _find_font()


# サムネ文字の色プリセット（投稿タブの「文字の色」欄。空＝既定）
THUMB_COLOR_PRESETS = {
    "既定": "", "白": "#FFFFFF", "黄": "#FFE600", "赤": "#FF3B3B", "オレンジ": "#FF9F1C",
    "水色": "#5CE1FF", "黄緑": "#7CFF4F", "ピンク": "#FF7AD9", "黒": "#111111",
}
_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
# 自動改行で「ここで切ると読みやすい」区切り（この文字の直後で割る）
_BREAK_CHARS = "。！？!?…、,.　 ・→"
# 行頭に来てはいけない文字（行頭禁則）
_NO_HEAD = "。、！？!?…」』）)］]｝}・"
MAX_LINE = 16          # 1行の上限（フォント縮小の都合）
MAX_AUTO = MAX_LINE * 2  # 「/」無しで自動で割る時の合計上限


def _is_dark(hexcol: str) -> bool:
    try:
        r, g, b = int(hexcol[1:3], 16), int(hexcol[3:5], 16), int(hexcol[5:7], 16)
        return (0.299 * r + 0.587 * g + 0.114 * b) < 110
    except Exception:
        return False


def resolve_color(val, default: str) -> str:
    """プリセット名 or #RRGGBB → #RRGGBB（不正・空は default）。"""
    v = (val or "").strip()
    if not v:
        return default
    if v in THUMB_COLOR_PRESETS:
        return THUMB_COLOR_PRESETS[v] or default
    return v.upper() if _HEX_RE.match(v) else default


def _auto_break(t: str) -> int:
    """「/」が無い長文の改行位置。両行とも16字以内に収まる範囲で、中央に近い句読点・記号の直後を
    優先（数字の小数点/桁区切りでは割らない・行頭禁則）。無ければその範囲の中央。"""
    n = len(t)
    lo, hi = max(1, n - MAX_LINE), min(n - 1, MAX_LINE)
    if lo > hi:   # 32字超（呼び出し側で丸めるので通常来ない）
        lo = hi = min(n - 1, MAX_LINE)
    mid = n // 2
    best, best_d = -1, 10 ** 9
    for i in range(lo, hi + 1):
        c = t[i - 1]
        if c not in _BREAK_CHARS:
            continue
        if c in ".," and (t[i - 2: i - 1].isdigit() or t[i: i + 1].isdigit()):
            continue   # 3.5 / 1,000 の途中で割らない
        if t[i: i + 1] and t[i] in _NO_HEAD:
            continue   # 「！？」「……」の2文字目が行頭に来ない
        d = abs(i - mid)
        if d < best_d:
            best, best_d = i, d
    return best if best > 0 else max(lo, min(hi, mid))


def _split_lines(text: str) -> list[str]:
    """「8〜13字×最大2行を/区切り」のTHUMB形式 → 行リスト（無ければ自動分割）。

    「/」（または「／」）の位置で上段/下段に分かれる＝改行位置はユーザーが「/」で指定できる。
    無い長文は句読点の直後を優先して割る（語の途中で割れにくく・2026-08-20配布先要望）。"""
    t = (text or "").strip().replace("／", "/")
    if not t:
        return []
    if "/" in t:
        parts = [s.strip() for s in t.split("/") if s.strip()]
        # 3区切り以上は2つ目以降をまとめて下段へ（3段目を無言で捨てない）
        lines = parts[:1] + (["".join(parts[1:])] if len(parts) > 1 else [])
    elif len(t) > 13:
        t = t[:MAX_AUTO]   # 自動で割る時は合計32字（各行16字）に丸める＝行の途中が欠けない
        m = _auto_break(t)
        lines = [t[:m].strip(), t[m:].strip()]
        lines = [ln for ln in lines if ln]
    else:
        lines = [t]
    return [ln[:MAX_LINE] for ln in lines]


def draw_thumb_text(png_path, text: str, cfg: dict, log=print) -> bool:
    """thumbnail.png にキャッチコピーを焼き込む（失敗しても元画像は残す）。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
        lines = _split_lines(text)
        if not lines:
            return False
        font_path = _resolve_font(cfg)
        if not font_path:
            log("    サムネ文字: 同梱フォントが見つからないため省略")
            return False
        im = Image.open(png_path).convert("RGBA")
        W, H = im.size

        # 下部グラデ帯（黒→透明）: 下段の文字と再生バッジ周りを引き締める
        grad_h = int(H * 0.36)
        grad = Image.new("L", (1, grad_h))
        for y in range(grad_h):
            grad.putpixel((0, y), int(140 * (y / grad_h)))
        band = Image.new("RGBA", (W, grad_h), (0, 0, 0, 255))
        band.putalpha(grad.resize((W, grad_h)))
        im.alpha_composite(band, (0, H - grad_h))

        draw = ImageDraw.Draw(im)
        max_w = int(W * 0.94)
        size = int(H * 0.24)  # 170px級から収まるまで縮小
        while size > 40:
            font = ImageFont.truetype(font_path, size)
            widest = max(draw.textlength(ln, font=font) for ln in lines)
            if widest <= max_w:
                break
            size -= 8
        font = ImageFont.truetype(font_path, size)
        stroke = max(4, int(size * 0.10))
        accent = cfg.get("thumb_accent_color", "#FFE600") or "#FFE600"
        # 上段/下段の色（投稿タブの「文字の色」欄。空＝従来どおり 上段=白／下段=アクセント色）
        top_col = resolve_color(cfg.get("thumb_color_top"), "#FFFFFF")
        bottom_col = resolve_color(cfg.get("thumb_color_bottom"), accent)
        x = int(W * 0.03)
        y = int(H * 0.04)
        line_h = int(size * 1.16)
        for i, ln in enumerate(lines):
            fill = top_col if i == 0 else bottom_col
            # 外側白縁（2行目のアクセント色を背景から浮かせる）→ 黒縁 → 本体
            # 本体が暗色（黒など）の時は縁を反転（内側白・外側黒）＝本体と縁が一体化して潰れない
            inner_s, outer_s = ("#FFFFFF", "#000000") if _is_dark(fill) else ("#000000", "#FFFFFF")
            draw.text((x + 4, y + 6), ln, font=font, fill=outer_s,
                      stroke_width=stroke, stroke_fill=outer_s)  # ドロップシャドウ
            draw.text((x, y), ln, font=font, fill=fill,
                      stroke_width=stroke + 3, stroke_fill=outer_s)
            draw.text((x, y), ln, font=font, fill=fill,
                      stroke_width=stroke, stroke_fill=inner_s)
            y += line_h
        im.convert("RGB").save(str(png_path), "PNG")
        log(f"    サムネ文字を焼き込みました: {'／'.join(lines)}")
        return True
    except Exception as e:
        log(f"    サムネ文字の焼き込みは省略（{str(e)[:80]}）")
        return False
