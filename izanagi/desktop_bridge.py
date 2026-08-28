"""デスクトップアプリ連携（ChatGPT/Codex・Claude Code 等）の橋渡し。

外部依存を足さず、Windows標準API(ctypes)だけで:
  - クリップボードの読み書き（CF_UNICODETEXT・ワーカースレッドから安全）
  - ウィンドウをタイトル部分一致で前面化
  - Ctrl+V→Enter のキー送出（プロンプトを貼って送信）
を行う。台本など「テキスト」はクリップボード往復、「画像」は保存フォルダ監視で受け取る。

※ デスクトップアプリには外部操作の公式APIが無いため、送信は自動・受け取りは
  「アプリのコピー(または画像保存)を1回押す」半自動になる（UI変更に強い最小構成）。
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import time
from ctypes import wintypes
from pathlib import Path

_IS_WIN = (ctypes.__name__ == "ctypes") and hasattr(ctypes, "windll")

if _IS_WIN:
    _u32 = ctypes.windll.user32
    _k32 = ctypes.windll.kernel32
    _CF_UNICODETEXT = 13
    _GMEM_MOVEABLE = 0x0002
    # 64bitでハンドル(=ポインタ)が切れないよう型を明示
    _k32.GlobalAlloc.restype = wintypes.HGLOBAL
    _k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    _k32.GlobalLock.restype = wintypes.LPVOID
    _k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    _k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    _u32.GetClipboardData.restype = wintypes.HANDLE
    _u32.GetClipboardData.argtypes = [wintypes.UINT]
    _u32.SetClipboardData.restype = wintypes.HANDLE
    _u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]


def available() -> bool:
    return bool(_IS_WIN)


# ── クリップボード ──────────────────────────────────────────────────────
def get_clipboard_text() -> str:
    if not _IS_WIN or not _u32.OpenClipboard(None):
        return ""
    try:
        h = _u32.GetClipboardData(_CF_UNICODETEXT)
        if not h:
            return ""
        p = _k32.GlobalLock(h)
        if not p:
            return ""
        try:
            return ctypes.wstring_at(p)
        finally:
            _k32.GlobalUnlock(h)
    finally:
        _u32.CloseClipboard()


def set_clipboard_text(s: str) -> bool:
    if not _IS_WIN or not _u32.OpenClipboard(None):
        return False
    try:
        _u32.EmptyClipboard()
        data = (s or "").encode("utf-16-le") + b"\x00\x00"
        h = _k32.GlobalAlloc(_GMEM_MOVEABLE, len(data))
        p = _k32.GlobalLock(h)
        ctypes.memmove(p, data, len(data))
        _k32.GlobalUnlock(h)
        _u32.SetClipboardData(_CF_UNICODETEXT, h)
        return True
    finally:
        _u32.CloseClipboard()


# ── ウィンドウ前面化 ────────────────────────────────────────────────────
# ブラウザのタブ（例「ChatGPT - Google Chrome」）はデスクトップアプリと誤認しやすい
# → 除外する（Webエンジンは browser_ai が担当。ここはデスクトップアプリ専用）
_BROWSER_TITLE_MARKERS = (" - google chrome", " - microsoft edge", " - brave",
                          "mozilla firefox", " - opera", " - vivaldi", " - chromium")


def find_window(title_substr: str):
    """タイトルに title_substr を含む可視ウィンドウのハンドルを返す（無ければNone）。

    完全一致 > 前方一致 > 部分一致 の順で最良の1個を選ぶ（Z順の偶然に依存しない）。"""
    if not _IS_WIN or not title_substr:
        return None
    target = title_substr.lower()
    exact, prefix, part = [], [], []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _l):
        if not _u32.IsWindowVisible(hwnd):
            return True
        n = _u32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        _u32.GetWindowTextW(hwnd, buf, n + 1)
        t = buf.value.lower()
        if target not in t or any(mk in t for mk in _BROWSER_TITLE_MARKERS):
            return True
        (exact if t == target else prefix if t.startswith(target) else part).append(hwnd)
        return True

    _u32.EnumWindows(_cb, 0)
    for group in (exact, prefix, part):
        if group:
            return group[0]
    return None


def activate_window(title_substr: str):
    """対象を前面化して hwnd を返す（前面化を確認できなければ None）。

    背面プロセスからの SetForegroundWindow はWindowsのフォアグラウンドロックで
    黙って失敗する（タスクバー点滅のみ）→ ALT空打ちでロックを解き、
    GetForegroundWindow で「本当に前面になったか」を毎回検証する。"""
    hwnd = find_window(title_substr)
    if not hwnd:
        return None
    VK_MENU = 0x12
    for _ in range(3):
        _key(VK_MENU); _key(VK_MENU, True)  # フォアグラウンドロック解除
        _u32.ShowWindow(hwnd, 9)            # SW_RESTORE
        _u32.SetForegroundWindow(hwnd)
        time.sleep(0.4)
        if _u32.GetForegroundWindow() == hwnd:
            return hwnd
    return None


def _key(vk: int, up: bool = False):
    _u32.keybd_event(vk, 0, 2 if up else 0, 0)


def _user_idle_sec() -> float:
    """ユーザーが最後にマウス/キーボードを触ってからの秒数（取得不能時は大きい値）。"""
    if not _IS_WIN:
        return 999.0
    try:
        class _LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]
        lii = _LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(lii)
        if not _u32.GetLastInputInfo(ctypes.byref(lii)):
            return 999.0
        return max(0.0, (_k32.GetTickCount() - lii.dwTime) / 1000.0)
    except Exception:
        return 999.0


def send_hotkey(expect_hwnd, hotkey: str) -> bool:
    """前面のアプリへ任意のホットキー（例 ctrl+shift+o）を送る（前面検証つき）。"""
    if not _IS_WIN:
        return False
    if expect_hwnd is not None and _u32.GetForegroundWindow() != expect_hwnd:
        return False
    vk_map = {"ctrl": 0x11, "shift": 0x10, "alt": 0x12}
    parts = [p.strip().lower() for p in (hotkey or "").split("+") if p.strip()]
    if not parts or len(parts[-1]) != 1:
        return False
    mods = [vk_map[p] for p in parts[:-1] if p in vk_map]
    vk = ord(parts[-1].upper())
    for m in mods:
        _key(m)
    _key(vk); _key(vk, True)
    for m in reversed(mods):
        _key(m, True)
    return True


def send_copy_hotkey(expect_hwnd, hotkey: str = "ctrl+shift+c") -> bool:
    """前面のアプリへ「最後の回答をコピー」ショートカットを送る（前面検証つき）。

    ChatGPT系アプリは Ctrl+Shift+C が「直前の回答をコピー」。これで人が
    コピーボタンを押さなくても回答を取り込める（押した場合も従来どおり有効）。"""
    return send_hotkey(expect_hwnd, hotkey)


def send_paste_and_enter(expect_hwnd=None) -> bool:
    """アクティブウィンドウへ Ctrl+V → Enter を送る（プロンプト貼付→送信）。

    expect_hwnd を渡すと、キー送出の直前ごとに前面ウィンドウが対象のままかを検証する
    ＝ユーザーが使っている別アプリへ貼り付けて送信してしまう事故の防止。"""
    if not _IS_WIN:
        return False
    VK_CONTROL, VK_V, VK_RETURN = 0x11, 0x56, 0x0D
    if expect_hwnd is not None and _u32.GetForegroundWindow() != expect_hwnd:
        return False
    _key(VK_CONTROL); _key(VK_V); _key(VK_V, True); _key(VK_CONTROL, True)
    time.sleep(0.25)
    if expect_hwnd is not None and _u32.GetForegroundWindow() != expect_hwnd:
        return False
    _key(VK_RETURN); _key(VK_RETURN, True)
    return True


# ── アプリの自動起動 ────────────────────────────────────────────────────
def _launchable_links(name_hints: list[str]):
    """スタートメニューから名前が合う .lnk を探す（完全一致→前方一致→部分一致）。"""
    import os
    dirs = [Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs",
            Path(os.environ.get("PROGRAMDATA", "")) / "Microsoft/Windows/Start Menu/Programs"]
    hints = [h.lower() for h in name_hints if h]
    exact, prefix, part = [], [], []
    for d in dirs:
        if not d.exists():
            continue
        try:
            for lnk in d.rglob("*.lnk"):
                stem = lnk.stem.lower()
                if "uninstall" in stem or "アンインストール" in stem:
                    continue
                if any(h == stem for h in hints):
                    exact.append(lnk)
                elif any(stem.startswith(h) for h in hints):
                    prefix.append(lnk)
                elif any(h in stem for h in hints):
                    part.append(lnk)
        except Exception:
            continue
    return exact + prefix + part


def launch_app(name_hints: list[str], explicit_path: str = "", log=print) -> bool:
    """デスクトップアプリを起動する（未起動時の自動起動）。

    優先順: settingsの明示パス(exe/lnk) → スタートメニューのショートカット →
    ストアアプリ一覧(Get-StartApps)。どれかを起動できたら True。"""
    import os
    import subprocess
    if not _IS_WIN:
        return False
    if explicit_path:
        p = Path(explicit_path)
        if p.exists():
            try:
                os.startfile(str(p))  # noqa: S606
                log(f"    ▶ アプリを起動: {p.name}")
                return True
            except Exception as e:
                log(f"    ⚠ 指定パスの起動に失敗（{str(e)[:60]}）→ 自動探索します。")
        else:
            log(f"    ⚠ desktop_app_paths のパスが存在しません: {explicit_path} → 自動探索します。")
    for lnk in _launchable_links(name_hints):
        try:
            os.startfile(str(lnk))  # noqa: S606
            log(f"    ▶ アプリを起動: スタートメニュー「{lnk.stem}」")
            return True
        except Exception:
            continue
    try:  # ストア/MSIXアプリ（スタートメニューにlnkが無い形式）
        pr = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-StartApps | ForEach-Object { $_.Name + '|' + $_.AppID }"],
            capture_output=True, text=True, errors="replace", timeout=30,
            creationflags=0x08000000)  # CREATE_NO_WINDOW
        hints = [h.lower() for h in name_hints if h]
        rows = [ln.partition("|") for ln in (pr.stdout or "").splitlines() if "|" in ln]
        rows = [(nm.strip(), appid.strip()) for nm, _, appid in rows
                if "uninstall" not in nm.lower()]
        for match in (lambda nm: nm.lower() in hints,
                      lambda nm: any(nm.lower().startswith(h) for h in hints),
                      lambda nm: any(h in nm.lower() for h in hints)):
            for nm, appid in rows:
                if match(nm):
                    subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{appid}"],
                                     creationflags=0x08000000)
                    log(f"    ▶ アプリを起動: ストアアプリ「{nm}」")
                    return True
    except Exception:
        pass
    return False


def wait_for_window(title_substr: str, timeout: float = 60,
                    stop=lambda: False, log=print):
    """起動したアプリのウィンドウが現れるまで待つ（hwnd / 出なければ None）。"""
    start = time.time()
    last_note = 0.0
    while time.time() - start < timeout:
        if stop():
            return None
        hwnd = find_window(title_substr)
        if hwnd:
            return hwnd
        now = time.time() - start
        if now - last_note >= 10:
            log(f"      …アプリの起動待ち（{int(now)}秒）")
            last_note = now
        time.sleep(1.0)
    return None


# ── デスクトップアプリの会話ログ監視（回答/画像の自動取得） ──────────────
def codex_sessions_dir(cfg: dict | None = None):
    """Codexデスクトップアプリの会話ログ置き場（無ければ None）。"""
    d = ((cfg or {}).get("codex_sessions_dir") or "").strip()
    p = Path(d) if d else Path.home() / ".codex" / "sessions"
    return p if p.exists() else None


def claude_projects_dir(cfg: dict | None = None):
    """Claude Code（デスクトップ）のセッションログ置き場（無ければ None）。"""
    d = ((cfg or {}).get("claude_projects_dir") or "").strip()
    p = Path(d) if d else Path.home() / ".claude" / "projects"
    return p if p.exists() else None


class _JsonlTailWatcher:
    """JSONL会話ログの「追記分」だけを読み、完成した回答/画像を拾う共通基盤。

    監視開始時点のファイルサイズを基準にする＝過去の回答や画像を誤って掴まない。
    書き込み途中の行はバッファに置き、行が完成してから解釈する。"""

    PATTERN = "*.jsonl"

    def __init__(self, root):
        self.dir = root
        self.offsets: dict = {}
        self.buffers: dict = {}
        self.texts: list[str] = []
        self.images: list[bytes] = []
        self._seen_img: set[str] = set()
        if self.dir:
            for p in self._files():
                try:
                    self.offsets[p] = p.stat().st_size  # 監視開始前の分は対象外
                except Exception:
                    pass

    def _files(self):
        try:
            return list(self.dir.rglob(self.PATTERN))
        except Exception:
            return []

    def available(self) -> bool:
        return self.dir is not None

    def _consume_line(self, line: bytes, path) -> None:
        raise NotImplementedError

    def _add_image_b64(self, b64) -> bool:
        if not (isinstance(b64, str) and len(b64) > 1000):  # 実サイズはpop側の寸法で判定
            return False
        try:
            img = base64.b64decode(b64)
        except Exception:
            return False
        h = hashlib.md5(img).hexdigest()
        if h not in self._seen_img:
            self._seen_img.add(h)
            self.images.append(img)
        return True

    def _add_image_file(self, path) -> bool:
        """新形式の savedPath（アプリが ~/.codex/generated_images/ に保存したPNG）から取り込む。"""
        try:
            p = Path(str(path or "")).expanduser()
            if not (path and p.exists() and p.stat().st_size > 1000):
                return False
            img = p.read_bytes()
        except Exception:
            return False
        h = hashlib.md5(img).hexdigest()
        if h not in self._seen_img:
            self._seen_img.add(h)
            self.images.append(img)
        return True

    def poll(self) -> None:
        if not self.dir:
            return
        for p in self._files():
            try:
                size = p.stat().st_size
            except Exception:
                continue
            off = self.offsets.get(p, 0)
            if size <= off:
                continue
            try:
                with open(p, "rb") as f:
                    f.seek(off)
                    chunk = f.read(size - off)
            except Exception:
                continue
            self.offsets[p] = off + len(chunk)
            buf = self.buffers.get(p, b"") + chunk
            *lines, rest = buf.split(b"\n")
            self.buffers[p] = rest  # 書き込み途中の行は次のpollで完成してから読む
            for ln in lines:
                if ln.strip():
                    self._consume_line(ln, p)

    def pop_text(self) -> str:
        self.poll()
        return self.texts.pop(0) if self.texts else ""

    def pop_image_file(self) -> str | None:
        """新しい生成画像があれば一時PNGに保存してパスを返す（無ければNone）。"""
        self.poll()
        while self.images:
            img = self.images.pop(0)
            try:
                import io as _io
                import tempfile
                from PIL import Image
                im = Image.open(_io.BytesIO(img))
                im.load()
                if im.width < 200 or im.height < 200:
                    continue  # アイコン等の小画像は動画素材として扱わない
                fd, path = tempfile.mkstemp(prefix="izanagi_codex_", suffix=".png")
                import os as _os
                with _os.fdopen(fd, "wb") as f:
                    f.write(img)
                return path
            except Exception:
                continue
        return None


class RolloutWatcher(_JsonlTailWatcher):
    """~/.codex/sessions/**/rollout-*.jsonl から回答/生成画像を自動取得する。

    Codexデスクトップアプリは会話の全イベントをこのJSONLへリアルタイムに書く:
      - task_complete.last_agent_message = そのターンの最終回答テキスト（完成品）
      - image_generation_end.result      = 生成画像そのもの（base64 PNG）
    ＝人が「コピー」や「画像を保存」を押さなくても、完成した瞬間に取り込める。"""

    PATTERN = "rollout-*.jsonl"

    # マルチエージェント対策のマーカー（Codexアプリが各セッション先頭に書く定型文）。
    # ChatGPT(Codex)アプリのマルチエージェントモードでは、部下エージェントごとに
    # 新しい rollout ファイルが作られ、そこにも task_complete（部下の作業報告＝
    # 「…親へ詳細報告しました」等の短文）が書かれる。これを回答と誤認して
    # 途中で掴む実バグがあった（2026-08-04 LP実機テスト・248字の部下報告を採用）。
    _SUB_MARK = b"an agent in a team of agents"     # 部下セッションの開発者メッセージ
    _MAIN_MARK = b"the primary agent"               # 親セッション（/root）の開発者メッセージ

    def __init__(self, cfg: dict | None = None):
        super().__init__(codex_sessions_dir(cfg))
        self._main = None      # 今回の会話（親セッション）のファイル
        self._subs: set = set()  # 部下エージェントのファイル＝テキスト回答を採らない

    def _consume_line(self, line: bytes, path) -> None:
        # ファイルの素性判定はJSON解釈より先にバイト照合で（1行ごとでも軽い）
        if self._SUB_MARK in line:
            self._subs.add(path)
            if self._main == path:
                self._main = None
            return
        if self._main is None and self._MAIN_MARK in line and path not in self._subs:
            self._main = path
        try:
            d = json.loads(line)
        except Exception:
            return
        pl = d.get("payload") or {}
        t = pl.get("type")
        if t == "image_generation_end" and pl.get("status") == "completed":
            self._add_image_b64(pl.get("result") or "")   # 画像はどのセッションでも回収（旧形式）
            return
        if t == "item_completed":
            # 2026-08アプリ更新の新形式（配布先の調査報告 2026-08-24）:
            #   payload.item.kind == "image_gen.generation" / item.result = base64 PNG /
            #   item.savedPath = アプリが保存したPNGのパス。旧 image_generation_end はもう発生しない
            it = pl.get("item")
            if not isinstance(it, dict):   # 形式が想定外でも監視を止めない
                return
            if (str(it.get("kind") or "") == "image_gen.generation"
                    and str(it.get("status") or "completed") == "completed"
                    and not it.get("failure")):
                if not self._add_image_b64(it.get("result") or ""):
                    self._add_image_file(it.get("savedPath"))   # base64が無い/壊れている時の保険
            return
        if t != "task_complete" or path in self._subs:
            return
        if self._main is None:
            self._main = path      # マーカーが無い＝従来どおりの単独セッション
        if path != self._main:
            return                 # 親以外の完了通知は回答ではない
        msg = (pl.get("last_agent_message") or "").strip()
        if msg.startswith("Message Type:"):
            return                 # エージェント間の封筒（NEW_TASK/FINAL_ANSWER等）は無視
        if msg:
            self.texts.append(msg)


class ClaudeCodeWatcher(_JsonlTailWatcher):
    """~/.claude/projects/**/<session>.jsonl から回答を自動取得する（Claude Code連携）。

    Claude Codeはセッションの全メッセージをJSONLへリアルタイムに書く:
      - type=assistant + message.stop_reason=end_turn = ターン最終回答（完成品）
      - 途中のtool_use応答やサブエージェント(isSidechain)は最終回答ではない＝無視
    安全策: 送信したプロンプトを含む user 行が現れたファイルだけを対象にする
    （＝別ウィンドウで進行中の無関係なClaude会話を誤って掴まない）。"""

    def __init__(self, cfg: dict | None = None, prompt: str = ""):
        super().__init__(claude_projects_dir(cfg))
        self._key = (prompt or "").strip().replace("\r\n", "\n")[:60]
        self._armed: set = set()

    def _files(self):
        try:
            return [p for p in self.dir.rglob(self.PATTERN)
                    if "subagents" not in p.parts]
        except Exception:
            return []

    def _consume_line(self, line: bytes, path) -> None:
        try:
            d = json.loads(line)
        except Exception:
            return
        if d.get("isSidechain"):
            return
        t = d.get("type")
        msg = d.get("message") or {}
        if t == "user":
            # 自分が貼ったプロンプトを含むuser行＝このファイルが今回の会話
            c = msg.get("content")
            body = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
            if self._key and self._key in (body or "").replace("\r\n", "\n"):
                self._armed.add(path)
            return
        if t != "assistant" or (self._key and path not in self._armed):
            return
        if msg.get("stop_reason") not in ("end_turn", "stop_sequence"):
            return  # tool_use等=まだ途中
        parts = [c.get("text", "") for c in (msg.get("content") or [])
                 if isinstance(c, dict) and c.get("type") == "text"]
        text = "\n".join(x for x in parts if x).strip()
        if text:
            self.texts.append(text)


# ── 高レベル: テキストを送って回答をもらう ──────────────────────────────
def send_prompt(app_title: str, prompt: str, log=print, new_chat_hotkey: str = "") -> bool:
    """app_title のアプリを前面化→プロンプトを貼って送信。確実に送れた時だけTrue。

    new_chat_hotkey を渡すと貼り付け前にそのキー（例 ctrl+shift+o=ChatGPTアプリの
    新規チャット）を送り、開いている過去会話への追記＝文脈汚染を防ぐ。
    キーが効かないアプリでは何も起きないだけ（従来どおり今の会話に送る）。"""
    ok_clip = False
    for _ in range(5):  # クリップボードは他プロセスが掴んでいると一瞬失敗する
        if set_clipboard_text(prompt) and get_clipboard_text() == (prompt or ""):
            ok_clip = True
            break
        time.sleep(0.2)
    if not ok_clip:
        log("    ⚠ クリップボードに書き込めませんでした（他のアプリが使用中の可能性）。"
            "数秒おいてもう一度お試しください。")
        return False
    hwnd = activate_window(app_title)
    if not hwnd:
        log(f"    ⚠ アプリ『{app_title}』を前面にできませんでした。"
            "起動してログインし、一度そのウィンドウをクリックしてから再実行してください。")
        return False
    if new_chat_hotkey:
        if send_hotkey(hwnd, new_chat_hotkey):
            log("    🆕 新規チャットを開いて送信します。")
            time.sleep(1.2)  # 画面切替と入力欄フォーカスを待つ
            if _u32.GetForegroundWindow() != hwnd:
                hwnd = activate_window(app_title) or hwnd
    if not send_paste_and_enter(hwnd):
        log("    ⚠ 送信直前に別のウィンドウが前面になったため送信を中止しました（誤送信防止）。")
        return False
    log(f"    ▶ 『{app_title}』にプロンプトを送りました。")
    return True


def wait_for_reply(prompt: str, timeout: float = 480, stop=lambda: False,
                   min_len: int = 20, log=print, validate=None,
                   auto_copy_title: str | None = None,
                   copy_hotkey: str = "ctrl+shift+c",
                   copy_interval: float = 6.0,
                   rollout: "RolloutWatcher | None" = None) -> str:
    """アプリの回答をクリップボード経由で取り込む。

    - 手動: アプリの「コピー」を押すとクリップボードが変わる → 即採用（従来動作）。
    - 自動: auto_copy_title を渡すと、定期的にアプリへ「回答コピー」ショートカットを
      送って自分で取り込む（人がコピーを押さなくてよい）。生成途中の部分テキストを
      掴まないよう「2回連続で同じ内容が取れた時だけ完成」とみなす。
      フォーカス奪取対策: アプリが前面か、ユーザーが数秒無操作の時だけ前面化する。
    validate(text)->bool を渡すと、形式が合わない内容（待機中にユーザーが別作業で
    コピーしたURL・メール文など）を回答として誤採用せず、待ち続ける。"""
    start = time.time()
    last_note = 0.0
    rejected = ""
    prev_capture = ""      # 自動コピーの前回取得値（安定=完成判定）
    baseline_capture = None  # 初回取得値=「前の回答/無関係な固定内容」の基準（誤採用防止）
    last_copy = 0.0
    base = (prompt or "").strip()
    if rollout is not None and rollout.available():
        log("      回答が完成したら自動で取り込みます（操作は不要です）。")
    elif auto_copy_title:
        log("      回答が完成したら自動でコピーを取り込みます"
            "（アプリの「コピー」を手で押してもOK・そちらが先なら即採用）。")
    else:
        log("      回答が出たらアプリの「コピー」を押してください（自動で取り込みます）。")

    def _ok(text: str) -> bool:
        return (bool(text) and text != base and text != rejected
                and text != (baseline_capture or "")
                and len(text) >= min_len and (validate is None or validate(text)))

    while time.time() - start < timeout:
        if stop():
            raise RuntimeError("中断要求により停止（デスクトップ連携）")
        # 本命: Codexアプリの会話ログから完成した回答を直接取得（操作不要・部分文なし）
        if rollout is not None:
            txt = rollout.pop_text()
            if txt and len(txt) >= min_len and (validate is None or validate(txt)):
                log("      ✅ 回答をアプリのログから自動取得しました。")
                return txt
        now0 = time.time()
        # 初回は早めに一度押して「基準値」を取る（この時点の内容=前の回答や、
        # ショートカットが別機能だった場合の固定文字列。以後それと同じ内容は採用しない）
        need_wait = 1.5 if baseline_capture is None else copy_interval
        if (auto_copy_title and now0 - start >= need_wait
                and now0 - last_copy >= min(need_wait, copy_interval)):
            last_copy = now0
            # ユーザーの作業を邪魔しない: アプリが既に前面 or 数秒無操作の時だけ
            front_is_app = _IS_WIN and _u32.GetForegroundWindow() == find_window(auto_copy_title)
            if front_is_app or _user_idle_sec() >= 4.0:
                hwnd = activate_window(auto_copy_title)
                if hwnd and send_copy_hotkey(hwnd, copy_hotkey):
                    time.sleep(0.4)
                    cap = get_clipboard_text().strip()
                    if baseline_capture is None:
                        baseline_capture = cap  # 基準値として記録（candidateにしない）
                    elif _ok(cap):
                        if cap == prev_capture:  # 2回連続同一＝生成完了
                            log("      ✅ 回答を自動コピーで取り込みました。")
                            return cap
                        prev_capture = cap
                        log("      …回答らしきテキストを取得。完成を確認しています…")
        cur = get_clipboard_text().strip()
        if (cur and cur != base and cur != prev_capture
                and cur != (baseline_capture or "") and len(cur) >= min_len):
            if validate is None or validate(cur):
                return cur  # 人がコピーを押した（＝完成済み）→ 即採用
            if cur != rejected:  # 同じ内容で毎秒ログを出さない
                rejected = cur
                log("      …クリップボードの内容が回答の形式と違うため無視しました"
                    "（アプリの「コピー」で回答全文をコピーしてください）。")
        now = time.time() - start
        if now - last_note >= 30:
            hint = ("自動取り込み待機中" if auto_copy_title
                    else "アプリの「コピー」を押すと取り込みます")
            log(f"      …回答待ち（{int(now)}秒）。{hint}。")
            last_note = now
        time.sleep(1.0)
    raise RuntimeError("回答が時間内に取り込めませんでした（アプリの『コピー』を押しましたか？）。")


# ── 高レベル: 画像を保存フォルダ監視で受け取る ──────────────────────────
_IMG_EXT = (".png", ".jpg", ".jpeg", ".webp")


def downloads_dir() -> str:
    """実際の「ダウンロード」フォルダを返す（ユーザーフォルダ移動/リダイレクト環境対応）。

    Path.home()/Downloads の決め打ちは、フォルダをDドライブ等へ移動した環境で
    実際の保存先と別の場所を監視してしまう → シェルの既知フォルダAPIで実体を解決する。"""
    if _IS_WIN:
        try:
            from uuid import UUID

            class _GUID(ctypes.Structure):
                _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD),
                            ("d3", wintypes.WORD), ("d4", ctypes.c_ubyte * 8)]

            u = UUID("{374DE290-123F-4565-9164-39C4925E467B}")  # FOLDERID_Downloads
            g = _GUID(u.time_low, u.time_mid, u.time_hi_version,
                      (ctypes.c_ubyte * 8)(*u.bytes[8:]))
            pp = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(
                    ctypes.byref(g), 0, None, ctypes.byref(pp)) == 0:
                p = pp.value
                ctypes.windll.ole32.CoTaskMemFree(pp)
                if p and Path(p).exists():
                    return p
        except Exception:
            pass
    return str(Path.home() / "Downloads")


def newest_image_after(folder: str, since: float, timeout: float = 480,
                       stop=lambda: False, log=print,
                       rollout: "RolloutWatcher | None" = None) -> str | None:
    """folder に since 以降で新しく現れた画像ファイルのパスを返す（保存されるまで待つ）。

    rollout を渡すと、Codexアプリの会話ログから生成画像を直接取り込む
    （＝人が「保存」を押さなくてよい。手動保存も従来どおり有効・先に来た方を採用）。"""
    d = Path(folder)
    start = time.time()
    last_note = 0.0
    while time.time() - start < timeout:
        if stop():
            raise RuntimeError("中断要求により停止（デスクトップ連携・画像）")
        if rollout is not None:  # 本命: アプリのログから自動取得
            auto = rollout.pop_image_file()
            if auto:
                log("      → 生成画像をアプリから自動取得しました。")
                return auto
        newest = None
        try:
            cands = [p for p in d.iterdir()
                     if p.is_file() and p.suffix.lower() in _IMG_EXT
                     and p.stat().st_mtime > since]
            if cands:
                newest = max(cands, key=lambda p: p.stat().st_mtime)
        except Exception:
            newest = None
        if newest is not None:
            try:
                # 書き込み完了を待つ（サイズが安定するまで）
                s1 = newest.stat().st_size
                time.sleep(0.8)
                if newest.stat().st_size == s1 and s1 > 1000:
                    log(f"      → 取り込み: {newest.name}")
                    return str(newest)
            except Exception:
                pass  # 安定待ちの間に移動/削除された → 次のループで探し直す
        now = time.time() - start
        if now - last_note >= 30:
            if rollout is not None and rollout.available():
                log(f"      …画像の生成待ち（{int(now)}秒）。完成すると自動で取り込みます。")
            else:
                log(f"      …画像の保存待ち（{int(now)}秒）。アプリで画像を『{folder}』へ保存してください。")
            last_note = now
        time.sleep(1.0)
    return None
