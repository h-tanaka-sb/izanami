"""BGMのAI新規作成（Suno）。

Sunoに公式の開発者APIは無い（2026-07時点）ため、次の2経路を用意する:
  suno_web … suno.com を共有Chromeで自動操作（自分のSunoアカウント・無料枠OK）。
             プロンプト投入まで自動、生成後のダウンロードは人が押す（DLイベントを自動保存）。
  suno_api … サードパーティAPI sunoapi.org（従量課金・APIキーが必要）。
             POST /api/v1/generate → GET /api/v1/generate/record-info をポーリング。
             1回の生成で2曲返る。生成物は bgm/ に保存し、BGMライブラリに登録して使う。
"""
from __future__ import annotations

import json
import random
import re
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BGM_DIR = ROOT / "bgm"

_BRIEF_FALLBACK = {"genre": "汎用",
                   "prompt": "calm upbeat Japanese pop instrumental background music, "
                             "no vocals, gentle and pleasant"}


def join_bgm(paths: list[str], out_path: str, log=print) -> str:
    """複数のBGMを順番につなげて1本のm4a(AAC)にする（BGMタブ「🔗つなげて1本に」）。

    形式・サンプルレートがバラバラでも安全なよう、各入力を 44.1kHz/stereo に正規化してから
    concat フィルタで連結（再エンコード）。戻り値=出力パス（失敗時はRuntimeError）。"""
    from . import util
    srcs = [p for p in paths if p and Path(p).exists()]
    if len(srcs) < 2:
        raise RuntimeError("つなげる曲が2曲未満です（ファイルの実在も確認してください）。")
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [util.find_ffmpeg(), "-y"]
    for s in srcs:
        cmd += ["-i", str(s)]
    norm = ";".join(
        f"[{i}:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[a{i}]"
        for i in range(len(srcs)))
    concat_in = "".join(f"[a{i}]" for i in range(len(srcs)))
    fc = f"{norm};{concat_in}concat=n={len(srcs)}:v=0:a=1[out]"
    cmd += ["-filter_complex", fc, "-map", "[out]", "-c:a", "aac", "-b:a", "192k", str(out)]
    rc = util.run_watched(cmd, timeout_sec=1800)
    if rc != 0 or not out.exists() or out.stat().st_size < 10_000:
        raise RuntimeError(f"ffmpegの連結に失敗しました（rc={rc}）。")
    log(f"    🔗 {len(srcs)}曲を連結: {out.name}")
    return str(out)


def bgm_brief(title: str, script: str, cfg: dict, log=print) -> dict:
    """台本から音楽ブリーフ {genre, prompt} を作る（Gemini・格安1コール）。失敗時は汎用。"""
    if not cfg.get("gemini_api_key"):
        log("    自動BGM: ジャンルのAI判定には Gemini APIキーが必要です（設定・キー タブ）。"
            "キーが無いため、曲名・ジャンル名と台本の文字照合で選びます。")
        return dict(_BRIEF_FALLBACK)
    try:
        from google import genai
        client = genai.Client(api_key=cfg["gemini_api_key"])
        model = cfg.get("gemini_text_model") or "gemini-2.5-flash"
        req = (
            "次のYouTube動画に合うBGMの仕様を決めてください。\n"
            f"タイトル: {title}\n台本の冒頭:\n{(script or '')[:1200]}\n\n"
            "JSONだけを出力（他の文章は書かない）:\n"
            '{"genre": "日本語2〜8文字のジャンル名（例: スカッと系/ほのぼの/緊迫サスペンス/コミカル）", '
            '"prompt": "英語60語以内のBGM生成プロンプト。instrumental, no vocals を必ず含める"}')
        resp = client.models.generate_content(model=model, contents=req)
        text = (getattr(resp, "text", None) or "").strip()
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        j = json.loads(m.group(0)) if m else {}
        genre = (j.get("genre") or "").strip()[:12]
        prompt = (j.get("prompt") or "").strip()[:400]
        if genre and prompt:
            return {"genre": genre, "prompt": prompt}
    except Exception as e:
        log(f"    BGMブリーフ生成に失敗（{str(e)[:60]}）→ 汎用BGMで続行")
    return dict(_BRIEF_FALLBACK)


