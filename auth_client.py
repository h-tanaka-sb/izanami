# -*- coding: utf-8 -*-
"""
auth_client.py ―― move認証システム 接続部品（このアプリ用に設定済み）
==============================================================
このファイルはそのままアプリに同梱して使えます（書き換え不要）。
変える可能性があるのは LOCAL_VERSION（アプリ自身のバージョン）だけです。

使い方（アプリ側）:
    import auth_client
    res = auth_client.authenticate("入力されたID", "入力されたパスワード")
    if res["result"] == "ok":
        # 起動してよい。res["message"] があればお知らせ表示。
        # 稼働監視: auth_client.ping(res["session"]) を約60秒ごとに呼び、
        #           アプリ終了時に auth_client.ping(res["session"], "end", sync=True)
    elif res["result"] == "expired":
        # 「有効期限切れ」表示
    else:
        # 「起動できません」表示
==============================================================
"""

import base64
import json
import os
import re
import ssl
import threading
import urllib.parse
import urllib.request

# ============ 設定（発行済み） ============
API_URL       = "https://move-auth.com/api/check.php"
PING_URL      = "https://move-auth.com/api/ping.php"
RESET_URL     = "https://move-auth.com/api/reset.php"
APP_ID        = "izanagi"
LOCAL_VERSION = "1.0.0"   # このアプリ自身のバージョン
# ==========================================

# メッセージ中の最初のURLを取り出す正規表現（お知らせ・更新通知のリンク化に使う）
_URL_RE = re.compile(r'https?://[^\s　]+')


def _ssl_context():
    """HTTPSの証明書検証に使うCA証明書を用意する。
    同梱Python(uv等)ではOSの証明書ストアを参照できず
    『certificate verify failed』で通信が落ちることがあるため、
    certifi のCA証明書束を優先して使い、確実に検証できるようにする。
    （証明書検証は無効化しない＝なりすまし対策を保ったまま安定させる）"""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()

_SSL_CTX = _ssl_context()


# ===== ログイン情報の保存（自動入力用） =====
# パスワードは Windows の DPAPI でそのPCユーザー専用に暗号化して保存（平文では残さない）。
def _cred_path():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "move-auth", APP_ID)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        d = "."
    return os.path.join(d, "login.dat")


def _dpapi(protect, data):
    """WindowsのDPAPIでバイト列を暗号化(True)/復号(False)する。"""
    import ctypes
    from ctypes import wintypes
    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)):
        raise ctypes.WinError()
    out = ctypes.string_at(blob_out.pbData, blob_out.cbData)
    ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    return out


def save_credentials(login_id, password):
    """ID・パスワードを保存（パスワードはDPAPIで暗号化）。失敗しても無視。"""
    try:
        enc = base64.b64encode(_dpapi(True, password.encode("utf-8"))).decode("ascii")
        with open(_cred_path(), "w", encoding="utf-8") as f:
            json.dump({"id": login_id, "pw": enc}, f)
    except Exception:
        pass


