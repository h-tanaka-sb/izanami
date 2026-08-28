# -*- coding: utf-8 -*-
"""起動時認証ゲート（move認証システム・auth_client.py 使用）。

app.py の main() 冒頭で run_gate() を呼ぶ。認証OKの時だけ応答dictを返し、
expired / busy / denied / 通信失敗 / エラー / 画面を閉じた場合は None
＝本体を起動しない（フェイルクローズ）。

開発時のみ: ROOT に「.dev_skip_auth」ファイルがあれば認証を省略する。
このマーカーは配布物に絶対に含めない（配布ビルドの監査で不在をチェック）。
frozen(exe) ではマーカーがあっても必ず認証を通す。
"""
from __future__ import annotations

import hashlib
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).parent
_DEV_MARKER = ROOT / ".dev_skip_auth"

# 開発バイパスは「この開発ワークスペースの実パスから起動した時」だけ有効にする。
# 配布物は必ず別フォルダに置かれる＝下のハッシュに一致しない＝マーカーを作っても無効。
# （このアプリは exe 化せず素の .py で配布されるため sys.frozen は常に False。
#   frozen 判定だけでは配布版のバイパスを塞げないので、実行パス束縛を主ガードにする。）
# 実パスは平文で晒さずハッシュで照合する（値は開発機の _here_hash() から確定）。
_DEV_ROOT_HASH = "c59b6103262bdd8aa30ce1258c9417e58ee600bd3257a680cef5294c764b110c"


def _here_hash() -> str:
    return hashlib.sha256(
        os.path.normcase(os.path.abspath(str(ROOT))).encode("utf-8")).hexdigest()


def dev_skip_allowed() -> bool:
    """開発機の開発ワークスペースからの起動時だけ認証を省略できるか。

    配布版では（1）frozen でなくても（2）実行パスが開発ワークスペースと一致しないため
    False。空マーカーを置くだけの認証回避は成立しない。"""
    if getattr(sys, "frozen", False):
        return False
    try:
        if _here_hash() != _DEV_ROOT_HASH:
            return False
        return _DEV_MARKER.exists()
    except Exception:
        return False


def decide(res: dict | None) -> tuple[str, str]:
    """認証応答 → (動作, 表示文)。動作: "ok"=起動 / "exit"=表示して終了 /
    "stay"=ログイン画面に留まる（再入力可）。未知のresultもフェイルクローズ。"""
    r = (res or {}).get("result", "")
    if r == "ok":
        return "ok", ""
    if r == "expired":
        return "exit", "有効期限切れです。"
    if r == "busy":
        return "exit", "別の端末で起動中です。先に他の端末を閉じてください。"
    if r == "denied":
        return "stay", "起動できません。IDまたはパスワードをご確認ください。"
    return "stay", f"認証エラーが発生しました（{r or '不明な応答'}）。"


