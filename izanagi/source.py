"""①台本の「入力ソース」前処理（script_source）。

  topic    … お題からそのまま台本を作る（従来どおり）
  youtube  … お題欄に貼られた YouTube URL の字幕（文字起こし）を取得し、
             リライトして“新しいオリジナルストーリー”の台本を作る
  research … お題欄のキーワードで Web リサーチ（Gemini 検索グラウンディング
             → Claude Web検索 の順で自動選択）し、事実に基づいた台本を作る

prepare_source() が参考資料ブロック（cfg["_source_context"]）を作り、
providers.script_claude.build_user_prompt() がプロンプト先頭に差し込む。
"""
from __future__ import annotations

import re
from pathlib import Path

# 参考資料としてプロンプトに入れる最大文字数（超過分は切り捨て）
_MAX_CONTEXT_CHARS = 16000

_YT_ID_RE = re.compile(
    r"(?:v=|/shorts/|youtu\.be/|/live/|/embed/)([A-Za-z0-9_-]{11})")


def extract_video_id(text: str) -> str | None:
    m = _YT_ID_RE.search(text or "")
    if m:
        return m.group(1)
    t = (text or "").strip()
    return t if re.fullmatch(r"[A-Za-z0-9_-]{11}", t) else None


def fetch_youtube_transcript(url: str, log=print) -> str:
    """YouTube動画の字幕（自動生成含む）を取得して平文にする。失敗は RuntimeError。"""
    vid = extract_video_id(url)
    if not vid:
        raise RuntimeError(
            "YouTubeのURLが見つかりません。お題欄に動画URL"
            "（例 https://www.youtube.com/watch?v=XXXXXXXXXXX）を貼ってください。")
    langs = ["ja", "ja-JP", "en", "en-US"]
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        try:  # 1.x 系（インスタンスAPI）
            fetched = YouTubeTranscriptApi().fetch(vid, languages=langs)
            parts = [getattr(s, "text", "") for s in fetched]
        except AttributeError:  # 旧API
            data = YouTubeTranscriptApi.get_transcript(vid, languages=langs)
            parts = [d.get("text", "") for d in data]
    except Exception as e:
        raise RuntimeError(
            f"字幕（文字起こし）を取得できませんでした: {str(e)[:150]}\n"
            "    → 字幕が無い動画・メンバー限定・年齢制限つき動画は取得できません。")
    text = re.sub(r"\s+", " ", " ".join(p for p in parts if p)).strip()
    if len(text) < 50:
        raise RuntimeError("字幕がほぼ空でした。別の動画でお試しください。")
    return text


# ── キーワードリサーチ ────────────────────────────────────────────────
def _research_prompt(keywords: str) -> str:
    return (
        f"「{keywords}」について、YouTube動画の台本を作るための情報をWebで調べてください。\n"
        "重要な事実・数字・日付・登場する人物や団体・具体例・意外なエピソード・"
        "視聴者が驚くポイント・よくある誤解を、日本語の箇条書き（15〜30項目）でまとめてください。"
        "最新の情報を優先し、分かるものは出典（サイト名）も添えてください。")


def _research_gemini(prompt: str, cfg: dict, log=print) -> str:
    from google import genai
    from google.genai import types as gt
    client = genai.Client(api_key=cfg["gemini_api_key"])
    model = cfg.get("gemini_text_model") or "gemini-2.5-flash"
    log(f"    Gemini（Google検索グラウンディング）でリサーチ中… ({model})")
    resp = client.models.generate_content(
        model=model, contents=prompt,
        config=gt.GenerateContentConfig(tools=[gt.Tool(google_search=gt.GoogleSearch())]))
    return (getattr(resp, "text", None) or "").strip()


def _research_claude(prompt: str, cfg: dict, log=print) -> str:
    import anthropic
    client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
    model = cfg.get("ai_model") or "claude-opus-4-8"
    log(f"    Claude（Web検索ツール）でリサーチ中… ({model})")

    def _run(tool_type: str):
        messages = [{"role": "user", "content": prompt}]
        resp = None
        for _ in range(4):  # サーバ側ツールの pause_turn 継続
            resp = client.messages.create(
                model=model, max_tokens=8000,
                tools=[{"type": tool_type, "name": "web_search", "max_uses": 5}],
                messages=messages)
            if resp.stop_reason != "pause_turn":
                break
            messages = [{"role": "user", "content": prompt},
                        {"role": "assistant", "content": resp.content}]
        return "\n".join(b.text for b in resp.content if b.type == "text").strip()

    try:
        return _run("web_search_20260209")
    except Exception as e:
        if "web_search" in str(e):  # 旧モデル等でツール版が違う場合
            return _run("web_search_20250305")
        raise


