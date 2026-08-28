# -*- coding: utf-8 -*-
"""IZANAMI - GUIエントリポイント（tkinter / Notebook）。

分かりやすさ重視（TSUKUYOMI流）:
  ・エンジン等は生の値(api/none)ではなく日本語ラベル（推奨/無料/手動 の一言つき）で選ぶ。
  ・各所にグレーの一言ヒント。難しい設定は「設定・キー」タブへ。
  ・「作成」と「投稿」を分離。作成=①台本〜⑤保存、投稿=別タブで内容確認→予約投稿。
"""
from __future__ import annotations

import datetime
import os
import queue
import re
import sys
import threading
import traceback
from pathlib import Path

if os.name == "nt":
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("BananaDesk.IZANAMI")
    except Exception:
        pass


def _ensure_std_streams():
    for fd, flags in ((0, os.O_RDONLY), (1, os.O_WRONLY), (2, os.O_WRONLY)):
        try:
            os.fstat(fd)
        except Exception:
            try:
                nul = os.open(os.devnull, flags)
                if nul != fd:
                    os.dup2(nul, fd); os.close(nul)
            except Exception:
                pass
    try:
        if sys.stdin is None:
            sys.stdin = open(os.devnull, "r", encoding="utf-8")
    except Exception:
        pass
    for name in ("stdout", "stderr"):
        try:
            if getattr(sys, name, None) is None:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
        except Exception:
            pass


_ensure_std_streams()

ROOT = Path(__file__).resolve().parent
# バージョン（＝リリース日）。ウィンドウタイトル・ログ・サポート用zipに出る。
# ★配布ビルド前にこの1行を配布日に更新すること。
APP_VERSION = "2026-08-26"
sys.path.insert(0, str(ROOT))
_site = ROOT / ".venv" / "Lib" / "site-packages"
if _site.exists():
    sys.path.insert(0, str(_site))

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    import sv_ttk
    import izanagi
    from izanagi import config, pipeline, project, util
    from izanagi.providers.tts_gemini import GEMINI_VOICES
    from izanagi.providers.tts_edge import EDGE_VOICES
    from izanagi.providers.tts_fish import FISH_VOICES
