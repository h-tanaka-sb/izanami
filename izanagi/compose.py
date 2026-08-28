"""④合成: 画像/動画クリップ列 ＋ ナレーション音声 ＋ ASSテロップ（＋BGM）→ final.mp4。

エンコーダ選択・音声処理は ★ムービーデスク export.py / TikTok render.py 準拠。
ASSパスのWindowsエスケープ問題は「compose ディレクトリを cwd にして相対パス subs.ass を渡す」で回避。
- 画像1枚のみ      : ループ1パス（最速）
- 画像複数 / 動画クリップ有: 基礎動画(base.mp4)を組んでから ASS焼込＋音声の最終パス
"""
from __future__ import annotations

from pathlib import Path

from . import util

# BGMの平坦化チェーン（2026-07-30・全曲共通）:
# 20dB持ち上げてリミッター(-20dBFS)に当て、level=1で天井まで自動レベル
# ＝曲の静かな導入も盛り上がりも一定レベル（約-4.6LUFS）に揃う。
# 実測: FXチャンネル01で揺れ幅7.6dB→1.7dB。この後に volume=(BGM音量dB-17)dB を
# かけて基準（0dB時≈-22LUFS）へ落とす。通常合成と猫ミームの両方で使う。
BGM_FLATTEN = "volume=20dB,alimiter=limit=0.1:attack=5:release=500:level=1"