def research_keywords(keywords: str, cfg: dict, log=print) -> str:
    """キーワードをWebリサーチして箇条書きノートを返す。使えるキーが無ければ ""。"""
    prompt = _research_prompt(keywords)
    if cfg.get("gemini_api_key"):
        try:
            notes = _research_gemini(prompt, cfg, log)
            if notes:
                return notes
            log("    Geminiリサーチが空でした。")
        except Exception as e:
            log(f"    Geminiリサーチ失敗: {str(e)[:120]}")
    if cfg.get("anthropic_api_key"):
        try:
            notes = _research_claude(prompt, cfg, log)
            if notes:
                return notes
        except Exception as e:
            log(f"    Claudeリサーチ失敗: {str(e)[:120]}")
    return ""


# ── YouTubeリライトの2モード判定（2026-07-07 ユーザー仕様） ──────────────
#  事実系（実話・ニュース・事件・実在人物）: 登場人物や出来事はそのまま＋Web検索で深掘り
#  創作系（フィクション・体験談風）      : 骨格を保った完全リライト（大きくは外れない）
_FACT_HINTS = re.compile(
    r"ニュース|事件|逮捕|裁判|判決|容疑|報道|発表|株式会社|県警|警察|政府|省庁|"
    r"選手|監督|市長|知事|大統領|首相|億円|兆円|昭和\d|平成\d|令和\d|\d{4}年")


def _classify_transcript(text: str, cfg: dict, log=print) -> str:
    """文字起こしを 'fact' / 'fiction' に分類する。

    優先: Gemini/Claude APIで判定 → キー無し/失敗はキーワードのヒューリスティック。
    cfg['youtube_rewrite_mode'] が fact/fiction ならそれを強制（auto=判定）。"""
    forced = (cfg.get("youtube_rewrite_mode") or "auto").strip()
    if forced in ("fact", "fiction"):
        return forced
    head = text[:4000]
    q = ("次のYouTube動画の文字起こしは、実在の人物・団体・事件・ニュース等の"
         "「事実に基づく内容」ですか？ それとも体験談風の創作・フィクションですか？\n"
         "fact か fiction の1単語だけで答えてください。\n\n" + head)
    try:
        if cfg.get("gemini_api_key"):
            from google import genai
            client = genai.Client(api_key=cfg["gemini_api_key"])
            r = client.models.generate_content(
                model=cfg.get("gemini_text_model") or "gemini-2.5-flash", contents=q)
            a = (getattr(r, "text", "") or "").lower()
            if "fact" in a or "fiction" in a:
                return "fact" if "fact" in a else "fiction"
        elif cfg.get("anthropic_api_key"):
            import anthropic
            client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
            r = client.messages.create(model=cfg.get("ai_model") or "claude-opus-4-8",
                                       max_tokens=10,
                                       messages=[{"role": "user", "content": q}])
            a = "".join(getattr(b, "text", "") for b in r.content).lower()
            if "fact" in a or "fiction" in a:
                return "fact" if "fact" in a else "fiction"
    except Exception as e:
        log(f"    種別判定APIが失敗（{str(e)[:80]}）→ キーワードで判定します。")
    hits = len(_FACT_HINTS.findall(head))
    return "fact" if hits >= 3 else "fiction"


def _fact_query(text: str) -> str:
    """事実系の追加リサーチ用クエリ（文字起こし冒頭から固有名詞っぽい塊を拾う）。"""
    head = re.sub(r"\s+", " ", text[:600])
    words = re.findall(r"[ァ-ヶー]{3,}|[A-Za-z][A-Za-z0-9 ]{3,}|[一-龠]{2,}", head)
    uniq = list(dict.fromkeys(w.strip() for w in words if len(w.strip()) >= 2))
    return " ".join(uniq[:6]) or head[:60]


