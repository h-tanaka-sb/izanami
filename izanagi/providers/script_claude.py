"""①台本生成: Anthropic Messages API（Claude）。

出力は TITLE/DESCRIPTION/TAGS/--- + 本文（ナレーション。1文1行＝テロップに割りやすい）。
プロンプトは settings の script_prompt / brushup_prompt で上書き可能（""＝既定）。
"""
from __future__ import annotations

import re

from .base import ScriptProvider, register

DEFAULT_SYSTEM = (
    "あなたはYouTube動画の構成作家兼ナレーション台本ライターです。"
    "視聴維持率を最大化する、声に出して読むためのナレーション台本を書きます。"
    "ルール:\n"
    "・冒頭の挨拶・自己紹介・「今日は〜についてお話しします」は禁止（コールドオープン）。\n"
    "・第1文は物語のクライマックス直前の一瞬から始める。\n"
    "・3文目までに主人公が直面する理不尽・謎を提示する。\n"
    "・5文目までに「この後、結末は想像の斜め上でした」型のオープンループを置き、"
    "そこから時系列に戻って語り始める。\n"
    "・尺配分の目安: 起15% / 承40% / 転35% / 結10%（展開の谷を作らない）。\n"
    "・話し言葉で、1文を短く（テロップに載せやすいよう句点で適切に区切る）。\n"
    "・架空の登場人物の名前は、ひらがなで書く（例: さちこ、たけし。音声の読み間違い防止）。"
    "実在の人物・団体名はそのまま。\n"
    "・専門用語は噛み砕き、具体例を入れる。\n"
    "・終盤にチャンネル登録/次回予告のCTAを一言だけ。\n"
    "・ナレーションとして読む文だけを書く（ト書き・カメラ指示・記号の羅列は書かない）。\n"
    "・話者ラベル（「私：」「部長：」等の行頭ラベル）は書かない。セリフはかぎ括弧「」で書き、"
    "地の文で誰の発言か分かるようにする。"
)

USER_TEMPLATE = (
    "次のお題でYouTube動画のナレーション台本を作ってください。\n\n"
    "【お題】{topic}\n"
    "【追加指示】{instruction}\n"
    "【目安の尺】約{length}分＝ナレーション本文はおよそ{chars}字（±15%目安。"
    "これを大きく超えないこと）\n\n"
    "次の形式で厳密に出力してください（他の文章は付けない）:\n"
    "TITLES:\n"
    "結果先出し|タイトル（32字以内。例:「〜した結果…」）\n"
    "伏せ字|タイトル（32字以内。核心を●●で伏せる）\n"
    "数字|タイトル（32字以内。金額・年数・回数など数字を入れる）\n"
    "立場逆転|タイトル（32字以内。強者と弱者が入れ替わる予感）\n"
    "疑問形|タイトル（32字以内。読者への問いかけ）\n"
    "※各行とも先頭13文字に一番強いワードを置く\n"
    "THUMB: サムネ用キャッチコピー（タイトルの言い換え。8〜13字×最大2行を/区切り）\n"
    "DESCRIPTION: 概要欄（2〜4行。ハッシュタグは書かない）\n"
    "TAGS: タグをカンマ区切りで5〜10個\n"
    "---\n"
    "（ここから下にナレーション本文。1文ずつ改行。読む文だけ。）"
)


def score_title(t: str) -> int:
    """タイトル候補の加点式スコア（CTRの定石ベース・決定論）。"""
    s = 0
    if re.search(r"[0-9０-９]", t):
        s += 2
    if "●" in t or "○" in t:
        s += 1
    if "「" in t or "『" in t:
        s += 1
    if re.search(r"(結果|末路|真相|正体)", t):
        s += 1
    if not re.search(r"[。！？!?]$", t):
        s += 1  # 体言止め・言い切り
    if len(t) > 28:
        s -= 2
    return s


BRUSHUP_DEFAULT = ("以下の台本を、フックをより強く・冗長表現を削り・"
                   "テンポ良く読めるよう推敲してください。")

# ユーザーの推敲指示（例「もっと面白くしてください」）に必ず付ける出力規則。
# これが無いとAIが「リライトしました。〜」等の解説を書き、それがナレーションとして
# 読み上げられてしまう（2026-07-06 の実走で発生）。
BRUSHUP_RULES = (
    "\n\n【出力規則】推敲後のナレーション本文だけを出力すること。"
    "前置き・あいさつ・変更点の解説・改善提案・タイトル案・見出し・箇条書き・"
    "区切り線(---)・質問は一切書かない。1文ずつ改行し、声に出して読む文だけを書く。")


