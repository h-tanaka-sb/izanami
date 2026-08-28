"""⑥YouTube予約投稿: Data API v3（api・既定） / Studio Web（browser・フォールバック）。

API版: videos.insert を resumable で実行。予約は status.privacyStatus="private" ＋
status.publishAt（未来UTC RFC3339）。selfDeclaredMadeForKids 必須。投稿後 thumbnails.set。
※ 実投稿は外部公開に当たるため、検証時は upload_dry_run=True で本文だけ組み立てて確認できる。
"""
from __future__ import annotations

import datetime
import random
import time
from pathlib import Path

from .. import browser_ai
from .base import UploadProvider, register

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]


def _stopped() -> bool:
    """⏹中断ボタン（pipeline が browser_ai.STOP_CHECK に橋渡し）。投稿の途中でも止められるように。"""
    try:
        return bool(browser_ai.STOP_CHECK and browser_ai.STOP_CHECK())
    except Exception:
        return False


_STOP_RES = {"ok": False, "stopped": True, "note": "⏹ 中断しました"}


def _to_utc_rfc3339(meta: dict, cfg: dict) -> str:
    """予約時刻（未来UTC・末尾Z）を決める。meta優先、無ければcfgの既定スロット。"""
    dt = None
    s = (meta or {}).get("scheduled_publish_at") or ""
    if s:
        try:
            dt = datetime.datetime.fromisoformat(s)
        except Exception:
            dt = None
    if dt is None:
        slots = cfg.get("default_schedule_slots", ["09:00"])
        offset = int(cfg.get("default_publish_offset_days", 1))
        hh, mm = (slots[0] if slots else "09:00").split(":")
        jst = datetime.timezone(datetime.timedelta(hours=9))
        d = datetime.datetime.now(jst) + datetime.timedelta(days=offset)
        dt = d.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=9)))
    utc = dt.astimezone(datetime.timezone.utc)
    now = datetime.datetime.now(datetime.timezone.utc)
    if utc <= now + datetime.timedelta(minutes=20):
        utc = now + datetime.timedelta(days=1)  # 過去/直近は翌日へ
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_body(meta: dict, cfg: dict, publish_at: str) -> dict:
    title = (meta.get("title") or "無題")[:100]
    desc = (meta.get("description") or "")[:5000]
    tags = meta.get("tags") or []
    status = {"privacyStatus": cfg.get("youtube_privacy", "private"),
              "selfDeclaredMadeForKids": bool(cfg.get("youtube_made_for_kids", False))}
    # YouTube Data API は publishAt を privacyStatus=private の時しか受け付けない。
    # 無条件に付けると「すぐ公開/限定公開」が必ず 400 で失敗する（2026-07-16監査 #25）
    if status["privacyStatus"] == "private" and publish_at:
        status["publishAt"] = publish_at
    return {
        "snippet": {"title": title, "description": desc, "tags": tags,
                    "categoryId": str(cfg.get("youtube_category_id", "22")),
                    "defaultLanguage": "ja", "defaultAudioLanguage": "ja"},
        "status": status,
    }


