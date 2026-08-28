"""オーケストレータ: 6ステージを順に実行し、sentinel で skip/run/再開を制御する。

各ステージは成功時のみ sentinel 成果物を書く。失敗時は _debug に診断を残して停止し、
同じプロジェクトフォルダを resume_dir に指定すれば続きから再開できる。
Web自動操作が要るエンジンが選ばれていれば、共有Chromeセッション(ctx)を1度だけ開いて全ステージで共用する。
"""
from __future__ import annotations

import csv
import datetime
import os
import re
import time
import traceback
from pathlib import Path

from . import browser_ai
from . import compose as _compose
from . import providers, telop, util
from .project import STAGE_LABEL, STAGES, Project


# ── エンジン自動フォールバック ───────────────────────────────────────────
def _run_with_fallback(capability: str, cfg: dict, ctx, log, run_fn):
    """選択エンジン→fallback_chainの順に試す（夜間のquota/キー切れ自己回復）。

    run_fn(prov, c) -> truthy | None。返り値 (result, engine_used, prov)。
    ※ get_provider は利用不可時に黙ってmockへ代替するため、ここで検出して次の候補へ回す
    （=固定サンプル台本のゴミ動画量産を根絶）。"""
    primary = cfg.get(f"{capability}_engine", "")
    chain = [primary] + [e for e in (cfg.get(f"{capability}_fallback_chain") or [])
                         if e and e != primary]
    what = {"script": "台本", "visual": "画像"}.get(capability, capability)
    if str(primary).endswith("_desktop"):
        # デスクトップ連携は「人が付いて操作する」明示選択＝勝手にAPI(課金/quota)へ切替えない。
        # 失敗したら理由を出して停止（アプリ起動/ウィンドウ名を直して♻続きからで再開）。
        chain = [primary]
        log("    ※ デスクトップ連携が選択されています。失敗しても他のAIへは自動切替しません"
            "（切替したい場合は②〜⑤のエンジンを変更してください）。")

    def _stopped():
        try:
            return bool(browser_ai.STOP_CHECK and browser_ai.STOP_CHECK())
        except Exception:
            return False

    for pos, eng in enumerate(chain):
        if _stopped():  # ⏹押下時に残りのフォールバック（Web再試行）を空回りしない
            log("    ⏹ 中断要求 → フォールバックを打ち切ります。")
            return None, "", None
        nxt = chain[pos + 1] if pos + 1 < len(chain) else None
        c = dict(cfg)
        c[f"{capability}_engine"] = eng
        # フォールバックでWeb版へ落ちた時、共有Chromeがまだ無ければここで遅延接続する
        # （起動時に主エンジンがAPIならChromeは開いていない＝夜間quota切れ時の自己回復の要）
        if ctx is None and providers.engine_needs_browser(capability, eng):
            opener = cfg.get("_ensure_browser")
            if callable(opener):
                ctx = opener()
        prov = providers.get_provider(capability, c, log, ctx)
        if getattr(prov, "engine", "") == "mock" and eng != "mock":
            if eng == primary:
                log(f"    {_ENGINE_LABEL.get(eng, eng)} は利用不可（キー/認証/ログインなし）"
                    "→ 代替エンジンを探します…")
            continue
        err = ""
        try:
            r = run_fn(prov, c)
        except Exception as e:
            err = str(e)
            r = None
        if r:
            if eng != primary:
                log(f"    ✅ 代替エンジン「{_ENGINE_LABEL.get(eng, eng)}」で続行します。")
            return r, eng, prov
        _explain_engine_failure(eng, err or "出力が空でした", nxt, log, what=what)
    return None, "", None


# ── 読み仮名マップ（動画ごとの読み間違い対策） ─────────────────────────────
def _brushup_ok(orig: str, bu: str, log=print, expect_ratio: float = 1.0) -> bool:
    """推敲の戻り値が「同じ台本を磨いたもの」か検査する。

    デスクトップ/ブラウザ連携は回答をクリップボード等から拾うため、
    無関係な文字列（アプリ自身のログ等）を掴むと**台本がそれに置き換わり**、
    まったく別の内容の動画ができてしまう（実際に発生した）。
    長さと語の重なりで「別物」を弾き、疑わしければ推敲前を使う。
    expect_ratio: 意図的なリサイズ（短縮リライト等）の期待倍率。長さレンジだけを
    期待値中心にずらし、n-gramの同一性検査はそのまま維持する（2026-08-17）。"""
    o, b = (orig or "").strip(), (bu or "").strip()
    if len(b) < 100:
        log("    推敲の結果が短すぎるため、推敲前の台本を使います。")
        return False
    if not (0.4 * expect_ratio <= len(b) / max(1, len(o)) <= 2.5 * expect_ratio):
        log(f"    推敲の結果が元と大きく違う長さ（{len(o)}字→{len(b)}字）のため、"
            "推敲前の台本を使います。")
        return False
    # 元台本の特徴的な2文字を拾い、どれだけ残っているかで同一性を見る
    import re as _re
    grams = {o[i:i + 2] for i in range(0, len(o) - 1, 7) if _re.match(r"[^\s]{2}", o[i:i + 2])}
    if grams:
        keep = sum(1 for g in grams if g in b) / len(grams)
        if keep < 0.25:
            log(f"    推敲の結果が別の文章に見えます（一致 {keep:.0%}）。"
                "推敲前の台本を使います。")
            return False
    return True


def _kata_to_hira(s: str) -> str:
    """カタカナをひらがなに寄せる（AIがカタカナで読みを返しても取りこぼさない）。"""
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


def _parse_reading_lines(text: str) -> dict:
    """「語=よみ」形式の行 → {語: ひらがな読み}。不正な行は捨てる。

    よみがカタカナで返ってきた場合もひらがなへ正規化して受け入れる
    （形式ゆれで全滅すると読み間違い対策が黙って無効になるため）。"""
    out: dict[str, str] = {}
    for ln in (text or "").splitlines():
        if "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        k = k.strip().lstrip("・-*　 ")
        v = _kata_to_hira(v.strip())
        if (k and v and k != v and len(k) <= 12
                and re.fullmatch(r"[ぁ-んー]+", v)):
            out[k] = v
    return out


_READING_PROMPT = (
    "次のナレーション台本から、日本語の音声合成が読み間違えやすい語だけを抽出してください。\n"
    "対象: 人名・地名・会社名・商品名・難読漢字・特殊な読みの熟語。\n"
    "  さらに、文脈で読みが変わる語も対象（例: 一日=いちにち/ついたち、市場=しじょう/いちば、"
    "人気=にんき/ひとけ、最中=さいちゅう/もなか、額=がく/ひたい、君=きみ/くん、"
    "入れ=いれ/はいれ）。**この台本での正しい読み**を台本の文脈から判定して出すこと。\n"
    "  文脈で読みが変わる語は、誤爆を防ぐため**前後の文字を含むキー**で出すこと"
    "（例: 額に入れて=がくにいれて、君の部下=きみのぶか）。そのキーが同じ文字列を含む"
    "別の語（総額・田中君 等）を壊さないか確認してから出すこと。\n"
    "  **業界・専門用語で一般の音読みと違う読みをする語は必ず入れる**"
    "（例: 終値=おわりね、出来高=できだか、建玉=たてぎょく、上手=じょうず/うわて/かみて）。\n"
    "  『普通に読めるから不要』と判断せず、**少しでも読み違いうる語は入れる**こと"
    "（読み上げAIは文脈を読めないため、人間が自明と思う語ほど間違える）。\n"
    "出力形式: 1行1語で「語=よみ」（よみは全てひらがな）。最大30語。\n"
    "確実に正しい読みだけを出す（読みが不確かな語・普通に読める語は含めない）。\n"
    "該当なしなら「なし」とだけ出力。\n\n")


def _build_reading_map(script: str, cfg: dict, log, ctx=None) -> dict:
    """台本から読み間違えやすい語（人名・難読漢字等）をAIで抽出し読み仮名マップを作る。

    2026-07-24 修正: 旧実装は「Geminiキーがあればgeminiだけ試す（if/elif）」で、
    Geminiが残高切れ等で失敗すると **Claudeキーがあっても諦めていた**（実際に
    9本連続で reading_map.json が作られず、読み間違い対策が全く効いていなかった）。
    → Claude→Gemini→ブラウザ/デスクトップと順に試す `_oneshot_text` に統一する。"""
    text = _oneshot_text(cfg, "あなたは日本語の読み方に詳しい校正者です。",
                         _READING_PROMPT + script[:6000], log=log, ctx=ctx)
    if not (text or "").strip():
        log("    ⚠ 読み仮名マップを作れませんでした（AIに接続できず）。"
            "人名・難読語が読み間違えられる可能性があります"
            "（設定タブの『📖 読み方辞書を編集』に語を追加すると確実に直せます）。")
        return {}
    return _parse_reading_lines(text)


# ── 主人公プロファイル（2026-08-04配布先要望: 声・画像の性別不一致対策） ──────
_PROFILE_PROMPT = (
    "次のナレーション台本の「主人公（一人称の語り手）」について、台本の記述だけから判定してください。\n"
    "判定材料の例: 一人称（私/俺/僕）、自己言及（妻として/夫として/母親の私）、"
    "呼ばれ方（〜さん/くん/ちゃん）、職場での立場。\n"
    "出力は次の1行のJSONだけ（説明文・コードブロックは書かない）:\n"
    '{"gender":"female|male|unknown","age_range":"20s|30s|40s|50s|60s|unknown",'
    '"role":"職業や立場（日本語・不明なら空）","confidence":"high|low"}\n'
    "台本から推測できない場合は gender=unknown / confidence=low とする。\n\n")


def _parse_profile(text: str) -> dict:
    """AI出力→ {gender, age_range, role}。低確信・解析不能は unknown（従来動作へ）。"""
    m = re.search(r"\{[^{}]*\}", text or "", re.S)
    if m:
        try:
            import json as _j
            d = _j.loads(m.group(0))
            g = str(d.get("gender") or "").strip().lower()
            if g not in ("male", "female") or str(d.get("confidence") or "").lower() == "low":
                g = "unknown"
            age = str(d.get("age_range") or "").strip().lower()
            if not re.fullmatch(r"[2-9]0s", age):
                age = ""
            return {"gender": g, "age_range": age,
                    "role": str(d.get("role") or "").strip()[:30]}
        except Exception:
            pass
    return {"gender": "unknown", "age_range": "", "role": ""}


def _ensure_protagonist(proj: "Project", cfg: dict, log, ctx=None) -> dict:
    """主人公プロファイルを返す。無ければ台本から1回だけAI判定し meta.json に保存する。

    「主人公が誰か」の情報がどの工程にも無いことが、女性主人公が男声で読まれる/
    画像ごとに人物の性別が変わる問題の共通の根（2026-08-04配布先報告）。
    ♻再開・作り直しでは保存済みを使う（再判定しない）。判定不能でも生成は止めない。"""
    meta = util.load_json(proj.path("script/meta.json"), {}) or {}
    prot = meta.get("protagonist")
    if isinstance(prot, dict) and prot.get("gender"):
        return prot
    vbg = cfg.get("voice_by_gender") or {}
    if not (isinstance(vbg, dict) and vbg.get("auto_enabled")):
        return {}
    if cfg.get("video_mode") == "neko" or cfg.get("script_engine") == "mock":
        return {}   # 猫ミームは読み上げなし / mockはテスト用
    try:
        script = proj.path("script/script.txt").read_text(encoding="utf-8")
    except Exception:
        return {}
    text = _oneshot_text(cfg, "あなたは物語の登場人物分析が得意な編集者です。",
                         _PROFILE_PROMPT + script[:6000], log=log, ctx=ctx)
    prot = _parse_profile(text)
    meta["protagonist"] = prot
    util.save_json_atomic(proj.path("script/meta.json"), meta)
    return prot


_GENDER_VOICE_KEY = {"api": "tts_voice", "gemini": "gemini_tts_voice",
                     "edge": "edge_voice", "fish": "fish_voice"}


def _apply_gender_voice(c: dict, eng: str, gender: str, log=None) -> None:
    """判定した主人公の性別に応じて、このエンジン用の声キーを差し替える。

    対象は api/gemini/edge/fish。Fishは声IDに性別の既定が無いため、女声/男声欄に
    **自分の声IDを登録した時だけ**切り替わる（2026-08-17配布先報告「✅していても
    動作していない」→ 未登録時は従来どおり＋その旨を案内ログで明示する）。
    unknown・トグルOFF・声未設定なら何もしない＝完全に従来動作。"""
    vbg = c.get("voice_by_gender") or {}
    if not (isinstance(vbg, dict) and vbg.get("auto_enabled") and gender in ("male", "female")):
        return
    key = _GENDER_VOICE_KEY.get(eng)
    v = str(((vbg.get(gender) or {}).get(eng)) or "").strip() if key else ""
    _g = "女性" if gender == "female" else "男性"
    if key and v:
        c[key] = v
        if log:
            log(f"    主人公={_g}と判定 → 声を {v} に自動切替")
    elif key and eng == "fish" and log:
        log(f"    主人公={_g}と判定しましたが、Fishの{_g}用の声IDが未登録のため"
            "声はいつものままです（作成タブの女声/男声欄に声IDを登録すると切り替わります）。")


# ── 各ステージ ──────────────────────────────────────────────────────────
def _with_intro_outro_text(script: str, cfg: dict) -> str:
    """チャンネルの「冒頭・終了」定型文を台本の前後に挿入する（空なら何もしない）。

    縦ショートのしめは横用のoutro_textでなく**ショート専用の誘導文**だけを使う
    （2026-08-19ユーザー要望「動画最後にショート専用の誘導文を入れる設定が欲しい」。
    横用の登録導線はショートでは長すぎ/文脈違いになりがちのため分離）。"""
    intro = (cfg.get("intro_text") or "").strip()
    if int(cfg.get("video_height", 1080)) > int(cfg.get("video_width", 1920)):
        outro = (cfg.get("outro_text_shorts") or "").strip()
    else:
        outro = (cfg.get("outro_text") or "").strip()
    parts = [p for p in (intro, (script or "").strip(), outro) if p]
    return "\n".join(parts)