def media_duration(path) -> float:
    try:
        pr = util.run(
            [util.find_ffprobe(), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        return float((pr.stdout or "0").strip() or 0.0)
    except Exception:
        return 0.0


def _vf_fit(w: int, h: int) -> str:
    return (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1")


def ass_vf(ass_name: str) -> str:
    """ass=フィルタ（同梱フォントフォルダ直指定つき）。

    2026-08-19配布先報告「和風毛筆で漢字が歯抜け」対応: 配布先PCに同名の古い毛筆
    フォント（漢字収録が少ない版）が入っていると、ユーザーフォント登録よりそちらが
    優先解決されて無い字が空白で焼かれることがある。fontsdirで同梱assets/fontsを
    直接使わせれば、PCのフォント事情やインストール成否に依存しない。
    パスはffmpegフィルタ引数用に \\ と : をエスケープする。"""
    from pathlib import Path as _P
    fdir = _P(__file__).resolve().parent.parent / "assets" / "fonts"
    if not fdir.exists():
        return f"ass={ass_name}"
    esc = str(fdir).replace("\\", "/").replace(":", r"\:")
    return f"ass={ass_name}:fontsdir='{esc}'"


def _has_audio_stream(path) -> bool:
    try:
        pr = util.run(
            [util.find_ffprobe(), "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        return "audio" in (pr.stdout or "")
    except Exception:
        return False


OPED_JSON = "oped.json"          # compose/ に置く「OP/EDを付けたか」の記録
BODY_MP4 = "final_body.mp4"      # compose/ に残す本編（OP/ED連結前）＝後からEDだけ付け直せる


def write_oped_record(final_mp4, data: dict) -> None:
    """compose/oped.json を書く（失敗しても本体を止めない）。"""
    try:
        from datetime import datetime as _dt
        rec = dict(data)
        rec["at"] = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
        util.save_json_atomic(Path(final_mp4).parent / OPED_JSON, rec)
    except Exception:
        pass


def read_oped_record(final_mp4) -> dict | None:
    """compose/oped.json を読む（無ければ None＝この機能より前に作られた動画）。"""
    try:
        p = Path(final_mp4).parent / OPED_JSON
        if not p.exists():
            return None
        d = util.load_json(p, None)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def wrap_with_intro_outro(final_mp4, intro: str, outro: str, settings: dict,
                          log=print, stop=None, body=None) -> dict:
    """完成動画の前後にOP/ED動画を連結する（チャンネルの「冒頭・終了」機能）。

    - OP/EDは本編と同じ解像度/fps/SARへ正規化して concat フィルタで1パス連結
      （デマルチプレクサ連結はストリーム条件が揃わないと壊れるため使わない）。
    - 音声はOP/ED自身の音をそのまま使用（無ければ無音）。本編のテロップ/BGMは
      本編区間に焼き込み済みのため位置ズレは起きない。
    - 出力は一時ファイルへ書き、成功時のみ final.mp4 へ昇格（壊れ動画を完成扱いしない）。
    - 成功時、連結前の本編は compose/final_body.mp4 に残し、結果を compose/oped.json に記録
      （後から「終了画面だけ付け直す」ため・2026-08-20配布先要望）。
    - body= を渡すと、その本編ファイルを使って final_mp4 を作り直す（付け直し用。
      本編は移動しない）。
    """
    final_mp4 = Path(final_mp4)
    body_path = Path(body) if body else None
    clips, missing = [], []
    for label, p in (("OP", intro), ("ED", outro)):
        p = (p or "").strip()
        if not p:
            continue
        if not Path(p).exists():
            log(f"    ⚠ {label}動画が見つからないためスキップ: {p}")
            missing.append({"label": label, "path": p})
            continue
        clips.append((label, str(Path(p).resolve())))
    if not clips:
        if body_path is None:   # 通常合成: 「付いていない」ことを記録（付け直しの判定に使う）
            write_oped_record(final_mp4, {"attached": False, "intro": "", "outro": "",
                                          "missing": missing, "body": "",
                                          "reason": "missing" if missing else "none"})
        return {"ok": True, "skipped": True, "missing": missing}

    w = int(settings.get("video_width", 1920)); h = int(settings.get("video_height", 1080))
    fps = int(settings.get("video_fps", 30)); q = int(settings.get("enc_quality", 20))
    ffmpeg = util.find_ffmpeg(); enc = util.pick_encoder(ffmpeg, settings)

    order = []  # (path, has_audio) を再生順に
    for label, p in clips:
        if label == "OP":
            order.append((p, _has_audio_stream(p)))
    order.append((str((body_path or final_mp4).resolve()), True))  # 本編（音声あり）
    for label, p in clips:
        if label == "ED":
            order.append((p, _has_audio_stream(p)))

    inputs = ["-y", "-hide_banner"]
    filters = []
    concat_in = ""
    n_real = len(order)
    for _i, (p, _ha) in enumerate(order):
        inputs += ["-i", p]
    extra_idx = n_real  # 無音クリップ用の anullsrc はクリップごとに1本（ラベル二重消費防止）
    for i, (p, ha) in enumerate(order):
        filters.append(f"[{i}:v]{_vf_fit(w, h)},fps={fps}[v{i}]")
        if ha:
            filters.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]")
        else:
            dur = max(0.1, media_duration(p))
            inputs += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
            filters.append(f"[{extra_idx}:a]asetpts=PTS-STARTPTS[a{i}]")
            extra_idx += 1
        concat_in += f"[v{i}][a{i}]"
    fc = ";".join(filters) + f";{concat_in}concat=n={n_real}:v=1:a=1[v][a]"

    out_tmp = final_mp4.with_name("final_oped_wip.mp4")
    cmd = [ffmpeg, *inputs, "-filter_complex", fc, "-map", "[v]", "-map", "[a]",
           *util.enc_args(enc, q), "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
           str(out_tmp.resolve())]
    total = sum(media_duration(p) for p, _ in order)
    logf = final_mp4.parent / "ffmpeg_oped.log"
    log(f"    冒頭・終了の動画を連結中（{'+'.join(l for l, _ in clips)}＋本編）…")
    with open(logf, "wb") as fl:
        rc = util.run_watched(cmd, timeout_sec=max(900, total * 6), stop=stop,
                              stdout=fl, stderr=fl, cwd=str(final_mp4.parent))
    res = util.probe_ok(out_tmp)
    res["returncode"] = rc
    if res.get("ok"):
        import os as _os
        body_kept = ""
        moved_body = False
        if body_path is None:
            # 連結前の本編を残す（後からEDだけ付け直せるように）。残せなくても連結は成立
            try:
                _os.replace(final_mp4, final_mp4.with_name(BODY_MP4))
                body_kept = BODY_MP4
                moved_body = True
            except Exception as e:
                log(f"    （本編の控え final_body.mp4 は残せませんでした: {str(e)[:60]}）")
        else:
            body_kept = body_path.name if body_path.parent == final_mp4.parent else str(body_path)
        try:
            _os.replace(out_tmp, final_mp4)
        except Exception as e:
            # 昇格できない（final.mp4 を他アプリが開いている等）→ 本編を元の位置へ戻して失敗扱い
            # （final.mp4 が消えた状態を残さない）
            if moved_body:
                try:
                    _os.replace(final_mp4.with_name(BODY_MP4), final_mp4)
                except Exception:
                    pass
            try:
                out_tmp.unlink(missing_ok=True)
            except Exception:
                pass
            log("    ⚠ 連結した動画を final.mp4 に置けませんでした（動画プレイヤー等で開いていませんか？）。"
                f"本編のみで完成にします（{str(e)[:60]}）。")
            if body_path is None:
                write_oped_record(final_mp4, {"attached": False, "intro": intro or "", "outro": outro or "",
                                              "missing": missing, "body": "", "reason": "replace_failed"})
            res["ok"] = False
            res["error"] = "final.mp4 を書き換えられませんでした（他のアプリで開かれている可能性）"
            return res
        used = {l: p for l, p in clips}
        write_oped_record(final_mp4, {
            "attached": True, "intro": used.get("OP", ""), "outro": used.get("ED", ""),
            "missing": missing, "body": body_kept,
            "intro_dur": round(media_duration(used["OP"]), 3) if "OP" in used else 0.0,
            "outro_dur": round(media_duration(used["ED"]), 3) if "ED" in used else 0.0,
            "total": round(float(res.get("duration") or 0), 3)})
        log(f"    冒頭・終了を連結しました（{'+'.join(l for l, _ in clips)}・"
            f"全体 {float(res.get('duration') or 0):.1f}秒）。")
    else:
        if body_path is None:
            write_oped_record(final_mp4, {"attached": False, "intro": intro or "", "outro": outro or "",
                                          "missing": missing, "body": "", "reason": "failed"})
        try:
            out_tmp.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            res["log_tail"] = logf.read_text(encoding="utf-8", errors="replace")[-1200:]
        except Exception:
            pass
        log("    ⚠ 冒頭・終了の連結に失敗したため、本編のみで完成にします"
            f"（ログ: {logf.name}）。")
    return res


def _kenburns_vf(w: int, h: int, fps: int, dur: float, variant: int) -> str:
    """静止画に動きを付ける Ken Burns フィルタ（ズームイン/アウト/左右パンの輪番）。

    2倍プリスケールがサブピクセルジッタ防止の要。zoompan は s=WxH で完全一致出力。
    """
    n = max(2, int(dur * fps))
    center = "x='(iw-iw/zoom)/2':y='(ih-ih/zoom)/2'"
    v = variant % 4
    if v == 0:    # ズームイン
        z = f"z='min(1+0.10*on/{n},1.10)':{center}"
    elif v == 1:  # ズームアウト
        z = f"z='max(1.10-0.10*on/{n},1.0)':{center}"
    elif v == 2:  # 左→右パン
        z = f"z=1.08:x='(iw-iw/zoom)*on/{n}':y='(ih-ih/zoom)/2'"
    else:         # 右→左パン
        z = f"z=1.08:x='(iw-iw/zoom)*(1-on/{n})':y='(ih-ih/zoom)/2'"
    return (f"scale={w * 2}:-2,zoompan={z}:d=1:s={w}x{h}:fps={fps},setsar=1")


def _build_base_video(visuals: list[dict], total: float, out_path: Path,
                      settings: dict, log=print) -> Path | None:
    """visuals=[{kind:'image'|'clip', path}] を順に並べた無音の基礎動画を作る。

    クリップは元の長さ、画像は「残り時間 ÷ 画像枚数」で配分。合計が total に近づくよう調整。
    """
    import math
    import subprocess as _sp

    w = int(settings.get("video_width", 1920)); h = int(settings.get("video_height", 1080))
    fps = int(settings.get("video_fps", 30)); q = int(settings.get("enc_quality", 20))
    kb = bool(settings.get("kenburns", True))
    fmc = float(settings.get("first_minute_max_cut", 8))
    ffmpeg = util.find_ffmpeg(); enc = util.pick_encoder(ffmpeg, settings)
    seg_dir = out_path.parent / "_segments"; seg_dir.mkdir(parents=True, exist_ok=True)

    clips = [v for v in visuals if v.get("kind") == "clip" and Path(v["path"]).exists()]
    images = [v for v in visuals if v.get("kind") == "image" and Path(v["path"]).exists()]
    clip_total = sum(media_duration(c["path"]) for c in clips)
    remain = max(0.0, total - clip_total)
    per_img = (remain / len(images)) if images else 0.0
    if not clips and images:  # 画像のみ等分
        per_img = max(0.5, total / len(images))
    log(f"    構成: 動画{len(clips)}本(先頭・計{clip_total:.0f}秒) → "
        f"画像{len(images)}枚(残り{remain:.0f}秒を均等割={per_img:.1f}秒/枚)"
        + ("・Ken Burnsズームあり" if kb and images else ""))

    def _run_seg(cmd, timeout, cwd=None):  # timeout付きで実行（ハング1本で夜間全停止しない）
        try:
            pr = util.run(cmd, capture_output=True, timeout=timeout, cwd=cwd)
            return pr.returncode == 0
        except _sp.TimeoutExpired:
            log("    ⚠ セグメント生成がタイムアウト（スキップして続行）")
            return False

    seg_paths: list[Path] = []
    idx = 0
    for c in clips:  # クリップ（無音化・尺はそのまま、ただし total を超えたら切る）
        seg = seg_dir / f"seg_{idx:03d}.mp4"; idx += 1
        cd = media_duration(c["path"])
        t = min(cd, total) if total else cd
        cmd = [ffmpeg, "-y", "-hide_banner", "-i", str(c["path"]),
               "-t", f"{max(0.5, t):.3f}", "-an", "-vf", f"{_vf_fit(w, h)},fps={fps}",
               *util.enc_args(enc, q), "-pix_fmt", "yuv420p", str(seg)]
        if _run_seg(cmd, max(300, t * 10)) and seg.exists():
            seg_paths.append(seg)

    # 画像: Ken Burns輪番。冒頭60秒はカットを細かく割って画面が死なないようにする
    pos = clip_total
    variant = 0
    for img_i, im in enumerate(images):
        log(f"      …セグメント {img_i + 1}/{len(images)}")
        if per_img <= 0:
            break
        cuts = 1
        if kb and pos < 60 and per_img > fmc:
            cuts = max(1, math.ceil(per_img / fmc))
        cut_dur = per_img / cuts
        for _ in range(cuts):
            seg = seg_dir / f"seg_{idx:03d}.mp4"; idx += 1
            if kb:
                vf = _kenburns_vf(w, h, fps, cut_dur, variant)
                cmd = [ffmpeg, "-y", "-hide_banner", "-loop", "1", "-framerate", str(fps),
                       "-t", f"{cut_dur:.3f}", "-i", str(im["path"]), "-vf", vf,
                       "-t", f"{cut_dur:.3f}",
                       *util.enc_args(enc, q), "-pix_fmt", "yuv420p", str(seg)]
            else:
                cmd = [ffmpeg, "-y", "-hide_banner", "-loop", "1", "-t", f"{cut_dur:.3f}",
                       "-i", str(im["path"]), "-vf", f"{_vf_fit(w, h)},fps={fps}",
                       *util.enc_args(enc, q), "-pix_fmt", "yuv420p", str(seg)]
            ok = _run_seg(cmd, max(120, cut_dur * 10)) and seg.exists()
            if not ok and kb:  # zoompanが環境依存で失敗したら静止画で保険
                cmd = [ffmpeg, "-y", "-hide_banner", "-loop", "1", "-t", f"{cut_dur:.3f}",
                       "-i", str(im["path"]), "-vf", f"{_vf_fit(w, h)},fps={fps}",
                       *util.enc_args(enc, q), "-pix_fmt", "yuv420p", str(seg)]
                ok = _run_seg(cmd, max(120, cut_dur * 10)) and seg.exists()
            if ok:
                seg_paths.append(seg)
            variant += 1
            pos += cut_dur
    if not seg_paths:
        return None
    listf = seg_dir / "list.txt"
    listf.write_text("".join(f"file '{p.name}'\n" for p in seg_paths), encoding="utf-8")
    cmd = [ffmpeg, "-y", "-hide_banner", "-f", "concat", "-safe", "0",
           "-i", "list.txt", "-c", "copy", str(out_path.resolve())]
    if not _run_seg(cmd, 600, cwd=str(seg_dir)):
        return None
    return out_path if out_path.exists() else None


def video_stream_duration(path) -> float:
    """mp4の**映像ストリーム**の長さ（秒）。取れなければ0。

    probe_okのduration=コンテナ長（＝長い方の音声）なので、
    「映像だけ早く尽きた不良品」はそれでは見抜けない（2026-08-05配布先報告）。"""
    try:
        import json as _j
        pr = util.run([util.find_ffprobe(), "-v", "error", "-select_streams", "v:0",
                       "-show_entries", "stream=duration", "-of", "json", str(path)],
                      capture_output=True, timeout=120)
        d = _j.loads((pr.stdout or b"").decode("utf-8", "replace") or "{}")
        return float((d.get("streams") or [{}])[0].get("duration") or 0)
    except Exception:
        return 0.0


def compose(images: list[str], voice_wav: str, ass_path: str, out_mp4: str,
            settings: dict, clips: list[str] | None = None, bgm_path: str | None = None,
            duration: float | None = None, log=print, stop=None) -> dict:
    """合成のメイン。戻り値: util.probe_ok の結果 dict（"ok": True/False）。"""
    w = int(settings.get("video_width", 1920)); h = int(settings.get("video_height", 1080))
    fps = int(settings.get("video_fps", 30)); q = int(settings.get("enc_quality", 20))
    ffmpeg = util.find_ffmpeg(); enc = util.pick_encoder(ffmpeg, settings)

    out_mp4 = Path(out_mp4); out_mp4.parent.mkdir(parents=True, exist_ok=True)
    compose_dir = Path(ass_path).resolve().parent
    ass_name = Path(ass_path).name

    dur = duration if duration else media_duration(voice_wav)
    if dur <= 0:
        dur = 1.0

    images = [im for im in (images or []) if im and Path(im).exists()]
    clips = [c for c in (clips or []) if c and Path(c).exists()]
    log(f"    合成: 画像{len(images)}枚 / 動画{len(clips)}本 / 音声{dur:.1f}秒 / enc={enc}")

    inputs = ["-y", "-hide_banner"]
    simple = (not clips) and len(images) <= 1
    if simple:
        if images:
            # -framerate必須: 無いとimageデマクサ既定25fpsで入力され、kenburns(zoompan d=1)は
            # フレームを水増ししないため映像が音声の25/30=83.3%で尽きる＝終盤のテロップ消失・
            # 映像静止（2026-08-05配布先報告・実測frame数まで一致）。216行の複数枚パスと同じ形に
            inputs += ["-loop", "1", "-framerate", str(fps), "-t", f"{dur:.3f}",
                       "-i", str(Path(images[0]).resolve())]
        else:
            inputs += ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", f"color=c=black:s={w}x{h}:r={fps}"]
    else:
        visuals = [{"kind": "clip", "path": c} for c in clips] + \
                  [{"kind": "image", "path": im} for im in images]
        base = compose_dir / "base.mp4"
        if _build_base_video(visuals, dur, base, settings, log) is None:
            return {"ok": False, "error": "基礎動画の作成に失敗"}
        inputs += ["-i", str(base.resolve())]

    audio_idx = 1
    inputs += ["-i", str(Path(voice_wav).resolve())]
    have_bgm = bool(bgm_path and Path(bgm_path).exists())
    if have_bgm:
        inputs += ["-stream_loop", "-1", "-i", str(Path(bgm_path).resolve())]

    if simple and images and settings.get("kenburns", True):
        # 画像1枚でもゆっくりズームで画面が死なないように
        vchain = f"[0:v]{_kenburns_vf(w, h, fps, dur, 0)},{ass_vf(ass_name)}[v]"
    else:
        vchain = f"[0:v]{_vf_fit(w, h)},fps={fps},{ass_vf(ass_name)}[v]"
    if have_bgm:
        # 曲別の音量(dB)がBGMライブラリに設定されていれば全体設定より優先
        _db = settings.get("_bgm_gain_db")
        if _db is None:
            _db = settings.get("bgm_base_db", -8)
        # BGMは混ぜる前に平坦化する（2026-07-30・ダッキング廃止）。
        # 旧sidechaincompressは「セリフの切れ目ごとにBGMが膨らみ、話すと沈む」
        # ポンピングの原因だった（実測: 揺れ幅15dB）。曲自体の静かな導入/盛り上がり
        # （実測7.6dB差）もリミッターで一定に均し、固定音量でナレの下に敷く
        # （新チェーン実測: 揺れ幅1.7dB）。BGM音量(dB)=0で約-22LUFS、
        # 既定-8dBで約-30LUFS＝ナレーションの10〜14LU下（放送の推奨レンジ）。
        bgm_post_db = float(_db) - 17.0
        achain = (
            f"[{audio_idx}:a]aresample=48000,aformat=channel_layouts=stereo[voicemix];"
            f"[{audio_idx+1}:a]aresample=48000,aformat=channel_layouts=stereo,"
            f"{BGM_FLATTEN},volume={bgm_post_db:.1f}dB[bgmv];"
            f"[voicemix][bgmv]amix=inputs=2:normalize=0:dropout_transition=0,"
            # loudnormは内部192kHzで出力するため48kへ戻す（戻さないとAACが96kHz化する）
            f"loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[a]"
        )
        filter_complex = vchain + ";" + achain
        amap = "[a]"
    else:
        filter_complex = vchain
        amap = f"{audio_idx}:a"

    # final.mp4 は④完了のsentinel。直接書くと、⏹中断/watchdog kill/ffmpeg失敗で残った
    # 壊れmp4が「完成」と誤認され、再開スキップ→投稿まで素通りする（2026-07-16監査 #3）
    # → 一時ファイルに書き、probe成功時にだけ本番パスへ昇格させる
    out_tmp = out_mp4.with_name(out_mp4.stem + "_wip" + out_mp4.suffix)
    cmd = [ffmpeg, *inputs, "-filter_complex", filter_complex,
           "-map", "[v]", "-map", amap, *util.enc_args(enc, q), "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-t", f"{dur:.3f}",
           "-movflags", "+faststart", str(out_tmp.resolve())]

    logf = compose_dir / "ffmpeg.log"
    with open(logf, "wb") as fl:
        # ウォッチドッグ付き: NVENCハング等で無期限に固まらない（尺の6倍か15分の長い方）
        rc = util.run_watched(cmd, timeout_sec=max(900, dur * 6), stop=stop,
                              stdout=fl, stderr=fl, cwd=str(compose_dir))
    if rc == -9:
        log("    ⚠ 最終書き出しをウォッチドッグが打ち切りました（ハング/中断）。")

    res = util.probe_ok(out_tmp)
    res["returncode"] = rc; res["log"] = str(logf)
    if res.get("ok"):
        # 完成検査: 映像トラックが音声より早く尽きていないか。尽きていると終盤は
        # 映像静止＋テロップ全消失なのに probe_ok（コンテナ長）は通ってしまい、
        # ok:true の不良品が投稿まで素通りする（2026-08-05配布先報告＝3本全滅・警告ゼロ）
        vdur = video_stream_duration(out_tmp)
        if vdur and (dur - vdur) > 2.0:
            res = {"ok": False, "returncode": rc, "log": str(logf),
                   "error": (f"映像({vdur:.1f}秒)が音声({dur:.1f}秒)より{dur - vdur:.1f}秒短く、"
                             "終盤のテロップ・映像が欠ける不良品のため失敗にしました")}
            try:  # 原因調査用に別名で残す（final.mp4=完成の印にはしない）
                import os as _os
                _os.replace(out_tmp, out_mp4.with_name(out_mp4.stem + "_bad" + out_mp4.suffix))
            except Exception:
                pass
            return res
    if res.get("ok"):
        import os as _os
        _os.replace(out_tmp, out_mp4)  # 成功した時だけ final.mp4（=完成の印）が現れる
        # 中間生成物はここで役目を終える。残すと1本あたり動画2本分（数百MB〜GB）が
        # プロジェクトに恒久的に積み上がる（失敗時は原因調査用に残す）
        try:
            import shutil as _sh
            _sh.rmtree(compose_dir / "_segments", ignore_errors=True)
            (compose_dir / "base.mp4").unlink(missing_ok=True)
        except Exception:
            pass
    else:
        try:
            out_tmp.unlink(missing_ok=True)  # 壊れた書きかけを「完成」と誤認させない
        except Exception:
            pass
        try:
            res["log_tail"] = logf.read_text(encoding="utf-8", errors="replace")[-1500:]
        except Exception:
            pass
    return res
