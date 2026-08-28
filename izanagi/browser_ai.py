"""Web自動操作ハブ（★Bダッシュ browser_ai.py 移植＋拡張）。

専用Chromeプロファイル（永続）に claude.ai / chatgpt.com / gemini / Flow / Sora /
YouTube Studio へ一度ログインしておけば、以降そのログイン状態でCDP接続して操作する。
※Web UIはDOM変更・bot検出・完了検知が不安定。セレクタは実運用で要調整。API版を確実なフォールバックに。
"""
from __future__ import annotations

import base64
import os
import subprocess
import time
from pathlib import Path

from . import util

_LOCALAPPDATA = os.environ.get("LOCALAPPDATA", r"C:\_no_localappdata")
CHROME_PATHS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    # 管理者権限なしのユーザー単位インストール（会社PCで多い配置）
    rf"{_LOCALAPPDATA}\Google\Chrome\Application\chrome.exe",
]
EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
BRAVE_PATHS = [
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    rf"{_LOCALAPPDATA}\BraveSoftware\Brave-Browser\Application\brave.exe",
]

CLAUDE_URL = "https://claude.ai/new"
CHATGPT_URL = "https://chatgpt.com/"
GEMINI_URL = "https://gemini.google.com/app"
FLOW_URL = "https://labs.google/fx/tools/flow"
SORA_URL = "https://sora.chatgpt.com/explore"
STUDIO_URL = "https://studio.youtube.com/"


# ───────── 普段のChromeからログインを取り込む ─────────
# ※普段のChromeを直接自動操作することはできない（Chrome 136以降、既定プロファイルでの
#   リモートデバッグをChrome自体が拒否する）。代わりにログイン情報を専用プロファイルへ
#   コピーして「ログイン済みの専用Chrome」を作る。

def chrome_running() -> bool:
    try:
        pr = subprocess.run(["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/FO", "CSV"],
                            capture_output=True, text=True, timeout=15)
        return "chrome.exe" in (pr.stdout or "")
    except Exception:
        return False


def _chrome_user_data() -> Path:
    import os
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"


def list_source_profiles() -> list[tuple[str, str]]:
    """普段のChromeのプロファイル一覧 [(フォルダ名, 表示名)]。

    表示名は「プロファイル名｜Googleアカウント」（どのアカウントか分かるように）。"""
    import json
    root = _chrome_user_data()
    out: list[tuple[str, str]] = []
    try:
        st = json.loads((root / "Local State").read_text(encoding="utf-8"))
        for d, info in (st.get("profile", {}).get("info_cache", {}) or {}).items():
            if not (root / d).exists():
                continue
            name = info.get("name") or d
            mail = info.get("user_name") or ""
            out.append((d, f"{name}｜{mail}" if mail else name))
    except Exception:
        pass
    if not out and (root / "Default").exists():
        out = [("Default", "Default")]
    return out


def import_chrome_logins(dest_profile: str, src_dir_name: str = "Default", log=print) -> bool:
    """普段のChromeのログイン状態（Cookie等）を専用プロファイルへコピーする。

    Chromeを全部閉じてから実行すること（開いているとCookie DBがロックされ壊れる）。
    キャッシュ類は除外してコピーする（それでも数百MB〜になることがある）。"""
    import shutil
    root = _chrome_user_data()
    src = root / src_dir_name
    if not src.exists() or not (root / "Local State").exists():
        log(f"    普段のChromeが見つかりません: {src}")
        return False
    if chrome_running():
        log("    Chromeが起動中です。すべてのChromeウィンドウを閉じてから実行してください。")
        return False
    dest = Path(dest_profile)
    log(f"    取り込み元: Chrome「{src_dir_name}」 → {dest}")
    # 専用プロファイルを作り直してからコピー（中途半端な混在を避ける）
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.mkdir(parents=True, exist_ok=True)
    ignore = shutil.ignore_patterns(
        "*Cache*", "Code Cache", "GPUCache", "GrShaderCache", "ShaderCache",
        "Crashpad", "Crash Reports", "OptimizationGuide*", "Safe Browsing*",
        "component_crx_cache", "Download Service", "segmentation_platform")
    # Local State はそのまま持ち込むと「元の12プロファイル構成」を引きずり、
    # 存在しないプロファイルを開こうとして空プロファイルが出来てしまう。
    # → コピーした1つだけを Default として認識するよう書き換える。
    import json
    st = json.loads((root / "Local State").read_text(encoding="utf-8"))
    info = (st.get("profile", {}).get("info_cache", {}) or {}).get(src_dir_name, {})
    st["profile"] = {"info_cache": {"Default": info}, "last_used": "Default",
                     "last_active_profiles": ["Default"]}
    st.pop("profiles_order", None)
    (dest / "Local State").write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    (dest / "First Run").write_text("", encoding="utf-8")  # 初回ウィザードを出さない
    log("    コピー中…（数分かかることがあります）")
    shutil.copytree(src, dest / "Default", ignore=ignore, dirs_exist_ok=True)
    try:  # シングルトンロックの残骸は消す
        for junk in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            (dest / junk).unlink(missing_ok=True)
    except Exception:
        pass
    log("    ✅ 取り込み完了。「🌐 ログイン用ブラウザを開く」で各サイトがログイン済みか確認してください。")
    log("    ※ それでもログアウト状態の場合は、Chromeの新しいCookie保護（コピー先では復号不可）が原因です。"
        "その時だけ開いたブラウザで手動ログインしてください（一度入れば以後は保持されます）。")
    return True


def resolve_browser(choice: str = "") -> tuple[str | None, str]:
    """browser_exe設定 → (実行ファイルパス, 種別名)。

    choice: '' = Chrome自動検出 / 'edge' / 'brave' / 実行ファイルのフルパス。
    Chromium系のみ対応（CDP接続が必要。Firefoxは不可）。"""
    c = (choice or "").strip()
    if c.lower() == "edge":
        return next((p for p in EDGE_PATHS if Path(p).exists()), None), "edge"
    if c.lower() == "brave":
        return next((p for p in BRAVE_PATHS if Path(p).exists()), None), "brave"
    if c:
        return (c if Path(c).exists() else None), Path(c).stem.lower()
    return next((p for p in CHROME_PATHS if Path(p).exists()), None), "chrome"


def _profile_for(profile_dir: str, kind: str) -> str:
    """Chrome以外はプロファイルを分ける（同じuser-data-dirを別ブラウザで開くと壊れるため）。"""
    if not profile_dir or kind == "chrome":
        return profile_dir
    return f"{profile_dir}_{kind}"


def launch_login_chrome(profile_dir: str, port: int = 9222, log=print,
                        browser_exe: str = "") -> bool:
    exe, kind = resolve_browser(browser_exe)
    if not exe:
        log(f"    ブラウザ（{kind}）が見つかりません。設定タブの「使うブラウザ」を確認してください。")
        return False
    profile_dir = _profile_for(profile_dir, kind)
    if kind != "chrome":
        log(f"    使うブラウザ: {kind}（プロファイル: {profile_dir}）")
    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    try:
        subprocess.Popen([
            exe, f"--remote-debugging-port={port}", f"--user-data-dir={profile_dir}",
            "--profile-directory=Default",  # 取込プロファイルを確実に開く
            "https://claude.ai/", "https://chatgpt.com/", "https://gemini.google.com/",
            "https://studio.youtube.com/",
        ])
        return True
    except Exception as e:
        log(f"    ログイン用ブラウザ起動エラー: {e}")
        return False


def open_session(profile_dir: str, port: int = 9222, log=print, wait_sec: int = 180,
                 browser_exe: str = ""):
    """ログイン用ブラウザ(デバッグポート)にCDP接続し (pw, browser, context) を返す。"""
    from playwright.sync_api import sync_playwright

    endpoint = f"http://127.0.0.1:{port}"
    pw = sync_playwright().start()

    def _connect():
        try:
            return pw.chromium.connect_over_cdp(endpoint)
        except Exception:
            return None

    browser = _connect()
    if browser is None:
        log("ログイン用ブラウザを起動します。各サイト(claude.ai/ChatGPT/Gemini/YouTube)にログインしてください…")
        launch_login_chrome(profile_dir, port, log=log, browser_exe=browser_exe)
        end = time.time() + wait_sec
        while time.time() < end and browser is None:
            time.sleep(3)
            browser = _connect()
    if browser is None:
        pw.stop()
        raise RuntimeError(
            f"ログイン用Chrome(ポート{port})に接続できませんでした。"
            "他のChromeを閉じ、設定タブの『ブラウザ起動（ログイン用）』で再実行してください。")
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    return pw, browser, ctx