def stage_script(proj: Project, cfg, ctx, log, stop) -> bool:
    from . import source
    # 入力ソース前処理（YouTube文字起こし / キーワードリサーチ）。失敗(None)は停止
    src_ctx = source.prepare_source(cfg, log, save_dir=str(proj.path("script")))
    if src_ctx is None:
        proj.write_debug("script_source_error.txt",
                         f"script_source={cfg.get('script_source')} topic={cfg.get('topic')}")
        return False
    cfg = dict(cfg)
    cfg["_source_context"] = src_ctx

    def _gen(prov_, c_):
        r = prov_.generate(c_, log)
        return r if (r.get("script") or "").strip() else None

    result, used_eng, prov = _run_with_fallback("script", cfg, ctx, log, _gen)
    if not result:
        log("    【台本エラー】どのエンジンでも台本を生成できませんでした"
            "（APIキー/ログイン/quotaを確認してください）。")
        proj.write_debug("script_error.txt", "all script engines failed or unavailable")
        return False
    script = (result.get("script") or "").strip()
    # 生成直後の生台本にも整形をかける（従来は推敲経由のみ＝推敲なし/棄却時に
    # Markdownの表や見出しが素通りし、TTSが「|」を「パイプ」と読み上げていた）
    if cfg.get("video_mode") != "neko":   # 猫ミームは | 区切りの行形式＝整形禁止
        from .providers.script_claude import clean_narration as _cn0
        c0 = _cn0(script)
        if c0 and len(c0) > 20:
            if len(c0) < len(script):
                log(f"    台本から表・見出しなどの混入 {len(script) - len(c0)}字を除去。")
            script = c0
    if (cfg.get("brushup_engine") and cfg.get("brushup_engine") != "none"
            and cfg.get("video_mode") != "neko"):  # 猫ミームは行形式が壊れるので推敲なし
        try:
            proj.path("script/script_raw.txt").write_text(script, encoding="utf-8")  # 推敲前を保存
            bu = prov.brushup(script, cfg, log)
            if bu and not _brushup_ok(script, bu, log):
                bu = ""     # 別物が返ってきた＝推敲前の台本をそのまま使う
            if bu and len(bu) > 20:
                from .providers.script_claude import clean_narration
                cleaned = clean_narration(bu)  # 解説・前置きの混入を除去（読み上げ事故防止）
                # 除去しすぎ（＝本文まで削れた）ときも採用しない。整形後の文で再判定する
                if cleaned and len(cleaned) > 20 and _brushup_ok(script, cleaned, log):
                    if len(cleaned) < len(bu):
                        log(f"    推敲出力から解説とみられる {len(bu) - len(cleaned)}字を除去。")
                    proj.path("script/brushup.txt").write_text(cleaned, encoding="utf-8")
                    script = cleaned
        except Exception as e:
            log(f"    ブラッシュアップは省略（{e}）")
    # 長さゲート（2026-08-17配布先報告: 10分設定が17〜22分/2分の動画になる）。
    # 「約N分」の一言だけではAIは平気で2倍書き、Web応答の途中確定では極端に短くなる。
    # 目安=330字/分で照合し、超過は1回だけ短縮リライト・不足は打ち切り疑いで1回だけ再生成
    if cfg.get("video_mode") != "neko" and cfg.get("script_engine") != "mock":
        from .providers.script_claude import clean_narration as _cn1
        _tgt = int(cfg.get("video_length_min", 5) or 5) * 330

        def _nlen(s: str) -> int:
            return len(re.sub(r"\s", "", s or ""))

        def _shorten(cur: str) -> str:
            """超過台本を1回だけ短縮リライトする（採用できなければ元を返す）。
            採用は _brushup_ok（期待倍率つき）に加えて **短縮後も0.55×目安以上** を課す
            （expect比の下限0.4×だけだと「短縮版の途中切れ」＝結末欠落の台本が
            合格してしまう・敵対検証2026-08-17）。"""
            try:
                c3 = dict(cfg)
                c3["brushup_prompt"] = (
                    f"本文を約{_tgt}字（約{cfg.get('video_length_min', 5)}分の読み上げ量）へ"
                    "短縮する。柱となる展開・結末は残し、冗長な描写と重複を削る。"
                    "短縮後の本文だけを出力（前置きや説明は書かない）。")
                bu3 = _cn1(prov.brushup(cur, c3, log) or "")
                if (bu3 and len(bu3) > 20
                        and _brushup_ok(cur, bu3, log,
                                        expect_ratio=_tgt / max(1, _nlen(cur)))
                        and _nlen(bu3) >= _tgt * 0.55):
                    log(f"    短縮リライト完了: 約{_nlen(bu3)}字≒{_nlen(bu3) / 330:.0f}分")
                    return bu3
                log("    短縮リライトは採用できず、元の台本のまま進みます"
                    "（動画が目安より長くなります）。")
            except Exception as e:
                log(f"    短縮リライトは省略（{e}）")
            return cur

        _n = _nlen(script)
        # 縦ショートは超過に厳しく（1〜3分想定＝少しの超過でも体感が長い・2026-08-19要望）
        _hi = 1.2 if int(cfg.get("video_height", 1080)) > int(cfg.get("video_width", 1920)) \
            else 1.35
        if _n > _tgt * _hi and prov is not None:
            log(f"    ⚠ 台本が目安より長すぎます（約{_n}字≒{_n / 330:.0f}分 / "
                f"目安{_tgt}字）→ 1回だけ短縮リライトします…")
            script = _shorten(script)
        elif _n < _tgt * 0.55 and prov is not None:
            log(f"    ⚠ 台本が目安より短すぎます（約{_n}字≒{_n / 330:.0f}分 / "
                f"目安{_tgt}字）＝応答の途中切れの疑い → 1回だけ作り直します…")
            try:
                r2 = _gen(prov, cfg)
            except Exception:
                r2 = None
            _s2 = _cn1((r2 or {}).get("script") or "") if r2 else ""
            _n2 = _nlen(_s2)
            if _s2 and _n2 > _n:
                script = _s2
                for _k in ("title", "title_candidates", "thumb_text",
                           "description", "tags"):
                    if r2.get(_k):
                        result[_k] = r2[_k]
                # 採用後にもう一度目安と突合する（「元より1字でも長ければ採用」だけだと
                # 途中切れ→今度は2倍、がゲートを素通りする・敵対検証2026-08-17）
                if _n2 > _tgt * _hi:   # 縦は1.2×の厳格判定を再突合にも適用（2026-08-19）
                    log(f"    ⚠ 作り直しは今度は長すぎました（約{_n2}字≒{_n2 / 330:.0f}分）"
                        "→ 短縮リライトします…")
                    script = _shorten(script)
                elif _n2 < _tgt * 0.55:
                    log(f"    ⚠ 作り直しでも目安に届きません（約{_n2}字≒{_n2 / 330:.0f}分）。"
                        "このまま進みますが、動画は目安より短くなります。")
                else:
                    log(f"    作り直し完了: 約{_n2}字≒{_n2 / 330:.0f}分")
            else:
                log("    作り直しでも改善しなかったため、元の台本のまま進みます"
                    "（動画が目安より短くなります）。")
    # コールドオープン＋収益化セーフティの機械検査 → 必要ならもう1周だけ自動リライト
    if cfg.get("video_mode") != "neko":
        from . import policy
        from .providers.script_claude import clean_narration as _clean
        from .providers.script_claude import has_greeting_opening
        fix_notes = []
        if has_greeting_opening(script):
            fix_notes.append("冒頭の挨拶・前置き・自己紹介を削除し、事件のクライマックス直前の"
                             "一文から始める（コールドオープン）。")
        findings = policy.check_text(script, result.get("title", ""))
        if findings:
            words = "、".join(f["word"] for f in findings[:12])
            fix_notes.append(f"次の表現を収益化セーフな穏当表現に言い換える（意味は保つ）: {words}")
        if fix_notes and prov is not None:
            try:
                c2 = dict(cfg)
                c2["brushup_prompt"] = ("次の修正だけを行い、それ以外の内容・構成・長さは変えない:\n- "
                                        + "\n- ".join(fix_notes))
                bu2 = _clean(prov.brushup(script, c2, log) or "")
                # 1回目と同じく別物チェックを通す（ここが素通しだと、AIが「承知しました」＋
                # 無関係な文章を返したときに台本がまるごと差し替わって、そのまま音声になる）
                if bu2 and len(bu2) > 20 and _brushup_ok(script, bu2, log):
                    script = bu2
                    log("    コールドオープン/表現ガードで自動リライトしました。")
            except Exception as e:
                log(f"    ガード再推敲は省略（{e}）")
        report = {"greeting": has_greeting_opening(script),
                  "findings": policy.check_text(script, result.get("title", ""))}
        util.save_json_atomic(proj.path("script/policy_report.json"), report)
        highs = [f for f in report["findings"] if f["severity"] == "high"]
        if highs:
            log("    ⚠ 収益化リスクの強い表現が残っています: "
                + "、".join(f["word"] for f in highs[:5])
                + "（投稿タブで「⚠要確認」表示になります）")

    # 読み間違い対策: この台本専用の読み仮名マップ（人名・難読語→ひらがな）を自動生成
    if cfg.get("video_mode") != "neko" and cfg.get("script_engine") != "mock":
        rm = _build_reading_map(script, cfg, log, ctx)
        if rm:
            util.save_json_atomic(proj.path("script/reading_map.json"), rm)
            preview = "、".join(f"{k}={v}" for k, v in list(rm.items())[:5])
            log(f"    読み仮名マップ: {len(rm)}語を自動抽出（例: {preview}）")
            log("    → TTSにはひらがな読みで渡します（テロップは原文のまま）。")

    # チャンネルの「冒頭・終了」定型文を台本の最初/最後に挿入（読み上げ＋テロップ一体）
    script2 = _with_intro_outro_text(script, cfg)
    if script2 != script:
        log("    冒頭・終了の定型ナレーションを挿入しました。")
        script = script2

    # タイトル/説明の欠落救済（2026-08-17配布先報告: ランダムで「無題」・説明テンプレのみ）。
    # AIが出力形式を無視するとヘッダ抽出が空になる → 完成台本を添えてメタだけ再取得する
    if (cfg.get("video_mode") != "neko" and cfg.get("script_engine") != "mock"
            and (result.get("title") in ("", None, "無題の動画")
                 or not (result.get("description") or "").strip())):
        log("    ⚠ タイトル/説明が取れていません → 台本からメタ情報だけ再取得します…")
        try:
            from .providers.script_claude import _parse as _mparse
            _mtxt = _oneshot_text(
                dict(cfg, _web_timeout=120),
                "あなたはYouTube動画のタイトル・概要欄づくりが得意な編集者です。",
                "次のナレーション台本に合うメタ情報を、この形式で**厳密に**出力してください"
                "（他の文章は書かない・各ヘッダは行頭に半角コロンで）:\n"
                "TITLES:\n（タイトル候補を5行・各32字以内）\n"
                "THUMB: サムネ用キャッチコピー（8〜13字×最大2行を/区切り）\n"
                "DESCRIPTION: 概要欄（2〜4行。ハッシュタグは書かない）\n"
                "TAGS: タグをカンマ区切りで5〜10個\n\n【台本】\n" + script[:6000],
                log=log, ctx=ctx)
            _mp = _mparse(_mtxt or "")
            if _mp.get("title") not in ("", None, "無題の動画"):
                for _k in ("title", "title_candidates", "thumb_text"):
                    if _mp.get(_k):
                        result[_k] = _mp[_k]
                log(f"    メタ再取得OK: {_mp['title']}")
            if (_mp.get("description") or "").strip():
                result["description"] = _mp["description"]
            if _mp.get("tags"):
                result["tags"] = _mp["tags"]
        except Exception as e:
            log(f"    メタ再取得は省略（{e}）")
    meta = {"title": result.get("title", "無題の動画"),
            "title_candidates": result.get("title_candidates", []),
            "thumb_text": result.get("thumb_text", ""),
            "description": result.get("description", ""),
            "tags": result.get("tags", []),
            "engine_used": used_eng}
    util.save_json_atomic(proj.path("script/meta.json"), meta)
    proj.path("script/script.txt").write_text(script, encoding="utf-8")  # ← sentinel
    if meta["title"] in ("", "無題の動画"):
        log("    ⚠ タイトルを自動生成できませんでした。このままだと投稿は自動確定されません"
            "（投稿タブでタイトルを付けてから🚀してください）。")
    else:
        log(f"    タイトル: {meta['title']}")
    return True


_ENGINE_LABEL = {"api": "Google Cloud（高品質）", "gemini": "Gemini（API）",
                 "edge": "Edge（無料）", "gemini_api": "Gemini（API）",
                 "chatgpt_api": "ChatGPT（API）", "claude_web": "Claude（ブラウザ）",
                 "gemini_web": "Gemini（ブラウザ）", "chatgpt_web": "ChatGPT（ブラウザ）"}

_QUOTA_RE = re.compile(r"429|RESOURCE_EXHAUSTED|quota|rate ?limit|レート制限|無料枠|閾値",
                       re.IGNORECASE)


def _explain_engine_failure(eng: str, err: str, next_eng: str | None, log, what: str = "音声"):
    """失敗理由（特に無料枠切れ）を人に分かる言葉で知らせ、切替を宣言する。"""
    label = _ENGINE_LABEL.get(eng, eng)
    if _QUOTA_RE.search(err or ""):
        log(f"    ⚠ {label} は無料枠の上限（quota）に達しています（枠は毎日リセットされます）。")
    else:
        log(f"    ⚠ {label} で{what}の生成に失敗しました: {(err or '')[:100]}")
    if next_eng:
        log(f"    ♻ {what}を「{_ENGINE_LABEL.get(next_eng, next_eng)}」に切り替えて作り直します。")


def _tts_engine_chain(cfg) -> list[str]:
    primary = cfg.get("tts_engine", "api")
    return [primary] + [e for e in (cfg.get("tts_fallback_chain") or [])
                        if e and e != primary]