except Exception:
    (ROOT / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
    try:
        import tkinter as _tk
        from tkinter import messagebox as _mb
        _r = _tk.Tk(); _r.withdraw()
        _mb.showerror("IZANAMI 起動エラー", traceback.format_exc())
    except Exception:
        pass
    raise

FB = "Yu Gothic UI"    # main()で BIZ UDPゴシック があれば差し替え（UD=より読みやすい）
FBM = "Meiryo UI"      # ログ用（BIZ UDゴシック があれば差し替え）
MUTED = "#b9c3de"      # ヒント文字（旧#9aa4bfは暗すぎた）
GOLD = "#dcb85e"       # 見出し・アクセント（コントラスト比 約8.5:1）
WARN = "#f2b04e"       # 警告
INK2 = "#222636"       # 入力欄の背景
LINE = "#2c3145"       # 枠線
SEL = "#3a4260"        # 選択色


def _mk_text(parent, height, size=12):
    """読みやすい共通スタイルの複数行入力欄（あふれたら右端にスクロールバーが出る）。"""
    # width=40（文字数）: 自然幅を小さくしておき、実際の幅は fill/sticky="ew" で枠に合わせる
    # （既定の80文字幅だと実機フォントで約1000px＝横スクロール枠で枠より広がる。2026-08-21）
    t = tk.Text(parent, height=height, width=40, wrap="word", font=(FB, size),
                bg=INK2, fg="#eceef6", insertbackground="#eceef6",
                selectbackground=SEL, relief="flat",
                highlightthickness=1, highlightbackground=LINE, highlightcolor=GOLD,
                padx=16, pady=12, spacing1=4, spacing2=2, spacing3=4)
    sb = ttk.Scrollbar(parent, orient="vertical", command=t.yview)

    def _sync(first, last):
        sb.set(first, last)
        try:  # 全部見えている時はしまう（必要な時だけ出す）
            if float(first) <= 0.0 and float(last) >= 1.0:
                sb.place_forget()
            else:
                sb.place(in_=t, relx=1.0, rely=0.0, relheight=1.0, anchor="ne")
        except Exception:
            pass

    t.configure(yscrollcommand=_sync)
    return t

# ── 選択肢は「(内部値, 表示ラベル)」で持つ（表示は日本語＋一言） ──
# AIの呼び名は Claude / ChatGPT / Gemini / Google Cloud に統一（OpenAI等の社名は使わない）
ENG_LABELS = {
    "script": [
        ("api", "Claude（API）"),
        ("gemini_api", "Gemini（API）"),
        ("chatgpt_api", "ChatGPT（API）"),
        ("claude_web", "Claude（ブラウザ・無料）"),
        ("gemini_web", "Gemini（ブラウザ・無料）"),
        ("chatgpt_web", "ChatGPT（ブラウザ・無料）"),
        ("codex_desktop", "ChatGPT Codex（デスクトップ）"),
        ("claudecode_desktop", "Claude Code（デスクトップ）"),
    ],
    # Google Cloud TTS(api)はGUIから撤去（2026-07-07ユーザー判断: Gemini一本化）。
    # コードは providers/tts_google.py に温存＝戻す時はここに1行足すだけ
    "tts": [
        ("gemini", "Gemini（AIスタジオ）"),
        ("fish", "Fish Audio（高品質・API）"),
        ("edge", "Edge（無料）"),
    ],
    "visual": [
        ("gemini_api", "Gemini（API）"),
        ("api", "ChatGPT（API）"),
        ("gemini_web", "Gemini（ブラウザ・無料）"),
        ("chatgpt_web", "ChatGPT（ブラウザ・無料）"),
        ("codex_desktop", "ChatGPT Codex（デスクトップ）"),
        ("none", "画像を作らない"),
    ],
    # Flow等のブラウザ手動(HITL)動画はGUIから撤去のまま（人の操作待ちで量産が止まる）。
    # SoraはOpenAIがサービス終了（アプリ2026-04-26終了・API2026-09-24廃止）のため撤去。
    # gemini_web は 2026-07-25 に完全自動化（＋→動画を作成→送信→<video>をDL）できたので復活。
    "video": [
        ("none", "使わない（画像だけ）"),
        ("gemini_web", "Gemini（ブラウザ・無料）"),
        ("veo_api", "Veo（Gemini API・自動）"),
    ],
    "thumb": [
        ("gemini_api", "Gemini（API）"),
        ("api", "ChatGPT（API）"),
        ("gemini_web", "Gemini（ブラウザ・無料）"),
        ("chatgpt_web", "ChatGPT（ブラウザ・無料）"),
        ("codex_desktop", "ChatGPT Codex（デスクトップ）"),
        ("none", "サムネを作らない"),
    ],
    "upload": [
        ("studio_web", "YouTube Studio（ブラウザ・推奨）"),
        ("api", "YouTube API（要OAuth設定）"),
    ],
}

PRIVACY_PAIRS = [("private", "非公開（予約はこれ）"), ("unlisted", "限定公開"), ("public", "すぐ公開")]
BGM_PAIRS = [("none", "BGMなし"), ("auto", "動画に合わせて自動選曲（ライブラリから）"),
             ("select", "選んだ曲を使う"), ("random", "ランダムで使う")]


def _bgm_row_text(item: dict) -> str:
    chs = item.get("channels") or []
    ch_lab = "全ch" if not chs else "ch " + ",".join(str(int(i) + 1) for i in sorted(chs))
    return (f"{item.get('title', '')}｜🎵{item.get('mood') or '未設定'}｜{ch_lab}"
            f"  [{item.get('path', '')}]")
ALIGN_PAIRS = [("segments", "標準（速い・おすすめ）"), ("whisper", "精密同期（遅い）")]

VOICES = [
    "ja-JP-Neural2-B (男)", "ja-JP-Neural2-C (女)", "ja-JP-Neural2-D (男)",
    "ja-JP-Wavenet-A (女)", "ja-JP-Wavenet-C (男)", "ja-JP-Wavenet-D (男)",
    "ja-JP-Chirp3-HD-Aoede (女・自然)", "ja-JP-Chirp3-HD-Leda (女・自然)",
    "ja-JP-Chirp3-HD-Kore (女・自然)", "ja-JP-Chirp3-HD-Zephyr (女・自然)",
    "ja-JP-Chirp3-HD-Charon (男・自然)", "ja-JP-Chirp3-HD-Fenrir (男・自然)",
    "ja-JP-Chirp3-HD-Puck (男・自然)", "ja-JP-Chirp3-HD-Orus (男・自然)",
]


def pdisp(pairs, val):
    for v, d in pairs:
        if v == val:
            return d
    return val  # 未知値はそのまま表示（設定を壊さない）


def pval(pairs, disp):
    for v, d in pairs:
        if d == disp:
            return v
    return disp


def _iso_to_disp(iso: str) -> str:
    if not iso:
        return ""
    try:
        return datetime.datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return iso


def _disp_to_iso(disp: str) -> str:
    disp = (disp or "").strip()
    if not disp:
        return ""
    try:
        return datetime.datetime.strptime(disp, "%Y-%m-%d %H:%M").strftime("%Y-%m-%dT%H:%M:00+09:00")
    except Exception:
        return disp


def _hint_breaks(text: str) -> str:
    """長いヒントを文（。！？）ごとに改行して読みやすくする（閉じ括弧の前では折らない）。"""
    import re
    return re.sub(r"(?<=[。！？])(?=[^）)』」】、。！？\s])", "\n", (text or "").strip())


def _hint(parent, text, **grid):
    """一言ヒント（明るめグレー・11pt）。文ごとに改行し、上下に余白を入れて見やすく。"""
    lb = ttk.Label(parent, text=_hint_breaks(text), style="Muted.TLabel",
                   wraplength=grid.pop("wrap", 0) or 760, justify="left")
    if grid:
        grid.setdefault("pady", (4, 11))  # 自分の行のヒントは前後に余白（横並びのpackは各自指定）
        lb.grid(**grid)
    else:
        lb.pack(anchor="w")
    return lb


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = config.load_settings()
        # 以後、別プロセスの保存を検知（二重起動の上書き防止）。getattr＝中途半端な更新
        # （app.pyだけ新しい）でも起動して「更新が未完了」の警告まで辿り着けるように
        getattr(config, "note_settings_seen", lambda: None)()
        config.ON_SAVE_SKIPPED = self._on_save_skipped
        config.backup_settings()
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.worker: threading.Thread | None = None
        self._stop = False
        self._thumb_busy = False
        self._thumb_photo = None
        self._logo = None
        self._scroll_canvases = []  # スクロール枠（ページ/サイドバー）。ホイール振り分けに使う
        self._scroll_fitters = []   # 各ページ枠の「中身幅に合わせ直す」関数（横スクロール用）

        # 既定は空文字＝「CORE_VERSIONを持たない古いizanagi\」。これも不一致として扱う
        # （2026-07-30より前の版には CORE_VERSION が無いので、bool(core)で弾くと
        #   まさに守りたい相手＝旧版から更新した人だけ検出できなくなる）
        core = getattr(izanagi, "CORE_VERSION", "")
        half = core != APP_VERSION
        root.title(f"IZANAMI v{APP_VERSION}"
                   + (f"（⚠更新が未完了: 中身は{'v' + core if core else '旧版'}）" if half else "")
                   + " - AI YouTube動画メーカー")
        if half:
            # 差分更新の貼り付けで「フォルダーの結合を確認」を［スキップ］した状態。
            # app.py だけ新しく izanagi\ が古い＝直したはずの不具合が直っていないのに
            # バージョン表示だけ新しく見えるので、必ず気づけるように起動時に知らせる
            root.after(400, lambda: messagebox.showwarning(
                "更新が最後まで終わっていません",
                f"本体は v{APP_VERSION} ですが、中身のプログラム（izanagiフォルダ）が "
                + (f"v{core} のままです。" if core else "古いままです。") + "\n\n"
                "更新パックを貼り付けたときに「フォルダーの結合を確認」で\n"
                "［スキップ］を選んだ可能性があります。\n\n"
                "もう一度「IZANAMIフォルダへコピー」の中身を全部貼り付けて、\n"
                "　・「フォルダーの結合を確認」→［はい］\n"
                "　・「ファイルを置き換えますか？」→［ファイルを置き換える］\n"
                "を選んでください。"))
        self._cleanup_old_logs()                       # 古い日別ログを整理
        self._log_session_header("起動")               # 起動＝ログの区切り＋環境情報
        try:
            scale = max(1.0, root.winfo_fpixels("1i") / 96.0)
        except Exception:
            scale = 1.0
        # 高DPI(表示スケール150%等)では 1240×scale が物理画面を超え、minsizeのせいで
        # 縮めることもできず右下（プレビュー・🚀ボタン）が画面外＝操作不能になる
        # （2026-08-05配布先報告）→ 画面サイズでクランプする
        try:
            sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        except Exception:
            sw, sh = 1920, 1080
        # 既定幅は1440（作成タブ②が横バー無しで収まる幅。2026-08-20。画面が狭ければクランプ）
        w = min(int(1440 * scale), max(800, sw - 80))
        h = min(int(1000 * scale), max(600, sh - 120))
        root.geometry(f"{w}x{h}")
        root.minsize(min(int(1100 * scale), w), min(int(760 * scale), h))
        try:
            ico = ROOT / "assets" / "icon.ico"
            if ico.exists():
                root.iconbitmap(default=str(ico))
        except Exception:
            pass
        self._init_styles()
        self._build_header()
        self._build_ui()
        self._poll_log()
        try:  # 同梱テロップフォントのユーザー登録（初回のみ実処理・以後は即スキップ）
            from izanagi.fonts import ensure_fonts_installed
            ensure_fonts_installed(self._log)
        except Exception:
            pass
        # 自動保存: 💾を押し忘れても入力（声コード・プロンプト等）が消えないように
        # （2026-08-06ユーザー要望。従来は💾/▶/×閉じ時のみ＝クラッシュや強制終了で消えていた）
        self._autosave_snap = None
        self.root.after(self._AUTOSAVE_MS, self._autosave_tick)
        self.root.after(500, self._refit_tick)
        self.root.after(3000, self._check_single_instance)

    _MUTEX_HANDLE = None   # プロセス内で1回だけ（テスト等で App を複数作っても二重警告しない）

    def _check_single_instance(self):
        """同じフォルダのIZANAMIが既に起動していたら知らせる（止めはしない）。
        二重起動のまま古い窓を閉じると、その窓の古い設定で settings.json が上書きされる。"""
        if App._MUTEX_HANDLE is not None:
            return
        try:
            import ctypes, hashlib
            k = ctypes.windll.kernel32
            name = "Local\\IZANAMI_" + hashlib.md5(str(ROOT).lower().encode("utf-8")).hexdigest()[:16]
            App._MUTEX_HANDLE = k.CreateMutexW(None, False, name) or True
            if k.GetLastError() == 183:   # ERROR_ALREADY_EXISTS
                self._log("⚠ IZANAMIがすでに別ウィンドウで起動しています。二重起動のまま古い方を閉じると"
                          "設定（OP/ED・世界観など）が古い内容で上書きされることがあります。"
                          "片方だけ使ってください。")
                try:
                    self._set_status("⚠ 二重起動中")
                except Exception:
                    pass
        except Exception:
            pass

    def _init_styles(self):
        st = ttk.Style()
        try:
            # ── 全体の“余白”を底上げ（ツール全体が窮屈で見にくい対策・2026-07-08） ──
            # ここのpaddingは全タブの同種ウィジェットに自動で効く（行が高くなり間隔が出る）。
            st.configure(".", font=(FB, 11))
            st.configure("TLabel", font=(FB, 11), padding=(0, 3))        # 文字の上下に余白
            st.configure("TButton", font=(FB, 11), padding=(14, 10))
            st.configure("TRadiobutton", font=(FB, 11), padding=(3, 6))  # 選択肢の間に余白
            st.configure("TCheckbutton", font=(FB, 11), padding=(3, 6))
            st.configure("TEntry", padding=6)                            # 入力欄を少し高く
            st.configure("TCombobox", padding=6)
            st.configure("TSpinbox", padding=5)
            st.configure("TNotebook.Tab", font=(FB, 12, "bold"), padding=(22, 12))
            st.map("TNotebook.Tab",
                   foreground=[("selected", "#eceef6"), ("!selected", "#8f97b2")])
            st.configure("Accent.TButton", font=(FB, 12, "bold"), padding=(16, 12))
            st.configure("Big.TButton", font=(FB, 13, "bold"), padding=(18, 14))
            st.configure("Muted.TLabel", foreground=MUTED, font=(FB, 11), padding=(0, 3))
            st.configure("Sub.TLabel", foreground=MUTED, font=(FB, 11))
            st.configure("Warn.TLabel", foreground=WARN, font=(FB, 11, "bold"))
            st.configure("ColHead.TLabel", foreground=GOLD, font=(FB, 11, "bold"))
            st.configure("Guide.TLabel", foreground="#f2e8cf", font=(FB, 12, "bold"), padding=(0, 5))
            # 全LabelFrameの見出しを金・太字に（①どんな動画にする？等）＋内側の余白を確保
            # 縦18=見出しと中身の間に余白（枠と文字がくっついて見にくい対策）
            st.configure("TLabelframe", padding=(18, 18))
            st.configure("TLabelframe.Label", font=(FB, 12, "bold"), foreground=GOLD)
            st.configure("Treeview", rowheight=30)                       # 一覧の行を高く（詰まり解消）
            # コンボボックスのドロップダウンも読みやすく
            self.root.option_add("*TCombobox*Listbox.font", f"{{{FB}}} 11")
            self.root.option_add("*TCombobox*Listbox.background", INK2)
            self.root.option_add("*TCombobox*Listbox.selectBackground", SEL)
        except Exception:
            pass

    # ブランドカラー: 墨紺×金×真朱（可読性優先の明度に調整済み）
    BRAND = {"banner": "#0f1120", "title": "#f5f1e8", "sub": "#b7bed6", "line": "#c8a24a",
             "btn": "#262b4a", "btn_active": "#3a4170", "btn_fg": "#ffffff",
             "primary": "#d0413c", "primary_active": "#e25752"}

    def _build_header(self):
        """ブランドヘッダー（キャラ＋タイトル。TSUKUYOMIと同じ構成）。"""
        b = self.BRAND
        banner = tk.Frame(self.root, bg=b["banner"])
        banner.pack(fill="x", side="top")
        inner = tk.Frame(banner, bg=b["banner"])
        inner.pack(fill="x", padx=24, pady=(14, 12))
        try:
            from PIL import Image, ImageTk
            path = ROOT / "assets" / "avatar.png"
            if not path.exists():
                path = ROOT / "assets" / "icon.png"
            im = Image.open(path).convert("RGBA").resize((72, 72), Image.LANCZOS)
            self._logo = ImageTk.PhotoImage(im)
            tk.Label(inner, image=self._logo, bg=b["banner"]).pack(side="left", padx=(0, 18))
        except Exception:
            pass
        txt = tk.Frame(inner, bg=b["banner"]); txt.pack(side="left", fill="y")
        row = tk.Frame(txt, bg=b["banner"]); row.pack(anchor="w")
        tk.Label(row, text="IZANAMI", bg=b["banner"], fg=b["title"],
                 font=("Yu Mincho", 26, "bold")).pack(side="left")
        tk.Label(row, text="  国産AI動画スタジオ", bg=b["banner"], fg=GOLD,
                 font=(FB, 12, "bold")).pack(side="left", pady=(10, 0))
        tk.Label(txt, text="お題を入れるだけで 台本・音声・映像・字幕・量産まで — 通常動画も猫ミームも",
                 bg=b["banner"], fg=b["sub"], font=(FB, 11)).pack(anchor="w", pady=(3, 0))
        tk.Frame(self.root, bg=b["line"], height=2).pack(fill="x", side="top")

    # ---------- UI ----------
    def _build_ui(self):
        body = ttk.Frame(self.root)
        body.pack(fill="both", expand=True)
        self._build_sidebar(body)
        nb = ttk.Notebook(body)
        self.nb = nb
        nb.pack(side="left", fill="both", expand=True, padx=(10, 4), pady=8)
        self.tab_main = ttk.Frame(nb); nb.add(self.tab_main, text="作成")
        self.tab_step = ttk.Frame(nb); nb.add(self.tab_step, text="ステップ実行")
        self.tab_remake = ttk.Frame(nb); nb.add(self.tab_remake, text="作り直し")
        self.tab_prompt = ttk.Frame(nb); nb.add(self.tab_prompt, text="文章・世界観")
        self.tab_bgm = ttk.Frame(nb); nb.add(self.tab_bgm, text="BGM")
        self.tab_oped = ttk.Frame(nb); nb.add(self.tab_oped, text="冒頭・終了")
        self.tab_post = ttk.Frame(nb); nb.add(self.tab_post, text="投稿")
        self.tab_log = ttk.Frame(nb); nb.add(self.tab_log, text="ログ")
        self.tab_set = ttk.Frame(nb); nb.add(self.tab_set, text="設定・キー")
        # ログ以外のタブは中身を縦スクロール可能に（枠を大きくしても切れない）
        self._build_main(self._scroll_body(self.tab_main, hscroll=True))
        self._build_step(self._scroll_body(self.tab_step, hscroll=True))    # _build_mainの後（out_var等を使う）
        self._build_remake(self._scroll_body(self.tab_remake, hscroll=True))
        self._build_prompt(self._scroll_body(self.tab_prompt))
        self._build_bgm(self._scroll_body(self.tab_bgm))
        self._build_oped(self._scroll_body(self.tab_oped))  # 文章世界観の後（prompt_texts初期化済み）
        self._build_post(self._scroll_body(self.tab_post))
        self._build_log(self.tab_log)
        self._build_set(self._scroll_body(self.tab_set))
        self._refresh_channel_combos()
        self._wire_scroll_wheels()  # ホイールを枠内/枠外で賢く振り分け
        # サイドバーの大ボタンは「今開いているタブ」に合わせる。
        # （作り直しタブを見ているのに『🎬 動画を作る』を押すと新規作成が始まり、
        #   作り直したかった動画とは別物が1本できてしまうため）
        nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self._on_tab_changed()

    def _on_tab_changed(self, _e=None):
        """サイドバーの大ボタンを「開いているタブ」に合わせる（誤爆防止）。"""
        try:
            cur = self.nb.select()
            if cur == str(self.tab_remake):
                self.start_btn.configure(text="🔁 この動画を作り直す", command=self._rm_start)
            elif cur == str(self.tab_step):
                self.start_btn.configure(text="▶ 次の工程へ進む", command=self._st_next)
            else:
                self.start_btn.configure(text="🎬 動画を作る", command=self._start)
        except Exception:
            pass
        try:   # タブを離れるタイミングでも自動保存（編集直後の取りこぼし防止）
            if getattr(self, "_autosave_snap", "no") != "no":
                self._autosave()
        except Exception:
            pass

    def _scroll_body(self, tab, hscroll: bool = False):
        """タブの中身を縦スクロール可能にして、中身用フレームを返す。

        入力枠を大きくしても画面から切れない（バーは必要な時だけ出る・ホイール対応）。
        hscroll=True のタブは横にもスクロールできる＝枠が中身より狭い時だけ横バーが出る
        （2026-08-20配布先要望「右側が切れる」＝文字を小さくせず横スクロールで見られるように。
        Shift+ホイールで横に動く）。他のタブは従来どおり中身を枠幅に収める（横バーは出ない）。"""
        bgc = ttk.Style().lookup("TFrame", "background") or "#1c1c1c"
        cv = tk.Canvas(tab, bg=bgc, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(tab, orient="vertical", command=cv.yview)
        hsb = ttk.Scrollbar(tab, orient="horizontal", command=cv.xview) if hscroll else None

        def _auto(bar, **pk):  # 必要な時だけバーを出す（縦横共通）
            def _sync(a, b):
                bar.set(a, b)
                try:
                    if float(a) <= 0.0 and float(b) >= 1.0:
                        bar.pack_forget()
                    elif not bar.winfo_ismapped():
                        bar.pack(before=cv, **pk)  # cvより前に詰める（後ろだと横バーの場所が残らない）
                except Exception:
                    pass
            return _sync

        cv.configure(yscrollcommand=_auto(vsb, side="right", fill="y"))
        if hsb is not None:
            cv.configure(xscrollcommand=_auto(hsb, side="bottom", fill="x"))
        cv.pack(side="left", fill="both", expand=True)
        inner = ttk.Frame(cv)
        win = cv.create_window((0, 0), window=inner, anchor="nw")

        def _fit(_e=None):
            # 枠が中身より広い＝従来どおり枠幅いっぱい／狭い＝中身の要求幅（→横バーが出る）
            try:
                w = max(cv.winfo_width(), inner.winfo_reqwidth()) if hsb is not None else cv.winfo_width()
                try:
                    cur = int(float(cv.itemcget(win, "width") or 0))
                except Exception:
                    cur = -1
                if cur != w:
                    cv.itemconfigure(win, width=w)
                cv.configure(scrollregion=cv.bbox("all"))
                first, last = cv.yview()
                if float(first) <= 0.0 and float(last) >= 1.0:
                    cv.yview_moveto(0)   # 全部見えている時は原点固定（上に空白が残らない）
            except Exception:
                pass

        inner.bind("<Configure>", _fit)
        cv.bind("<Configure>", _fit)
        self._scroll_canvases.append(cv)  # ホイール配線は _wire_scroll_wheels が一括で行う
        self._scroll_fitters.append(_fit)
        return inner

    def _refit_scroll(self):
        """中身の要求幅だけが変わった時（Fishボタン出現・一覧更新など）は<Configure>が飛ばないので、
        ページ枠の幅を合わせ直す。"""
        for f in getattr(self, "_scroll_fitters", ()):
            try:
                f()
            except Exception:
                pass

    def _refit_tick(self):
        """取りこぼし保険（0.5秒ごと・8枠×winfo 2回＝負荷は無視できる）。"""
        try:
            self._refit_scroll()
            self.root.after(500, self._refit_tick)
        except Exception:
            pass

    # ── マウスホイールの振り分け（枠内＝一覧/入力欄の中／枠外＝ページ・サイドバー） ──
    def _ancestor_canvas(self, w):
        """w から遡って、最初に見つかったスクロール枠キャンバスを返す。"""
        n = w
        while n is not None:
            if n in self._scroll_canvases:
                return n
            n = getattr(n, "master", None)
        return None

    def _wheel_router(self, e):
        """カーソル下（または祖先）に“中身がはみ出している”一覧/入力欄があれば、その中を
        スクロール（枠内スクロール）。無ければカーソルが乗っているページ枠をスクロール（枠外）。"""
        delta = int(-e.delta / 120) or (-1 if e.delta > 0 else 1)
        n = e.widget
        while n is not None and n not in self._scroll_canvases:
            if n.winfo_class() in ("Text", "Listbox", "Treeview"):
                try:
                    first, last = n.yview()
                    if not (float(first) <= 0.0 and float(last) >= 1.0):  # はみ出しあり＝枠内
                        n.yview_scroll(delta, "units")
                        return "break"
                except Exception:
                    pass
                break  # はみ出しが無い入力欄→ページ側を動かす（カーソルが乗っても詰まらない）
            n = getattr(n, "master", None)
        cv = self._ancestor_canvas(e.widget)
        if cv is not None:
            try:
                first, last = cv.yview()
                if float(first) <= 0.0 and float(last) >= 1.0:
                    return "break"   # 全部見えている＝動かさない（短いタブで上に空白が出る・2026-08-26報告）
            except Exception:
                pass
            cv.yview_scroll(delta, "units")
        return "break"

    def _wheel_router_x(self, e):
        """Shift+ホイール＝カーソルが乗っているページ枠を横にスクロール
        （横にはみ出している一覧/入力欄の上では、その中を動かす）。"""
        delta = int(-e.delta / 120) or (-1 if e.delta > 0 else 1)
        n = e.widget
        while n is not None and n not in self._scroll_canvases:
            if n.winfo_class() in ("Text", "Listbox", "Treeview"):
                try:
                    first, last = n.xview()
                    if not (float(first) <= 0.0 and float(last) >= 1.0):
                        n.xview_scroll(delta, "units")
                        return "break"
                except Exception:
                    pass
                break
            n = getattr(n, "master", None)
        cv = self._ancestor_canvas(e.widget)
        if cv is not None:
            try:
                cv.xview_scroll(delta, "units")
            except Exception:
                pass
        return "break"

    def _wire_scroll_wheels(self):
        """全ウィジェットに <MouseWheel> を配線（各widgetのタグで先取り→classの二重スクロール防止）。"""
        def walk(w):
            try:
                w.bind("<MouseWheel>", self._wheel_router, add="+")
                w.bind("<Shift-MouseWheel>", self._wheel_router_x, add="+")
            except Exception:
                pass
            for c in w.winfo_children():
                walk(c)
        walk(self.root)

    def _build_log(self, p):
        bar = ttk.Frame(p); bar.pack(fill="x", padx=16, pady=(12, 0))
        ttk.Label(bar, text="実行ログ（作成・投稿・BGM作成の詳しい進みぐあい）",
                  style="Guide.TLabel").pack(side="left")
        ttk.Button(bar, text="🗑 クリア", command=self._clear_log).pack(side="right")
        ttk.Button(bar, text="⏹ 停止", command=self._stop_run).pack(side="right", padx=8)
        ttk.Button(bar, text="📁 ログの保存先", command=self._open_log_folder).pack(side="right", padx=(0, 8))
        ttk.Button(bar, text="📦 サポート用にまとめる",
                   command=self._export_support_log).pack(side="right", padx=(0, 8))
        _hint(p, "ログは自動でファイルにも保存されています（logsフォルダ・14日分）。"
                 "うまく動かない時は「📦サポート用にまとめる」を押すと、"
                 "ログと環境情報をまとめたzipがデスクトップにできます（APIキーは入りません）。"
                 "それを開発者に送ってください。").pack(anchor="w", padx=16, pady=(8, 0))
        logf = ttk.Frame(p); logf.pack(fill="both", expand=True, padx=16, pady=12)
        self.log_text = tk.Text(logf, height=10, wrap="word", state="disabled", font=(FBM, 11),
                                bg="#12141c", fg="#e0e6f5", insertbackground="#e0e6f5",
                                selectbackground=SEL, relief="flat", padx=12, pady=10,
                                spacing1=3, spacing3=3)
        self.log_text.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(logf, command=self.log_text.yview); sb.pack(side="right", fill="y")
        self.log_text.config(yscrollcommand=sb.set)

    # ── 右サイドバー（キャラ画像付きの大きな操作ボタン。TSUKUYOMI流） ──
    def _build_sidebar(self, parent):
        # 縦に入り切らない画面でも全ボタンに届くよう、中身をキャンバスに載せてスクロール可能にする
        outer = ttk.Frame(parent, width=226)
        outer.pack(side="right", fill="y", padx=(0, 12), pady=(10, 12))
        outer.pack_propagate(False)
        bgc = ttk.Style().lookup("TFrame", "background") or "#1c1c1c"
        cv = tk.Canvas(outer, width=210, bg=bgc, highlightthickness=0, bd=0)
        sbar = ttk.Scrollbar(outer, orient="vertical", command=cv.yview)

        def _sync(first, last):  # 必要な時だけバーを出す
            sbar.set(first, last)
            try:
                if float(first) <= 0.0 and float(last) >= 1.0:
                    sbar.pack_forget()
                elif not sbar.winfo_ismapped():
                    sbar.pack(side="right", fill="y")
            except Exception:
                pass

        cv.configure(yscrollcommand=_sync)
        cv.pack(side="left", fill="both", expand=True)
        side = ttk.Frame(cv)
        cv.create_window((0, 0), window=side, anchor="nw", width=208)
        side.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))

        self._scroll_canvases.append(cv)  # ホイール配線は _wire_scroll_wheels が一括で行う
        self._btn_imgs: list = []   # キャラボタン画像のGC回避用

        ttk.Label(side, text="操作パネル", font=(FB, 13, "bold"),
                  foreground=GOLD).pack(anchor="w", pady=(0, 8))

        self.start_btn = self._char_button(side, "btn_make.png", "🎬 動画を作る",
                                           self._start, primary=True)
        self.start_btn.pack(fill="x", pady=(0, 8))
        self.resume_btn = self._char_button(side, "btn_resume.png", "♻ 続きから", self._resume)
        self.resume_btn.pack(fill="x", pady=(0, 8))
        self.stop_btn = self._char_button(side, "btn_stop.png", "⏹ 中断する", self._stop_run)
        self.stop_btn.configure(state="disabled")
        self.stop_btn.pack(fill="x", pady=(0, 8))

        # ── 進行状況（全タブから常に見える） ──
        ttk.Separator(side).pack(fill="x", pady=(2, 8))
        ttk.Label(side, text="進行状況", font=(FB, 13, "bold"),
                  foreground=GOLD).pack(anchor="w")
        self.status_var = tk.StringVar(value="待機中")
        self.status_label = ttk.Label(side, textvariable=self.status_var, anchor="w",
                                      font=(FB, 12, "bold"), foreground="#e3b95f")
        self.status_label.pack(fill="x", pady=(2, 4))
        # 全体の進行タイマー（2026-08-17改善要望: TSUKUYOMI風。開始で0から進み、
        # 完了/中断で止まる＝止まった値がそのまま「かかった時間」として残る）
        self.timer_var = tk.StringVar(value="")
        ttk.Label(side, textvariable=self.timer_var, anchor="w",
                  font=(FB, 12, "bold"), foreground="#8fd6a0").pack(fill="x", pady=(0, 4))
        self._timer_start = None
        self.pills = {}
        for key, lab in [("script", "① 台本"), ("tts", "② 音声"), ("visuals", "③ 映像"),
                         ("compose", "④ 合成"), ("project", "⑤ 保存")]:
            l = ttk.Label(side, text=f"○ {lab}", font=(FB, 11, "bold"), foreground="#9aa8cc")
            l.pack(anchor="w", pady=1)
            self.pills[key] = (l, lab)
        _hint(side, "くわしくは「ログ」タブへ", wrap=190).pack(anchor="w", pady=(2, 0))

        ttk.Separator(side).pack(fill="x", pady=8)
        ttk.Button(side, text="🌐 ログイン用ブラウザ", command=self._launch_login_chrome).pack(fill="x", pady=4)
        ttk.Button(side, text="📁 保存先を開く", command=self._open_out).pack(fill="x", pady=4)
        # 読み方辞書はどのタブからでも直行できるように常設（2026-08-04ユーザー要望）
        ttk.Button(side, text="📖 読み方辞書（登録）", command=self._open_reading_fixes).pack(fill="x", pady=4)
        ttk.Button(side, text="📚 辞書の一覧", command=self._open_dict_catalog).pack(fill="x", pady=4)
        ttk.Button(side, text="💾 設定を保存", command=self._save).pack(fill="x", pady=4)

    def _timer_begin(self):
        import time as _t
        # 世代番号で古いafterチェーンを打ち切る（連続開始で2本走らないように）
        self._timer_gen = getattr(self, "_timer_gen", 0) + 1
        self._timer_start = _t.time()
        self.timer_var.set("⏱ 0分00秒")
        self._timer_tick(self._timer_gen)

    def _timer_end(self):
        # 値は消さない＝止まった表示がそのまま「かかった時間」（改善要望2026-08-17）
        self._timer_start = None

    def _timer_tick(self, gen=None):
        if self._timer_start is None or (gen is not None
                                         and gen != getattr(self, "_timer_gen", 0)):
            return
        import time as _t
        s = int(_t.time() - self._timer_start)
        self.timer_var.set(f"⏱ {s // 60}分{s % 60:02d}秒")
        try:
            # ※self.after ではない（Appは素のクラス。書き損じるとexceptに吸われて
            #   タイマーが1秒も進まない＝敵対検証2026-08-19で実機再現した実バグ）
            self.root.after(1000, lambda: self._timer_tick(gen))
        except Exception:
            pass

    _STATUS_COLORS = (("エラー", "#ff8f8a"), ("中断", WARN),
                      ("量産中", "#7db4ff"), ("作成中", "#7db4ff"), ("待機中", "#e3b95f"))

    def _set_status(self, text: str):
        self.status_var.set(text)
        color = "#e3b95f"
        for k, c in self._STATUS_COLORS:
            if k in text:
                color = c
                break
        try:
            self.status_label.config(foreground=color)
        except Exception:
            pass

    def _char_button(self, parent, asset: str, label: str, command, primary: bool = False):
        """キャラ画像＋大きな文字の操作ボタン（TSUKUYOMIの_char_button移植）。"""
        b = self.BRAND
        img = None
        try:
            from PIL import Image, ImageTk
            p = ROOT / "assets" / asset
            if p.exists():
                pil = Image.open(p).convert("RGBA").resize((108, 108), Image.LANCZOS)
                img = ImageTk.PhotoImage(pil)
                self._btn_imgs.append(img)  # GC回避
        except Exception:
            img = None
        bg = b["primary"] if primary else b["btn"]
        act = b["primary_active"] if primary else b["btn_active"]
        btn = tk.Button(parent, text=label, command=command, compound="top",
                        image=img, cursor="hand2", relief="flat", bd=0,
                        padx=10, pady=8, bg=bg, fg=b["btn_fg"],
                        activebackground=act, activeforeground=b["btn_fg"],
                        disabledforeground=("#f2cdc9" if primary else "#a9a3c4"),
                        highlightthickness=1, highlightbackground=LINE,
                        font=(FB, 13, "bold"))
        return btn

    def _build_main(self, p):
        pad = {"padx": 16, "pady": 11}  # 枠と枠の間隔（全体的に窮屈対策）
        self._channel_combos = []  # チャンネル選択コンボは複数箇所で同期（ここで初期化）

        ttk.Label(p, text="使い方：❶ チャンネルを選ぶ → ❷ お題を入れる → ❸「🎬 動画を作る」→ 「投稿」タブで 🚀 予約投稿",
                  style="Guide.TLabel").pack(anchor="w", padx=16, pady=(12, 2))

        # ── チャンネル選択（一番上：この1本の世界観を決める） ──
        chf = ttk.LabelFrame(p, text="🎬 チャンネル（この動画の世界観）"); chf.pack(fill="x", **pad)
        chrow = ttk.Frame(chf); chrow.pack(fill="x", padx=8, pady=4)
        ttk.Label(chrow, text="いま作るチャンネル").pack(side="left")
        chcb = ttk.Combobox(chrow, width=26, state="readonly")
        chcb.pack(side="left", padx=(8, 12))
        chcb.bind("<<ComboboxSelected>>", self._on_channel_selected)
        self._channel_combos.append(chcb)
        # ヒントは次の行へ（同じ行に並べると実機フォントで1100px超＝右端が見切れる・2026-08-21）
        _hint(chf, "台本の性格・声・画像・サムネ・投稿先がまとめて切り替わります"
                   "（中身は「文章・世界観」タブで設定）。").pack(anchor="w", padx=8, pady=(0, 6))

        # ── STEP 1: どんな動画？ ──
        top = ttk.LabelFrame(p, text="① どんな動画にする？"); top.pack(fill="x", **pad)
        g = ttk.Frame(top); g.pack(fill="x", padx=14, pady=12)
        ttk.Label(g, text="作り方").grid(row=0, column=0, sticky="e", padx=(0, 12), pady=(0, 8))
        sf = ttk.Frame(g); sf.grid(row=0, column=1, sticky="w", pady=(0, 8))
        self.src_var = tk.StringVar(value=self.cfg.get("script_source", "topic"))
        for v, lab in [("topic", "お題から作る"), ("youtube", "YouTube動画をリライト"),
                       ("research", "キーワードをリサーチして作る")]:
            ttk.Radiobutton(sf, text=lab, variable=self.src_var, value=v,
                            command=self._update_source_mode).pack(side="left", padx=(0, 18))
        ttk.Label(g, text="動画タイプ").grid(row=1, column=0, sticky="e", padx=(0, 12), pady=(0, 8))
        mf = ttk.Frame(g); mf.grid(row=1, column=1, sticky="w", pady=(0, 8))
        self.vmode_var = tk.StringVar(value=self.cfg.get("video_mode", "normal"))
        for v, lab in [("normal", "通常（ナレーション動画）"), ("neko", "猫ミーム（読み上げなし）")]:
            ttk.Radiobutton(mf, text=lab, variable=self.vmode_var, value=v).pack(side="left", padx=(0, 18))
        ttk.Label(mf, text="｜ 画面").pack(side="left", padx=(4, 8))
        self.orient_var = tk.StringVar(value=self.cfg.get("video_orientation", "landscape"))
        for v, lab in [("landscape", "横 16:9"), ("portrait", "縦ショート 9:16")]:
            ttk.Radiobutton(mf, text=lab, variable=self.orient_var, value=v).pack(side="left", padx=(0, 14))
        # 台本形式は「一人語り」固定（会話/対談形式は2026-07-07ユーザー判断で機能ごと廃止。
        # pipeline._stage_tts_dialog 等のコードは旧プロジェクトの再開用に温存）
        self.topic_lab = ttk.Label(g, text="動画のお題")
        self.topic_lab.grid(row=3, column=0, sticky="ne", padx=(0, 14), pady=(8, 0))
        self.topic_text = _mk_text(g, height=4)  # 量産はスクロールで何行でも
        self.topic_text.grid(row=3, column=1, sticky="ew", pady=(4, 4))
        self.topic_text.insert("1.0", self.cfg.get("topic", ""))
        self.src_hint = _hint(g, "", row=4, column=1, sticky="w", pady=(6, 14))
        self._update_source_mode()
        ttk.Label(g, text="追加の指示").grid(row=5, column=0, sticky="ne", padx=(0, 14), pady=(4, 0))
        self.instr_text = _mk_text(g, height=3)
        self.instr_text.grid(row=5, column=1, sticky="ew", pady=(0, 4))
        self.instr_text.insert("1.0", self.cfg.get("script_instruction", ""))
        lenf = ttk.Frame(g); lenf.grid(row=6, column=1, sticky="w", pady=(4, 2))
        _hint(lenf, "任意：「明るいトーンで」など。").pack(side="left", padx=(0, 24))
        ttk.Label(lenf, text="動画の長さ").pack(side="left")
        self.len_var = tk.StringVar(value=str(self.cfg.get("video_length_min", 5)))
        ttk.Spinbox(lenf, textvariable=self.len_var, from_=1, to=30, width=5).pack(side="left", padx=(8, 4))
        ttk.Label(lenf, text="分ぐらい").pack(side="left")
        ops = ttk.Frame(g); ops.grid(row=7, column=1, sticky="w", pady=(6, 2))
        ttk.Button(ops, text="💡 お題を自動生成（20個）",
                   command=self._gen_topics_clicked).pack(side="left")
        ttk.Label(ops, text="｜ 🌙 各chのキューから").pack(side="left", padx=(14, 2))
        self.night_n_var = tk.StringVar(value="1")
        ttk.Spinbox(ops, textvariable=self.night_n_var, from_=1, to=10, width=4).pack(side="left")
        ttk.Label(ops, text="本ずつ").pack(side="left", padx=(2, 6))
        ttk.Button(ops, text="🌙 今夜の分を回す", command=self._start_night_run).pack(side="left")
        _hint(g, "💡お題を自動生成＝上のお題欄に案を出す（各行＝1本で量産）。"
                 "🌙今夜の分を回す＝「文章・世界観」タブの各チャンネルのお題キューから上から順に"
                 "N本ずつ・全チャンネルを巡回して自動で連続生成（寝ている間の量産用）。",
              row=8, column=1, sticky="w", pady=(8, 14))
        g.columnconfigure(1, weight=1)

        # ── STEP 2: 何で作る？（左＝AIの選択 ／ 右＝細かい設定） ──
        eng = ttk.LabelFrame(p, text="② 何で作る？"); eng.pack(fill="x", **pad)
        wrap = ttk.Frame(eng); wrap.pack(fill="x", padx=14, pady=12)

        # AIの選択（左）と細かい設定（右）を横に並べると、実機フォント（BIZ UD 11pt）で
        # 約1360px＝既定ウィンドウでも右端が見切れる（2026-08-20配布先報告・敵対検証2026-08-21）
        # → 上下に積む（幅は最大でも約740px＝最小ウィンドウでも収まる。縦はスクロール）
        lgrid = ttk.Frame(wrap); lgrid.pack(anchor="w")
        self.engine_vars = {}
        for r, (cap, label) in enumerate(
                [("script", "① 台本"), ("tts", "② 音声"), ("visual", "③ 画像"),
                 ("video", "④ 動画"), ("thumb", "⑤ サムネ")]):
            ttk.Label(lgrid, text=label, width=8).grid(row=2 * r, column=0, sticky="e", padx=(0, 10), pady=7)
            cur = self.cfg.get(f"{cap}_engine", ENG_LABELS[cap][0][0])
            v = tk.StringVar(value=pdisp(ENG_LABELS[cap], cur))
            cb = ttk.Combobox(lgrid, textvariable=v, values=[d for _, d in ENG_LABELS[cap]],
                              width=28, state="readonly")
            cb.grid(row=2 * r, column=1, sticky="w", pady=7)
            self.engine_vars[cap] = v
            if cap == "tts":
                cb.bind("<<ComboboxSelected>>", lambda e: self._update_voice_choices())
            if cap == "visual":
                # 画像をどのレベル（モデル）で作るか選べるように（2026-08-10ユーザー要望）。
                # Web版(ChatGPT/Gemini)は生成前に自動でこのモデルへ切替→画像取得後に元へ復元。
                # デスクトップ版は自動切替不可のためログで案内。自由入力可＝名前が変わっても対応
                # （2026-08-20: 横に並べると右端が見切れるため③画像の下の行へ）
                mf = ttk.Frame(lgrid); mf.grid(row=2 * r + 1, column=1, sticky="w", pady=(0, 7))
                ttk.Label(mf, text="GPTモデル:").pack(side="left")
                self.img_model_cg_var = tk.StringVar(
                    value=self.cfg.get("web_image_model_chatgpt", "") or "変更しない")
                ttk.Combobox(mf, textvariable=self.img_model_cg_var, width=11,
                             values=["変更しない", "GPT-5.6 Sol", "GPT-5.5", "o3"]).pack(
                    side="left", padx=(2, 8))
                ttk.Label(mf, text="思考量:").pack(side="left")
                self.img_effort_cg_var = tk.StringVar(
                    value=self.cfg.get("web_image_effort_chatgpt", "最速") or "変更しない")
                ttk.Combobox(mf, textvariable=self.img_effort_cg_var, width=9,
                             values=["変更しない", "最速", "中程度", "高い"]).pack(
                    side="left", padx=(2, 8))
                ttk.Label(mf, text="Gemini:").pack(side="left")
                self.img_model_gm_var = tk.StringVar(
                    value=self.cfg.get("web_image_model_gemini", "3.7 Flash") or "変更しない")
                ttk.Combobox(mf, textvariable=self.img_model_gm_var, width=12,
                             values=["変更しない", "3.5 Flash-Lite", "3.7 Flash", "3.1 Pro"]).pack(
                    side="left", padx=2)

        ttk.Separator(wrap, orient="horizontal").pack(fill="x", pady=(10, 8))

        rgrid = ttk.Frame(wrap); rgrid.pack(anchor="w", fill="x")
        self.voice_lab = ttk.Label(rgrid, text="声")
        self.voice_lab.grid(row=0, column=0, sticky="e", padx=(0, 10), pady=6)
        vf0 = ttk.Frame(rgrid); vf0.grid(row=0, column=1, sticky="w", pady=6)
        vf = ttk.Frame(vf0); vf.pack(anchor="w")
        self.voice_var = tk.StringVar(value="")
        self.voice_combo = ttk.Combobox(vf, textvariable=self.voice_var, values=[],
                                        width=30, state="readonly")
        self.voice_combo.pack(side="left")
        self.voice_btn = ttk.Button(vf, text="▶ 試聴", width=8, command=self._play_voice_sample)
        self.voice_btn.pack(side="left", padx=(8, 0))
        # Fish選択時だけ出るボタン（登録済みの声の管理／fish.audioの声一覧を開く）
        # （2026-08-20: 声欄の右に並べると右端が見切れるため、声欄の下の行へ）
        vf2 = ttk.Frame(vf0); vf2.pack(anchor="w")
        self.fish_book_btn = ttk.Button(vf2, text="＋声を登録", command=self._fish_book_dialog)
        self.fish_search_btn = ttk.Button(vf2, text="🌐 声を探す",
                                          command=lambda: __import__("webbrowser").open(
                                              self.FISH_DISCOVERY_URL))
        self.voice_hint = _hint(rgrid, "", row=1, column=1, sticky="w", wrap=440)
        # 声B/声ナレ（会話形式用）は機能廃止に伴い非表示（変数とスロット管理は温存＝gridしないだけ）
        self.voice_b_var = tk.StringVar(value="")
        self.voice_b_combo = ttk.Combobox(rgrid, textvariable=self.voice_b_var, values=[],
                                          width=30, state="readonly")
        self.voice_n_var = tk.StringVar(value="")
        self.voice_n_combo = ttk.Combobox(rgrid, textvariable=self.voice_n_var, values=[],
                                          width=30, state="readonly")
        # 主人公の性別に合わせた声・画像の自動調整（2026-08-04配布先要望）
        gwrap = ttk.Frame(rgrid); gwrap.grid(row=2, column=1, sticky="w", pady=(0, 4))
        gfr = ttk.Frame(gwrap); gfr.pack(anchor="w")
        self.gender_auto_var = tk.BooleanVar(
            value=bool((self.cfg.get("voice_by_gender") or {}).get("auto_enabled")))
        ttk.Checkbutton(gfr, text="主人公の性別に合わせて声・画像を自動調整",
                        variable=self.gender_auto_var).pack(side="left")
        # 女声/男声はチェックの次の行へ（同一行だと横に伸びて、狭いウィンドウで
        # 右端が見切れる＝2026-08-19配布先スクショの「画面が切れる」対応）
        gfr2 = ttk.Frame(gwrap); gfr2.pack(anchor="w", pady=(2, 0))
        ttk.Label(gfr2, text="女声").pack(side="left", padx=(24, 2))
        self.voice_f_var = tk.StringVar(value="")
        self.voice_f_combo = ttk.Combobox(gfr2, textvariable=self.voice_f_var, values=[],
                                          width=20, state="readonly")
        self.voice_f_combo.pack(side="left")
        ttk.Label(gfr2, text="男声").pack(side="left", padx=(8, 2))
        self.voice_m_var = tk.StringVar(value="")
        self.voice_m_combo = ttk.Combobox(gfr2, textvariable=self.voice_m_var, values=[],
                                          width=20, state="readonly")
        self.voice_m_combo.pack(side="left")
        _hint(gwrap, "ON: 台本完成後にAIが主人公の性別を判定し、女性なら女声/男性なら男声＋"
                     "画像の人物も統一。判定できない時は上で選んだ声のまま。"
                     "Fishは女声/男声欄に自分の声IDを登録した時だけ切り替わります"
                     "（空欄=いつもの声のまま）。", wrap=440)
        ttk.Label(rgrid, text="本数・枚数").grid(row=3, column=0, sticky="e", padx=(0, 10), pady=6)
        nf = ttk.Frame(rgrid); nf.grid(row=3, column=1, sticky="w", pady=6)
        ttk.Label(nf, text="動画").pack(side="left")
        self.nclip_var = tk.StringVar(value=str(self.cfg.get("num_video_clips", 3)))
        ttk.Spinbox(nf, textvariable=self.nclip_var, from_=0, to=30, width=5).pack(side="left", padx=(6, 4))
        ttk.Label(nf, text="本　　画像").pack(side="left")
        self.nimg_var = tk.StringVar(value=str(self.cfg.get("num_images", 5)))
        ttk.Spinbox(nf, textvariable=self.nimg_var, from_=1, to=120, width=5).pack(side="left", padx=(6, 4))
        ttk.Label(nf, text="枚").pack(side="left")
        # 画替わりの速さ（1枚あたり何秒か）を出して、少なすぎ／多すぎに気づけるようにする
        # （2026-08-20: 右隣に置くと右端が見切れるため、本数・枚数の下の行へ）
        ttk.Label(rgrid, text="画替わり").grid(row=4, column=0, sticky="e", padx=(0, 10), pady=6)
        pf0 = ttk.Frame(rgrid); pf0.grid(row=4, column=1, sticky="w", pady=6)
        pf = ttk.Frame(pf0); pf.pack(anchor="w")
        self.pace_var = tk.StringVar(value="標準（20秒に1回）")
        pace_cb = ttk.Combobox(pf, textvariable=self.pace_var, width=18, state="readonly",
                               values=["ゆっくり（30秒に1回）", "標準（20秒に1回）",
                                       "テンポ良く（15秒に1回）", "かなり速い（10秒に1回）"])
        pace_cb.pack(side="left", padx=(0, 6))
        ttk.Button(pf, text="📐 枚数を合わせる", command=self._apply_pace).pack(side="left")
        self.pace_lab = ttk.Label(pf0, text="", foreground=GOLD)
        self.pace_lab.pack(anchor="w", pady=(2, 0))
        for v in (self.len_var, self.nimg_var, self.nclip_var):
            v.trace_add("write", lambda *_a: self._update_pace())
        self._update_pace()
        # チャンネル選択はタブ最上部へ移動（ここでは扱わない）
        ttk.Label(rgrid, text="テロップ").grid(row=5, column=0, sticky="e", padx=(0, 10), pady=6)
        self.telop_var = tk.StringVar(value=self.cfg.get("telop_preset", "標準（白・黒縁）"))
        ttk.Combobox(rgrid, textvariable=self.telop_var, values=list(config.TELOP_PRESETS.keys()),
                     width=28, state="readonly").grid(row=5, column=1, sticky="w", pady=6)
        self._init_voice_slots()

        hints = ttk.Frame(eng); hints.pack(fill="x", padx=16, pady=(0, 10))
        ttk.Label(hints, text="（ブラウザ・無料）＝ログインして使う（設定・キーのボタンから）／（API）＝キーを入れて全自動（従量課金）",
                  style="Muted.TLabel").pack(anchor="w")
        ttk.Label(hints, text="⚠ ④動画のAPI（Veo/Sora）は高額（8秒1本で約$1）",
                  style="Warn.TLabel").pack(anchor="w", pady=(3, 0))

        # 保存先は「設定・キー」タブへ移動。進捗＝右サイドバー、ログ＝「ログ」タブ。
        self.out_var = tk.StringVar(value=self.cfg.get("output_dir", ""))

    # ── ①の「作り方」に応じて入力欄のラベル/ヒントを切り替え ──
    _SRC_TEXTS = {
        "topic": ("動画のお題",
                  "例：スカッとする話／車の豆知識 など。複数行入れると1行=1本で連続量産します（寝ている間に量産OK）。"),
        "youtube": ("動画のURL",
                    "YouTube動画のURLで字幕を取得し、設定を変えた“新しいストーリー”にリライト。"
                    "複数行入れると1行=1本で連続量産します（字幕が無い動画は不可）。"),
        "research": ("キーワード",
                     "例：新NISA 落とし穴 など。Webリサーチした事実に基づいて台本を作ります。"
                     "複数行入れると1行=1本で連続量産します（Gemini/ClaudeのAPIキーが必要）。"),
    }

    def _update_source_mode(self):
        lab, hint = self._SRC_TEXTS.get(self.src_var.get(), self._SRC_TEXTS["topic"])
        self.topic_lab.config(text=lab)
        self.src_hint.config(text=_hint_breaks(hint))  # 文ごとに改行して見やすく

    # ---------- 声（②のエンジン選択に連動） ----------
    _VOICE_SETS = {
        "api": ("声（Google Cloud）", VOICES),
        "gemini": ("声（Gemini）", GEMINI_VOICES),
        "fish": ("声ID（Fish・reference_id）", FISH_VOICES),
        "edge": ("声（Edge・無料）", EDGE_VOICES),
    }

    # ── Fishの声リスト（登録済みreference_id） ──
    FISH_DISCOVERY_URL = "https://fish.audio/ja/app/discovery/"

    def _fish_voice_values(self) -> list[str]:
        """声コンボの選択肢: デフォルト + 登録済み（表示は「名前｜ID」）。"""
        vals = ["（デフォルトの声）"]
        for v in (self.cfg.get("fish_voice_book") or []):
            if v.get("id"):
                vals.append(f"{v.get('name') or '無名'}｜{v['id']}")
        return vals

    @staticmethod
    def _fish_display_to_id(disp: str) -> str:
        """コンボの表示値 → reference_id（「名前｜ID」/生ID/デフォルト を吸収）。"""
        s = (disp or "").strip()
        if s in ("", "（デフォルトの声）"):
            return ""
        if "｜" in s:
            return s.rsplit("｜", 1)[-1].strip()
        return s

    def _fish_id_to_display(self, vid: str) -> str:
        """保存済みreference_id → 登録名があれば「名前｜ID」表示に。"""
        vid = (vid or "").strip()
        if not vid:
            return "（デフォルトの声）"
        for v in (self.cfg.get("fish_voice_book") or []):
            if v.get("id") == vid:
                return f"{v.get('name') or '無名'}｜{vid}"
        return vid

    def _init_voice_slots(self):
        """エンジンごとの声の選択（A/B/ナレ）を覚え、②音声の選択に合わせて切り替える。"""
        def _disp(lst, raw, fallback):
            raw = (raw or fallback)
            return next((x for x in lst if x.startswith(raw)), lst[0])

        cur_g = self.cfg.get("tts_voice", "ja-JP-Neural2-C")
        cur_gm = self.cfg.get("gemini_tts_voice", "Kore")
        cur_e = self.cfg.get("edge_voice", "ja-JP-NanamiNeural")
        cur_f = self._fish_id_to_display(self.cfg.get("fish_voice", ""))
        self._voice_slots = {
            "api": _disp(VOICES, cur_g, "ja-JP-Neural2-C"),
            "gemini": cur_gm if cur_gm in GEMINI_VOICES else GEMINI_VOICES[0],
            "fish": cur_f,
            "edge": _disp(EDGE_VOICES, cur_e, "ja-JP-NanamiNeural"),
        }
        dv = self.cfg.get("dialog_voices") or {}
        b, n = dv.get("b") or {}, dv.get("n") or {}
        self._voice_slots_b = {
            "api": _disp(VOICES, b.get("api"), "ja-JP-Neural2-D"),
            "gemini": b.get("gemini") if b.get("gemini") in GEMINI_VOICES else "Puck",
            "edge": _disp(EDGE_VOICES, b.get("edge"), "ja-JP-KeitaNeural"),
        }
        self._voice_slots_n = {
            "api": _disp(VOICES, n.get("api"), "ja-JP-Chirp3-HD-Charon"),
            "gemini": n.get("gemini") if n.get("gemini") in GEMINI_VOICES else "Charon",
            "edge": _disp(EDGE_VOICES, n.get("edge"), "en-US-AndrewMultilingualNeural"),
        }
        # 主人公の性別自動調整用（女声/男声の既定）
        vbg = self.cfg.get("voice_by_gender") or {}
        gf, gm = vbg.get("female") or {}, vbg.get("male") or {}
        self._voice_slots_f = {
            "api": _disp(VOICES, gf.get("api"), "ja-JP-Chirp3-HD-Aoede"),
            "gemini": gf.get("gemini") if gf.get("gemini") in GEMINI_VOICES else "Kore",
            "edge": _disp(EDGE_VOICES, gf.get("edge"), "ja-JP-NanamiNeural"),
            "fish": self._fish_id_to_display(gf.get("fish", "")) if gf.get("fish") else "",
        }
        self._voice_slots_m = {
            "api": _disp(VOICES, gm.get("api"), "ja-JP-Chirp3-HD-Charon"),
            "gemini": gm.get("gemini") if gm.get("gemini") in GEMINI_VOICES else "Charon",
            "edge": _disp(EDGE_VOICES, gm.get("edge"), "ja-JP-KeitaNeural"),
            "fish": self._fish_id_to_display(gm.get("fish", "")) if gm.get("fish") else "",
        }
        self._voice_slot = None
        self._update_voice_choices()

    def _current_tts_engine(self) -> str:
        return pval(ENG_LABELS["tts"], self.engine_vars["tts"].get())

    def _flush_voice_slot(self):
        if not self._voice_slot:
            return
        for var, slots in ((self.voice_var, self._voice_slots),
                           (self.voice_b_var, self._voice_slots_b),
                           (self.voice_n_var, self._voice_slots_n),
                           (self.voice_f_var, self._voice_slots_f),
                           (self.voice_m_var, self._voice_slots_m)):
            if var.get():
                slots[self._voice_slot] = var.get()

    def _update_voice_choices(self):
        self._flush_voice_slot()
        eng = self._current_tts_engine()
        label, values = self._VOICE_SETS.get(eng, ("声", []))
        self.voice_lab.config(text=label.replace("声（", "声A（"))
        values = list(values)
        if eng == "fish":  # 登録済みの声（名前｜ID）から選べる
            values = self._fish_voice_values()
        # Fishの声は reference_id（登録から選ぶ or 直接貼り付け）＝コンボを編集可にする。他は選択式
        state = "normal" if eng == "fish" else "readonly"
        for combo, var, slots in ((self.voice_combo, self.voice_var, self._voice_slots),
                                  (self.voice_b_combo, self.voice_b_var, self._voice_slots_b),
                                  (self.voice_n_combo, self.voice_n_var, self._voice_slots_n)):
            combo.config(values=values, state=state)
            var.set(slots.get(eng, values[0] if values else ""))
        # 女声/男声（主人公の性別自動調整）: 性別の既定を持てるエンジンだけ選択可
        for combo, var, slots in ((self.voice_f_combo, self.voice_f_var, self._voice_slots_f),
                                  (self.voice_m_combo, self.voice_m_var, self._voice_slots_m)):
            if eng in ("api", "gemini", "edge"):
                combo.config(values=values, state="readonly")
                var.set(slots.get(eng, values[0] if values else ""))
            elif eng == "fish":
                # Fishも女性用/男性用の声IDを登録すれば性別で自動切替できる
                # （2026-08-17配布先報告「✅していても動作していない」→ 空欄=切替しない
                # =いつもの声のまま、と明示できるように欄を開放）
                combo.config(values=values, state="normal")
                var.set(slots.get("fish", ""))
            else:
                combo.config(values=[], state="disabled")
                var.set("")
        self._voice_slot = eng if eng in self._VOICE_SETS else None
        self._st_update_voices()   # ステップ実行タブの声欄も同じエンジンの一覧へ
        # Fish専用ボタン（＋声を登録／🌐声を探す）はfish選択時だけ表示
        if hasattr(self, "fish_book_btn"):
            if eng == "fish":
                self.fish_book_btn.pack(side="left", pady=(4, 0))
                self.fish_search_btn.pack(side="left", padx=(6, 0), pady=(4, 0))
            else:
                self.fish_book_btn.pack_forget()
                self.fish_search_btn.pack_forget()
        if hasattr(self, "voice_hint"):
            self.voice_hint.config(
                text=("▼から登録済みの声を選ぶか、reference_idを直接貼り付け。"
                      "「🌐声を探す」で fish.audio の声一覧が開きます（空欄=毎回声が変わるので必ず設定）。"
                      if eng == "fish" else ""))

    def _fish_book_dialog(self):
        """Fishの声を「名前＋reference_id」で登録・削除する。作成タブの声▼に反映。"""
        book = [dict(v) for v in (self.cfg.get("fish_voice_book") or [])]
        dlg = tk.Toplevel(self.root); dlg.title("Fishの声を登録")
        dlg.configure(bg=INK2); dlg.transient(self.root); dlg.resizable(False, False)
        frm = ttk.Frame(dlg); frm.pack(fill="both", expand=True, padx=16, pady=12)
        _hint(frm, "fish.audioで声を選び、その reference_id（モデルID）と分かりやすい名前を登録。"
                   "「🌐声を探す」で一覧ページを開けます。").grid(row=0, column=0, columnspan=3, sticky="w")
        lb = tk.Listbox(frm, height=7, width=52, font=(FB, 11), bg=INK2, fg="#eceef6",
                        selectbackground=SEL, relief="flat", highlightthickness=1,
                        highlightbackground=LINE, activestyle="none")
        lb.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 8))

        def _redraw():
            lb.delete(0, "end")
            for v in book:
                lb.insert("end", f"{v.get('name') or '無名'}　｜　{v.get('id', '')}")
        _redraw()
        name_var = tk.StringVar(); id_var = tk.StringVar()
        ttk.Label(frm, text="名前").grid(row=2, column=0, sticky="e", padx=(0, 6))
        ttk.Entry(frm, textvariable=name_var, width=18).grid(row=2, column=1, sticky="w")
        ttk.Button(frm, text="🌐 声を探す",
                   command=lambda: __import__("webbrowser").open(self.FISH_DISCOVERY_URL)).grid(
            row=2, column=2, sticky="e")
        ttk.Label(frm, text="reference_id").grid(row=3, column=0, sticky="e", padx=(0, 6), pady=(4, 0))
        ttk.Entry(frm, textvariable=id_var, width=40).grid(row=3, column=1, columnspan=2,
                                                           sticky="ew", pady=(4, 0))

        def _add():
            vid = id_var.get().strip()
            if not vid:
                return
            book.append({"name": name_var.get().strip() or "無名", "id": vid})
            name_var.set(""); id_var.set(""); _redraw()

        def _del():
            for i in reversed(list(lb.curselection())):
                if i < len(book):
                    del book[i]
            _redraw()

        def _save():
            self.cfg["fish_voice_book"] = book
            try:
                config.save_settings(self.cfg)
            except Exception:
                pass
            self._update_voice_choices()  # 声▼を更新
            dlg.destroy()

        bar = ttk.Frame(frm); bar.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(10, 0))
        ttk.Button(bar, text="＋ 追加", command=_add).pack(side="left")
        ttk.Button(bar, text="🗑 選択を削除", command=_del).pack(side="left", padx=6)
        ttk.Button(bar, text="💾 保存", command=_save, style="Accent.TButton").pack(side="right")
        ttk.Button(bar, text="キャンセル", command=dlg.destroy).pack(side="right", padx=(0, 6))
        frm.columnconfigure(1, weight=1)
        dlg.update_idletasks(); dlg.grab_set()

    # ---------- チャンネル（世界観プリセット×10） ----------
    def _channel_names(self):
        return [f"{i + 1}. {c.get('name') or f'チャンネル{i + 1}'}"
                for i, c in enumerate(self.cfg.get("channels", []))]

    def _refresh_channel_combos(self):
        names = self._channel_names()
        idx = self.cfg.get("active_channel", 0)
        for cb in self._channel_combos:
            cb.config(values=names)
            if names:
                cb.set(names[idx])
        if hasattr(self, "ch_name_var"):
            self.ch_name_var.set(self.cfg["channels"][idx].get("name", ""))
        if hasattr(self, "post_ch_combo"):  # 投稿タブの絞り込み（選択位置は維持）
            cur = max(0, self.post_ch_combo.current())
            vals = ["すべて（全チャンネル）"] + names
            self.post_ch_combo.config(values=vals)
            self.post_ch_combo.current(min(cur, len(vals) - 1))

    def _save_channel_fields(self, idx):
        try:
            ch = self.cfg["channels"][idx]
        except (IndexError, KeyError):
            return
        if hasattr(self, "ch_name_var"):
            ch["name"] = self.ch_name_var.get().strip() or f"チャンネル{idx + 1}"
        for k in config.CHANNEL_KEYS:
            if k in self.prompt_texts:
                ch[k] = self.prompt_texts[k].get("1.0", "end").strip()
            elif hasattr(self, "channel_vars") and k in self.channel_vars:
                ch[k] = self.channel_vars[k].get().strip()
        if hasattr(self, "queue_list"):
            ch["topic_queue"] = list(self.queue_list.get(0, "end"))

    def _load_channel_fields(self, idx):
        ch = self.cfg["channels"][idx]
        if hasattr(self, "ch_name_var"):
            self.ch_name_var.set(ch.get("name", ""))
        for k in config.CHANNEL_KEYS:
            if k in self.prompt_texts:
                t = self.prompt_texts[k]
                t.delete("1.0", "end")
                t.insert("1.0", ch.get(k, ""))
            elif hasattr(self, "channel_vars") and k in self.channel_vars:
                self.channel_vars[k].set(ch.get(k, ""))
        if hasattr(self, "queue_list"):
            self.queue_list.delete(0, "end")
            for t in (ch.get("topic_queue") or []):
                self.queue_list.insert("end", t)

    def _on_channel_selected(self, event=None):
        new = event.widget.current() if event else -1
        old = self.cfg.get("active_channel", 0)
        if new < 0:
            return
        if new != old:
            self._save_channel_fields(old)
            self.cfg["active_channel"] = new
            self._load_channel_fields(new)
            try:  # 切替時点でディスクへも保存（×で閉じても編集が消えないように）
                config.save_settings(self.cfg)
            except Exception:
                pass
            self._log(f"チャンネル切替: {self.cfg['channels'][new].get('name')}")
        self._refresh_channel_combos()

    def _rename_channel(self):
        idx = self.cfg.get("active_channel", 0)
        self._save_channel_fields(idx)
        self._refresh_channel_combos()
        try:
            config.save_settings(self.cfg)
        except Exception:
            pass
        self._log(f"チャンネル名を変更: {self.cfg['channels'][idx].get('name')}")

    def _on_close(self):
        """×で閉じる時に編集中の設定を保存する。

        従来は終了時保存が無く、文章・世界観タブで書いたプロンプトが黙って消えていた
        （「画像プロンプトが前半だけ保存されていた」事故の発生機構＝2026-07-16監査 #4）。"""
        try:
            self.cfg = self._gather_cfg()
        except Exception:
            try:
                self._save_channel_fields(self.cfg.get("active_channel", 0))
            except Exception:
                pass
        do_save = True
        try:
            if config.settings_changed_externally() and \
                    self._cfg_fingerprint(self.cfg) != getattr(self, "_autosave_snap", None):
                do_save = messagebox.askyesno(
                    "設定の保存",
                    "この窓を開いた後に、別のIZANAMIウィンドウ（または別の処理）が設定を保存しています。\n"
                    "この窓の設定で上書きすると、相手側の変更（OP/ED・世界観など）が巻き戻ります。\n\n"
                    "この窓の設定で上書きしますか？\n（いいえ＝この窓の変更は保存せずに閉じる）",
                    default="no")
        except Exception:
            do_save = True
        if do_save:
            try:
                config.save_settings(self.cfg, force=True)
            except Exception:
                pass
        else:
            # 上書きしない選択でも、この窓の設定は控えに残す（必要なら 📂読込 で戻せる）
            try:
                import json as _json
                from datetime import datetime as _dt
                config.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
                p = config.BACKUP_DIR / f"settings_{_dt.now():%Y%m%d_%H%M%S}_unsaved_window.json"
                p.write_text(_json.dumps(config.export_settings(self.cfg, include_secrets=True),
                                         ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception:
                pass
        self.root.destroy()

    def _refresh_queue_view(self, ch_idx):
        """cfg上のお題キューをGUIのListboxへ再読込する（GUIスレッドから呼ぶ）。

        🌙夜間ワーカーが消化した分をListboxへ反映しないと、翌朝の💾/▶で
        古い表示がチャンネルへ書き戻り、同じ動画を重複生産する（2026-07-16監査 #9）。"""
        if not hasattr(self, "queue_list"):
            return
        if ch_idx != self.cfg.get("active_channel", 0):
            return  # 表示中でないチャンネルはListboxに出ていない＝反映不要
        try:
            q = self.cfg["channels"][ch_idx].get("topic_queue") or []
            self.queue_list.delete(0, "end")
            for t in q:
                self.queue_list.insert("end", t)
        except Exception:
            pass

    def _queue_add(self):
        from tkinter import simpledialog
        t = simpledialog.askstring("IZANAMI", "キューに追加するお題:", parent=self.root)
        if t and t.strip():
            self.queue_list.insert("end", t.strip())
            self._save_channel_fields(self.cfg.get("active_channel", 0))
            config.save_settings(self.cfg)

    def _queue_del(self):
        sel = list(self.queue_list.curselection())
        for i in reversed(sel):
            self.queue_list.delete(i)
        if sel:
            self._save_channel_fields(self.cfg.get("active_channel", 0))
            config.save_settings(self.cfg)

    def _gen_topics_clicked(self, n=20, to_queue=False):
        """💡 チャンネルの世界観からお題をn個自動生成（お題欄 or キューへ追記）。"""
        if getattr(self, "_topics_running", False):
            return
        # 動画作成中に押すと、同じChromeとクリップボードを2つの処理が奪い合って
        # 台本が壊れる（デスクトップ連携は特に危険）
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("IZANAMI", "いま動画を作成中です。\n"
                                           "終わってからお題を生成してください。")
            return
        cfg = self._gather_cfg()
        self._topics_running = True
        self._log(f"💡 お題を{n}個生成します（チャンネル: {cfg.get('_channel_name') or '共通'}）…")

        def _worker():
            pw = browser = ctx = None
            try:
                from izanagi import browser_ai, providers, topics as topics_mod
                cls = providers.REGISTRY.get(("script", cfg.get("script_engine")))
                if cls and cls.needs_browser():
                    try:
                        pw, browser, ctx = browser_ai.open_session(
                            cfg.get("chrome_profile"), int(cfg.get("cdp_port", 9222)),
                            self._log, browser_exe=cfg.get("browser_exe", ""))
                    except Exception as e:
                        self._log(f"ブラウザ接続に失敗: {e}")
                got = topics_mod.generate_topics(cfg, n, log=self._log, ctx=ctx)
                if got:
                    def _apply():
                        if to_queue:
                            for t in got:
                                self.queue_list.insert("end", t)
                            self._save_channel_fields(self.cfg.get("active_channel", 0))
                            config.save_settings(self.cfg)
                            self._log(f"💡 お題{len(got)}個をキューに補充しました。")
                        else:
                            cur = self.topic_text.get("1.0", "end").strip()
                            self.topic_text.insert("end", ("\n" if cur else "") + "\n".join(got))
                            self._log(f"💡 お題{len(got)}個をお題欄に追記しました（不要な行は消してください）。")
                    self.root.after(0, _apply)
                else:
                    self._log("お題を生成できませんでした。")
            except Exception:
                self._log("【エラー】\n" + traceback.format_exc())
            finally:
                if pw or browser:
                    try:
                        from izanagi import browser_ai
                        browser_ai.close_session(pw, browser, int(cfg.get("cdp_port", 9222)),
                                                 self._log, kill=False)
                    except Exception:
                        pass
                self._topics_running = False

        threading.Thread(target=_worker, daemon=True).start()

    def _start_night_run(self):
        """🌙 各チャンネルのお題キューからN本ずつ、round-robinで夜間量産する。"""
        if self.worker and self.worker.is_alive():
            return
        cfg = self._gather_cfg()
        self.cfg = cfg
        config.save_settings(cfg)
        try:
            n = max(1, int(self.night_n_var.get()))
        except ValueError:
            n = 1
        plan = []
        for r in range(n):
            for idx, chd in enumerate(cfg.get("channels", [])):
                q = [x for x in (chd.get("topic_queue") or []) if str(x).strip()]
                if len(q) > r:
                    plan.append((idx, q[r]))
        if not plan:
            messagebox.showinfo("IZANAMI", "お題キューが空です。\n"
                                "「文章・世界観」タブの各チャンネルにお題を入れてください"
                                "（💡ネタ帳から10個補充 が便利です）。")
            return
        if not messagebox.askyesno("IZANAMI",
                                   f"🌙 {len(plan)}本を順番に作成します（キュー先頭から各ch {n}本ずつ）。\n"
                                   "完成した分はキューから自動で消えます。よろしいですか？"):
            return
        self._stop = False
        self.start_btn.config(state="disabled"); self.stop_btn.config(state="normal")
        self._clear_log()
        self._set_status(f"量産中 0/{len(plan)}")
        self._timer_begin()
        self.worker = threading.Thread(target=self._run_plan_worker, args=(cfg, plan), daemon=True)
        self.worker.start()
        try:
            self.nb.select(self.tab_log)
        except Exception:
            pass

    def _play_voice_sample(self):
        """今選んでいるエンジン＋声で短いサンプルを合成して再生する。"""
        if getattr(self, "_voice_playing", False):
            return
        cfg = self._gather_cfg()
        eng = cfg.get("tts_engine", "api")
        self._voice_playing = True
        self.voice_btn.config(state="disabled", text="…合成中")

        def _worker():
            try:
                from izanagi import providers
                cls = providers.REGISTRY.get(("tts", eng))
                if cls is None:
                    self._log(f"この音声エンジン（{eng}）は使えません。")
                    return
                ok, reason = cls.available(cfg)
                if not ok:
                    self._log(f"試聴できません: {reason}")
                    return
                out = Path(cfg.get("output_dir") or str(ROOT / "output")) / "_voice_sample.wav"
                out.parent.mkdir(parents=True, exist_ok=True)
                self._log(f"🔊 試聴サンプルを合成中（{self.voice_var.get()}）…")
                cls().synthesize(["こんにちは。この声で、動画のナレーションをお届けします。"],
                                 cfg, str(out), log=lambda *_: None)
                if out.exists() and out.stat().st_size > 1000:
                    import winsound
                    winsound.PlaySound(str(out), winsound.SND_FILENAME | winsound.SND_ASYNC)
                    self._log("🔊 再生中…")
                else:
                    self._log("試聴サンプルの合成に失敗しました。")
            except Exception as e:
                self._log(f"試聴エラー: {e}")
            finally:
                self._voice_playing = False
                try:
                    self.voice_btn.config(state="normal", text="▶ 試聴")
                except Exception:
                    pass

        threading.Thread(target=_worker, daemon=True).start()

    # ══════════════════ ステップ実行タブ ══════════════════
    _ST_STEPS = [("script", "① 台本"), ("tts", "② 音声"), ("visuals", "③ 画像・映像"),
                 ("compose", "④ 合成"), ("project", "⑤ サムネ・保存")]

    def _build_step(self, p):
        """1工程ずつ確認しながら動画を作るタブ（台本を直す→音声→辞書を足す…）。"""
        pad = {"padx": 16, "pady": 10}
        _hint(p, "1つの工程が終わるたびに止まります。中身を確認・修正してから次へ進めます。"
                 "（自動で最後まで作るのは「作成」タブ）").pack(anchor="w", padx=12, pady=(8, 2))

        # ── ① お題 ──
        f1 = ttk.LabelFrame(p, text="① 何を作る？"); f1.pack(fill="x", **pad)
        g = ttk.Frame(f1); g.pack(fill="x", padx=14, pady=12)
        ttk.Label(g, text="動画のお題").grid(row=0, column=0, sticky="ne", padx=(0, 10), pady=4)
        self.st_topic = _mk_text(g, height=2)
        self.st_topic.grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Label(g, text="動画タイプ").grid(row=1, column=0, sticky="e", padx=(0, 10), pady=4)
        vm = ttk.Frame(g); vm.grid(row=1, column=1, sticky="w", pady=4)
        self.st_vmode = tk.StringVar(value="normal")
        ttk.Radiobutton(vm, text="通常（ナレーション）", variable=self.st_vmode,
                        value="normal").pack(side="left")
        ttk.Radiobutton(vm, text="猫ミーム", variable=self.st_vmode,
                        value="neko").pack(side="left", padx=(12, 0))
        g.columnconfigure(1, weight=1)
        r1 = ttk.Frame(f1); r1.pack(fill="x", padx=14, pady=(0, 12))
        self.st_new_btn = ttk.Button(r1, text="🆕 この内容で始める（①台本を作る）",
                                     command=lambda: self._st_start_new())
        self.st_new_btn.pack(side="left")
        ttk.Button(r1, text="📂 途中の動画を開く", command=self._st_open_existing).pack(
            side="left", padx=(8, 0))
        self.st_where = ttk.Label(p, text="（まだ始めていません）", foreground=MUTED,
                                  wraplength=880, justify="left")
        self.st_where.pack(anchor="w", padx=18)

        # ── ② 進み具合 ──
        f2 = ttk.LabelFrame(p, text="② 進み具合（1工程ずつ進みます）"); f2.pack(fill="x", **pad)
        pg = ttk.Frame(f2); pg.pack(fill="x", padx=14, pady=12)
        self.st_marks = {}
        for i, (key, label) in enumerate(self._ST_STEPS):
            lb = ttk.Label(pg, text=f"⬜ {label}")
            lb.grid(row=0, column=i, padx=(0, 18), sticky="w")
            self.st_marks[key] = lb
        r2 = ttk.Frame(f2); r2.pack(fill="x", padx=14, pady=(0, 12))
        self.st_next_btn = ttk.Button(r2, text="▶ 次の工程へ進む", command=self._st_next,
                                      style="Accent.TButton")
        self.st_next_btn.pack(side="left", ipadx=10)
        self.st_next_lab = ttk.Label(r2, text="", foreground=GOLD)
        self.st_next_lab.pack(side="left", padx=(12, 0))
        ttk.Button(r2, text="🔁 今の工程をやり直す", command=self._st_redo).pack(side="right")

        # ── ②′ 声とBGM（2026-08-20配布先要望: ステップ実行でも声を選びたい／BGMだけ直したい） ──
        fv = ttk.LabelFrame(p, text="🎙 声とBGM（②音声・④合成で使う）"); fv.pack(fill="x", **pad)
        gv = ttk.Frame(fv); gv.pack(fill="x", padx=14, pady=12)
        ttk.Label(gv, text="声").grid(row=0, column=0, sticky="e", padx=(0, 10), pady=6)
        self.st_voice_var = tk.StringVar(value=self.cfg.get("step_voice", ""))
        self.st_voice_combo = ttk.Combobox(gv, textvariable=self.st_voice_var, values=[],
                                           width=30, state="readonly")
        self.st_voice_combo.grid(row=0, column=1, sticky="w", pady=6)
        ttk.Button(gv, text="🔁 この声で②音声からやり直す",
                   command=lambda: self._st_redo_from("tts")).grid(row=0, column=2, sticky="w",
                                                                   padx=(10, 0))
        _hint(gv, "空欄＝作成タブの声。②音声を作る時（▶で②へ進む時・やり直す時）にこの声を使います。"
                  "エンジン（Fish/Edge等）は作成タブの「②音声」欄のまま。",
              row=1, column=1, columnspan=2, sticky="w")
        ttk.Label(gv, text="🎵 BGM").grid(row=2, column=0, sticky="e", padx=(0, 10), pady=6)
        self.st_bgm_var = tk.StringVar(value="元のまま")
        self.st_bgm_combo = ttk.Combobox(gv, textvariable=self.st_bgm_var, width=34,
                                         state="readonly", values=self._bgm_choices())
        self.st_bgm_combo.grid(row=2, column=1, sticky="w", pady=6)
        ttk.Button(gv, text="🎵 BGMだけ入れ替える", command=self._st_bgm_only).grid(
            row=2, column=2, sticky="w", padx=(10, 0))
        _hint(gv, "④合成まで終わった動画のBGMだけ差し替えて、④合成→⑤仕上げを作り直します"
                  "（台本・音声・画像はそのまま）。",
              row=3, column=1, columnspan=2, sticky="w")
        self._st_update_voices()

        # ── ③ 台本の確認・修正 ──
        f3 = ttk.LabelFrame(p, text="③ 台本を確認・修正（②音声の前に直せます）")
        f3.pack(fill="both", expand=True, **pad)
        self.st_script = _mk_text(f3, height=14)
        self.st_script.pack(fill="both", expand=True, padx=14, pady=(12, 6))
        r3 = ttk.Frame(f3); r3.pack(fill="x", padx=14, pady=(0, 12))
        ttk.Button(r3, text="📥 台本を読み込む", command=self._st_load_script).pack(side="left")
        ttk.Button(r3, text="💾 台本を保存", command=self._st_save_script).pack(side="left", padx=8)
        _hint(r3, "保存したら「②音声」から作り直すと反映されます。").pack(side="left", padx=(8, 0))

        # ── ④ 読み方をその場で直す ──
        f4 = ttk.LabelFrame(p, text="④ 読み間違いを直す（音声を聞いて気づいたら、ここで登録）")
        f4.pack(fill="x", **pad)
        r4 = ttk.Frame(f4); r4.pack(fill="x", padx=14, pady=12)
        ttk.Label(r4, text="語").pack(side="left")
        self.st_dw = tk.StringVar(); self.st_dr = tk.StringVar()
        ttk.Entry(r4, textvariable=self.st_dw, width=18).pack(side="left", padx=(6, 12))
        ttk.Label(r4, text="よみ").pack(side="left")
        ttk.Entry(r4, textvariable=self.st_dr, width=18).pack(side="left", padx=(6, 12))
        ttk.Button(r4, text="＋ 辞書に追加", command=self._st_add_word).pack(side="left")
        ttk.Button(r4, text="📖 辞書を開く", command=self._open_reading_fixes).pack(
            side="left", padx=8)
        ttk.Button(r4, text="📚 登録済み一覧", command=self._open_dict_catalog).pack(
            side="left")
        self.st_dmsg = ttk.Label(f4, text="", foreground=GOLD)
        self.st_dmsg.pack(anchor="w", padx=14, pady=(0, 10))

        # ── ⑤ できたものを見る ──
        f5 = ttk.LabelFrame(p, text="⑤ できたものを確認"); f5.pack(fill="x", **pad)
        r5 = ttk.Frame(f5); r5.pack(fill="x", padx=14, pady=12)
        ttk.Button(r5, text="🔊 音声を聞く", command=lambda: self._st_open("audio/voice.wav")).pack(side="left")
        ttk.Button(r5, text="🖼 画像フォルダ", command=lambda: self._st_open("visuals")).pack(side="left", padx=8)
        ttk.Button(r5, text="🎬 動画を見る", command=lambda: self._st_open("compose/final.mp4")).pack(side="left")
        ttk.Button(r5, text="📁 フォルダを開く", command=lambda: self._st_open("")).pack(side="left", padx=8)
        self.st_dir = ""
        p.after(300, self._st_refresh)

    # ── ステップ実行: ヘルパー ──
    def _st_proj(self):
        from izanagi.project import Project
        return Project(Path(self.st_dir)) if self.st_dir else None

    def _st_refresh(self):
        """進み具合の表示と「次は何か」を更新する。"""
        from izanagi import project as P
        proj = self._st_proj()
        done = []
        for key, label in self._ST_STEPS:
            ok = bool(proj and proj.done(key))
            if ok:
                done.append(key)
            self.st_marks[key].config(text=f"{'✅' if ok else '⬜'} {label}")
        if not self.st_dir:
            self.st_where.config(text="（まだ始めていません）")
            self.st_next_lab.config(text="お題を入れて「🆕 この内容で始める」を押してください")
            return
        self.st_where.config(text=f"作業中: {self.st_dir}")
        nxt = next((lb for k, lb in self._ST_STEPS if k not in done), None)
        self.st_next_lab.config(
            text=(f"次は「{nxt}」です" if nxt else "全工程おわり。投稿タブへどうぞ"))

    def _st_open(self, rel):
        if not self.st_dir:
            messagebox.showinfo("IZANAMI", "先に動画を始めてください。"); return
        t = Path(self.st_dir) / rel if rel else Path(self.st_dir)
        if not t.exists():
            messagebox.showinfo("IZANAMI", f"まだありません:\n{t}"); return
        try:
            os.startfile(str(t))  # noqa: S606
        except Exception as e:
            messagebox.showerror("IZANAMI", f"開けませんでした: {e}")

    def _st_load_script(self):
        f = Path(self.st_dir or ".") / "script" / "script.txt"
        if not f.exists():
            messagebox.showinfo("IZANAMI", "台本はまだありません。"); return
        self.st_script.delete("1.0", "end")
        self.st_script.insert("1.0", f.read_text(encoding="utf-8"))
        self._log(f"台本を読み込みました（{f}）。")

    def _st_save_script(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("IZANAMI", "いま作成中です。終わってから保存してください。"); return
        if not self.st_dir:
            messagebox.showinfo("IZANAMI", "先に動画を始めてください。"); return
        body = self.st_script.get("1.0", "end").strip()
        if len(body) < 20:
            messagebox.showwarning("IZANAMI", "台本が短すぎます。"); return
        # 台本を変えたら、その台本から作った音声・映像・動画は作り直しになる。
        # ★先に無効化してから保存する。逆順だと、無効化に失敗（ファイルが開かれている等）
        #   したときに「新しい台本＋古い音声・動画」で全工程が完了済みのまま残ってしまう。
        try:
            proj = self._st_proj()
            after = [s for s in ("tts", "visuals", "compose", "project")
                     if proj and proj.done(s)]
            if after:
                todo, bk = project.clear_stages(self.st_dir, ["tts"], log=self._log)
                self._log("    ✕ 台本が変わったので "
                          + "、".join(project.STAGE_LABEL.get(s, s) for s in todo)
                          + " を作り直し対象にしました")
        except RuntimeError as e:
            messagebox.showwarning("IZANAMI", str(e)); return
        except Exception:
            pass
        f = Path(self.st_dir) / "script" / "script.txt"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body + "\n", encoding="utf-8")
        # 読み仮名マップは前の台本のために作ったもの。残すと新しい台本に古い読みが当たる
        try:
            rm = Path(self.st_dir) / "script" / "reading_map.json"
            if rm.exists():
                rm.unlink()
                self._log("    ✕ 読み仮名マップを作り直し対象にしました（台本が変わったため）")
        except Exception:
            pass
        self._st_refresh()
        self._log(f"台本を保存しました（{len(body)}字）。→「▶ 次の工程へ進む」で続けられます。")
        messagebox.showinfo("IZANAMI", "台本を保存しました。\n"
                                       "「▶ 次の工程へ進む」を押すと、この台本で音声から作り直します。")

    def _st_add_word(self):
        import re as _re
        w, r = self.st_dw.get().strip(), self.st_dr.get().strip()
        if not w or not r:
            self.st_dmsg.config(text="語とよみの両方を入れてください。"); return
        if not _re.fullmatch(r"[ぁ-んー]+", r):
            self.st_dmsg.config(text="よみは『ひらがな』で入れてください。"); return
        rows = [(k, v) for k, v in self._read_user_dict() if k != w] + [(w, r)]
        self._write_user_dict(rows)
        from izanagi.providers import base as _pb
        _pb._READ_FIXES = None
        self.st_dmsg.config(text=f"「{w}＝{r}」を辞書に追加しました。"
                                 "②音声をやり直すと反映されます。")
        self.st_dw.set(""); self.st_dr.set("")

    def _st_open_existing(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("IZANAMI", "いま作成中です。終わってからにしてください。"); return
        d = filedialog.askdirectory(title="途中の動画フォルダ(IZANAMI_...)を選ぶ",
                                    initialdir=self.out_var.get() or str(Path.home()))
        if not d:
            return
        if not project.looks_like_project(d):
            messagebox.showwarning(
                "IZANAMI",
                "IZANAMIの動画フォルダではないようです。\n"
                "「IZANAMI_日付_時刻」（旧IZANAGI_も可）という名前のフォルダを選んでください。")
            return
        self.st_dir = d
        self._st_refresh()
        if (Path(d) / "script" / "script.txt").exists():
            self._st_load_script()

    def _st_cfg(self):
        """このタブ用のcfg（作成タブの設定を土台に、お題と動画タイプだけ差し替え）。"""
        cfg = self._gather_cfg()
        cfg["topic"] = self.st_topic.get("1.0", "end").strip()
        cfg["video_mode"] = self.st_vmode.get()
        cfg["upload_engine"] = "none"
        # このタブには「作り方」欄が無い＝作成タブがYouTubeリライト等になっていると
        # お題を入れても台本が作れない。ここは常に「お題から作る」に固定する。
        cfg["script_source"] = "topic"
        # 1工程ごとに止める（既存のチェックポイント機構を使う）
        from izanagi.project import STAGES
        cfg["require_checkpoint"] = {s: True for s in STAGES}
        # このタブの声欄（空欄＝作成タブの声のまま）
        if hasattr(self, "st_voice_var"):
            self._apply_voice_override(cfg, self.st_voice_var.get())
        return cfg

    def _st_update_voices(self):
        """作成タブの②音声エンジンに合わせて、このタブの声欄の選択肢を差し替える。"""
        if not hasattr(self, "st_voice_combo"):
            return
        try:
            eng = self._current_tts_engine()
        except Exception:
            return
        self._set_voice_override_combo(self.st_voice_combo, self.st_voice_var, eng)

    def _st_redo_from(self, key):
        """指定工程（例: ②音声）から、確認ダイアログ無しで作り直して即実行する。"""
        from izanagi import project as P
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("IZANAMI", "いま作成中です。終わってからにしてください。"); return
        if not self.st_dir or not Path(self.st_dir).exists():
            messagebox.showinfo("IZANAMI", "先に動画を始めてください。"); return
        if getattr(self, "_thumb_busy", False):
            messagebox.showinfo("IZANAMI", "サムネを作り直し中です。終わってからお試しください。"); return
        proj = self._st_proj()
        if not (proj and proj.done(key)):
            messagebox.showinfo("IZANAMI",
                                f"{P.STAGE_LABEL.get(key, key)}はまだできていません。"
                                "「▶ 次の工程へ進む」で進めてください。"); return
        if key == "tts":
            try:
                if (proj.load_run_cfg() or {}).get("video_mode") == "neko":
                    messagebox.showinfo("IZANAMI", "猫ミームに②音声はありません（無音＋文字数タイミング）。")
                    return
            except Exception:
                pass
            if not (self.st_voice_var.get() or "").strip():
                messagebox.showinfo("IZANAMI", "声欄で声を選んでください（空欄＝作成タブの声のまま）。")
                return
            # 退避する**前**に声の妥当性を確かめる（Fishで「（デフォルトの声）」＝ID空だと
            # ②が始まらず、退避だけ済んで止まる・敵対検証2026-08-21）
            cfg0 = self._st_cfg()
            if self._fish_voice_missing(cfg0):
                messagebox.showwarning(
                    "IZANAMI",
                    "Fishの「（デフォルトの声）」は声IDが空＝1行ごとに違う声になるため使えません。\n"
                    "声欄で登録済みの声（名前｜ID）を選ぶか、声IDを直接貼り付けてください。")
                return
        try:
            todo, _bk = P.clear_stages(self.st_dir, [key], log=self._log)
        except RuntimeError as e:   # 元ファイルが使用中で移動できない
            messagebox.showwarning("やり直しを中止しました", str(e)); return
        except Exception as e:
            messagebox.showerror("IZANAMI", f"やり直せませんでした: {e}"); return
        self._log("　やり直し対象: " + "、".join(P.STAGE_LABEL.get(s, s) for s in todo))
        self._st_refresh()
        self._st_next()   # 1工程（②）だけ実行して止まる

    def _st_bgm_only(self):
        """④合成まで終わった動画のBGMだけ差し替える（④→⑤を作り直し・作り直しワーカーを利用）。"""
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("IZANAMI", "いま別の処理を実行中です。終わってからお試しください。"); return
        if not self.st_dir or not Path(self.st_dir).exists():
            messagebox.showinfo("IZANAMI", "先に動画を始めてください（📂で途中の動画も開けます）。"); return
        sel = (self.st_bgm_var.get() or "").strip()
        if sel in ("", "元のまま"):
            messagebox.showinfo("IZANAMI", "🎵BGM欄で曲（または「おまかせ（再抽選）」）を選んでください。"); return
        proj = self._st_proj()
        if not (proj and proj.done("compose")):
            messagebox.showinfo("IZANAMI", "④合成がまだです。「▶ 次の工程へ進む」で④まで進めてから使ってください。")
            return
        if getattr(self, "_thumb_busy", False):
            messagebox.showinfo("IZANAMI", "サムネを作り直し中です。終わってからお試しください。"); return
        try:
            rc0 = proj.load_run_cfg() or {}
        except Exception:
            rc0 = {}
        if rc0.get("video_mode") == "neko":
            messagebox.showwarning(
                "IZANAMI",
                "この動画は猫ミームです。猫ミームのBGMは猫素材フォルダの曲から\n"
                "自動で選ばれる仕組みのため、🎵BGM欄では変更できません。")
            return
        bgm = self._resolve_bgm_override(sel)
        if not bgm:
            return
        if not messagebox.askyesno(
                "BGM入れ替え",
                f"「{Path(self.st_dir).name}」のBGMを「{sel}」にして④合成だけ作り直します\n"
                "（台本・音声・画像・サムネ・タイトルはそのまま。テロップ・枚数などは作成タブの今の設定で焼き直します）。\n"
                "このフォルダを直接更新します（失敗した時は元に戻ります）。\n\nよろしいですか？"):
            return
        cfg = self._st_cfg()
        cfg["_remake"] = True                 # ④でBGM等「画面の設定」を勝たせる（作り直しタブと同じ）
        cfg["_bgm_only"] = True               # ⑤（サムネ再生成＝課金）は作り直さない
        cfg.pop("require_checkpoint", None)   # ④を止めずに進める
        # 本数・枚数は元の動画のまま（作成タブの値で変わらないようにrun_cfgの値を写す）
        for _k in ("num_images", "num_video_clips"):
            if _k in rc0:
                cfg[_k] = rc0[_k]
        cfg.update(bgm)
        self._stop = False
        self.st_next_btn.config(state="disabled")
        self.start_btn.config(state="disabled"); self.stop_btn.config(state="normal")
        self._clear_log()
        self._set_status("BGM入れ替え中…")
        self._timer_begin()
        self.worker = threading.Thread(target=self._rm_worker,
                                       args=(cfg, self.st_dir, ["compose"], True), daemon=True)
        self.worker.start()
        try:
            self.nb.select(self.tab_log)
        except Exception:
            pass

    def _st_start_new(self):
        # 先に実行中を弾く（後だと st_dir だけ消えて作業中フォルダを見失う）
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("IZANAMI", "いま別の処理を実行中です。終わってからお試しください。")
            return
        if not self.st_topic.get("1.0", "end").strip():
            messagebox.showwarning("IZANAMI", "先に「動画のお題」を入れてください。"); return
        self.st_dir = ""
        self._st_refresh()
        self._st_next()

    def _st_next(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("IZANAMI", "いま別の処理を実行中です。"); return
        # 作業フォルダが消えていたら（投稿タブの🗑削除等）新規①から黙って作り直さない
        # ＝お代・時間を勝手に消費しない（2026-08-04敵対レビュー）
        if self.st_dir and not Path(self.st_dir).exists():
            messagebox.showwarning(
                "IZANAMI", "作業中のフォルダが見つかりません（削除された可能性があります）。\n"
                           f"{self.st_dir}\n\n"
                           "最初からやり直す場合は「🆕 この内容で始める」を押してください。")
            self.st_dir = ""
            self.st_where.config(text="（まだ始めていません）")
            self._st_refresh()
            return
        cfg = self._st_cfg()
        if not self.st_dir and not cfg.get("topic"):
            messagebox.showwarning("IZANAMI", "先に「動画のお題」を入れてください。"); return
        _proj = self._st_proj() if self.st_dir else None
        if not (_proj and _proj.done("tts")) and self._fish_voice_missing(cfg) and self._warn_fish_voice():
            return   # ②の課金前だけ止める（②完了後の③④⑤は声と無関係）
        self._stop = False
        self.st_next_btn.config(state="disabled")
        self.start_btn.config(state="disabled"); self.stop_btn.config(state="normal")
        self._clear_log()
        self._set_status("1工程だけ実行中…")
        self._timer_begin()
        self.worker = threading.Thread(target=self._st_worker, args=(cfg,), daemon=True)
        self.worker.start()
        try:
            self.nb.select(self.tab_log)
        except Exception:
            pass

    def _st_worker(self, cfg):
        util.prevent_sleep(True)
        try:
            self._log("=" * 46)
            self._log("▶ 1工程だけ実行します（終わったら止まります）")
            out = pipeline.run(cfg, log=self._log, stop_flag=lambda: self._stop,
                               resume_dir=self.st_dir or None,
                               checkpoint=lambda stage, proj: False)  # 1工程ごとに停止
            self.st_dir = out
            self._last_out = out
            self._log(f"\n⏸ ここまでの保存先: {out}")
            self._log("→ 中身を確認して、よければ「▶ 次の工程へ進む」を押してください。")
        except Exception:
            self._log("【エラー】\n" + traceback.format_exc())
        finally:
            util.prevent_sleep(False)
            self.log_queue.put("__STEP_DONE__")

    def _st_redo(self):
        """いま終わっている最後の工程をやり直す（台本を直した後の②など）。"""
        from izanagi import project as P
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("IZANAMI", "いま作成中です。終わってからにしてください。"); return
        if not self.st_dir:
            messagebox.showinfo("IZANAMI", "先に動画を始めてください。"); return
        proj = self._st_proj()
        done = [k for k, _ in self._ST_STEPS if proj and proj.done(k)]
        if not done:
            messagebox.showinfo("IZANAMI", "やり直せる工程がまだありません。"); return
        names = {k: lb for k, lb in self._ST_STEPS}
        dlg = tk.Toplevel(self.root); dlg.title("どの工程をやり直す？")
        dlg.transient(self.root); dlg.grab_set()
        ttk.Label(dlg, text="やり直す工程を選んでください\n（それより後の工程も作り直しになります）",
                  justify="left").pack(padx=18, pady=(14, 8))
        var = tk.StringVar(value=names[done[-1]])
        ttk.Combobox(dlg, textvariable=var, values=[names[k] for k in done],
                     state="readonly", width=24).pack(padx=18)

        def _go():
            key = next((k for k in done if names[k] == var.get()), None)
            dlg.destroy()
            if not key:
                return
            try:
                todo, bk = P.clear_stages(self.st_dir, [key], log=self._log)
            except RuntimeError as e:   # 元ファイルが使用中で移動できない
                messagebox.showwarning("やり直しを中止しました", str(e)); return
            except Exception as e:
                messagebox.showerror("IZANAMI", f"やり直せませんでした: {e}"); return
            self._log("　やり直し対象: " + "、".join(P.STAGE_LABEL.get(s, s) for s in todo))
            self._st_refresh()
            self._log("→「▶ 次の工程へ進む」で作り直せます。")

        ttk.Button(dlg, text="この工程からやり直す", command=_go).pack(pady=14)

    # ══════════════════ 作り直しタブ ══════════════════
    def _build_remake(self, p):
        """出来た動画の“気に入らない所だけ”を作り直す。素材（台本/音声/画像）は再利用する。"""
        pad = {"padx": 16, "pady": 11}
        ttk.Label(p, text="使い方：❶ 元の動画を選ぶ → ❷ 作り直す工程にチェック → ❸ 使うAIを決める → ❹「🔁 作り直す」",
                  style="Guide.TLabel").pack(anchor="w", padx=16, pady=(12, 2))
        _hint(p, "チェックを入れなかった工程は、前に作ったものをそのまま使い回します"
                 "（例：音声だけチェック＝台本も画像もそのままで、声だけ入れ替えて動画を作り直す）。").pack(
            anchor="w", padx=16, pady=(0, 6))

        # ── ① 元の動画 ──
        f1 = ttk.LabelFrame(p, text="① どの動画を作り直す？"); f1.pack(fill="x", **pad)
        r1 = ttk.Frame(f1); r1.pack(fill="x", padx=14, pady=(12, 4))
        ttk.Label(r1, text="動画フォルダ").pack(side="left")
        self.rm_dir_var = tk.StringVar(value="")
        self.rm_dir_combo = ttk.Combobox(r1, textvariable=self.rm_dir_var, width=46, state="readonly")
        self.rm_dir_combo.pack(side="left", padx=(8, 6))
        self.rm_dir_combo.bind("<<ComboboxSelected>>", lambda e: self._rm_refresh_info())
        ttk.Button(r1, text="🔄 一覧更新", command=self._rm_reload_list).pack(side="left")
        ttk.Button(r1, text="📂 他から選ぶ", command=self._rm_pick_dir).pack(side="left", padx=(6, 0))
        ttk.Button(r1, text="📁 開く", command=self._rm_open_dir).pack(side="left", padx=(6, 0))
        self.rm_info = ttk.Label(f1, text="（動画フォルダを選んでください）", style="Muted.TLabel",
                                 justify="left", wraplength=880)
        self.rm_info.pack(anchor="w", padx=14, pady=(2, 12))

        # ── ② 作り直す工程 ──
        f2 = ttk.LabelFrame(p, text="② どこを作り直す？（チェックしたものだけ作り直します）"); f2.pack(fill="x", **pad)
        g2 = ttk.Frame(f2); g2.pack(fill="x", padx=14, pady=12)
        self.rm_stage_vars = {}
        for r, (st, label, note) in enumerate([
                ("script", "① 台本を作り直す", "お題から書き直します（音声・画像も自動で作り直しになります）"),
                ("tts", "② 音声を作り直す", "台本はそのまま。声やAIを変えて読み上げ直します"),
                ("visuals", "③ 画像・映像を作り直す", "構成はそのまま。絵だけ描き直します"),
                ("project", "⑤ サムネを作り直す", "サムネイル画像だけ作り直します")]):
            v = tk.BooleanVar(value=False)
            self.rm_stage_vars[st] = v
            ttk.Checkbutton(g2, text=label, variable=v,
                            command=self._rm_refresh_info).grid(row=r, column=0, sticky="w", pady=5)
            _hint(g2, note, row=r, column=1, sticky="w", padx=(14, 0), pady=5)
        _hint(f2, "※ ④合成（テロップ・BGM入れ）は、上のどれかを作り直すと自動でやり直します。"
                  "　※ 台本を自分で書き換えたい時は「📝 台本を開く」で直接編集 → ②音声・③画像にチェックして作り直せます。").pack(
            anchor="w", padx=14, pady=(0, 10))
        r2 = ttk.Frame(f2); r2.pack(fill="x", padx=14, pady=(0, 12))
        ttk.Button(r2, text="📝 台本を開く", command=self._rm_open_script).pack(side="left")
        ttk.Button(r2, text="🎬 今の動画を見る", command=self._rm_play_video).pack(side="left", padx=(8, 0))
        # 終了画面（OP/ED）だけ付け直す（2026-08-20配布先要望。④合成は行わず本編はそのまま）
        r2b = ttk.Frame(f2); r2b.pack(fill="x", padx=14, pady=(0, 12))
        ttk.Button(r2b, text="🎬 終了画面（OP/ED）だけ付け直す", command=self._rm_oped_only).pack(side="left")
        _hint(r2b, "冒頭・終了タブの「今の」OP/ED動画を、この動画（①で選んだもの）に付け直します。"
                   "本編・サムネはそのまま（数分で終わります）。", wrap=640).pack(side="left", padx=(10, 0))

        # ── ③ 使うAI（作り直し専用） ──
        f3 = ttk.LabelFrame(p, text="③ 何で作り直す？（この画面専用の設定）"); f3.pack(fill="x", **pad)
        wrap3 = ttk.Frame(f3); wrap3.pack(fill="x", padx=14, pady=12)
        # 作成タブ②と同じく上下に積む（横並びは実機フォントで約1130px＝右端が見切れる・2026-08-21）
        lg = ttk.Frame(wrap3); lg.pack(anchor="w")
        self.rm_engine_vars = {}
        for r, (cap, label) in enumerate([("script", "① 台本"), ("tts", "② 音声"),
                                          ("visual", "③ 画像"), ("video", "④ 動画"),
                                          ("thumb", "⑤ サムネ")]):
            ttk.Label(lg, text=label, width=8).grid(row=r, column=0, sticky="e", padx=(0, 10), pady=6)
            cur = self.cfg.get(f"{cap}_engine", ENG_LABELS[cap][0][0])
            v = tk.StringVar(value=pdisp(ENG_LABELS[cap], cur))
            cb = ttk.Combobox(lg, textvariable=v, values=[d for _, d in ENG_LABELS[cap]],
                              width=28, state="readonly")
            cb.grid(row=r, column=1, sticky="w", pady=6)
            self.rm_engine_vars[cap] = v
            if cap == "tts":
                cb.bind("<<ComboboxSelected>>", lambda e: self._rm_update_voices())
        ttk.Separator(wrap3, orient="horizontal").pack(fill="x", pady=(10, 8))
        rg = ttk.Frame(wrap3); rg.pack(anchor="w", fill="x")
        ttk.Label(rg, text="声").grid(row=0, column=0, sticky="e", padx=(0, 10), pady=6)
        # 前回入力の復元（自動保存の控え。fishの声コードを打ち直さなくて済むように）
        self.rm_voice_var = tk.StringVar(value=self.cfg.get("remake_voice", ""))
        self.rm_voice_combo = ttk.Combobox(rg, textvariable=self.rm_voice_var, values=[],
                                           width=30, state="readonly")
        self.rm_voice_combo.grid(row=0, column=1, sticky="w", pady=6)
        # ③画像を作り直す時の本数・枚数（空欄＝元の動画と同じ。2026-08-05ユーザー要望:
        # 作り直しでも画像の枚数を変えられるように＝画像1枚で作った動画の増量リメイク用）
        ttk.Label(rg, text="本数・枚数").grid(row=1, column=0, sticky="e", padx=(0, 10), pady=6)
        nfr = ttk.Frame(rg); nfr.grid(row=1, column=1, sticky="w", pady=6)
        ttk.Label(nfr, text="動画").pack(side="left")
        self.rm_nclip_var = tk.StringVar(value=str(self.cfg.get("remake_num_clips", "")))
        ttk.Spinbox(nfr, textvariable=self.rm_nclip_var, from_=0, to=30, width=5).pack(
            side="left", padx=(6, 4))
        ttk.Label(nfr, text="本  画像").pack(side="left")
        self.rm_nimg_var = tk.StringVar(value=str(self.cfg.get("remake_num_images", "")))
        ttk.Spinbox(nfr, textvariable=self.rm_nimg_var, from_=1, to=120, width=5).pack(
            side="left", padx=(6, 4))
        ttk.Label(nfr, text="枚").pack(side="left")
        # BGMだけ入れ替え（2026-08-17ユーザー要望「実際の動画を見た後にBGMだけ変えたい」）。
        # 「元のまま」以外を選ぶと④合成を自動で含めてBGMを差し替える
        ttk.Label(rg, text="🎵 BGM").grid(row=2, column=0, sticky="e", padx=(0, 10), pady=6)
        bfr = ttk.Frame(rg); bfr.grid(row=2, column=1, sticky="w", pady=6)
        self.rm_bgm_var = tk.StringVar(value="元のまま")
        self.rm_bgm_combo = ttk.Combobox(
            bfr, textvariable=self.rm_bgm_var, width=34, state="readonly",
            values=self._bgm_choices())
        self.rm_bgm_combo.pack(side="left")
        ttk.Label(bfr, text="（変えると④合成を自動で含めます）",
                  style="Muted.TLabel").pack(side="left", padx=(8, 0))
        # 説明文は row=3（🎵BGM行と同じ row=2 に置いて重なっていた＝BGM欄が説明文の下に
        # 埋まり「BGMだけ作り直しが無い」ように見えていた・2026-08-26配布先報告）
        _hint(rg, "「作成」タブの設定とは別に、ここだけで使うAI・声・枚数を選べます。"
                  "（声・枚数は空欄＝元の動画と同じ。枚数は③画像に✔した時だけ使われます。"
                  "入力は自動保存され、次回起動時も残ります）",
              row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))
        ttk.Button(rg, text="↺ 「作成」タブの設定に戻す", command=self._rm_reset_engines).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))

        # ── ④ 保存先 ──
        f4 = ttk.LabelFrame(p, text="④ どこに保存する？"); f4.pack(fill="x", **pad)
        self.rm_copy_var = tk.BooleanVar(value=True)
        ttk.Radiobutton(f4, text="コピーを作って作り直す（元の動画はそのまま残す）★おすすめ",
                        variable=self.rm_copy_var, value=True).pack(anchor="w", padx=14, pady=(12, 2))
        ttk.Radiobutton(f4, text="元のフォルダを直接作り直す（上書き・元の動画は消えます）",
                        variable=self.rm_copy_var, value=False).pack(anchor="w", padx=14, pady=(0, 12))

        # ── 実行 ──
        rowx = ttk.Frame(p); rowx.pack(fill="x", padx=16, pady=(4, 20))
        self.rm_start_btn = ttk.Button(rowx, text="🔁 この内容で作り直す", command=self._rm_start)
        self.rm_start_btn.pack(side="left")
        _hint(rowx, "作り直しの進み具合は「ログ」タブに出ます。").pack(side="left", padx=(12, 0))
        self._rm_reload_list()
        self._rm_update_voices()   # 起動直後から声を選べるようにする

    # ── 作り直し：ヘルパー ──
    def _rm_out_root(self) -> Path:
        return Path(self.out_var.get() or (Path.cwd() / "output"))

    def _rm_reload_list(self):
        """output フォルダの動画一覧を新しい順で読み込む。"""
        try:
            dirs = sorted([d for d in [*self._rm_out_root().glob("IZANAMI_*"),
                                       *self._rm_out_root().glob("IZANAGI_*")] if d.is_dir()],
                          key=lambda d: d.stat().st_mtime, reverse=True)[:60]
        except Exception:
            dirs = []
        self._rm_dirs = {d.name: d for d in dirs}
        self.rm_dir_combo["values"] = list(self._rm_dirs.keys())
        if dirs and not self.rm_dir_var.get():
            self.rm_dir_var.set(dirs[0].name)
        self._rm_refresh_info()

    def _rm_pick_dir(self):
        d = filedialog.askdirectory(title="作り直す動画フォルダ(IZANAMI_...)を選ぶ",
                                    initialdir=str(self._rm_out_root()))
        if d:
            if not project.looks_like_project(d):
                messagebox.showwarning(
                    "IZANAMI",
                    "IZANAMIの動画フォルダではないようです。\n"
                    "「IZANAMI_日付_時刻」（旧IZANAGI_も可）という名前のフォルダを選んでください。\n"
                    "（保存先フォルダ自体を選ぶと、中の動画が全部コピーされてしまいます）")
                return
            p = Path(d)
            self._rm_dirs[p.name] = p
            vals = list(self.rm_dir_combo["values"])
            if p.name not in vals:
                self.rm_dir_combo["values"] = [p.name] + vals
            self.rm_dir_var.set(p.name)
            self._rm_refresh_info()

    def _rm_selected(self) -> Path | None:
        name = self.rm_dir_var.get().strip()
        d = getattr(self, "_rm_dirs", {}).get(name)
        return d if d and Path(d).exists() else None

    def _rm_open_dir(self):
        d = self._rm_selected()
        if d:
            try:
                os.startfile(str(d))
            except Exception as e:
                messagebox.showerror("IZANAMI", f"フォルダを開けませんでした: {e}")

    def _rm_open_script(self):
        d = self._rm_selected()
        if not d:
            messagebox.showinfo("IZANAMI", "先に動画フォルダを選んでください。"); return
        s = Path(d) / "script" / "script.txt"
        if not s.exists():
            messagebox.showinfo("IZANAMI", "この動画には台本がまだありません。"); return
        try:
            os.startfile(str(s))
            self._log("📝 台本を開きました。書き換えて保存したら、②音声・③画像にチェックして作り直してください。")
        except Exception as e:
            messagebox.showerror("IZANAMI", f"台本を開けませんでした: {e}")

    def _rm_play_video(self):
        d = self._rm_selected()
        if not d:
            messagebox.showinfo("IZANAMI", "先に動画フォルダを選んでください。"); return
        v = Path(d) / "compose" / "final.mp4"
        if not v.exists():
            messagebox.showinfo("IZANAMI", "この動画はまだ完成していません（final.mp4 がありません）。"); return
        try:
            os.startfile(str(v))
        except Exception as e:
            messagebox.showerror("IZANAMI", f"動画を開けませんでした: {e}")

    def _rm_refresh_info(self):
        """選択中の動画の状態＋今のチェックで何が作り直しになるかを表示する。"""
        d = self._rm_selected()
        if not d:
            self.rm_info.config(text="（動画フォルダを選んでください）")
            return
        try:
            # 表示更新なのでフォルダを作らない（Project.open は空フォルダ6個を作る）
            proj = project.Project(Path(d))
            meta = util.load_json(Path(d) / "script" / "meta.json", {}) or {}
            rc = proj.load_run_cfg()
            title = (meta.get("title") or rc.get("topic") or "").strip()
            marks = " / ".join(
                f"{'✅' if proj.done(s) else '⬜'}{project.STAGE_LABEL.get(s, s)}"
                for s in project.STAGES)
            picked = [s for s, v in self.rm_stage_vars.items() if v.get()]
            todo = project.expand_remake_stages(picked)
            plan = ("→ 作り直す工程： " + "、".join(project.STAGE_LABEL.get(s, s) for s in todo)
                    if todo else "→ 作り直す工程にチェックを入れてください")
            reuse = [project.STAGE_LABEL.get(s, s) for s in project.STAGES
                     if s not in todo and proj.done(s)]
            reuse_t = ("　／ そのまま使う： " + "、".join(reuse)) if (todo and reuse) else ""
            sizes = (f"　元の枚数： 動画{rc.get('num_video_clips', '?')}本・"
                     f"画像{rc.get('num_images', '?')}枚" if rc else "")
            self.rm_info.config(
                text=(f"お題/タイトル： {title or '(不明)'}\n状態： {marks}{sizes}\n{plan}{reuse_t}"))
        except Exception as e:
            self.rm_info.config(text=f"（読み込めませんでした: {str(e)[:80]}）")

    # ── 声/BGMの上書き（作り直しタブ・ステップ実行タブ共通） ──
    def _voice_values_for(self, eng):
        """そのTTSエンジンで選べる声の表示名一覧（fishは登録済みの声）。"""
        try:
            _lab, vals = self._VOICE_SETS.get(eng, ("声", []))
            return self._fish_voice_values() if eng == "fish" else list(vals)
        except Exception:
            return []

    def _set_voice_override_combo(self, combo, var, eng):
        """上書き用の声コンボ（空欄＝今の設定のまま）をエンジンに合わせて差し替える。"""
        vals = self._voice_values_for(eng)
        if eng == "fish":   # ID空＝1行ごとに声が変わる「（デフォルトの声）」は上書き候補にしない
            vals = [v for v in vals if v != "（デフォルトの声）"]
        combo.config(values=[""] + list(vals), state="normal" if eng == "fish" else "readonly")
        # fishは声コード直貼りの自由入力＝一覧に無くても消さない（復元値を守る）。
        # 他エンジンは選択式なので、そのエンジンに無い声は空欄（=今の設定のまま）へ戻す
        if eng != "fish" and var.get() not in vals:
            var.set("")
        elif eng == "fish":
            # 他エンジンの声名（例 ja-JP-NanamiNeural (女)）が残ったままfishへ切り替わると、
            # その文字列が声IDとして送られてしまう＝他エンジンの一覧にある値は空欄へ戻す
            others = set()
            for _k, (_l, _vs) in self._VOICE_SETS.items():
                if _k != "fish":
                    others.update(_vs)
            if var.get() in others:
                var.set("")

    def _apply_voice_override(self, cfg, disp):
        """声欄で選んだ時だけ、そのエンジンの声キーを差し替える（空欄＝今の設定のまま）。"""
        disp = (disp or "").strip()
        if not disp:
            return
        eng = cfg.get("tts_engine")
        if eng == "fish":
            cfg["fish_voice"] = self._fish_display_to_id(disp)
        else:
            key = {"gemini": "gemini_tts_voice", "edge": "edge_voice"}.get(eng, "tts_voice")
            cfg[key] = disp.split(" ")[0]
        # 「この声で」と明示した以上、主人公性別の自動切替で別の声に差し替えない
        vbg = cfg.get("voice_by_gender")
        if isinstance(vbg, dict) and vbg.get("auto_enabled"):
            cfg["voice_by_gender"] = dict(vbg, auto_enabled=False)
            try:
                self._log("声欄で選んだ声を優先します（主人公の性別による声の自動調整は、この実行では行いません）。")
            except Exception:
                pass

    def _bgm_choices(self):
        return (["元のまま", "おまかせ（再抽選）"]
                + [e.get("title") or Path(e.get("path", "")).stem
                   for e in (self.cfg.get("bgm_library") or [])])

    def _refresh_bgm_combos(self):
        """🎵BGMタブで曲が増えた/改名された時に、作り直し/ステップ実行のBGM欄へ反映。"""
        for name in ("rm_bgm_combo", "st_bgm_combo"):
            cb = getattr(self, name, None)
            if cb is not None:
                try:
                    cb.config(values=self._bgm_choices())
                except Exception:
                    pass

    def _resolve_bgm_override(self, sel):
        """🎵BGM欄の選択→④合成のcfgへ足すキー。「元のまま」は{}、照合できなければ警告してNone。"""
        sel = (sel or "").strip()
        if sel in ("", "元のまま"):
            return {}
        if sel == "おまかせ（再抽選）":
            return {"bgm_mode": "random", "_bgm_reshuffle": True}
        for _e in (self.cfg.get("bgm_library") or []):
            _t = _e.get("title") or Path(_e.get("path", "")).stem
            if _t == sel:
                _p = _e.get("path", "")
                if _p and Path(_p).exists():
                    return {"bgm_mode": "select", "bgm_selected": _p}
                break
        # 曲名が照合できない/ファイルが無い時は黙って別のBGM設定で再合成しない
        # （🎵タブでの改名・削除後に古い曲名が残ることがある・敵対検証2026-08-19）
        messagebox.showwarning(
            "IZANAMI",
            f"選んだBGM「{sel}」が見つかりません\n"
            "（曲名の変更・削除後は選び直しが必要です）。\n"
            "🎵BGM欄を選び直してから、もう一度実行してください。")
        return None

    def _rm_update_voices(self):
        """作り直しタブのTTSエンジンに合わせて声の選択肢を差し替える（空欄＝今の設定のまま）。"""
        eng = pval(ENG_LABELS["tts"], self.rm_engine_vars["tts"].get())
        self._set_voice_override_combo(self.rm_voice_combo, self.rm_voice_var, eng)

    def _rm_reset_engines(self):
        for cap, v in self.rm_engine_vars.items():
            v.set(self.engine_vars[cap].get())
        self._rm_update_voices()
        self.rm_voice_var.set("")

    def _rm_oped_only(self):
        """作り直しタブ: ①で選んだ動画に、今のOP/ED動画だけ付け直す（本編・サムネは触らない）。"""
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("IZANAMI", "いま別の処理を実行中です。終わってからお試しください。"); return
        if getattr(self, "_thumb_busy", False):
            messagebox.showinfo("IZANAMI", "サムネを作り直し中です。終わってからお試しください。"); return
        d = self._rm_selected()
        if not d:
            messagebox.showwarning("IZANAMI", "①で付け直す動画フォルダを選んでください。"); return
        if not (Path(d) / "compose" / "final.mp4").exists():
            messagebox.showwarning("IZANAMI", "この動画には完成動画（compose/final.mp4）がありません。"); return
        cfg = self._gather_cfg()
        try:
            sel = pipeline.channel_oped_for(str(d), cfg)
        except Exception:
            sel = {"intro_video": cfg.get("intro_video", ""), "outro_video": cfg.get("outro_video", ""),
                   "channel": "", "from_channel": False}
        intro, outro = sel.get("intro_video", ""), sel.get("outro_video", "")
        miss = [f"{l}: {p}" for l, p in (("OP", intro), ("ED", outro)) if p and not Path(p).exists()]
        if miss:
            messagebox.showwarning("IZANAMI", "OP/EDの動画ファイルが見つかりません。\n"
                                   "冒頭・終了タブで「参照…」から選び直してください。\n\n" + "\n".join(miss))
            return
        src = (f"チャンネル「{sel.get('channel')}」" if sel.get("from_channel")
               else ("この動画を作った時" if sel.get("from_run_cfg") else "冒頭・終了タブ（いま表示中のチャンネル）"))
        if not (intro or outro):
            if not messagebox.askyesno(
                    "終了画面の付け直し",
                    f"{src}のOP/ED動画が両方とも空です。\n"
                    "このまま実行すると、付いている冒頭・終了画面を外して本編だけの動画にします。\n\n"
                    "続けますか？"):
                return
        from izanagi import compose as _compose
        rec = _compose.read_oped_record(Path(d) / "compose" / "final.mp4")
        assume = False
        is_neko = False
        try:
            is_neko = pipeline.is_neko_project(str(d))
        except Exception:
            pass
        if is_neko:
            pass   # 猫ミームは④合成をやり直して付け直す（同梱の終了画面の二重付けを防ぐ）＝旧版確認は不要
        elif rec is None and not (Path(d) / "compose" / _compose.BODY_MP4).exists():
            # この機能より前に作られた動画＝終了画面が入っているか機械判定できない
            if not messagebox.askyesno(
                    "旧版の動画です",
                    "この動画は「本編の控え」が無い旧版で作られているため、\n"
                    "いまの動画に終了画面が入っているかを自動で判定できません。\n\n"
                    "【入っていない（本編だけ）】なら「はい」→ そのまま付けます。\n"
                    "【すでに入っている】なら「いいえ」→ 二重に付くのを防ぐため中止します\n"
                    "（その場合は、この画面の「🎵 BGM」欄で今の曲名（または おまかせ）を選んで\n"
                    "「🔁 この内容で作り直す」＝④合成から作り直すと、終了画面も今の設定で付き直ります）。\n\n"
                    "この動画に終了画面は入っていませんか？（はい＝入っていない）"):
                return
            assume = True
        msg = (f"「{Path(d).name}」に次を付け直します。\n\n"
               f"　OP: {Path(intro).name if intro else '（なし）'}\n"
               f"　ED: {Path(outro).name if outro else ('（なし・猫ミーム同梱の終了画面）' if is_neko else '（なし）')}\n"
               f"　（{src}の設定）\n\n"
               + ("猫ミームのため④合成をやり直してから付けます（AIは使いません・数分）。\n" if is_neko else "")
               + "本編・サムネはそのまま、このフォルダの final.mp4 を直接更新します。\n"
               "よろしいですか？")
        if not messagebox.askyesno("終了画面の付け直し", msg):
            return
        self._stop = False
        self.rm_start_btn.config(state="disabled")
        self.start_btn.config(state="disabled"); self.stop_btn.config(state="normal")
        self._clear_log()
        self._set_status("終了画面を付け直し中…")
        self._timer_begin()
        self.worker = threading.Thread(target=self._rm_oped_worker, args=(cfg, str(d), assume), daemon=True)
        self.worker.start()
        try:
            self.nb.select(self.tab_log)
        except Exception:
            pass

    def _rm_oped_worker(self, cfg, src_dir, assume):
        util.prevent_sleep(True)
        self._log_session_header("終了画面の付け直し")
        try:
            self._log("=" * 46)
            self._log(f"🎬 終了画面（OP/ED）の付け直し: {Path(src_dir).name}")
            r = pipeline.reattach_oped(src_dir, cfg, log=self._log, stop=lambda: self._stop,
                                       assume_body=assume)
            if r.get("ok"):
                self._last_out = src_dir
                self._log(f"\n🎬 付け直しできました。保存先: {src_dir}")
                self._log("→ 「投稿」タブに読み込みました（サムネ・タイトルはそのまま）。")
            else:
                self._log("⚠ 付け直せませんでした: " + str(r.get("error", "")) + " " + str(r.get("detail", "")))
        except Exception:
            self._log("【エラー】\n" + traceback.format_exc())
        finally:
            util.prevent_sleep(False)
            self.log_queue.put("__DONE__")

    def _rm_start(self):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("IZANAMI", "いま別の処理を実行中です。終わってからお試しください。"); return
        d = self._rm_selected()
        if not d:
            messagebox.showwarning("IZANAMI", "作り直す動画フォルダを選んでください。"); return
        if getattr(self, "_thumb_busy", False):
            messagebox.showinfo("IZANAMI", "サムネを作り直し中です。終わってからお試しください。"); return
        picked = [s for s, v in self.rm_stage_vars.items() if v.get()]
        # BGM入れ替え（2026-08-17要望）: 「元のまま」以外ならBGMは④合成で焼くため
        # ④を自動で含める（チェックだけでBGM差し替えの1操作にする）
        _bgm_sel = (self.rm_bgm_var.get() or "").strip() if hasattr(self, "rm_bgm_var") else ""
        _bgm_override = _bgm_sel not in ("", "元のまま")
        _bgm_only = _bgm_override and not picked   # BGMだけ＝④だけ（⑤サムネは作り直さない）
        if _bgm_override and "compose" not in picked:
            picked.append("compose")
        if not picked:
            messagebox.showwarning("IZANAMI", "作り直す工程にチェックを入れてください"
                                              "（BGMだけ変える場合はBGM欄を選ぶだけでOK）。"); return
        # 猫ミームに②音声は無い（無音WAV＋文字数タイミング）＝②に✔すると
        # 実質④合成まで作り直しになる。知らずに✔して「⑤だけのつもりが動画まで
        # 作り直された」となる報告があったため、先に知らせる（2026-08-03）
        try:
            rc = project.Project(Path(d)).load_run_cfg()
            # 猫ミームのBGMは猫素材フォルダから自動選択される仕組み＝この欄では変えられない
            # （黙って全再合成＋ランダム引き直しになるのを防ぐ・敵対検証2026-08-19）
            if rc.get("video_mode") == "neko" and _bgm_override:
                messagebox.showwarning(
                    "IZANAMI",
                    "この動画は猫ミームです。猫ミームのBGMは猫素材フォルダの曲から\n"
                    "自動で選ばれる仕組みのため、🎵BGM欄では変更できません。\n"
                    "（BGM欄を「元のまま」に戻してから実行してください）")
                return
            if rc.get("video_mode") == "neko" and "tts" in picked and \
                    not messagebox.askyesno(
                        "確認（猫ミーム）",
                        "この動画は猫ミームです。猫ミームに「②音声」は無いため、\n"
                        "②に✔すると実質④合成（動画の作り直し）まで行われます。\n\n"
                        "このまま続けますか？\n"
                        "（サムネだけ作り直したい場合は⑤だけに✔してください）"):
                return
        except Exception:
            pass
        todo = ["compose"] if _bgm_only else project.expand_remake_stages(picked)
        labels = "、".join(project.STAGE_LABEL.get(s, s) for s in todo)
        if _bgm_only:
            labels += "（BGMだけ＝サムネ・タイトルはそのまま）"
        overwrite = not self.rm_copy_var.get()
        msg = (f"「{Path(d).name}」を作り直します。\n\n作り直す工程： {labels}\n"
               + ("★元のフォルダを上書きします（元の動画は消えます）" if overwrite
                  else "コピーを作って作り直します（元はそのまま残ります）"))
        if not messagebox.askyesno("作り直しの確認", msg):
            return
        cfg = self._gather_cfg()          # 画面の設定を土台に…
        cfg["_remake"] = True             # 見た目・分量の設定は画面のものを勝たせる
        if _bgm_only:
            cfg["_bgm_only"] = True
        for cap, v in self.rm_engine_vars.items():   # …エンジンだけこのタブの選択で上書き
            cfg[f"{cap}_engine"] = pval(ENG_LABELS[cap], v.get())
        cfg["brushup_engine"] = cfg["script_engine"]
        cfg["upload_engine"] = "none"
        # 声を選んだ時だけ、そのエンジンの声を差し替える（空欄＝今の設定のまま）
        self._apply_voice_override(cfg, self.rm_voice_var.get())
        # 本数・枚数: このタブで指定した時だけその値、空欄＝元の動画と同じ
        # （num_imagesは「画面が勝つ」キーのため、空欄時は元動画の値をcfgへ写して
        #   作成タブの枚数で意図せず変わらないようにする。2026-08-05ユーザー要望）
        rc0 = {}
        try:
            rc0 = project.Project(Path(d)).load_run_cfg() or {}
        except Exception:
            pass
        for var, key in ((self.rm_nimg_var, "num_images"),
                         (self.rm_nclip_var, "num_video_clips")):
            s = (var.get() or "").strip()
            try:
                cfg[key] = int(s) if s else int(rc0.get(key, cfg.get(key, 1)))
            except (TypeError, ValueError):
                pass
        # BGMの差し替え指定を④合成のcfgへ反映（この画面専用＝設定は保存しない）
        if _bgm_override:
            _bgm = self._resolve_bgm_override(_bgm_sel)
            if _bgm is None:
                return
            cfg.update(_bgm)
        elif "compose" in todo:
            # 「元のまま」の約束を守る: bgm_mode/bgm_selectedは「画面が勝つ」キーのため、
            # 元動画のrun_cfgの値をcfgへ写しておく＝作成タブのBGM設定に無言で差し替わらない
            # （敵対検証2026-08-19。本数・枚数のrc0方式と同じ）
            for _k in ("bgm_mode", "bgm_selected"):
                if _k in rc0:
                    cfg[_k] = rc0[_k]
        # ※ここでの選択は「この画面専用」＝self.cfg も settings.json も変更しない
        #   （保存すると作成タブのエンジン設定が作り直しの選択で永久に上書きされる）
        self._stop = False
        self.rm_start_btn.config(state="disabled")
        self.start_btn.config(state="disabled"); self.stop_btn.config(state="normal")
        self._clear_log()
        self._set_status("作り直し中…")
        self._timer_begin()
        self.worker = threading.Thread(target=self._rm_worker,
                                       args=(cfg, str(d), picked, overwrite), daemon=True)
        self.worker.start()
        try:
            self.nb.select(self.tab_log)
        except Exception:
            pass

    def _rm_worker(self, cfg, src_dir, picked, overwrite):
        util.prevent_sleep(True)
        self._log_session_header(f"作り直し（{'/'.join(picked)}）")
        out, bk, target, done_all = "", None, Path(src_dir), False
        try:
            self._log("=" * 46)
            self._log(f"🔁 作り直し: {Path(src_dir).name}")
            target = Path(src_dir)
            if not overwrite:
                target = project.copy_project(src_dir, self._rm_out_root(), log=self._log)
            # 投稿タブでの編集（タイトル/概要/タグ/予約時刻/チャンネル）を引き継ぐため、
            # 消す前の memo を控えておく（⑤を作り直しても手編集が消えないようにする）
            old_memo = project.load_memo(target)
            try:
                # BGMだけ＝④だけ作り直す（⑤へ依存展開しない＝サムネ再生成の課金をしない）
                todo, bk = project.clear_stages(target, picked, log=self._log,
                                                expand=not cfg.get("_bgm_only"))
            except RuntimeError as e:   # 元ファイルを移動できない＝作り直しを始めない
                self._log("⚠ " + str(e))
                # ダイアログはUIスレッドで出す（ワーカーから直接出すとtkinterが固まる）
                self.root.after(0, lambda m=str(e):
                                messagebox.showwarning("作り直しを中止しました", m))
                return
            if old_memo:
                cfg = dict(cfg)
                cfg["_keep_memo"] = old_memo
                cfg["_keep_memo_texts"] = "script" not in todo  # 台本が同じなら題名等も保持
            keep = [project.STAGE_LABEL.get(s, s) for s in project.STAGES if s not in todo]
            if keep:
                self._log("    ♻ そのまま使う: " + "、".join(keep))
            out = pipeline.run(cfg, log=self._log, stop_flag=lambda: self._stop,
                               resume_dir=str(target))
            self._last_out = out
            done_all = True
            proj = project.Project(Path(out))
            failed = [s for s in todo if not proj.done(s)]
            if failed:   # 途中で止まった → 作り直す**前**の状態へ完全に戻す
                done_all = False
                self._log("⚠ 作り直しが最後まで進みませんでした: "
                          + "、".join(project.STAGE_LABEL.get(s, s) for s in failed))
            elif (Path(out) / "compose" / "final.mp4").exists():
                self._log(f"\n🎬 作り直しできました。保存先: {out}")
                if set(todo) == {"project"}:
                    # ⑤のみ＝動画は触っていない。コピー方式だと新フォルダに同じ動画が
                    # 入るため「動画まで作り直された」と誤解されやすい（2026-08-03報告）
                    self._log("    ※ 動画(final.mp4)は再作成していません"
                              "（サムネ・タイトル等の⑤だけを作り直し。"
                              + ("動画は元のコピーそのままです）" if not overwrite
                                 else "動画は元のままです）"))
                    if not overwrite and (Path(src_dir) / "uploaded.flag").exists():
                        self._log("    ⚠ 元の動画は投稿済みです。このコピーは未投稿扱いに"
                                  "なるため、そのまま🚀すると同じ動画を二重投稿します。"
                                  "サムネ差し替えだけが目的なら、投稿タブの"
                                  "「🖼 AIで作り直す」をお使いください。")
                self._log("→ 「投稿」タブに読み込みました。内容を確認して 🚀 予約投稿できます。")
                # 成功したら古い退避は不要（直前1回分だけ残す）。放っておくと
                # 1回あたり数百MBが恒久的に積み上がる
                try:
                    project.prune_backups(out, keep=1, log=self._log)
                except Exception:
                    pass
        except Exception:
            self._log("【エラー】\n" + traceback.format_exc())
        finally:
            # 中断・例外・×終了のどれでも、失敗なら元へ戻す（新旧が混ざった動画を残さない）
            try:
                if not done_all and bk:
                    if project.restore_backup(out or str(target), bk,
                                              log=self._log, overwrite=True):
                        self._log("    ※ 【作り直す前】の状態に戻しました。"
                                  "もう一度「🔁 この内容で作り直す」でやり直せます。")
            except Exception:
                pass
            util.prevent_sleep(False)
            self.log_queue.put("__DONE__")

    def _build_prompt(self, p):
        bar = ttk.Frame(p); bar.pack(fill="x", padx=12, pady=(10, 0))
        ttk.Label(bar, text="チャンネル", font=(FB, 11, "bold")).pack(side="left")
        cb = ttk.Combobox(bar, width=24, state="readonly")
        cb.pack(side="left", padx=(6, 16))
        cb.bind("<<ComboboxSelected>>", self._on_channel_selected)
        self._channel_combos.append(cb)
        ttk.Label(bar, text="チャンネル名").pack(side="left")
        self.ch_name_var = tk.StringVar()
        ttk.Entry(bar, textvariable=self.ch_name_var, width=20).pack(side="left", padx=(4, 4))
        ttk.Button(bar, text="↻ 名前を反映", command=self._rename_channel).pack(side="left")
        _hint(p, "ここの指示は選択中のチャンネルに保存されます（10チャンネルまで）。空欄なら既定の指示が使われます。"
                 "チャンネルを切り替えると作成タブにも反映されます。").pack(anchor="w", padx=12, pady=(8, 14))
        # 初期表示は「アクティブチャンネルの生の値」から読む。トップレベル展開値だと
        # youtube_channel_url が全体設定URLで埋まって見え、保存時にチャンネルへ
        # 刻印されてしまう（グローバル⇄ch相互汚染＝2026-07-16監査 #19）
        try:
            _ch_active = self.cfg["channels"][self.cfg.get("active_channel", 0)]
        except Exception:
            _ch_active = {}
        self.prompt_texts = {}
        for key, label, h in [("script_prompt", "台本づくりの方針（AIの性格・構成のルール）", 7),
                              ("topic_prompt",
                               "お題の自動生成への指示（💡お題を自動生成／🌙今夜の分 の指示部を丸ごと差し替え。空欄＝標準の指示）", 4),
                              ("brushup_prompt", "台本の推敲（磨き）への指示", 4),
                              ("gemini_tts_style",
                               "音声の話し方（②音声が Gemini（API）の時だけ。例：落ち着いた低めの声で、朗読ナレーターのように感情を込めて）", 3),
                              ("visual_prompt_template", "画像の世界観（英語推奨）", 4),
                              ("thumbnail_prompt", "サムネのイメージ指示（背景のみ。文字はツールが自動で焼き込む）", 4),
                              ("desc_footer", "概要欄の定型フッター（チャンネル登録導線・SNS等。全動画の概要欄末尾に入る）", 3)]:
            fr = ttk.LabelFrame(p, text=label); fr.pack(fill="both", expand=True, padx=14, pady=7)
            t = _mk_text(fr, height=h, size=11)
            t.pack(fill="both", expand=True, padx=8, pady=(12, 10))  # 見出しと入力欄の間に余白
            t.insert("1.0", _ch_active.get(key, self.cfg.get(key, "")))
            self.prompt_texts[key] = t
            if key == "topic_prompt":
                # お題生成の指示部は欄の内容で丸ごと差し替わる（2026-08-03ユーザー要望）。
                # 標準の指示文（クリック誘引・実在事件は西暦+固有名詞ルール）を欄へ
                # 挿入して、編集の土台にできるボタン
                br = ttk.Frame(fr); br.pack(fill="x", padx=8, pady=(0, 8))
                ttk.Button(br, text="📋 標準の指示文を欄に入れて編集する",
                           command=self._insert_default_topic_guide).pack(side="left")
                _hint(br, "空欄のままなら標準の指示が使われます。書けば全文がそれに置き換わります。"
                      ).pack(side="left", padx=(10, 0))

        # ── チャンネル運用設定（タイトル・タグ・投稿先・時刻） ──
        opf = ttk.LabelFrame(p, text="チャンネル運用設定"); opf.pack(fill="x", padx=14, pady=5)
        og = ttk.Frame(opf); og.pack(fill="x", padx=8, pady=8)
        self.channel_vars = {}
        for r, (k, lab, hint) in enumerate([
                ("title_prefix", "タイトル接頭辞", "例:【スカッと】 全動画のタイトル先頭に付く"),
                ("fixed_tags", "固定タグ", "カンマ区切り。全動画のタグに追加され、先頭3個が#タグになる"),
                ("youtube_channel_url", "投稿先チャンネルURL", "このチャンネルの投稿先（誤爆投稿の防止）"),
                ("schedule_slots", "投稿時刻スロット", "例 09:00,18:00（空=設定タブの全体スロットを使う）")]):
            ttk.Label(og, text=lab, width=18).grid(row=r, column=0, sticky="w")
            v = tk.StringVar(value=_ch_active.get(k, self.cfg.get(k, "")))
            ttk.Entry(og, textvariable=v, width=34).grid(row=r, column=1, sticky="ew", padx=6, pady=3)
            _hint(og, hint, row=r, column=2, sticky="w", padx=(8, 0))
            self.channel_vars[k] = v
        # 概要欄の目次（チャプター）: 既定OFF（全チャンネル共通の全体設定）。
        # 従来は必ず挿入され、見出しが文の断片・時刻も最大68秒ズレていた（2026-08-05配布先報告）
        self.chapters_var = tk.BooleanVar(value=bool(self.cfg.get("desc_chapters", False)))
        ttk.Checkbutton(og, text="概要欄に目次（チャプター）を入れる",
                        variable=self.chapters_var).grid(row=4, column=0, columnspan=2,
                                                         sticky="w", pady=(8, 0))
        _hint(og, "全チャンネル共通。時刻は音声の実時刻・見出しはAI要約（AI不可時は台本の文頭）。"
                  "ストーリー系は目次なし（OFF）が定番です。",
              row=4, column=2, sticky="w", padx=(8, 0), pady=(8, 0))
        og.columnconfigure(1, weight=1)

        # ── お題キュー（🌙夜間バッチの燃料） ──
        qf = ttk.LabelFrame(p, text="お題キュー（🌙「今夜の分を回す」で上から順に使われ、完成した分は自動で消えます）")
        qf.pack(fill="x", padx=14, pady=7)
        qrow = ttk.Frame(qf); qrow.pack(fill="x", padx=8, pady=(12, 2))  # 見出しと一覧の間に余白
        self.queue_list = tk.Listbox(qrow, height=7, font=(FB, 11), bg=INK2, fg="#eceef6",
                                     selectbackground=SEL, relief="flat", highlightthickness=1,
                                     highlightbackground=LINE, activestyle="none")
        self.queue_list.pack(side="left", fill="x", expand=True)
        qsb = ttk.Scrollbar(qrow, orient="vertical", command=self.queue_list.yview)
        self.queue_list.config(yscrollcommand=qsb.set)
        qsb.pack(side="left", fill="y")
        qb = ttk.Frame(qf); qb.pack(fill="x", padx=8, pady=(2, 8))
        ttk.Button(qb, text="＋ 追加", command=self._queue_add).pack(side="left")
        ttk.Button(qb, text="🗑 削除", command=self._queue_del).pack(side="left", padx=6)
        ttk.Button(qb, text="💡 ネタ帳から10個補充", command=lambda: self._gen_topics_clicked(10, True)
                   ).pack(side="left", padx=6)
        for t in (self.cfg.get("channels", [{}])[self.cfg.get("active_channel", 0)]
                  .get("topic_queue") or []):
            self.queue_list.insert("end", t)

    def _build_oped(self, p):
        """冒頭・終了タブ: OP/ED動画と定型ナレーションをチャンネルごとに登録する。"""
        bar = ttk.Frame(p); bar.pack(fill="x", padx=12, pady=(10, 0))
        ttk.Label(bar, text="チャンネル", font=(FB, 11, "bold")).pack(side="left")
        cb = ttk.Combobox(bar, width=24, state="readonly")
        cb.pack(side="left", padx=(6, 16))
        cb.bind("<<ComboboxSelected>>", self._on_channel_selected)
        self._channel_combos.append(cb)
        _hint(p, "動画の最初と最後に入れるものを、選択中のチャンネルに保存します。空欄=無し。"
                 "定型ナレーションは台本の最初/最後に挿入され読み上げ＋テロップ付き、"
                 "OP/ED動画は完成動画の前後に連結されます（両方使う場合は 冒頭=OP動画→冒頭文、"
                 "終了=終了文→ED動画 の順になります）。").pack(anchor="w", padx=12, pady=(8, 10))

        try:
            _ch = self.cfg["channels"][self.cfg.get("active_channel", 0)]
        except Exception:
            _ch = {}

        def _video_row(parent, key, label):
            row = ttk.Frame(parent); row.pack(fill="x", padx=8, pady=(10, 4))
            ttk.Label(row, text=label, width=14).pack(side="left")
            v = tk.StringVar(value=_ch.get(key, ""))
            ttk.Entry(row, textvariable=v).pack(side="left", fill="x", expand=True, padx=6)

            def _pick():
                f = filedialog.askopenfilename(
                    title=f"{label}を選ぶ",
                    filetypes=[("動画ファイル", "*.mp4 *.mov *.mkv *.webm *.avi"),
                               ("すべて", "*.*")])
                if f:
                    v.set(f)
                    self._save_channel_fields(self.cfg.get("active_channel", 0))
                    config.save_settings(self.cfg)

            def _clear():
                v.set("")
                self._save_channel_fields(self.cfg.get("active_channel", 0))
                config.save_settings(self.cfg)

            ttk.Button(row, text="参照…", command=_pick).pack(side="left", padx=(0, 4))
            ttk.Button(row, text="×", width=3, command=_clear).pack(side="left")
            self.channel_vars[key] = v

        def _text_box(parent, key, label, hint):
            fr = ttk.LabelFrame(parent, text=label)
            fr.pack(fill="both", expand=True, padx=8, pady=(6, 10))
            t = _mk_text(fr, height=3, size=11)
            t.pack(fill="both", expand=True, padx=8, pady=(10, 8))
            t.insert("1.0", _ch.get(key, ""))
            self.prompt_texts[key] = t
            _hint(fr, hint).pack(anchor="w", padx=8, pady=(0, 8))

        opf = ttk.LabelFrame(p, text="🎬 冒頭（動画の最初に入る）")
        opf.pack(fill="both", expand=True, padx=14, pady=7)
        _video_row(opf, "intro_video", "OP動画ファイル")
        _text_box(opf, "intro_text", "冒頭の定型ナレーション",
                  "例: この物語はフィクションです。実在の人物・団体とは関係ありません。")

        edf = ttk.LabelFrame(p, text="🏁 終了（動画の最後に入る）")
        edf.pack(fill="both", expand=True, padx=14, pady=7)
        # ED動画は枠の先頭に（2026-08-19bで最下段へ押し出され「設定が消えた」と見えた。
        # 再生順は 終了文→ED動画 のまま＝表示順だけ）
        _video_row(edf, "outro_video", "ED動画ファイル（終了文の後に流れます）")
        _text_box(edf, "outro_text", "終了の定型ナレーション（横動画用）",
                  "例: チャンネル登録と高評価で応援してもらえると励みになります。")
        _text_box(edf, "outro_text_shorts", "ショート用のしめ誘導文（縦動画はこちらだけが使われます）",
                  "例: 続きが気になったらフォロー！次の話はプロフィールから。"
                  "（空欄=縦動画には何も付けない。横用の文とED動画は縦では使われません）")

    def _build_bgm(self, p):
        _hint(p, "「動画に合わせて自動選曲」＝台本からジャンルを判定し、下のライブラリから同ジャンルの曲を自動で選びます"
                 "（同ジャンルが無ければ全体からランダム）。曲に「ジャンル」を付けておくと精度が上がります。").pack(
            anchor="w", padx=16, pady=10)
        self.bgm_lib = list(self.cfg.get("bgm_library", []))
        top = ttk.Frame(p); top.pack(fill="x", padx=16)
        ttk.Label(top, text="BGMの使い方").pack(side="left")
        self.bgm_mode_var = tk.StringVar(value=pdisp(BGM_PAIRS, self.cfg.get("bgm_mode", "none")))
        ttk.Combobox(top, textvariable=self.bgm_mode_var, values=[d for _, d in BGM_PAIRS],
                     width=28, state="readonly").pack(side="left", padx=6)
        ttk.Label(top, text="音量").pack(side="left", padx=(12, 2))
        self.bgm_db_var = tk.StringVar(value=str(self.cfg.get("bgm_base_db", -8)))
        ttk.Spinbox(top, textvariable=self.bgm_db_var, from_=-30, to=0, width=5).pack(side="left")
        ttk.Label(top, text="dB", style="Muted.TLabel").pack(side="left", padx=(2, 0))
        ttk.Label(top, text="猫ミームBGM").pack(side="left", padx=(12, 2))
        self.neko_bgm_db_var = tk.StringVar(value=str(self.cfg.get("neko_bgm_db", -14)))
        ttk.Spinbox(top, textvariable=self.neko_bgm_db_var, from_=-30, to=0, width=5).pack(side="left")
        ttk.Label(top, text="dB（小さいほど静か）", style="Muted.TLabel").pack(side="left", padx=(2, 0))
        lf = ttk.Frame(p); lf.pack(fill="both", expand=True, padx=14, pady=6)
        self.bgm_list = tk.Listbox(lf, height=8, font=(FB, 11), bg=INK2, fg="#eceef6",
                                   selectbackground=SEL, selectforeground="#ffffff",
                                   relief="flat", highlightthickness=1,
                                   highlightbackground=LINE, activestyle="none",
                                   exportselection=False,   # 他の欄をクリックしても選択が消えない
                                   selectmode="extended")  # Ctrl/Shiftで複数選択（連結用）
        self.bgm_list.pack(side="left", fill="both", expand=True)
        for it in self.bgm_lib:
            self.bgm_list.insert("end", _bgm_row_text(it))
        self.bgm_list.bind("<Double-1>", lambda e: self._bgm_edit())  # ダブルクリックで設定
        # 「選んだ曲」の取りこぼし対策（2026-08-25配布先報告「選んでも反映されない」）:
        # 旧実装は▶を押す瞬間に一覧が選択中の時だけ bgm_selected を保存＝クリック直後に
        # 他タブへ移る等で選択が外れると古い曲のままだった。クリックした時点で控える
        self._bgm_sel_path = (self.cfg.get("bgm_selected") or "").strip()
        self.bgm_list.bind("<<ListboxSelect>>", self._on_bgm_select, add="+")
        self.bgm_sel_lab = ttk.Label(p, text="", foreground=GOLD)
        self.bgm_sel_lab.pack(anchor="w", padx=16)
        self.root.after(200, self._bgm_restore_selection)
        bb = ttk.Frame(p); bb.pack(fill="x", padx=12)
        ttk.Button(bb, text="＋ 追加", command=self._bgm_add).pack(side="left")
        ttk.Button(bb, text="⚙ 設定（ジャンル/チャンネル）", command=self._bgm_edit).pack(side="left", padx=6)
        ttk.Button(bb, text="🗑 削除", command=self._bgm_del).pack(side="left", padx=6)
        self.bgm_join_btn = ttk.Button(bb, text="🔗 つなげて1本に", command=self._bgm_join)
        self.bgm_join_btn.pack(side="left", padx=6)
        _hint(bb, "曲をダブルクリック（または選んで「⚙設定」）で、ジャンルと"
                  "「どのチャンネルで使うか」を設定できます（未設定＝全チャンネルで使用）。").pack(
            side="left", padx=8)

        # Suno新規作成セクションはユーザー判断で撤去（2026-07-06）。
        # music.py の生成コードと _suno_run は温存＝復活させる時はここにUIを戻すだけ。

    def _build_post(self, p):
        pad = {"padx": 16, "pady": 10}  # 枠と枠の間隔
        _hint(p, "上の一覧で✓を入れて「まとめて予約投稿」、または1本選んで下の編集欄で確認→🚀。ダブルクリックで動画を再生。").pack(
            anchor="w", padx=12, pady=(8, 2))

        # ── できた動画の一覧（チェックしてまとめて投稿） ──
        lf = ttk.LabelFrame(p, text="🎞 できた動画"); lf.pack(fill="x", **pad)
        chf = ttk.Frame(lf); chf.pack(fill="x", padx=10, pady=(6, 2))
        ttk.Label(chf, text="チャンネル").pack(side="left")
        self.post_ch_filter_var = tk.StringVar(value="すべて（全チャンネル）")
        self.post_ch_combo = ttk.Combobox(chf, textvariable=self.post_ch_filter_var,
                                          values=["すべて（全チャンネル）"], width=26,
                                          state="readonly")
        self.post_ch_combo.pack(side="left", padx=(8, 12))
        self.post_ch_combo.bind("<<ComboboxSelected>>", self._on_post_channel_filter)
        _hint(chf, "選ぶと一覧がそのチャンネルの動画だけになり、投稿先チャンネルURLも"
                   "そのチャンネルの設定に切り替わります。").pack(side="left")
        tf = ttk.Frame(lf); tf.pack(fill="x", padx=10, pady=(8, 4))
        cols = ("sel", "name", "title", "made", "status")
        self.post_tree = ttk.Treeview(tf, columns=cols, show="headings", height=6,
                                      selectmode="browse")
        for c, (lab, wdt, anc) in {"sel": ("✓", 44, "center"), "name": ("フォルダ", 190, "w"),
                                   "title": ("タイトル", 430, "w"), "made": ("作成", 120, "center"),
                                   "status": ("状態", 130, "center")}.items():
            self.post_tree.heading(c, text=lab)
            self.post_tree.column(c, width=wdt, anchor=anc, stretch=(c == "title"))
        tsb = ttk.Scrollbar(tf, orient="vertical", command=self.post_tree.yview)
        self.post_tree.configure(yscrollcommand=tsb.set)
        self.post_tree.pack(side="left", fill="x", expand=True)
        tsb.pack(side="right", fill="y")
        self.post_tree.bind("<Button-1>", self._post_tree_click)
        self.post_tree.bind("<Double-1>", lambda e: self._post_play_selected())
        self._post_checked: set[str] = set()
        lb = ttk.Frame(lf); lb.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Button(lb, text="🔄 一覧を更新", command=self._post_refresh_list).pack(side="left")
        ttk.Button(lb, text="▶ 再生", command=self._post_play_selected).pack(side="left", padx=6)
        ttk.Button(lb, text="📝 下の編集欄へ読み込む", command=self._post_load_selected).pack(side="left")
        self.post_batch_btn = ttk.Button(lb, text="🚀 チェックした動画をまとめて予約投稿",
                                         command=self._post_run_batch, style="Accent.TButton")
        self.post_batch_btn.pack(side="left", padx=12)
        # 削除は右端に離して配置（🚀の隣だと誤クリックが怖い）。ごみ箱移動＝復元可能
        ttk.Button(lb, text="🗑 チェックした動画を削除",
                   command=self._post_delete_checked).pack(side="right")
        _hint(lb, "予約時刻は「設定」の時刻スロットに沿って1本ずつ自動で割り振ります。").pack(side="left")
        p.after(400, self._post_refresh_list)

        pf = ttk.Frame(p); pf.pack(fill="x", **pad)
        ttk.Label(pf, text="動画フォルダ").pack(side="left")
        self.post_folder_var = tk.StringVar(value="")
        ttk.Entry(pf, textvariable=self.post_folder_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(pf, text="🕒 最新の動画", command=self._post_latest).pack(side="left")
        ttk.Button(pf, text="選ぶ", command=self._post_pick).pack(side="left", padx=4)
        ttk.Button(pf, text="読み込む", command=self._post_load).pack(side="left")

        cf = ttk.Frame(p); cf.pack(fill="x", **pad)
        ttk.Label(cf, text="投稿先チャンネル").pack(side="left")
        self.post_channel_var = tk.StringVar(
            value=self.cfg.get("youtube_channel_url_global")
            or self.cfg.get("youtube_channel_url", ""))
        ttk.Entry(cf, textvariable=self.post_channel_var).pack(side="left", fill="x", expand=True, padx=6)
        _hint(cf, "URLでOK（例 youtube.com/channel/UC...）。ログイン中Chromeがこのチャンネルにアクセスできること").pack(side="left")

        body = ttk.Frame(p); body.pack(fill="both", expand=True, **pad)
        # プレビュー枠を先にpack（packは先勝ち＝幅が足りない時に後からpackした左側が
        # 縮み、右のプレビューが消えない。2026-08-05配布先報告）
        right = ttk.LabelFrame(body, text="プレビュー"); right.pack(side="right", fill="y", padx=(8, 0))
        left = ttk.LabelFrame(body, text="投稿する内容（確認・編集できます）"); left.pack(side="left", fill="both", expand=True)
        g = ttk.Frame(left); g.pack(fill="both", expand=True, padx=10, pady=8)
        ttk.Label(g, text="タイトル").grid(row=0, column=0, sticky="e", padx=(0, 6))
        self.post_title_var = tk.StringVar()
        ttk.Entry(g, textvariable=self.post_title_var).grid(row=0, column=1, columnspan=2, sticky="ew", pady=2)
        self.post_title_cands: list[str] = []
        self.post_cand_var = tk.StringVar(value="他の候補▼")
        cand_cb = ttk.Combobox(g, textvariable=self.post_cand_var, values=[], width=14,
                               state="readonly")
        cand_cb.grid(row=0, column=3, sticky="e", padx=(6, 0))
        cand_cb.bind("<<ComboboxSelected>>",
                     lambda e: self.post_title_var.set(
                         self.post_cand_var.get().split("｜", 1)[-1]))
        self.post_cand_combo = cand_cb
        ttk.Label(g, text="説明欄").grid(row=1, column=0, sticky="ne", padx=(0, 6))
        self.post_desc_text = _mk_text(g, height=9, size=11)
        self.post_desc_text.grid(row=1, column=1, columnspan=3, sticky="ew", pady=4)
        ttk.Label(g, text="タグ").grid(row=2, column=0, sticky="e", padx=(0, 6))
        self.post_tags_var = tk.StringVar()
        ttk.Entry(g, textvariable=self.post_tags_var).grid(row=2, column=1, columnspan=3, sticky="ew", pady=2)
        _hint(g, "カンマ「,」区切りで。例：車, サーキット, 解説", row=3, column=1, columnspan=3, sticky="w")
        ttk.Label(g, text="公開する日時").grid(row=4, column=0, sticky="e", padx=(0, 6))
        self.post_when_var = tk.StringVar()
        ttk.Entry(g, textvariable=self.post_when_var, width=20).grid(row=4, column=1, sticky="w", pady=2)
        _hint(g, "例 2026-07-05 09:00（日本時間）", row=4, column=2, columnspan=2, sticky="w")
        ttk.Label(g, text="公開のしかた").grid(row=5, column=0, sticky="e", padx=(0, 6))
        self.post_privacy_var = tk.StringVar(value=pdisp(PRIVACY_PAIRS, self.cfg.get("youtube_privacy", "private")))
        ttk.Combobox(g, textvariable=self.post_privacy_var, values=[d for _, d in PRIVACY_PAIRS],
                     width=18, state="readonly").grid(row=5, column=1, sticky="w")
        ttk.Label(g, text="投稿方法").grid(row=6, column=0, sticky="e", padx=(0, 6))
        self.post_engine_var = tk.StringVar(value=pdisp(ENG_LABELS["upload"], "studio_web"))
        ttk.Combobox(g, textvariable=self.post_engine_var, values=[d for _, d in ENG_LABELS["upload"]],
                     width=34, state="readonly").grid(row=6, column=1, columnspan=2, sticky="w")
        self.post_cat_var = tk.StringVar(value=str(self.cfg.get("youtube_category_id", "22")))
        self.post_kids_var = tk.BooleanVar(value=self.cfg.get("youtube_made_for_kids", False))
        ttk.Checkbutton(g, text="子ども向けの動画", variable=self.post_kids_var).grid(row=7, column=1, sticky="w")
        # 予約の確定まで全自動（2026-08-06方針転換）。OFF=従来の「最後は画面で押す」
        self.post_auto_var = tk.BooleanVar(value=bool(self.cfg.get("studio_auto_publish", True)))
        ttk.Checkbutton(g, text="予約の確定まで全自動（OFF=最後は画面で押す）",
                        variable=self.post_auto_var).grid(row=7, column=2, columnspan=2, sticky="w")
        self.post_dry_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(g, text="お試し（投稿せず内容だけ確認）", variable=self.post_dry_var).grid(
            row=8, column=1, columnspan=3, sticky="w", pady=2)
        # Studio(手動)では日時・公開設定は使われない＝「設定したのに効かない」誤解の防止
        # （2026-08-05配布先報告・不具合3-B）
        self.post_engine_note = _hint(
            g, "", row=9, column=1, columnspan=3, sticky="w", wrap=560, pady=(0, 2))

        def _upd_engine_note(*_a):
            eng = pval(ENG_LABELS["upload"], self.post_engine_var.get())
            if eng != "studio_web":
                self.post_engine_note.config(text="")
            elif self.post_auto_var.get():
                self.post_engine_note.config(text=(
                    "※Studio方式: 動画・タイトル・説明・タグ・サムネ・予約日時の入力から"
                    "『スケジュール』の確定まで自動で行います（日時は読み戻し検証に合格した時だけ"
                    "確定。失敗時は画面へ引き渡し）。"))
            else:
                self.post_engine_note.config(text=(
                    "※Studio方式（手動仕上げ）: 予約日時と公開のしかたは最後にブラウザ画面で"
                    "選びます（ツールは詳細画面の入力まで自動）。"))
        self.post_engine_var.trace_add("write", _upd_engine_note)
        self.post_auto_var.trace_add("write", _upd_engine_note)
        _upd_engine_note()
        g.columnconfigure(1, weight=1)

        self.thumb_label = ttk.Label(right, text="（サムネ未読み込み）", cursor="hand2")
        self.thumb_label.pack(padx=8, pady=8)
        self.thumb_label.bind("<Button-1>", lambda e: self._open_thumb_preview())
        _hint(right, "クリックで原寸表示", wrap=200)

        # ── サムネを作り直す（投稿前・この動画の中身に合わせて） ──
        tw = ttk.Frame(right); tw.pack(fill="x", padx=8, pady=(0, 3))
        ttk.Label(tw, text="サムネ文字").pack(side="left")
        self.post_thumbtext_var = tk.StringVar()
        self.post_thumbtext_entry = ttk.Entry(tw, textvariable=self.post_thumbtext_var, width=20)
        self.post_thumbtext_entry.pack(side="left", fill="x", expand=True, padx=(4, 0))
        # ↵＝カーソル位置に「/」を入れる（改行位置を自分で決める・2026-08-20配布先要望）
        ttk.Button(tw, text="↵", width=3, command=self._thumb_insert_break).pack(
            side="left", padx=(4, 0))
        # ✨ AIでバズる文字案を5個（台本を全文読ませて生成）
        self.thumb_suggest_btn = ttk.Button(right, text="✨ バズる文字をAIで5案（台本を全部読む）",
                                            command=self._thumb_suggest)
        self.thumb_suggest_btn.pack(fill="x", padx=8, pady=(0, 3))
        # 候補は“自分の行”でフル幅＝長いタイトルも見切れない
        self.post_thumbcopy_var = tk.StringVar(value="候補（ここから選ぶ）▼")
        self.thumb_copy_combo = ttk.Combobox(right, textvariable=self.post_thumbcopy_var,
                                             values=[], width=30, state="readonly")
        self.thumb_copy_combo.pack(fill="x", padx=8, pady=(0, 3))
        self.thumb_copy_combo.bind(
            "<<ComboboxSelected>>",
            lambda e: self.post_thumbtext_var.set(self.post_thumbcopy_var.get()))
        # 文字のフォント選択
        ff = ttk.Frame(right); ff.pack(fill="x", padx=8, pady=(0, 3))
        ttk.Label(ff, text="文字のフォント").pack(side="left")
        from izanagi.thumb_text import THUMB_FONTS
        self._thumb_fonts = THUMB_FONTS
        self.thumb_font_var = tk.StringVar(value=self._thumb_font_label())
        tfc = ttk.Combobox(ff, textvariable=self.thumb_font_var,
                           values=list(THUMB_FONTS.keys()), width=18, state="readonly")
        tfc.pack(side="left", fill="x", expand=True, padx=(4, 0))
        tfc.bind("<<ComboboxSelected>>", self._on_thumb_font)
        # 文字の色（上段/下段で別々に。2026-08-20配布先要望）
        from izanagi.thumb_text import THUMB_COLOR_PRESETS
        cf = ttk.Frame(right); cf.pack(fill="x", padx=8, pady=(0, 3))
        ttk.Label(cf, text="文字の色  上段").pack(side="left")
        self.thumb_color_top_var = tk.StringVar(value=self._thumb_color_label("thumb_color_top"))
        self.thumb_color_bottom_var = tk.StringVar(
            value=self._thumb_color_label("thumb_color_bottom"))
        for _var in (self.thumb_color_top_var, self.thumb_color_bottom_var):
            _cb = ttk.Combobox(cf, textvariable=_var, values=list(THUMB_COLOR_PRESETS.keys()),
                               width=8, state="normal")
            _cb.pack(side="left", padx=(4, 0))
            _cb.bind("<<ComboboxSelected>>", self._on_thumb_color)
            _cb.bind("<FocusOut>", self._on_thumb_color)
            _cb.bind("<Return>", self._on_thumb_color)
            if _var is self.thumb_color_top_var:
                ttk.Label(cf, text=" 下段").pack(side="left")
        _hint(right, "「/」の位置で上段/下段に分かれます（↵でカーソル位置に「/」を入れる。"
                     "例：地球を捨てる日/人類の行き先は？）。2行まで・各行16字まで。"
                     "色は一覧から選ぶか #FF3B3B のように直接入力。"
                     "✨で候補→選ぶ→🖼か✏で反映。").pack(anchor="w", padx=8)
        tb = ttk.Frame(right); tb.pack(fill="x", padx=8, pady=(2, 6))
        self.thumb_regen_btn = ttk.Button(tb, text="🖼 AIで作り直す",
                                          command=lambda: self._thumb_regen(full=True))
        self.thumb_regen_btn.pack(side="left")
        self.thumb_text_btn = ttk.Button(tb, text="✏ 文字だけ",
                                         command=lambda: self._thumb_regen(full=False))
        self.thumb_text_btn.pack(side="left", padx=6)
        self.thumb_regen_status = ttk.Label(right, text="", style="Muted.TLabel", wraplength=260)
        self.thumb_regen_status.pack(anchor="w", padx=8, pady=(0, 4))

        self.post_video_label = ttk.Label(right, text="", style="Muted.TLabel", wraplength=260)
        self.post_video_label.pack(padx=8, pady=(0, 8))
        self.post_status_label = ttk.Label(right, text="", foreground="#ffb45e",
                                           font=(FB, 11, "bold"), wraplength=260)
        self.post_status_label.pack(padx=8, pady=(0, 8))

        pb = ttk.Frame(p); pb.pack(fill="x", **pad)
        ttk.Button(pb, text="💾 内容を保存", command=self._post_save).pack(side="left")
        self.post_btn = ttk.Button(pb, text="🚀 予約投稿する", command=self._post_run, style="Accent.TButton")
        self.post_btn.pack(side="left", padx=8)
        ttk.Button(pb, text="▶ 動画を再生", command=self._post_open_video).pack(side="left")

    def _build_set(self, p):
        pad = {"padx": 16, "pady": 10}  # 枠と枠の間隔
        kf = ttk.LabelFrame(p, text="APIキー / 認証（お持ちのものだけ入れればOK）"); kf.pack(fill="x", **pad)
        kg = ttk.Frame(kf); kg.pack(fill="x", padx=10, pady=8)
        self.key_vars = {}
        # Gemini音声用キー欄は撤去（未使用・空=共通キーで動く。値と内部フォールバックは温存）
        for r, (k, lab) in enumerate([("anthropic_api_key", "Claude（Anthropic）"),
                                      ("openai_api_key", "ChatGPT（OpenAI）"),
                                      ("gemini_api_key", "Gemini（AIスタジオ・共通）")]):
            ttk.Label(kg, text=lab, width=20).grid(row=r, column=0, sticky="w")
            v = tk.StringVar(value=self.cfg.get(k, ""))
            ttk.Entry(kg, textvariable=v, show="*").grid(row=r, column=1, sticky="ew", padx=6, pady=4)
            self.key_vars[k] = v
        # YouTube 認証
        ttk.Label(kg, text="YouTube 認証(client_secret)", width=30).grid(row=3, column=0, sticky="w")
        self.cs_var = tk.StringVar(value=self.cfg.get("youtube_client_secret", ""))
        ttk.Entry(kg, textvariable=self.cs_var).grid(row=3, column=1, sticky="ew", padx=6)
        ttk.Button(kg, text="参照", command=lambda: self._pick_file(self.cs_var)).grid(row=3, column=2)
        # Fish Audio（高品質・従量課金）音声キー＋音質グレード
        ttk.Label(kg, text="Fish Audio 音声キー", width=20).grid(row=4, column=0, sticky="w")
        _fv = tk.StringVar(value=self.cfg.get("fish_api_key", ""))
        ttk.Entry(kg, textvariable=_fv, show="*").grid(row=4, column=1, sticky="ew", padx=6, pady=4)
        self.key_vars["fish_api_key"] = _fv
        from izanagi.providers.tts_fish import FISH_MODELS
        self.fish_model_var = tk.StringVar(value=self.cfg.get("fish_model", "s2.1-pro-free"))
        ttk.Combobox(kg, textvariable=self.fish_model_var, values=FISH_MODELS, width=14,
                     state="readonly").grid(row=4, column=2, padx=(4, 0))
        # Google Cloud音声の認証JSON欄はGUIから撤去（音声はGemini一本化）。値は温存
        self.gtts_var = tk.StringVar(value=self.cfg.get("google_tts_credentials_json", ""))
        self.tok_var = tk.StringVar(value=self.cfg.get("youtube_token", ""))  # 保持（画面には出さない）
        dict_btns = ttk.Frame(kg)
        dict_btns.grid(row=5, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Button(dict_btns, text="📖 読み方辞書を編集（読み間違いを直す）",
                   command=self._open_reading_fixes).pack(side="left")
        ttk.Button(dict_btns, text="📚 登録済み一覧",
                   command=self._open_dict_catalog).pack(side="left", padx=(6, 0))
        # 次の行へ（辞書ボタン行 row=5 と列が重なっていた・2026-08-26の全数走査で検出）
        _hint(kg, "「語=よみ」で1行ずつ（例: 残クレ=ざんくれ）。全動画のTTSに即反映。"
                  "動画ごとの人名・難読語は台本から自動抽出されます。"
                  "誤読が気になる場合は②音声をGemini（文脈理解型）にすると発生自体が減ります。",
              row=6, column=0, columnspan=3, sticky="w", pady=(2, 0))
        kg.columnconfigure(1, weight=1)

        _hint(kf, "声の選択は『作成』タブの「声」で行います（②音声のエンジンに合わせて自動で切り替わり、▶で試聴できます）。").pack(
            anchor="w", padx=12, pady=(0, 8))

        bf = ttk.LabelFrame(p, text="無料AI・投稿(手動)で使うブラウザ"); bf.pack(fill="x", **pad)
        bg = ttk.Frame(bf); bg.pack(fill="x", padx=10, pady=8)
        _hint(bg, "「無料（要ログイン）」や「手動」を使う時に、専用ブラウザで各サイトにログインしておきます。",
              row=0, column=0, columnspan=3, sticky="w")
        ttk.Button(bg, text="🌐 ログイン用ブラウザを開く", command=self._launch_login_chrome).grid(
            row=1, column=0, sticky="w", pady=6)
        self._BROWSER_PAIRS = [("", "Chrome（自動検出）"), ("edge", "Microsoft Edge"),
                               ("brave", "Brave"), ("custom", "その他（参照で選ぶ）")]
        raw = self.cfg.get("browser_exe", "")
        key = raw if raw in ("", "edge", "brave") else "custom"
        self._browser_custom = raw if key == "custom" else ""
        brow = ttk.Frame(bg); brow.grid(row=1, column=1, columnspan=2, sticky="w", padx=(12, 0))
        ttk.Label(brow, text="使うブラウザ").pack(side="left", padx=(0, 6))
        self.browser_var = tk.StringVar(value=pdisp(self._BROWSER_PAIRS, key))
        ttk.Combobox(brow, textvariable=self.browser_var, state="readonly", width=20,
                     values=[d for _, d in self._BROWSER_PAIRS]).pack(side="left")
        ttk.Button(brow, text="参照", command=self._pick_browser).pack(side="left", padx=6)
        self.browser_path_lab = ttk.Label(brow, text=self._browser_custom, style="Muted.TLabel")
        self.browser_path_lab.pack(side="left", padx=4)
        _hint(bg, "Chrome / Edge / Brave などChromium系のみ。切り替えるとプロファイルが別になるため、"
                  "各サイト(claude.ai/ChatGPT/Gemini/YouTube)への再ログインが必要です。",
              row=2, column=0, columnspan=3, sticky="w")
        self.import_btn = ttk.Button(bg, text="🔁 普段のChromeからログインを取り込む",
                                     command=self._import_chrome_logins)
        self.import_btn.grid(row=3, column=0, sticky="w", pady=(6, 0))
        src_now = self.cfg.get("browser_profile_source", "")
        self.import_src_lab = ttk.Label(
            bg, text=(f"取込中: {src_now}" if src_now else "（まだ取り込んでいません）"),
            foreground=GOLD, font=(FB, 11, "bold"))
        self.import_src_lab.grid(row=3, column=1, sticky="w", padx=(12, 0))
        _hint(bg, "どのプロファイル（Googleアカウント）で動かすかはここで選べます。別のアカウントに"
                  "切り替えたい時は、Chromeを全部閉じて取り込み直すだけ（普段のChromeは変更されません）。",
              row=3, column=2, sticky="w", padx=(12, 0))
        ttk.Button(bg, text="🖥 デスクトップにショートカット作成",
                   command=self._make_browser_shortcut).grid(row=4, column=0, sticky="w", pady=(6, 0))
        _hint(bg, "「IZANAMI用ブラウザ」を普段使いにする用。ここから開いたブラウザで普段の作業をしていれば、"
                  "ツールはいつでも“今開いているブラウザ”に接続して動きます（再ログイン不要）。",
              row=4, column=1, columnspan=2, sticky="w", padx=(12, 0))
        self.profile_var = tk.StringVar(value=self.cfg.get("chrome_profile", ""))
        self.port_var = tk.StringVar(value=str(self.cfg.get("cdp_port", 9222)))

        of = ttk.LabelFrame(p, text="保存先"); of.pack(fill="x", **pad)
        og = ttk.Frame(of); og.pack(fill="x", padx=10, pady=8)
        ttk.Label(og, text="保存先フォルダ", width=20).grid(row=0, column=0, sticky="w")
        ttk.Entry(og, textvariable=self.out_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(og, text="参照", command=self._choose_out).grid(row=0, column=2)
        og.columnconfigure(1, weight=1)

        xf = ttk.LabelFrame(p, text="設定ファイル（バックアップ／別PCへ引っ越し／人に渡す）")
        xf.pack(fill="x", **pad)
        xg = ttk.Frame(xf); xg.pack(fill="x", padx=10, pady=8)
        ttk.Button(xg, text="💾 いまの設定をファイルへ保存",
                   command=self._export_settings).pack(side="left")
        ttk.Button(xg, text="📂 設定ファイルを読み込む",
                   command=self._import_settings).pack(side="left", padx=8)
        _hint(xf, "作成タブのお題・エンジン・チャンネルの世界観・テロップ・BGM・冒頭終了など、"
                  "画面の設定をまるごと1つのファイル(JSON)に保存/復元できます。"
                  "保存時に「人に渡す用」を選ぶとAPIキーやこのPC固有のパスは入りません。"
                  "読み込み時は今の設定の控えを自動で取ってから反映（再起動）します。").pack(
            anchor="w", padx=12, pady=(0, 8))

        af = ttk.LabelFrame(p, text="こまかい設定（ふつうは触らなくてOK）"); af.pack(fill="x", **pad)
        ag = ttk.Frame(af); ag.pack(fill="x", padx=10, pady=8)
        ttk.Label(ag, text="字幕の合わせ方").grid(row=0, column=0, sticky="e")
        self.align_var = tk.StringVar(value=pdisp(ALIGN_PAIRS, self.cfg.get("align_method", "segments")))
        ttk.Combobox(ag, textvariable=self.align_var, values=[d for _, d in ALIGN_PAIRS], width=20,
                     state="readonly").grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(ag, text="予約の時刻（カンマ区切り）").grid(row=1, column=0, sticky="e", pady=(6, 0))
        self.slots_var = tk.StringVar(value=",".join(self.cfg.get("default_schedule_slots", ["09:00"])))
        ttk.Entry(ag, textvariable=self.slots_var, width=28).grid(row=1, column=1, sticky="w", padx=6, pady=(6, 0))
        ttk.Label(ag, text="何日後に公開").grid(row=2, column=0, sticky="e", pady=(6, 0))
        self.offset_var = tk.StringVar(value=str(self.cfg.get("default_publish_offset_days", 1)))
        ttk.Spinbox(ag, textvariable=self.offset_var, from_=0, to=30, width=5).grid(row=2, column=1, sticky="w", padx=6, pady=(6, 0))
        self.wmodel_var = tk.StringVar(value=self.cfg.get("whisper_model", "medium"))  # 保持
        self.debug_var = tk.BooleanVar(value=self.cfg.get("debug_dump", True))
        ttk.Checkbutton(ag, text="うまくいかない時の記録を残す(_debug)", variable=self.debug_var).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

    # ---------- 補助操作 ----------
    def _export_settings(self):
        """画面の設定をまるごと1つのJSONへ保存（バックアップ/引っ越し/人に渡す用）。"""
        import json as _json
        try:
            cfg = self._gather_cfg()      # 作成タブのお題・エンジン等、いまの画面の状態
        except Exception:
            cfg = dict(self.cfg)
        ans = messagebox.askyesnocancel(
            "設定をファイルへ保存",
            "APIキーも一緒に保存しますか？\n\n"
            "［はい］　＝ 自分のバックアップ・別PCへの引っ越し用\n"
            "　　　　　　（APIキー・このPCのパスも入ります）\n"
            "［いいえ］＝ 人に渡す用\n"
            "　　　　　　（キーとこのPC固有のパスは入れません）\n"
            "［キャンセル］＝ やめる")
        if ans is None:
            return
        stamp = datetime.datetime.now().strftime("%Y%m%d")
        name = f"IZANAMI設定_{stamp}" + ("" if ans else "_渡す用") + ".json"
        f = filedialog.asksaveasfilename(
            title="設定ファイルの保存先", defaultextension=".json", initialfile=name,
            filetypes=[("IZANAMI設定 (JSON)", "*.json")])
        if not f:
            return
        try:
            data = config.export_settings(cfg, include_secrets=bool(ans))
            Path(f).write_text(_json.dumps(data, ensure_ascii=False, indent=2),
                               encoding="utf-8")
        except Exception as e:
            messagebox.showerror("IZANAMI", f"保存できませんでした:\n{e}")
            return
        self._log("設定をファイルへ保存しました: " + f
                  + ("" if ans else "（APIキー・PC固有パスは含めていません）"))
        messagebox.showinfo("IZANAMI", "設定を保存しました。\n\n" + f
                            + ("" if ans else "\n\n※APIキーは入っていないので"
                                              "そのまま人に渡せます。"))

    def _import_settings(self):
        """設定ファイルを読み込んで反映する（今の設定は控えを取ってから置き換え）。"""
        import json as _json
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("IZANAMI", "いま作成中です。終わってから読み込んでください。")
            return
        # self.worker に載らない背景処理も塞ぐ（実行中に再起動すると、
        # ブラウザプロファイルの半端コピーやBGMの書きかけが残る。さらに完了通知が
        # モーダル中に届くと _poll_log の保存が読み込んだ設定を旧設定で上書きする）
        busy = []
        if getattr(self, "_bgm_joining", False):
            busy.append("BGMの連結")
        if getattr(self, "_suno_running", False):
            busy.append("BGMの生成")
        if getattr(self, "_thumb_busy", False):
            busy.append("サムネの作り直し")
        try:
            if str(self.import_btn["state"]) == "disabled":
                busy.append("Chromeログインの取り込み")
        except Exception:
            pass
        if busy:
            messagebox.showinfo("IZANAMI", "いま「" + "・".join(busy) + "」の処理中です。\n"
                                           "終わってから読み込んでください。")
            return
        f = filedialog.askopenfilename(
            title="読み込む設定ファイル",
            filetypes=[("IZANAMI設定 (JSON)", "*.json"), ("すべて", "*.*")])
        if not f:
            return
        try:
            raw = Path(f).read_bytes()
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                # メモ帳等でANSI(cp932)保存し直されたファイルも読む
                text = raw.decode("cp932")
            data = _json.loads(text)
        except UnicodeDecodeError:
            messagebox.showerror("IZANAMI", "読み込めませんでした。\n"
                                            "文字コードが対応外です（UTF-8で保存し直してください）。")
            return
        except Exception as e:
            messagebox.showerror("IZANAMI", f"読み込めませんでした（JSONとして壊れています）:\n{e}")
            return
        if not config.looks_like_settings(data):
            messagebox.showwarning(
                "IZANAMI", "IZANAMIの設定ファイルではないようです。\n"
                           "（設定の項目が見つかりません。読み方辞書などの別のJSONを"
                           "選んでいませんか？）")
            return
        if not messagebox.askyesno(
                "設定ファイルを読み込む",
                "今の設定はこのファイルの内容に置き換わります。\n\n"
                "・今の設定は backups フォルダに控えを残します\n"
                "・APIキーや保存先は、ファイル側が空なら今のものを残します\n"
                "・再起動後にログイン画面が出た場合は、いつものID/パスワードで\n"
                "　そのままログインしてください\n\n"
                "読み込んで、反映のため再起動しますか？"):
            return
        try:
            cur = self._gather_cfg()
        except Exception:
            cur = dict(self.cfg)
        config.save_settings(cur)                        # 画面の最新状態を確定してから
        bak = config.backup_settings(label="読込前")      # 控えを取る
        if not bak:
            # 確認ダイアログで「控えを残します」と約束している＝作れなかったのに
            # 無言で置き換えるのは約束違反（戻せない）。続けるかを本人に聞く
            if not messagebox.askyesno(
                    "IZANAMI",
                    "今の設定の控え（バックアップ）が作れませんでした。\n"
                    "読み込みを続けると、今の設定に戻せなくなる可能性があります。\n\n"
                    "それでも続けますか？"):
                return
        merged = config.merge_imported(data, cur)
        # 「渡す用」ファイルは配布者のクローン声ID(fish_voice)を意図的に空にして書き出す。
        # Fish選択のまま受け取った人がキーだけ入れて量産すると1行ごとに声が変わる
        # （2026-08-17配布先報告）→ 読み込み時に一度だけ注意する
        if (merged.get("tts_engine") == "fish"
                and not (merged.get("fish_voice") or "").strip()):
            messagebox.showwarning(
                "IZANAMI",
                "この設定ファイルの音声エンジンはFishですが、声ID(reference_id)は\n"
                "含まれていません（配布者のクローン声は渡らない仕様です）。\n\n"
                "Fishを使う場合は、再起動後に作成タブの声欄「＋声を登録」で\n"
                "ご自身の声IDを登録してください（未登録のままだと開始できません）。")
        # ここから先は一切の追加保存を許さない（背景処理の完了通知が _poll_log 経由で
        # 旧設定を保存し、読み込んだばかりの設定を上書きするのを防ぐ）。
        # 完了ダイアログも出さない＝モーダル中の150msポーリングが競合の窓になる
        self._importing = True
        config.save_settings(merged)
        self._log("設定ファイルを読み込みました: " + f
                  + (f"（控え: {bak}）" if bak else "") + " → 再起動します。")
        self._restart()

    def _restart(self):
        """新しいIZANAMIを起動してから自分を閉じる（設定読込の反映用）。

        ※root.destroy()を直接呼ぶ＝_on_close（×閉じ時の上書き保存）を通らない。
        　通ってしまうと、いま画面に残っている古い設定で settings.json を
        　書き戻し、読み込んだばかりの設定が消える。"""
        try:
            import subprocess
            subprocess.Popen([sys.executable, str(ROOT / "app.py")], cwd=str(ROOT),
                             close_fds=True)
        except Exception:
            messagebox.showwarning("IZANAMI", "自動で再起動できませんでした。\n"
                                              "お手数ですが、手動で起動し直してください。")
        self.root.destroy()

    def _choose_out(self):
        d = filedialog.askdirectory(initialdir=self.out_var.get() or str(Path.home()))
        if d:
            self.out_var.set(d)

    def _pick_file(self, var):
        f = filedialog.askopenfilename(filetypes=[("JSON", "*.json"), ("すべて", "*.*")])
        if f:
            var.set(f)

    def _open_out(self):
        p = self.out_var.get()
        if p and Path(p).exists():
            os.startfile(p)  # noqa
        else:
            messagebox.showinfo("IZANAMI", "保存先フォルダがまだありません。")

    def _bgm_add(self):
        fs = filedialog.askopenfilenames(
            title="BGMを追加（複数選ぶと『つなげて1本』にできます）",
            filetypes=[("音声", "*.mp3 *.wav *.m4a *.aac *.flac"), ("すべて", "*.*")])
        fs = [f for f in fs if f]
        if not fs:
            return
        # 複数選んだら「選んだ順に1本へつなげる」か「別々に追加」かを聞く
        if len(fs) >= 2:
            join = messagebox.askyesno(
                "IZANAMI",
                f"{len(fs)}曲を選びました。\n\n"
                "「はい」＝選んだ順につなげて“1本のBGM”にして追加\n"
                "「いいえ」＝それぞれ別々に追加")
            if join:
                self._bgm_join_files(list(fs))
                return
        for f in fs:
            item = {"path": f, "title": Path(f).stem, "mood": "", "gain_db": -6}
            self.bgm_lib.append(item)
            self.bgm_list.insert("end", _bgm_row_text(item))

    def _bgm_join_files(self, paths, default_name="つなぎBGM"):
        """与えられた音声ファイル群を（選んだ順に）1本へ連結してライブラリへ追加。"""
        if getattr(self, "_bgm_joining", False):
            return
        paths = [p for p in paths if p and Path(p).exists()]
        if len(paths) < 2:
            messagebox.showinfo("IZANAMI", "つなげるには2曲以上必要です。")
            return
        from tkinter import simpledialog
        name = simpledialog.askstring(
            "IZANAMI", f"{len(paths)}曲をつなげます。新しいBGMの名前:",
            initialvalue=default_name, parent=self.root)
        if not name:
            return
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        out = ROOT / "bgm" / f"{util.safe_name(name)}_{stamp}.m4a"
        self._bgm_joining = True
        try:
            self.bgm_join_btn.config(state="disabled", text="🔗 連結中…")
        except Exception:
            pass
        self._log(f"🔗 BGMを連結します（{len(paths)}曲・選んだ順）: "
                  + " → ".join(Path(p).stem for p in paths))

        def _worker():
            try:
                from izanagi import music
                p = music.join_bgm(paths, str(out), log=self._log)
                self.log_queue.put(f"__BGM_ADD__\t{p}")
                self._log(f"✅ つなぎBGMができました: {Path(p).name}（ライブラリに追加済み）")
            except Exception as e:
                self._log(f"⚠ BGMの連結に失敗: {e}")
            finally:
                self.log_queue.put("__BGMJOIN_DONE__")

        threading.Thread(target=_worker, daemon=True).start()

    def _reload_bgm_lib(self):
        """パイプライン（自動BGM）が settings に登録した曲をGUI側リストへ取り込む。"""
        try:
            saved = config.load_settings().get("bgm_library", []) or []
        except Exception:
            return
        known = {e.get("path") for e in self.bgm_lib}
        for e in saved:
            if e.get("path") not in known:
                self.bgm_lib.append(e)
                self.bgm_list.insert("end", _bgm_row_text(e))
        self.cfg["bgm_library"] = self.bgm_lib

    def _bgm_del(self):
        for i in reversed(list(self.bgm_list.curselection())):
            self.bgm_list.delete(i); del self.bgm_lib[i]
        # 消した曲が「選んだ曲」の控えに残ると、選んだ曲モードが恒久的にBGM無しになる（敵対検証2026-08-25）
        if getattr(self, "_bgm_sel_path", "") and                 self._bgm_sel_path not in {e.get("path") for e in self.bgm_lib}:
            self._bgm_sel_path = ""
            try:
                self.bgm_sel_lab.config(text="（選んだ曲は削除されました。一覧から選び直してください）")
            except Exception:
                pass

    def _bgm_edit(self):
        """選択中（またはダブルクリックした）曲の 名前/ジャンル/対応チャンネル を編集する。"""
        sel = self.bgm_list.curselection()
        if not sel:
            messagebox.showinfo("IZANAMI", "設定したい曲を一覧で選んでください。")
            return
        idx = sel[0]
        if idx >= len(self.bgm_lib):
            return
        item = self.bgm_lib[idx]
        chans = self.cfg.get("channels", []) or []

        dlg = tk.Toplevel(self.root)
        dlg.title("BGMの設定")
        dlg.configure(bg=INK2)
        dlg.transient(self.root)
        dlg.resizable(False, False)
        frm = ttk.Frame(dlg); frm.pack(fill="both", expand=True, padx=16, pady=12)

        ttk.Label(frm, text="曲名").grid(row=0, column=0, sticky="e", padx=(0, 8), pady=5)
        name_var = tk.StringVar(value=item.get("title", ""))
        ttk.Entry(frm, textvariable=name_var, width=34).grid(row=0, column=1, sticky="ew", pady=5)

        ttk.Label(frm, text="ジャンル").grid(row=1, column=0, sticky="e", padx=(0, 8), pady=5)
        genres = sorted({(e.get("mood") or "").strip() for e in self.bgm_lib if e.get("mood")}
                        | {"スカッと系", "ほのぼの", "緊迫サスペンス", "コミカル", "感動", "汎用"})
        genre_var = tk.StringVar(value=item.get("mood", ""))
        ttk.Combobox(frm, textvariable=genre_var, values=genres, width=32).grid(
            row=1, column=1, sticky="ew", pady=5)
        _hint(frm, "自動選曲（動画に合わせて）で、台本のジャンルと一致する曲が優先されます。",
              row=2, column=1, sticky="w")

        ttk.Label(frm, text="使うチャンネル").grid(row=3, column=0, sticky="ne", padx=(0, 8), pady=5)
        cf = ttk.Frame(frm); cf.grid(row=3, column=1, sticky="w", pady=5)
        cur = set(int(i) for i in (item.get("channels") or []))
        all_var = tk.BooleanVar(value=not cur)  # 未設定＝全チャンネル
        ch_vars = []

        def _toggle_all():
            for v, cb in ch_vars:
                cb.config(state="disabled" if all_var.get() else "normal")
        ttk.Checkbutton(cf, text="すべてのチャンネルで使う", variable=all_var,
                        command=_toggle_all).grid(row=0, column=0, columnspan=2, sticky="w")
        for i in range(len(chans)):
            v = tk.BooleanVar(value=(i in cur))
            cb = ttk.Checkbutton(cf, text=f"{i + 1}. {chans[i].get('name') or f'チャンネル{i+1}'}",
                                 variable=v)
            cb.grid(row=1 + i // 2, column=i % 2, sticky="w", padx=(0, 16))
            ch_vars.append((v, cb))
        _toggle_all()

        ttk.Label(frm, text="音量(dB)").grid(row=4 + len(chans) // 2, column=0, sticky="e",
                                            padx=(0, 8), pady=5)
        gain_var = tk.StringVar(value=str(item.get("gain_db", -6)))
        ttk.Spinbox(frm, textvariable=gain_var, from_=-30, to=6, width=6).grid(
            row=4 + len(chans) // 2, column=1, sticky="w", pady=5)

        def _save():
            item["title"] = name_var.get().strip() or item.get("title", "")
            item["mood"] = genre_var.get().strip()
            item["channels"] = [] if all_var.get() else [i for i, (v, _) in enumerate(ch_vars) if v.get()]
            try:
                item["gain_db"] = int(float(gain_var.get()))
            except ValueError:
                pass
            self.bgm_list.delete(idx)
            self.bgm_list.insert(idx, _bgm_row_text(item))
            self.bgm_list.selection_set(idx)
            self.cfg["bgm_library"] = self.bgm_lib
            try:
                config.save_settings(self.cfg)
            except Exception:
                pass
            self._log(f"🎵 BGM設定を保存: {item.get('title')}"
                      f"（ジャンル:{item.get('mood') or '未設定'}／"
                      f"{'全ch' if not item['channels'] else 'ch '+','.join(str(i+1) for i in item['channels'])}）")
            dlg.destroy()

        btn = ttk.Frame(frm); btn.grid(row=6 + len(chans) // 2, column=0, columnspan=2,
                                       sticky="e", pady=(10, 0))
        ttk.Button(btn, text="キャンセル", command=dlg.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(btn, text="💾 保存", command=_save, style="Accent.TButton").pack(side="right")
        frm.columnconfigure(1, weight=1)
        dlg.update_idletasks()
        dlg.grab_set()

    def _bgm_join(self):
        """🔗 一覧で選択済みの複数BGMを（上から順に）1本につなげてライブラリへ追加。"""
        sel = sorted(self.bgm_list.curselection())
        if len(sel) < 2:
            messagebox.showinfo("IZANAMI", "つなげたい曲を Ctrl（または Shift）を押しながら\n"
                                           "2曲以上選んでから「🔗」を押してください。\n"
                                           "（新しく取り込む曲は『＋追加』で複数選ぶ時にもつなげられます）")
            return
        paths = [self.bgm_lib[i].get("path", "") for i in sel if i < len(self.bgm_lib)]
        missing = [Path(p).name for p in paths if not Path(p).exists()]
        if missing:
            messagebox.showwarning("IZANAMI", "見つからない曲があります:\n" + "\n".join(missing[:5]))
            return
        self._bgm_join_files(paths)

    # ---------- Suno BGM作成 ----------
    def _suno_run(self):
        if getattr(self, "_suno_running", False):
            return
        prompt = self.suno_prompt_var.get().strip()
        if not prompt:
            messagebox.showwarning("IZANAMI", "曲の指示を入力してください（例：明るいポップなBGM）。")
            return
        engine = pval(self._SUNO_ENGINES, self.suno_engine_var.get())
        cfg = self._gather_cfg()
        cfg["_suno_prompt"] = prompt
        cfg["suno_instrumental"] = self.suno_inst_var.get()
        self.cfg["suno_instrumental"] = cfg["suno_instrumental"]
        if engine == "suno_api" and not cfg.get("suno_api_key"):
            messagebox.showwarning("IZANAMI", "Suno APIキーが未設定です（設定・キー タブ）。"
                                              "Web版（Sunoにログイン）なら無料枠で作れます。")
            return
        self._suno_running = True
        self.suno_btn.config(state="disabled", text="…作成中")

        genre = self.suno_genre_var.get().strip()

        def _worker():
            try:
                from izanagi import music
                if engine == "suno_api":
                    paths = music.generate_suno_api(prompt, cfg, log=self._log)
                else:
                    paths = music.generate_suno_web(cfg, log=self._log)
                for p in paths:
                    self.log_queue.put(f"__BGM_ADD__{genre}\t{p}")
                self._log(f"🎵 BGMを{len(paths)}曲作成しました（BGMライブラリに追加済み）。")
            except Exception as e:
                self._log(f"🎵 BGM作成エラー: {e}")
            finally:
                self._suno_running = False
                self.log_queue.put("__SUNO_DONE__")

        threading.Thread(target=_worker, daemon=True).start()

    def _browser_exe_value(self) -> str:
        """設定タブの「使うブラウザ」選択 → browser_exe 値（''/edge/brave/カスタムパス）。"""
        key = pval(self._BROWSER_PAIRS, self.browser_var.get())
        return self._browser_custom if key == "custom" else key

    def _pick_browser(self):
        f = filedialog.askopenfilename(title="ブラウザの実行ファイル(.exe)を選ぶ（Chromium系のみ）",
                                       filetypes=[("実行ファイル", "*.exe")])
        if f:
            self._browser_custom = f
            self.browser_var.set(pdisp(self._BROWSER_PAIRS, "custom"))
            self.browser_path_lab.config(text=f)

    def _launch_login_chrome(self):
        try:
            from izanagi import browser_ai
            ok = browser_ai.launch_login_chrome(self.profile_var.get().strip(),
                                                int(self.port_var.get() or 9222), log=self._log,
                                                browser_exe=self._browser_exe_value())
            self._log("ログイン用ブラウザを開きました。各サイトにログインしてください。" if ok else "ブラウザ起動に失敗。")
        except Exception as e:
            self._log(f"ブラウザ起動エラー: {e}")

    def _make_browser_shortcut(self):
        """デスクトップに「IZANAMI用ブラウザ」ショートカットを作る（普段使い用）。"""
        from izanagi import browser_ai
        exe, kind = browser_ai.resolve_browser(self._browser_exe_value())
        if not exe:
            messagebox.showwarning("IZANAMI", "ブラウザの実行ファイルが見つかりません。")
            return
        prof = browser_ai._profile_for(
            self.profile_var.get().strip() or config.DEFAULTS["chrome_profile"], kind)
        try:
            port = int(self.port_var.get() or 9222)
        except ValueError:
            port = 9222
        ps = (
            "$ws = New-Object -ComObject WScript.Shell\n"
            "$p = Join-Path $ws.SpecialFolders('Desktop') 'IZANAMI用ブラウザ.lnk'\n"
            "$l = $ws.CreateShortcut($p)\n"
            f"$l.TargetPath = '{exe}'\n"
            f"$l.Arguments = '--remote-debugging-port={port} --user-data-dir=\"{prof}\" "
            "--profile-directory=Default'\n"
            f"$l.IconLocation = '{exe},0'\n"
            "$l.Save()\nWrite-Output $p\n")
        try:
            import subprocess as sp
            import tempfile
            f = Path(tempfile.gettempdir()) / "izanagi_mklnk.ps1"
            f.write_text(ps, encoding="utf-8-sig")
            pr = sp.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                         "-File", str(f)], capture_output=True, text=True, timeout=30)
            out = (pr.stdout or "").strip()
            if pr.returncode == 0 and out:
                self._log(f"デスクトップにショートカットを作成しました: {out}")
                self._log("→ 普段のブラウジングをこの「IZANAMI用ブラウザ」で行えば、"
                          "ツールは常に“今開いているブラウザ”で稼働します。")
            else:
                self._log("ショートカット作成に失敗: " + (pr.stderr or "")[:200])
        except Exception as e:
            self._log(f"ショートカット作成エラー: {e}")

    _READ_USER_FILE = "data/reading_fixes_user.txt"

    def _read_user_dict(self) -> list[tuple[str, str]]:
        f = ROOT / self._READ_USER_FILE
        try:
            # メモ帳のANSI(cp932)保存でも読めるようにする。ここで読めないと
            # 一覧が空になり、そのまま保存すると登録済みの語が消えてしまう
            from izanagi.providers.base import parse_user_dict, read_user_dict_text
            return list(parse_user_dict(read_user_dict_text(f)).items())
        except Exception:
            return []

    def _write_user_dict(self, rows) -> None:
        f = ROOT / self._READ_USER_FILE
        f.parent.mkdir(parents=True, exist_ok=True)
        body = ["# 読み方辞書（自分用）: 読み間違える語を「語=よみ」で1行ずつ",
                "# ※よみはひらがな。ここの語が最優先で使われます（同梱辞書より強い）", ""]
        body += [f"{k}={v}" for k, v in rows]
        f.write_text("\n".join(body) + "\n", encoding="utf-8")

    # ── 画替わりの速さ（画像枚数の目安） ──────────────────────────────
    _PACE_SEC = {"ゆっくり（30秒に1回）": 30, "標準（20秒に1回）": 20,
                 "テンポ良く（15秒に1回）": 15, "かなり速い（10秒に1回）": 10}

    def _pace_numbers(self):
        """(動画の秒数, 画像枚数, 動画本数) を安全に取り出す。"""
        def _i(var, d):
            try:
                return max(0, int(str(var.get()).strip()))
            except Exception:
                return d
        n_clip = _i(self.nclip_var, 0)
        # ④動画が「使わない」ならクリップは1本も作られない＝尺を引いてはいけない
        try:
            if pval(ENG_LABELS["video"], self.engine_vars["video"].get()) in ("none", ""):
                n_clip = 0
        except Exception:
            pass
        return _i(self.len_var, 5) * 60, _i(self.nimg_var, 5), n_clip

    def _update_pace(self, *_a):
        """いまの設定だと1枚あたり何秒になるかを表示する（少なすぎに気づけるように）。"""
        if not hasattr(self, "pace_lab"):
            return
        total, n_img, n_clip = self._pace_numbers()
        rest = max(0, total - n_clip * 10)      # 動画クリップは1本10秒前後
        if n_img <= 0 or rest <= 0:
            self.pace_lab.config(text="", foreground=GOLD); return
        sec = rest / n_img
        if sec > 35:
            note, col = "（長め：画が止まって見えます）", "#f2b04e"
        elif sec < 8:
            note, col = "（かなり速い）", "#f2b04e"
        else:
            note, col = "", GOLD
        self.pace_lab.config(text=f"いまは1枚 約{sec:.0f}秒{note}", foreground=col)

    def _apply_pace(self):
        """選んだテンポに合う画像枚数を計算して入れる（作る時間の目安も出す）。"""
        total, _n_img, n_clip = self._pace_numbers()
        per = self._PACE_SEC.get(self.pace_var.get(), 20)
        rest = max(0, total - n_clip * 10)
        n = max(1, min(120, round(rest / per)))
        self.nimg_var.set(str(n))
        self._update_pace()
        self._log(f"画像を{n}枚にしました（{total // 60}分・{per}秒に1回の画替わり）。"
                  "※枚数を増やすと生成の時間とAPI料金も増えます。")

    def _open_reading_fixes(self):
        """読み方辞書エディタ（一覧・追加・編集・削除・検索）をアプリ内で開く。"""
        import re as _re
        from izanagi.providers import base as _pb

        dlg = tk.Toplevel(self.root)
        dlg.title("読み方辞書")
        dlg.geometry("760x560")
        dlg.transient(self.root)
        frm = ttk.Frame(dlg, padding=(14, 12)); frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="読み間違える語と、その正しい読み（ひらがな）を登録します。",
                  font=(FB, 11, "bold")).pack(anchor="w")
        _hint(frm, "音声だけがこの読みに変わります（テロップ・字幕は台本の文字のまま）。"
                   "「＋ 追加/更新」を押した時点で保存され、次に②音声を作る時から反映されます"
                   "（作成済みの動画は作り直しタブで「②音声」に✔。④合成だけでは音声は変わりません）。"
              ).pack(anchor="w", pady=(2, 10))

        # 一覧（自分用の辞書）
        tf = ttk.Frame(frm); tf.pack(fill="both", expand=True)
        tv = ttk.Treeview(tf, columns=("w", "r"), show="headings", height=12)
        tv.heading("w", text="語（台本での書き方）"); tv.column("w", width=280)
        tv.heading("r", text="よみ（ひらがな）"); tv.column("r", width=280)
        sb = ttk.Scrollbar(tf, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True); sb.pack(side="right", fill="y")

        rows = self._read_user_dict()
        self._dict_snapshot = list(rows)   # 外部編集の検知用

        def _refresh(sel_word=None):
            tv.delete(*tv.get_children())
            for k, v in rows:
                tv.insert("", "end", values=(k, v))
            if sel_word:
                for iid in tv.get_children():
                    if tv.item(iid, "values")[0] == sel_word:
                        tv.selection_set(iid); tv.see(iid); break

        _refresh()

        # 入力欄
        ef = ttk.Frame(frm); ef.pack(fill="x", pady=(10, 4))
        ttk.Label(ef, text="語").pack(side="left")
        w_var = tk.StringVar(); r_var = tk.StringVar()
        ttk.Entry(ef, textvariable=w_var, width=22).pack(side="left", padx=(6, 12))
        ttk.Label(ef, text="よみ").pack(side="left")
        ttk.Entry(ef, textvariable=r_var, width=22).pack(side="left", padx=(6, 12))
        msg = ttk.Label(frm, text="", foreground="#c0392b")

        def _persist(what: str):
            """一覧(rows)をその場でファイルへ保存し、TTS側のキャッシュも無効化する。"""
            try:
                self._write_user_dict(rows)
                _pb._READ_FIXES = None
                self._log(f"読み方辞書を{what}・保存しました（{len(rows)}語・次の②音声から反映）。")
            except Exception as e:
                msg.config(text=f"保存できませんでした: {e}")

        def _add():
            w, r = w_var.get().strip(), r_var.get().strip()
            if not w or not r:
                msg.config(text="語とよみの両方を入れてください。"); return
            if not _re.fullmatch(r"[ぁ-んー]+", r):
                msg.config(text="よみは「ひらがな」で入れてください（カタカナ・漢字は不可）。"); return
            if w == r:
                msg.config(text="語とよみが同じです。"); return
            for i, (k, _v) in enumerate(rows):
                if k == w:
                    rows[i] = (w, r); break
            else:
                rows.append((w, r))
            # 追加/更新した時点で即ファイルへ保存する（2026-08-20配布先報告:
            # 「登録しても次に開くと消えている」＝旧実装は💾保存ボタンを押さず
            # 「閉じる」/×で閉じると一覧の変更が捨てられていた。ステップ実行側の
            # 登録は即保存だったため挙動が食い違っていた）
            _persist("追加")
            msg.config(text="", foreground="#c0392b")
            w_var.set(""); r_var.set(""); _refresh(w)

        def _del():
            sel = tv.selection()
            if not sel:
                msg.config(text="消したい行を選んでください。"); return
            w = tv.item(sel[0], "values")[0]
            rows[:] = [(k, v) for k, v in rows if k != w]
            _persist("削除")
            _refresh()

        def _pick(_e=None):
            sel = tv.selection()
            if sel:
                w, r = tv.item(sel[0], "values")
                w_var.set(w); r_var.set(r)

        tv.bind("<<TreeviewSelect>>", _pick)
        ttk.Button(ef, text="＋ 追加/更新（すぐ保存）", command=_add).pack(side="left")
        ttk.Button(ef, text="🗑 選択を削除", command=_del).pack(side="left", padx=(8, 0))
        msg.pack(anchor="w")

        # 同梱辞書の検索（何が既に入っているか調べられる）
        sf = ttk.Frame(frm); sf.pack(fill="x", pady=(10, 2))
        ttk.Label(sf, text="同梱辞書を検索").pack(side="left")
        q_var = tk.StringVar()
        ttk.Entry(sf, textvariable=q_var, width=18).pack(side="left", padx=6)
        found = ttk.Label(sf, text="", foreground=MUTED)
        found.pack(side="left", padx=(6, 0))

        def _search():
            q = q_var.get().strip()
            if not q:
                return
            try:
                all_fix = _pb._load_read_fixes()
            except Exception:
                all_fix = {}
            hit = [f"{k}={v}" for k, v in all_fix.items() if q in k][:6]
            found.config(text="／".join(hit) if hit
                         else f"「{q}」は未登録です（上で追加できます）")

        ttk.Button(sf, text="🔍", width=3, command=_search).pack(side="left")
        ttk.Button(sf, text="📚 登録済みの辞書を一覧で見る",
                   command=self._open_dict_catalog).pack(side="left", padx=(10, 0))

        # 保存
        bf = ttk.Frame(frm); bf.pack(fill="x", pady=(12, 0))

        def _save():
            # メモ帳で直接編集された場合、ダイアログを開いた時のスナップショットで
            # 上書きすると外部の編集が消える → 変更を検知したら取り込む
            try:
                cur = self._read_user_dict()
                snap = getattr(self, "_dict_snapshot", None)
                if snap is not None and cur != snap and cur != rows:
                    # 取り込むのは「ダイアログを開いた後にファイル側で**増えた**語」だけ。
                    # 「rowsに無い語」を全部戻すと、ダイアログで削除した語（snapには
                    # あってrowsから消した語）まで無言で復活してしまう
                    seen = {k for k, _ in rows} | {k for k, _ in snap}
                    add = [kv for kv in cur if kv[0] not in seen]
                    if add:
                        rows.extend(add)
                        self._log(f"　（ファイル側で追加された {len(add)} 語も取り込みました）")
            except Exception:
                pass
            try:
                self._write_user_dict(rows)
                _pb._READ_FIXES = None          # 次の合成で読み直させる
                self._log(f"読み方辞書を保存しました（{len(rows)}語・次の合成から反映）。")
                dlg.destroy()
            except Exception as e:
                msg.config(text=f"保存できませんでした: {e}")

        ttk.Button(bf, text="💾 保存して閉じる", command=_save,
                   style="Accent.TButton").pack(side="left", ipadx=10)
        ttk.Button(bf, text="閉じる", command=dlg.destroy).pack(side="left", padx=8)
        ttk.Button(bf, text="📄 ファイルで開く",
                   command=lambda: self._open_reading_file()).pack(side="right")
        dlg.grab_set()

    def _insert_default_topic_guide(self):
        """お題生成の標準指示文を「お題の自動生成への指示」欄へ挿入（編集の土台）。"""
        from izanagi.topics import DEFAULT_TOPIC_GUIDE
        t = self.prompt_texts.get("topic_prompt")
        if t is None:
            return
        cur = t.get("1.0", "end").strip()
        if cur and cur != DEFAULT_TOPIC_GUIDE:
            if not messagebox.askyesno(
                    "IZANAMI", "「お題の自動生成への指示」に既に文章があります。\n"
                               "標準の指示文に置き換えますか？\n"
                               "（いいえ＝今の内容のまま）"):
                return
        t.delete("1.0", "end")
        t.insert("1.0", DEFAULT_TOPIC_GUIDE)
        self._log("標準の指示文を欄に入れました。自由に編集してください"
                  "（この内容がお題生成の指示部にそのまま使われます）。")

    def _open_dict_catalog(self):
        """登録済み辞書（組込＋同梱の全ジャンル辞書）を一覧で見る（読むだけ）。

        従来は1語ずつ検索するしかなく「何が登録されているのか見えない」との要望対応
        （2026-08-03）。"""
        from izanagi.providers.base import dict_catalog
        rows = dict_catalog()
        # 自分の辞書（読み方辞書（登録）で登録した語）も同じ一覧に出す
        # （2026-08-17配布先の混乱対応: 「登録したのに一覧に出ない」＝同梱辞書とは別物
        #  だが、同じ窓で「自分」の出どころ付きで見える方が分かりやすい。最優先で効く）
        try:
            from izanagi.providers.base import _user_fixes
            rows = [(w, r, "自分") for w, r in (_user_fixes() or {}).items()] + list(rows)
        except Exception:
            pass
        dlg = tk.Toplevel(self.root)
        dlg.title(f"登録済みの読み方辞書（{len(rows)}語・読むだけ）")
        dlg.geometry("640x600")
        dlg.transient(self.root)
        frm = ttk.Frame(dlg, padding=(14, 12)); frm.pack(fill="both", expand=True)
        _hint(frm, "ここにある語は、音声を作る時に自動でこの読みに直されます"
                   "（テロップは台本のまま）。出どころ「自分」＝📖読み方辞書（登録）で"
                   "登録した語（自分の辞書が最優先で効きます）。読みを変えたい語は"
                   "同じ語を自分の辞書に登録すると上書きできます。"
              ).pack(anchor="w", pady=(0, 8))
        sf = ttk.Frame(frm); sf.pack(fill="x", pady=(0, 6))
        ttk.Label(sf, text="しぼり込み").pack(side="left")
        q = tk.StringVar()
        ent = ttk.Entry(sf, textvariable=q, width=24)
        ent.pack(side="left", padx=6)
        cnt = ttk.Label(sf, text=f"{len(rows)}語", style="Muted.TLabel")
        cnt.pack(side="left", padx=6)
        tf = ttk.Frame(frm); tf.pack(fill="both", expand=True)
        tv = ttk.Treeview(tf, columns=("w", "r", "s"), show="headings")
        tv.heading("w", text="語"); tv.column("w", width=220)
        tv.heading("r", text="よみ"); tv.column("r", width=240)
        tv.heading("s", text="出どころ"); tv.column("s", width=80, anchor="center")
        sb = ttk.Scrollbar(tf, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True); sb.pack(side="right", fill="y")

        def _fill(*_a):
            key = q.get().strip()
            tv.delete(*tv.get_children())
            n = 0
            for w, r, s in rows:
                if not key or key in w or key in r:
                    tv.insert("", "end", values=(w, r, s))
                    n += 1
            cnt.config(text=f"{n}語" + ("（しぼり込み中）" if key else ""))

        ent.bind("<KeyRelease>", _fill)
        _fill()
        ttk.Button(frm, text="閉じる", command=dlg.destroy).pack(anchor="e", pady=(8, 0))

    def _open_reading_file(self):
        """辞書ファイルをメモ帳で開く（まとめて編集したい人向け）。"""
        f = ROOT / self._READ_USER_FILE
        try:
            if not f.exists():
                self._write_user_dict([])
            os.startfile(str(f))  # noqa: S606
        except Exception as e:
            self._log(f"辞書を開けませんでした: {e}")

    def _import_chrome_logins(self):
        from izanagi import browser_ai
        if browser_ai.chrome_running():
            messagebox.showwarning(
                "IZANAMI",
                "Chromeが起動中です。\nすべてのChromeウィンドウを閉じてから、もう一度押してください。\n"
                "（開いたままコピーするとログイン情報が壊れるためです）")
            return
        profiles = browser_ai.list_source_profiles()
        if not profiles:
            messagebox.showwarning("IZANAMI", "普段のChromeのプロファイルが見つかりませんでした。")
            return
        src = profiles[0][0]
        if len(profiles) > 1:  # 複数プロファイル → 選ばせる
            dlg = tk.Toplevel(self.root); dlg.title("どのChromeプロファイルを取り込む？")
            dlg.transient(self.root); dlg.grab_set()
            ttk.Label(dlg, text="普段使っているプロファイルを選んでください").pack(padx=16, pady=(14, 6))
            disp = [f"{name}（{d}）" for d, name in profiles]
            v = tk.StringVar(value=disp[0])
            ttk.Combobox(dlg, textvariable=v, values=disp, state="readonly",
                         width=40).pack(padx=16, pady=4)
            picked = {"v": None}

            def _ok():
                picked["v"] = profiles[disp.index(v.get())][0]
                dlg.destroy()

            bt = ttk.Frame(dlg); bt.pack(pady=12)
            ttk.Button(bt, text="取り込む", command=_ok, style="Accent.TButton").pack(side="left", padx=6)
            ttk.Button(bt, text="キャンセル", command=dlg.destroy).pack(side="left")
            self.root.wait_window(dlg)
            if not picked["v"]:
                return
            src = picked["v"]
        if not messagebox.askyesno(
                "IZANAMI",
                "普段のChromeのログイン情報を、このツール専用のブラウザプロファイルへコピーします。\n"
                "・普段のChromeは変更されません\n・専用プロファイルの今の中身は作り直しになります\n"
                "・数分かかることがあります\n\nよろしいですか？"):
            return
        self.import_btn.config(state="disabled", text="…取り込み中")
        dest = self.profile_var.get().strip() or config.DEFAULTS["chrome_profile"]
        src_disp = next((n for d, n in profiles if d == src), src)

        def _worker():
            try:
                if browser_ai.import_chrome_logins(dest, src, log=self._log):
                    self.log_queue.put("__BROWSER_IMPORT_OK__" + src_disp)
            except Exception:
                self._log("【エラー】\n" + traceback.format_exc())
            finally:
                self.log_queue.put("__BROWSER_IMPORT_DONE__")

        threading.Thread(target=_worker, daemon=True).start()
        self.nb.select(self.tab_log)

    # ---------- 投稿タブ ----------
    def _post_refresh_list(self):
        """出力フォルダを走査して、完成動画(final.mp4あり)を一覧に出す。"""
        try:
            out = Path(self.out_var.get() or config.DEFAULTS["output_dir"])
        except Exception:
            out = Path(config.DEFAULTS["output_dir"])
        flt = self._post_filter_name()  # ''=すべて
        rows = []
        # 旧名IZANAGI_*のフォルダも一覧に出す（改名前に作った動画の互換・2026-08-06改名）
        for d in sorted([*out.glob("IZANAMI_*"), *out.glob("IZANAGI_*")],
                        key=lambda x: x.name.split("_", 1)[-1], reverse=True):
            v = d / "compose" / "final.mp4"
            if not v.exists():
                continue
            memo = util.load_json(d / "memo.json", {}) or {}
            if flt and (memo.get("channel") or "") != flt:
                continue  # チャンネル絞り込み中はその動画だけ
            made = datetime.datetime.fromtimestamp(v.stat().st_mtime).strftime("%m/%d %H:%M")
            if (d / "uploaded.flag").exists():
                st = "✅ 投稿済み"
            elif (d / "studio_pending.flag").exists():
                st = "⏳Studioで公開"  # 添付・入力済み。人がブラウザで公開ボタンを押す
            else:
                st = "未投稿"
                rep = util.load_json(d / "script" / "policy_report.json", {}) or {}
                if any(f.get("severity") == "high" for f in rep.get("findings", [])):
                    st = "⚠要確認"  # 収益化リスク語が残っている
                elif not self._has_thumb(d, memo):
                    # サムネ生成に失敗しても動画は完成扱いになる（設計）。ただし
                    # そのまま投稿するとYouTubeの自動切り出し画像になる＝気づけるようにする
                    st = "⚠サムネ無"
            name = memo.get("channel") or d.name
            # タイトルの前に作業フォルダの日時（IZANAMI_20260820_110213_… の数字）を付ける
            # （2026-08-20配布先要望: 一覧とフォルダを突き合わせやすくする）
            _m = re.match(r"IZANA[MG]I_(\d{8})_(\d{6})", d.name)
            _stamp = f"[{_m.group(1)[4:]}_{_m.group(2)}] " if _m else ""
            rows.append((str(d), name if memo.get("channel") else d.name,
                         _stamp + memo.get("title", "（タイトルなし）"), made, st))
        self._post_checked &= {r[0] for r in rows}
        tree = self.post_tree
        tree.delete(*tree.get_children())
        for iid, name, title, made, st in rows:
            chk = "☑" if iid in self._post_checked else "☐"
            tree.insert("", "end", iid=iid, values=(chk, name, title, made, st))

    @staticmethod
    def _has_thumb(folder, memo: dict) -> bool:
        """この動画にサムネ画像があるか（memoが空でも実ファイルがあればOK）。"""
        t = (memo or {}).get("thumbnail") or ""
        if t and Path(t).exists():
            return True
        return (Path(folder) / "thumb" / "thumbnail.png").exists()

    def _post_filter_name(self) -> str:
        """投稿タブのチャンネル絞り込み。''=すべて / それ以外=チャンネル名。"""
        try:
            i = self.post_ch_combo.current()  # 0=すべて / 1..10=チャンネル
        except Exception:
            return ""
        if i <= 0:
            return ""
        try:
            return self.cfg["channels"][i - 1].get("name") or f"チャンネル{i}"
        except Exception:
            return ""

    def _on_post_channel_filter(self, event=None):
        """チャンネルを選ぶ → 一覧を絞り込み＋そのチャンネルの投稿先URLを表示。"""
        i = self.post_ch_combo.current()
        if i <= 0:  # すべて → 全体設定のURLに戻す
            self.post_channel_var.set(self.cfg.get("youtube_channel_url_global")
                                      or self.cfg.get("youtube_channel_url", ""))
        else:
            try:
                ch = self.cfg["channels"][i - 1]
            except Exception:
                ch = {}
            url = (ch.get("youtube_channel_url") or "").strip()
            # ch専用URLが未設定なら全体設定を表示（空で照合が消えるのを防ぐ）
            self.post_channel_var.set(url or self.cfg.get("youtube_channel_url_global")
                                      or self.cfg.get("youtube_channel_url", ""))
            if not url:
                self._log(f"※ {ch.get('name', '')} の投稿先URLは未設定です"
                          "（文章・世界観タブの「チャンネル運用設定」で設定できます）。")
        self._post_refresh_list()

    def _post_tree_click(self, ev):
        row = self.post_tree.identify_row(ev.y)
        if not row:
            return
        if self.post_tree.identify_column(ev.x) == "#1":  # ✓列クリックでオン/オフ
            if row in self._post_checked:
                self._post_checked.discard(row)
            else:
                self._post_checked.add(row)
            vals = list(self.post_tree.item(row, "values"))
            vals[0] = "☑" if row in self._post_checked else "☐"
            self.post_tree.item(row, values=vals)

    def _post_play_selected(self):
        sel = self.post_tree.selection()
        if not sel:
            messagebox.showinfo("IZANAMI", "一覧から動画を選んでください。")
            return
        v = Path(sel[0]) / "compose" / "final.mp4"
        if v.exists():
            os.startfile(str(v))  # noqa: S606

    @staticmethod
    def _deletable_projects(targets, out_root) -> list:
        """削除対象の安全確認: 保存先(output)直下に実在するフォルダだけ通す。

        一覧のiid=フォルダパスだが、万一別の場所を指す値が紛れても
        保存先の外は絶対に消さない（誤消去のフェイルセーフ）。"""
        try:
            root = Path(out_root).resolve()
        except Exception:
            return []
        out = []
        for t in targets:
            try:
                p = Path(t)
                if p.is_dir() and p.resolve().parent == root:
                    out.append(p)
            except Exception:
                continue
        return out

    def _post_delete_checked(self):
        """一覧でチェックした動画フォルダをごみ箱へ移動する（復元できる削除）。"""
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("IZANAMI", "実行中は削除できません（終わってから、または⏹中断してから）。")
            return
        # サムネ再生成/文字提案は self.worker でない別スレッド＝これも削除とぶつかる
        # （書き込み中のフォルダを消すとゾンビフォルダが復活する・2026-08-04敵対レビュー）
        if getattr(self, "_thumb_busy", False):
            messagebox.showinfo("IZANAMI", "サムネ生成の実行中は削除できません（終わってからどうぞ）。")
            return
        targets = [t for t in self.post_tree.get_children() if t in self._post_checked]
        if not targets:
            messagebox.showinfo("IZANAMI", "一覧の ✓ 列をクリックして、削除する動画にチェックを入れてください。")
            return
        out_root = self.out_var.get() or config.DEFAULTS["output_dir"]
        safe = self._deletable_projects(targets, out_root)
        if not safe:
            messagebox.showinfo("IZANAMI", "削除できる動画フォルダがありません（保存先の外は削除しません）。")
            return
        names = []
        for p in safe[:8]:
            memo = util.load_json(p / "memo.json", {}) or {}
            names.append("・" + (memo.get("title") or p.name)[:40])
        more = f"\n…ほか{len(safe) - 8}本" if len(safe) > 8 else ""
        uploaded = [p for p in safe if (p / "uploaded.flag").exists()
                    or (p / "studio_pending.flag").exists()]
        # ネットワーク/USB等の保存先はごみ箱に入らず完全削除になる＝正直にそう聞く
        recyclable = util.recycle_supported(out_root)
        what = "ごみ箱へ移動" if recyclable else "削除"
        msg = (f"チェックした {len(safe)} 本の動画フォルダを{what}します。\n"
               + "\n".join(names) + more + "\n\n")
        if uploaded:
            msg += (f"⚠ うち {len(uploaded)} 本は投稿済み/Studio手続き済みです"
                    "（YouTube上の動画は消えません。ローカルの元データだけ消えます）。\n\n")
        if recyclable:
            msg += "よろしいですか？（ごみ箱から元に戻せます）"
        else:
            msg += ("⚠ この保存先（ネットワーク/USBドライブ等）ではごみ箱が使えないため、"
                    "完全に削除されます（元に戻せません）。本当に削除しますか？")
        if not messagebox.askyesno("動画の削除", msg):
            return
        err = util.send_to_recycle(safe)
        self._post_checked.clear()
        self._post_refresh_list()
        if err:
            messagebox.showwarning("IZANAMI", err)
            self._log(f"⚠ 動画の削除: {err}")
        elif recyclable:
            self._log(f"🗑 {len(safe)} 本の動画フォルダをごみ箱へ移動しました（ごみ箱から復元できます）。")
        else:
            self._log(f"🗑 {len(safe)} 本の動画フォルダを削除しました（この保存先ではごみ箱に入りません）。")

    def _post_load_selected(self):
        sel = self.post_tree.selection()
        if sel:
            self.post_folder_var.set(sel[0])
            self._post_load()

    def _post_cfg(self) -> dict:
        """投稿タブの設定からアップロード用cfgを組み立てる（単発/まとめて共通）。"""
        # 文章・世界観タブで編集中のch別スロット等を先に取り込む（💾保存前でも効くように。
        # これが無いとスロット編集直後の「まとめて投稿」が旧値で予約される＝2026-08-05敵対検証）
        try:
            self._save_channel_fields(self.cfg.get("active_channel", 0))
        except Exception:
            pass
        cfg = dict(self.cfg)
        # 設定タブの予約時刻・何日後に公開も画面の最新値を使う（💾保存前の古い値で
        # YouTubeの実予約時刻が確定していた＝2026-08-05敵対検証・HIGH）
        try:
            slots = [s.strip() for s in self.slots_var.get().split(",") if s.strip()]
            if slots:
                cfg["default_schedule_slots"] = slots
            cfg["default_publish_offset_days"] = int(self.offset_var.get().strip())
        except Exception:
            pass
        cfg.update({
            "upload_engine": pval(ENG_LABELS["upload"], self.post_engine_var.get()),
            "upload_dry_run": self.post_dry_var.get(),
            "youtube_privacy": pval(PRIVACY_PAIRS, self.post_privacy_var.get()),
            "youtube_category_id": self.post_cat_var.get().strip(),
            "youtube_made_for_kids": self.post_kids_var.get(),
            "youtube_client_secret": self.cs_var.get().strip(),
            "youtube_token": self.tok_var.get().strip(),
            "youtube_channel_url": self.post_channel_var.get().strip(),
            "youtube_channel_url_global": self.post_channel_var.get().strip(),
            "chrome_profile": self.profile_var.get().strip(),
            "browser_exe": self._browser_exe_value(),
            # 予約の確定まで全自動（画面のチェックが正・2026-08-06方針転換）
            "studio_auto_publish": bool(self.post_auto_var.get()),
        })
        try:
            cfg["cdp_port"] = int(self.port_var.get())
        except ValueError:
            cfg["cdp_port"] = 9222
        return cfg

    def _post_run_batch(self):
        """一覧でチェックした動画を、予約時刻を割り振りながら順番に投稿する。"""
        if self.worker and self.worker.is_alive():
            return
        targets = [t for t in self.post_tree.get_children() if t in self._post_checked]
        if not targets:
            messagebox.showinfo("IZANAMI", "一覧の ✓ 列をクリックして、投稿する動画にチェックを入れてください。")
            return
        # 投稿済み（uploaded.flag）＋Studio引き渡し済み（studio_pending.flag）は再投稿しない
        # ＝二重投稿の防止（Studio版はflagを書かず素通りしていた穴を塞ぐ）
        skip = [t for t in targets if (Path(t) / "uploaded.flag").exists()
                or (Path(t) / "studio_pending.flag").exists()]
        if skip:
            self._log(f"⚠ 投稿済み/Studio手続き済みのため {len(skip)} 本はスキップします"
                      "（もう一度出す場合は各動画を単発🚀で）。")
        targets = [t for t in targets if t not in skip]
        if not targets:
            return
        # サムネ無しのまま出すとYouTube側の自動切り出し画像になる＝クリック率に直結するので確認
        no_thumb = [t for t in targets
                    if not self._has_thumb(t, util.load_json(Path(t) / "memo.json", {}) or {})]
        if no_thumb:
            names = "\n".join("・" + Path(t).name for t in no_thumb[:5])
            more = f"\n…ほか{len(no_thumb) - 5}本" if len(no_thumb) > 5 else ""
            if not messagebox.askyesno(
                    "サムネイルがありません",
                    f"次の {len(no_thumb)} 本にはサムネイル画像がありません。\n{names}{more}\n\n"
                    "このまま投稿すると、YouTubeが動画から自動で切り出した画像になります。\n"
                    "（投稿タブの「🖼 AIで作り直す」で先に作れます）\n\n"
                    "それでも投稿しますか？"):
                return
        cfg = self._post_cfg()
        if (cfg["upload_engine"] == "api" and not cfg["upload_dry_run"]
                and not (cfg.get("youtube_client_secret") and Path(cfg["youtube_client_secret"]).exists())):
            messagebox.showwarning("IZANAMI", "YouTube の認証(client_secret.json)が未設定です（設定・キー タブ）。"
                                              "先に設定するか、投稿方法を「YouTube Studio（手動）」か「お試し」にしてください。")
            return
        if not cfg["upload_dry_run"]:
            studio_auto = (cfg["upload_engine"] == "studio_web"
                           and bool(cfg.get("studio_auto_publish", True)))
            extra = ("Studioでは予約の確定（スケジュール設定）まで全自動で行います。\n"
                     if studio_auto else "")
            if (cfg.get("youtube_privacy") or "private") != "private":
                slot_note = ("公開のしかたが「限定公開/すぐ公開」のため予約はせず、\n"
                             "各動画を即時その公開状態で投稿します（YouTubeの予約は\n"
                             "「非公開→予約時刻に公開」のみ）。\n")
            else:
                slot_note = ("公開日時は「空いている予約枠」へ自動で割り当てます\n"
                             "（既存の予約と重複しない・1日の上限本数を守る・チャンネル別時刻に対応）。\n")
            if not messagebox.askyesno(
                    "IZANAMI",
                    f"{len(targets)}本をまとめて投稿します。\n"
                    + slot_note + extra +
                    "よろしいですか？"):
                return
        self._stop = False
        self.post_btn.config(state="disabled")
        self.post_batch_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.worker = threading.Thread(target=self._post_batch_worker, args=(targets, cfg), daemon=True)
        self.worker.start()

    def _post_batch_worker(self, targets, cfg):
        from izanagi import schedule
        okc = 0
        manualc = 0
        out = self.out_var.get().strip() or config.DEFAULTS["output_dir"]
        util.prevent_sleep(True)  # まとめて投稿の完走中もPCを寝かせない
        try:
            for i, folder in enumerate(targets, 1):
                if self._stop:
                    self._log("⏹ まとめて投稿を中断しました。")
                    break
                self.log_queue.put(f"__STATUS__予約投稿中 {i}/{len(targets)}")
                memo = util.load_json(Path(folder) / "memo.json", {}) or {}
                ch = memo.get("channel", "")
                # 予約枠の自動割当（memoに書いてから投稿→次の1本は自動的にその枠を避ける）
                c2 = dict(cfg)
                # 既定は全体設定スロット（アクティブchのスロットを他chの動画に流用しない）
                c2["schedule_slots"] = ""
                for chd in self.cfg.get("channels", []):  # ch別の時刻スロット
                    if ch and chd.get("name") == ch and chd.get("schedule_slots"):
                        c2["schedule_slots"] = chd["schedule_slots"]
                        break
                # 限定公開/すぐ公開は予約できない（YouTube仕様）→ 枠を割り当てず即時投稿。
                # 割り当てると予約枠だけ消費して、provider側で捨てられる（2026-08-17）
                if (cfg.get("youtube_privacy") or "private") != "private":
                    when = ""
                    memo["scheduled_publish_at"] = ""
                else:
                    slots = schedule.next_free_slots(c2, out, n=1, channel=ch)
                    when = slots[0] if slots else ""
                self._log(f"—— まとめて投稿 {i}/{len(targets)}: {Path(folder).name}"
                          f"（公開: {when or '即時'}｜ch: {ch or '共通'}） ——")
                if when:
                    memo["scheduled_publish_at"] = when
                memo["privacy"] = cfg.get("youtube_privacy", "private")
                memo["category_id"] = cfg.get("youtube_category_id", "22")
                util.save_json_atomic(Path(folder) / "memo.json", memo)
                # チャンネル別の投稿先URL（memo刻印を優先＝誤爆投稿の防止）
                if memo.get("channel_url"):
                    if c2.get("youtube_channel_url") and \
                            c2["youtube_channel_url"] != memo["channel_url"]:
                        self._log(f"    ⚠ 投稿先をこの動画のチャンネルに合わせます: {memo['channel_url']}")
                    c2["youtube_channel_url"] = memo["channel_url"]
                try:
                    res = pipeline.upload_project(folder, c2, log=self._log, stop_flag=lambda: self._stop)
                    if res.get("ok"):
                        okc += 1
                    elif res.get("manual"):
                        manualc += 1  # Studioに引き渡し済み＝人が公開ボタンを押す必要あり
                        self._log("    ⏳ ブラウザで公開設定して『公開/スケジュール』を押してください"
                                  "（まだYouTubeには予約されていません）。")
                    elif not self._stop:  # ⏹中断のときは次のループ先頭のログに任せる
                        self._log("⚠ この動画は完了しませんでした: "
                                  + str(res.get("error") or res.get("note") or ""))
                except Exception:
                    self._log("【エラー】\n" + traceback.format_exc())
            tail = f"🌙 まとめて投稿おわり: 予約完了 {okc}本"
            if manualc:
                tail += f" / 要ブラウザ操作 {manualc}本"
            fails = len(targets) - okc - manualc
            if fails > 0:
                tail += f" / 失敗 {fails}本"
            self._log(tail)
        finally:
            util.prevent_sleep(False)
            self.log_queue.put("__POST_DONE__")

    def _post_latest(self):
        """出力フォルダから最新の完成プロジェクトを自動で読み込む（チャンネル絞り込み中はその中で最新）。"""
        out = Path(self.out_var.get() or config.DEFAULTS["output_dir"])
        flt = self._post_filter_name()
        cands = []
        for d in [*out.glob("IZANAMI_*"), *out.glob("IZANAGI_*")]:  # 旧名も対象
            if not (d / "memo.json").exists():
                continue
            if flt:
                memo = util.load_json(d / "memo.json", {}) or {}
                if (memo.get("channel") or "") != flt:
                    continue
            cands.append(d)
        cands.sort(key=lambda d: d.stat().st_mtime, reverse=True)
        if not cands:
            messagebox.showinfo("IZANAMI", ("このチャンネルの完成動画がまだありません。" if flt else
                                            "完成した動画がまだありません。先に「作成」タブで作ってください。"))
            return
        self.post_folder_var.set(str(cands[0]))
        self._post_load()

    def _suggest_when(self) -> str:
        """予約スロット設定から次の公開日時（表示形式）を提案する。"""
        try:
            slots = [s.strip() for s in self.slots_var.get().split(",") if s.strip()] or ["09:00"]
            off = max(0, int(self.offset_var.get() or 1))
        except Exception:
            slots, off = ["09:00"], 1
        d = datetime.date.today() + datetime.timedelta(days=max(1, off))
        return f"{d.isoformat()} {slots[0]}"

    def _post_pick(self):
        d = filedialog.askdirectory(title="完成した動画フォルダ(IZANAMI_...)を選ぶ",
                                    initialdir=self.out_var.get() or str(Path.home()))
        if d:
            self.post_folder_var.set(d)
            self._post_load()

    def _post_load(self):
        folder = Path(self.post_folder_var.get().strip())
        memo = util.load_json(folder / "memo.json", None)
        if memo is None:
            messagebox.showwarning("IZANAMI", "この中に memo.json がありません。先に「動画を作る」を完了してください。")
            return
        self.post_title_var.set(memo.get("title", ""))
        cands = [f"{c.get('pattern','')}｜{c.get('title','')}"
                 for c in memo.get("title_candidates", []) if c.get("title")]
        try:
            self.post_cand_combo.config(values=cands)
            self.post_cand_var.set("他の候補▼" if cands else "候補なし")
        except Exception:
            pass
        self.post_desc_text.delete("1.0", "end"); self.post_desc_text.insert("1.0", memo.get("description", ""))
        self.post_tags_var.set(", ".join(memo.get("tags", [])))
        when = _iso_to_disp(memo.get("scheduled_publish_at", ""))
        self.post_when_var.set(when or self._suggest_when())
        self.post_privacy_var.set(pdisp(PRIVACY_PAIRS, memo.get("privacy", "private")))
        self.post_cat_var.set(str(memo.get("category_id", self.post_cat_var.get())))
        video = memo.get("video", "") or str(folder / "compose" / "final.mp4")
        self.post_video_label.config(text=f"動画: {'あり ✅' if Path(video).exists() else '無し ⚠'}\n{video}")
        flag = folder / "uploaded.flag"
        self.post_status_label.config(
            text=("⚠ すでに投稿済み: " + flag.read_text(encoding='utf-8').strip()) if flag.exists() else "")
        self._show_thumb(memo.get("thumbnail", "") or str(folder / "thumb" / "thumbnail.png"))
        # サムネ文字の初期値: meta.thumb_text（無ければタイトル）
        smeta = util.load_json(folder / "script" / "meta.json", {}) or {}
        self.post_thumbtext_var.set(smeta.get("thumb_text", "") or memo.get("title", ""))
        self.thumb_regen_status.config(text="")
        self._log(f"投稿内容を読み込みました: {folder.name}")

    def _show_thumb(self, path):
        self._thumb_preview_path = str(path or "")
        try:
            from PIL import Image, ImageTk
            if path and Path(path).exists():
                im = Image.open(path).convert("RGB"); im.thumbnail((280, 180))
                self._thumb_photo = ImageTk.PhotoImage(im)
                self.thumb_label.config(image=self._thumb_photo, text="")
                return
        except Exception:
            pass
        self.thumb_label.config(image="", text="（サムネなし）")

    def _open_thumb_preview(self):
        """プレビューをクリック→サムネを原寸で開く（小画面でも確認できるように）。"""
        p = getattr(self, "_thumb_preview_path", "")
        if p and Path(p).exists():
            os.startfile(p)  # noqa: S606

    def _post_open_video(self):
        v = Path(self.post_folder_var.get().strip()) / "compose" / "final.mp4"
        if v.exists():
            os.startfile(str(v))  # noqa
        else:
            messagebox.showinfo("IZANAMI", "final.mp4 が見つかりません。")

    def _thumb_font_label(self) -> str:
        """cfg["thumb_font"]（ファイル名 or 表示名）→ フォント選択コンボの表示名。"""
        from izanagi.thumb_text import THUMB_FONTS
        cur = (self.cfg.get("thumb_font") or "").strip()
        for label, fname in THUMB_FONTS.items():
            if cur == label or (fname and cur == fname):
                return label
        return "おまかせ（自動）"

    def _thumb_color_label(self, key: str) -> str:
        """設定値（#RRGGBB or プリセット名）→ 欄の表示（プリセット名があればそれ）。"""
        from izanagi.thumb_text import THUMB_COLOR_PRESETS
        v = (self.cfg.get(key) or "").strip()
        if not v:
            return "既定"
        for name, hx in THUMB_COLOR_PRESETS.items():
            if hx and hx.lower() == v.lower():
                return name
        return v

    def _on_thumb_color(self, event=None):
        """サムネ文字の上段/下段の色を保存（次の焼き込みから反映。✏文字だけ で即確認できる）。"""
        from izanagi.thumb_text import THUMB_COLOR_PRESETS, resolve_color
        changed = False
        for key, var in (("thumb_color_top", self.thumb_color_top_var),
                         ("thumb_color_bottom", self.thumb_color_bottom_var)):
            raw = (var.get() or "").strip()
            if raw in ("", "既定"):
                val = ""
            elif raw in THUMB_COLOR_PRESETS:
                val = THUMB_COLOR_PRESETS[raw]
            else:
                val = resolve_color(raw, "")
                if not val:   # 読めない入力は既定へ戻して知らせる
                    var.set("既定")
                    self._log(f"サムネ文字の色「{raw}」は読めません（#FF3B3B の形式か一覧から）。既定に戻しました。")
            if (self.cfg.get(key) or "") != val:
                self.cfg[key] = val
                changed = True
        if changed:
            try:
                config.save_settings(self.cfg)
            except Exception:
                pass

    def _thumb_insert_break(self):
        """サムネ文字欄のカーソル位置に改行記号「/」を入れる。"""
        try:
            e = self.post_thumbtext_entry
            e.insert("insert", "/")
            e.focus_set()
        except Exception:
            pass

    def _on_thumb_font(self, event=None):
        """サムネ文字フォントの選択を保存（次のサムネ焼き込みから反映）。"""
        from izanagi.thumb_text import THUMB_FONTS
        self.cfg["thumb_font"] = THUMB_FONTS.get(self.thumb_font_var.get(), "")
        try:
            config.save_settings(self.cfg)
        except Exception:
            pass

    def _thumb_cfg(self) -> dict:
        """サムネ操作用のcfg。APIキー＋いま選択中チャンネルの最新フィールド
        （thumbnail_prompt / title_prefix 等）を反映させる。"""
        idx = self.cfg.get("active_channel", 0)
        try:
            self._save_channel_fields(idx)  # 文章・世界観タブの編集を取り込む
        except Exception:
            pass
        cfg = dict(self.cfg)
        # 画面で選択中のエンジンを反映（self.cfgは💾保存時にしか更新されない＝
        # 保存前だと古いエンジンでAIが動く。実害: ①台本=デスクトップ選択なのに
        # claude_webでChromeを開いて12分×2失敗＝2026-08-05配布先報告・不具合4-1）
        try:
            for cap in ("script", "visual", "thumb"):
                cfg[f"{cap}_engine"] = pval(ENG_LABELS[cap], self.engine_vars[cap].get())
            cfg["brushup_engine"] = cfg["script_engine"]
        except Exception:
            pass
        for kk in ("openai_api_key", "gemini_api_key", "anthropic_api_key"):
            v = self.key_vars.get(kk) if hasattr(self, "key_vars") else None
            if v is not None:
                cfg[kk] = v.get().strip()
        try:
            for k in config.CHANNEL_KEYS:
                cfg[k] = self.cfg["channels"][idx].get(k, cfg.get(k, ""))
        except Exception:
            pass
        # 失敗時の診断ダンプが残るように（従来はdebug_dir=Noneで空応答の証拠が残らなかった）
        try:
            folder = self.post_folder_var.get().strip()
            if folder:
                cfg["debug_dump"] = True
                cfg["_debug_dir"] = str(Path(folder) / "_debug")
        except Exception:
            pass
        try:  # 「使うブラウザ」も画面の最新値（💾前の旧ブラウザで3分ハングする穴）
            cfg["browser_exe"] = self._browser_exe_value()
        except Exception:
            pass
        return cfg

    def _thumb_regen(self, full: bool = True):
        """投稿タブで読み込み中の動画のサムネを作り直す。full=AI背景から / False=文字だけ入れ直し。"""
        if getattr(self, "_thumb_busy", False):
            return
        folder = self.post_folder_var.get().strip()
        if not folder or not (Path(folder) / "memo.json").exists():
            messagebox.showwarning("IZANAMI", "先に動画フォルダを読み込んでください。")
            return
        cfg = self._thumb_cfg()
        if full:
            self._log(f"🖼 サムネを作り直します（⑤サムネのエンジン: {self.engine_vars['thumb'].get()}）…")
        copy_text = self.post_thumbtext_var.get().strip()
        self._thumb_busy = True
        self.thumb_regen_btn.config(state="disabled")
        self.thumb_text_btn.config(state="disabled")
        self.thumb_regen_status.config(
            text="AIで背景を作成中…（20〜40秒）" if full else "文字を入れ直しています…")
        threading.Thread(target=self._thumb_regen_worker,
                         args=(folder, cfg, copy_text, full), daemon=True).start()

    def _thumb_suggest(self):
        """✨ この動画の中身から「バズるサムネ文字」案をAIで5個出す。"""
        if getattr(self, "_thumb_busy", False):
            return
        folder = self.post_folder_var.get().strip()
        if not folder or not (Path(folder) / "memo.json").exists():
            messagebox.showwarning("IZANAMI", "先に動画フォルダを読み込んでください。")
            return
        cfg = self._thumb_cfg()
        # 「実際に何を使うか」を冒頭で明示（設定の取り違えにその場で気づけるように）
        self._log(f"✨ 文字案を生成します（①台本のエンジン: {self.engine_vars['script'].get()}）…")
        self._thumb_busy = True
        self.thumb_suggest_btn.config(state="disabled")
        self.thumb_regen_status.config(foreground=GOLD,
                                       text="AIでバズる文字を考え中…（数秒〜十数秒）")
        threading.Thread(target=self._thumb_suggest_worker,
                         args=(folder, cfg), daemon=True).start()

    def _thumb_suggest_worker(self, folder, cfg):
        cands = []
        pw = browser = ctx = None
        try:
            from izanagi import browser_ai, providers
            # APIキーが無く、台本エンジンがWeb版なら、ログイン済みブラウザで生成する
            need_web = not (cfg.get("anthropic_api_key") or cfg.get("gemini_api_key"))
            if need_web and providers.engine_needs_browser("script", cfg.get("script_engine", "")):
                try:
                    pw, browser, ctx = browser_ai.open_session(
                        cfg.get("chrome_profile"), int(cfg.get("cdp_port", 9222)),
                        self._log, browser_exe=cfg.get("browser_exe", ""))
                except Exception as e:
                    self._log(f"    ブラウザ接続に失敗: {e}")
            cands = pipeline.suggest_thumb_copies(folder, cfg, n=5, log=self._log, ctx=ctx)
        except Exception:
            self._log("【エラー】\n" + traceback.format_exc())
        finally:
            if pw or browser:
                try:
                    from izanagi import browser_ai
                    browser_ai.close_session(pw, browser, int(cfg.get("cdp_port", 9222)),
                                             self._log, kill=False)
                except Exception:
                    pass
        self.log_queue.put("__THUMBCOPY__" + "\t".join(cands))

    def _thumb_regen_worker(self, folder, cfg, copy_text, full):
        res = {"ok": False, "error": "不明なエラー"}
        pw = browser = ctx = None
        try:
            from izanagi import browser_ai, providers
            # サムネのエンジンがWeb版なら、ログイン済みブラウザを開いて渡す
            # （既定の配布設定は全エンジンWeb版＝APIキー無し。ここでブラウザを
            #   開かないと「🖼 AIで作り直す」が必ずキー未設定エラーになる）
            if full and providers.engine_needs_browser("thumb", cfg.get("thumb_engine", "")):
                try:
                    pw, browser, ctx = browser_ai.open_session(
                        cfg.get("chrome_profile"), int(cfg.get("cdp_port", 9222)),
                        self._log, browser_exe=cfg.get("browser_exe", ""))
                except Exception as e:
                    self._log(f"    ブラウザ接続に失敗: {e}（APIキーがあればそちらを使います）")
            res = pipeline.regen_thumbnail(folder, cfg, copy_text=copy_text,
                                           full=full, log=self._log, ctx=ctx)
        except Exception:
            self._log("【エラー】\n" + traceback.format_exc())
            res = {"ok": False, "error": "例外が発生しました（ログ参照）"}
        finally:
            if pw or browser:
                try:
                    from izanagi import browser_ai
                    browser_ai.close_session(pw, browser, int(cfg.get("cdp_port", 9222)),
                                             self._log, kill=False)
                except Exception:
                    pass
            self.log_queue.put("__THUMB_DONE__" + ("1" if res.get("ok") else "0")
                               + "\t" + (res.get("thumb") or res.get("error") or ""))

    def _post_save(self) -> bool:
        folder = Path(self.post_folder_var.get().strip())
        if not (folder / "memo.json").exists():
            messagebox.showwarning("IZANAMI", "先に動画フォルダを選んで読み込んでください。")
            return False
        memo = util.load_json(folder / "memo.json", {}) or {}
        memo["title"] = self.post_title_var.get().strip()
        memo["description"] = self.post_desc_text.get("1.0", "end").strip()
        memo["tags"] = [t.strip() for t in self.post_tags_var.get().split(",") if t.strip()]
        memo["privacy"] = pval(PRIVACY_PAIRS, self.post_privacy_var.get())
        # 限定公開/すぐ公開は予約できない＝日時をmemoに残すと、使われない「幽霊予約枠」
        # として以後の枠割当・1日上限を狂わせる（敵対検証2026-08-17）→ 空で保存
        memo["scheduled_publish_at"] = ("" if memo["privacy"] != "private"
                                        else _disp_to_iso(self.post_when_var.get()))
        memo["category_id"] = self.post_cat_var.get().strip()
        util.save_json_atomic(folder / "memo.json", memo)
        self._log("投稿内容を保存しました。")
        return True

    def _post_run(self):
        if self.worker and self.worker.is_alive():
            return
        folder = self.post_folder_var.get().strip()
        if not folder or not (Path(folder) / "memo.json").exists():
            messagebox.showwarning("IZANAMI", "先に動画フォルダを選んで読み込んでください。")
            return
        if not (Path(folder) / "compose" / "final.mp4").exists():
            messagebox.showwarning("IZANAMI", "この中に完成動画(final.mp4)がありません。先に作成を完了してください。")
            return
        cfg = self._post_cfg()
        engine, dry = cfg["upload_engine"], cfg["upload_dry_run"]
        if engine == "api" and not dry and not (cfg.get("youtube_client_secret") and Path(cfg["youtube_client_secret"]).exists()):
            messagebox.showwarning("IZANAMI", "YouTube の認証(client_secret.json)が未設定です（設定・キー タブ）。"
                                              "先に設定するか、投稿方法を「YouTube Studio（手動）」か「お試し」にしてください。")
            return
        if not self._post_save():
            return
        # Studioに引き渡し済み（studio_pending.flag）を再度出す時は二重投稿の確認
        if (Path(folder) / "studio_pending.flag").exists() and not dry:
            if not messagebox.askyesno("IZANAMI",
                    "この動画はすでにStudioへ引き渡し済みです（ブラウザで公開待ちの可能性）。\n"
                    "もう一度Studioを開きますか？（二重投稿にご注意ください）"):
                return
        # 限定公開/すぐ公開は予約できない（YouTube仕様: 予約=非公開→予約時刻に公開のみ）。
        # 旧版は日時と限定公開を並記したまま公開予約で確定していた（2026-08-17配布先報告）
        _pv = cfg.get("youtube_privacy", "private")
        _when_note = ("※「限定公開/すぐ公開」は予約できないため、公開日時は使わず"
                      "即時にその公開状態で確定します\n"
                      "（予約したい場合は「非公開（予約はこれ）」を選択）\n"
                      if _pv != "private" and self.post_when_var.get() else "")
        if not dry and engine == "api":
            when = self.post_when_var.get() or "(保存した予約時刻)"
            if not messagebox.askyesno("IZANAMI",
                                       f"YouTubeへ投稿します。\n公開日時: {when}\n公開のしかた: {self.post_privacy_var.get()}\n"
                                       + _when_note + "よろしいですか？"):
                return
        # Studio全自動も実予約が成立するので、API経路と同じ最終確認を出す（2026-08-06）
        if not dry and engine == "studio_web" and bool(cfg.get("studio_auto_publish", True)):
            when = self.post_when_var.get() or "（日時未指定 → 公開のしかたで確定）"
            if not messagebox.askyesno("IZANAMI",
                                       f"Studioで予約の確定（スケジュール設定）まで全自動で行います。\n"
                                       f"公開日時: {when}\n公開のしかた: {self.post_privacy_var.get()}\n"
                                       + _when_note +
                                       "（自動で確定できない場合だけ、最後の操作を画面に引き渡します）\n"
                                       "よろしいですか？"):
                return
        self._stop = False
        self.post_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.worker = threading.Thread(target=self._post_worker, args=(folder, cfg), daemon=True)
        self.worker.start()

    def _post_worker(self, folder, cfg):
        try:
            self.log_queue.put("__STATUS__予約投稿中")
            # memoのチャンネル刻印を照合（まとめて投稿と同じ誤爆防止を単発🚀にも適用）
            memo = util.load_json(Path(folder) / "memo.json", {}) or {}
            if memo.get("channel_url"):
                if cfg.get("youtube_channel_url") and \
                        cfg["youtube_channel_url"] != memo["channel_url"]:
                    self._log(f"⚠ 投稿先をこの動画のチャンネルに合わせます: {memo['channel_url']}")
                cfg = dict(cfg)
                cfg["youtube_channel_url"] = memo["channel_url"]
            res = pipeline.upload_project(folder, cfg, log=self._log, stop_flag=lambda: self._stop)
            if res.get("ok"):
                self._log("✅ 投稿処理が完了しました。" if not cfg.get("upload_dry_run") else "✅ お試し完了（投稿はしていません）。")
            elif res.get("manual"):
                self._log("⏳ Studioに引き渡しました。ブラウザで公開設定・予約日時を選び、"
                          "最後に『公開/スケジュール設定』を押してください（まだYouTubeには予約されていません）。")
            elif not self._stop:
                self._log("⚠ 投稿は完了しませんでした: " + str(res.get("error") or res.get("note") or ""))
        except Exception:
            self._log("【エラー】\n" + traceback.format_exc())
        finally:
            self.log_queue.put("__POST_DONE__")

    # ---------- 設定収集（作成用） ----------
    def _gather_cfg(self) -> dict:
        cfg = dict(self.cfg)
        cfg["topic"] = self.topic_text.get("1.0", "end").strip()
        cfg["script_source"] = self.src_var.get()
        cfg["video_mode"] = self.vmode_var.get()
        cfg["script_instruction"] = self.instr_text.get("1.0", "end").strip()
        # チャンネル: 表示中の内容を保存してから展開
        self._save_channel_fields(self.cfg.get("active_channel", 0))
        cfg["channels"] = self.cfg.get("channels", [])
        cfg["active_channel"] = self.cfg.get("active_channel", 0)
        # テロップ: プリセット選択を実キーへ展開
        cfg["telop_preset"] = self.telop_var.get()
        cfg.update(config.TELOP_PRESETS.get(cfg["telop_preset"], {}))
        try:
            cfg["video_length_min"] = int(self.len_var.get().strip())
        except ValueError:
            cfg["video_length_min"] = config.DEFAULTS["video_length_min"]
        for cap in ("script", "tts", "visual", "video", "thumb"):
            cfg[f"{cap}_engine"] = pval(ENG_LABELS[cap], self.engine_vars[cap].get())
        cfg["brushup_engine"] = cfg["script_engine"]
        cfg["upload_engine"] = "none"  # 作成では投稿しない
        # 画像モデル選択（③の「モデル」欄）: 「変更しない」/空=現在のモデルのまま生成
        if hasattr(self, "img_model_cg_var"):
            _cg = (self.img_model_cg_var.get() or "").strip()
            _ef = (self.img_effort_cg_var.get() or "").strip()
            _gm = (self.img_model_gm_var.get() or "").strip()
            cfg["web_image_model_chatgpt"] = "" if _cg in ("", "変更しない") else _cg
            cfg["web_image_effort_chatgpt"] = "" if _ef in ("", "変更しない") else _ef
            cfg["web_image_model_gemini"] = "" if _gm in ("", "変更しない") else _gm
        if hasattr(self, "chapters_var"):
            cfg["desc_chapters"] = bool(self.chapters_var.get())
        if hasattr(self, "post_auto_var"):
            cfg["studio_auto_publish"] = bool(self.post_auto_var.get())
        # 作り直しタブの入力も控える（自動保存の対象＝再起動しても声コード・枚数が残る）
        if hasattr(self, "rm_voice_var"):
            cfg["remake_voice"] = self.rm_voice_var.get().strip()
            cfg["remake_num_clips"] = self.rm_nclip_var.get().strip()
            cfg["remake_num_images"] = self.rm_nimg_var.get().strip()
        if hasattr(self, "st_voice_var"):
            cfg["step_voice"] = self.st_voice_var.get().strip()
        # 声: 表示中の選択をスロットへ反映してから、A/B/ナレ×3系統を保存
        self._flush_voice_slot()
        cfg["tts_voice"] = self._voice_slots["api"].split(" ")[0]
        cfg["gemini_tts_voice"] = self._voice_slots["gemini"].split(" ")[0] or "Kore"
        cfg["edge_voice"] = self._voice_slots["edge"].split(" ")[0]
        cfg["fish_voice"] = self._fish_display_to_id(self._voice_slots.get("fish", ""))
        if hasattr(self, "fish_model_var"):
            cfg["fish_model"] = self.fish_model_var.get()
        cfg["dialog_voices"] = {
            "b": {"api": self._voice_slots_b["api"].split(" ")[0],
                  "gemini": self._voice_slots_b["gemini"].split(" ")[0],
                  "edge": self._voice_slots_b["edge"].split(" ")[0]},
            "n": {"api": self._voice_slots_n["api"].split(" ")[0],
                  "gemini": self._voice_slots_n["gemini"].split(" ")[0],
                  "edge": self._voice_slots_n["edge"].split(" ")[0]},
        }
        cfg["voice_by_gender"] = {
            "auto_enabled": bool(self.gender_auto_var.get()),
            "female": {"api": self._voice_slots_f["api"].split(" ")[0],
                       "gemini": self._voice_slots_f["gemini"].split(" ")[0],
                       "edge": self._voice_slots_f["edge"].split(" ")[0],
                       "fish": self._fish_display_to_id(self._voice_slots_f.get("fish", ""))},
            "male": {"api": self._voice_slots_m["api"].split(" ")[0],
                     "gemini": self._voice_slots_m["gemini"].split(" ")[0],
                     "edge": self._voice_slots_m["edge"].split(" ")[0],
                     "fish": self._fish_display_to_id(self._voice_slots_m.get("fish", ""))},
        }
        cfg["script_format"] = "narration"  # 会話形式は機能廃止（一人語り固定）
        cfg["video_orientation"] = self.orient_var.get()
        if cfg["video_orientation"] == "portrait":   # 縦ショート
            cfg["video_width"], cfg["video_height"] = 1080, 1920
        else:
            cfg["video_width"], cfg["video_height"] = 1920, 1080
        cfg["output_dir"] = self.out_var.get().strip() or config.DEFAULTS["output_dir"]
        for k, t in self.prompt_texts.items():
            cfg[k] = t.get("1.0", "end").strip()
        for k, v in self.channel_vars.items():
            cfg[k] = v.get().strip()
        try:  # memoへのチャンネル刻印（誤爆投稿防止・夜間レポート用）
            cfg["_channel_name"] = cfg["channels"][cfg["active_channel"]].get("name", "")
        except Exception:
            cfg["_channel_name"] = ""
        for k, v in self.key_vars.items():
            cfg[k] = v.get().strip()
        cfg["google_tts_credentials_json"] = self.gtts_var.get().strip()
        cfg["youtube_client_secret"] = self.cs_var.get().strip()
        cfg["youtube_token"] = self.tok_var.get().strip()
        # 全体設定URLは専用キーに保存（ch別URLと同一キーを共有すると相互汚染する）
        cfg["youtube_channel_url_global"] = self.post_channel_var.get().strip()
        if not cfg.get("youtube_channel_url"):  # ch別URLが空なら投稿タブの全体設定
            cfg["youtube_channel_url"] = cfg["youtube_channel_url_global"]
        cfg["chrome_profile"] = self.profile_var.get().strip() or config.DEFAULTS["chrome_profile"]
        cfg["browser_exe"] = self._browser_exe_value()
        try:
            cfg["cdp_port"] = int(self.port_var.get().strip())
        except ValueError:
            cfg["cdp_port"] = 9222
        cfg["align_method"] = pval(ALIGN_PAIRS, self.align_var.get())
        cfg["whisper_model"] = self.wmodel_var.get()
        try:
            cfg["num_images"] = max(1, int(self.nimg_var.get().strip()))
        except ValueError:
            cfg["num_images"] = 5
        try:
            cfg["num_video_clips"] = int(self.nclip_var.get().strip())
        except ValueError:
            cfg["num_video_clips"] = 3
        cfg["debug_dump"] = self.debug_var.get()
        cfg["default_schedule_slots"] = [s.strip() for s in self.slots_var.get().split(",") if s.strip()]
        try:
            cfg["default_publish_offset_days"] = int(self.offset_var.get().strip())
        except ValueError:
            cfg["default_publish_offset_days"] = 1
        cfg["bgm_library"] = self.bgm_lib
        cfg["bgm_mode"] = pval(BGM_PAIRS, self.bgm_mode_var.get())
        try:
            cfg["bgm_base_db"] = float(self.bgm_db_var.get())
        except ValueError:
            cfg["bgm_base_db"] = -8
        try:
            cfg["neko_bgm_db"] = float(self.neko_bgm_db_var.get())
        except ValueError:
            cfg["neko_bgm_db"] = -14
        sel = self.bgm_list.curselection()
        if sel and sel[0] < len(self.bgm_lib):
            cfg["bgm_selected"] = self.bgm_lib[sel[0]].get("path", "")
        elif getattr(self, "_bgm_sel_path", "") and                 self._bgm_sel_path in {e.get("path") for e in self.bgm_lib}:
            cfg["bgm_selected"] = self._bgm_sel_path   # 選択が外れていてもクリック時の控えを使う
        return cfg

    def _on_bgm_select(self, _e=None):
        sel = self.bgm_list.curselection()
        if not (sel and sel[0] < len(self.bgm_lib)):
            return
        it = self.bgm_lib[sel[0]]
        self._bgm_sel_path = it.get("path", "")
        note = ""
        try:
            if pval(BGM_PAIRS, self.bgm_mode_var.get()) != "select":
                note = "　※この曲を使うには「BGMの使い方」を「選んだ曲を使う」にしてください"
        except Exception:
            pass
        self.bgm_sel_lab.config(text=f"🎵 選んだ曲: {it.get('title') or Path(it.get('path', '')).stem}{note}")

    def _bgm_restore_selection(self):
        """起動時、保存済みの「選んだ曲」を一覧上でも選択表示する（どれが選ばれているか見える）。"""
        try:
            sel = (self.cfg.get("bgm_selected") or "").strip()
            if not sel:
                return
            for i, it in enumerate(self.bgm_lib):
                if it.get("path") == sel:
                    self.bgm_list.selection_clear(0, "end")
                    self.bgm_list.selection_set(i)
                    self.bgm_list.see(i)
                    self._on_bgm_select()
                    break
        except Exception:
            pass

    def _save(self):
        self.cfg = self._gather_cfg()
        config.save_settings(self.cfg, force=True)   # 💾＝明示の保存（外部更新があっても書く）
        self._autosave_snap = self._cfg_fingerprint(self.cfg)  # 直後の自動保存の二重書き防止
        self._ext_warned = False
        self._log("設定を保存しました。")

    def _on_save_skipped(self):
        """別プロセスが settings.json を更新していたため、この窓からの保存を見送った時のログ（1回だけ）。"""
        if getattr(self, "_ext_warned", False):
            return
        self._ext_warned = True
        try:
            self._log("⚠ 別のIZANAMIウィンドウ（または別の処理）が settings.json を更新したため、"
                      "この窓からの保存を見送りました。二重起動している場合は片方を閉じてください"
                      "（この窓の変更を残したい時は 💾 を押してください）。")
        except Exception:
            pass

    # ---------- 自動保存 ----------
    _AUTOSAVE_MS = 60000   # 60秒ごとに変更をチェック（変更が無ければ何も書かない）

    @staticmethod
    def _cfg_fingerprint(cfg: dict) -> str:
        import json as _j
        try:
            return _j.dumps({k: cfg.get(k) for k in config.DEFAULTS}, ensure_ascii=False,
                            sort_keys=True, default=str)
        except Exception:
            return ""

    def _autosave(self):
        """画面の設定を自動保存する（変更がある時だけ・失敗しても本体を止めない）。

        💾の押し忘れ・クラッシュ・強制終了で「入力した声コードやプロンプトが消える」
        問題への対策（2026-08-06ユーザー要望）。
        ※設定ファイル読込中(_importing)は絶対に走らせない（読込直後に画面の旧値で
        上書きして読込が消える競合＝2026-08-03敵対検証で塞いだ穴を開け直さないため）。"""
        if getattr(self, "_importing", False):
            return
        try:
            cfg = self._gather_cfg()
        except Exception:
            return   # UI構築途中などは何もしない
        fp = self._cfg_fingerprint(cfg)
        if not fp:
            return
        if self._autosave_snap is None:      # 初回は基準を取りつつ現状態を保存
            pass
        elif fp == self._autosave_snap:      # 変更なし＝ディスクに触らない
            return
        try:
            # 二重起動した別の窓（や別プロセス）が保存した後に、この窓の古い画面値で
            # 上書きすると相手の設定（OP/ED等）が巻き戻る（2026-08-20配布先報告の有力機構）
            # → config.save_settings 側のガードが見送る（ログは _on_save_skipped）
            if not config.save_settings(cfg):
                return
            self.cfg = cfg
            first = self._autosave_snap is None
            self._autosave_snap = fp
            self._ext_warned = False
            if not first:
                self._log("💾 設定を自動保存しました。")
        except Exception:
            pass

    def _autosave_tick(self):
        try:
            self._autosave()
        finally:
            try:
                self.root.after(self._AUTOSAVE_MS, self._autosave_tick)
            except Exception:
                pass

    # ---------- 作成実行 ----------
    @staticmethod
    def _fish_voice_missing(cfg) -> bool:
        """Fish選択なのに声ID(reference_id)が空＝1行ごとに声が変わる状態か
        （2026-08-17配布先報告: 5本全部で声バラバラ・中国語読み混入）。
        猫ミームは②音声を使わない（無音＋文字数タイミング）ため対象外＝誤ブロックしない。"""
        return (cfg.get("tts_engine") == "fish"
                and cfg.get("video_mode") != "neko"
                and not (cfg.get("fish_voice") or "").strip())

    def _warn_fish_voice(self) -> bool:
        """②の課金前に止めて案内する。True=続行不可。"""
        messagebox.showwarning(
            "IZANAMI",
            "音声エンジンがFishですが「声ID(reference_id)」が未設定です。\n"
            "空のままだと1行ごとに違う声になり（外国語読みが混ざることもあります）、\n"
            "使いものにならない音声ができてしまいます。\n\n"
            "作成タブの声欄の「＋声を登録」で自分の声IDを登録するか、\n"
            "エンジンをEdge等へ変更してから開始してください。")
        return True

    def _start(self, resume_dir: str = ""):
        if self.worker and self.worker.is_alive():
            return
        # 保険: 別タブを開いたまま「全自動の新規作成」が始まる事故を防ぐ
        # （作り直したい動画とは別物が1本できてしまい、時間もAPI課金も無駄になる）
        if not resume_dir:
            try:
                cur = self.nb.select()
                if cur == str(self.tab_remake):
                    self._rm_start(); return
                if cur == str(self.tab_step):
                    self._st_next(); return
            except Exception:
                pass
        cfg = self._gather_cfg()
        topics = [l.strip() for l in cfg.get("topic", "").splitlines() if l.strip()]
        if not resume_dir and not topics and cfg["script_engine"] != "mock":
            messagebox.showwarning("IZANAMI", "まず「動画のお題」を入力してください。")
            return
        # ①の課金前に止める（声バラバラ事故の根絶）。▶続きから（resume）は②が済んで
        # いる場合もあるためGUIでは止めず、②が走るならstage_tts側のゲートが止める
        if not resume_dir and self._fish_voice_missing(cfg):
            self._warn_fish_voice()
            return
        self.cfg = cfg
        config.save_settings(cfg)
        self._stop = False
        self.start_btn.config(state="disabled"); self.stop_btn.config(state="normal")
        self._clear_log()
        self._timer_begin()
        if not resume_dir and len(topics) > 1:  # 量産モード: 1行=1本で連続生成
            self._set_status(f"量産中 0/{len(topics)}")
            self.worker = threading.Thread(target=self._run_batch_worker,
                                           args=(cfg, topics), daemon=True)
        else:
            one = dict(cfg)
            one["topic"] = topics[0] if topics else ""
            self._set_status("作成中…")
            self.worker = threading.Thread(target=self._run_worker,
                                           args=(one, resume_dir), daemon=True)
        self.worker.start()
        try:
            self.nb.select(self.tab_log)  # 実行中はログタブへ自動移動
        except Exception:
            pass

    def _resume(self):
        d = filedialog.askdirectory(title="続きから作る動画フォルダ(IZANAMI_...)を選ぶ",
                                    initialdir=self.out_var.get() or str(Path.home()))
        if d:
            # 保存先フォルダ自体などを選ぶと、その直下に成果物を作り始めてしまう
            if not project.looks_like_project(d):
                messagebox.showwarning(
                    "IZANAMI",
                    "IZANAMIの動画フォルダではないようです。\n"
                    "「IZANAMI_日付_時刻」（旧IZANAGI_も可）という名前のフォルダを選んでください。\n"
                    "（保存先フォルダそのものを選ぶと、その中に散らかってしまいます）")
                return
            self._start(resume_dir=d)

    def _stop_run(self):
        self._stop = True
        self._set_status("中断待ち…")
        self._log("中断します。今の処理が終わり次第とまります。")

    def _run_worker(self, cfg, resume_dir):
        util.prevent_sleep(True)  # 作成中はPCを寝かせない
        self._log_session_header("続きから作成" if resume_dir else "動画を作成")
        try:
            out = pipeline.run(cfg, log=self._log, stop_flag=lambda: self._stop,
                               resume_dir=resume_dir or None)
            self._last_out = out
            if (Path(out) / "compose" / "final.mp4").exists():
                self._log(f"\n🎬 完成しました。保存先: {out}")
                self._log("→ 「投稿」タブに読み込みました。内容を確認して 🚀 予約投稿できます。")
        except Exception:
            self._log("【エラー】\n" + traceback.format_exc())
        finally:
            util.prevent_sleep(False)
            self.log_queue.put("__DONE__")

    def _run_batch_worker(self, cfg, topics):
        """量産モード: 1行=1本（アクティブチャンネルで作る）。"""
        self._run_plan_worker(cfg, [(None, t) for t in topics])

    def _run_plan_worker(self, cfg, plan):
        """量産の一般形: plan=[(chインデックス|None, お題)]。

        自己回復一式: 1本失敗→60秒後にセカンドパス（続きから再走）／本間クールダウン／
        連続失敗ブレーカ／結果はbatch_report_*.jsonへ永続化（翌朝の状況把握用）。"""
        import time as _time
        from izanagi import topics as topics_mod
        from izanagi.project import STAGES as _STAGES
        from izanagi.project import Project as _Proj
        results = []
        if self._fish_voice_missing(cfg):
            # 夜間・無人でダイアログは出せない → ログで理由を明示して開始前に中止
            self._log("⚠ 音声エンジンがFishですが声ID(reference_id)が未設定です。"
                      "1行ごとに声が変わる音声になるため、量産を開始しませんでした。"
                      "作成タブの声欄で声IDを登録するか、エンジンを変更してください。")
            self.log_queue.put("__DONE__")
            return
        cooldown = int(cfg.get("batch_cooldown_sec", 45))
        breaker = int(cfg.get("batch_max_consecutive_fails", 3))
        fail_streak = 0
        out_dir = cfg.get("output_dir") or config.DEFAULTS["output_dir"]
        util.prevent_sleep(True)  # 量産の完走中はPCを寝かせない（「寝るだけ」の生命線）
        try:
            for i, (ch_idx, t) in enumerate(plan, 1):
                if self._stop:
                    self._log(f"\n⏹ 中断しました（{i - 1}/{len(plan)}本まで実行）。")
                    break
                c = dict(cfg)
                ch_name = ""
                if ch_idx is not None:  # チャンネル別設定を丸ごと適用
                    try:
                        chd = self.cfg["channels"][ch_idx]
                        for k in config.CHANNEL_KEYS:
                            v = chd.get(k, "")
                            if k == "youtube_channel_url" and not v:
                                # 空でグローバルURLを潰さない（config._init_channelsと同じ規則。
                                # 潰すとmemo刻印が空になり誤爆防止照合が無効化される）
                                continue
                            c[k] = v
                        ch_name = chd.get("name", "")
                        c["_channel_name"] = ch_name
                        c["active_channel"] = ch_idx
                    except Exception:
                        pass
                c["topic"] = t
                self.log_queue.put(f"__STATUS__量産中 {i}/{len(plan)}")
                self._log(f"\n════════ 量産 {i}/{len(plan)} 本目"
                          + (f"（ch: {ch_name}）" if ch_name else "") + " ════════")
                self._log(f"お題: {t[:60]}")
                t0 = _time.time()
                out = ""
                ok = False
                try:
                    out = pipeline.run(c, log=self._log, stop_flag=lambda: self._stop)
                    ok = (Path(out) / "compose" / "final.mp4").exists()
                except Exception:
                    self._log("【エラー】\n" + traceback.format_exc())
                if not ok and out and cfg.get("batch_second_pass", True) and not self._stop:
                    self._log("    ♻ セカンドパス: 60秒おいて、この1本だけ続きから再挑戦します…")
                    for _ in range(60):
                        if self._stop:
                            break
                        _time.sleep(1)
                    if not self._stop:
                        try:
                            out = pipeline.run(c, log=self._log, stop_flag=lambda: self._stop,
                                               resume_dir=out)
                            ok = (Path(out) / "compose" / "final.mp4").exists()
                            if ok:
                                self._log("    ✅ セカンドパスで復活しました。")
                        except Exception:
                            self._log("【エラー】\n" + traceback.format_exc())
                failed_stage = ""
                if out and not ok:
                    pr = _Proj(Path(out))
                    failed_stage = next((s for s in _STAGES if not pr.done(s)), "")
                if ok:
                    fail_streak = 0
                    self._last_out = out
                    hist_idx = ch_idx if ch_idx is not None else self.cfg.get("active_channel", 0)
                    try:
                        topics_mod.add_history(hist_idx, [t])
                    except Exception:
                        pass
                    if ch_idx is not None:  # 完成した分だけキューから除去（失敗は翌晩リトライ）
                        try:
                            q = self.cfg["channels"][ch_idx].get("topic_queue") or []
                            self.cfg["channels"][ch_idx]["topic_queue"] = [x for x in q if x != t]
                            config.save_settings(self.cfg)
                            # GUIのキュー一覧にも反映（古い表示からの「消化済みお題の復活」防止）
                            self.root.after(0, self._refresh_queue_view, ch_idx)
                        except Exception:
                            pass
                else:
                    fail_streak += 1
                results.append({"topic": t, "channel": ch_name, "out": out, "ok": ok,
                                "failed_stage": failed_stage, "sec": int(_time.time() - t0)})
                if fail_streak >= breaker:
                    self._log(f"\n⛔ {breaker}本連続で失敗したため残りを中止します"
                              "（quota切れ/ログイン切れの可能性。朝に原因を直して再実行を）。")
                    break
                if i < len(plan) and cooldown > 0 and not self._stop:
                    self._log(f"    …次の1本まで{cooldown}秒休憩（レート制限対策）")
                    for _ in range(cooldown):
                        if self._stop:
                            break
                        _time.sleep(1)
            done = sum(1 for r in results if r["ok"])
            self._log(f"\n🌙 量産おわり: 完成 {done}本 / 失敗 {len(results) - done}本")
            for r in results:
                name = Path(r["out"]).name if r["out"] else "(未作成)"
                extra = f" [×{r['failed_stage']}]" if r["failed_stage"] else ""
                self._log(f"  {'✅' if r['ok'] else '⚠'} {name}  {r['topic'][:40]}{extra}")
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            rp = Path(out_dir) / f"batch_report_{stamp}.json"
            util.save_json_atomic(rp, {"when": stamp, "items": results})
            self._log(f"📋 実行レポート: {rp.name}")
            if done:
                self._log("→ 「投稿」タブの一覧に✓を入れて、まとめて予約投稿できます。")
        except Exception:
            self._log("【エラー】\n" + traceback.format_exc())
        finally:
            util.prevent_sleep(False)
            self.log_queue.put("__DONE__")

    # ---------- ログ / 進捗ピル ----------
    _PILL_MAP = {"①台本生成": "script", "②ナレーション音声": "tts",
                 "③映像・画像生成": "visuals", "④合成（テロップ/BGM）": "compose",
                 "⑤プロジェクト保存・サムネ": "project"}

    # 全体進捗のステージ配分（%）: 体感の重さに合わせた割り振り
    _STAGE_RANGE = {"script": (2, 15), "tts": (15, 40), "visuals": (40, 72),
                    "compose": (72, 96), "project": (96, 99)}
    # ログから拾う「n/m」進行パターン（行・画像・チャンク・区間・セグメント）
    _FRAC_RE = re.compile(r"(?:…|画像\s*|クリップ\s*|区間|セグメント\s*)(\d+)\s*/\s*(\d+)")

    def _reset_pills(self):
        for key, (l, lab) in self.pills.items():
            l.config(text=f"○ {lab}", foreground="#9aa8cc")
        self._cur_stage = None
        self._batch_prefix = ""

    def _show_progress(self, pct: int, note: str = ""):
        pct = max(0, min(100, int(pct)))
        prefix = getattr(self, "_batch_prefix", "")
        text = (f"{prefix}・" if prefix else "") + f"作成中 {pct}%"
        if note:
            text += f"（{note}）"
        self._set_status(text)

    def _update_progress(self, msg: str):
        """ログ行から全体進捗%を推定してサイドバーに出す（動いてる感）。"""
        s = msg.strip()
        stage = getattr(self, "_cur_stage", None)
        if s.startswith("▶") or s.startswith("✅") or s.startswith("♻"):
            for label_text, key in self._PILL_MAP.items():
                if label_text in s:
                    lo, hi = self._STAGE_RANGE[key]
                    if s.startswith("▶"):
                        self._cur_stage = key
                        self._show_progress(lo, label_text[:1] + label_text[1:3])
                    else:  # ✅完了 / ♻スキップ
                        self._cur_stage = key
                        self._show_progress(hi)
                    return
        if not stage:
            return
        m = self._FRAC_RE.search(s)
        if m:
            n, total = int(m.group(1)), int(m.group(2))
            if total > 0 and n <= total:
                lo, hi = self._STAGE_RANGE[stage]
                self._show_progress(lo + (hi - lo) * n / total)

    def _update_pills(self, msg: str):
        s = msg.strip()
        for label_text, key in self._PILL_MAP.items():
            if label_text in s:
                l, lab = self.pills[key]
                if s.startswith("▶"):
                    l.config(text=f"▶ {lab}", foreground="#7db4ff")
                elif s.startswith("✅") or "スキップ" in s:
                    l.config(text=f"✓ {lab}", foreground="#5fd287")
                elif "で停止" in s:
                    l.config(text=f"⚠ {lab}", foreground=WARN)
                break

    def _log(self, msg: str):
        self.log_queue.put(str(msg))

    # ── ログのファイル保存（サポート用。画面をクリアしても残る） ──
    LOG_KEEP_DAYS = 14        # これより古い日別ログは自動で消す（溜め込みすぎ防止）
    LOG_MAX_MB = 20           # 1日分がこのサイズを超えたら新しいファイルに切り替える

    def _log_dir(self) -> Path:
        d = ROOT / "logs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _log_file(self) -> Path:
        """今日の日別ログ。大きくなりすぎたら連番で分割する。"""
        day = datetime.datetime.now().strftime("%Y%m%d")
        p = self._log_dir() / f"izanagi_{day}.log"
        n = 1
        while p.exists() and p.stat().st_size > self.LOG_MAX_MB * 1048576:
            p = self._log_dir() / f"izanagi_{day}_{n}.log"
            n += 1
        return p

    def _log_to_file(self, msg: str) -> None:
        """1行を時刻つきで追記（失敗しても本処理は止めない）。"""
        try:
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            with open(self._log_file(), "a", encoding="utf-8", errors="replace") as f:
                f.write(f"[{ts}] {msg}\n")
        except Exception:
            pass

    def _log_session_header(self, title: str) -> None:
        """実行の区切り＋環境情報をログ先頭に入れる（サポート時の切り分け用）。"""
        try:
            import platform
            c = self.cfg
            ch = (c.get("channels") or [{}])[c.get("active_channel", 0) or 0]
            self._log_to_file("=" * 70)
            self._log_to_file(f"■ {title}  {datetime.datetime.now():%Y-%m-%d %H:%M:%S}")
            _core = getattr(izanagi, "CORE_VERSION", "?")
            self._log_to_file(
                f"  IZANAMI v{APP_VERSION}"
                + (f" / 中身 v{_core}（⚠不一致＝更新が未完了）" if _core != APP_VERSION else "")
                + f" / Windows {platform.release()} / Python {platform.python_version()}")
            self._log_to_file(f"  チャンネル: {ch.get('name', '(既定)')}")
            self._log_to_file("  エンジン: " + " / ".join(
                f"{k}={c.get(f'{k}_engine', '-')}" for k in ("script", "tts", "visual", "video", "thumb")))
            self._log_to_file("=" * 70)
        except Exception:
            pass

    def _open_log_folder(self):
        try:
            os.startfile(str(self._log_dir()))
        except Exception as e:
            messagebox.showerror("IZANAMI", f"ログフォルダを開けませんでした: {e}")

    def _cleanup_old_logs(self) -> None:
        """古い日別ログを消す（起動時に1回）。"""
        try:
            limit = datetime.datetime.now().timestamp() - self.LOG_KEEP_DAYS * 86400
            for f in self._log_dir().glob("izanagi_*.log"):
                if f.stat().st_mtime < limit:
                    f.unlink()
        except Exception:
            pass

    def _export_support_log(self):
        """サポート送付用に、直近のログ＋設定（キーは除く）を1つのzipにまとめる。"""
        import zipfile
        try:
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            dst = Path.home() / "Desktop" / f"IZANAMIログ_{stamp}.zip"
            if not dst.parent.exists():          # Desktopが別ドライブ等で無い場合はツール直下へ
                dst = ROOT / dst.name
            logs = sorted(self._log_dir().glob("izanagi_*.log"),
                          key=lambda f: f.stat().st_mtime, reverse=True)[:5]
            with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
                for f in logs:
                    z.write(f, f"logs/{f.name}")
                # 画面に出ている分（未保存の行が残っていても拾えるよう）も同梱
                try:
                    z.writestr("画面のログ.txt", self.log_text.get("1.0", "end"))
                except Exception:
                    pass
                z.writestr("設定（キーは除く）.json", self._safe_settings_json())
                z.writestr("環境.txt", self._env_report())
            self._log(f"📦 サポート用のログをまとめました: {dst}")
            messagebox.showinfo(
                "ログをまとめました",
                f"デスクトップに保存しました。\n\n{dst.name}\n\n"
                "このファイルを開発者に送ってください。\n"
                "※ APIキー・パスワードは入れていません。")
            try:
                os.startfile(str(dst.parent))
            except Exception:
                pass
        except Exception as e:
            messagebox.showerror("IZANAMI", f"ログをまとめられませんでした: {e}")

    _SECRET_HINTS = ("key", "token", "secret", "password", "cookie", "credential", "client_id")

    def _safe_settings_json(self) -> str:
        """設定のうち、キー・パスワード類を伏せたものを返す（サポートに安全に渡すため）。"""
        import json

        def scrub(o):
            if isinstance(o, dict):
                out = {}
                for k, v in o.items():
                    if any(h in str(k).lower() for h in self._SECRET_HINTS):
                        out[k] = f"(設定あり:{len(str(v))}文字)" if v else "(未設定)"
                    else:
                        out[k] = scrub(v)
                return out
            if isinstance(o, list):
                return [scrub(x) for x in o]
            return o
        try:
            return json.dumps(scrub(dict(self.cfg)), ensure_ascii=False, indent=1)
        except Exception as e:
            return f"(設定を書き出せませんでした: {e})"

    def _env_report(self) -> str:
        """環境情報（サポートの切り分け用）。"""
        import platform
        _core = getattr(izanagi, "CORE_VERSION", "?")
        L = [f"IZANAMI v{APP_VERSION}"
             + (f"（⚠中身 v{_core}＝更新が未完了）" if _core != APP_VERSION else ""),
             f"日時: {datetime.datetime.now():%Y-%m-%d %H:%M:%S}",
             f"OS: {platform.platform()}",
             f"Python: {platform.python_version()}",
             f"ツールの場所: {ROOT}",
             f"保存先: {self.cfg.get('output_dir', '')}",
             f"ffmpeg: {util.find_ffmpeg() or '(見つかりません)'}"]
        try:
            _oroot = Path(self.cfg.get("output_dir") or (ROOT / "output"))
            outs = sorted([*_oroot.glob("IZANAMI_*"), *_oroot.glob("IZANAGI_*")],
                          key=lambda d: d.stat().st_mtime, reverse=True)[:5]
            L.append("最近作った動画:")
            for d in outs:
                pr = project.Project.open(d)
                done = [s for s in project.STAGES if pr.done(s)]
                L.append(f"  {d.name}  完了 {len(done)}/{len(project.STAGES)}: {','.join(done)}")
        except Exception:
            pass
        return "\n".join(L)

    def _clear_log(self):
        self.log_text.config(state="normal"); self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")
        self._reset_pills()

    def _poll_log(self):
        # 設定ファイル読み込みの確定後は何も処理しない（背景処理の完了ハンドラが
        # config.save_settings(self.cfg)＝旧設定の保存を行い、読み込んだばかりの
        # settings.json を上書きしてしまう。直後に再起動するので取りこぼしは無害）
        if getattr(self, "_importing", False):
            return
        try:
            while True:
                msg = self.log_queue.get_nowait()
                if msg == "__STEP_DONE__":
                    # ステップ実行: 1工程おわり（ボタンを戻し、進み具合と台本を更新）
                    self._timer_end()   # ここを通らないとタイマーが回り続ける（敵対検証2026-08-19）
                    self.start_btn.config(state="normal"); self.stop_btn.config(state="disabled")
                    if hasattr(self, "st_next_btn"):
                        self.st_next_btn.config(state="normal")
                    self._set_status("待機中")
                    try:
                        self._st_refresh()
                        if self.st_dir and (Path(self.st_dir) / "script" / "script.txt").exists():
                            self._st_load_script()
                        self.nb.select(self.tab_step)   # 確認できるようタブを戻す
                    except Exception:
                        pass
                    try:  # 完成していれば投稿タブへも反映（案内どおりにする）
                        self._post_refresh_list()
                        if self.st_dir and (Path(self.st_dir) / "memo.json").exists():
                            self.post_folder_var.set(self.st_dir)
                            self._post_load()
                    except Exception:
                        pass
                    continue
                if msg == "__DONE__":
                    self._timer_end()
                    self.start_btn.config(state="normal"); self.stop_btn.config(state="disabled")
                    if hasattr(self, "st_next_btn"):   # ステップ実行の「BGMだけ入れ替える」経由
                        self.st_next_btn.config(state="normal")
                        try:
                            self._st_refresh()
                        except Exception:
                            pass
                    if hasattr(self, "rm_start_btn"):
                        self.rm_start_btn.config(state="normal")
                        try:
                            self._rm_reload_list()   # 作り直しでできた新フォルダを一覧へ
                        except Exception:
                            pass
                    self._set_status("待機中")
                    self._reload_bgm_lib()  # 自動BGMで増えた曲をリストへ反映
                    self._refresh_bgm_combos()
                    try:
                        self._post_refresh_list()  # できた動画を投稿タブの一覧へ反映
                    except Exception:
                        pass
                    # 完成していたら投稿タブへ自動読み込み（次の操作を1クリック減らす）
                    out = getattr(self, "_last_out", "")
                    if out and (Path(out) / "memo.json").exists():
                        self.post_folder_var.set(out)
                        try:
                            self._post_load()
                        except Exception:
                            pass
                    continue
                if msg.startswith("__STATUS__"):
                    self._batch_prefix = msg[10:]  # 例「量産中 2/6」（%はログ行から追記）
                    self._set_status(msg[10:])
                    continue
                if msg.startswith("__BGM_ADD__"):
                    body = msg[11:]
                    genre, _, path = body.partition("\t")
                    if not path:
                        genre, path = "", body
                    item = {"path": path, "title": Path(path).stem, "mood": genre, "gain_db": -6}
                    self.bgm_lib.append(item)
                    self.bgm_list.insert("end", _bgm_row_text(item))
                    self.cfg["bgm_library"] = self.bgm_lib
                    continue
                if msg == "__SUNO_DONE__":
                    try:
                        self.suno_btn.config(state="normal", text="🎵 作成")
                    except Exception:
                        pass
                    continue
                if msg == "__BGMJOIN_DONE__":
                    self._bgm_joining = False
                    try:
                        self.bgm_join_btn.config(state="normal", text="🔗 選んだ曲をつなげて1本に")
                        config.save_settings(self.cfg)  # 増えた曲をsettingsへ永続化
                    except Exception:
                        pass
                    continue
                if isinstance(msg, str) and msg.startswith("__BROWSER_IMPORT_OK__"):
                    src_disp = msg[len("__BROWSER_IMPORT_OK__"):]
                    self.cfg["browser_profile_source"] = src_disp
                    try:
                        config.save_settings(self.cfg)
                        self.import_src_lab.config(text=f"取込中: {src_disp}")
                    except Exception:
                        pass
                    continue
                if msg == "__BROWSER_IMPORT_DONE__":
                    try:
                        self.import_btn.config(state="normal", text="🔁 普段のChromeからログインを取り込む")
                    except Exception:
                        pass
                    continue
                if isinstance(msg, str) and msg.startswith("__THUMBCOPY__"):
                    self._thumb_busy = False
                    try:
                        self.thumb_suggest_btn.config(state="normal")
                    except Exception:
                        pass
                    cands = [c for c in msg[len("__THUMBCOPY__"):].split("\t") if c]
                    if cands:
                        self.thumb_copy_combo.config(values=cands)
                        self.post_thumbcopy_var.set(cands[0])
                        self.post_thumbtext_var.set(cands[0])  # 先頭案を自動で反映
                        self.thumb_regen_status.config(
                            foreground=GOLD,
                            text=f"✨ {len(cands)}案できました（候補▼で選び直せます→下のボタンで反映）")
                    else:
                        self.thumb_regen_status.config(
                            foreground="#ff8f8a",
                            text="⚠ 文字案を作れませんでした（ログ参照。エンジン/キー/ログインを確認）")
                    continue
                if isinstance(msg, str) and msg.startswith("__THUMB_DONE__"):
                    self._thumb_busy = False
                    try:
                        self.thumb_regen_btn.config(state="normal")
                        self.thumb_text_btn.config(state="normal")
                    except Exception:
                        pass
                    ok = msg[len("__THUMB_DONE__"):len("__THUMB_DONE__") + 1] == "1"
                    payload = msg.split("\t", 1)[-1]
                    if ok:
                        self.thumb_regen_status.config(text="✅ サムネを更新しました")
                        self._show_thumb(payload)  # payload=更新後のサムネpath
                    else:
                        self.thumb_regen_status.config(text="⚠ " + payload[:120])
                    continue
                if msg == "__POST_DONE__":
                    self.post_btn.config(state="normal")
                    self._batch_prefix = ""
                    self._set_status("待機中")  # 中断時に「中断待ち…」のまま残さない
                    try:
                        self.stop_btn.config(state="disabled")
                        self.post_batch_btn.config(state="normal")
                        self._post_refresh_list()
                    except Exception:
                        pass
                    try:  # 編集欄に動画を読み込んでいる時だけ再読込（未読込で警告を出さない）
                        pf = self.post_folder_var.get().strip()
                        if pf and (Path(pf) / "memo.json").exists():
                            self._post_load()
                    except Exception:
                        pass
                    continue
                self._update_pills(msg)
                try:
                    self._update_progress(msg)
                except Exception:
                    pass
                self._log_to_file(msg)   # サポート用に必ずファイルへ残す（お客さんに操作を頼らない）
                self.log_text.config(state="normal")
                self.log_text.insert("end", msg + "\n"); self.log_text.see("end")
                self.log_text.config(state="disabled")
        except queue.Empty:
            pass
        self.root.after(150, self._poll_log)


def main():
    global FB, FBM
    try:  # 高DPIでの文字のにじみ防止
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    try:
        import tkinter.font as tkfont
        fams = set(tkfont.families(root))
        if "BIZ UDPゴシック" in fams or "BIZ UDPGothic" in fams:
            FB = "BIZ UDPGothic"   # ユニバーサルデザインフォント優先
        if "BIZ UDゴシック" in fams or "BIZ UDGothic" in fams:
            FBM = "BIZ UDGothic"
        dpi = root.winfo_fpixels("1i")
        root.tk.call("tk", "scaling", dpi / 72.0)
    except Exception:
        pass
    try:
        sv_ttk.set_theme("dark")
    except Exception:
        pass
    try:  # 名前付きフォントを一括で大きく（未指定ウィジェット全部に効く）
        import tkinter.font as tkfont
        for n in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont"):
            tkfont.nametofont(n).configure(family=FB, size=11)
    except Exception:
        pass
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", app._on_close)  # ×で閉じても設定を保存
    try:
        # 隠し起動(_run_hidden.vbs)の「ウィンドウ非表示」指定をTkが継承して
        # 最小化のまま画面に出ないことがある → 必ず通常表示で前面に出す
        root.deiconify()
        root.state("normal")
        root.attributes("-topmost", True)
        root.lift()
        root.focus_force()
        root.after(500, lambda: root.attributes("-topmost", False))
    except Exception:
        pass
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        (ROOT / "error.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise
