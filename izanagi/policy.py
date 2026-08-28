"""収益化セーフティ・リンター（辞書＋正規表現・完全オフライン）。

無人量産で最も高くつく事故＝「一晩10本まるごと黄色マーク（広告制限）」を防ぐ。
検出 → 自動リライト1周（pipeline側） → 残った high は投稿タブで⚠表示の3段構え。
追加のNG語は data/ng_words_user.txt（1行1語・high扱い）で足せる。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (カテゴリ, severity, 正規表現, 言い換えヒント)
NG_RULES = [
    ("暴力", "high", r"殺す|殺した|殺して|殺害|刺し(た|て)|絞め(た|て)|撲殺|殴り殺", "「手にかける」「命を奪う」等へ"),
    ("暴力", "mid", r"ボコボコ|半殺し|袋叩き|ぶん殴", "「ひどく痛めつける」等へ"),
    ("自傷", "high", r"自殺|首を吊|リストカット|飛び降り自殺", "「自ら命を絶つ」等・文脈に注意"),
    ("差別", "high", r"ガイジ|キチガイ|きちがい|池沼|かたわ|土人", "使用しない"),
    ("性的", "high", r"レイプ|強姦|売春|援助交際|援交", "「乱暴される」「違法な関係」等へ"),
    ("性的", "mid", r"セックス|性行為|下着姿|あえぎ", "直接表現を避ける"),
    ("薬物", "high", r"覚醒剤|覚せい剤|大麻|コカイン|シャブ|ヤク中", "「違法薬物」等へ"),
    ("残酷描写", "mid", r"血まみれ|血だらけ|遺体|死体|バラバラ", "「倒れていた」等へ"),
    ("煽り", "mid", r"死ね|くたばれ|ぶっ殺", "強い言葉を避ける"),
    ("医療断定", "mid", r"必ず治る|絶対に治る|絶対に痩せ|がんが消え", "「個人差があります」等・断定を避ける"),
    ("金融断定", "mid", r"絶対(に)?儲かる|必ず稼げる|元本保証|確実に勝てる", "断定を避ける"),
    ("賭博", "mid", r"裏カジノ|闇スロット|違法賭博", "文脈の説明を足す"),
]


def _user_rules() -> list[tuple[str, str, str, str]]:
    f = ROOT / "data" / "ng_words_user.txt"
    out = []
    try:
        for ln in f.read_text(encoding="utf-8").splitlines():
            w = ln.strip()
            if w and not w.startswith("#"):
                out.append(("ユーザー辞書", "high", re.escape(w), "言い換える"))
    except Exception:
        pass
    return out


def check_text(*texts: str) -> list[dict]:
    """台本/タイトル/概要欄を検査して所見リストを返す（word単位で重複除去）。"""
    joined = "\n".join(t for t in texts if t)
    seen = set()
    findings: list[dict] = []
    for cat, sev, pat, suggest in NG_RULES + _user_rules():
        for m in re.finditer(pat, joined):
            w = m.group(0)
            if w in seen:
                continue
            seen.add(w)
            findings.append({"cat": cat, "severity": sev, "word": w, "suggest": suggest})
    findings.sort(key=lambda f: 0 if f["severity"] == "high" else 1)
    return findings