@register("upload", "api")
class YouTubeDataAPIUpload(UploadProvider):
    @classmethod
    def available(cls, cfg: dict):
        cs = (cfg.get("youtube_client_secret") or "").strip()
        if not cs or not Path(cs).exists():
            return False, "youtube_client_secret(OAuthクライアントJSON) が見つかりません"
        return True, ""

    def _service(self, cfg, log):
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        token = cfg.get("youtube_token") or "token.json"
        secret = cfg.get("youtube_client_secret")
        creds = None
        if Path(token).exists():
            creds = Credentials.from_authorized_user_file(token, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                log("    初回はブラウザでYouTubeの認可を行ってください（token.json に保存されます）…")
                flow = InstalledAppFlow.from_client_secrets_file(secret, SCOPES)
                creds = flow.run_local_server(port=0)
            Path(token).write_text(creds.to_json(), encoding="utf-8")
        return build("youtube", "v3", credentials=creds)

    def schedule_upload(self, video, meta, thumb, cfg, log=print) -> dict:
        publish_at = _to_utc_rfc3339(meta, cfg)
        # 限定公開/すぐ公開はYouTube仕様上「予約」できない（予約=非公開→予約時刻に公開のみ）。
        # 旧実装はpublishAtを黙って捨てつつログ/記録に「予約: …」を残していた＝虚偽の予約記録
        # （2026-08-17配布先報告④⑤と同根の整合修正）
        _privacy = (cfg.get("youtube_privacy") or "private").lower()
        if _privacy != "private" and publish_at:
            log(f"    公開のしかたが「{_privacy}」のため予約日時は使わず、即時その公開状態で"
                "投稿します（予約したい場合は「非公開（予約はこれ）」を選択）。")
            publish_at = ""
        # タイトルが自動生成できていない動画を全自動で投稿しない（2026-08-17）
        _t = (meta.get("title") or "").strip()
        if _t in ("", "無題", "無題の動画") and not cfg.get("upload_dry_run"):
            return {"ok": False, "error": "タイトルが自動生成できていません。"
                                          "投稿タブでタイトルを付けてから🚀してください。"}
        body = _build_body(meta, cfg, publish_at)
        if cfg.get("upload_dry_run"):
            log("    [dry-run] 投稿本文を組み立てました（実投稿はしません）。")
            return {"ok": True, "dry_run": True, "publish_at": publish_at, "body": body}
        if not Path(video).exists():
            return {"ok": False, "error": "動画ファイルが見つかりません"}
        if _stopped():
            return dict(_STOP_RES)
        from googleapiclient.http import MediaFileUpload
        from googleapiclient.errors import HttpError
        yt = self._service(cfg, log)
        if _stopped():
            return dict(_STOP_RES)
        # chunksizeを刻む＝チャンク間で⏹中断チェックできる（-1だと1回で全量＝止められない）
        media = MediaFileUpload(str(video), chunksize=8 * 1024 * 1024, resumable=True)
        req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
        log(f"    アップロード中（予約: {publish_at}）…" if publish_at
            else f"    アップロード中（即時・{_privacy}）…")
        resp = None; retry = 0
        while resp is None:
            if _stopped():
                log("    ⏹ 中断要求 → アップロードを途中でやめます（YouTube側には投稿されません）。")
                return dict(_STOP_RES)
            try:
                _, resp = req.next_chunk()
            except HttpError as e:
                if getattr(e, "resp", None) and e.resp.status in (500, 502, 503, 504) and retry < 8:
                    retry += 1
                    wait = random.random() * (2 ** retry)
                    while wait > 0:  # 1秒刻み＝バックオフ中も⏹中断が効く
                        if _stopped():
                            log("    ⏹ 中断要求 → リトライ待機を打ち切ります。")
                            return dict(_STOP_RES)
                        time.sleep(min(1.0, wait))
                        wait -= 1.0
                    continue
                return {"ok": False, "error": f"insert失敗: {e}"}
        vid = resp.get("id")
        log(f"    アップロード完了 videoId={vid}")
        result = {"ok": True, "video_id": vid, "publish_at": publish_at}
        if thumb and Path(thumb).exists():
            try:
                yt.thumbnails().set(
                    videoId=vid, media_body=MediaFileUpload(str(thumb), mimetype="image/png")
                ).execute()
                log("    カスタムサムネを設定しました。")
            except HttpError as e:
                if getattr(e, "resp", None) and e.resp.status == 403:
                    log("    ※チャンネル未認証のためサムネ設定不可（youtube.com/verify）。投稿は成功。")
                    result["thumbnail_error"] = "403 not verified"
                else:
                    result["thumbnail_error"] = str(e)
        return result


# CDP接続(connect_over_cdp)の set_input_files はファイル全体をbase64化して通信路に流すため
# **50MB上限**がある（Playwright本体が _isBrowserCollocatedWithServer=false 固定で安全側に
# 倒すため。実測: 450MBの動画添付が100%失敗＝2026-08-04配布先報告）。
# 実際はブラウザもPlaywrightも同一PC上＝CDPの DOM.setFileInputFiles で「パスを渡して
# ブラウザ自身に読ませる」ことができ、サイズ制限を受けない（報告書の案A）。
_JS_PICK_FILE_INPUT = """(() => {
  const found = [];
  const walk = (root) => {
    root.querySelectorAll('input[type=file]').forEach(e => found.push(e));
    root.querySelectorAll('*').forEach(n => { if (n.shadowRoot) walk(n.shadowRoot); });
  };
  walk(document);
  return found[%s];
})()"""


def _cdp_set_files(page, path: str, pick: str = "first", log=print) -> None:
    """shadow DOMを貫通してfile inputを取得し、CDPでファイルパスを直接渡す（50MB制限なし）。

    pick="first"=アップロード画面の動画入力 / "last"=詳細画面のサムネ入力
    （既存の set_input_files 経路と同じ要素選択に合わせる）。失敗は例外＝呼び元で処理。"""
    idx = "0" if pick == "first" else "found.length-1"
    cdp = page.context.new_cdp_session(page)
    try:
        try:
            cdp.send("DOM.enable")
        except Exception:
            pass
        res = cdp.send("Runtime.evaluate",
                       {"expression": _JS_PICK_FILE_INPUT % idx, "returnByValue": False})
        obj = (res.get("result") or {}).get("objectId")
        if not obj:
            raise RuntimeError("file inputが見つかりません（CDP経路）")
        cdp.send("DOM.setFileInputFiles", {"files": [str(path)], "objectId": obj})
    finally:
        try:
            cdp.detach()
        except Exception:
            pass


def _attach_file(page, target, path: str, pick: str, log=print) -> None:
    """動画の添付はCDP直接方式（パスを渡すだけ・サイズ制限なし）を第一にする。

    2026-08-17配布先報告: 通常経路(set_input_files)はCDP接続だとファイル全体を
    base64で通信路に流すため、**50MB弱の動画が30秒タイムアウトで失敗**し、
    50MB超（即エラー→CDPへ切替）だけ成功する逆転が実測された（49.6MB/48.4MBで再現・
    62.7MB/94.4MBはCDP方式で成功）。→ 動画は実績のあるCDP方式を最初から使う。
    サムネ等の小ファイルは従来経路を先に使い、サイズに限らず失敗全般でCDPへ落とす。"""
    def _legacy():
        if hasattr(target, "set_input_files") and target is not page:
            target.set_input_files(path, timeout=10000)
        else:
            page.set_input_files('input[type="file"]', path, timeout=10000)

    if pick == "first":   # 動画
        try:
            _cdp_set_files(page, path, pick=pick, log=log)
            return
        except Exception as e:
            log(f"    パス直接方式（CDP）で添付できませんでした（{str(e)[:60]}）"
                "→ 通常方式で再試行します…")
        _legacy()
        return
    try:                  # サムネ等の小ファイル
        _legacy()
    except Exception:
        log("    通常方式で添付できないため、ブラウザへパスを直接渡す方式（CDP）で"
            "添付します…")
        _cdp_set_files(page, path, pick=pick, log=log)


def _finish_publish(page, cfg: dict, when: str, log=print, dump=lambda n: None):
    """公開設定タブまで進め、予約日時（無ければ公開範囲）を**確定まで**自動で行う。

    2026-08-06ユーザー指示「自動で予約投稿まで完了させる設計に」。
    誤予約・虚偽の完了記録のフェイルセーフ（同日の敵対検証で5点強化）:
    ・過去/直近すぎる予約日時は打鍵前に拒否（API経路の未来クランプと対称）
    ・日付の検証は入力欄でなく**確定表示（datepicker-trigger）**を読む
      （入力欄は「自分が打った文字が残っているだけ」でも一致してしまう）
    ・AM/PM・午前/午後（12時間表記ロケール）を検出したら確定しない
    ・確定ボタンは厳密セレクタのみ（部分一致は「スケジュールを設定」展開ボタンに誤爆）
    ・確定ボタン押下後は**確定状態（ボタン消滅）になったこと**を確認。ならなければ
      未確定=手動へ。押下後に確認そのものができない（ブラウザが閉じられた等）時だけは
      「押した後・結果未確認」を明示して返す（pending扱いにせず二重投稿の注意を促す）
    2026-08-07: 収益化チャンネル対応＝ステップ数が増えるため「公開設定に着くまで」歩き、
      収益化ステップが出たら「オン」に設定（読み戻し確認・できなければ手動へ）、
      広告の適合性は「上記のいずれでもない」で評価を送信してから進む。
    戻り値: {"ok": True,...}=確定済み / None=確定せず断念（手動へ） /
            {"ok": False, "clicked_unverified": True,...}=押した後に確認不能。
    """
    import datetime as _dt
    import re as _re

    def _click(sels, t=4000):
        for s in sels:
            try:
                el = page.query_selector(s)
                if el and el.is_visible():
                    el.click(timeout=t)
                    return True
            except Exception:
                continue
        return False

    def _done_visible():
        el = (page.query_selector('ytcp-button#done-button')
              or page.query_selector('#done-button'))
        try:
            return bool(el and el.is_visible())
        except Exception:
            return False

    # 確定押下後に出るお知らせダイアログ（実機2026-08-07: チェック未完了のまま確定すると
    # 「現在、動画をチェックしています」が出て手前で止まる。OKで閉じれば予約は確定する）
    CHECKS_OK = ['tp-yt-paper-dialog:has-text("動画をチェックしています") '
                 'ytcp-button:has-text("OK")',
                 'ytcp-dialog:has-text("動画をチェックしています") ytcp-button:has-text("OK")',
                 'ytcp-prechecks-warning-dialog #confirm-button',
                 'ytcp-prechecks-warning-dialog ytcp-button']

    def _confirm_done(kind: str):
        """確定ボタンを押し、確定状態（ボタン消滅）まで見届ける。
        True=確定 / False=押したが未確定（Studio側エラー等） / None=押せず /
        "unknown"=押した後に状態確認が不能。"""
        if not _click(['ytcp-button#done-button', '#done-button']):
            log(f"    ⚠ 自動確定: {kind}の確定ボタンが押せませんでした。")
            dump("studio_done_fail")
            return None
        try:
            for _ in range(12):
                page.wait_for_timeout(1000)
                if not _done_visible():
                    return True
                if _click(CHECKS_OK):
                    log("    Studioのお知らせ「現在、動画をチェックしています」をOKで"
                        "閉じました（チェックは投稿後も続き、予約はそのまま確定します）。")
            log("    ⚠ 自動確定: 確定ボタンを押しましたが画面が確定状態になりません"
                "（Studio側のエラーの可能性）→ 画面で確認してください。")
            dump("studio_done_not_committed")
            return False
        except Exception:
            return "unknown"

    UNVERIFIED = {"ok": False, "clicked_unverified": True,
                  "error": "確定ボタンを押した後、結果を確認できませんでした。"
                           "Studioの動画一覧で予約/公開済みかを必ず確認してください"
                           "（未確認のまま再投稿すると二重になります）。"}

    def _close_processing():
        """確定後に残る「動画を処理しています」お知らせを閉じる（予約は確定済み。
        開いたままだと途中で止まったように見える・2026-08-10配布先報告）。失敗は無害。"""
        try:
            if _click(['ytcp-uploads-still-processing-dialog #close-button',
                       'ytcp-uploads-still-processing-dialog ytcp-button',
                       'tp-yt-paper-dialog:has-text("動画を処理しています") '
                       'ytcp-button:has-text("閉じる")']):
                log("    「動画を処理しています」のお知らせを閉じました"
                    "（処理はYouTube側で続き、予約時刻に公開されます）。")
        except Exception:
            pass
    def _visible(sels):
        for s in sels:
            try:
                el = page.query_selector(s)
                if el and el.is_visible():
                    return el
            except Exception:
                continue
        return None

    def _on_visibility_step():
        return _visible(['ytcp-button#second-container-expand-button',
                         '#second-container-expand-button',
                         'tp-yt-paper-radio-button[name="PRIVATE"]']) is not None

    MONET_DROP = ['ytcp-video-monetization ytcp-dropdown-trigger',
                  'ytcp-video-monetization']

    def _monet_expected():
        """このウィザードに「収益化」ステップがあるか＝ダイアログ内の文言で先に判定する。
        収益化chでドロップダウンの描画が遅い/セレクタが実DOMと違っても、素通りして
        収益なしのまま予約確定してしまわないためのフェイルクローズ用（敵対検証2026-08-07）。
        詳細ステップの時点で読む＝チェック工程の「収益化が制限…」等の文言はまだ出ていない。"""
        el = _visible(['ytcp-uploads-dialog', 'tp-yt-paper-dialog'])
        try:
            return bool(el) and ("収益化" in el.inner_text())
        except Exception:
            return False

    def _monet_text():
        el = _visible(MONET_DROP)
        try:
            return el.inner_text() if el else ""
        except Exception:
            return ""

    MONET_HELP = ("画面で収益化を「オン」にし、広告の適合性を送信してから、"
                  "続きを進めてください。")

    def _handle_monetization():
        """収益化ステップ（収益化チャンネルのみ出現）: 「オン」に設定する（2026-08-07指示）。
        戻り値: "absent"=ドロップダウンが見当たらない（このページには無い）/
        True=オン済みにできた・すでにオン / False=出ているのにオンにできず（→手動へ）。
        オンにできないまま進めると収益なしで予約が確定してしまうため、こちらもフェイルクローズ。"""
        if _visible(MONET_DROP) is None:
            return "absent"
        txt = _monet_text()
        if "オン" in txt or _re.search(r"\bOn\b", txt):
            return True
        log("    収益化ステップを検出 → 「オン」に設定します…")
        if not _click(MONET_DROP):
            log("    ⚠ 自動確定: 収益化の選択を開けませんでした。")
            dump("studio_monetize_fail")
            return False
        page.wait_for_timeout(800)
        if not _click(['tp-yt-paper-radio-button#radio-on', '#radio-on',
                       'tp-yt-paper-radio-button:has-text("オン")']):
            log("    ⚠ 自動確定: 収益化の「オン」を選べませんでした。")
            dump("studio_monetize_fail")
            return False
        page.wait_for_timeout(400)
        _click(['ytcp-button#save-button', '#save-button'])   # ダイアログ型UIのみ存在（無ければ即時反映型）
        page.wait_for_timeout(800)
        txt = _monet_text()
        if not ("オン" in txt or _re.search(r"\bOn\b", txt)):
            log(f"    ⚠ 自動確定: 収益化がオンになったことを確認できません（表示={txt!r}）。")
            dump("studio_monetize_fail")
            return False
        log("    ✅ 収益化: オンに設定しました。")
        return True

    def _handle_suitability():
        """広告の適合性の自己申告（収益化チャンネルのみ）: 「上記のいずれでもない」→評価を送信。
        ステップが無ければ何もしない。失敗時は次へが押せず walker が手動へ落とす。"""
        box = _visible(['ytcp-checkbox-lit#checkbox-NONE_OF_THE_ABOVE',
                        '#checkbox-NONE_OF_THE_ABOVE',
                        'ytcp-checkbox-lit:has-text("上記のいずれ")'])
        if box is None:
            return
        try:
            if (box.get_attribute("aria-checked") or "") != "true":
                box.click(timeout=4000)
                page.wait_for_timeout(400)
        except Exception:
            return
        if _click(['ytcp-button#submit-questionnaire-button',
                   '#submit-questionnaire-button',
                   'ytcp-button:has-text("評価を送信")']):
            log("    広告の適合性: 「上記のいずれでもない」で評価を送信しました。")
            page.wait_for_timeout(1000)

    try:
        if when:
            m = _re.match(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})", when)
            if not m:
                log(f"    ⚠ 自動確定: 予約日時の形式を解釈できません: {when}")
                return None
            y, mo, d, hh, mi = (int(x) for x in m.groups())
            date_s, time_s = f"{y}/{mo:02d}/{d:02d}", f"{hh}:{mi:02d}"
            # 過去・直近すぎる日時は確定しない（作り直しの古い予約引き継ぎ等で実際に起こる。
            # Studioが受理すると即時公開相当になるため、ウィザードに触れる前に止めて手動へ）
            try:
                tgt = _dt.datetime(y, mo, d, hh, mi)
            except ValueError:
                log(f"    ⚠ 自動確定: 予約日時が不正です: {when}")
                return None
            if tgt <= _dt.datetime.now() + _dt.timedelta(minutes=10):
                log(f"    ⚠ 自動確定: 予約日時が過去または直近すぎます（{date_s} {time_s}）"
                    "→ 確定せず画面へ引き渡します。投稿タブで日時を選び直してください。")
                return None
        # 収益化チャンネルはステップが増える（詳細→収益化→広告の適合性→動画の要素→チェック→
        # 公開設定）ため、回数固定でなく「公開設定ステップに着くまで」次へで歩く（上限7回）。
        # ウィザードに「収益化」ステップがあるかは詳細ステップの文言で先に判定しておき、
        # ある場合はオンにできたことを確認できない限り絶対に確定しない（素通り＝収益なし予約の防止）
        monet_expected = _monet_expected()
        monet_done = False
        for step in range(7):
            if _stopped():
                log("    ⏹ 停止要求を検知 → 確定せず画面へ引き渡します。")
                return None
            if _on_visibility_step():
                break
            if monet_expected and not monet_done and step >= 1:
                # 収益化ステップの描画がネットワーク待ちで遅れることがある → 最大10秒待つ
                for _ in range(10):
                    if _visible(MONET_DROP) is not None:
                        break
                    page.wait_for_timeout(1000)
            r = _handle_monetization()
            if r is False:
                log("    ⚠ " + MONET_HELP)
                return None
            if r is True:
                monet_done = True
            _handle_suitability()
            ok_next = False
            for _att in range(3):
                if _click(['#next-button', 'ytcp-button#next-button']):
                    ok_next = True
                    break
                # サジェスト/トースト等の一時オーバーレイで1発失敗することがある
                # （ショートで実績・2026-08-10）→ Escapeで閉じて再試行
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
                page.wait_for_timeout(1500)
            if not ok_next:
                log(f"    ⚠ 自動確定: 『次へ』({step + 1}ページ目)が押せませんでした。")
                dump("studio_next_fail")
                return None
            page.wait_for_timeout(1500)
        else:
            if not _on_visibility_step():
                log("    ⚠ 自動確定: 公開設定ステップに到達できませんでした。")
                dump("studio_walk_fail")
                return None
        if monet_expected and not monet_done:
            # 収益化ステップがあるはずなのに一度も設定できていない（描画されなかった/
            # 画面部品が想定と違う）→ このまま確定すると収益なし予約になるので手動へ
            log("    ⚠ 自動確定: 収益化ステップをオンにできたか確認できませんでした。" + MONET_HELP)
            dump("studio_monetize_fail")
            return None
        if _stopped():
            log("    ⏹ 停止要求を検知 → 確定せず画面へ引き渡します。")
            return None
        if when:
            if not _click(['ytcp-button#second-container-expand-button',
                           '#second-container-expand-button',
                           'ytcp-icon-button#second-container-expand-button']):
                log("    ⚠ 自動確定: 『スケジュールを設定』を開けませんでした。")
                dump("studio_sched_open_fail")
                return None
            page.wait_for_timeout(1000)
            if not _click(['ytcp-text-dropdown-trigger#datepicker-trigger',
                           '#datepicker-trigger']):
                log("    ⚠ 自動確定: 日付ピッカーを開けませんでした。")
                dump("studio_sched_date_fail")
                return None
            page.wait_for_timeout(800)
            din = (page.query_selector('ytcp-date-picker tp-yt-paper-input input')
                   or page.query_selector('ytcp-date-picker input')
                   or page.query_selector('tp-yt-paper-dialog input'))
            if not din:
                log("    ⚠ 自動確定: 日付入力欄が見つかりません。")
                dump("studio_sched_date_fail")
                return None
            din.click(); page.wait_for_timeout(200)
            page.keyboard.press("Control+a")
            page.keyboard.insert_text(date_s)
            page.keyboard.press("Enter")
            page.wait_for_timeout(800)
            tin = (page.query_selector('#time-of-day-container input')
                   or page.query_selector(
                       'ytcp-form-input-container#time-of-day-container input')
                   or page.query_selector('input[aria-label*="時刻"]'))
            if not tin:
                log("    ⚠ 自動確定: 時刻入力欄が見つかりません。")
                dump("studio_sched_time_fail")
                return None
            tin.click(); page.wait_for_timeout(300)
            page.keyboard.press("Control+a")
            page.keyboard.insert_text(time_s)
            page.keyboard.press("Enter")
            page.wait_for_timeout(800)
            # フェイルセーフ検証:
            # ・日付=**確定表示（トリガー）**の数字列がYYYYMMDDと一致
            #   （入力欄の読み戻しは自分の打鍵が残っているだけでも通ってしまう）
            # ・時刻=入力欄の時:分が数値一致・AM/PM等の12時間表記が無いこと
            try:
                trig = (page.query_selector(
                            'ytcp-text-dropdown-trigger#datepicker-trigger')
                        or page.query_selector('#datepicker-trigger'))
                trig_text = trig.inner_text() if trig else ""
                tval = tin.input_value() or ""
            except Exception:
                trig_text, tval = "", ""
            got_d = _re.sub(r"\D", "", trig_text)
            tm = _re.search(r"(\d{1,2}):(\d{2})", tval)
            meridiem = _re.search(r"[AP]M|[ap]m|午前|午後", tval)
            if got_d != f"{y}{mo:02d}{d:02d}" or not tm or meridiem \
                    or (int(tm.group(1)), int(tm.group(2))) != (hh, mi):
                log(f"    ⚠ 自動確定: 確定表示の検証が一致しません"
                    f"（日付表示={trig_text!r} 時刻={tval!r} / 希望={date_s} {time_s}"
                    f"{'・12時間表記を検出' if meridiem else ''}）"
                    "→ 確定せず画面へ引き渡します。")
                dump("studio_sched_verify_fail")
                return None
            if _stopped():
                log("    ⏹ 停止要求を検知 → 確定ボタンは押さずに引き渡します。")
                return None
            st = _confirm_done("予約")
            if st is None or st is False:
                return None
            if st == "unknown":
                return dict(UNVERIFIED)
            dump("studio_scheduled")
            _close_processing()
            log(f"    ✅ 予約投稿を確定しました: {date_s} {time_s}")
            return {"ok": True, "publish_at": when,
                    "note": f"予約投稿を自動確定（{date_s} {time_s}）"}
        # 予約日時なし: 投稿タブの「公開のしかた」で確定
        privacy = (cfg.get("youtube_privacy") or "private").lower()
        # ※publicのhas-textフォールバックは廃止: 「公開」は「非公開」「限定公開」にも
        #   部分一致し、DOM順先頭の非公開ラジオを押してしまう（敵対検証2026-08-17）
        radios = {
            "public": ['tp-yt-paper-radio-button[name="PUBLIC"]'],
            "unlisted": ['tp-yt-paper-radio-button[name="UNLISTED"]',
                         'tp-yt-paper-radio-button:has-text("限定公開")'],
            "private": ['tp-yt-paper-radio-button[name="PRIVATE"]',
                        'tp-yt-paper-radio-button:has-text("非公開")'],
        }
        if not _click(radios.get(privacy, radios["private"])):
            log("    ⚠ 自動確定: 公開範囲を選べませんでした。")
            dump("studio_privacy_fail")
            return None
        page.wait_for_timeout(800)
        if _stopped():
            log("    ⏹ 停止要求を検知 → 確定ボタンは押さずに引き渡します。")
            return None
        st = _confirm_done("公開設定")
        if st is None or st is False:
            return None
        if st == "unknown":
            return dict(UNVERIFIED)
        dump("studio_published")
        _close_processing()
        log(f"    ✅ 公開設定「{privacy}」で確定しました。")
        return {"ok": True, "note": f"公開設定「{privacy}」で自動確定"}
    except Exception as e:
        log(f"    ⚠ 自動確定でエラー: {str(e)[:80]} → 画面へ引き渡します。")
        dump("studio_publish_error")
        return None


