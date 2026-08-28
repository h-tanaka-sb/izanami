"""プロジェクトフォルダ＝状態そのもの。

各ステージは sentinel 成果物の有無で skip/run を判定する（★Bダッシュ の .noted 思想を
ステージ別 sentinel に拡張）。部分的な sentinel は絶対に書かない（失敗時は _debug へ診断のみ）。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from . import util

# 「作成」パイプラインの実行順（投稿は含めない＝別操作）。
# 投稿(upload)は pipeline.upload_project() で独立して行う。
STAGES = ["script", "tts", "visuals", "compose", "project"]

STAGE_LABEL = {
    "script": "①台本生成",
    "tts": "②ナレーション音声",
    "visuals": "③映像・画像生成",
    "compose": "④合成（テロップ/BGM）",
    "project": "⑤プロジェクト保存・サムネ",
    "upload": "⑥YouTube予約投稿",
}

SENTINEL = {
    "script": "script/script.txt",
    "tts": "audio/voice.wav",
    "visuals": "visuals/manifest.json",
    "compose": "compose/final.mp4",
    "project": "memo.json",
    "upload": "uploaded.flag",
}

SUBDIRS = ["script", "audio", "visuals", "compose", "thumb", "_debug"]


def topic_slug(run_cfg: dict | None) -> str:
    """フォルダ名に付けるお題スラッグ（2026-08-19ユーザー要望「数字だけだと
    どのフォルダか分からない」）。お題1行目から、Windowsで使えない文字・空白・
    ドット・**%**（%dがあるとffmpeg 7.1系のimage2が連番パターンと誤解釈して
    画像を開けない＝敵対検証で実機再現）を除去して16字まで。"""
    if not run_cfg:
        return ""
    import re as _re
    _lines = (run_cfg.get("topic") or "").strip().splitlines()
    return _re.sub(r'[\\/:*?"<>|%\s.]+', "", _lines[0].strip() if _lines else "")[:16]


class Project:
    """1本の動画＝1フォルダ。パス解決と sentinel 判定を集約する。"""

    def __init__(self, base: Path):
        self.base = Path(base)

    # ── 生成/オープン ──
    @classmethod
    def create(cls, output_dir, run_cfg: dict | None = None) -> "Project":
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        slug = topic_slug(run_cfg)
        name = f"IZANAMI_{stamp}_{slug}" if slug else f"IZANAMI_{stamp}"
        base = Path(output_dir) / name
        n = 1
        while base.exists():
            base = Path(output_dir) / f"{name}_{n}"
            n += 1
        p = cls(base)
        p.ensure_dirs()
        if run_cfg is not None:
            p.save_run_cfg(run_cfg)
        return p

    @classmethod
    def open(cls, base) -> "Project":
        p = cls(base)
        p.ensure_dirs()
        return p

    def ensure_dirs(self) -> None:
        self.base.mkdir(parents=True, exist_ok=True)
        for d in SUBDIRS:
            (self.base / d).mkdir(parents=True, exist_ok=True)

    # ── パス ──
    def path(self, rel: str) -> Path:
        return self.base / rel

    def sentinel(self, stage: str) -> Path:
        return self.base / SENTINEL[stage]

    def done(self, stage: str) -> bool:
        s = self.sentinel(stage)
        try:
            return s.exists() and s.stat().st_size > 0
        except Exception:
            return False

    # ── run_cfg（再開時に同条件で続けるための非機密パラメータ） ──
    # ※ APIキーは保存しない（_RUN_KEYS に含めない）
    _RUN_KEYS = (
        "topic", "script_source", "video_mode", "script_format", "video_orientation",
        "script_instruction", "video_length_min",
        # 内容系（チャンネルの世界観・枚数）: ♻再開時に「同条件で続ける」ため保存する
        # （2026-07-16監査 K4: これが無いと再開時にアクティブchが違うだけで
        #   別の世界観プロンプト/枚数で続きが作られていた）
        "script_prompt", "topic_prompt", "brushup_prompt", "gemini_tts_style",
        "visual_prompt_template", "thumbnail_prompt",
        "intro_video", "outro_video", "intro_text", "outro_text",
        "outro_text_shorts",   # ショート用しめ誘導文（未登録だと♻再開でch混線・2026-08-19）
        "title_prefix", "desc_footer", "fixed_tags",
        "num_images", "num_video_clips",
        "script_engine", "brushup_engine", "tts_engine", "visual_engine",
        "video_engine", "thumb_engine", "upload_engine",
        "tts_voice", "tts_speaking_rate", "tts_lang",
        "telop_preset", "telop_font", "telop_fontsize", "telop_max_chars",
        "telop_primary_color", "telop_outline_color", "telop_outline", "telop_shadow",
        "telop_border_style", "telop_back_color", "dialog_voices", "voice_by_gender",
        "video_width", "video_height", "video_fps",
        "bgm_mode", "bgm_selected",
        "neko_sec_per_char", "neko_min_sec", "neko_bgm_db",
        "neko_meme_db", "neko_se_db", "neko_break_sec",
        "youtube_privacy", "youtube_category_id", "desc_chapters",
    )

    def save_run_cfg(self, cfg: dict) -> None:
        data = {k: cfg.get(k) for k in self._RUN_KEYS if k in cfg}
        util.save_json_atomic(self.base / "run_cfg.json", data)

    def load_run_cfg(self) -> dict:
        return util.load_json(self.base / "run_cfg.json", {}) or {}

    # ── 進捗 ──
    def progress(self) -> tuple[int, int]:
        """(総ステージ数, 完了ステージ数) を返す。再開ダイアログ用。"""
        done = sum(1 for s in STAGES if self.done(s))
        return len(STAGES), done

    def debug_path(self, name: str) -> Path:
        return self.base / "_debug" / name

    def write_debug(self, name: str, text: str) -> None:
        try:
            (self.base / "_debug").mkdir(parents=True, exist_ok=True)
            (self.base / "_debug" / name).write_text(text, encoding="utf-8")
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════
# 作り直し（既存の素材を再利用して、選んだ工程だけ作り直す）
# ══════════════════════════════════════════════════════════════════
# どのステージを作り直すと、どのステージまで作り直しが必要になるか。
# ※ STAGES の並び順で「以降すべて」にはしない：音声を作り直しても画像は
#   そのまま使える（画像は台本＝絵コンテ由来で、音声には依存しないため）。
#   ここを順序任せにすると、声を変えるだけで画像を全部作り直して課金・時間を無駄にする。
REMAKE_DEPS = {
    "script":  ["script", "tts", "visuals", "compose", "project"],
    "tts":     ["tts", "compose", "project"],
    "visuals": ["visuals", "compose", "project"],
    "compose": ["compose", "project"],
    "project": ["project"],
}

# 各ステージを「未完了」に戻すために消すもの（相対パス／末尾 / はフォルダ・* はglob）。
# sentinel だけ消すと中途半端な成果物が再利用されるので、その工程の生成物ごと消す。
_REMAKE_FILES = {
    # 読み仮名マップは台本専用＝台本を作り直すなら一緒に無効化する
    # （残すと前の台本の読みが新しい台本の音声に当たる）
    "script": ["script/script.txt", "script/script_raw.txt", "script/brushup.txt",
               "script/meta.json", "script/policy_report.json", "script/reading_map.json",
               # 絵コンテ・画像プロンプトは台本由来 → 台本を作り直すなら作り直す
               "visuals/scenes.json", "visuals/prompts.json"],
    "tts": ["audio/voice.wav", "audio/timings.json", "audio/*.wav", "audio/*.mp3"],
    # 絵コンテ(scenes.json)は残す＝同じ構成のまま絵だけ描き直す
    "visuals": ["visuals/manifest.json", "visuals/prompts.json",
                "visuals/*.png", "visuals/*.jpg", "visuals/*.jpeg", "visuals/*.webp",
                "visuals/*.mp4"],
    "compose": ["compose/final.mp4", "compose/base.mp4", "compose/subs.ass",
                "compose/ffmpeg.log", "compose/_segments/",
                # 本編の控えとOP/ED記録も一緒に（残すと古い本編にEDを付け直してしまう）
                "compose/final_body.mp4", "compose/oped.json", "compose/ffmpeg_oped.log"],
    "project": ["memo.json", "thumb/thumbnail.png", "thumb/thumbnail_bg.png",
                "thumb/*.png", "thumb/*.jpg"],
}


def expand_remake_stages(stages) -> list[str]:
    """選んだ工程 → 実際に作り直しが必要な工程一覧（依存を展開・STAGES順）。"""
    need = set()
    for s in stages or []:
        need.update(REMAKE_DEPS.get(s, [s]))
    return [s for s in STAGES if s in need]


REMAKE_BACKUP = "_remake_backup"


def clear_stages(base, stages, log=print, expand: bool = True) -> tuple[list[str], Path | None]:
    """指定ステージの成果物を「消さずに退避」して未完了に戻す（依存ステージも自動で含める）。

    戻り値 = (実際に作り直しになるステージ一覧, 退避先フォルダ|None)。
    削除ではなく `_remake_backup/<日時>/` へ移動する＝作り直しが途中で失敗しても
    完成済みの動画・課金して作った画像を失わない（restore_backup で元に戻せる）。
    ※投稿済みフラグは消さずに改名する（video_id の唯一の記録＝消すと
      YouTube上の旧動画が迷子になり、二重公開の原因になる）。"""
    import shutil
    base = Path(base)
    # expand=False: 依存展開しない（BGMだけ入れ替え＝④だけ。⑤のサムネ再生成＝課金を避ける）
    todo = expand_remake_stages(stages) if expand else [s for s in STAGES if s in set(stages or [])]
    bk = base / REMAKE_BACKUP / datetime.now().strftime("%Y%m%d_%H%M%S")
    # 同じ秒に2回走ると退避先が同名になり、_stash が「同名あり＝捨てる」を選んで
    # バックアップ無しで成果物が消える。連番でぶつからないようにする
    i = 1
    while bk.exists():
        bk = bk.with_name(bk.name.split("-")[0] + f"-{i}")
        i += 1
    moved = 0

    def _stash(src: Path):
        nonlocal moved
        try:
            rel = src.relative_to(base)
            dst = bk / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():  # 同名が既にあるなら上書きせず捨てる（退避が本命）
                if src.is_dir():
                    shutil.rmtree(src, ignore_errors=True)
                else:
                    src.unlink()
            else:
                shutil.move(str(src), str(dst))
            moved += 1
        except Exception as e:
            log(f"    （退避できないファイルがありました: {src.name} / {str(e)[:60]}）")

    for st in todo:
        for rel in _REMAKE_FILES.get(st, []):
            try:
                if rel.endswith("/"):
                    d = base / rel.rstrip("/")
                    if d.is_dir():
                        _stash(d)
                elif "*" in rel:
                    parent = base / Path(rel).parent
                    for f in sorted(parent.glob(Path(rel).name)):
                        if f.is_file():
                            _stash(f)
                else:
                    f = base / rel
                    if f.is_file():
                        _stash(f)
            except Exception as e:
                log(f"    （退避できないファイルがありました: {rel} / {str(e)[:60]}）")
        log(f"    ✕ {STAGE_LABEL.get(st, st)} を作り直し対象にしました")
    # 退避に失敗して sentinel が残っていると、pipeline が「♻済み→スキップ」して
    # 作り直していないのに「できました」と出る（＝旧動画をそのまま投稿する事故）。
    # Windowsでは再生中/プレビュー中のファイルは移動できないので必ず起こりうる。
    # ※投稿フラグを改名する**前**に判定する（中止時にフラグを壊さない）
    stuck = []
    for st in todo:
        s = base / SENTINEL[st]
        try:
            if s.exists() and s.stat().st_size > 0:
                stuck.append(st)
        except Exception:
            pass
    if stuck:
        names = "、".join(STAGE_LABEL.get(s, s) for s in stuck)
        files = "、".join(SENTINEL[s] for s in stuck)
        log(f"    ✋ {names} の元ファイルを移動できませんでした（{files}）。")
        restore_backup(base, bk, log=log)   # 途中まで退避した分を戻す
        raise RuntimeError(
            f"作り直しを中止しました（{files} を移動できません）。\n"
            "その動画を開いているアプリ（動画プレイヤー、エクスプローラのプレビュー）を"
            "閉じてから、もう一度実行してください。")
    # 投稿済み記録は「消さずに改名」＝旧動画のvideo_idを残す（二重公開の防止）
    # ※中身が変わらない⑤サムネだけの作り直しでは改名しない（投稿済み判定を保つ）
    renamed_flags = {}   # この回で改名したフラグ（復元時に戻すのはこれだけ）
    if [s for s in todo if s != "project"]:
        for name, newname in ((SENTINEL["upload"], "uploaded_prev.flag"),
                              ("studio_pending.flag", "studio_pending_prev.flag")):
            try:
                f = base / name
                if f.exists():
                    renamed_flags[name] = f.read_text(encoding="utf-8", errors="replace")
                    # 2回目以降の作り直しでは _prev が既にある＝前回のvideo_id記録。
                    # 上書きすると最初に投稿した動画の唯一の記録が消えるので番号を振って残す
                    old = base / newname
                    if old.exists():
                        i = 2
                        while (base / newname.replace(".flag", f"{i}.flag")).exists():
                            i += 1
                        old.replace(base / newname.replace(".flag", f"{i}.flag"))
                    f.replace(base / newname)
                    log(f"    ⚠ この動画は投稿済みでした。記録を {newname} に残しました"
                        "（YouTube上の旧動画は自動では消えません。"
                        "必要なら手動で削除/非公開にしてください）")
            except Exception:
                pass
    # 改名の事実を退避フォルダに記録する。復元(restore_backup)はこの記録がある時だけ
    # フラグを戻す＝過去の作り直しで残った _prev を誤って昇格させない
    # （残留 _prev を無条件に戻すと、未投稿の新動画が「投稿済み」表示になり投稿できなくなる）
    if renamed_flags:
        try:
            import json as _json
            bk.mkdir(parents=True, exist_ok=True)
            (bk / "_renamed_flags.json").write_text(
                _json.dumps(renamed_flags, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
    if moved:
        log(f"    🗃 元の成果物 {moved} 件を {REMAKE_BACKUP}/{bk.name} へ退避しました"
            "（失敗しても元に戻せます）")
        return todo, bk
    return todo, (bk if renamed_flags and bk.exists() else None)


def restore_backup(base, bk, log=print, overwrite: bool = False) -> int:
    """退避した成果物を戻す。戻した件数を返す。

    作り直しが失敗した時に、完成していた動画や画像を復元するための保険。
    overwrite=False: 今そこに無いものだけ戻す（中止直後の巻き戻し用）
    overwrite=True : 途中まで作れた新しい成果物を捨てて、作り直す前の状態へ完全に戻す
      （新旧が混ざった動画＝新音声×旧映像 が「完成」に見える事故を防ぐ）"""
    import shutil
    base, bk = Path(base), Path(bk) if bk else None
    if not bk or not bk.exists():
        return 0
    # 完全復元で上書きする「途中まで作れた新しい成果物」は捨てずに横へ退避する。
    # 例: 作り直しで④の新動画まで完成した直後に⏹ → ⑤未完で失敗扱い → 完全復元、
    # のとき出来たての新動画が古い動画で上書き消滅していた（<退避名>-aborted/ に残す）
    aborted = bk.with_name(bk.name + "-aborted")
    stashed = 0
    n = 0
    for src in sorted(bk.rglob("*")):
        if not (src.is_file() or src.is_dir()):
            continue
        rel = src.relative_to(bk)
        if rel.name == "_renamed_flags.json":
            continue   # 改名記録は復元対象ではない（下のフラグ復元で使う内部ファイル）
        dst = base / rel
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            continue
        if dst.exists() and not overwrite:
            continue  # 新しく作れた分は残す（上書きしない）
        try:
            if overwrite and dst.exists():
                a = aborted / rel
                a.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dst), str(a))
                stashed += 1
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
            n += 1
        except Exception:
            pass
    if stashed:
        log(f"    🗃 作りかけの新しい成果物 {stashed} 件は {REMAKE_BACKUP}/{aborted.name} に"
            "残してあります（必要ならそこから取り出せます）。")
    # 投稿済み記録も戻す（clear_stages は「消さずに改名」なので退避フォルダには無い）。
    # 戻さないと、動画は元の投稿済みのものに戻っているのに投稿タブでは「未投稿」に見え、
    # もう一度YouTubeへ上げてしまう＝二重公開になる。
    # ※戻すのは「この回の clear_stages が改名した」と記録されたフラグだけ。
    #   残留 _prev を無条件に昇格させると、投稿前の作り直し失敗で未投稿の動画が
    #   旧video_id付きの「投稿済み」表示になり、投稿できなくなる（逆方向の事故）。
    if overwrite:
        renamed = {}
        try:
            import json as _json
            marker = bk / "_renamed_flags.json"
            if marker.exists():
                renamed = _json.loads(marker.read_text(encoding="utf-8")) or {}
        except Exception:
            renamed = {}
        for cur, content in renamed.items():
            try:
                dst = base / cur
                if not dst.exists():
                    dst.write_text(content, encoding="utf-8")
                    n += 1
                    log(f"    ♻ 投稿済みの記録を {cur} に戻しました（二重公開の防止）。")
                # この回の改名で作られた _prev は同じ中身の複製 → 片付ける
                p = base / cur.replace(".flag", "_prev.flag")
                if p.exists() and p.read_text(encoding="utf-8", errors="replace") == content:
                    p.unlink()
            except Exception:
                pass
    if n:
        log(f"    ♻ 作り直しに失敗したため、元の成果物 {n} 件を復元しました"
            + ("（作り直す前の状態に戻しました）。" if overwrite else "。"))
    return n


def prune_backups(base, keep: int = 1, log=print) -> int:
    """作り直しの退避フォルダを新しい順に keep 個だけ残して消す。消した容量(MB)を返す。

    退避は「作り直しに失敗しても元に戻せる」ための保険。成功したあとも全世代を
    残し続けると、1回あたり数百MB（実測474MB）が恒久的に積み上がる。"""
    import shutil
    root = Path(base) / REMAKE_BACKUP
    if not root.is_dir():
        return 0
    # 「20260727_170301」「20260727_170301-1」「20260727_170301-aborted」は同じ回。
    # スタンプ単位でまとめ、新しい回から keep 回分を残す
    groups: dict[str, list[Path]] = {}
    for d in root.iterdir():
        if d.is_dir():
            groups.setdefault(d.name.split("-")[0], []).append(d)
    freed = 0
    for stamp in sorted(groups, reverse=True)[max(0, keep):]:
        for d in groups[stamp]:
            try:
                freed += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
                shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass
    mb = int(freed / 1e6)
    if mb:
        log(f"    🧹 古い退避フォルダを整理しました（{mb}MB を解放。直前の{keep}回分は残します）")
    return mb


def looks_like_project(base) -> bool:
    """IZANAMIの動画フォルダらしいか（他フォルダを誤って作業対象にしない）。"""
    b = Path(base)
    if not b.is_dir():
        return False
    if (b / "run_cfg.json").exists() or (b / "memo.json").exists():
        return True
    return any((b / s).exists() for s in
               ("script/script.txt", "audio/voice.wav", "visuals/manifest.json",
                "compose/final.mp4"))


def _rebase_manifest(src: Path, dst: Path, log=print) -> None:
    """コピー先の visuals/manifest.json が持つ「元フォルダの絶対パス」を貼り替える。

    manifestは画像/クリップを絶対パスで持つため、コピーしただけでは
    **コピー先が元フォルダの画像を参照し続ける**。元を消したり作り直したりすると
    真っ黒な動画が「完成」してしまうので、コピー直後に自分のパスへ直す。
    （素材ライブラリ等、元フォルダの外を指すパスは触らない）"""
    mf = dst / "visuals" / "manifest.json"
    if not mf.exists():
        return
    try:
        raw = mf.read_text(encoding="utf-8")
    except Exception:
        return
    s, d = str(src.resolve()), str(dst.resolve())
    fixed = raw.replace(s.replace("\\", "\\\\"), d.replace("\\", "\\\\")).replace(s, d)
    if fixed != raw:
        try:
            mf.write_text(fixed, encoding="utf-8")
            log("    🔗 画像の参照先をコピー先に付け替えました（元フォルダに依存しない）")
        except Exception:
            pass


def load_memo(base) -> dict:
    """memo.json を読む（作り直しで消える前の内容を保持するため）。"""
    return util.load_json(Path(base) / "memo.json", {}) or {}


# コピー時に持って行かない中間ファイル（作り直しで必ず作られる／サイズが大きい）
_COPY_IGNORE = ("_debug", "__pycache__", REMAKE_BACKUP, "_segments",
                "base.mp4", "final_wip.mp4", "final_oped_wip.mp4", "_full_tmp.wav",
                # 投稿済み記録は「元の動画」のもの。コピー（＝まだ投稿していない別動画）へ
                # 持ち込むと、最初から投稿済み扱いになって投稿タブでスキップされる
                "uploaded.flag", "uploaded_prev*.flag",
                "studio_pending.flag", "studio_pending_prev*.flag")


def copy_project(src, output_dir, log=print) -> Path:
    """作り直し用に、元プロジェクトを新フォルダへ丸ごと複製して返す（元は無傷のまま）。"""
    import shutil
    src = Path(src)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    # コピー先にも元のお題スラッグを付ける（createだけだと作り直しコピーが
    # 「数字だけ」に戻り、一覧でお題が分からない問題が再発する・敵対検証2026-08-19）
    try:
        slug = topic_slug(util.load_json(src / "run_cfg.json", {}) or {})
    except Exception:
        slug = ""
    name = f"IZANAMI_{stamp}_{slug}" if slug else f"IZANAMI_{stamp}"
    dst = Path(output_dir) / name
    n = 1
    while dst.exists():
        dst = Path(output_dir) / f"{name}_{n}"
        n += 1
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*_COPY_IGNORE))
    _rebase_manifest(src, dst, log=log)   # 元フォルダの画像を参照し続ける事故を防ぐ
    log(f"    📁 元の動画をコピーしました: {dst.name}（元 {src.name} はそのまま残ります）")
    return dst