# 冒頭挨拶の機械検査（コールドオープン・ガード）。プロンプト指示だけでは挨拶型に流れるため
_GREETING_RE = re.compile(
    r"こんにちは|こんばんは|おはよう|ご視聴|皆さん|みなさん|本日は|"
    r"今日は.{0,20}(について|お話|ご紹介|テーマ)")


def has_greeting_opening(script: str) -> bool:
    return bool(_GREETING_RE.search((script or "")[:200]))


def build_brushup_prompt(script: str, cfg: dict) -> str:
    """推敲指示＋出力規則＋台本。全scriptプロバイダ共通。"""
    instr = (cfg.get("brushup_prompt") or "").strip() or BRUSHUP_DEFAULT
    rules = BRUSHUP_RULES
    if (cfg.get("script_format") or "narration") == "dialog":
        rules += "「話者|セリフ」（A/B/N）の行形式は必ず維持すること。"
    return f"{instr}{rules}\n\n---\n{script}"


def clean_narration(text: str) -> str:
    """AI出力からナレーション本文だけを取り出す（解説・前置き混入への防御）。

    ・TITLE/DESCRIPTION/TAGS ヘッダが紛れたら _parse で本文抽出
    ・「---」区切りが混ざっていたら最長ブロック＝本文とみなす
    ・markdown見出し/箇条書き/太字だけの行（本文には出ない）を除去
    ・Markdownの表（|セル|セル| と罫線 |---|）を除去。リサーチ結果の表が台本に
      混入すると、TTSが「|」を**「パイプ」と読み上げる**（2026-08-03配布先報告・
      実台本で59箇所を確認）
    """
    t = (text or "").strip()
    if re.search(r"^(TITLE|DESCRIPTION|TAGS):", t, flags=re.MULTILINE):
        t = _parse(t)["script"]
    if re.search(r"^\s*-{3,}\s*$", t, flags=re.MULTILINE):
        blocks = [b.strip() for b in re.split(r"^\s*-{3,}\s*$", t, flags=re.MULTILINE)
                  if b.strip()]
        if blocks:
            longest = max(blocks, key=len)
            # AIが章間の区切りに---を使うと「最長の1章だけ」が台本になり、20分の台本が
            # 2分に化ける（2026-08-17配布先報告⑤）→ 最長ブロックが全体の7割未満なら
            # 「本文が章で分かれている」とみなし、罫線だけ除いて全ブロックを残す。
            # ただし極端に短いブロック（末尾の解説・締めの一言等）は本文でないとみなして
            # 除く＝旧実装が落としていた防御の維持（敵対検証2026-08-17）
            total = sum(len(b) for b in blocks)
            if len(longest) >= total * 0.7:
                t = longest
            else:
                t = "\n".join(b for b in blocks if len(b) >= total * 0.1)
    lines = []
    from ..telop import UI_JUNK_LINES
    for ln in t.splitlines():
        if re.match(r"\s*(#{1,6}\s|[-*・]\s|\*\*)", ln):
            continue
        s = ln.strip()
        if s in UI_JUNK_LINES:
            continue   # チャットUIのボタン文字（ChatGPT Webの「編集」等）の混入
        if s and re.fullmatch(r"[|｜:\-\s]+", s) and ("|" in s or "｜" in s):
            continue   # 表の罫線行 |---:|---|
        if (s.startswith(("|", "｜")) and s.endswith(("|", "｜"))
                and (s.count("|") + s.count("｜")) >= 2):
            continue   # 表のデータ行 | 1987 | 約200億ドル |
        lines.append(ln)
    return "\n".join(lines).strip()