def add_library_entries(paths: list[str], genre: str, gain_db: int = -6) -> None:
    """生成した曲をジャンル(mood)付きで settings.json の bgm_library に永続登録。"""
    from . import config as _config
    cfg = _config.load_settings()
    lib = cfg.get("bgm_library", []) or []
    known = {e.get("path") for e in lib}
    for p in paths:
        if p not in known:
            lib.append({"path": p, "title": Path(p).stem, "mood": genre, "gain_db": gain_db})
    cfg["bgm_library"] = lib
    _config.save_settings(cfg)


def bgm_for_channel(lib: list, ch_idx) -> list:
    """このチャンネルで使える曲だけに絞る。channels未設定(空)=全ch共通。

    そのチャンネル専用の曲が1曲も無ければ全体にフォールバック（BGM無しにしない）。"""
    try:
        ci = int(ch_idx)
    except (TypeError, ValueError):
        ci = -1
    sub = [e for e in lib if not e.get("channels") or ci in (e.get("channels") or [])]
    return sub or lib


def ensure_auto_bgm(cfg: dict, title: str, script: str, log=print) -> str | None:
    """bgm_mode=auto: 台本に合うBGMをライブラリから自動で選ぶ。

    1) ブリーフ（ジャンル判定）を作る
    2) ライブラリに同ジャンルの曲があればそれを使う
    3) 無ければライブラリ全体からランダム
    ※ Sunoでの新規生成はユーザー判断で廃止（2026-07-06）。生成コード自体は下に温存。
    """
    lib = [e for e in (cfg.get("bgm_library", []) or []) if Path(e.get("path", "")).exists()]
    if not lib:
        log("    自動BGM: ライブラリが空です → BGMなしで続行（BGMタブで曲を追加してください）。")
        return None
    lib = bgm_for_channel(lib, cfg.get("active_channel", 0))  # このチャンネルの曲に絞る
    brief = bgm_brief(title, script, cfg, log)
    genre = brief["genre"]
    match = [e for e in lib if e.get("mood") == genre]
    if match:
        pick = random.choice(match)
        log(f"    自動BGM: ライブラリ「{genre}」から選曲 → {pick.get('title')}")
        return pick["path"]
    # AI判定なし/一致なし: 曲のジャンル名・曲名が台本に出てくるものを優先（オフライン照合）
    pick = _match_by_text(lib, title, script)
    if pick is not None:
        log(f"    自動BGM: 台本の内容と曲のジャンル・曲名を照合して選曲 → {pick.get('title')}")
        return pick["path"]
    pick = random.choice(lib)
    log(f"    自動BGM: ジャンル「{genre}」の曲が無いので全体から選曲 → {pick.get('title')}")
    log("    → 曲にジャンルを付けておくと、雰囲気に合う曲が優先されます。")
    return pick["path"]


def _match_by_text(lib: list, title: str, script: str):
    """曲の mood（ジャンル名）や曲名が台本・タイトルに現れる曲を選ぶ（AI不要の保険）。

    例: mood「ほのぼの」が台本に出てくる／曲名「怖い」がタイトルに含まれる。
    どれも一致しなければ None（呼び元で従来どおり全体ランダム）。"""
    text = f"{title or ''}\n{(script or '')[:3000]}"
    best, best_score = None, 0
    for e in lib:
        score = 0
        mood = (e.get("mood") or "").strip()
        if len(mood) >= 2 and mood != "汎用" and mood in text:
            score += 2
        name = (e.get("title") or "").strip()
        if len(name) >= 2 and name in text:
            score += 1
        if score > best_score:
            best, best_score = e, score
    return best


def _out_paths(n: int = 2) -> list[Path]:
    BGM_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return [BGM_DIR / f"suno_{stamp}_{i + 1}.mp3" for i in range(n)]


