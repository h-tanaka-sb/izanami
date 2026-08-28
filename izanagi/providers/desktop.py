"""デスクトップアプリ連携プロバイダ（ChatGPT/Codex・Claude Code のデスクトップ版）。

外部操作の公式APIが無いため、izanagi.desktop_bridge 経由で
  台本: プロンプトを貼付→送信→アプリの「コピー」でクリップボードから取り込み
  画像/サムネ: プロンプトを貼付→送信→保存フォルダに現れた新規画像を取り込み
という半自動（送信は自動・受け取りは1操作）で行う。Windows専用。

※ Claude Code は画像を作れないため、画像/サムネのデスクトップ版は Codex のみ。
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from .base import ScriptProvider, VisualProvider, ThumbnailProvider, register
from .script_claude import _parse, build_brushup_prompt
from .script_web import _build_prompt as _script_prompt
from .visual_openai import DEFAULT_WORLD_STYLE


def _app_title(cfg: dict, engine: str, default: str) -> str:
    return (cfg.get("desktop_app_titles", {}) or {}).get(engine, "") or default


# 自動起動時にアプリを探す名前の候補（settingsのタイトルに加えて使う）
_LAUNCH_HINTS = {"codex_desktop": ["ChatGPT", "Codex"],
                 "claudecode_desktop": ["Claude"]}


def _new_chat_hotkey(cfg: dict, engine: str) -> str:
    """送信前に新規チャットを開くホットキー（2026-07-23）。

    開いている過去会話へ追記すると文脈汚染・古い画像の混入を招くため、
    両デスクトップとも既定ON（Codex=Ctrl+Shift+O / Claude=Ctrl+N。実機で
    新規セッション作成→回答の自動取得まで実証済み）。プロンプトは毎回
    自己完結（出力形式ルール込み）なので新規チャットでも回答形式は保たれる。
    settings の desktop_new_chat_hotkeys {engine: キー} で上書き可（空文字=無効化）。"""
    hk = (cfg.get("desktop_new_chat_hotkeys", {}) or {}).get(engine)
    if hk is not None:
        return (hk or "").strip()
    return {"codex_desktop": "ctrl+shift+o",
            "claudecode_desktop": "ctrl+n"}.get(engine, "")


def _require_window(title: str, log, cfg: dict | None = None, engine: str = "") -> bool:
    """対象アプリのウィンドウ存在チェック。無ければ自動起動を試み、ダメなら False。

    これが無いと（例: アプリ未起動）15枚分silentに空振りして「出力が空でした」だけが残る。
    2026-07-17: 未起動ならエラーで止めず、アプリを自動起動してウィンドウを待つように変更。"""
    from .. import browser_ai as _ba, desktop_bridge as db
    if db.find_window(title):
        return True
    if cfg is not None:
        log(f"    『{title}』のウィンドウが見つかりません → アプリの起動を試みます…")
        hints = [title] + [h for h in _LAUNCH_HINTS.get(engine, [])
                           if h.lower() != (title or "").lower()]
        path = ((cfg.get("desktop_app_paths") or {}).get(engine, "") or "").strip()
        if db.launch_app(hints, path, log):
            hwnd = db.wait_for_window(title, timeout=float(cfg.get("desktop_launch_wait", 60)),
                                      stop=lambda: _ba._stopped(), log=log)
            if hwnd:
                time.sleep(3.0)  # 起動直後の描画・セッション読込みを少し待つ
                log(f"    ✅ 『{title}』を起動しました。")
                return True
            log(f"    ⚠ アプリは起動しましたが『{title}』のウィンドウが時間内に現れませんでした"
                "（ログイン画面等の可能性。画面を確認してください）。")
    log(f"    ⚠ デスクトップ連携: 『{title}』を使えません。"
        "アプリを起動してログインしてください（自動起動のアプリを指定するには settings の "
        "desktop_app_paths にexe/lnkのパス、ウィンドウ名が違う場合は desktop_app_titles を変更）。")
    return False


def _watch_dir(cfg: dict) -> str:
    from .. import desktop_bridge as db
    return (cfg.get("desktop_watch_dir") or "").strip() or db.downloads_dir()


def _looks_like_script(s: str) -> bool:
    """台本/推敲の回答らしさ（待機中にユーザーが別作業でコピーした短文の誤採用防止）。"""
    s = (s or "").strip()
    return len(s) >= 120 or "TITLES" in s.upper()


class _DesktopScript(ScriptProvider):
    DEFAULT_TITLE = "ChatGPT"

    @classmethod
    def available(cls, cfg: dict):
        from .. import desktop_bridge as db
        if not db.available():
            return False, "デスクトップ連携はWindowsのみ対応です"
        return True, ""

    def _send_and_get(self, cfg, prompt, log, validate=None) -> str:
        from .. import desktop_bridge as db, browser_ai as _ba
        title = _app_title(cfg, self.engine, self.DEFAULT_TITLE)
        # デスクトップアプリの会話ログから回答を直接取得できる（操作不要・最優先）。
        # Codex=rollout / Claude Code=~/.claude/projects のセッションJSONL。
        # 監視は送信前に開始する＝過去の回答を誤って掴まない
        ro = None
        if cfg.get("codex_auto_capture", True):
            if self.engine == "codex_desktop":
                ro = db.RolloutWatcher(cfg)
            elif self.engine == "claudecode_desktop":
                ro = db.ClaudeCodeWatcher(cfg, prompt=prompt)
            if ro is not None and not ro.available():
                ro = None
        if not db.send_prompt(title, prompt, log,
                              new_chat_hotkey=_new_chat_hotkey(cfg, self.engine)):
            return ""
        hotkey = (cfg.get("desktop_copy_hotkey") or "ctrl+shift+c").strip()
        # ログ自動取得が使える時はホットキー送出をしない（フォーカスを奪わない）
        auto = ro is None and bool(cfg.get("desktop_auto_copy", True)) and bool(hotkey)
        return db.wait_for_reply(prompt, timeout=float(cfg.get("desktop_timeout", 480)),
                                 stop=lambda: _ba._stopped(), log=log, validate=validate,
                                 auto_copy_title=(title if auto else None),
                                 copy_hotkey=hotkey, rollout=ro)

    def generate(self, cfg: dict, log=print) -> dict:
        if not _require_window(_app_title(cfg, self.engine, self.DEFAULT_TITLE), log, cfg, self.engine):
            return {"script": "", "title": "", "description": "", "tags": []}
        raw = self._send_and_get(cfg, _script_prompt(cfg), log, validate=_looks_like_script)
        if not raw:
            return {"script": "", "title": "", "description": "", "tags": []}
        return _parse(raw)

    def brushup(self, script: str, cfg: dict, log=print) -> str:
        raw = self._send_and_get(cfg, build_brushup_prompt(script, cfg), log,
                                 validate=_looks_like_script)
        return raw.strip() if raw else script

    def _call(self, cfg: dict, system: str, user: str, log=print) -> str:
        """生テキスト依頼（お題生成など、台本テンプレートを通したくない用途）。

        これが無いと topics.generate_topics が generate() に落ち、AIが台本を書いて
        その本文が「お題」としてお題欄/夜間キューに流れ込む（Web版で2026-07-08に
        実際に起きた事故と同型）。"""
        if not _require_window(_app_title(cfg, self.engine, self.DEFAULT_TITLE), log, cfg, self.engine):
            raise RuntimeError("デスクトップアプリのウィンドウが見つかりません")
        out = self._send_and_get(cfg, f"{system}\n\n{user}", log)
        if not out:
            raise RuntimeError("デスクトップアプリからの応答が取り込めませんでした")
        return out


@register("script", "codex_desktop")
class CodexDesktopScript(_DesktopScript):
    DEFAULT_TITLE = "ChatGPT"


@register("script", "claudecode_desktop")
class ClaudeCodeDesktopScript(_DesktopScript):
    DEFAULT_TITLE = "Claude"


class _DesktopImageMixin:
    """保存フォルダ監視で画像を1枚受け取る共通処理。"""
    DEFAULT_TITLE = "ChatGPT"

    @classmethod
    def available(cls, cfg: dict):
        from .. import desktop_bridge as db
        if not db.available():
            return False, "デスクトップ連携はWindowsのみ対応です"
        return True, ""

    def _make_one(self, cfg, prompt, dst_path, log) -> bool:
        from .. import desktop_bridge as db, browser_ai as _ba
        title = _app_title(cfg, self.engine, self.DEFAULT_TITLE)
        watch = _watch_dir(cfg)
        since = time.time()
        # Codexアプリは生成画像が会話ログ(rollout)に入る＝保存操作なしで自動取得できる。
        # 監視は送信前に開始する＝前のシーンの画像を誤って掴まない
        ro = None
        if cfg.get("codex_auto_capture", True):
            ro = db.RolloutWatcher(cfg)
            if not ro.available():
                ro = None
        if not db.send_prompt(title, prompt, log,
                              new_chat_hotkey=_new_chat_hotkey(cfg, self.engine)):
            return False
        if ro is not None:
            log(f"    画像を『{title}』で生成中…（完成したら自動で取り込みます。"
                f"手動で『{watch}』へ保存してもOK）。")
        else:
            log(f"    画像を『{title}』で作って『{watch}』へ保存してください（自動で取り込みます）。")
        p = db.newest_image_after(watch, since, timeout=float(cfg.get("desktop_timeout", 480)),
                                  stop=lambda: _ba._stopped(), log=log, rollout=ro)
        if not p:
            return False
        Path(dst_path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, dst_path)
        try:  # rollout経由の一時PNGは取り込み後に掃除
            if Path(p).name.startswith("izanagi_codex_"):
                Path(p).unlink()
        except Exception:
            pass
        return True


@register("visual", "codex_desktop")
class CodexDesktopVisual(_DesktopImageMixin, VisualProvider):
    def _scene_prompt(self, cfg, scene):
        style = (cfg.get("visual_prompt_template") or "").strip() or DEFAULT_WORLD_STYLE
        scene = (scene or "").strip() or "an engaging scene matching the video theme"
        portrait = int(cfg.get("video_height", 1080)) > int(cfg.get("video_width", 1920))
        aspect = "9:16（縦長）" if portrait else "16:9（横長）"
        # チャットアプリに貼るため「画像を作れ」という指示文が必須（説明文だけだと
        # テキストで回答されたり解釈が揺れる）。縦横もここで明示する。
        prot = (cfg.get("_protagonist_line") or "").strip()
        prot = f" {prot}" if prot else ""
        return (f"次の内容で画像を1枚だけ生成してください。文章での回答は不要です。"
                f"アスペクト比は {aspect}。\n\n{style} Scene/subject: {scene}.{prot}")

    def generate(self, cfg: dict, meta: dict, out_dir: str, log=print) -> dict:
        from .base import dump_prompts
        if not _require_window(_app_title(cfg, self.engine, self.DEFAULT_TITLE), log, cfg, self.engine):
            return {"images": [], "clips": []}  # アプリ未起動＝理由を出して即終了（15枚空振りしない）
        # 画像モデルの選択（③の「モデル」欄）はデスクトップアプリでは自動切替できない
        # （2026-08-10ユーザー要望への注記）→ アプリ側での選択を案内する
        _lvl = (cfg.get("web_image_model_chatgpt") or "").strip()
        if _lvl:
            log(f"    ※デスクトップアプリはモデルの自動切替ができません。画像を「{_lvl}」で"
                "作りたい場合は、アプリ側のモデル選択で選んでおいてください。")
        out = Path(out_dir)
        scenes = (meta or {}).get("scenes") or [cfg.get("topic", "") or (meta or {}).get("title", "")]
        prompts = [self._scene_prompt(cfg, s) for s in scenes]
        dump_prompts(out_dir, prompts)  # 実際に送るプロンプトを記録（「反映されない」の検証用）
        images: list[str] = []
        misses = 0
        for i, prompt in enumerate(prompts):
            dst = out / f"{i:02d}_img.png"
            if dst.exists() and dst.stat().st_size > 1000:  # ♻再開時は取得済み画像を再利用
                images.append(str(dst))
                continue
            log(f"    画像 {i + 1}/{len(prompts)} をデスクトップアプリで作成…")
            log(f"      プロンプト: {prompt[:70].replace(chr(10), ' ')}…")
            if self._make_one(cfg, prompt, dst, log) and dst.exists():
                images.append(str(dst))
                misses = 0
            else:
                log(f"    画像{i + 1}は取り込めませんでした（スキップ）。")
                misses += 1
                if misses >= 2 and not images:
                    log("    ⚠ 連続で受け取れないため中断します（アプリ側で画像を保存フォルダへ"
                        "保存できているか確認してください）。")
                    break
        return {"images": images, "clips": []}


@register("thumb", "codex_desktop")
class CodexDesktopThumbnail(_DesktopImageMixin, ThumbnailProvider):
    def generate(self, cfg: dict, meta: dict, out_png: str, log=print) -> bool:
        if not _require_window(_app_title(cfg, self.engine, self.DEFAULT_TITLE), log, cfg, self.engine):
            return False
        from .. import util
        from .thumb import THUMB_H, THUMB_W, _build_prompt as _thumb_prompt
        prompt = ("次の内容でYouTubeサムネイル用の画像を1枚だけ生成してください。"
                  "文章での回答は不要です。アスペクト比は 16:9（横長）。\n\n"
                  + _thumb_prompt(cfg, meta))
        if not (self._make_one(cfg, prompt, out_png, log) and Path(out_png).exists()):
            return False
        util.resize_cover(Path(out_png), THUMB_W, THUMB_H)  # 他エンジンと同じ1280×720へ正規化
        return Path(out_png).exists()