def stage_tts(proj: Project, cfg, ctx, log, stop) -> bool:
    if cfg.get("video_mode") == "neko":  # 猫ミーム=読み上げなし（無音＋文字数タイミング）
        from . import nekomeme
        return nekomeme.stage_tts_neko(proj, cfg, log)
    engines = _tts_engine_chain(cfg)
    primary = engines[0]
    # Fishの声ID未設定ガード（2026-08-17配布先報告: 声が1行ごとにバラバラ）。
    # Fishは1行=1リクエストで、reference_id無しだと毎回違う声が返る（中国語読みの行が
    # 混ざる報告③も同根＝中国語ネイティブ声を引いた行）。警告ログだけでは夜間量産で
    # すり抜けるため、②をここで止める＝フェイルクローズ（黙ってEdgeに落として
    # 「Fishを選んだのに別品質」にもしない）
    if primary == "fish" and not (cfg.get("fish_voice") or "").strip():
        log("    【音声エラー】Fishの「声ID(reference_id)」が未設定です。空のままだと"
            "1行ごとに声が変わる（外国語読みの行が混ざることもある）ため、②を中止しました。")
        log("    → 作成タブの声欄「＋声を登録」で自分の声IDを登録するか、"
            "エンジンをEdge等へ変更してください。")
        proj.write_debug("tts_error.txt", "fish selected but fish_voice(reference_id) empty")
        return False
    # フォールバック候補にfishが居る場合も同じ理由で外す（主エンジンの失敗時に
    # 声ID空のfishへ落ちると、結局バラバラ声の音声が完走してしまう・敵対検証2026-08-17）
    if "fish" in engines[1:] and not (cfg.get("fish_voice") or "").strip():
        engines = [e for e in engines if e != "fish"]
        log("    ※フォールバック候補のFishは声ID未設定のため使いません（声が毎行変わるため）。")
    # この動画専用の読み仮名マップ（stage_scriptで自動抽出）をTTSへ渡す
    reading_map = util.load_json(proj.path("script/reading_map.json"), {}) or {}
    if reading_map:
        log(f"    読み仮名マップ {len(reading_map)}語を適用します。")
    try:  # 「辞書に登録したのに反映されない」の切り分け用に、適用の事実をログに残す
        from .providers.base import _user_fixes
        _un = len(_user_fixes())
        if _un:
            log(f"    自分の読み方辞書 {_un}語を最優先で適用します。")
    except Exception:
        pass
    # 主人公プロファイル（性別に合わせた声の自動切替。トグルOFF/判定不能なら従来動作）
    prot = _ensure_protagonist(proj, cfg, log, ctx)
    gender = str((prot or {}).get("gender") or "")
    vbg = cfg.get("voice_by_gender") or {}
    if isinstance(vbg, dict) and vbg.get("auto_enabled"):
        if gender in ("male", "female"):
            detail = "・".join(x for x in (prot.get("age_range"), prot.get("role")) if x)
            log(f"    主人公プロファイル: {'女性' if gender == 'female' else '男性'}"
                + (f"（{detail}）" if detail else ""))
        else:
            log("    主人公の性別は判定できませんでした → 設定の声をそのまま使います。")
    if (cfg.get("script_format") or "narration") == "dialog":  # 会話形式=話者別に声を切替
        for eng in engines:
            c = dict(cfg)
            c["tts_engine"] = eng
            c["_reading_map"] = reading_map
            c["_protagonist_gender"] = gender
            if eng != primary:
                log(f"    ♻ 音声エンジンを「{eng}」へ自動切替して作り直します。")
            if _stage_tts_dialog(proj, c, ctx, log):
                return True
        return False
    script = proj.path("script/script.txt").read_text(encoding="utf-8")
    cues = telop.split_into_cues(script, int(cfg.get("telop_max_chars", 24)),
                                 int(cfg.get("telop_min_chars", 8)))
    if not cues:
        log("    テロップ行が作れませんでした。")
        return False
    log(f"    テロップ行数: {len(cues)}")
    wav = str(proj.path("audio/voice.wav"))
    # 声はステージ単位で切替（quota切れ等でも、動画の途中で声が変わることはない）
    for pos, eng in enumerate(engines):
        nxt = engines[pos + 1] if pos + 1 < len(engines) else None
        c = dict(cfg)
        c["tts_engine"] = eng
        c["_reading_map"] = reading_map
        _apply_gender_voice(c, eng, gender, log)
        prov = providers.get_provider("tts", c, log, ctx)
        # 無音事故の防止: 実エンジンを選んだのに mock（=無音）へ落ちたら次の候補へ
        if eng != "mock" and getattr(prov, "engine", "") == "mock":
            if eng == primary:
                log(f"    {_ENGINE_LABEL.get(eng, eng)} は利用不可（キー/認証なし）→ 代替の声を探します…")
            continue
        try:
            method = c.get("align_method", "segments")
            if eng == "file":
                method = "whisper"  # 録音ファイルは行単位合成できない＝必ず強制アライン
            if eng == "gemini" and method != "whisper":
                # Geminiはチャンク合成＝文字数比の近似時刻しか出せず、テロップが大きくズレる
                # （2026-07-07 実走で確認）→ whisperで実際の発話時刻に精密アラインする
                log("    Gemini音声はテロップ精密同期のため whisper アラインを使います"
                    "（音声解析に数分・初回はモデルDLあり）。")
                method = "whisper"
            if method == "whisper":
                full = "\n".join(cues)
                # voice.wav は②のsentinel。先に本番パスへ書くと、アライン失敗でも
                # 「②完了」扱いになり、テロップ皆無の動画が♻再開で完成してしまう
                # → 一時wavに合成し、アライン成功後にだけ voice.wav へ昇格させる
                tmp_wav = str(proj.path("audio/_full_tmp.wav"))
                dur = prov.synthesize_full(full, c, tmp_wav, log)
                if dur is None:
                    log("    このTTSは全文合成に未対応 → 行単位合成に切替。")
                    timings = prov.synthesize(cues, c, wav, log)
                else:
                    from . import align
                    try:
                        words, wdur = align.transcribe_words(tmp_wav, log,
                                                             c.get("whisper_model", "medium"))
                        timings = align.align(cues, words, dur or wdur)
                        if not timings:
                            raise RuntimeError("whisperアラインの結果が空でした")
                        # 文字起こしの途切れ診断（テロップ後半停止の早期発見用）
                        cov = max((float(w.get("end") or 0) for w in words), default=0.0)
                        if (dur or wdur) and ((dur or wdur) - cov) > 30.0:
                            log(f"    ⚠ 文字起こしが途中で途切れました"
                                f"（{cov:.0f}秒/全体{(dur or wdur):.0f}秒）。"
                                "以降のテロップは文字数から自動配置しています"
                                "（多少ズレる可能性があります）。")
                    except Exception:
                        Path(tmp_wav).unlink(missing_ok=True)
                        raise
                    os.replace(tmp_wav, wav)
            else:
                timings = prov.synthesize(cues, c, wav, log)
        except Exception as e:
            _explain_engine_failure(eng, str(e), nxt, log, what="音声")
            continue
        if not timings or not proj.path("audio/voice.wav").exists():
            _explain_engine_failure(eng, "出力が空でした", nxt, log, what="音声")
            continue
        util.save_json_atomic(proj.path("audio/timings.json"), timings)
        total = timings[-1]["end"] if timings else 0
        log(f"    音声長: 約{total:.1f}秒（{eng}）")
        # 目安尺との実測突合（2026-08-17）: 大きくズレていたら気づけるよう警告だけ出す
        _tgt_min = int(cfg.get("video_length_min", 5) or 5)
        if _tgt_min > 0:
            _ratio = (total / 60.0) / _tgt_min
            if _ratio > 1.5 or _ratio < 0.5:
                log(f"    ⚠ 目安{_tgt_min}分に対し実測{total / 60.0:.1f}分です"
                    "（台本の長さ由来。気になる場合は⑤作り直しで台本からやり直せます）。")
        return True
    log("    【音声エラー】どの音声エンジンでも合成できませんでした"
        "（キー/認証/quotaを確認してください）。")
    proj.write_debug("tts_error.txt", "all tts engines failed or unavailable")
    return False


_SPEAKER_RE = re.compile(r"^(A|B|N|話者A|話者B|ナレーション|ナレ)\s*[|｜:：]\s*(.+)$")


def _parse_dialog_lines(script: str) -> list[tuple[str, str]]:
    """会話台本 →[(speaker, セリフ)]。speaker∈{A,B,N}。形式外の行はNに。"""
    out = []
    for raw in (script or "").splitlines():
        s = raw.strip()
        if not s or re.fullmatch(r"-{3,}", s):
            continue
        m = _SPEAKER_RE.match(s)
        if m:
            sp = m.group(1)
            sp = {"話者A": "A", "話者B": "B", "ナレーション": "N", "ナレ": "N"}.get(sp, sp)
            out.append((sp, m.group(2).strip()))
        else:
            out.append(("N", s))
    return [(sp, t) for sp, t in out if t]


