"""テロップ: 台本→キュー行への分割と、(行, start, end) → ASS 生成。

Phase1 の同期方式は align_method="segments":
  TTSを「キュー行ごと」に合成し、各行の音声長＝その行の表示時間とする。
  → SSMLの<mark>連結の落とし穴を避けつつ、行と音声が必ず一致する。
ASSヘッダ/時刻整形/折返しは TikTokクリエイター shortmaker/render.py 準拠。
"""
from __future__ import annotations

import re

# 文末記号（ここで切る）
_SENT_END = "。．！？!?…"

# 「読み上げられる文字」を含むか（かな/カナ/漢字/英数）。記号だけのキュー検出用
_SPEECH_RE = re.compile(r"[ぁ-んァ-ヶ一-龥々a-zA-Z0-9Ａ-Ｚａ-ｚ０-９]")

# AIチャット画面のUIボタン文字が回答テキストへ紛れ込むことがある
# （ChatGPT Webの「編集」ボタンが台本1行目に混入→冒頭に無関係なテロップと
# 読み上げが出る＝2026-08-06報告・chatgpt_web製の実動画3本すべてで確認）。
# 句読点の無い単独行がこれらの語そのものの時だけUI由来とみなして捨てる
# （「編集。」「編集した映像」のような本文は残る）
UI_JUNK_LINES = frozenset({"編集", "コピー", "共有", "再試行", "再生成", "もっと見る",
                           "Copy", "Edit", "Share", "Regenerate", "Retry"})

# 行頭の「短い語＋コロン」（私：/部長：/別部署社員： 等）。話者ラベル検出用
_LABEL_RE = re.compile(r"^([一-龥々ぁ-んァ-ヶーA-Za-z0-9Ａ-Ｚａ-ｚ０-９]{1,7})[:：]\s*")
# ラベルに見えるが本文の一部である定番（誤って削らない）
_CONTENT_LABELS = {"注意", "注", "例", "補足", "結論", "理由", "警告", "重要", "追記",
                   "参考", "ポイント", "まとめ", "ヒント", "問題", "答え", "正解",
                   "質問", "回答", "http", "https"}


def _detect_speaker_labels(lines: list[str]) -> set[str]:
    """台本全体で2回以上繰り返される行頭ラベル＝話者ラベルとみなす。

    narration形式でもAIが「私：」「部長：」の会話劇スタイルで書くことがあり、
    ラベルが字幕に表示され音声でも読み上げられていた（2026-08-04配布先報告・259行）。
    1回だけの「注意：」等の内容ラベルや時刻表記「12:30」は対象外。"""
    from collections import Counter
    cnt: Counter = Counter()
    for ln in lines:
        m = _LABEL_RE.match(ln)
        if m:
            cnt[m.group(1)] += 1
    return {k for k, n in cnt.items()
            if n >= 2 and k not in _CONTENT_LABELS and not k.isdigit()}


def split_into_cues(script: str, max_chars: int = 24, min_chars: int = 8) -> list[str]:
    """台本をテロップ1行（＝TTS1セグメント）に分割する。

    分割規則（2026-07-07 自然な読み上げ対応）:
    1) テロップの切れ目は必ず「、」「。」等の直後（単語・フレーズの途中でぶった切らない）
    2) 区切っても min_chars に満たない短い断片は、次に続けて1枚にまとめる
    3) 長さ上限はソフト: 次の句読点まで max_chars を超えても1枚に入れてしまう
       （読みが途切れるより、テロップが画面内で2行に折り返す方が自然。ユーザー指定）。
       句読点なしで max_chars×2 を超える異常長だけ従来の強制分割
    ナレーション台本の前置き記号（#見出し・箇条書きの-等）は軽く除去。
    """
    if not script:
        return []
    text = script.replace("\r\n", "\n").replace("\r", "\n")
    hard_cap = max(max_chars * 2, max_chars + 8)
    cleaned = [_clean_line(l) for l in text.split("\n")]
    labels = _detect_speaker_labels([l for l in cleaned if l])
    cues: list[str] = []
    for line in cleaned:
        if not line:
            continue
        if labels:   # 話者ラベルは字幕にもTTSにも載せない（cuesは両方の共通ソース）
            m = _LABEL_RE.match(line)
            if m and m.group(1) in labels:
                line = line[m.end():].strip()
                if not line:
                    continue
        # 句読点の直後で節に割る（記号は残す＝テロップは必ず、や。で終わる）
        parts = [p for p in re.split(rf"(?<=[{_SENT_END}、，])", line) if p.strip()]
        cur = ""
        for p in parts:
            cur += p
            if len(cur.strip()) >= max(1, min_chars):
                s = cur.strip()
                if len(s) <= hard_cap:
                    cues.append(s)          # 句読点で終わる塊はそのまま1枚
                else:
                    cues.extend(_chunk(s, max_chars))  # 異常長のみ強制分割
                cur = ""
        rest = cur.strip()
        if rest:  # 行末の短い残り: 前のテロップに続ける（無ければそのまま1枚）
            if cues and len(rest) < min_chars and len(cues[-1]) + len(rest) <= hard_cap:
                cues[-1] += rest
            else:
                cues.append(rest)
    # 記号・句読点だけのキューはTTSが発話できない（Edgeで3回リトライ失敗＋無音0.6秒）ので
    # 前のキューへ吸収する（先頭なら捨てる）
    out: list[str] = []
    for c in cues:
        if not c:
            continue
        if _SPEECH_RE.search(c):
            out.append(c)
        elif out:
            out[-1] += c
    return out