def _kill_chrome_on_port(port: int, log=print) -> None:
    try:
        ps = (
            "Get-CimInstance Win32_Process -Filter "
            "\"Name='chrome.exe' OR Name='msedge.exe' OR Name='brave.exe'\" | "
            f"Where-Object {{ $_.CommandLine -like '*--remote-debugging-port={port}*' }} | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, timeout=25)
    except Exception as e:
        log(f"      Chrome終了時の警告: {e}")


def reset_tabs(ctx, log=print, keep=None) -> None:
    try:
        pages = list(ctx.pages)
        if not pages:
            return
        keeper = keep if (keep in pages) else pages[0]
        if keeper is pages[0] and keep is None:
            try:
                keeper.goto("about:blank", wait_until="domcontentloaded", timeout=8000)
            except Exception:
                pass
        for p in pages:
            if p is keeper:
                continue
            try:
                p.close()
            except Exception:
                pass
    except Exception as e:
        log(f"    タブ整理の警告: {e}")


def close_session(pw, browser, port: int = 9222, log=print, kill: bool = False) -> None:
    try:
        if browser:
            browser.close()
    except Exception:
        pass
    try:
        if pw:
            pw.stop()
    except Exception:
        pass
    if kill:
        _kill_chrome_on_port(port, log)
        time.sleep(2)


# アプリの⏹停止をブラウザ待機ループへ伝えるフック。
# pipeline.run / upload_project が実行中だけ stop_flag を設定する。
STOP_CHECK = None


def _stopped() -> bool:
    try:
        return bool(STOP_CHECK and STOP_CHECK())
    except Exception:
        return False


def _wait_login(page, ready_selectors: list[str], label: str, log, max_sec: int = 240) -> bool:
    end = time.time() + max_sec
    asked = False
    while time.time() < end:
        if _stopped():
            return False
        for s in ready_selectors:
            try:
                el = page.query_selector(s)
                if el and el.is_visible():
                    return True
            except Exception:
                pass
        if not asked:
            log(f"    {label} にログインしてください（開いたChromeで手動ログイン・最大{max_sec}秒待機）…")
            asked = True
        page.wait_for_timeout(2000)
    return False


def _find(page, selectors):
    for s in selectors:
        try:
            el = page.query_selector(s)
            if el and el.is_visible():
                return el
        except Exception:
            continue
    return None


def _read_clipboard() -> str:
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; Get-Clipboard -Raw"],
            capture_output=True, timeout=15)
        b = r.stdout or b""
        for enc in ("utf-8", "cp932", "utf-16"):
            try:
                return b.decode(enc).strip()
            except Exception:
                continue
        return b.decode("utf-8", "ignore").strip()
    except Exception:
        return ""


PROBE_JS = r"""
(includeImgs) => {
  const info = (el) => ({ tag: el.tagName.toLowerCase(), id: el.id||'',
    cls:(el.className&&el.className.toString?el.className.toString().slice(0,90):''),
    role: el.getAttribute('role')||'', aria: el.getAttribute('aria-label')||'',
    ph: el.getAttribute('placeholder')||'', test: el.getAttribute('data-testid')||'',
    ce: el.getAttribute('contenteditable')||'', type: el.getAttribute('type')||'',
    text: ((el.innerText||el.value||'')+'').trim().slice(0,50) });
  const sel='textarea, input, [contenteditable], button, [role="textbox"]';
  const els=Array.from(document.querySelectorAll(sel)).slice(0,150).map(info);
  const out={url:location.href, title:document.title, elements:els};
  if(includeImgs){ out.images=Array.from(document.querySelectorAll('img')).slice(0,50)
    .map(i=>({src:(i.src||'').slice(0,160), w:i.naturalWidth, h:i.naturalHeight})); }
  return out; }
"""


def dump_page(page, out_dir, name: str, log=print, include_imgs: bool = False) -> None:
    import json
    try:
        d = Path(out_dir); d.mkdir(parents=True, exist_ok=True)
        try:
            page.screenshot(path=str(d / f"{name}.png"), full_page=True)
        except Exception:
            try:
                page.screenshot(path=str(d / f"{name}.png"))
            except Exception:
                pass
        try:
            data = page.evaluate(PROBE_JS, include_imgs)
        except Exception as e:
            data = {"error": str(e)}
        (d / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"      [診断] {name}.png / {name}.json を保存")
    except Exception as e:
        log(f"      [診断] ダンプ失敗: {e}")


# ───────── claude.ai テキスト生成（移植） ─────────
CLAUDE_INPUT = ['[data-testid="chat-input"]', 'div.ProseMirror[contenteditable="true"]',
                'div[contenteditable="true"]', '[contenteditable="true"]']
CLAUDE_SEND = ['button[aria-label*="送信"]', 'button[aria-label*="Send"]',
               'button[aria-label="メッセージを送信"]']
CLAUDE_STOP = ['button[aria-label*="停止"]', 'button[aria-label*="Stop"]',
               'button[aria-label="応答を停止"]']
# コピー ボタン（クリップボード経路の保険用）。2026-08-05のUI変更で
# data-testid="action-bar-copy" が消え aria-label のみになった（不具合報告4-2）
CLAUDE_COPY_SELECTORS = ['button[data-testid="action-bar-copy"]',
                         'button[aria-label="コピー"]', 'button[aria-label*="Copy"]']
CLAUDE_COPY = CLAUDE_COPY_SELECTORS[0]   # 後方互換（旧名を参照するコードが残っても壊れない）
#: 生成中か（claude.aiのメッセージコンテナ属性。生成中=true/完了=false。2026-08-05実測）
CLAUDE_STREAMING_JS = "() => !!document.querySelector('div[data-is-streaming=\"true\"]')"
#: 最後の回答本文。'.standard-markdown' は本文だけをクリーンに含む
#: （「N秒間思考しました」等の思考ヘッダが混ざらない）。コピー ボタン非依存。
CLAUDE_TEXT_JS = r"""
() => {
  const turns = document.querySelectorAll('div[data-is-streaming]');
  const last = turns.length ? turns[turns.length - 1] : null;
  if (last) {
    const md = last.querySelector('.standard-markdown');
    if (md) { const t = (md.innerText || '').trim(); if (t) return t; }
  }
  const mds = document.querySelectorAll('.standard-markdown');
  if (mds.length) {
    const t = (mds[mds.length - 1].innerText || '').trim(); if (t) return t;
  }
  return last ? (last.innerText || '').trim() : '';
}
"""
ASSISTANT_TEXT_JS = r"""
() => { const c=document.querySelectorAll(
    'button[data-testid="action-bar-copy"], button[aria-label="コピー"], button[aria-label*="Copy"]');
  if(!c.length) return ''; let el=c[c.length-1];
  for(let i=0;i<12&&el;i++){ el=el.parentElement; if(!el) break;
    const t=(el.innerText||'').trim(); if(t.length>120&&t.length<60000) return t; }
  return el?(el.innerText||'').trim():''; }
"""


