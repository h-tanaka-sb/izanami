"""③動画生成（Web自動操作・HITL）: Google Flow(Veo) / Sora / Gemini。

動画生成系は完了検知/ダウンロードが極めて不安定なため、プロンプト投入まで自動化し、
生成・選択・DLは人が行う前提（チェックポイントで待つ）。DLイベントを掴めたら保存する。
台本から数カット分のプロンプトを作って順に投入する。
"""
from __future__ import annotations

from pathlib import Path

from .. import browser_ai
from .base import VideoProvider, register


def _scene_prompts(cfg: dict, meta: dict, n: int = 3) -> list[str]:
    # 縦ショートの時は既定プロンプトも 9:16 に（16:9固定だと黒帯クリップになる・2026-08-10）
    _aspect = ("9:16 vertical" if int(cfg.get("video_height", 1080)) >
               int(cfg.get("video_width", 1920)) else "16:9")
    style = (cfg.get("visual_prompt_template") or "").strip() or \
        f"cinematic {_aspect}, smooth camera motion, no text/captions/watermark"
    prot = (cfg.get("_protagonist_line") or "").strip()  # 主人公の人物指定（性別統一）
    if prot:
        style = f"{style}. {prot}"
    scenes = (meta or {}).get("scenes")
    if scenes:  # 絵コンテのシーンを動画クリップにも流用（先頭nつ）
        return [f"{style}. {s}" for s in scenes[:n]]
    topic = (cfg.get("topic") or meta.get("title", "")).strip()
    return [f"{style}. Scene {i+1} for a video about: {topic}." for i in range(n)]


class _WebVideo(VideoProvider):
    _url = ""
    _label = ""

    @classmethod
    def needs_browser(cls):
        return True

    def _debug_dir(self, cfg):
        return cfg.get("_debug_dir") if cfg.get("debug_dump") else None

    def generate(self, cfg: dict, meta: dict, out_dir: str, log=print) -> dict:
        if self.ctx is None:
            return {"clips": []}
        out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
        n = int(cfg.get("num_video_clips", 1))
        clips = []
        for i, prompt in enumerate(_scene_prompts(cfg, meta, n)):
            out = out_dir / f"clip_{i:02d}.mp4"
            log(f"    {self._label}: クリップ {i+1}/{n} を生成（人の操作で生成→DLしてください）")
            ok = browser_ai.video_generate_hitl(
                self.ctx, self._url, prompt, out, self._label, log=log,
                wait_sec=int(cfg.get("video_wait_sec", 600)), debug_dir=self._debug_dir(cfg))
            if ok and out.exists():
                clips.append(str(out))
            else:
                log(f"    {self._label}: クリップ {i+1} は取得できませんでした（スキップ）。")
        return {"clips": clips}


@register("video", "flow_web")
class FlowVideo(_WebVideo):
    _url = browser_ai.FLOW_URL
    _label = "Flow(Veo)"


@register("video", "sora_web")
class SoraVideo(_WebVideo):
    _url = browser_ai.SORA_URL
    _label = "Sora"


@register("video", "gemini_web")
class GeminiVideo(_WebVideo):
    """Gemini(Chrome)の動画生成。2026-07-25に**完全自動**化（人の操作は不要）。

    他のWeb動画（Flow/Sora）は依然HITL＝人が押す前提だが、Geminiだけは
    「＋→動画を作成→送信→<video>のsrcをDL」まで自動で通ることを実地検証した。"""

    _url = browser_ai.GEMINI_URL
    _label = "Gemini動画"

    def generate(self, cfg: dict, meta: dict, out_dir: str, log=print) -> dict:
        if self.ctx is None:
            return {"clips": []}
        out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
        n = int(cfg.get("num_video_clips", 1))
        wait = int(cfg.get("video_wait_sec", 600))
        clips, fails = [], 0
        for i, prompt in enumerate(_scene_prompts(cfg, meta, n)):
            out = out_dir / f"clip_{i:02d}.mp4"
            if out.exists() and out.stat().st_size > 10000:
                log(f"    {self._label}: クリップ {i+1}/{n} は作成済み → 再利用")
                clips.append(str(out)); continue
            log(f"    {self._label}: クリップ {i+1}/{n} を生成中…")
            ok = browser_ai.gemini_video(self.ctx, prompt, out, log=log, wait_sec=wait,
                                         debug_dir=self._debug_dir(cfg))
            if ok and out.exists():
                clips.append(str(out))
                fails = 0
            else:
                fails += 1
                log(f"    {self._label}: クリップ {i+1} は取得できませんでした（スキップ）。")
                if fails >= 2:   # 連続失敗＝仕様変更/quota切れ。無駄打ちを止める
                    log(f"    {self._label}: 続けて失敗したため中断します"
                        "（画像だけでも動画は作れます）。")
                    break
        return {"clips": clips}
