"""ネタ帳ジェネレーター: チャンネルの世界観プロンプトからお題を自動量産する。

毎日複数本の最大ボトルネック＝ネタ切れ対策。履歴と突き合わせて重複を弾く。
"""
from __future__ import annotations

import re
from pathlib import Path

from . import providers, util

ROOT = Path(__file__).resolve().parent.parent


def _hist_path(ch_idx: int) -> Path:
    return ROOT / "data" / f"topics_history_ch{int(ch_idx)}.json"


def _norm(s: str) -> str:
    """重複判定用の正規化（空白/記号/全半角ゆらぎを吸収）。"""
    s = re.sub(r"[\s　【】\[\]「」『』…。、,.!?！？~〜・|｜/]+", "", str(s))
    return s.lower()


# Web版チャットが混ぜる前置き/締めの定型（お題として拾わない）
_META_RE = re.compile(
    r"承知(しました|いたしました)|^(はい|いいえ|了解|かしこまりました|わかりました)[、。]"
    r"|以下(が|の|に|、)|以上(です|になります|でお|、)|ご確認|ご参考|参考にして"
    r"|お題(です|は以下|を\d+|の案|一覧)|(作成|生成|提案|用意|考え)(しました|します|いたしました)"
    r"|よろしくお願い|楽しんで|お役に立て")


def load_history(ch_idx: int) -> list[str]:
    return util.load_json(_hist_path(ch_idx), []) or []


def add_history(ch_idx: int, topics: list[str]) -> None:
    hist = load_history(ch_idx)
    known = {_norm(t) for t in hist}
    for t in topics:
        if _norm(t) not in known:
            hist.append(t)
            known.add(_norm(t))
    util.save_json_atomic(_hist_path(ch_idx), hist[-500:])  # 直近500件だけ保持


# 「お題の自動生成への指示」欄が空の時に使う標準の指示。
# 欄に文章が入っていれば**この標準指示は丸ごと差し替え**られる（2026-08-03ユーザー要望:
# 「毎回この定型になる。この部分を指示欄で変えられるようにするべき」）。
# GUIの📋ボタンでこの文面を欄へ挿入して編集の土台にできる。
DEFAULT_TOPIC_GUIDE = (
    "クリックしたくなる具体的なシチュエーション（人物・立場・事件）を入れること。\n"
    # 実話系チャンネルで「年も名前も無い＝何の話か分からない」お題が量産される問題への対策
    "【実在の事件・人物を扱う場合の必須ルール】\n"
    "・いつ（西暦）と誰（人名・会社名などの固有名詞）を必ず入れること。\n"
    "・「ある女将」「地上げ王」「大手銀行」のような、誰の話か特定できない書き方は禁止。\n"
    "  悪い例:「1991年、料亭女将はなぜ銀行を信じ込ませたのか」\n"
    "  良い例:「1991年、尾上縫はなぜ2兆円を借りられたのか」\n"
    "・固有名詞は確実に事実である場合だけ書く（うろ覚え・推測で名前を出さない）。\n"
    "・架空・創作のお題（実在の事件でないもの）にはこのルールを適用しない。")


def build_topics_prompt(cfg: dict, n: int, hist: list[str]) -> str:
    """お題生成のユーザープロンプトを組み立てる（テスト可能に分離）。

    構造: 出力形式（固定・行パーサの契約）→ 指示（欄の内容が最優先。空なら標準指示）
    → 世界観 → 履歴。指示欄を**ルール文の前・最上段**に置く＝「書いても反映されない」
    と感じさせない（旧実装は長いルールの後ろで埋もれていた）。"""
    world = (cfg.get("script_prompt") or "").strip() or (cfg.get("topic") or "").strip() \
        or "視聴者がスカッとする実話風ストーリー"
    instr = (cfg.get("topic_prompt") or "").strip()
    recent = "\n".join(f"・{t}" for t in hist[-100:]) or "（なし）"
    return (
        f"次の世界観のYouTubeチャンネルの動画のお題を{n}個、"
        "1行1個・番号なし・各36文字以内で出してください。\n\n"
        "【お題づくりの指示（最優先で従うこと）】\n"
        + (instr if instr else DEFAULT_TOPIC_GUIDE) + "\n\n"
        + f"【チャンネルの世界観】\n{world[:1500]}\n\n"
        f"【過去に使ったお題（被らないこと）】\n{recent}\n\n"
        "お題だけを出力（説明・番号・記号は不要）:")