def _clean_line(line: str) -> str:
    line = line.strip()
    if not line:
        return ""
    # Markdownの区切り線（--- 等の記号だけの行）は捨てる（「--」がテロップ/読み上げに混入する対策）
    if re.fullmatch(r"[-*_＝=・~～\s]{3,}", line):
        return ""
    # Markdown見出し/箇条書き/番号の先頭記号を除去
    line = re.sub(r"^#{1,6}\s*", "", line)
    line = re.sub(r"^[-*・]\s*", "", line)
    # 箇条書き番号「1.」「2)」は除去。ただし直後が数字なら本文の小数（3.5倍/42.195km/3.11）なので残す
    line = re.sub(r"^\d+[.)、]\s*(?!\d)", "", line)
    # ナレーション台本でありがちな「ナレーション：」等のラベルを除去
    line = re.sub(r"^[（(【\[][^）)】\]]{0,12}[）)】\]]\s*", "", line)
    line = re.sub(r"^(ナレ(ーション)?|NA|N)\s*[:：]\s*", "", line, flags=re.IGNORECASE)
    line = line.strip()
    if line in UI_JUNK_LINES:   # チャットUIのボタン文字（編集/コピー等）の単独行
        return ""
    return line


def _chunk(sent: str, max_chars: int) -> list[str]:
    if max_chars <= 0 or len(sent) <= max_chars:
        return [sent]
    out: list[str] = []
    rest = sent
    while len(rest) > max_chars:
        window = rest[:max_chars]
        # 読点・空白で気持ちよく切れる位置を探す（後ろ寄り優先）
        cut = max(window.rfind("、"), window.rfind("，"),
                  window.rfind(" "), window.rfind("　"))
        if cut < max_chars // 2:
            cut = max_chars - 1
        out.append(rest[:cut + 1].strip())
        rest = rest[cut + 1:].strip()
    if rest:
        out.append(rest)
    return [c for c in out if c]


# ── ASS生成 ────────────────────────────────────────────────────────────
def _fmt_time(t: float) -> str:
    """ASS の H:MM:SS.cs 形式（centiseconds）。"""
    if t < 0:
        t = 0.0
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_escape(text: str) -> str:
    # ASSテキスト内のエスケープ。改行は \N。
    return (text.replace("\\", "\\\\").replace("{", "(").replace("}", ")")
            .replace("\n", r"\N"))