# ── API（sunoapi.org・サードパーティ） ────────────────────────────────
def generate_suno_api(prompt: str, cfg: dict, log=print) -> list[str]:
    import httpx
    key = (cfg.get("suno_api_key") or "").strip()
    if not key:
        raise RuntimeError("suno_api_key が未設定です（設定・キー タブ。sunoapi.org で発行）。")
    base = (cfg.get("suno_api_base") or "https://api.sunoapi.org").rstrip("/")
    model = cfg.get("suno_model") or "V5"
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    body = {
        "customMode": False,
        "instrumental": bool(cfg.get("suno_instrumental", True)),
        "model": model,
        "prompt": (prompt or "").strip()[:490],
        "callBackUrl": "https://example.com/suno-callback",  # ポーリング利用のためダミー
    }
    log(f"    Suno(API)で作曲リクエスト送信…（{model}・インスト={body['instrumental']}）")
    r = httpx.post(f"{base}/api/v1/generate", json=body, headers=headers, timeout=60)
    j = r.json()
    if r.status_code != 200 or j.get("code") != 200:
        raise RuntimeError(f"Suno API エラー: {r.status_code} {str(j)[:200]}")
    task = (j.get("data") or {}).get("taskId")
    if not task:
        raise RuntimeError(f"taskId が取得できません: {str(j)[:200]}")

    wait_max = int(cfg.get("suno_wait_sec", 300))
    end = time.time() + wait_max
    urls: list[str] = []
    log("    生成待ち（2〜3分）…")
    while time.time() < end:
        time.sleep(10)
        d = httpx.get(f"{base}/api/v1/generate/record-info",
                      params={"taskId": task}, headers=headers, timeout=30).json()
        data = d.get("data") or {}
        status = data.get("status", "")
        if status in ("CREATE_TASK_FAILED", "GENERATE_AUDIO_FAILED",
                      "CALLBACK_EXCEPTION", "SENSITIVE_WORD_ERROR"):
            raise RuntimeError(f"Suno生成失敗: {status}")
        songs = ((data.get("response") or {}).get("sunoData")) or []
        urls = [s.get("audioUrl") for s in songs if s.get("audioUrl")]
        if status == "SUCCESS" and urls:
            break
        log(f"      …{status or 'PENDING'}")
    if not urls:
        raise RuntimeError("時間内に曲が完成しませんでした（suno_wait_sec を延ばして再試行可）。")

    outs = _out_paths(len(urls))
    saved = []
    for u, p in zip(urls, outs):
        try:
            audio = httpx.get(u, timeout=120, follow_redirects=True)
            p.write_bytes(audio.content)
            if p.stat().st_size > 10000:
                saved.append(str(p))
                log(f"    保存: {p.name}")
        except Exception as e:
            log(f"    ダウンロード失敗（スキップ）: {str(e)[:80]}")
    if not saved:
        raise RuntimeError("mp3のダウンロードに失敗しました。")
    return saved


# ── Web版（suno.com・共有Chrome） ─────────────────────────────────────
def generate_suno_web(cfg: dict, log=print) -> list[str]:
    from . import browser_ai
    prompt = (cfg.get("_suno_prompt") or "").strip()
    profile = cfg.get("chrome_profile") or ""
    port = int(cfg.get("cdp_port", 9222))
    wait_sec = int(cfg.get("suno_wait_sec", 300))
    outs: list[str] = []
    pw = browser = None
    try:
        pw, browser, ctx = browser_ai.open_session(profile, port, log=log)
        page = ctx.new_page()
        got: list[Path] = []

        def _on_download(d):
            try:
                BGM_DIR.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                suffix = Path(d.suggested_filename or "song.mp3").suffix or ".mp3"
                p = BGM_DIR / f"suno_{stamp}_{len(got) + 1}{suffix}"
                d.save_as(str(p))
                got.append(p)
                log(f"    ダウンロードを保存: {p.name}")
            except Exception as e:
                log(f"    DL保存エラー: {e}")

        try:
            page.on("download", _on_download)
            page.goto("https://suno.com/create", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
            box = browser_ai._find(page, ["textarea", 'div[contenteditable="true"]'])
            if box:
                try:
                    box.click(); page.wait_for_timeout(300)
                    page.keyboard.insert_text(prompt)
                    log("    Sunoにプロンプトを入力しました。")
                except Exception:
                    pass
                for sel in ['button:has-text("Create")', 'button:has-text("作成")']:
                    try:
                        el = page.query_selector(sel)
                        if el and el.is_visible():
                            el.click(timeout=4000)
                            log("    Create を押しました。生成を待っています…")
                            break
                    except Exception:
                        continue
            else:
                log("    入力欄が見つかりません。Sunoに未ログインの可能性→開いた画面でログインしてください。")
            log("    生成が終わったら、曲の「…」メニューから Download → MP3 を押してください"
                f"（自動で bgm/ に保存します。最大{wait_sec}秒待機・2曲まで）。")
            end = time.time() + wait_sec
            while time.time() < end and len(got) < 2:
                page.wait_for_timeout(2000)
        finally:
            try:
                page.close()
            except Exception:
                pass
        outs = [str(p) for p in got if p.exists()]
    finally:
        try:
            browser_ai.close_session(pw, browser, port, log=log)
        except Exception:
            pass
    if not outs:
        raise RuntimeError("曲のダウンロードを確認できませんでした（時間切れ）。")
    return outs