def _claude_once(context, prompt: str, log, timeout: int, debug_dir=None) -> str:
    page = context.new_page()
    try:
        page.goto(CLAUDE_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
        if not _wait_login(page, CLAUDE_INPUT, "claude.ai", log):
            if debug_dir:
                dump_page(page, debug_dir, "claude_nologin", log)
            return ""
        editor = _find(page, CLAUDE_INPUT)
        if not editor:
            if debug_dir:
                dump_page(page, debug_dir, "claude_noinput", log)
            return ""
        editor.click()
        page.wait_for_timeout(300)
        page.keyboard.insert_text(prompt)
        page.wait_for_timeout(500)
        send = _find(page, CLAUDE_SEND)
        if send:
            send.click()
        else:
            editor.click(); page.keyboard.press("Enter")
        log("      claude: 生成待機中…")
        end = time.time() + timeout
        # ① 生成開始を待つ（streaming属性 / 停止ボタン / 本文出現のどれか）
        while time.time() < end:
            if _stopped():
                return ""
            try:
                if page.evaluate(CLAUDE_STREAMING_JS) or (page.evaluate(CLAUDE_TEXT_JS) or "").strip():
                    break
            except Exception:
                pass
            if _find(page, CLAUDE_STOP):
                break
            page.wait_for_timeout(500)
        # ② 完了を待つ: streamingが消え、本文が変化しなくなったら完了（UI変更に強い
        #    安定検知方式。旧実装はコピー ボタンのtestid消滅で永久に完了を検知できず、
        #    タイムアウトまで待って空を返していた＝2026-08-05不具合報告4-2）
        prev, stable = "", 0
        while time.time() < end:
            if _stopped():
                return ""
            try:
                streaming = bool(page.evaluate(CLAUDE_STREAMING_JS))
                cur = (page.evaluate(CLAUDE_TEXT_JS) or "").strip()
            except Exception:
                streaming, cur = True, ""
            if (not streaming) and (not _find(page, CLAUDE_STOP)) and cur and cur == prev:
                stable += 1
                if stable >= 2:      # 約1.6秒間 本文が変化しない＝完了
                    break
            else:
                stable = 0
            prev = cur
            page.wait_for_timeout(800)
        page.wait_for_timeout(800)
        # ③ 本文取得: DOM直読み（.standard-markdown＝本文だけ・クリップボード非依存）が本命。
        #    取れたら短くてもそれが正＝保険経路で「より長いもの」に置き換えない
        #    （親歩き保険は会話全体のゴミを拾うため、正しい短答を壊す。2026-08-05実測）
        text = ""
        try:
            text = (page.evaluate(CLAUDE_TEXT_JS) or "").strip()
        except Exception:
            pass
        # ④ 保険1（空のときだけ）: コピー ボタン（新旧セレクタ）→ クリップボード
        if not text:
            try:
                copies = []
                for sel in CLAUDE_COPY_SELECTORS:
                    copies = page.query_selector_all(sel)
                    if copies:
                        break
                if copies:
                    try:
                        copies[-1].scroll_into_view_if_needed(timeout=3000)
                    except Exception:
                        pass
                    copies[-1].click()
                    page.wait_for_timeout(1200)
                    text = _read_clipboard()
            except Exception as e:
                log(f"      クリップボード取得エラー: {e}")
        # ⑤ 保険2（それでも空のときだけ）: コピー ボタンの親を辿る旧方式
        if not text:
            try:
                text = (page.evaluate(ASSISTANT_TEXT_JS) or "").strip()
            except Exception:
                pass
        last = (text or "").strip()
        if last:
            log(f"      claude応答を取得（{len(last)}字）")
        elif debug_dir:
            dump_page(page, debug_dir, "claude_empty", log)
        return last
    finally:
        try:
            page.close()
        except Exception:
            pass


def claude_generate(context, prompt: str, log=print, timeout: int = 360, retries: int = 2,
                    debug_dir=None) -> str:
    for attempt in range(1, retries + 1):
        try:
            r = _claude_once(context, prompt, log, timeout, debug_dir=debug_dir)
            if r:
                return r
            log(f"      claude応答が空（{attempt}/{retries}）。リトライ…")
        except Exception as e:
            log(f"      claude生成エラー（{attempt}/{retries}）: {e}")
    return ""


# ───────── ChatGPT / Gemini テキスト生成（汎用） ─────────
CHATGPT_INPUT = ['#prompt-textarea', 'div[contenteditable="true"]', 'textarea']
CHATGPT_SEND = ['button[data-testid="send-button"]', 'button[aria-label*="送信"]',
                'button[aria-label*="Send"]']
CHATGPT_STOP = ['button[data-testid="stop-button"]', 'button[aria-label*="停止"]',
                'button[aria-label*="Stop"]']
# 2026-07 実地確認: ChatGPT（Pro/長考モデル）は「回答を仕上げ中」のまま数分〜無期限に
# 粘ることがあり、「今すぐ回答」を押すまで本文/画像が出ない（実測: 7分待っても出ず、
# クリック10秒後に画像出現）。待機ループから定期的に押して強制確定させる。
_ANSWER_NOW = ['button:has-text("今すぐ回答")', 'button:has-text("Answer now")']


def _click_answer_now(page, log=print) -> bool:
    for sel in _ANSWER_NOW:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click(timeout=3000)
                log("      長考中のため「今すぐ回答」で確定させます")
                return True
        except Exception:
            continue
    return False


def _clear_composer(page) -> None:
    """入力欄の復元下書きを消す（前回の途中入力がプロンプト先頭に混入する実害を確認済み）。"""
    try:
        page.keyboard.press("Control+a")
        page.keyboard.press("Delete")
    except Exception:
        pass
CHATGPT_TEXT_JS = r"""
() => { const ts=document.querySelectorAll('[data-message-author-role="assistant"]');
  if(ts.length) return (ts[ts.length-1].innerText||'').trim();
  const md=document.querySelectorAll('.markdown, .prose');
  return md.length?(md[md.length-1].innerText||'').trim():''; }
"""
GEMINI_INPUT = ['div.ql-editor[contenteditable="true"]', 'rich-textarea div[contenteditable="true"]',
                'div[contenteditable="true"]', 'textarea']
GEMINI_SEND = ['button[aria-label*="送信"]', 'button[aria-label*="Send"]',
               'button.send-button', 'button[mattooltip*="送信"]']
GEMINI_TEXT_JS = r"""
() => { const r=document.querySelectorAll('message-content, .model-response-text, .markdown');
  return r.length?(r[r.length-1].innerText||'').trim():''; }
"""


def _web_text_once(context, url, inputs, sends, stops, read_js, prompt, label, log,
                   timeout, debug_dir=None) -> str:
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
        if not _wait_login(page, inputs, label, log):
            if debug_dir:
                dump_page(page, debug_dir, f"{label}_nologin", log)
            return ""
        box = _find(page, inputs)
        if not box:
            if debug_dir:
                dump_page(page, debug_dir, f"{label}_noinput", log)
            return ""
        box.click()
        page.wait_for_timeout(300)
        _clear_composer(page)  # 復元下書きの混入防止
        page.keyboard.insert_text(prompt)
        page.wait_for_timeout(500)
        send = _find(page, sends)
        if send:
            try:
                send.click(timeout=4000)
            except Exception:
                page.keyboard.press("Enter")
        else:
            page.keyboard.press("Enter")
        log(f"      {label}: 生成待機中…")
        # 完了判定は2本立て:
        #  a) 停止ボタンを見たことがあり、それが消えた → 完了（従来・最速）
        #  b) 停止ボタンが検出できないDOMでも、本文が約6秒間伸びなければ完了
        #     （Gemini等のDOM変更で停止ボタンを見失うと、完了済みなのに
        #       タイムアウトまで待ち続けてしまうのを防ぐ）
        end = time.time() + timeout
        t_sent = time.time()
        saw_stop = False
        last_txt = ""
        stable = 0
        no_stop_seen = 0
        while time.time() < end:
            if _stopped():
                return ""
            # 長考モデル対策（ChatGPT）: 90秒過ぎても仕上げ中なら「今すぐ回答」で確定
            # （Gemini等ではセレクタが無くno-op）
            if time.time() - t_sent > 90:
                _click_answer_now(page, log)
            has_stop = bool(_find(page, stops))
            try:
                txt = (page.evaluate(read_js) or "").strip()
            except Exception:
                txt = ""
            if has_stop:
                saw_stop = True
                stable = 0
                no_stop_seen = 0
            elif saw_stop and txt:
                # a) 停止ボタンが消えた。ただし長考→回答のDOM再構築で一瞬消えることが
                # あるため、2回連続で不在＋本文が伸びていない時だけ確定する
                # （即confirmすると書きかけ台本を「完了」として取り込み、
                #  10分設定が2分・タイトル無題になる＝2026-08-17配布先報告⑤）
                no_stop_seen += 1
                if no_stop_seen >= 2 and txt == last_txt:
                    break
                last_txt = txt
                page.wait_for_timeout(1500)
                continue
            elif not txt or txt != last_txt:
                stable = 0
            else:
                stable += 1
                if stable >= 8:  # b) 約12秒本文が変わらない（章間の息継ぎで切らない）
                    break
            last_txt = txt
            page.wait_for_timeout(1500)
        page.wait_for_timeout(2000)
        try:
            text = (page.evaluate(read_js) or "").strip()
        except Exception:
            text = ""
        # 確定後の再読で本文がまだ伸びていた＝早すぎる確定 → 伸びが止まるまで追い読み。
        # 判定は絶対値（+50字）: 相対5%だと長文ほど検知に必要な速度が上がり、守るべき
        # 長文ケースで機能しない（敵対検証2026-08-17。50字≒実勢30〜100字/秒の1〜2秒分）
        try:
            grow_guard = 0
            while text and len(text) > len(last_txt) + 50 and grow_guard < 40 \
                    and time.time() < end:
                last_txt = text
                page.wait_for_timeout(3000)
                text = (page.evaluate(read_js) or "").strip() or text
                grow_guard += 1
        except Exception:
            pass
        if not text:
            try:
                copies = page.query_selector_all('button[aria-label*="コピー"], button[aria-label*="Copy"]')
                if copies:
                    copies[-1].click(); page.wait_for_timeout(1000)
                    text = _read_clipboard()
            except Exception:
                pass
        if text:
            log(f"      {label}応答を取得（{len(text)}字）")
        elif debug_dir:
            dump_page(page, debug_dir, f"{label}_empty", log)
        return text
    finally:
        try:
            page.close()
        except Exception:
            pass


def chatgpt_text(context, prompt, log=print, timeout=360, retries=2, debug_dir=None) -> str:
    for a in range(1, retries + 1):
        try:
            r = _web_text_once(context, CHATGPT_URL, CHATGPT_INPUT, CHATGPT_SEND, CHATGPT_STOP,
                               CHATGPT_TEXT_JS, prompt, "ChatGPT", log, timeout, debug_dir)
            if r:
                return r
        except Exception as e:
            log(f"      ChatGPT生成エラー（{a}/{retries}）: {e}")
    return ""


def gemini_text(context, prompt, log=print, timeout=360, retries=2, debug_dir=None) -> str:
    for a in range(1, retries + 1):
        try:
            r = _web_text_once(context, GEMINI_URL, GEMINI_INPUT, GEMINI_SEND, GEMINI_SEND,
                               GEMINI_TEXT_JS, prompt, "Gemini", log, timeout, debug_dir)
            if r:
                return r
        except Exception as e:
            log(f"      Gemini生成エラー（{a}/{retries}）: {e}")
    return ""


# ───────── ChatGPT 画像生成（移植） ─────────
# 2026-07-21 配布先報告の修正: 旧実装はページ全体の単一img走査＋送信前基準なしで、
# サイドバー（画像ライブラリ/履歴サムネ）の既存画像を「生成完了」と誤認し、
# 全シーンが同一の無関係画像になる事故が起きた → Gemini側と同じ
# 「配列で返す＋送信前baseline＋差分のみ採用」方式に統一。
CHATGPT_IMGS_JS = r"""
() => { const scope=document.querySelector('main')||document;  /* サイドバー除外 */
  const imgs=Array.from(scope.querySelectorAll('img'));
  const cand=imgs.filter(i=>/oaiusercontent|blob:|\/backend-api\//i.test(i.src||''));
  const pick=(cand.length?cand:imgs)
    .filter(i=>(i.naturalWidth||0)>=256&&(i.naturalHeight||0)>=256);
  return pick.map(i=>i.src||'').filter(Boolean); }
"""
# baseline用: ロード状態に関係なく main 内の全img srcを返す。
# （未ロードimgはnaturalWidth=0でCHATGPT_IMGS_JSに出ない→過去会話の画像が
#   読み込まれた瞬間に「新規生成」へ化ける遅延ロード穴を塞ぐ）
CHATGPT_ALLSRC_JS = r"""
() => { const scope=document.querySelector('main')||document;
  return Array.from(scope.querySelectorAll('img'))
    .map(i=>i.src||i.getAttribute('src')||'').filter(Boolean); }
"""


# ChatGPT「思考量」の項目（2026-08-25 UI・実DOM確認: スライダー0..4と同じ5段）
_CG_EFFORTS = ("最速", "中程度", "高い", "非常に高い", "Pro")
# 実DOM検証(2026-08-25): モデル/思考量の行は透明なコンポーザー容器(z-10)にヒットテストを
# 奪われ、実クリック/実ホバーではサブメニューが開かない。bubbles付きの合成PointerEventを
# 行要素へ直接dispatchするとReactが拾って開く（項目選択も同様）＝この2つが唯一動く経路
_CG_HOVER_JS = """(el) => {
    const b = el.getBoundingClientRect();
    const init = {bubbles: true, cancelable: true, composed: true, pointerType: 'mouse',
                  clientX: b.x + b.width / 2, clientY: b.y + b.height / 2, isPrimary: true};
    el.dispatchEvent(new PointerEvent('pointerover', init));
    el.dispatchEvent(new PointerEvent('pointerenter', init));
    el.dispatchEvent(new PointerEvent('pointermove', init));
}"""
_CG_CLICK_JS = """(el) => {
    const b = el.getBoundingClientRect();
    const init = {bubbles: true, cancelable: true, composed: true, pointerType: 'mouse',
                  clientX: b.x + b.width / 2, clientY: b.y + b.height / 2, isPrimary: true, button: 0};
    el.dispatchEvent(new PointerEvent('pointerover', init));
    el.dispatchEvent(new PointerEvent('pointerenter', init));
    el.dispatchEvent(new PointerEvent('pointermove', init));
    el.dispatchEvent(new PointerEvent('pointerdown', init));
    el.dispatchEvent(new MouseEvent('mousedown', init));
    el.dispatchEvent(new PointerEvent('pointerup', init));
    el.dispatchEvent(new MouseEvent('mouseup', init));
    el.dispatchEvent(new MouseEvent('click', init));
}"""


def _cg_match(cur: str, want: str, dim: str) -> bool:
    """現在値と希望値の照合。思考量は完全一致（「高い」⊂「非常に高い」の誤判定防止）、
    モデルは双方向の包含（選択中は「5.5」のような短縮表示になる・実DOM確認）。"""
    c = (cur or "").strip().lower()
    w = (want or "").strip().lower()
    if not c or not w:
        return False
    if dim == "思考量":
        return c == w
    return c in w or w in c
# Gemini 2026-08-25 UI: 入力欄右のチップ（表示=現在のモデル名）に出る短いラベル
_GM_CHIP_WORDS = ("Pro", "Flash", "Flash-Lite", "Fast", "Thinking",
                  "3.5 Flash-Lite", "3.7 Flash", "3.1 Pro")


def _first_visible(page, sels):
    for s in sels:
        try:
            el = page.query_selector(s)
            if el and el.is_visible():
                return el
        except Exception:
            continue
    return None


def _menu_item(page, want: str):
    """開いているメニューから want（部分一致）の項目を返す。

    実DOM検証(2026-08-25): Geminiの項目は GEM-MENU-ITEM[role="menuitem"]（div以外）＝
    タグ非依存の[role=...]で探す。ChatGPTのモデル/思考量の行（サブメニューの親・
    aria-haspopup=menu）は行のラベルに現在値が含まれ誤マッチするため除外する。"""
    for s in [f'[role="menuitemradio"]:has-text("{want}")',
              f'[role="option"]:has-text("{want}")',
              f'div[role="menuitem"]:has-text("{want}")',
              f'[role="menuitem"]:has-text("{want}")',
              f'button[role="menuitem"]:has-text("{want}")',
              f'button.mat-mdc-menu-item:has-text("{want}")']:
        try:
            els = page.query_selector_all(s) or []
        except Exception:
            continue
        for el in els:
            try:
                if not el.is_visible():
                    continue
                if (el.get_attribute("aria-haspopup") or "") == "menu":
                    continue   # サブメニューの親行は選択項目ではない
                return el
            except Exception:
                continue
    return None


def _close_menus(page):
    for _ in range(2):
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(250)
        except Exception:
            break


def _cg_effort_chip(page):
    """ChatGPT新UI（2026-08-25）: 入力欄右のチップ。表示は「高い」「5.5 高い」等モデルで変わるため
    クラス（__composer-pill）で掴むのが本命（実DOM確認）。テキスト・ariaは保険。"""
    el = _first_visible(page, ['form button[class*="__composer-pill"][aria-haspopup="menu"]',
                               'button[class*="__composer-pill"][aria-haspopup="menu"]'])
    if el is not None:
        return el
    for scope in ("form ", "main ", ""):
        for word in _CG_EFFORTS:
            try:
                els = page.query_selector_all(f'{scope}button:has-text("{word}")') or []
            except Exception:
                continue
            for el in els:
                try:
                    t = (el.inner_text() or "").strip().replace("\n", "")
                    if el.is_visible() and t.startswith(word) and len(t) <= len(word) + 2:
                        return el
                except Exception:
                    continue
        if scope == "form":
            continue
    return _first_visible(page, ['button[aria-label*="思考"]',
                                 'button[aria-label*="reasoning" i]',
                                 'button[aria-label*="effort" i]'])


def _cg_menu_row(page, label: str):
    """ChatGPT詳細設定メニューの行（「モデル …」「思考量 …」）。

    実DOM検証(2026-08-25): Escapeで閉じた直後は閉じアニメーション中の残骸メニューが
    まだ見えており、それを掴むと「開いている」と誤認して操作が空振りする
    → 親メニューが data-state="open" の行だけを有効とする。"""
    for s in [f'[role="menuitem"]:has-text("{label}")',
              f'[role="menuitemradio"]:has-text("{label}")',
              f'div[role="button"]:has-text("{label}")',
              f'button:has-text("{label}")']:
        try:
            els = page.query_selector_all(s) or []
        except Exception:
            continue
        for el in els:
            try:
                t = (el.inner_text() or "").strip()
                if not (el.is_visible() and t.startswith(label)):
                    continue
                try:
                    st = el.evaluate("el => {const m = el.closest('[role=\"menu\"]');"
                                     " return m ? (m.getAttribute('data-state') || 'open') : 'open';}")
                except Exception:
                    st = "open"
                if st == "open":
                    return el
            except Exception:
                continue
    return None


def _cg_open_advanced(page) -> bool:
    """ChatGPT新UI: 思考量チップ→「詳細設定」を開き、「モデル/思考量」の行が見える状態にする。"""
    if _cg_menu_row(page, "モデル") or _cg_menu_row(page, "思考量"):
        return True   # すでに開いている
    chip = _cg_effort_chip(page)
    if chip is None:
        return False
    try:
        chip.click(timeout=4000)
    except Exception:
        return False
    page.wait_for_timeout(700)
    if _cg_menu_row(page, "モデル") or _cg_menu_row(page, "思考量"):
        return True
    adv = _first_visible(page, ['[role="menuitem"]:has-text("詳細設定")',
                                'button:has-text("詳細設定")',
                                'div[role="button"]:has-text("詳細設定")',
                                'text=詳細設定',
                                '[role="menuitem"]:has-text("Advanced")',
                                'text=Advanced'])
    if adv is None:
        _close_menus(page)   # チップで開いたポップアップを残さない（開きっぱなし対策）
        return False
    try:
        adv.click(timeout=4000)
    except Exception:
        try:
            adv.evaluate(_CG_CLICK_JS)   # オーバーレイに覆われている時は合成クリック
        except Exception:
            _close_menus(page)
            return False
    page.wait_for_timeout(700)
    if _cg_menu_row(page, "モデル") or _cg_menu_row(page, "思考量"):
        return True
    _close_menus(page)
    return False


def _cg_pick(page, dim_label: str, want: str):
    """詳細設定の行（モデル/思考量）のサブメニューで want を選ぶ。

    実DOM検証(2026-08-25): 行は透明オーバーレイに覆われ実クリック/ホバー不可
    → 合成PointerEventのdispatchでサブメニューを開き、項目も合成クリックで選ぶ。
    戻り値: ("same", "")=すでに目的の設定 / ("ok", 切替前の表示) / ("noitem", 現在値) /
    ("norow", "")=行が見つからない。"""
    row = _cg_menu_row(page, dim_label)
    if row is None:
        return "norow", ""
    try:
        cur = (row.inner_text() or "").replace(dim_label, "", 1).strip()
        cur = (cur.splitlines() or [""])[0].strip()
    except Exception:
        cur = ""
    if _cg_match(cur, want, dim_label):
        return "same", ""
    item = None
    for _try in range(3):   # 合成ホバー＝サブメニューを開く（開かなければ引き直して再試行）
        try:
            row = _cg_menu_row(page, dim_label) or row
            row.evaluate(_CG_HOVER_JS)
            page.wait_for_timeout(900)
            item = _menu_item(page, want)
            if item is not None:
                break
            st = ""
            try:
                st = row.get_attribute("data-state") or ""
            except Exception:
                pass
            if st == "open":   # サブは開いたが目当ての項目名が無い＝リトライしても無駄
                break
        except Exception:
            page.wait_for_timeout(300)
    if item is None:
        try:   # 保険: 実ホバー（レイアウトが変わってオーバーレイが無い場合はこちらで開く）
            row.hover(timeout=2500)
            page.wait_for_timeout(700)
            item = _menu_item(page, want)
        except Exception:
            item = None
    if item is None:
        return "noitem", cur
    try:
        item.evaluate(_CG_CLICK_JS)     # 合成クリック＝選択（実機で切替を確認済み）
    except Exception:
        try:
            item.click(timeout=3000)
        except Exception:
            return "noitem", cur
    page.wait_for_timeout(700)
    # 反映確認（行の現在値が希望どおりになったか。読めない時は成功扱い＝フェイルオープン）
    try:
        row2 = _cg_menu_row(page, dim_label)
        if row2 is not None:
            now = (row2.inner_text() or "").replace(dim_label, "", 1).strip()
            now = (now.splitlines() or [""])[0].strip()
            if now and not _cg_match(now, want, dim_label):
                return "noitem", cur
    except Exception:
        pass
    return "ok", cur


def _select_chatgpt_advanced(page, want: str, log, debug_dir=None):
    """ChatGPT新UI（2026-08-25の「詳細設定」方式）でモデル/思考量を切り替える。

    戻り値: (handled, prev)。handled=False は新UIが見つからない＝旧UIを試してよい。"""
    dim = "思考量" if want in _CG_EFFORTS else "モデル"
    if not _cg_open_advanced(page):
        return False, None
    status, prev = _cg_pick(page, dim, want)
    if status in ("noitem", "norow") and debug_dir:
        dump_page(page, debug_dir, f"model_picker_chatgpt_{status}", log)   # 閉じる前に実DOMを保存
    _close_menus(page)
    if status == "same":
        return True, None
    if status == "ok":
        log(f"      ChatGPTの{dim}を「{want}」に切り替えました（画像生成用）")
        if not prev:
            log(f"      ※切替前の{dim}を読み取れなかったため、生成後の自動復元はできません"
                "（気になる時は画面の詳細設定で確認してください）")
            return True, None
        # 「どの行（次元）を戻すか」をタグで持ち回る＝復元時に文言のメンバーシップ推定をしない
        # （思考量の項目名が3語以外に変わった時にモデル行を誤操作しない・敵対検証2026-08-25）
        return True, f"{dim}::{prev}"
    if status == "noitem":
        log(f"      ※ChatGPTの{dim}メニューに「{want}」が見つかりません → 現在の設定のまま生成します"
            "（名前が変わった場合は作成タブ③の欄に新しい名前を直接入力）")
        return True, None
    # 行が見つからない（詳細設定は開けたがUIがさらに変わった）
    log("      ※ChatGPTの詳細設定メニューの形が変わっています → 現在の設定のまま生成します")
    return True, None


def _find_model_trigger(page, engine: str):
    """モデルピッカーの開閉ボタンを返す（見つからなければNone）。

    2026-08-17配布先報告「Instantにしても画面が変わっていて選択できない」→
    候補を広げる（見つからない時は呼び元が診断ダンプを保存＝実DOMで次回直せる）。"""
    if engine == "chatgpt":
        sels = ['button[data-testid="model-switcher-dropdown-button"]',
                '[data-testid*="model-switcher"] button',
                'button[data-testid*="model-switcher"]',
                'button[aria-label*="モデル"]',
                'button[aria-label*="Model" i]',
                'main button[aria-haspopup="menu"]:has-text("ChatGPT")',
                'header button:has-text("ChatGPT")']
    else:   # gemini
        sels = ['button[aria-label*="モード選択"]',      # 2026-08-25 実DOM: 「モード選択ツールを開く（現在のモデル: Pro）」
                'button[aria-label*="現在のモデル"]',
                'bard-mode-switcher button',
                'button[data-test-id="bard-mode-menu-button"]',
                'button[class*="mode-switch"]',
                'button[aria-label*="モデル"]']
    for s in sels:
        try:
            el = page.query_selector(s)
            if el and el.is_visible():
                return el
        except Exception:
            continue
    if engine != "chatgpt":
        # 2026-08-25 UI: 入力欄右の「Pro ▾」等のチップ（クラス名が無いので表示文字で探す）。
        # サイドバー等の紛れ（アップグレード案内など）を避けるため main 内を先に探す
        for scope in ("main button", "form button", "button"):
            hit = _gm_chip_in(page, scope)
            if hit is not None:
                return hit
    return None


def _gm_chip_in(page, scope: str):
    try:
        for el in page.query_selector_all(scope) or []:
                try:
                    if not el.is_visible():
                        continue
                    t = (el.inner_text() or "").strip().replace("\n", " ")
                except Exception:
                    continue
                if t and any(k == t or (k in t and len(t) <= len(k) + 3) for k in _GM_CHIP_WORDS):
                    return el
    except Exception:
        pass
    return None


def select_fast_model(page, engine: str, want: str, log=print, debug_dir=None):
    """画像生成の前にモデルピッカーで高速モデルを選ぶ（2026-08-10ユーザー要望）。

    長考モデル（Thinking/High/Pro等）だと画像1枚に数分かかるため、画像生成のチャット
    ではChatGPT=Instant / Gemini=Flash系を選ぶ。ピッカーのDOMは変わりやすいので
    **見つからなければ何もしない**（フェイルオープン＝従来どおり現在のモデルで生成。
    生成が失敗するわけではないので安全側）。すでに目的モデルなら触らない。
    want はメニュー項目の部分一致文字列（設定 web_image_model_* で変更可・空=無効）。
    戻り値: 切り替えた時=**切替前のトリガー表示テキスト**（生成後に restore_model で
    元へ戻すため。最後に選んだモデルは以後の新規チャットの既定になる＝戻さないと
    台本用チャットまで高速モデルで走ってしまう）/ 切替なし・失敗=None。
    """
    if not (want or "").strip():
        return None
    want = want.strip()
    if engine == "chatgpt":
        try:
            handled, prev = _select_chatgpt_advanced(page, want, log, debug_dir)
        except Exception as e:
            _close_menus(page)
            log(f"      ※モデル切替中にエラー（{str(e)[:60]}）→ 現在の設定のまま生成します")
            return None
        if handled:
            return prev
        # 新UI（思考量チップ）が見つからない → 従来のモデルスイッチャーを試す
    try:
        trig = _find_model_trigger(page, engine)
        if trig is None:
            log(f"      ※モデル選択ボタンが見つかりません → 現在のモデルのまま生成します"
                f"（希望: {want}）")
            if debug_dir:   # 実DOMを保存＝報告してもらえれば次版でセレクタを直せる
                dump_page(page, debug_dir, f"model_picker_{engine}_notrigger", log)
            return None
        prev = ""
        try:
            prev = (trig.inner_text() or "").strip()
            if want.lower() in prev.lower():
                return None   # すでに目的モデル
        except Exception:
            pass
        trig.click(timeout=4000)
        page.wait_for_timeout(800)
        item = _menu_item(page, want)
        if item is None:
            log(f"      ※モデル「{want}」がメニューに見つかりません → 現在のモデルのまま"
                "生成します（名前が変わった場合は settings.json の web_image_model_* で調整）")
            if debug_dir:   # 開いたメニューごと保存＝実際の項目名が分かる
                dump_page(page, debug_dir, f"model_picker_{engine}_noitem", log)
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            return None
        item.click(timeout=4000)
        page.wait_for_timeout(800)
        log(f"      モデルを「{want}」に切り替えました（画像生成を高速化）")
        return prev or "(不明)"
    except Exception as e:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        log(f"      ※モデル切替中にエラー（{str(e)[:60]}）→ 現在のモデルのまま生成します")
        return None


def restore_model(page, engine: str, prev: str, log=print) -> None:
    """画像生成が終わったらモデルを元に戻す（2026-08-10敵対検証: 最後に選んだモデルは
    アカウントの既定として次の新規チャットへ引き継がれるため、戻さないと以後の
    台本・ブラッシュアップまで高速モデルで生成されてしまう）。

    prev = select_fast_model が返した切替前のトリガー表示（例「ChatGPT 5.6 Thinking」）。
    メニューを開き、表示テキスト（1行目）が prev に含まれる項目のうち最長のものを選ぶ。
    特定できなければ案内ログを出して放置（生成物は既に取得済み＝実害は台本側のみ）。
    """
    if not (prev or "").strip():
        return
    prev = prev.strip()
    if engine == "chatgpt":
        if prev == "(不明)":   # 切替前を読めていない＝復元先が分からない（旧版の値の保険）
            return
        try:
            if "::" in prev:   # 新UIの切替が付けた次元タグ（例「思考量::中程度」）
                dim, prev = prev.split("::", 1)
                prev = prev.strip() or prev
            else:
                dim = "思考量" if prev in _CG_EFFORTS else "モデル"
            if _cg_open_advanced(page):
                status, _cur = _cg_pick(page, dim, prev)
                _close_menus(page)
                if status == "ok":
                    log(f"      ChatGPTの{dim}を元（{prev}）に戻しました（台本用チャットへの影響防止）。")
                    return
                if status == "same":
                    return
                log(f"      ※ChatGPTの{dim}を元（{prev}）に戻せませんでした。"
                    "台本の生成がおかしい時は画面の詳細設定で確認してください。")
                return
        except Exception:
            _close_menus(page)
        # 新UIで戻せない場合は従来のモデルスイッチャーへ
    try:
        trig = _find_model_trigger(page, engine)
        if trig is None:
            log("      ※モデルを元に戻せませんでした。台本の生成が速いモデルのままに"
                "なっていたら、画面のモデル選択で戻してください。")
            return
        try:
            cur = (trig.inner_text() or "").strip()
            if cur and cur == prev:
                return   # すでに元どおり
        except Exception:
            pass
        trig.click(timeout=4000)
        page.wait_for_timeout(800)
        best = None
        for s in ['[role="menuitemradio"]', '[role="option"]', 'div[role="menuitem"]',
                  '[role="menuitem"]', 'button[role="menuitem"]', 'button.mat-mdc-menu-item']:
            try:
                els = page.query_selector_all(s) or []
            except Exception:
                continue
            for el in els:
                try:
                    if not el.is_visible():
                        continue
                    if (el.get_attribute("aria-haspopup") or "") == "menu":
                        continue   # サブメニューの親行は選択項目ではない（2026-08-25実DOM）
                    t = ((el.inner_text() or "").splitlines() or [""])[0].strip()
                except Exception:
                    continue
                # t in prev: 旧トリガー表示（例 ChatGPT 5.6 Instant）に項目名が含まれる形
                # prev in t: 新チップ表示（例 Pro）が項目名（例 3.1 Pro）に含まれる形（2026-08-25 UI）
                if t and (t in prev or prev in t) and (best is None or len(t) > len(best[0])):
                    best = (t, el)
        if best is None:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            log(f"      ※元のモデル（{prev}）の項目を特定できませんでした。"
                "台本用チャットのモデルを画面で確認してください。")
            return
        best[1].click(timeout=4000)
        page.wait_for_timeout(500)
        log(f"      モデルを元（{best[0]}）に戻しました（台本用チャットへの影響防止）。")
    except Exception:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        log("      ※モデルの復元中にエラー。台本用チャットのモデルを画面で確認してください。")


def chatgpt_image(context, prompt: str, out_path: Path, log=print, timeout: int = 360,
                  size_w: int = 1920, size_h: int = 1080, debug_dir=None,
                  model: str = "", effort: str = "") -> bool:
    page = context.new_page()
    prev_model = None
    prev_effort = None
    try:
        page.goto(CHATGPT_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
        if not _wait_login(page, CHATGPT_INPUT, "ChatGPT", log):
            if debug_dir:
                dump_page(page, debug_dir, "chatgpt_nologin", log, include_imgs=True)
            return False
        prev_model = select_fast_model(page, "chatgpt", model, log, debug_dir=debug_dir)
        if (effort or "").strip():   # 思考量（最速/中程度/高い・2026-08-25 UI）
            prev_effort = select_fast_model(page, "chatgpt", effort.strip(), log, debug_dir=debug_dir)
        # 2026-07-23 実地調査の真相: 旧フォールバック（aria-labelに「画像」を含む要素を
        # 拾う広域セレクタ）がサイドバーの履歴リンク（タイトルに「画像」を含む過去会話。
        # 本ツールの生成履歴「画像生成リクエスト」等がまさに該当）に一致し、過去会話へ
        # 移動→そこへ送信→遅延ロードで現れた過去画像を「新規生成」と誤採用していた。
        # → クリックは composer のある main 内のチップに限定。無ければ何もしない
        #   （プロンプト文中の「生成してください」だけで画像ツールは起動する）。
        for s in ['main button:has-text("画像を作成")', 'main button:has-text("Create image")']:
            try:
                el = page.query_selector(s)
                if el and el.is_visible():
                    el.click(timeout=5000); page.wait_for_timeout(1200); break
            except Exception:
                continue
        # 保険: 万一 過去会話(/c/…)に居たら新規チャットへ戻す（過去画像の混入源を断つ）
        try:
            if "/c/" in (page.url or ""):
                log("      ※過去の会話を検出。新規チャットへ戻します")
                page.goto(CHATGPT_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(2000)
        except Exception:
            pass
        box = _find(page, CHATGPT_INPUT)
        if not box:
            if debug_dir:
                dump_page(page, debug_dir, "chatgpt_noinput", log, include_imgs=True)
            return False
        try:
            box.click(timeout=5000)
        except Exception:
            try:
                page.evaluate("()=>{const e=document.querySelector('#prompt-textarea')"
                              "||document.querySelector('div[contenteditable=\"true\"]')"
                              "||document.querySelector('textarea'); if(e)e.focus();}")
            except Exception:
                pass
        page.wait_for_timeout(300)
        _clear_composer(page)  # 復元下書きの混入防止
        # 送信前に「いま画面にある画像」を記録＝既存画像を新規生成と誤認しない。
        # ロード未完のimgも含める全src方式＋1秒あけて2回取り和集合（遅延ロード対策）
        try:
            baseline = set(page.evaluate(CHATGPT_ALLSRC_JS) or [])
            page.wait_for_timeout(1000)
            baseline |= set(page.evaluate(CHATGPT_ALLSRC_JS) or [])
        except Exception:
            baseline = set()
        page.keyboard.insert_text(prompt + "\nこの内容で画像を1枚だけ生成してください。")
        page.wait_for_timeout(800)
        send = _find(page, CHATGPT_SEND)
        if send:
            try:
                send.click(timeout=4000)
            except Exception:
                page.keyboard.press("Enter")
        else:
            page.keyboard.press("Enter")
        log(f"      送信。画像生成を待機（最大{timeout}秒）…")
        end = time.time() + timeout
        send_time = time.time()
        min_wait = 15  # 生成は最短でも数十秒＝直後の誤ヒットを防ぐ保険
        src = ""; waited = 0
        while time.time() < end:
            if _stopped():
                return False
            if time.time() - send_time < min_wait:
                page.wait_for_timeout(3000); waited += 3
                continue
            # 長考モデル対策: 60秒経っても仕上げ中なら「今すぐ回答」で確定させる
            if time.time() - send_time > 60:
                _click_answer_now(page, log)
            try:
                cur = page.evaluate(CHATGPT_IMGS_JS) or []
            except Exception:
                cur = []
            fresh = [s for s in cur if s not in baseline]
            if fresh:
                page.wait_for_timeout(2500)  # 高解像度版への差し替え待ち
                try:
                    cur = page.evaluate(CHATGPT_IMGS_JS) or cur
                except Exception:
                    pass
                fresh = [s for s in cur if s not in baseline] or fresh
                src = fresh[-1]
                break
            page.wait_for_timeout(3000); waited += 3
            if waited % 30 == 0:
                log(f"      …まだ生成中（{waited}秒）")
        if not src:
            if debug_dir:
                dump_page(page, debug_dir, "chatgpt_noimage", log, include_imgs=True)
            return False
        return download_image(page, context, src, out_path, size_w, size_h, log)
    finally:
        try:   # 成否に関わらずモデル・思考量を元へ（台本用チャットの既定を汚さない）
            restore_model(page, "chatgpt", prev_effort, log)
        except Exception:
            pass
        try:
            restore_model(page, "chatgpt", prev_model, log)
        except Exception:
            pass
        try:
            page.close()
        except Exception:
            pass


# ───────── 生成画像のダウンロード（ChatGPT/Gemini共通） ─────────
_CANVAS_JS = r"""(src) => {
  const img = Array.from(document.querySelectorAll('img')).find(i => i.src === src);
  if (!img || !(img.naturalWidth > 0)) return '';
  const c = document.createElement('canvas');
  c.width = img.naturalWidth; c.height = img.naturalHeight;
  c.getContext('2d').drawImage(img, 0, 0);
  return c.toDataURL('image/png');
}"""

_FETCH_JS = r"""async (u)=>{const r=await fetch(u);const b=await r.blob();
   return await new Promise(res=>{const fr=new FileReader();
   fr.onload=()=>res(fr.result);fr.readAsDataURL(b);});}"""


def _save_data_url(data_url: str, out_path: Path, size_w: int, size_h: int) -> bool:
    if data_url and "," in data_url:
        out_path.write_bytes(base64.b64decode(data_url.split(",", 1)[1]))
        util.resize_cover(out_path, size_w, size_h)
        return True
    return False


def download_image(page, context, src: str, out_path: Path,
                   size_w: int, size_h: int, log=print) -> bool:
    """生成画像のDL。http→認証付きAPIリクエスト、blob→canvas取り出し（fetchはCSPで
    落ちるサイトがあるため使わない）→fetch→画像要素スクショ、の順で粘る。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if src.startswith("http"):
        try:
            resp = context.request.get(src, timeout=60000)
            if resp.ok:
                out_path.write_bytes(resp.body())
                util.resize_cover(out_path, size_w, size_h)
                return True
            log(f"    画像DL失敗(http {resp.status})")
        except Exception as e:
            log(f"    画像DL失敗(http): {str(e)[:120]}")
    try:
        if _save_data_url(page.evaluate(_CANVAS_JS, src) or "", out_path, size_w, size_h):
            return True
    except Exception as e:
        log(f"    画像DL失敗(canvas): {str(e)[:120]}")
    try:
        if _save_data_url(page.evaluate(_FETCH_JS, src) or "", out_path, size_w, size_h):
            return True
    except Exception as e:
        log(f"    画像DL失敗(blob): {str(e)[:120]}")
    try:  # 最終手段: 画像要素そのもののスクリーンショット（表示解像度に落ちるが確実）
        el = page.query_selector(f'img[src="{src}"]')
        if el:
            el.scroll_into_view_if_needed()
            out_path.write_bytes(el.screenshot(type="png"))
            util.resize_cover(out_path, size_w, size_h)
            log("      （fetch不可のため画像要素のスクリーンショットで代替）")
            return True
    except Exception as e:
        log(f"    画像DL失敗(shot): {str(e)[:120]}")
    return False


# ───────── Gemini 画像生成 ─────────
GEMINI_IMGS_JS = r"""
() => { const imgs=Array.from(document.querySelectorAll('img'));
  const cand=imgs.filter(i=>/googleusercontent|blob:|\/generated/i.test(i.src||''));
  const pick=(cand.length?cand:imgs).filter(i=>(i.naturalWidth||0)>=256&&(i.naturalHeight||0)>=256);
  return pick.map(i=>i.src||'').filter(Boolean); }
"""


def gemini_image(context, prompt: str, out_path: Path, log=print, timeout: int = 360,
                 size_w: int = 1920, size_h: int = 1080, debug_dir=None,
                 model: str = "", effort: str = "") -> bool:
    """gemini.google.com で画像を1枚生成してDLする（要ログイン・DOM変更で要調整）。"""
    page = context.new_page()
    prev_model = None
    try:
        page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
        if not _wait_login(page, GEMINI_INPUT, "Gemini", log):
            if debug_dir:
                dump_page(page, debug_dir, "gemini_nologin", log, include_imgs=True)
            return False
        prev_model = select_fast_model(page, "gemini", model, log, debug_dir=debug_dir)
        try:
            baseline = set(page.evaluate(GEMINI_IMGS_JS) or [])
        except Exception:
            baseline = set()
        box = _find(page, GEMINI_INPUT)
        if not box:
            if debug_dir:
                dump_page(page, debug_dir, "gemini_noinput", log, include_imgs=True)
            return False
        box.click()
        page.wait_for_timeout(300)
        page.keyboard.insert_text("次の内容で画像を1枚だけ生成してください（説明文は不要・画像のみ）:\n" + prompt)
        page.wait_for_timeout(600)
        send = _find(page, GEMINI_SEND)
        if send:
            try:
                send.click(timeout=4000)
            except Exception:
                page.keyboard.press("Enter")
        else:
            page.keyboard.press("Enter")
        log(f"      送信。Gemini画像の生成を待機（最大{timeout}秒）…")
        end = time.time() + timeout
        src = ""; waited = 0
        while time.time() < end:
            if _stopped():
                return False
            try:
                cur = page.evaluate(GEMINI_IMGS_JS) or []
            except Exception:
                cur = []
            fresh = [s for s in cur if s not in baseline]
            if fresh:
                page.wait_for_timeout(2500)  # 高解像度への差し替え待ち
                try:
                    cur = page.evaluate(GEMINI_IMGS_JS) or cur
                except Exception:
                    pass
                fresh = [s for s in cur if s not in baseline] or fresh
                src = fresh[-1]
                break
            page.wait_for_timeout(3000); waited += 3
            if waited % 30 == 0:
                log(f"      …まだ生成中（{waited}秒）")
        if not src:
            if debug_dir:
                dump_page(page, debug_dir, "gemini_noimage", log, include_imgs=True)
            return False
        return download_image(page, context, src, out_path, size_w, size_h, log)
    finally:
        try:   # 成否に関わらずモデルを元へ（台本用チャットの既定を汚さない）
            restore_model(page, "gemini", prev_model, log)
        except Exception:
            pass
        try:
            page.close()
        except Exception:
            pass


# ───────── 動画生成（Flow/Sora/Gemini）= HITL（人の確認つき） ─────────
# ───────── Gemini(Chrome) 動画生成（完全自動） ─────────
# 2026-07-25 実地調査で確定した手順（実際に1本生成して検証済み）:
#   ＋「アップロードとツール」→「動画を作成」→（初回のみ）説明モーダルを「試してみる」で閉じる
#   → 入力欄の placeholder が「動画の説明を入力」に変わる＝動画モードの確証
#   → プロンプト送信 → 約220秒で <video> が出現 → src は素のHTTPS
#      （contribution.usercontent.google.com/download?c=…）＝そのままDLできる
GEMINI_VIDEO_JS = r"""
() => {
  const main = document.querySelector('main') || document;
  return Array.from(main.querySelectorAll('video'))
    .map(v => ({src: v.src || v.currentSrc || '', w: v.videoWidth || 0,
                h: v.videoHeight || 0, ready: v.readyState}))
    .filter(v => v.src);
}
"""


def _gemini_enter_video_mode(page, log=print) -> bool:
    """Geminiのcomposerを「動画」モードにする。成功時True。"""
    tool = None
    for _ in range(12):  # 起動直後は描画が遅い
        tool = page.query_selector('button[aria-label*="アップロードとツール"]')
        if tool and tool.is_visible():
            break
        page.wait_for_timeout(2500)
    if not tool:
        log("      Geminiの『アップロードとツール』が見つかりません（ログインを確認）")
        return False
    try:
        tool.click(timeout=6000)
    except Exception:
        return False
    page.wait_for_timeout(1500)
    # メニュー表記のゆれに対応（2026-08-17配布先報告「メニュー『動画を作成』がありません」＝
    # UI改版で文言が変わった可能性）: 動画を作成/Veo/短い「動画」/英語表記まで許容
    hit = False
    for it in page.query_selector_all('.cdk-overlay-container button, [role="menuitem"], '
                                      '.cdk-overlay-container [role="option"]'):
        try:
            t = (it.text_content() or "").strip()
        except Exception:
            continue
        if not t:
            continue
        tl = t.lower()
        if ("動画を作成" in t or "veo" in tl or "video" in tl
                or (len(t) <= 12 and "動画" in t)):
            try:
                it.click(timeout=6000)
                hit = True
                break
            except Exception:
                continue
    if not hit:
        log("      メニューに『動画を作成』がありません（提供状況を確認してください）")
        page.keyboard.press("Escape")
        return False
    page.wait_for_timeout(2500)
    # 初回だけ出る説明モーダル。閉じないと入力欄がオーバーレイに覆われて操作できない
    try:
        b = page.query_selector('.cdk-overlay-container button:has-text("試してみる")')
        if b and b.is_visible():
            b.click(timeout=5000)
        else:
            page.keyboard.press("Escape")
    except Exception:
        pass
    page.wait_for_timeout(2000)
    ph = ""
    try:
        ph = page.evaluate("""() => { const e = document.querySelector('div.ql-editor');
            return e ? (e.getAttribute('data-placeholder') || '') : ''; }""") or ""
    except Exception:
        pass
    if "動画" not in ph:
        log(f"      動画モードに切り替わりませんでした（入力欄の表示: {ph or '不明'}）")
        return False
    return True


def gemini_video(context, prompt: str, out_path: Path, log=print, wait_sec: int = 600,
                 debug_dir=None) -> bool:
    """Gemini(Chrome)で動画を1本生成してout_pathに保存する（人の操作は不要）。"""
    page = context.new_page()
    try:
        page.goto(GEMINI_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(6000)
        if not _wait_login(page, GEMINI_INPUT, "Gemini", log):
            if debug_dir:
                dump_page(page, debug_dir, "gemini_video_nologin", log)
            return False
        if not _gemini_enter_video_mode(page, log):
            if debug_dir:
                dump_page(page, debug_dir, "gemini_video_nomode", log)
            return False
        try:  # 送信前にすでに在る動画を記録（前の生成物を新作と誤認しない）
            before = {v["src"] for v in (page.evaluate(GEMINI_VIDEO_JS) or [])}
        except Exception:
            before = set()
        box = _find(page, GEMINI_INPUT)
        if not box:
            return False
        box.click(timeout=5000)
        page.wait_for_timeout(300)
        _clear_composer(page)
        page.keyboard.insert_text(prompt)
        page.wait_for_timeout(800)
        page.keyboard.press("Enter")
        log(f"      Gemini動画: 生成待機中（実測で約3〜4分・最大{wait_sec}秒）…")
        end = time.time() + wait_sec
        src = ""
        waited = 0
        while time.time() < end:
            if _stopped():
                return False
            page.wait_for_timeout(5000)
            waited += 5
            try:
                fresh = [v for v in (page.evaluate(GEMINI_VIDEO_JS) or [])
                         if v["src"] and v["src"] not in before]
            except Exception:
                fresh = []
            if fresh:
                page.wait_for_timeout(2500)  # 読み込み完了を少し待つ
                src = fresh[-1]["src"]
                log(f"      動画ができました（{fresh[-1]['w']}x{fresh[-1]['h']}・{waited}秒）")
                break
            if waited % 60 == 0:
                log(f"      …生成中（{waited}秒）")
        if not src:
            log("      時間内に動画ができませんでした。")
            if debug_dir:
                dump_page(page, debug_dir, "gemini_video_timeout", log)
            return False
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        try:  # srcは素のHTTPS＝そのまま取得できる
            resp = context.request.get(src, timeout=180000)
            if resp.ok:
                out_path.write_bytes(resp.body())
                if out_path.stat().st_size > 10000:
                    log(f"      保存: {out_path.name}（{out_path.stat().st_size // 1024}KB）")
                    return True
            log(f"      動画のDLに失敗（HTTP {resp.status}）")
        except Exception as e:
            log(f"      動画のDLに失敗: {str(e)[:100]}")
        return False
    finally:
        try:
            page.close()
        except Exception:
            pass


def video_generate_hitl(context, url: str, prompt: str, out_path: Path, label: str,
                        log=print, wait_sec: int = 600, debug_dir=None) -> bool:
    """サイトを開いてプロンプトを入れ、生成された動画を人の操作も交えてDLする。

    動画生成系は完了検知/DLが極めて不安定なため、プロンプト投入まで自動化し、
    生成・選択・ダウンロードは人が行う前提（チェックポイントで待つ）。
    ダウンロードイベントを掴めたら out_path に保存する。
    """
    page = context.new_page()
    got = {"path": None}

    def _on_download(d):
        try:
            d.save_as(str(out_path))
            got["path"] = str(out_path)
            log(f"      動画ダウンロードを保存: {out_path.name}")
        except Exception as e:
            log(f"      DL保存エラー: {e}")

    try:
        page.on("download", _on_download)
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        # プロンプト投入（best-effort）
        box = _find(page, ['textarea', 'div[contenteditable="true"]', '[role="textbox"]'])
        if box:
            try:
                box.click(); page.wait_for_timeout(300)
                page.keyboard.insert_text(prompt)
                log(f"      {label} にプロンプトを入力しました。生成→ダウンロードを行ってください。")
            except Exception:
                pass
        else:
            log(f"      {label} の入力欄が見つかりません。手動でプロンプトを貼ってください。")
        if debug_dir:
            dump_page(page, debug_dir, f"{label}_open", log, include_imgs=True)
        # 人がDLするまで待つ（download イベント or 既存ファイル）
        end = time.time() + wait_sec
        while time.time() < end and not got["path"]:
            if _stopped():
                return False
            page.wait_for_timeout(2000)
        return bool(got["path"] and Path(got["path"]).exists())
    finally:
        try:
            page.close()
        except Exception:
            pass