def _split_cues_for_shorts(cues: list[dict], limit: int) -> list[dict]:
    """ショート用: 1画面1フレーズの短いテロップへ再分割する（2026-08-19要望）。

    台本側にも「1行=1テロップ・13字以内」で書かせるが、長い行が来ても必ず短く割る。
    音声はそのまま＝各フレーズの表示時間は元cueの時間を文字数比例で按分する。
    1枚は最大2行（limit×2字）まで。"""
    out: list[dict] = []
    cap = max(6, limit * 2)
    for c in cues:
        text = (c.get("text") or "").strip()
        st, en = float(c.get("start", 0.0)), float(c.get("end", 0.0))
        if not text or en <= st or len(text) <= cap:
            out.append(c)
            continue
        parts = [p.strip() for p in re.split(r"(?<=[、。．！？!?…])", text) if p.strip()]
        frags: list[str] = []
        for p in parts:
            if len(p) <= cap:
                frags.append(p)
            else:   # 句読点が無い長文は均等割り
                n = -(-len(p) // cap)
                step = -(-len(p) // n)
                frags.extend(p[i:i + step] for i in range(0, len(p), step))
        # 記号だけの断片（「！？」「……」の2文字目以降が単独になったもの）は前へ吸収する
        # （split_into_cuesの_SPEECH_RE吸収と同じ。無いと91pxの「？」だけが
        #  約0.2秒フラッシュ表示される・敵対検証2026-08-19）
        merged: list[str] = []
        for f in frags:
            if merged and not _SPEECH_RE.search(f):
                merged[-1] += f
            else:
                merged.append(f)
        if len(merged) > 1 and not _SPEECH_RE.search(merged[0]):
            merged[1] = merged[0] + merged[1]
            merged = merged[1:]
        frags = merged or frags
        total = sum(len(f) for f in frags) or 1
        pos = st
        for i, f in enumerate(frags):
            e2 = en if i == len(frags) - 1 else round(pos + (en - st) * len(f) / total, 3)
            out.append({**c, "text": f, "start": round(pos, 3), "end": e2})
            pos = e2
    return out


def build_ass(cues: list[dict], settings: dict, log=print) -> str:
    """cues=[{text,start,end}] から ASS 文字列を作る。

    settings から font/サイズ/色/縁/位置/余白を読む。日本語フォントを必ず指定（豆腐回避）。
    """
    W = int(settings.get("video_width", 1920))
    H = int(settings.get("video_height", 1080))
    font = settings.get("telop_font", "Yu Gothic UI")
    # グリフ欠落の事前検査（2026-08-19配布先報告: 和風毛筆で漢字が歯抜け）。
    # 同梱フォントに無い字が台本に含まれる場合、歯抜けで焼くより読めるフォントの方が
    # ましなので、収録の広いKeifont（けいふぉんと）へ動画全体でフォールバックする
    try:
        from . import fonts as _fonts
        _miss = _fonts.missing_glyphs(font, "".join((c.get("text") or "") for c in cues))
        if _miss and font != "Keifont":
            log(f"    ⚠ テロップフォント「{font}」はこの台本の{len(_miss)}字"
                f"（例: {_miss[:8]}）を表示できません → 「けいふぉんと」で焼きます"
                "（歯抜け防止）。")
            font = "Keifont"
    except Exception:
        pass
    fs = int(settings.get("telop_fontsize", 72))
    primary = settings.get("telop_primary_color", "&H00FFFFFF")
    outline_c = settings.get("telop_outline_color", "&H00000000")
    outline = settings.get("telop_outline", 4)
    shadow = settings.get("telop_shadow", 2)
    align = int(settings.get("telop_alignment", 2))
    mv = int(settings.get("telop_margin_v", 90))
    max_chars = int(settings.get("telop_max_chars", 24))
    border_style = int(settings.get("telop_border_style", 1))  # 1=縁取り / 3=帯
    back = settings.get("telop_back_color", "&H64000000")
    margin_lr = 80
    # 縦動画（ショート）: ショート定番の「大きく短い」テロップにする（2026-08-19要望。
    # 旧: 幅比例縮小≒43pxは、はみ出しはしないが小さくてショートらしくなかった）。
    # フォントは幅の8.5%（1080幅で約91px）まで引き上げ、収まる文字数（≒10字/行）で
    # 折り返す＝物理的にはみ出さない保証はそのまま。表示は _split_cues_for_shorts が
    # 1画面1フレーズへ再分割する（音声は変えず時間を文字数で按分）。
    # ※横は従来どおり（大きめプリセット78〜84の折返し位置も変えない＝横の回帰なし）
    wrap_chars = max_chars
    shorts = H > W
    if shorts:
        fs = max(int(W * 0.085), int(fs * W / 1920))
        usable = W - 2 * margin_lr
        wrap_chars = max(5, usable // max(fs, 1))
        cues = _split_cues_for_shorts(cues, wrap_chars)

    def _style(name: str, color: str) -> str:
        return (f"Style: {name},{font},{fs},{color},{color},{outline_c},{back},"
                f"1,0,0,0,100,100,0,0,{border_style},{outline},{shadow},{align},"
                f"{margin_lr},{margin_lr},{mv},1")

    # 会話形式: 話者A/B/ナレで色分け（cueに speaker が付いている場合のみ使われる）
    styles = [_style("Caption", primary),
              _style("CapA", settings.get("dialog_color_a", "&H00FFFFFF")),
              _style("CapB", settings.get("dialog_color_b", "&H00FFD37E")),
              _style("CapN", settings.get("dialog_color_n", "&H0000FFFF"))]

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
""" + "\n".join(styles) + """

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines: list[str] = []
    for c in cues:
        text = (c.get("text") or "").strip()
        if not text:
            continue
        st = float(c.get("start", 0.0))
        en = float(c.get("end", st))
        if en - st < 0.05:
            continue
        style = "Caption"
        if c.get("speaker") in ("A", "B", "N"):
            style = f"Cap{c['speaker']}"
        wrapped = _wrap(text, wrap_chars)
        anim = r"{\fad(80,40)}"  # 軽いフェードイン/アウト
        lines.append(
            f"Dialogue: 0,{_fmt_time(st)},{_fmt_time(en)},{style},,0,0,0,,{anim}{_ass_escape(wrapped)}"
        )
    return header + "\n".join(lines) + "\n"


def _wrap(text: str, max_chars: int) -> str:
    """長すぎる行を全角換算で折り返す（\\N）。"""
    text = text.replace("\n", " ").strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    out = []
    cur = ""
    for ch in text:
        cur += ch
        if len(cur) >= max_chars:
            out.append(cur)
            cur = ""
    if cur:
        out.append(cur)
    return "\n".join(out)