def build_user_prompt(cfg: dict) -> str:
    """お題/追加指示＋（あれば）入力ソースの参考資料からユーザープロンプトを組み立てる。

    参考資料（cfg["_source_context"]）は pipeline.stage_script が izanagi.source で
    用意する（YouTube文字起こし / リサーチ結果）。全scriptプロバイダ共通。
    """
    if cfg.get("video_mode") == "neko":  # 猫ミームは専用フォーマット
        from .. import nekomeme
        return nekomeme.build_neko_user_prompt(cfg)
    mode = (cfg.get("script_source") or "topic").strip()
    topic = (cfg.get("topic") or "").strip() or "（お題未設定）"
    if mode == "youtube":
        topic = "下の【元動画の文字起こし】を題材にした、新しいオリジナルストーリー"
    # 尺→字数の目安換算（日本語TTS約330字/分）。「約N分」だけだと平気で2倍書かれる
    # （2026-08-17配布先報告: 10分設定→17〜22分）ため、字数バジェットを明記する。
    # 世界観プロンプトはDEFAULT_SYSTEMを置換するが、これはUSER側なので必ず届く
    _len_min = int(cfg.get("video_length_min", 5) or 5)
    base = USER_TEMPLATE.format(
        topic=topic,
        instruction=(cfg.get("script_instruction") or "特になし").strip(),
        length=_len_min,
        chars=_len_min * 330,
    )
    if int(cfg.get("video_height", 1080)) > int(cfg.get("video_width", 1920)):
        # 縦ショート専用の書き方（2026-08-19ユーザー要望: 1〜3分設定なのに長くなる・
        # ショートらしい短い大テロップが必要・テロップ割りもAIに考えさせる）
        base += (
            "\n\n【ショート動画（縦型）専用の書き方・厳守】\n"
            f"・本文は合計{_len_min * 330}字以内を厳守する（ショートは長いと最後まで"
            "見られない。超えそうなら要素を削る）\n"
            "・1行＝画面に大きく出る1枚のテロップ。**1行は最大13字ほど**の短い文にし、"
            "テロップ割り（どこで行を切るか）もあなたが設計する\n"
            "・言い切り・体言止め・口語でリズムよく。1文1情報で説明を伸ばさない\n"
            "・1行目は結末を匂わせる強いフックにする")
    if (cfg.get("script_format") or "narration") == "dialog":
        base += (
            "\n\n【本文の形式＝会話形式】ナレーション本文は掛け合いで書く。"
            "1行=1セリフ、形式は「話者|セリフ」（半角パイプ区切り）。話者は次の3種のみ:\n"
            "A＝主人公・聞き手 / B＝相手・解説役 / N＝ナレーション（状況説明）。\n"
            "AとBの掛け合いを中心に、要所でNを挟む。セリフは50文字以内の話し言葉。"
            "括弧のト書きや区切り線は書かない。")
    src = (cfg.get("_source_context") or "").strip()
    return f"{src}\n\n{base}" if src else base


@register("script", "api")
class ClaudeScript(ScriptProvider):
    @classmethod
    def available(cls, cfg: dict):
        if not cfg.get("anthropic_api_key"):
            return False, "anthropic_api_key 未設定"
        return True, ""

    def _client(self, cfg):
        import anthropic
        return anthropic.Anthropic(api_key=cfg["anthropic_api_key"])

    def _call(self, cfg, system: str, user: str, log) -> str:
        client = self._client(cfg)
        model = cfg.get("ai_model") or "claude-opus-4-8"
        # max_tokensは尺に連動（8000固定だと15分超の設定で途中切れする）
        _mt = min(16000, max(8000, int(cfg.get("video_length_min", 5) or 5) * 330 * 2))
        # 長文でもタイムアウトしないようストリームで受ける
        text_parts: list[str] = []
        with client.messages.stream(
            model=model, max_tokens=_mt, system=system,
            messages=[{"role": "user", "content": user}],
        ) as stream:
            for chunk in stream.text_stream:
                text_parts.append(chunk)
        return "".join(text_parts).strip()

    def generate(self, cfg: dict, log=print) -> dict:
        system = (cfg.get("script_prompt") or "").strip() or DEFAULT_SYSTEM
        user = build_user_prompt(cfg)
        log("    Claudeで台本を生成中…")
        raw = self._call(cfg, system, user, log)
        return _parse(raw)

    def brushup(self, script: str, cfg: dict, log=print) -> str:
        log("    Claudeで台本をブラッシュアップ中…")
        return self._call(cfg, DEFAULT_SYSTEM, build_brushup_prompt(script, cfg), log).strip()


def _norm_headers(text: str) -> str:
    """ヘッダ行の形式ゆれを正規形へ寄せる（2026-08-17配布先報告: タイトルが「無題」）。

    AIは指示どおり「TITLES:」と書かず、**TITLES:**（太字）/## TITLES（見出し）/
    TITLES：（全角コロン）/「TITLES: 1候補目を同じ行に」等のゆれを出すことがあり、
    旧regexは全て不一致→無題の動画に落ちていた。行頭のヘッダ行だけを対象に正規化する。
    """
    def _fix(m):
        label = m.group(2).upper()
        rest = (m.group(3) or "").strip().strip("*_ ")
        if label == "TITLES" and rest:   # 同一行に1候補目が書かれた場合は次行へ送る
            return f"TITLES:\n{rest}"
        return f"{label}: {rest}".rstrip()
    # ※コロン後の空白は[ \t]のみ＝改行を食わない（\s*だと「DESCRIPTION:\n次行」の改行を
    #   併合して次のヘッダ行まで説明文に潰す・敵対検証2026-08-17）
    return re.sub(
        r"(?m)^[ \t]*(\*\*|__|#{1,6}[ \t]*)?(TITLES|TITLE|THUMB|DESCRIPTION|TAGS)"
        r"(?:\*\*|__)?[ \t]*[:：][ \t]*(.*)$",
        _fix, text or "")