def generate_topics(cfg: dict, n: int, log=print, ctx=None) -> list[str]:
    """チャンネルの世界観からお題をn個生成（履歴重複は除去）。mockは連番ダミー。"""
    ch_idx = int(cfg.get("active_channel", 0))
    instr = (cfg.get("topic_prompt") or "").strip()
    hist = load_history(ch_idx)
    if cfg.get("script_engine") == "mock":
        return [f"テストお題{i + 1:02d}" for i in range(n)]
    prompt = build_topics_prompt(cfg, n, hist)
    if instr:
        log(f"    指示欄の内容を最優先で使用します（{len(instr)}字）。")
    else:
        log("    指示欄が空のため標準の指示（クリック誘引・実在事件は西暦+固有名詞）を"
            "使用します。変えたい時は「文章・世界観」タブの📋から編集できます。")
    c = dict(cfg)
    c["topic"] = prompt
    c["script_prompt"] = ""      # 世界観はprompt本文に織込済み（二重適用を避ける）
    c["_source_context"] = ""
    c["script_instruction"] = ""

    prov = providers.get_provider("script", c, log, ctx)
    if getattr(prov, "engine", "") == "mock":
        log("    ⚠ お題生成: 選択中の台本エンジンが使えません。"
            "設定・キータブでAPIキーを入れる（またはWeb版ならログイン）してください。")
        return []
    log(f"    お題を{n}個生成中（{c.get('script_engine')}）…")
    try:
        if hasattr(prov, "_call"):  # 全エンジン（API3種+Web3種）がここを通る
            # 役割文はシステム側だけ（ユーザープロンプト側にも書くと二重表示になる）
            raw = prov._call(c, "あなたはYouTubeチャンネルの企画者です。", prompt, log)
        else:  # 保険: 生テキストの口が無いプロバイダ
            raw = (prov.generate(c, log) or {}).get("script", "")
    except Exception as e:
        log(f"    お題生成に失敗: {str(e)[:120]}")
        return []
    # 安全網: AIがお題でなく台本（TITLES:/---形式）を書いてきたら、その行を
    # お題として拾わない（台本の文がお題欄に流れ込む事故の根絶）
    if re.search(r"^(TITLES?|DESCRIPTION|TAGS|THUMB)\s*:", raw or "", flags=re.MULTILINE) \
            or re.search(r"^\s*-{3,}\s*$", raw or "", flags=re.MULTILINE):
        log("    ⚠ AIがお題でなく台本を返しました。もう一度お試しください"
            "（続くようなら①台本のエンジンを変えてみてください）。")
        return []
    known = {_norm(t) for t in hist}
    out: list[str] = []
    seen = set()
    for ln in (raw or "").splitlines():
        # 行頭の「箇条書き記号」だけを外す。
        # 2026-07-25修正: 旧実装 ^[\s\d\-・*.)）]+ は先頭の数字を無差別に食うため、
        # 「1991年、地上げ王は…」→「年、地上げ王は…」と**西暦が丸ごと消えていた**
        # （サムネ文字案 pipeline.py:1153 で同じ罠を潰した時の書き方に合わせる）。
        t = re.sub(r"^\s*(?:[-–—・*•]|\d{1,2}[.)）、:：])\s*", "", ln.strip()).strip()
        if not (4 <= len(t) <= 48):
            continue
        if _META_RE.search(t):  # 「はい、承知しました」「以下がお題です」「以上です」等は除外
            continue
        key = _norm(t)
        if key in known or key in seen:
            continue
        seen.add(key)
        out.append(t)
        if len(out) >= n:
            break
    log(f"    お題 {len(out)}個を生成（履歴と重複する案は除外済み）。")
    return out