def run_gate() -> dict | None:
    """ログイン画面を表示して認証する。OKなら応答dict、それ以外は None。"""
    if dev_skip_allowed():
        print("[auth] 開発マーカーにより認証を省略（配布版では必ず認証されます）")
        return {"result": "ok", "session": "", "dev_skip": True}

    import tkinter as tk
    from tkinter import messagebox, ttk

    import auth_client

    state: dict = {"res": None}
    busy = {"v": False}      # 認証リクエスト実行中（Enter連打の多重送信を防ぐ）
    closed = {"v": False}    # rootを破棄済みか（二重destroy・キャンセル無視を防ぐ）

    root = tk.Tk()

    def _safe_destroy():
        if not closed["v"]:
            closed["v"] = True
            try:
                root.destroy()
            except Exception:
                pass
    root.title("IZANAMI ログイン")
    root.resizable(False, False)
    try:  # 隠し起動(vbs)のウィンドウ非表示指定を継承しても必ず前面に出す
        root.deiconify()
        root.state("normal")
        root.attributes("-topmost", True)
        root.lift()
        root.focus_force()
        root.after(800, lambda: root.attributes("-topmost", False))
    except Exception:
        pass

    frm = ttk.Frame(root, padding=(28, 22))
    frm.pack(fill="both", expand=True)
    ttk.Label(frm, text="IZANAMI", font=("", 16, "bold")).grid(
        row=0, column=0, columnspan=2, pady=(0, 2))
    ttk.Label(frm, text="ご利用にはログインが必要です").grid(
        row=1, column=0, columnspan=2, pady=(0, 14))

    saved_id, saved_pw = auth_client.load_credentials()
    ttk.Label(frm, text="ID").grid(row=2, column=0, sticky="e", padx=(0, 8), pady=4)
    id_var = tk.StringVar(value=saved_id)
    id_ent = ttk.Entry(frm, textvariable=id_var, width=28)
    id_ent.grid(row=2, column=1, sticky="w", pady=4)
    ttk.Label(frm, text="パスワード").grid(row=3, column=0, sticky="e", padx=(0, 8), pady=4)
    pw_var = tk.StringVar(value=saved_pw)
    pw_ent = ttk.Entry(frm, textvariable=pw_var, width=28, show="●")
    pw_ent.grid(row=3, column=1, sticky="w", pady=4)

    save_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(frm, text="ログイン情報を保存する", variable=save_var).grid(
        row=4, column=1, sticky="w", pady=(6, 2))

    status = ttk.Label(frm, text="", foreground="#c0392b", wraplength=280,
                       justify="left")
    status.grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 2))

    btn = ttk.Button(frm, text="ログイン")
    btn.grid(row=6, column=0, columnspan=2, pady=(8, 4), ipadx=24)

    # ── パスワード再発行（登録リンクは仕様により一切設けない） ──
    def _forgot(_e=None):
        dlg = tk.Toplevel(root)
        dlg.title("パスワード再発行")
        dlg.resizable(False, False)
        f = ttk.Frame(dlg, padding=(24, 18))
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="ご登録のメールアドレスを入力してください").pack(anchor="w")
        ev = tk.StringVar()
        ent = ttk.Entry(f, textvariable=ev, width=36)
        ent.pack(pady=8)
        ent.focus_set()

        dlg_busy = {"v": False}

        def _send():
            email = ev.get().strip()
            if not email or dlg_busy["v"]:
                return
            dlg_busy["v"] = True          # 二重送信防止
            send_btn.config(state="disabled")
            dlg.config(cursor="watch")

            def _done():
                # 登録の有無は画面に出さない（常に同じ文言）
                messagebox.showinfo(
                    "パスワード再発行",
                    "ご登録があれば、新しいパスワードをメールでお送りしました。",
                    parent=dlg)
                try:
                    dlg.destroy()
                except Exception:
                    pass

            def _work():
                # 通信はワーカースレッドで（メインスレッド同期通信のUIフリーズを防ぐ）
                try:
                    auth_client.request_reset(email)
                except Exception:
                    pass
                try:
                    dlg.after(0, _done)
                except Exception:
                    pass

            threading.Thread(target=_work, daemon=True).start()

        send_btn = ttk.Button(f, text="送信", command=_send)
        send_btn.pack(pady=(4, 0), ipadx=16)
        dlg.bind("<Return>", lambda _e: _send())
        dlg.transient(root)
        dlg.grab_set()

    forgot = ttk.Label(frm, text="パスワードを忘れた方はこちら",
                       foreground="#2563eb", cursor="hand2")
    forgot.grid(row=7, column=0, columnspan=2, pady=(2, 0))
    forgot.bind("<Button-1>", _forgot)

    def _finish_ok(res: dict):
        # お知らせ・更新通知・起動時ページは本体ウィンドウ表示後に出す（この画面では
        # モーダルを重ねない＝×で閉じてもキャンセルが無視される事故を防ぐ）。
        try:
            if save_var.get():
                auth_client.save_credentials(id_var.get().strip(), pw_var.get())
            else:
                auth_client.clear_credentials()
        except Exception:
            pass
        state["res"] = res
        _safe_destroy()

    def _on_result(res, error: str = ""):
        if closed["v"]:
            return  # 既に閉じている（×された）＝何もしない
        busy["v"] = False
        btn.config(state="normal")
        if error:
            status.config(text=f"サーバーに接続できません。\n{error}")
            return
        action, text = decide(res)
        if action == "ok":
            _finish_ok(res)
        elif action == "exit":
            # 期限切れ/同時起動制限: サーバーからのお知らせがあれば併せて表示
            try:
                if (res or {}).get("message"):
                    auth_client.show_notice(res.get("message"), parent=root)
            except Exception:
                pass
            if closed["v"]:      # 通知表示中に×された
                return
            messagebox.showerror("IZANAMI", text, parent=root)
            state["res"] = None
            _safe_destroy()
        else:  # stay
            status.config(text=text)

    def _login(_e=None):
        if busy["v"] or closed["v"]:
            return  # 実行中の多重送信（Enter連打）を無視
        uid, pw = id_var.get().strip(), pw_var.get()
        if not uid or not pw:
            status.config(text="IDとパスワードを入力してください。")
            return
        busy["v"] = True
        btn.config(state="disabled")
        status.config(text="確認中…")

        def _work():
            try:
                res = auth_client.authenticate(uid, pw)
                root.after(0, lambda: _on_result(res))
            except Exception as e:
                msg = str(e)[:120]
                root.after(0, lambda: _on_result(None, error=msg))

        threading.Thread(target=_work, daemon=True).start()

    btn.config(command=_login)
    root.bind("<Return>", _login)
    (id_ent if not saved_id else pw_ent).focus_set()

    def _cancel():
        state["res"] = None  # 閉じた=起動しない（フェイルクローズ）
        _safe_destroy()

    root.protocol("WM_DELETE_WINDOW", _cancel)

    # 画面中央へ
    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry("+%d+%d" % ((sw - w) // 2, (sh - h) // 3))

    root.mainloop()
    res = state["res"]
    return res if (res or {}).get("result") == "ok" else None


def show_post_login(res: dict | None, parent) -> None:
    """認証OK後、本体ウィンドウ(parent)を親にお知らせ／更新通知／起動時ページを出す。

    ログイン画面ではなく本体表示後に出すことで、通知モーダル中の×操作で
    認証キャンセルが無視される事故（レビュー指摘）を構造的に防ぐ。失敗は無視。"""
    import auth_client
    res = res or {}
    try:
        if res.get("message"):
            auth_client.show_notice(res.get("message"), parent=parent)
        if auth_client.is_update_available(res):
            auth_client.show_notice(
                (res.get("update_message") or "新しいバージョンがあります。"),
                title=f"更新のお知らせ（{res.get('latest_version')}）", parent=parent)
        auth_client.open_startup_page(res)
    except Exception:
        pass


def start_heartbeat(session: str, interval: float = 60.0) -> None:
    """約60秒ごとに生存信号を送る（稼働ログ用・失敗は無視）。"""
    if not session:
        return
    import auth_client

    def _loop():
        while True:
            time.sleep(interval)
            auth_client.ping(session)

    threading.Thread(target=_loop, daemon=True).start()


def end_ping(session: str) -> None:
    """アプリ終了時の稼働終了信号（同期送信）。"""
    if not session:
        return
    try:
        import auth_client
        auth_client.ping(session, "end", sync=True)
    except Exception:
        pass