# ── 入口 ─────────────────────────────────────────────────────────────
def prepare_source(cfg: dict, log=print, save_dir=None) -> str | None:
    """script_source に応じて参考資料ブロックを作る。

    戻り値: 参考資料テキスト（""＝資料なしで続行） / None＝致命的エラーで停止。
    """
    mode = (cfg.get("script_source") or "topic").strip()
    topic = (cfg.get("topic") or "").strip()

    if mode == "youtube":
        log("    ①-a 元動画の文字起こしを取得中…")
        try:
            text = fetch_youtube_transcript(topic, log)
        except RuntimeError as e:
            log(f"    【エラー】{e}")
            return None
        log(f"    文字起こし {len(text)}字を取得。")
        if save_dir:
            try:
                Path(save_dir).mkdir(parents=True, exist_ok=True)
                (Path(save_dir) / "source_transcript.txt").write_text(text, encoding="utf-8")
            except Exception:
                pass
        kind = _classify_transcript(text, cfg, log)
        if save_dir:
            try:
                (Path(save_dir) / "source_mode.txt").write_text(kind, encoding="utf-8")
            except Exception:
                pass
        if kind == "fact":
            # 事実系: 登場人物・出来事は保持。追加でWebリサーチして深掘り
            log("    種別判定: 事実系（実在の人物/出来事）→ 事実は保持し、Webで追加取材します…")
            notes = ""
            try:
                notes = research_keywords(_fact_query(text), cfg, log)
            except Exception as e:
                log(f"    追加リサーチは省略（{str(e)[:80]}）")
            if notes and save_dir:
                try:
                    (Path(save_dir) / "source_research.txt").write_text(notes, encoding="utf-8")
                except Exception:
                    pass
            block = "【元動画の文字起こし（事実ベースの題材）】\n" + text[:_MAX_CONTEXT_CHARS]
            if notes:
                block += "\n\n【追加リサーチ（Web検索で集めた関連情報）】\n" + notes[:6000]
                log(f"    追加リサーチ {len(notes)}字を取得。")
            else:
                log("    ⚠ 追加リサーチは取得できませんでした（文字起こしのみで作成）。")
            return (
                block +
                "\n\nこれは実在の人物・出来事に基づく題材です。次のルールで台本化してください:\n"
                "・登場人物名・団体名・出来事・時系列・数字などの【事実は変えない】。\n"
                "・追加リサーチの情報も織り込んで、元動画より深く・詳しく・多角的に。\n"
                "・出典が曖昧な内容は断定せず「〜と言われています」等の表現にする。\n"
                "・文字起こしの文をそのまま使うこと（丸写し）は禁止。構成と語りは完全に作り直す。")
        # 創作系: 完全リライト（ただし元ネタから大きくは外れない）
        log("    種別判定: 創作系 → 骨格を保った完全リライトで新作にします。")
        return (
            "【元動画の文字起こし（リライト元の題材）】\n" + text[:_MAX_CONTEXT_CHARS] +
            "\n\nこれは創作系の題材です。次のルールで「新しいオリジナルストーリー」に"
            "作り直してください:\n"
            "・話の骨格（テーマ・立場関係・展開の流れ・カタルシスの種類）は元ネタを踏襲し、"
            "大きくは外れないこと。\n"
            "・登場人物の名前・年齢・職業・場所・具体的なエピソードの中身は全て変える（完全リライト）。\n"
            "・文字起こしの文をそのまま使うこと（丸写し）は禁止。\n"
            "・元ネタより感情の起伏が強く、結末のスカッと感が増すように再構成する。")

    if mode == "research":
        if not topic:
            log("    【エラー】リサーチするキーワードが空です。")
            return None
        log(f"    ①-a 「{topic}」をリサーチ中…")
        notes = research_keywords(topic, cfg, log)
        if not notes:
            log("    ⚠ リサーチできませんでした（Gemini/Anthropicキー未設定または失敗）。"
                "AIの知識だけで作成を続けます。")
            return ""
        log(f"    リサーチ結果 {len(notes)}字を取得。")
        if save_dir:
            try:
                Path(save_dir).mkdir(parents=True, exist_ok=True)
                (Path(save_dir) / "source_research.txt").write_text(notes, encoding="utf-8")
            except Exception:
                pass
        return (
            "【リサーチ結果（Web検索で集めた参考情報）】\n" + notes[:_MAX_CONTEXT_CHARS] +
            "\n\n上のリサーチ結果の事実に基づいて台本を作ってください。"
            "数字・日付・固有名詞はリサーチ結果に合わせ、事実と異なる創作は避けてください。")

    return ""