def load_credentials():
    """保存済みの (ID, パスワード) を返す。無ければ ("", "")。"""
    try:
        with open(_cred_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        pw = _dpapi(False, base64.b64decode(d.get("pw", ""))).decode("utf-8")
        return d.get("id", ""), pw
    except Exception:
        return "", ""


def clear_credentials():
    """保存したログイン情報を削除する。"""
    try:
        os.remove(_cred_path())
    except Exception:
        pass


def authenticate(user_id, password):
    """承認サーバーに問い合わせ、結果（辞書）を返す。
    返り値の例:
      {"result":"ok","message":"...","expires_at":"2026-12-31 ...",
       "latest_version":"1.0.1","update_message":"...","session":"<稼働トークン>"}
    通信に失敗した場合は urllib の例外が送出される（呼び出し側で捕捉）。
    """
    data = urllib.parse.urlencode({
        "app_id":   APP_ID,
        "user_id":  user_id,
        "password": password,
    }).encode("utf-8")
    req = urllib.request.Request(API_URL, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as res:
        return json.loads(res.read().decode("utf-8"))


def ping(session, event="heartbeat", sync=False):
    """稼働監視サーバーに生存信号(heartbeat)または終了(end)を送る。
    session は authenticate() の戻り値の "session"。失敗しても無視する。
    sync=True なら同期送信（アプリ終了直前の end 用）。"""
    if not session:
        return
    def _do():
        try:
            data = urllib.parse.urlencode({"session": session, "event": event}).encode("utf-8")
            req = urllib.request.Request(PING_URL, data=data, method="POST")
            urllib.request.urlopen(req, timeout=8, context=_SSL_CTX)
        except Exception:
            pass
    if sync:
        _do()
    else:
        threading.Thread(target=_do, daemon=True).start()


def request_reset(email):
    """パスワード再発行を依頼する（「パスワードを忘れた方」用）。
    指定メールがこのアプリに登録済みなら、新パスワードが本人へメールされる。
    ※サーバーは登録の有無を返さない（常に成功扱い）。画面では
      「登録があれば新しいパスワードをお送りしました」と表示すること。"""
    try:
        data = urllib.parse.urlencode({"app_id": APP_ID, "email": email}).encode("utf-8")
        req = urllib.request.Request(RESET_URL, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as res:
            return json.loads(res.read().decode("utf-8")).get("ok", False)
    except Exception:
        return False


def find_url(text):
    """文字列の中から最初のURLを返す。無ければ None。
    お知らせ／更新通知をクリック可能なリンクとして表示する際に使う。"""
    if not text:
        return None
    m = _URL_RE.search(text)
    return m.group(0) if m else None


def show_notice(message, title="お知らせ", parent=None):
    """お知らせを「見やすい大きさ」のポップアップで表示する。
    - message 内に URL があれば、クリックで開けるリンク＋『リンクを開く』ボタンを表示。
    - parent にアプリのウィンドウ(Tk)を渡すとモーダル表示。無ければ単独で表示。
    使い方: res = authenticate(...); if res["result"]=="ok": show_notice(res["message"])
    ※ サイズや文字を変えたい時は、この関数の数値（minsize / wraplength / font）を編集。"""
    if not message:
        return
    import tkinter as tk
    import webbrowser

    standalone = (parent is None)
    if standalone:
        root = tk.Tk()
        root.withdraw()
        win = tk.Toplevel(root)
    else:
        win = tk.Toplevel(parent)

    win.title(title)
    win.minsize(480, 200)            # ← ポップアップの最小サイズ（ここを変えると大きさが変わる）
    try:
        win.attributes("-topmost", True)
    except Exception:
        pass

    frm = tk.Frame(win, padx=24, pady=20)
    frm.pack(fill="both", expand=True)
    tk.Label(frm, text=title, font=("", 15, "bold")).pack(anchor="w", pady=(0, 10))
    tk.Label(frm, text=message, font=("", 11), wraplength=430, justify="left").pack(anchor="w")

    url = find_url(message)
    if url:
        link = tk.Label(frm, text=url, font=("", 11), fg="#2563eb", cursor="hand2", wraplength=430)
        link.pack(anchor="w", pady=(8, 0))
        link.bind("<Button-1>", lambda e: webbrowser.open(url))
        tk.Button(frm, text="リンクを開く", command=lambda: webbrowser.open(url)).pack(anchor="w", pady=(6, 0))

    def _close():
        win.destroy()
        if standalone:
            root.destroy()

    tk.Button(frm, text="OK", width=12, command=_close).pack(pady=(16, 0))
    win.protocol("WM_DELETE_WINDOW", _close)

    # 画面中央あたりに表示
    win.update_idletasks()
    w, h = win.winfo_width(), win.winfo_height()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry("+%d+%d" % ((sw - w) // 2, (sh - h) // 3))

    if standalone:
        win.grab_set()
        root.mainloop()
    else:
        win.transient(parent)
        win.grab_set()
        parent.wait_window(win)


def open_startup_page(result):
    """ログインOK後、管理画面で設定した『起動時に開くページ』を既定ブラウザで開く。
    最大2つまで対応（open_urls）。未設定なら何もしない。安全のため http(s) のURLのみ開く。
    使い方: if res["result"]=="ok": open_startup_page(res)"""
    urls = (result or {}).get("open_urls") or []
    if not urls:
        u = (result or {}).get("open_url") or ""
        urls = [u] if u else []
    import webbrowser
    for url in urls:
        if isinstance(url, str) and (url.startswith("http://") or url.startswith("https://")):
            try:
                webbrowser.open(url)
            except Exception:
                pass


def is_update_available(result):
    """サーバーの latest_version が、このアプリより新しければ True。"""
    try:
        latest = [int(x) for x in str(result.get("latest_version", "")).split(".")]
        local  = [int(x) for x in str(LOCAL_VERSION).split(".")]
        return latest > local
    except Exception:
        return False