def _stage_tts_dialog(proj: Project, cfg, ctx, log) -> bool:
    """会話形式: 話者A/B/Nごとに声を切り替えて合成し、1本のWAVに連結する。"""
    import wave

    script = proj.path("script/script.txt").read_text(encoding="utf-8")
    pairs = _parse_dialog_lines(script)
    if not pairs:
        log("    会話行が作れませんでした（台本形式を確認）。")
        return False
    max_chars = int(cfg.get("telop_max_chars", 24))
    # セリフ→テロップ行（話者を保持）
    cue_rows: list[tuple[str, str]] = []
    for sp, text in pairs:
        for cue in telop.split_into_cues(text, max_chars):
            cue_rows.append((sp, cue))
    # 連続する同一話者をまとめて1回の合成に
    runs: list[tuple[str, list[str]]] = []
    for sp, cue in cue_rows:
        if runs and runs[-1][0] == sp:
            runs[-1][1].append(cue)
        else:
            runs.append((sp, [cue]))
    log(f"    会話形式: セリフ{len(cue_rows)}行 / 話者切替{len(runs)}回（A/B/ナレ）")

    prov = providers.get_provider("tts", cfg, log, ctx)
    if cfg.get("tts_engine", "api") != "mock" and getattr(prov, "engine", "") == "mock":
        log("    【音声エラー】選んだ音声エンジンが使えません（無音になるため中止）。設定・キーを確認してください。")
        proj.write_debug("tts_error.txt", "selected TTS engine unavailable (dialog mode)")
        return False

    eng = cfg.get("tts_engine", "api")
    kind = {"api": "api", "gemini": "gemini", "edge": "edge"}.get(eng)
    dv = cfg.get("dialog_voices") or {}

    def _voiced_cfg(sp: str) -> dict:
        c = dict(cfg)
        if sp == "A":   # A=主人公。性別判定があれば声を自動切替（B/Nは従来のdialog_voices）
            _apply_gender_voice(c, eng, str(cfg.get("_protagonist_gender") or ""))
            return c
        if kind is None:
            return c  # 対応外エンジンは全員同じ声
        v = (dv.get({"B": "b", "N": "n"}[sp]) or {}).get(kind)
        if v:
            key = {"api": "tts_voice", "gemini": "gemini_tts_voice", "edge": "edge_voice"}[kind]
            c[key] = v
        return c

    sr = int(cfg.get("tts_sample_rate", 24000))
    gap = 0.15
    pcm_all = bytearray()
    timings: list[dict] = []
    tmp = proj.path("audio/_run.wav")
    t = 0.0
    silent_runs = 0  # レート制限等で無音が続いたら早期中止（無音動画を作らない）
    for i, (sp, cues) in enumerate(runs):
        if browser_ai._stopped():
            log("    ■ 中断要求により停止しました（②音声）。")
            return False
        # logは握りつぶさない（リトライ/失敗が見えないと固まって見える）
        part = prov.synthesize(cues, _voiced_cfg(sp), str(tmp), log=log)
        try:
            with wave.open(str(tmp), "rb") as wf:
                if wf.getframerate() != sr or wf.getnchannels() != 1:
                    log(f"    【エラー】音声フォーマット不一致（run{i}）")
                    return False
                body = wf.readframes(wf.getnframes())
        except Exception as e:
            log(f"    【エラー】合成結果を読めません: {e}")
            return False
        if eng != "mock":
            stride = max(2, (len(body) // 2000) // 2 * 2)
            if body and any(body[k] or body[k + 1] for k in range(0, len(body) - 1, stride)):
                silent_runs = 0
            else:
                silent_runs += 1
                if silent_runs >= 3:
                    log("    【音声エラー】無音が続いています（レート制限/quota切れの可能性大）。中止します。")
                    log("    → ②音声を「Edge（無料）」か「Google Cloud（高品質）」に切り替えて"
                        "「♻続きから」で②からやり直してください（エンジンは画面の設定が使われます）。")
                    proj.write_debug("tts_error.txt", "3 consecutive silent runs (dialog mode)")
                    return False
        for seg in part:
            timings.append({"text": seg["text"], "speaker": sp,
                            "start": round(t + seg["start"], 3),
                            "end": round(t + seg["end"], 3)})
        pcm_all += body + b"\x00\x00" * int(gap * sr)
        t += len(body) / (sr * 2) + gap
        log(f"      …{i + 1}/{len(runs)}（{ {'A':'話者A','B':'話者B','N':'ナレ'}[sp] }）")
    try:
        tmp.unlink()
    except Exception:
        pass
    from .providers.base import write_wav
    write_wav(bytes(pcm_all), proj.path("audio/voice.wav"), sr)
    util.save_json_atomic(proj.path("audio/timings.json"), timings)
    log(f"    音声長: 約{t:.1f}秒（3声の掛け合い）")
    return True


def stage_visuals(proj: Project, cfg, ctx, log, stop) -> bool:
    if cfg.get("video_mode") == "neko":  # 猫ミーム=素材クリップを感情タグで割当て
        from . import nekomeme
        return nekomeme.stage_visuals_neko(proj, cfg, log, ctx)  # ctx=Web系の背景AI生成用
    from . import storyboard
    meta = util.load_json(proj.path("script/meta.json"), {}) or {}
    try:
        script = proj.path("script/script.txt").read_text(encoding="utf-8")
    except Exception:
        script = ""
    # 主人公プロファイル: 全画像のプロンプトに同一人物の指定を挿入
    # （画像ごとに主人公の性別・人物像が変わる問題への対策。判定不能なら従来どおり）
    prot = _ensure_protagonist(proj, cfg, log, ctx)
    if (prot or {}).get("gender") in ("male", "female"):
        who = "woman" if prot["gender"] == "female" else "man"
        pos = "her" if who == "woman" else "his"
        line = f"Main character: a Japanese {who}"
        if prot.get("age_range"):
            line += f" in {pos} {prot['age_range']}"
        if prot.get("role"):
            line += f" ({prot['role']})"
        line += ". Depict the same main character consistently in every image."
        cfg = dict(cfg)
        cfg["_protagonist_line"] = line
        log(f"    画像の人物指定: 主人公={'女性' if who == 'woman' else '男性'}で統一します。")
    images: list[str] = []
    clips: list[str] = []
    vis_engine = cfg.get("visual_engine", "api")
    if vis_engine not in ("none", ""):
        n_img = max(1, int(cfg.get("num_images", 1)))
        # 絵コンテは一度作ったら保存して固定する（♻再開/自動リトライのたびに再生成すると
        # 「前半=旧絵コンテの画像・後半=新絵コンテ」の時系列崩れが起きる＋API再課金）
        scenes_path = proj.path("visuals/scenes.json")
        saved_scenes = util.load_json(scenes_path, None)
        if isinstance(saved_scenes, list) and len(saved_scenes) == n_img:
            meta["scenes"] = saved_scenes
            log("    絵コンテ: 保存済みを再利用します（visuals/scenes.json）。")
        else:
            meta["scenes"] = storyboard.build_scenes(script, n_img, cfg, log)  # 時系列N場面
            util.save_json_atomic(scenes_path, meta["scenes"])

        def _vgen(prov_, c_):
            r = prov_.generate(c_, meta, str(proj.path("visuals")), log)
            got = len(r.get("images") or []) + len(r.get("clips") or [])
            if got and got < max(1, (n_img + 1) // 2):
                # 大半が失敗＝このエンジンは失敗扱いにして次の候補へ回す
                # （出来た分の画像はディスクに残り、次エンジン/♻再開で再利用される）
                log(f"    ⚠ 画像が {got}/{n_img} 枚しか用意できませんでした → 失敗扱いにします。")
                return None
            return r if got else None

        res, used_v, _p = _run_with_fallback("visual", cfg, ctx, log, _vgen)
        if not res:
            log("    【画像エラー】どの画像エンジンでも生成できませんでした。")
            proj.write_debug("visuals_error.txt", "all visual engines failed or unavailable")
            res = {}
        images += [p for p in res.get("images", []) if p and Path(p).exists()]
        clips += [p for p in res.get("clips", []) if p and Path(p).exists()]
        if images and len(images) < n_img:
            log(f"    ⚠ 画像は {len(images)}/{n_img} 枚です（不足分は他の画像の表示時間で吸収されます）。")
    vid_engine = cfg.get("video_engine", "none")
    if vid_engine not in ("none", ""):
        # 動画クリップは「あれば嬉しい」任意機能。ここで例外を出すと画像ができていても
        # ③全体が失敗になるので、失敗しても画像だけで続行する
        n_clip = int(cfg.get("num_video_clips", 0) or 0)
        if n_clip <= 0:
            log("    動画クリップは「0本」の設定なので作りません（画像だけで作ります）。")
        else:
            try:
                vidp = providers.get_provider("video", cfg, log, ctx)
                r = vidp.generate(cfg, meta, str(proj.path("visuals")), log)
                clips += [p for p in r.get("clips", []) if p and Path(p).exists()]
            except Exception as e:
                log(f"    ⚠ 動画クリップの生成に失敗しました（{str(e)[:90]}）。"
                    "画像だけで続けます。")
                proj.write_debug("video_error.txt", traceback.format_exc())
    if not images and not clips and vis_engine in ("none", ""):
        # 「画像を作らない」＝自分で用意した素材を使う運用。visualsフォルダの中身を拾う。
        # 並びは番号を数値として見る自然順（辞書順だと 10.png が 2.png より前に来て、
        # さらに拡張子ごとに固まってスライド順が崩れる）。*_part は書きかけの中間物。
        def _nat(p: Path):
            return [int(t) if t.isdigit() else t.lower()
                    for t in re.split(r"(\d+)", p.name)]
        vd = proj.path("visuals")
        found = [p for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp")
                 for p in vd.glob(ext) if not p.stem.endswith("_part")]
        images += [str(p) for p in sorted(found, key=_nat)]
        clips += [str(p) for p in sorted(vd.glob("*.mp4"), key=_nat)]
        if images or clips:
            log(f"    「画像を作らない」設定のため、visualsフォルダに入っている素材"
                f"（画像{len(images)}枚・動画{len(clips)}本）をファイル名の番号順に使います。")
    if not images and not clips:
        if vis_engine in ("none", ""):
            log("    【画像なし】画像エンジンが「画像を作らない」で、visualsフォルダにも"
                "素材がありません。画像エンジンを選ぶか、visualsフォルダに画像(png/jpg)を"
                "入れてから、もう一度実行してください。"
                "（作り直しで退避された素材は _remake_backup フォルダの中にあります）")
        else:
            log("    画像・動画が生成されませんでした。")
        return False
    util.save_json_atomic(proj.path("visuals/manifest.json"),
                          {"images": images, "clips": clips,  # ← sentinel
                           "chapters": storyboard.build_chapters(script, len(images))})
    return True


def stage_compose(proj: Project, cfg, ctx, log, stop) -> bool:
    timings = util.load_json(proj.path("audio/timings.json"), []) or []
    manifest = util.load_json(proj.path("visuals/manifest.json"), {}) or {}
    # 猫ミーム合成（キー存在/モードで分岐: 空タイムラインでも通常ルートへ落とさず明示エラーにする）
    if "neko_timeline" in manifest or cfg.get("video_mode") == "neko":
        from . import nekomeme
        res = nekomeme.compose_neko(proj, cfg, log)
        if not res.get("ok"):
            log(f"    合成に失敗（rc={res.get('returncode')}）。ログ: {res.get('log')}")
            if res.get("log_tail"):
                proj.write_debug("compose_ffmpeg_tail.txt", res["log_tail"])
            return False
        _r = _compose.wrap_with_intro_outro(proj.path("compose/final.mp4"),
                                            cfg.get("intro_video", ""), cfg.get("outro_video", ""),
                                            cfg, log, stop)  # 失敗しても本編は完成扱い
        _log_oped_result(_r, cfg, log)
        return True
    # manifestに載っている画像・動画が実在するか（全滅なら真っ黒動画を作る前に止める。
    # プロジェクトを手で移動した／画像フォルダを消した等で起こる）
    man_imgs = [p for p in (manifest.get("images") or []) if p]
    man_clips = [p for p in (manifest.get("clips") or []) if p]
    if (man_imgs or man_clips) and not any(Path(p).exists() for p in man_imgs + man_clips):
        log("    【素材エラー】visuals/manifest.json に載っている画像・動画が1つも"
            "見つかりません（フォルダを移動/削除した可能性）。"
            "「作り直し」タブで③画像から作り直してください。")
        proj.write_debug("compose_error.txt",
                         "all manifest media missing:\n" + "\n".join(man_imgs + man_clips))
        return False
    ass_text = telop.build_ass(timings, cfg, log=log)
    ass_path = proj.path("compose/subs.ass")
    ass_path.write_text(ass_text, encoding="utf-8")
    bgm = _pick_bgm(cfg, log, proj)
    if bgm:  # BGMライブラリの曲別「音量(dB)」を合成へ引き渡す（全体設定より優先）
        for e in (cfg.get("bgm_library") or []):
            if e.get("path") == bgm and e.get("gain_db") is not None:
                cfg = dict(cfg)
                cfg["_bgm_gain_db"] = e.get("gain_db")
                log(f"    BGM音量: {e.get('gain_db')}dB（この曲の個別設定）")
                break
    res = _compose.compose(
        images=manifest.get("images", []), clips=manifest.get("clips", []),
        voice_wav=str(proj.path("audio/voice.wav")), ass_path=str(ass_path),
        out_mp4=str(proj.path("compose/final.mp4")),  # ← sentinel
        settings=cfg, bgm_path=bgm, log=log, stop=stop)
    if not res.get("ok"):
        log(f"    合成に失敗（rc={res.get('returncode')}）。ログ: {res.get('log')}")
        if res.get("log_tail"):
            proj.write_debug("compose_ffmpeg_tail.txt", res["log_tail"])
        return False
    _r = _compose.wrap_with_intro_outro(proj.path("compose/final.mp4"),
                                        cfg.get("intro_video", ""), cfg.get("outro_video", ""),
                                        cfg, log, stop)  # 失敗しても本編は完成扱い
    _log_oped_result(_r, cfg, log)
    log(f"    完成: {proj.path('compose/final.mp4')}  ({res.get('duration','?')}秒)")
    return True


def _log_oped_result(r: dict, cfg: dict, log) -> None:
    """OP/ED連結の結果を1行で明示する（連結失敗・パス不在が「成功」に紛れないように）。"""
    try:
        want = [l for l, k in (("OP", "intro_video"), ("ED", "outro_video"))
                if (cfg.get(k) or "").strip()]
        if not want:
            return
        if r.get("ok") and not r.get("skipped"):
            if r.get("missing"):
                log("    ⚠ " + "・".join(m["label"] for m in r["missing"])
                    + " は動画ファイルが見つからず付きませんでした（冒頭・終了タブでパスを確認）。")
            return
        if r.get("skipped"):
            log("    ⚠ 冒頭・終了（" + "/".join(want) + "）は付いていません"
                "（動画ファイルが見つかりません。冒頭・終了タブで「参照…」から選び直し→"
                "作り直しタブの「🎬 終了画面だけ付け直す」で付けられます）。")
        else:
            log("    ⚠ 冒頭・終了（" + "/".join(want) + "）の連結に失敗したため付いていません"
                "（作り直しタブの「🎬 終了画面だけ付け直す」で再試行できます）。")
    except Exception:
        pass


_CHAPTER_LINE_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?(\s.*)$")


def _shift_chapters(desc: str, delta: float) -> str:
    """概要欄のチャプター行（MM:SS 見出し／H:MM:SS 見出し）の時刻を delta 秒ずらす。
    先頭の 00:00 は YouTube仕様で固定（動かさない）。"""
    out = []
    for line in (desc or "").splitlines():
        m = _CHAPTER_LINE_RE.match(line)
        if not m:
            out.append(line)
            continue
        a, b, c, rest = m.groups()
        sec = (int(a) * 3600 + int(b) * 60 + int(c)) if c is not None else (int(a) * 60 + int(b))
        if sec <= 0:
            out.append(line)
            continue
        sec = max(0, int(round(sec + delta)))
        out.append(f"{_fmt_chapter_time(sec)}{rest}")
    return "\n".join(out)


def channel_oped_for(folder: str, cfg: dict) -> dict:
    """この動画のチャンネル（memo刻印）の**現在の**OP/ED設定を返す。
    チャンネルが特定できなければ画面（アクティブch）の値。"""
    base = Path(folder)
    memo = util.load_json(base / "memo.json", {}) or {}
    name = (memo.get("channel") or "").strip()
    rc = util.load_json(base / "run_cfg.json", {}) or {}
    # チャンネルが特定できない時は「この動画を作った時の」OP/ED（run_cfg）を初期値にする
    # （画面のアクティブchの値だと別チャンネルのEDが混ざる）。run_cfgにも無ければ画面の値
    src = rc if ("intro_video" in rc or "outro_video" in rc) else cfg
    out = {"channel": name or "", "intro_video": (src.get("intro_video") or "").strip(),
           "outro_video": (src.get("outro_video") or "").strip(), "from_channel": False,
           "from_run_cfg": src is rc}
    if name:
        for chd in (cfg.get("channels") or []):
            if isinstance(chd, dict) and chd.get("name") == name:
                out["intro_video"] = (chd.get("intro_video") or "").strip()
                out["outro_video"] = (chd.get("outro_video") or "").strip()
                out["from_channel"] = True
                break
    return out


def reattach_oped(folder: str, cfg: dict, log=print, stop=None,
                  assume_body: bool = False) -> dict:
    """完成動画のOP/EDだけ付け直す（④合成は行わない・本編はそのまま）。

    本編 = compose/final_body.mp4（連結時に残した控え）。控えが無い旧動画は
    oped.json が「付いていない」と言っている時だけ final.mp4 を本編とみなす。
    どちらも無い（この機能より前の動画）時は assume_body=True（ユーザー確認済み）の
    場合だけ final.mp4 を本編とみなす＝二重に終了画面が付く事故を防ぐ。
    """
    from . import compose as _compose
    base = Path(folder)
    final = base / "compose" / "final.mp4"
    body = base / "compose" / _compose.BODY_MP4
    if not final.exists():
        return {"ok": False, "error": "完成動画（compose/final.mp4）がありません。④合成まで終わった動画を選んでください。"}
    rc = util.load_json(base / "run_cfg.json", {}) or {}
    if int(rc.get("video_height", 1080)) > int(rc.get("video_width", 1920)):
        return {"ok": False, "error": "この動画は縦ショートです。縦動画には冒頭・終了画面を付けない仕様です。"}
    if is_neko_project(base, rc):
        # 猫ミームは同梱の終了画面が本編(final.mp4)に焼き込まれている＝本編＋EDの連結だと二重になる
        # → ④合成（AI不要・数分）をやり直してから連結する（敵対検証2026-08-21）
        return _reattach_oped_neko(base, cfg, rc, log, stop)
    rec = _compose.read_oped_record(final)
    if body.exists() and (rec is None or rec.get("attached")):
        src_body = body             # 控えがある＝連結済み（記録が欠けていても控えを本編とみなす）
    elif rec is not None and not rec.get("attached"):
        src_body = final            # 付いていない＝今の final.mp4 が本編そのもの
    elif rec is None and assume_body:
        src_body = final
    elif rec is None:
        return {"ok": False, "error": "old",
                "detail": "この動画は終了画面の控え（final_body.mp4）が無い旧版の動画です。"}
    else:
        return {"ok": False, "error": "本編の控え（final_body.mp4）が見つかりません（消された可能性）。"
                                      "作り直しタブで「④合成」に✔して作り直してください。"}
    sel = channel_oped_for(folder, cfg)
    intro, outro = sel["intro_video"], sel["outro_video"]
    src_lab = (f"チャンネル「{sel['channel']}」の" if sel["from_channel"]
               else ("この動画を作った時の" if sel.get("from_run_cfg") else "画面の"))
    log(f"    {src_lab}設定: OP={intro or '（なし）'} / ED={outro or '（なし）'}")
    # 本編が final.mp4 自身なら、まず控えへ移して保全（失敗しても final は残る）
    if src_body == final:
        try:
            import shutil as _sh
            _sh.copyfile(final, body)
            src_body = body
        except Exception as e:
            return {"ok": False, "error": f"本編の控えを作れませんでした: {e}"}
    settings = dict(cfg)
    for k in ("video_width", "video_height", "video_fps", "enc_quality"):
        if k in rc:
            settings[k] = rc[k]
    if not ("video_width" in rc and "video_height" in rc):
        # run_cfg の無い動画は本編の実寸に合わせる（画面が縦表示中でも横動画を縦に作り替えない）
        pr = util.probe_ok(src_body)
        try:
            if pr.get("width") and pr.get("height"):
                settings["video_width"], settings["video_height"] = int(pr["width"]), int(pr["height"])
        except Exception:
            pass
    if not (intro or outro):
        # 両方空＝本編だけにする（付いていた終了画面を外す）
        try:
            import shutil as _sh
            tmp = final.with_name("final_oped_wip.mp4")
            _sh.copyfile(src_body, tmp)
            import os as _os
            _os.replace(tmp, final)
        except Exception as e:
            return {"ok": False, "error": f"本編だけに戻せませんでした: {e}"}
        _compose.write_oped_record(final, {"attached": False, "intro": "", "outro": "",
                                           "missing": [], "body": _compose.BODY_MP4, "reason": "none"})
        log("    OP/EDの設定が両方空のため、本編だけの動画にしました。")
        r = {"ok": True, "detached": True}
    else:
        try:
            r = _compose.wrap_with_intro_outro(final, intro, outro, settings, log, stop, body=str(src_body))
        except PermissionError as e:
            return {"ok": False, "error": "完成動画（final.mp4）が他のアプリで開かれているため書き換えられません。"
                                          "動画プレイヤーやエクスプローラのプレビューを閉じてから、もう一度お試しください。"
                                          f"（{str(e)[:60]}）"}
        except Exception as e:
            return {"ok": False, "error": f"連結中にエラー: {str(e)[:120]}"}
        if not r.get("ok"):
            return {"ok": False, "error": str(r.get("error") or "冒頭・終了の連結に失敗しました（compose/ffmpeg_oped.log を確認）。")
                                          + " 元の動画はそのまま残っています。"}
        # OPの長さが変わると概要欄のチャプター時刻（⑤で計算）がずれる → 差分だけ直す
        try:
            old_dur = float((rec or {}).get("intro_dur") or 0.0)
            new_dur = float(_compose.media_duration(intro)) if intro else 0.0
            delta = new_dur - old_dur
            if abs(delta) > 0.5:
                memo = util.load_json(base / "memo.json", {}) or {}
                desc = memo.get("description") or ""
                new_desc = _shift_chapters(desc, delta)
                if new_desc != desc:
                    memo["description"] = new_desc
                    util.save_json_atomic(base / "memo.json", memo)
                    log(f"    OPの長さが変わったため（{old_dur:.1f}秒→{new_dur:.1f}秒）、概要欄のチャプター時刻を"
                        f"{delta:+.0f}秒ずらしました（投稿タブで確認できます）。")
                else:
                    log(f"    OPの長さが変わりました（{old_dur:.1f}秒→{new_dur:.1f}秒）。概要欄に時刻を手で"
                        "書いている場合はずれるのでご確認ください。")
        except Exception:
            pass
        if r.get("skipped"):
            return {"ok": False, "error": "OP/EDの動画ファイルが見つかりません（冒頭・終了タブで「参照…」から選び直してください）。"
                                          + "".join(f"\n  {m['label']}: {m['path']}" for m in r.get("missing") or [])}
    _finish_reattach(base, rc, intro, outro, log)
    r["thumb_unchanged"] = True
    return r


def is_neko_project(base, rc: dict | None = None) -> bool:
    """この動画フォルダが猫ミームか（run_cfg の video_mode、無ければ manifest の neko_timeline）。"""
    base = Path(base)
    rc = rc if rc is not None else (util.load_json(base / "run_cfg.json", {}) or {})
    if (rc.get("video_mode") or "") == "neko":
        return True
    try:
        man = util.load_json(base / "visuals" / "manifest.json", {}) or {}
        return "neko_timeline" in man
    except Exception:
        return False


def _finish_reattach(base, rc: dict, intro: str, outro: str, log) -> None:
    """付け直し後の共通処理: run_cfg にOP/EDを書き戻し、投稿済み記録を _prev へ改名。"""
    # run_cfg にも今回のOP/EDを残す（次の作り直しでも同じものが付くように）
    try:
        rc["intro_video"], rc["outro_video"] = intro, outro
        util.save_json_atomic(base / "run_cfg.json", rc)
    except Exception:
        pass
    # 動画の中身が変わった＝投稿済み記録は「消さずに改名」（二重公開の防止・clear_stagesと同じ）
    try:
        for name, newname in (("uploaded.flag", "uploaded_prev.flag"),
                              ("studio_pending.flag", "studio_pending_prev.flag")):
            f = base / name
            if f.exists():
                old = base / newname
                if old.exists():
                    i = 2
                    while (base / newname.replace(".flag", f"{i}.flag")).exists():
                        i += 1
                    old.replace(base / newname.replace(".flag", f"{i}.flag"))
                f.replace(base / newname)
                log(f"    ⚠ この動画は投稿済みでした。記録を {newname} に残しました"
                    "（YouTube上の旧動画は自動では消えません）。")
    except Exception:
        pass


def _reattach_oped_neko(base, cfg: dict, rc: dict, log, stop) -> dict:
    """猫ミームの「終了画面だけ付け直す」＝④合成をやり直し（この動画の条件のまま・AI不要）→OP/ED連結。
    チャンネルのEDがあれば同梱の終了画面は付かず、無ければ同梱の終了画面が付く（compose_neko側で判定）。"""
    from . import nekomeme
    from .project import Project
    proj = Project(base)
    if not proj.path("visuals/manifest.json").exists():
        return {"ok": False, "error": "この猫ミーム動画には③の素材情報（visuals/manifest.json）が無いため付け直せません。"
                                      "作り直しタブで「③ 画像・映像」に✔して作り直してください。"}
    sel = channel_oped_for(str(base), cfg)
    intro, outro = sel["intro_video"], sel["outro_video"]
    src_lab = (f"チャンネル「{sel['channel']}」の" if sel["from_channel"]
               else ("この動画を作った時の" if sel.get("from_run_cfg") else "画面の"))
    log(f"    猫ミームのため④合成をやり直して付け直します。{src_lab}設定: "
        f"OP={intro or '（なし）'} / ED={outro or '（なし・同梱の終了画面を使用）'}")
    # この動画の条件（素材フォルダ・音量・サイズ等）は作成時のまま。OP/EDだけ今の設定
    c = dict(cfg)
    for k, v in rc.items():
        if v is None or k in ("intro_video", "outro_video"):
            continue
        c[k] = v
    nd = (c.get("nekomeme_dir") or "").strip()
    if not (nd and Path(nd).exists()):
        c["nekomeme_dir"] = cfg.get("nekomeme_dir", "")   # 作成時の素材フォルダが無ければ今の設定
    c["intro_video"], c["outro_video"] = intro, outro
    try:
        res = nekomeme.compose_neko(proj, c, log)
    except Exception as e:
        return {"ok": False, "error": f"④合成でエラー: {str(e)[:120]}"}
    if not res.get("ok"):
        return {"ok": False, "error": f"④合成に失敗しました（rc={res.get('returncode')}）。元の動画はそのまま残っています。"}
    final = proj.path("compose/final.mp4")
    try:
        r = _compose.wrap_with_intro_outro(final, intro, outro, c, log, stop)
    except PermissionError as e:
        return {"ok": False, "error": "完成動画（final.mp4）が他のアプリで開かれているため書き換えられません。"
                                      f"閉じてからもう一度お試しください（{str(e)[:60]}）"}
    _log_oped_result(r, c, log)
    if not r.get("ok") and not r.get("skipped"):
        return {"ok": False, "error": "冒頭・終了の連結に失敗しました（compose/ffmpeg_oped.log を確認）。"}
    if r.get("skipped") and (intro or outro):
        return {"ok": False, "error": "OP/EDの動画ファイルが見つかりません（冒頭・終了タブで「参照…」から選び直してください）。"
                                      + "".join(f"\n  {m['label']}: {m['path']}" for m in r.get("missing") or [])}
    _finish_reattach(base, rc, intro, outro, log)
    return {"ok": True, "neko": True, "thumb_unchanged": True}


_CH_NORM_RE = re.compile(r"[「」『』()（）。、！!？?…:：|｜\s]")


def _spoken_script(script: str) -> str:
    """台本から話者ラベル・タグを外し「実際に読み上げられる文」だけにする。

    timings.jsonのtextはラベル除去済み（telop/会話パーサ/猫パーサ）なのに、
    生のscript.txtで章キーを作ると照合が全滅し目次が無言で消える
    （会話「A|セリフ」・猫「話者|タグ|セリフ」・「私：」劇スタイル＝2026-08-05敵対検証）。"""
    out = []
    for ln in (script or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        if "|" in s or "｜" in s:   # 会話/猫ミーム: 最後のフィールドがセリフ
            s = re.split(r"[|｜]", s)[-1].strip()
        else:                        # 「私：」「部長：」等の行頭ラベル
            s = re.sub(r"^[一-龥々ぁ-んァ-ヶーA-Za-z0-9Ａ-Ｚａ-ｚ０-９]{1,7}[:：]\s*", "", s)
        if s:
            out.append(s)
    return "\n".join(out)


def _chapter_time(sent: str, timings: list, start: int = 0,
                  expected: float | None = None) -> tuple[float, int]:
    """章の先頭文が実際に読まれる時刻を timings から引く。(秒, 次の探索開始index)。

    cueは文を句読点で割った断片＝文の先頭cueは文の先頭と一致する。正規化した先頭8字で
    照合し、同じ決め台詞が繰り返される台本では**期待位置に最も近い出現**を選ぶ
    （最初の出現に貪欲マッチすると章が数分手前に刻まれる＝2026-08-05敵対検証）。
    「はい」等の短すぎるキー/cueは誤マッチ源なので照合に使わない。"""
    key = _CH_NORM_RE.sub("", sent or "")[:8]
    if len(key) < 3:
        return -1.0, start
    hits = []
    for j in range(max(0, start), len(timings)):
        c = _CH_NORM_RE.sub("", str(timings[j].get("text") or ""))[:8]
        if not c:
            continue
        if c == key or (len(key) >= 4 and c.startswith(key)) \
                or (len(c) >= 4 and key.startswith(c)):
            hits.append(j)
            if expected is None:
                break
    if not hits:
        return -1.0, start
    if expected is not None and len(hits) > 1:
        j = min(hits, key=lambda x: abs(float(timings[x].get("start") or 0) - expected))
    else:
        j = hits[0]
    return float(timings[j].get("start") or 0), j + 1


def _fmt_chapter_time(sec: int) -> str:
    if sec >= 3600:   # 1時間超はYouTube仕様どおり H:MM:SS
        return f"{sec // 3600}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"
    return f"{sec // 60:02d}:{sec % 60:02d}"


_CHAPTER_AI_PROMPT = (
    "次のナレーション台本を{n}個の章に分け、視聴者向けの目次を作ってください。\n"
    "出力は次のJSON配列だけ（コードブロック・説明文は書かない）:\n"
    '[{{"title":"章タイトル（14字以内・内容が分かる言い切りの名詞句）",'
    '"starts_with":"その章が始まる台本の文の先頭15字（台本から一字一句そのまま）"}}]\n'
    "章は台本の順番どおり・1章目は台本の最初の文から始めること。\n\n")


def _build_chapters(proj: Project, cfg, log=None, ctx=None) -> str:
    """概要欄チャプター（00:00 見出し…）。desc_chapters=Trueの時だけ作る（既定OFF）。

    2026-08-05配布先報告での作り直し:
    ・従来は必ず挿入され、止める設定が無かった → 既定OFF・設定でON
    ・時刻が「画像の切替グリッド（総尺÷枚数）」で実際の話の区切りと最大68秒ズレていた
      → 章の先頭文を audio/timings.json の実発話時刻と突き合わせる
    ・見出しが「区間の先頭文の頭12字」の断片だった → AIで章タイトルを生成
      （AI不可時は従来の文頭方式で続行）。章数は5〜8個（画像枚数に連動させない）。
    YouTube仕様（00:00必須/3本以上/各10秒以上）を満たせない時は空文字（=載せない）。"""
    log = log or (lambda *_a: None)
    if not cfg.get("desc_chapters", False):
        return ""
    timings = util.load_json(proj.path("audio/timings.json"), []) or []
    try:
        script = proj.path("script/script.txt").read_text(encoding="utf-8")
    except Exception:
        script = ""
    if not timings or not script.strip():
        return ""
    total = float(timings[-1].get("end", 0) or 0)
    n = min(8, max(3, int(total // 150)))   # 約2.5分に1章・5〜8個目安
    if total < 60:
        return ""
    spoken = _spoken_script(script)   # ラベル除去済み台本＝timingsのtextと同じ土俵
    op_off = 0.0  # OP動画を先頭に連結する場合、チャプターはそのぶん後ろへずれる
    iv = (cfg.get("intro_video") or "").strip()
    if iv and Path(iv).exists():
        op_off = _compose.media_duration(iv)

    def _resolve(pairs) -> list[str]:
        """(見出し, 先頭文)ペア → 実時刻に解決した「MM:SS 見出し」行。"""
        lines, prev, idx = [], -10.0, 0
        for i, (title, sent) in enumerate(pairs):
            if i == 0:
                sec = 0  # YouTube仕様: 先頭は必ず00:00
                idx = 1
            else:
                expected = total * i / max(1, len(pairs))
                t, idx = _chapter_time(sent, timings, idx, expected)
                if t < 0:
                    continue   # 台本に無い文（AIの創作等）は載せない
                sec = int(op_off + t)
            if sec - prev < 10:   # 各章10秒以上（YouTube仕様）
                continue
            prev = sec
            lines.append(f"{_fmt_chapter_time(sec)} {title}")
        return lines

    # 見出し: AI（章タイトル＋開始文）→ **時刻解決まで含めて**失敗したら決定論（文頭12字）
    # （AIがJSONを返しても starts_with が創作文/順不同だと解決に失敗する。
    #   その時に目次ごと消さず、必ず決定論方式で作り直す＝2026-08-05敵対検証）
    lines: list[str] = []
    if cfg.get("script_engine") != "mock":
        try:
            text = _oneshot_text(dict(cfg, _web_timeout=120),
                                 "あなたはYouTube動画の構成が得意な編集者です。",
                                 _CHAPTER_AI_PROMPT.format(n=n) + spoken[:6000],
                                 log=log, ctx=ctx)
            m = re.search(r"\[.*\]", text or "", re.S)
            if m:
                import json as _j
                pairs = []
                for d in _j.loads(m.group(0)):
                    t = str(d.get("title") or "").strip()[:24]
                    s = str(d.get("starts_with") or "").strip()
                    if t and s:
                        pairs.append((t, s))
                if len(pairs) >= 3:
                    lines = _resolve(pairs)
        except Exception:
            lines = []
    if len(lines) < 3:
        from . import storyboard
        lines = _resolve(storyboard.build_chapter_pairs(spoken, n))
    if len(lines) < 3:
        return ""
    log(f"    概要欄の目次: {len(lines)}章（音声の実時刻に同期）")
    return "\n".join(lines)


def stage_project(proj: Project, cfg, ctx, log, stop) -> bool:
    meta = util.load_json(proj.path("script/meta.json"), {}) or {}
    thumb = proj.path("thumb/thumbnail.png")
    if int(cfg.get("video_height", 1080)) > int(cfg.get("video_width", 1920)):
        # 縦ショート: YouTubeショートはカスタムサムネ非対応（2026-08-10配布先報告）。
        # 16:9固定のサムネを作っても使われないため、無言でなくログを出してスキップ
        if cfg.get("thumb_engine", "api") not in ("none", ""):
            log("    縦ショートのため、サムネイルは作りません"
                "（YouTubeショートはカスタムサムネイル非対応）。")
    elif cfg.get("thumb_engine", "api") not in ("none", ""):
        try:
            # 「世界観プロンプトが効いていない」系の切り分け用に、サムネ生成へ渡る
            # 指示の出どころを記録（配布先からログだけで判断できるように）
            proj.write_debug("thumb_prompt.txt",
                             f"thumb_engine={cfg.get('thumb_engine')}\n"
                             f"thumbnail_prompt={cfg.get('thumbnail_prompt', '')!r}\n"
                             f"topic={cfg.get('topic', '')!r}\n"
                             f"title={meta.get('title', '')!r}")
            tprov = providers.get_provider("thumb", cfg, log, ctx)
            # mock代替の検出（①②③と同じ防御）: 紺色ダミーがサムネとして投稿される事故防止
            if getattr(tprov, "engine", "") == "mock" and cfg.get("thumb_engine") != "mock":
                log("    ⚠ サムネ用エンジンが使えない（キー/ログイン無し）ため、サムネは作りません"
                    "（あとで投稿タブの🖼作り直しで作成できます）。")
            elif tprov.generate(cfg, meta, str(thumb), log) and thumb.exists():
                from . import thumb_text
                # 文字を焼く前の背景を別名で保存（投稿タブで文字だけ入れ直せるように）
                try:
                    import shutil as _sh
                    _sh.copyfile(thumb, proj.path("thumb/thumbnail_bg.png"))
                except Exception:
                    pass
                copy = (meta.get("thumb_text") or meta.get("title", ""))[:40]
                thumb_text.draw_thumb_text(thumb, copy, cfg, log)
        except Exception as e:
            if cfg.get("_remake"):
                # ⑤だけの作り直しでサムネが作れなかったのに「できました」と
                # 出すと、「作り直したのに変わっていない」報告になる
                log(f"    ⚠ サムネを作り直せませんでした（{e}）。"
                    "サムネのエンジンとログを確認してください。")
            else:
                log(f"    サムネ生成は省略（{e}）")
    publish_at = _next_publish_at(cfg)
    title = (cfg.get("title_prefix") or "") + meta.get("title", "")
    # 概要欄 = 本文 + チャプター + チャンネル定型フッター + ハッシュタグ3個（5000字以内）
    desc_parts = [meta.get("description", "").strip()]
    ch_txt = _build_chapters(proj, cfg, log, ctx)
    if ch_txt:
        desc_parts.append(ch_txt)
    footer = (cfg.get("desc_footer") or "").strip()
    if footer:
        desc_parts.append(footer)
    tags = list(dict.fromkeys(
        list(meta.get("tags", []))
        + [t.strip() for t in (cfg.get("fixed_tags") or "").split(",") if t.strip()]))
    if tags:
        desc_parts.append(" ".join("#" + t.replace(" ", "") for t in tags[:3]))
    description = "\n\n".join(p for p in desc_parts if p)[:4900]
    memo = {"title": title, "description": description,
            "tags": tags,
            "title_candidates": meta.get("title_candidates", []),
            "channel": cfg.get("_channel_name", ""),
            "channel_url": cfg.get("youtube_channel_url", ""),
            "thumbnail": str(thumb) if thumb.exists() else "",
            "video": str(proj.path("compose/final.mp4")),
            "scheduled_publish_at": publish_at,
            "privacy": cfg.get("youtube_privacy", "private"),
            "category_id": cfg.get("youtube_category_id", "22")}
    # 作り直し時は、投稿タブでの手編集と投稿先の刻印を引き継ぐ（新しく作った物だけ更新）。
    # これが無いと⑤を作り直すたびに、編集したタイトル/概要/タグが消え、予約時刻が
    # 次の空き枠へ後ろ倒しになり、チャンネル刻印が「今開いているch」に化ける（=誤爆投稿）。
    keep = cfg.get("_keep_memo") or {}
    if keep:
        for k in ("scheduled_publish_at", "privacy", "category_id",
                  "channel", "channel_url", "prev_video_id"):
            if keep.get(k):
                memo[k] = keep[k]
        if cfg.get("_keep_memo_texts"):   # 台本を作り直していない＝題名/概要/タグも維持
            for k in ("title", "description", "tags", "title_candidates"):
                if keep.get(k):
                    memo[k] = keep[k]
        log("    ♻ 前回のタイトル・投稿先・予約時刻を引き継ぎました。")
    util.save_json_atomic(proj.path("memo.json"), memo)  # ← sentinel
    log(f"    メモ保存: {proj.path('memo.json')}")
    return True


def stage_upload(proj: Project, cfg, ctx, log, stop, sink=None) -> bool:
    """sink（dict）を渡すと、providerの結果（ok/manual/note/error/video_id等）を書き戻す。
    upload_project がそれを見て「予約完了／要ブラウザ操作／失敗」を正直に区別する。"""
    def _sink(**kw):
        if sink is not None:
            sink.update(kw)
    engine = cfg.get("upload_engine", "none")
    if engine in ("none", ""):
        log("    ⑥投稿はスキップ設定（upload_engine=none）。完成動画はフォルダ内 final.mp4。")
        _sink(skipped=True)
        return True
    if stop():
        log("    ⏹ 中断要求により、この動画の投稿は始めません。")
        _sink(stopped=True)
        return False
    memo = util.load_json(proj.path("memo.json"), {}) or {}
    # memoの絶対パスはフォルダ移動/リネームで古くなる → 実在しなければ現フォルダ基準に直す
    video = memo.get("video") or ""
    if not video or not Path(video).exists():
        video = str(proj.path("compose/final.mp4"))
    thumb = memo.get("thumbnail") or ""
    if thumb and not Path(thumb).exists():
        t2 = proj.path("thumb/thumbnail.png")
        thumb = str(t2) if t2.exists() else ""
    # 縦ショートの保険: サムネ生成済みの旧プロジェクトでも添付しない（動画自体の向きは
    # run_cfg で判定＝投稿タブは画面のcfgを使うため、動画と画面設定の食い違いに耐える）
    if thumb:
        rc = proj.load_run_cfg() or {}
        if int(rc.get("video_height", 1080)) > int(rc.get("video_width", 1920)):
            log("    縦ショートのため、サムネイルは添付しません（ショートは非対応）。")
            thumb = ""
    prov = providers.get_provider("upload", cfg, log, ctx)
    res = prov.schedule_upload(video, memo, thumb or None, cfg, log)
    _sink(**res)
    if res.get("stopped"):
        log("    ⏹ 投稿を中断しました（uploaded.flag は書きません。もう一度🚀で最初からやり直せます）。")
        return False
    _append_upload_csv(proj, memo, res)
    if res.get("dry_run"):
        log("    [dry-run] 実投稿はしていません（uploaded.flag は書きません）。")
        return True
    if res.get("manual"):
        # ブラウザ投稿の半自動: 自動入力まで完了、最終公開は人が押す。
        # studio_pending.flag を残す＝「まとめて投稿」で再添付されない＆一覧に⏳表示。
        # （uploaded.flag はまだ書かない＝“完了”とは言わない＝黙って成功扱いにしない）
        proj.path("studio_pending.flag").write_text(
            f"studio_handed@{memo.get('scheduled_publish_at','')}\n", encoding="utf-8")
        log(f"    ▶ {res.get('note') or 'ブラウザで続きを操作してください。'}")
        return True
    if res.get("ok"):
        flag = f"scheduled@{res.get('publish_at','')} video_id={res.get('video_id','')}\n"
        proj.path("uploaded.flag").write_text(flag, encoding="utf-8")  # ← sentinel
        try:  # 予約完了したら仮フラグは掃除
            proj.path("studio_pending.flag").unlink()
        except Exception:
            pass
        log(f"    予約投稿 完了。{flag.strip()}")
        return True
    if res.get("clicked_unverified"):
        # 確定ボタンは押した後に結果を確認できなかった＝予約が実際に成立している可能性が高い。
        # 何も残さないと一覧で「未投稿」に見え、次のまとめて投稿/🚀が無警告で再投稿して
        # 二重予約になる（敵対検証2026-08-07）→ pendingフラグを残して再投稿ガードに乗せる
        proj.path("studio_pending.flag").write_text(
            f"clicked_unverified@{memo.get('scheduled_publish_at','')} "
            "確定押下済み・結果未確認。Studioの動画一覧で予約/公開済みかを確認してください。\n",
            encoding="utf-8")
        log(f"    投稿は未確認（{res.get('error')}）")
        return False
    log(f"    投稿は未完了（{res.get('error') or res.get('note')}）。")
    return False


STAGE_FN = {
    "script": stage_script, "tts": stage_tts, "visuals": stage_visuals,
    "compose": stage_compose, "project": stage_project, "upload": stage_upload,
}


# ── 補助 ────────────────────────────────────────────────────────────────
def _pick_bgm(cfg, log, proj=None):
    mode = cfg.get("bgm_mode", "none")
    lib = cfg.get("bgm_library", []) or []
    if mode == "auto" and proj is not None:  # 動画に合わせて自動作成（ジャンル一致は再利用）
        from . import music, util as _u
        meta = _u.load_json(proj.path("script/meta.json"), {}) or {}
        try:
            script = proj.path("script/script.txt").read_text(encoding="utf-8")
        except Exception:
            script = ""
        return music.ensure_auto_bgm(cfg, meta.get("title", ""), script, log)
    if mode in ("none", "auto") or not lib:
        return None
    if mode == "select":  # 明示選択は（チャンネル問わず）そのまま尊重
        sel = cfg.get("bgm_selected", "")
        for item in lib:
            if item.get("path") == sel and Path(sel).exists():
                log(f"    BGM: {item.get('title', sel)}")
                return sel
        # 無言でBGM無し合成にしない（ファイル移動/削除後の再合成で気づけるように）
        log(f"    ⚠ 選択したBGMが見つからないため、BGMなしで合成します"
            f"（{sel or '未選択'}）。")
        return None
    if mode == "random":  # このチャンネルの曲からランダム（動画ごとに変わる・♻再開では同じ曲）
        from . import music
        pool = music.bgm_for_channel([e for e in lib if Path(e.get("path", "")).exists()],
                                     cfg.get("active_channel", 0))
        if pool:
            import random as _rnd
            if cfg.get("_bgm_reshuffle"):   # 作り直しの「おまかせ（再抽選）」は毎回引き直す
                item = _rnd.choice(pool)
            else:
                # 旧実装は pool[0] 固定＝「ランダム」なのに毎回同じ曲（＝一覧先頭）だった
                # （2026-08-25配布先報告）。動画フォルダ名を種にする＝動画ごとに変わり、
                # ♻再開・作り直しでは同じ曲（途中でBGMが変わらない）
                seed = proj.base.name if proj is not None else ""
                item = _rnd.Random(seed).choice(pool) if seed else _rnd.choice(pool)
            log(f"    BGM: {item.get('title')}（このチャンネルの{len(pool)}曲からランダム）")
            return item["path"]
    return None


def _next_publish_at(cfg) -> str:
    """空いている予約枠を自動割当（既予約と衝突しない・1日上限・日跨ぎ巡回）。"""
    try:
        from . import schedule
        out_dir = cfg.get("output_dir") or str(Path.cwd() / "output")
        slots = schedule.next_free_slots(cfg, out_dir, n=1)
        return slots[0] if slots else ""
    except Exception:
        return ""


def _append_upload_csv(proj: Project, memo: dict, res: dict) -> None:
    try:
        path = proj.base / "予約投稿記録.csv"
        new = not path.exists()
        with open(path, "a", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["記録日時", "状態", "予約日時", "videoId", "タイトル", "備考"])
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            state = "予約完了" if res.get("ok") and not res.get("dry_run") else \
                    ("dry-run" if res.get("dry_run") else "未完了/手動")
            w.writerow([now, state, res.get("publish_at", ""), res.get("video_id", ""),
                        memo.get("title", ""), res.get("error") or res.get("note") or ""])
    except Exception:
        pass


# ── エントリポイント ────────────────────────────────────────────────────
# 再開時にrun_cfgから引き継がないキー（=今の画面の設定が勝つ）。
# エンジン/声は「失敗したから切り替えて再開」が定番の復旧手順なので固定しない。
_ENGINE_KEYS = ("script_engine", "brushup_engine", "tts_engine", "visual_engine",
                "video_engine", "thumb_engine", "upload_engine",
                "tts_voice", "dialog_voices", "voice_by_gender")


# 「作り直し」で画面の設定を勝たせるキー（見た目・分量の設定）。
# ♻続きから（中断の再開）では従来どおり run_cfg を優先する＝挙動を変えない。
# 作り直しで「今の画面の設定」を勝たせるキー＝見た目・分量だけ。
# ※世界観プロンプト・OP/ED・タイトル接頭辞・固定タグは**チャンネル固有**なので入れない。
#   作り直しタブにはチャンネル選択が無く、入れると「今アクティブな別チャンネル」の
#   世界観で作り直され、さらに run_cfg.json へ書き戻されて元動画の設定まで壊れる。
_REMAKE_OVERRIDE_KEYS = (
    "telop_preset", "telop_font", "telop_fontsize", "telop_max_chars",
    "telop_primary_color", "telop_outline_color", "telop_outline", "telop_shadow",
    "telop_border_style", "telop_back_color",
    "bgm_mode", "bgm_selected", "num_images", "num_video_clips",
    "neko_bgm_db", "neko_meme_db", "neko_se_db", "neko_break_sec",
)


# チャンネルに属する内容系キー（作り直し時は「この動画のチャンネルの現在値」を優先）
_CH_CONTENT_KEYS = ("script_prompt", "topic_prompt", "brushup_prompt",
                    "visual_prompt_template", "thumbnail_prompt", "title_prefix",
                    "desc_footer", "fixed_tags", "gemini_tts_style",
                    "intro_text", "outro_text", "outro_text_shorts",
                    # OP/ED動画も「今の設定」を使う（2026-08-20配布先報告「終了画面を設定し直しても
                    # 作り直しに入らない」＝run_cfgの作成時パスで固定されていた）
                    "intro_video", "outro_video")


def _overlay_video_channel(cfg: dict) -> list[str]:
    """作り直し用: この動画のチャンネルの**現在の**内容系設定をcfgへ適用する。

    2026-08-03報告「台本作りの方針やお題生成への指示を書き換えても反映されない」への対応。
    07-27の混線対策で内容系を作成時のrun_cfg固定にした結果、文章・世界観タブの
    編集が作り直しに一切効かなくなっていた。**この動画のチャンネル名**（memo刻印）で
    channelsから現在値を引く＝編集は反映され、別チャンネルの世界観も混ざらない。
    お題(topic)は動画固有なのでrun_cfgのまま。チャンネルが特定できない/改名済みなら
    何もしない（＝従来どおりrun_cfgの値）。戻り値=適用したキー（ログ用）。"""
    name = ((cfg.get("_keep_memo") or {}).get("channel") or "").strip()
    if not name:
        return []
    for chd in (cfg.get("channels") or []):
        if isinstance(chd, dict) and chd.get("name") == name:
            applied = []
            for k in _CH_CONTENT_KEYS:
                v = chd.get(k)
                if k in ("intro_video", "outro_video"):
                    # OP/EDは「空＝外す」も意思（チャンネルで消したのに古いEDが付き続けない）
                    v = (v or "") if isinstance(v, str) or v is None else ""
                    if cfg.get(k, "") != v:
                        cfg[k] = v
                        applied.append(k)
                    continue
                if isinstance(v, str) and v.strip() and cfg.get(k) != v:
                    cfg[k] = v
                    applied.append(k)
            return applied
    return []


def _merge_resume_cfg(cfg: dict, rc: dict, overrides=()) -> dict:
    """再開時の設定合成。run_cfg（元動画の条件）を基本にしつつ、
    overrides に挙げたキーだけは「今の画面の設定」を勝たせる（作り直し用）。

    ※run_cfg側の**空文字**は採用しない（2026-08-03）。空欄時代に作られた動画を
    作り直すと、空文字が画面のプロンプトを潰し、サムネが英語の汎用スタイル
    （DEFAULT_THUMB_STYLE）で生成される＝「世界観プロンプトが効かない」の真因だった。"""
    skip = set(_ENGINE_KEYS) | set(overrides or ())
    merged = dict(cfg)
    for k, v in rc.items():
        if v is None or k in skip:
            continue
        if isinstance(v, str) and not v.strip() and str(merged.get(k) or "").strip():
            continue   # 空のrun_cfg値で、画面の有効値を潰さない
        merged[k] = v
    return merged


def run(cfg: dict, log=print, stop_flag=lambda: False, resume_dir: str | None = None,
        checkpoint=None) -> str:
    """checkpoint(stage, proj) -> bool: require_checkpoint対象ステージ後に呼ぶ。Falseで停止。"""
    out_dir = cfg.get("output_dir") or str(Path.cwd() / "output")
    resume = (resume_dir or cfg.get("resume_dir") or "").strip()

    if resume and Path(resume).exists():
        proj = Project.open(resume)
        # 「作り直し」からの実行は、見た目・分量の設定を今の画面のものにする
        ov = _REMAKE_OVERRIDE_KEYS if cfg.get("_remake") else ()
        cfg = _merge_resume_cfg(cfg, proj.load_run_cfg(), overrides=ov)
        total, done = proj.progress()
        log(f"♻ 再開: {proj.base.name}（{done}/{total} ステージ完了済み）")
        if ov:
            # 世界観・台本方針・サムネ指示などの内容系は「この動画のチャンネルの現在値」
            # を優先＝文章・世界観タブの編集が作り直しに反映される（混線もしない）。
            # memoは⑤の作り直しで退避済みのことがあるため _keep_memo から引く
            applied = _overlay_video_channel(cfg)
            if applied:
                log("    ※ 世界観・台本方針などは「この動画のチャンネルの現在の設定」を"
                    "使います（文章・世界観タブの編集が反映されます。お題は元の動画のまま）")
            else:
                log("    ※ エンジン・声・テロップ・BGM・枚数は「今の画面の設定」を使います"
                    "（お題と世界観は元の動画のrun_cfgの値。チャンネルを特定できない"
                    "場合はタブの編集は反映されません）")
            proj.save_run_cfg(cfg)   # 次回以降もこの設定で続くように書き戻す
        else:
            log("    ※ エンジンと声は「今の画面の設定」を使います"
                "（お題・形式・テロップ等は元の動画のまま）")
    else:
        proj = Project.create(out_dir, run_cfg=cfg)
        log(f"▶ 新規プロジェクト: {proj.base}")

    cfg = dict(cfg)
    cfg["_debug_dir"] = str(proj.path("_debug"))

    # 縦ショート: OP/ED動画は16:9前提のチャンネル設定なので連結しない（2026-08-10配布先報告
    # 「ショートに終了画面が付く」。ここで一点消しておけば compose の連結2箇所と
    # 概要欄チャプターのOPオフセットが同じcfgを見るため整合が取れる）
    if int(cfg.get("video_height", 1080)) > int(cfg.get("video_width", 1920)):
        if cfg.get("intro_video") or cfg.get("outro_video"):
            log("    縦ショートのため、冒頭・終了画面の動画連結はスキップします"
                "（ショートに終了画面は不要）。")
        cfg["intro_video"] = cfg["outro_video"] = ""

    # 共有Chromeは「主エンジンがWebの時は起動時に」「フォールバックでWebに落ちた時は遅延で」開く。
    # session["ctx"] を各ステージが読む（遅延で開いた分も後続ステージで再利用される）。
    session = {"pw": None, "browser": None, "ctx": None}

    def ensure_ctx():
        if session["ctx"] is not None:
            return session["ctx"]
        try:
            log("Webエンジンに接続します（共有ブラウザ）…")
            pw2, br2, ctx2 = browser_ai.open_session(
                cfg.get("chrome_profile"), int(cfg.get("cdp_port", 9222)), log,
                browser_exe=cfg.get("browser_exe", ""))
            session.update(pw=pw2, browser=br2, ctx=ctx2)
        except Exception as e:
            log(f"Chrome接続に失敗（{e}）。Web系は mock 等にフォールバックします。")
            session["ctx"] = None
        return session["ctx"]

    cfg["_ensure_browser"] = ensure_ctx  # _run_with_fallback がWeb版到達時に呼ぶ
    if providers.any_engine_needs_browser(cfg):  # 主エンジンがWeb → 起動時に開く
        ensure_ctx()

    browser_ai.STOP_CHECK = stop_flag  # ⏹をブラウザ待機ループへも伝える
    _t0 = time.time()   # 経過時間表示（2026-08-17改善要望: ログに進行%と経過時間）

    def _elapsed() -> str:
        s = int(time.time() - _t0)
        return f"{s // 60}分{s % 60:02d}秒" if s >= 60 else f"{s}秒"

    try:
        for _si, stage in enumerate(STAGES):
            if stop_flag():
                log("■ 中断要求により停止しました。")
                break
            if proj.done(stage):
                log(f"♻ {STAGE_LABEL[stage]}: 済み → スキップ")
                continue
            _pct = int(_si * 100 / len(STAGES))
            log(f"\n▶ {STAGE_LABEL[stage]} …（進行 {_pct}%・経過 {_elapsed()}）")
            # 一時的な失敗（回線/レート制限/NVENC不調）は自動リトライで自己回復させる。
            # sentinel設計によりステージ再実行は冪等＝安全に繰り返せる。
            retries = int(cfg.get("stage_retries", 2)) if stage in ("script", "tts", "visuals") else 0
            attempt = 0
            while True:
                try:
                    ok = STAGE_FN[stage](proj, cfg, session["ctx"], log, stop_flag)
                except Exception:
                    tb = traceback.format_exc()
                    log("【エラー】\n" + tb)
                    proj.write_debug(f"{stage}_error.txt", tb)
                    ok = False
                if ok or stop_flag():
                    break
                if stage == "compose" and attempt == 0 and not cfg.get("force_encoder"):
                    # NVENC等のHWエンコーダ失敗はソフトウェアencへ縮退して1回だけ再挑戦
                    enc = util.pick_encoder(util.find_ffmpeg())
                    if enc != "libx264":
                        cfg["force_encoder"] = "libx264"
                        log("    ♻ エンコーダをlibx264へ縮退して再挑戦します（HWエンコーダ不調の保険）")
                        attempt += 1
                        continue
                if attempt >= retries:
                    break
                attempt += 1
                wait = 30 * attempt
                log(f"    ♻ 自動リトライ {attempt}/{retries}（{wait}秒後）…")
                for _ in range(wait):
                    if stop_flag():
                        break
                    time.sleep(1)
                if stop_flag():
                    break
            if not ok:
                log(f"→ {STAGE_LABEL[stage]} で停止。再開するにはこのフォルダを指定:\n   {proj.base}")
                break
            # 最終ステージ⑤には合計時間を出す（2026-08-17改善要望:「1本何分かが
            # わからない」。途中のステージは見出し側の経過表示に任せる）
            if stage == STAGES[-1]:
                log(f"✅ {STAGE_LABEL[stage]} 完了（🎬 この1本の合計 {_elapsed()}）")
            else:
                log(f"✅ {STAGE_LABEL[stage]} 完了")
            # チェックポイント（require_checkpoint対象のみ）
            if checkpoint and cfg.get("require_checkpoint", {}).get(stage) and not stop_flag():
                if not checkpoint(stage, proj):
                    log(f"⏸ {STAGE_LABEL[stage]} 後に確認で停止しました。再開可能です。")
                    break
    finally:
        browser_ai.STOP_CHECK = None
        if session["pw"] or session["browser"]:
            browser_ai.close_session(session["pw"], session["browser"],
                                     int(cfg.get("cdp_port", 9222)), log, kill=False)

    return str(proj.base)


def upload_project(folder: str, cfg: dict, log=print, stop_flag=lambda: False,
                   force: bool = False) -> dict:
    """完成済みプロジェクトを「投稿だけ」する（作成とは独立）。

    cfg は GUI の投稿設定（upload_engine / 公開設定 / dry-run / OAuth 等）がそのまま勝つ。
    memo.json の内容（タイトル/説明/タグ/予約時刻）で投稿するので、事前に編集・保存しておくこと。
    """
    proj = Project.open(folder)
    cfg = dict(cfg)
    cfg["_debug_dir"] = str(proj.path("_debug"))

    if not proj.path("compose/final.mp4").exists():
        return {"ok": False, "error": "final.mp4 がありません（先に動画を作成してください）"}
    if proj.done("upload") and not force:
        flag = ""
        try:
            flag = proj.path("uploaded.flag").read_text(encoding="utf-8").strip()
        except Exception:
            pass
        return {"ok": False, "already": True, "error": f"既に投稿済みです（{flag}）"}

    engine = cfg.get("upload_engine", "none")
    if engine in ("none", ""):
        return {"ok": False, "error": "投稿方式(upload_engine)が none です。api か studio_web を選んでください。"}

    # 投稿エンジン自体がブラウザを要する場合(studio_web)だけChromeに接続する。
    # （設定に残った作成系エンジン video=flow_web 等に引っ張られて無駄に接続しないため）
    up_eng = engine
    if up_eng == "browser":
        up_eng = providers.BROWSER_DEFAULT.get("upload", "")
    up_cls = providers.REGISTRY.get(("upload", up_eng))
    need_browser = bool(up_cls and up_cls.needs_browser())

    pw = browser = ctx = None
    if need_browser:  # studio_web のとき
        try:
            log("YouTube Studio 用に共有ブラウザへ接続します…")
            pw, browser, ctx = browser_ai.open_session(
                cfg.get("chrome_profile"), int(cfg.get("cdp_port", 9222)), log,
                browser_exe=cfg.get("browser_exe", ""))
        except Exception as e:
            log(f"Chrome接続に失敗（{e}）。")
            return {"ok": False, "error": f"Chrome接続失敗: {e}"}
    browser_ai.STOP_CHECK = stop_flag
    sink: dict = {}
    try:
        ok = stage_upload(proj, cfg, ctx, log, stop_flag, sink=sink)
    finally:
        browser_ai.STOP_CHECK = None
        if pw or browser:
            browser_ai.close_session(pw, browser, int(cfg.get("cdp_port", 9222)), log, kill=False)
    # 「予約完了(ok)」「要ブラウザ操作(manual)」「中断」「失敗」を正直に区別して返す
    # （studio_webのmanualを“完了”と誤カウントさせない＝一括投稿の集計を正確に）
    manual = bool(sink.get("manual"))
    return {"ok": bool(ok) and not manual and not sink.get("stopped"),
            "manual": manual, "stopped": bool(sink.get("stopped")),
            "note": sink.get("note", ""), "error": sink.get("error", ""),
            "video_id": sink.get("video_id", ""), "publish_at": sink.get("publish_at", "")}


def _pick_thumb_engine(cfg: dict, ctx=None) -> str:
    """投稿タブからのサムネ作り直しで使えるエンジンを選ぶ。

    ブラウザ(ctx)が渡っていればWeb版(chatgpt_web/gemini_web)をそのまま使う。
    ブラウザが無い場合だけ、キーのあるAPIエンジンへ振り替える。
    ※既定の配布設定は全エンジンWeb版＝APIキー無しなので、ctxを渡さないと必ず
      「APIキー未設定」で失敗する（この振り替えだけでは救えない）。"""
    e = (cfg.get("thumb_engine") or "api").strip()
    if ctx is not None and providers.engine_needs_browser("thumb", e):
        return e
    if e == "api" and cfg.get("openai_api_key"):
        return "api"
    if e == "gemini_api" and cfg.get("gemini_api_key"):
        return "gemini_api"
    if cfg.get("openai_api_key"):
        return "api"
    if cfg.get("gemini_api_key"):
        return "gemini_api"
    return ""  # APIキーもブラウザも無い → 呼び出し側でエラー表示


def _video_channel_cfg(base: Path, cfg: dict) -> dict:
    """その動画が作られたチャンネルの内容系設定でcfgを上書きしたコピーを返す。

    投稿タブの🖼サムネ作り直し/✨文字案が「今アクティブなチャンネル」の
    thumbnail_prompt/title_prefix で動いて、別チャンネルの世界観のサムネが
    焼かれる事故（2026-07-16監査 #17）の防止。
    優先順: run_cfg.json（新しい動画は内容系キーを保存）＞ memo.jsonの
    チャンネル名からchannelsを逆引き ＞ 今のアクティブチャンネル（従来動作）。"""
    c = dict(cfg)
    memo = util.load_json(base / "memo.json", {}) or {}
    name = (memo.get("channel") or "").strip()
    if name:
        for chd in (cfg.get("channels") or []):
            if chd.get("name") == name:
                for k in ("thumbnail_prompt", "title_prefix", "visual_prompt_template"):
                    if (chd.get(k) or "").strip():
                        c[k] = chd[k]
                break
    rc = util.load_json(base / "run_cfg.json", {}) or {}
    for k in ("topic", "thumbnail_prompt", "title_prefix", "visual_prompt_template"):
        if (rc.get(k) or "").strip():
            c[k] = rc[k]
    return c


def regen_thumbnail(folder: str, cfg: dict, copy_text: str = "", full: bool = True,
                    log=print, ctx=None) -> dict:
    """完成プロジェクトのサムネを作り直す（投稿前の差し替え）。

    full=True : AIで背景を新規生成 → キャッチコピーを焼き込む（この動画の内容に合わせる）。
    full=False: 保存済みの背景(thumbnail_bg.png)に文字だけ入れ直す（無ければfullに自動フォールバック）。
    copy_text : 焼き込む文字（空なら meta.thumb_text → title を使う）。
    背景プロンプトは _build_prompt が meta.title と cfg.topic から自動生成するため、
    cfg.topic はこの動画の run_cfg.json から取り直して「中身に合った」サムネにする。
    """
    from . import thumb_text
    from .providers import thumb as thumb_mod
    base = Path(folder)
    # 縦ショートは作っても投稿時に必ず添付されない（stage_uploadの保険）＝課金だけ発生する
    # ため、入口で止めて案内する（2026-08-10敵対検証）。向きはこの動画のrun_cfgで判定
    rc = util.load_json(base / "run_cfg.json", {}) or {}
    if int(rc.get("video_height", 1080)) > int(rc.get("video_width", 1920)):
        log("    この動画は縦ショートです。YouTubeショートはカスタムサムネイル非対応の"
            "ため、サムネイルは作りません（作っても投稿時に使われません）。")
        return {"ok": False, "error": "縦ショートはサムネイル非対応"}
    thumb = base / "thumb" / "thumbnail.png"
    bg = base / "thumb" / "thumbnail_bg.png"
    meta = util.load_json(base / "script" / "meta.json", {}) or {}
    memo = util.load_json(base / "memo.json", {}) or {}
    if not meta.get("title"):
        meta["title"] = memo.get("title", "")
    # お題・サムネ世界観は「この動画のチャンネル」から取り直す
    # （作成タブの今のお題/アクティブchに引っ張られないように）
    c = _video_channel_cfg(base, cfg)
    copy = (copy_text or "").strip() or meta.get("thumb_text") or meta.get("title", "")

    thumb.parent.mkdir(parents=True, exist_ok=True)
    if not full and bg.exists():
        # 文字だけ入れ直し: きれいな背景から焼き直す（文字の二重焼き防止）
        try:
            import shutil as _sh
            _sh.copyfile(bg, thumb)
        except Exception as e:
            return {"ok": False, "error": f"背景の複製に失敗: {e}"}
        thumb_text.draw_thumb_text(thumb, copy[:40], c, log)
    else:
        if not full and not bg.exists():
            log("    きれいな背景が無いのでAIで作り直します（この動画は旧版のためbg未保存）。")
        eng = _pick_thumb_engine(c, ctx)
        if not eng:
            return {"ok": False, "error": "サムネを作れませんでした。"
                                          "設定・キー タブでAPIキーを入れるか、"
                                          "サムネのエンジンをWeb版（ChatGPT/Gemini）にして"
                                          "ブラウザにログインしておいてください。"}
        c["thumb_engine"] = eng
        prov = providers.get_provider("thumb", c, log, ctx=ctx)
        try:
            ok = prov.generate(c, meta, str(bg), log)  # まず背景を生成
        except Exception as e:
            return {"ok": False, "error": f"サムネ生成でエラー: {e}"}
        if not ok or not bg.exists():
            return {"ok": False, "error": "AIが画像を返しませんでした（時間をおいて再試行してください）。"}
        try:
            import shutil as _sh
            _sh.copyfile(bg, thumb)
        except Exception as e:
            return {"ok": False, "error": f"背景の複製に失敗: {e}"}
        thumb_text.draw_thumb_text(thumb, copy[:40], c, log)

    # 焼いた文字を控える（投稿タブを読み直した時に最後の文字が復元されるように。2026-08-20）
    try:
        _mp = base / "script" / "meta.json"
        _meta2 = util.load_json(_mp, {}) or {}
        if copy[:40] and _meta2.get("thumb_text") != copy[:40]:
            _meta2["thumb_text"] = copy[:40]
            util.save_json_atomic(_mp, _meta2)
    except Exception:
        pass

    # memoは保存直前に読み直す（AI生成の20〜40秒の間にユーザーが保存した
    # タイトル/予約日時などを、古いスナップショットで潰さないため）
    memo = util.load_json(base / "memo.json", {}) or {}
    memo["thumbnail"] = str(thumb)
    util.save_json_atomic(base / "memo.json", memo)
    log(f"    ✅ サムネを更新しました: {thumb}")
    return {"ok": True, "thumb": str(thumb), "copy": copy[:40]}


def _oneshot_text(cfg: dict, system: str, user: str, log=print, ctx=None) -> str:
    """単発のテキスト生成（サムネ文字案などの小さな用途）。

    Claude API → Gemini API → （キー無しなら）選択中エンジンのWeb版(_call) の順で試す。
    ＝キー無しのChromeログイン運用でも✨バズる文字が動くようにする（topics同型の穴を塞ぐ）。"""
    if cfg.get("anthropic_api_key"):
        try:
            from .providers.script_claude import ClaudeScript
            return ClaudeScript()._call(cfg, system, user, log)
        except Exception as e:
            log(f"    文字案の生成に失敗（Claude）: {str(e)[:80]}")
    if cfg.get("gemini_api_key"):
        try:
            from google import genai
            client = genai.Client(api_key=cfg["gemini_api_key"])
            model = cfg.get("gemini_text_model") or "gemini-2.5-flash"
            resp = client.models.generate_content(model=model, contents=f"{system}\n\n{user}")
            return getattr(resp, "text", "") or ""
        except Exception as e:
            log(f"    文字案の生成に失敗（Gemini）: {str(e)[:80]}")
    # APIキーが無い → デスクトップ連携（ChatGPT/Claudeアプリ）が選ばれていればそれで生成
    # （キー無し運用の配布先でも、読み仮名マップ等の補助生成が動くようにする）
    eng0 = (cfg.get("script_engine") or "").strip()
    if eng0.endswith("_desktop"):
        try:
            prov = providers.get_provider("script", cfg, log, ctx)
            if hasattr(prov, "_call") and getattr(prov, "engine", "") != "mock":
                return prov._call(cfg, system, user, log)
        except Exception as e:
            log(f"    文字案の生成に失敗（デスクトップ）: {str(e)[:80]}")
    # 選択中の台本エンジンがWeb版なら、ログイン済みブラウザで生成
    if ctx is not None:
        eng = (cfg.get("script_engine") or "").strip()
        if not providers.engine_needs_browser("script", eng):  # api等が選ばれていればWeb版へ寄せる
            eng = "claude_web"
        try:
            # 小さな依頼に本番の6分タイムアウトは長すぎる（失敗確定まで12分＝
            # 2026-08-05配布先報告）→ 単発用の短いタイムアウトを渡す
            c2 = dict(cfg, script_engine=eng)
            c2.setdefault("_web_timeout", 120)
            prov = providers.get_provider("script", c2, log, ctx)
            if hasattr(prov, "_call") and getattr(prov, "engine", "") != "mock":
                return prov._call(c2, system, user, log)
        except Exception as e:
            log(f"    文字案の生成に失敗（Web）: {str(e)[:80]}")
    return ""


def suggest_thumb_copies(folder: str, cfg: dict, n: int = 5, log=print, ctx=None) -> list[str]:
    """この動画の内容から「バズるサムネ文字」案をn個作る（投稿タブの✨ボタン用）。

    タイトル・お題・本文冒頭に加え、チャンネルの傾向（title_prefix / thumbnail_prompt）も
    文脈として渡す＝「もちろん以下も反映」への対応。出力は 8〜13字×最大2行(/区切り)。
    """
    base = Path(folder)
    cfg = _video_channel_cfg(base, cfg)  # 傾向(title_prefix/thumbnail_prompt)はこの動画のchから
    meta = util.load_json(base / "script" / "meta.json", {}) or {}
    memo = util.load_json(base / "memo.json", {}) or {}
    rc = util.load_json(base / "run_cfg.json", {}) or {}
    title = meta.get("title") or memo.get("title", "")
    topic = rc.get("topic", "")
    try:  # 台本は全文読ませる（動画全体の“一番おいしい山場”からコピーを作らせる）
        script = (base / "script" / "script.txt").read_text(encoding="utf-8")[:16000]
    except Exception:
        script = ""
    prefix = (cfg.get("title_prefix") or "").strip()
    style = (cfg.get("thumbnail_prompt") or "").strip()
    system = ("あなたはYouTubeのサムネ職人。台本を最後まで読み、動画の中で最もクリックを"
              "誘う山場・結末・意外性を見抜いて、クリック率(CTR)を最大化する短いサムネ用"
              "キャッチコピーだけを作る。説明や前置きは書かない。")
    user = (f"次の動画の『サムネに焼く文字』を{n}案ください。\n"
            f"【タイトル】{title}\n【お題】{topic}\n【台本（全文）】\n{script}\n"
            + (f"【チャンネルの傾向・タイトル接頭辞】{prefix}\n" if prefix else "")
            + (f"【サムネの世界観】{style}\n" if style else "")
            + "ルール:\n"
              "・1案 = 8〜13文字 × 最大2行。2行に分ける時は「/」で区切る。\n"
              "・数字/落差/結末の予感/伏せ字●● などCTRの定石を使う。\n"
              "・誇大・嘘はNG。動画の中身と一致させる。\n"
              f"・{n}行、1行に1案だけ。番号・記号の羅列・説明は書かない。")
    text = _oneshot_text(cfg, system, user, log, ctx=ctx)
    out: list[str] = []
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        # 行頭の「箇条書き記号」や「1桁2桁の番号＋区切り」だけを除去
        # （"100年後" のような本文中の数字は消さない＝数字はCTRの武器）
        ln = re.sub(r"^\s*(?:[-–—・*•]|\d{1,2}[.)、:：])\s*", "", ln).strip()
        ln = ln.strip('"\'「」『』 　')
        if not ln:
            continue
        core = ln.replace("/", "").replace("／", "")
        if 2 <= len(core) <= 30:
            out.append(ln.replace("／", "/"))
        if len(out) >= n:
            break
    return out