@register("upload", "studio_web")
class YouTubeStudioUpload(UploadProvider):
    """YouTube Studio（ブラウザ）投稿。共有Chrome(chrome_profile)がYouTubeにログイン済みである前提。

    動画添付→タイトル/説明/子ども向け/サムネ/タグの自動入力の後、
    studio_auto_publish=True（2026-08-06から既定ON）なら予約日時の入力と
    『スケジュール設定』の確定まで自動で行う（_finish_publish）。読み戻し検証に
    合格しない・UIが想定と違う等の場合は確定せず、従来どおり最後だけ人が押す
    手動引き渡しへフェイルセーフで落ちる。ページは開いたままにする。
    """

    @classmethod
    def needs_browser(cls):
        return True

    def schedule_upload(self, video, meta, thumb, cfg, log=print) -> dict:
        from pathlib import Path
        if self.ctx is None:
            return {"ok": False, "error": "Webセッションなし（設定・キーの『ログイン用ブラウザを開く』でYouTubeにログインを）"}
        if cfg.get("upload_dry_run"):
            return {"ok": True, "dry_run": True, "note": "studio_web は dry-run 非対象（実際に画面で確認して投稿します）"}
        video = str(Path(video).resolve())
        if not Path(video).exists():
            return {"ok": False, "error": "動画ファイルが見つかりません"}
        title = (meta.get("title") or "無題")[:100]
        desc = (meta.get("description") or "")[:4900]
        tags = meta.get("tags") or []
        debug = cfg.get("_debug_dir") if cfg.get("debug_dump") else None
        page = self.ctx.new_page()

        def dump(name):
            if debug:
                browser_ai.dump_page(page, debug, name, log, include_imgs=True)

        def click_first(sels, t=4000):
            for s in sels:
                try:
                    el = page.query_selector(s)
                    if el and el.is_visible():
                        el.click(timeout=t)
                        return True
                except Exception:
                    continue
            return False

        # 投稿先チャンネル指定があれば、そのチャンネルのStudioを直接開く
        import re as _re
        studio_url = "https://studio.youtube.com/"
        ch = (cfg.get("youtube_channel_url") or "").strip()
        m = _re.search(r"/channel/([A-Za-z0-9_\-]+)", ch)
        if m:
            studio_url = f"https://studio.youtube.com/channel/{m.group(1)}/"
        try:
            if _stopped():
                page.close()
                return dict(_STOP_RES)
            log(f"    YouTube Studio を開いています…（{studio_url}）")
            page.goto(studio_url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
            if _stopped():
                page.close()
                return dict(_STOP_RES)
            if "accounts.google.com" in (page.url or ""):
                dump("studio_nologin")
                return {"ok": False, "error": "このChromeがYouTubeに未ログインです。設定・キー『ログイン用ブラウザを開く』で投稿先チャンネルにログインしてください。"}

            # 作成 → 動画をアップロード
            click_first(['ytcp-button#create-icon', '#create-icon', 'button[aria-label*="作成"]', 'button[aria-label*="Create"]'])
            page.wait_for_timeout(1200)
            click_first(['#text-item-0', 'tp-yt-paper-item:has-text("アップロード")',
                         'tp-yt-paper-item:has-text("動画をアップロード")', 'tp-yt-paper-item:has-text("Upload")'])
            page.wait_for_timeout(1500)

            if _stopped():
                page.close()
                return dict(_STOP_RES)
            # 動画ファイルを添付（file input は Playwright が shadow DOM を貫通して取得）
            try:
                # Studioのfile inputは常に非表示(aria-hidden)＝既定のvisible待ちだと
                # 「要素は見つかるが永遠に表示されない」で20秒タイムアウトする
                # （配布先の実ログで43回 hidden 解決を確認）。DOM存在(attached)を待つ
                page.wait_for_selector('input[type="file"]', timeout=20000,
                                       state="attached")
                # 50MB超はCDP直接添付へ自動フォールバック（CDP接続のbase64転送上限対策）
                _attach_file(page, page, video, pick="first", log=log)
                log("    動画ファイルを添付しました。アップロード開始…")
            except Exception as e:
                dump("studio_nofileinput")
                # 添付できなかった＝“完了”でも“手続き済み”でもない＝失敗として扱う
                # （manual にすると集計で成功数に化けるので error で返す）
                return {"ok": False, "error": f"アップロード画面は開けましたが、ファイル添付が自動でできませんでした（{e}）。画面で動画を選んで続けてください。"}

            # 詳細フォーム（タイトル欄）を待つ（1秒刻み＝⏹中断が即効く）
            title_ok = False
            try:
                for _ in range(90):
                    if _stopped():
                        log("    ⏹ 中断要求 → Studioページはこのまま残します"
                            "（続けるなら画面で・やめるなら下書きを破棄してください）。")
                        return dict(_STOP_RES)
                    if page.query_selector('#textbox'):
                        break
                    page.wait_for_timeout(1000)
                else:
                    raise TimeoutError("タイトル欄が90秒で現れませんでした")
                page.wait_for_timeout(2500)

                # ショートはハッシュタグ候補パネル等で #textbox の並び/構造が変わるため、
                # タイトルは**名指し**でのみ検証する（2026-08-10配布先報告①）。
                # 旧index方式（boxes[0]）は「打つだけの入力補助」に格下げ＝誤要素へ打鍵し
                # 同じ誤要素から読み戻すと必ず一致する自己検証になるため、title_ok
                # （自動確定に入る前提）は名指しで読み戻し確認できた時だけ立てる
                TITLE_SEL = 'ytcp-social-suggestions-textbox#title-textarea #textbox'
                DESC_SEL = 'ytcp-social-suggestions-textbox#description-textarea #textbox'

                _norm = lambda s: "".join((s or "").split())
                for attempt in range(3):
                    el = page.query_selector(TITLE_SEL)
                    if el is None:   # 名指し欄の描画がまだ → 待って再取得
                        page.wait_for_timeout(1500)
                        continue
                    try:
                        el.click(timeout=5000); page.wait_for_timeout(300)
                        page.keyboard.press("Control+a"); page.keyboard.press("Delete")
                        page.keyboard.insert_text(title)
                        page.wait_for_timeout(400)
                        el2 = page.query_selector(TITLE_SEL)
                        got = el2.inner_text() if el2 else ""
                    except Exception:
                        got = ""
                    # 読み戻し検証: 打ったつもりでも空振り（detach/フォーカス喪失）だと
                    # ファイル名タイトルのまま進んでしまうため、一致した時だけ成功にする
                    if _norm(got) == _norm(title):
                        title_ok = True
                        log(f"    タイトルを入力: {title}")
                        break
                    log("    タイトルの読み戻しが一致しません → 再入力します…")
                    page.wait_for_timeout(1500)
                if not title_ok:
                    dump("studio_title_fail")
                    # 検証なしの入力補助（自動確定には進まない）: 旧index方式で打つだけ打つ
                    try:
                        bx = page.query_selector_all('#textbox')
                        if bx:
                            bx[0].click(timeout=5000); page.wait_for_timeout(300)
                            page.keyboard.press("Control+a"); page.keyboard.press("Delete")
                            page.keyboard.insert_text(title)
                    except Exception:
                        pass
                    log("    ⚠ タイトルを確認できませんでした（画面で確認してください）。")
                if desc:
                    el = page.query_selector(DESC_SEL)
                    if el is None:
                        bx = page.query_selector_all('#textbox')
                        el = bx[1] if len(bx) > 1 else None
                    if el is not None:
                        el.click(timeout=5000); page.wait_for_timeout(300)
                        page.keyboard.insert_text(desc)
                        log("    説明を入力しました。")
                    else:
                        log("    ⚠ 説明欄が見つかりませんでした。画面で入力してください。")
            except Exception as e:
                log(f"    タイトル/説明の自動入力に失敗（{e}）→ 画面で入力してください。")

            # 子ども向けではない（自動でできなかった時も黙らない＝無言スキップ禁止）
            if not click_first(['tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"]',
                                'tp-yt-paper-radio-button:has-text("いいえ")',
                                'tp-yt-paper-radio-button:has-text("子ども向けではありません")']):
                log("    ⚠ 「子ども向けではない」を自動選択できませんでした。画面で選択してください。")

            # サムネ: サムネuploaderを**名指し**で取得する。
            # 旧実装の「file inputが2個以上なら最後」は誤り＝動画添付後はアップロード用の
            # inputがDOMから消え、残るのはサムネ用の1個だけ（実測 #file-loader）。
            # 個数条件が常にFalseで**無言でスキップ**されていた（2026-08-05配布先報告・不具合2）
            if thumb and Path(thumb).exists():
                try:
                    try:  # 詳細画面が組み上がるまで待つ（タイトル入力直後は際どい）
                        page.wait_for_selector("ytcp-thumbnail-uploader",
                                               timeout=8000, state="attached")
                    except Exception:
                        pass
                    # ※「ページ内最後のinput[type=file]」の広域フォールバックは廃止
                    #   （ショート等でuploader不在のとき無関係なinputへ流し込む危険・2026-08-10）
                    el = (page.query_selector('ytcp-thumbnail-uploader input[type="file"]')
                          or page.query_selector('#file-loader'))
                    if el:
                        _attach_file(page, el, str(Path(thumb).resolve()),
                                     pick="last", log=log)
                        page.wait_for_timeout(2000)
                        log("    サムネを設定しました。")
                    else:
                        dump("studio_nothumb")
                        log("    ⚠ サムネ入力欄が見つかりませんでした。画面で設定してください。")
                except Exception as e:
                    log(f"    ⚠ サムネの自動設定に失敗（{e}）。画面で設定してください。")

            # タグ: 「詳細設定を表示（すべて表示）」の中にある。
            # 旧実装はtagsを読み出すだけで入力コードが無かった（デッドコード＝
            # 2026-08-05配布先報告・不具合1）
            if tags:
                try:
                    click_first(['button[aria-label="詳細設定を表示"]',
                                 'button[aria-label*="詳細設定"]',
                                 'ytcp-button:has-text("すべて表示")',
                                 'button:has-text("すべて表示")',
                                 'button:has-text("Show more")'])
                    page.wait_for_timeout(1500)
                    el = (page.query_selector('ytcp-form-input-container#tags-container input')
                          or page.query_selector('#tags-container #text-input')
                          or page.query_selector('#tags-container input')
                          or page.query_selector('input[aria-label*="タグ"]')
                          or page.query_selector('input[placeholder*="タグ"]'))
                    if el:
                        el.click(); page.wait_for_timeout(300)
                        # Studioのタグ欄は**カンマの打鍵**で1個ずつチップに確定する仕様。
                        # 文字列を一括挿入すると後半が未確定の生テキストのまま残る
                        # （2026-08-06実機確認: 4個だけチップ化・残りが入力欄に残留）
                        # → 1個ずつ insert_text → 「,」キー押下で確定させる。重複は除去
                        seen, n_in, total = set(), 0, 0
                        for tg in tags[:30]:
                            tg = (tg or "").strip().replace(",", " ").strip()
                            if not tg or tg in seen:
                                continue
                            if total + len(tg) + 1 > 480:  # Studioは合計500字まで
                                break
                            page.keyboard.insert_text(tg)
                            page.keyboard.press(",")
                            page.wait_for_timeout(150)
                            seen.add(tg)
                            n_in += 1
                            total += len(tg) + 1
                        page.wait_for_timeout(400)
                        log(f"    タグを入力しました（{n_in}件・1個ずつ確定）。")
                    else:
                        dump("studio_notags")
                        log("    ⚠ タグ入力欄が見つかりませんでした。"
                            "画面の「すべて表示」から入力してください。")
                except Exception as e:
                    log(f"    ⚠ タグの自動入力に失敗（{e}）。画面で入力してください。")

            dump("studio_filled")
            when = meta.get("scheduled_publish_at", "")
            # 限定公開/すぐ公開はYouTube仕様上「予約」できない（Studioのスケジュール設定=
            # 非公開→予約時刻に公開のみ）。予約日時があるとprivacyを無視して公開予約で
            # 確定していた（2026-08-17配布先報告④⑤）→ 日時を使わず公開範囲で即時確定へ
            _priv = (cfg.get("youtube_privacy") or "private").lower()
            if when and _priv != "private":
                log(f"    公開のしかたが「{'限定公開' if _priv == 'unlisted' else 'すぐ公開'}」"
                    "のため、予約日時は使わず即時その公開状態で確定します"
                    "（予約したい場合は「非公開（予約はこれ）」を選択）。")
                when = ""
            if title_ok:
                log("    ✅ 動画・タイトル・説明・タグ・サムネ・子ども向け設定まで自動入力しました。")
            else:
                log("    ⚠ タイトル以外の項目は入力しました（タイトルは未確認）。")

            # 予約の確定まで全自動（2026-08-06方針転換。OFFにすると従来の手動引き渡し）。
            # タイトルが確認できていない時は自動確定しない＝ファイル名タイトルのまま
            # 予約が確定する事故を防ぐ（2026-08-10配布先報告①のフェイルクローズ）。
            # タイトルが「無題（の動画）」＝自動生成に失敗した動画も確定しない（2026-08-17）
            _title_bad = (meta.get("title") or "").strip() in ("", "無題", "無題の動画")
            if bool(cfg.get("studio_auto_publish", True)):
                if _title_bad:
                    log("    ⚠ タイトルが自動生成できていない（無題）ため自動確定はしません → "
                        "画面でタイトルを付けてから確定してください"
                        "（投稿タブのタイトル欄で編集→💾でも直せます）。")
                elif not title_ok:
                    log("    ⚠ タイトルを確認できていないため自動確定はしません → "
                        "画面でタイトルを入力してから、下の手順で確定してください。")
                else:
                    log("    予約の確定まで自動で進めます…"
                        + (f"（予約日時: {when}）" if when else "（公開設定で確定）"))
                    res = _finish_publish(page, cfg, when, log, dump)
                    if res is not None:
                        return res
                    log("    → 自動確定できなかったため、最後の操作を画面へ引き渡します。")

            log("    ─────────────────────────────────")
            log("    ⚠ ここから先はブラウザ画面で【あと3ステップ】:")
            log("      ① 各ステップを『次へ』で進める（チェック中でも進めます。")
            log("        収益化チャンネルは 収益化=オン と 広告の適合性の送信 もここで）")
            log(f"      ② 公開設定/予約日時を選ぶ（希望: {when or '未指定'}）")
            log("      ③ 『公開』または『スケジュール設定』を押す")
            log("    ─────────────────────────────────")
            return {"ok": False, "manual": True,
                    "note": "詳細まで自動入力しました。ブラウザで各ステップを確認し"
                            "（収益化チャンネルは収益化=オンと広告の適合性も）、"
                            "公開設定・予約を選んで最後の『公開/スケジュール』を押してください。"}
        except Exception as e:
            dump("studio_error")
            return {"ok": False, "error": f"Studio操作エラー: {e}. ブラウザで続けて操作してください。"}
        # ページは開いたまま（ユーザーが続きを操作）