def _parse(raw: str) -> dict:
    """TITLES(5候補)/THUMB/DESCRIPTION/TAGS/--- 形式を分解。旧TITLE:単行も後方互換で受ける。"""
    raw = _norm_headers(raw)
    title = description = thumb_text = ""
    tags: list[str] = []
    candidates: list[dict] = []
    body = raw
    # 区切りの「---」は**ヘッダ群より後**のものだけを本文境界とみなす
    # （応答の先頭に飾りの罫線があると、旧実装はheadが空になりタイトルが必ず落ちた）
    _hidx = raw.find("TITLES:")
    _search_from = _hidx if _hidx >= 0 else 0
    _sep = re.search(r"(?m)^\s*-{3,}\s*$", raw[_search_from:])
    if _sep:
        _sep_at = _search_from + _sep.start()
        _sep_end = _search_from + _sep.end()
        head = raw[:_sep_at]
    else:
        _sep_at = _sep_end = -1
        head = raw

    m = re.search(r"^TITLES:\s*$(.*?)(?=^(?:THUMB|DESCRIPTION|TAGS):|^\s*-{3,}\s*$|\Z)", head,
                  flags=re.MULTILINE | re.DOTALL)
    if m:
        for ln in m.group(1).splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("※"):
                continue
            if "|" in ln:
                pat, t = ln.split("|", 1)
                if t.strip():
                    candidates.append({"pattern": pat.strip(), "title": t.strip()})
            elif len(ln) >= 8:  # 型名なしで書かれた場合の保険
                candidates.append({"pattern": "", "title": ln})
    m = re.search(r"^TITLE:\s*(.+)$", head, flags=re.MULTILINE)
    if m and not candidates:
        candidates.append({"pattern": "", "title": m.group(1).strip()})
    if candidates:  # 加点式スコアで自動選定
        title = max(candidates, key=lambda c: score_title(c["title"]))["title"]
    m = re.search(r"^THUMB:\s*(.+)$", head, flags=re.MULTILINE)
    if m:
        thumb_text = m.group(1).strip()
    # 「DESCRIPTION:」の直後に改行して本文を書くゆれにも対応（.+?は同一行を要求していた）。
    # ただし複数行の捕捉は---で本文境界が明確な時だけ（---無しで許すと本文全部を説明欄へ
    # 飲み込む）。捕捉の先頭が別ヘッダなら「説明は空」扱い＝メタ再取得が発火できる。
    # 空行も説明の終端＝いずれも敵対検証2026-08-17
    if _sep_at >= 0:
        m = re.search(r"^DESCRIPTION:[ \t]*\n?(?!\s*(?:TAGS|THUMB|TITLES?)[:：])"
                      r"(.+?)(?=^TAGS:|^THUMB:|^\s*-{3,}\s*$|\n[ \t]*\n|\Z)", head,
                      flags=re.MULTILINE | re.DOTALL)
    else:   # 本文との境界が曖昧 → 従来どおり同一行のみ
        m = re.search(r"^DESCRIPTION:[ \t]*(.+)$", head, flags=re.MULTILINE)
    if m:
        description = m.group(1).strip()
    m = re.search(r"^TAGS:\s*(.+)$", raw, flags=re.MULTILINE)
    if m:
        tags = [t.strip() for t in re.split(r"[,、]", m.group(1)) if t.strip()]

    if _sep_end >= 0:
        body = raw[_sep_end:].strip()
    # ヘッダ行が本文に混ざっていたら除去
    body = re.sub(r"^(TITLES?|THUMB|DESCRIPTION|TAGS):.*$", "", body, flags=re.MULTILINE).strip()
    body = re.sub(r"^(結果先出し|伏せ字|数字|立場逆転|疑問形)\|.*$", "", body,
                  flags=re.MULTILINE).strip()
    # 同一行TITLESゆれ＋---欠落のとき、正規化で独立行になった候補タイトルが本文へ残る
    # ことがある → 候補と完全一致する行は本文から除去（敵対検証2026-08-17）
    if candidates:
        _cand = {c["title"] for c in candidates}
        body = "\n".join(l for l in body.splitlines()
                         if l.strip() not in _cand).strip()

    return {
        "script": body or raw,
        "title": title or "無題の動画",
        "title_candidates": candidates,
        "thumb_text": thumb_text,
        "description": description,
        "tags": tags,
    }
