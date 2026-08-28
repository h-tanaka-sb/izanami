"""③動画生成（API・全自動）: OpenAI Sora / Google Veo。

「動画のAPIは本当にないのか？」→ 2026年7月時点で両方とも公式APIあり:
  - Sora  : OpenAI Videos API（videos.create → poll → download_content）。
            sora-2=720p 約$0.10/秒 / sora-2-pro=最大1080p 約$0.30〜0.70/秒。
            ※旧 Videos API は 2026-09-24 廃止予定（OpenAIの案内）。後継が出たら model 名を差し替え。
  - Veo   : Gemini API generate_videos（veo-3.1-*-generate-preview）。8秒/回・音声込み。
            fast=約$0.15/秒 / standard=約$0.40/秒。生成成功時のみ課金。

どちらも従量課金が高額になり得るため、本数(num_video_clips)と秒数の既定は控えめ。
生成物は visuals/clip_XX.mp4（無音化・尺調整は compose 側が行う）。
"""
from __future__ import annotations

import time
from pathlib import Path

from .base import VideoProvider, register
from .video_web import _scene_prompts


@register("video", "sora_api")
class SoraAPIVideo(VideoProvider):
    """OpenAI Videos API（sora-2 / sora-2-pro）。"""

    @classmethod
    def available(cls, cfg: dict):
        if not cfg.get("openai_api_key"):
            return False, "openai_api_key 未設定"
        try:
            from openai import OpenAI  # noqa: F401
        except Exception:
            return False, "openai パッケージが古いか未導入（uv sync）"
        return True, ""

    def generate(self, cfg: dict, meta: dict, out_dir: str, log=print) -> dict:
        from openai import OpenAI
        out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
        client = OpenAI(api_key=cfg["openai_api_key"])
        model = cfg.get("sora_model") or "sora-2"
        seconds = str(cfg.get("sora_seconds") or 8)
        portrait = int(cfg.get("video_height", 1080)) > int(cfg.get("video_width", 1920))
        hi = "pro" in model and str(cfg.get("video_api_resolution", "720p")) == "1080p"
        if portrait:
            size = "1080x1920" if hi else "720x1280"
        else:
            size = "1920x1080" if hi else "1280x720"
        n = max(1, int(cfg.get("num_video_clips", 1)))
        wait_max = int(cfg.get("video_wait_sec", 600))
        est = float(seconds) * n * (0.30 if "pro" in model else 0.10)
        log(f"    Sora(API)で動画クリップを生成（{n}本×{seconds}秒 / {model} {size}）")
        log(f"    ※従量課金の目安: 約${est:.2f}（sora-2=約$0.10/秒, pro=約$0.30/秒〜）")
        clips: list[str] = []
        for i, prompt in enumerate(_scene_prompts(cfg, meta, n)):
            out = out_dir / f"clip_{i:02d}.mp4"
            try:
                log(f"      クリップ {i+1}/{n}: 生成リクエスト送信…")
                video = client.videos.create(model=model, prompt=prompt,
                                             seconds=seconds, size=size)
                end = time.time() + wait_max
                while video.status in ("queued", "in_progress") and time.time() < end:
                    time.sleep(10)
                    video = client.videos.retrieve(video.id)
                if video.status != "completed":
                    log(f"      クリップ {i+1} 失敗/タイムアウト（status={video.status}）。スキップ。")
                    continue
                content = client.videos.download_content(video.id, variant="video")
                content.write_to_file(str(out))
                if out.exists() and out.stat().st_size > 0:
                    clips.append(str(out))
                    log(f"      クリップ {i+1} 保存: {out.name}")
            except Exception as e:
                log(f"      クリップ {i+1} エラー（スキップ）: {str(e)[:120]}")
        return {"clips": clips}


@register("video", "veo_api")
class VeoAPIVideo(VideoProvider):
    """Gemini API の Veo 3.1（generate_videos）。8秒/回・音声込み。"""

    @classmethod
    def available(cls, cfg: dict):
        if not cfg.get("gemini_api_key"):
            return False, "gemini_api_key（AIスタジオのAPIキー）が未設定です"
        return True, ""

    def generate(self, cfg: dict, meta: dict, out_dir: str, log=print) -> dict:
        from google import genai
        from google.genai import types as gt
        out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
        client = genai.Client(api_key=cfg["gemini_api_key"])
        model = cfg.get("veo_model") or "veo-3.1-fast-generate-preview"
        seconds = int(cfg.get("veo_seconds") or 8)
        resolution = str(cfg.get("video_api_resolution") or "720p")
        aspect = ("9:16" if int(cfg.get("video_height", 1080)) > int(cfg.get("video_width", 1920))
                  else "16:9")
        n = max(1, int(cfg.get("num_video_clips", 1)))
        wait_max = int(cfg.get("video_wait_sec", 600))
        est = seconds * n * (0.15 if "fast" in model else 0.40)
        log(f"    Veo(API)で動画クリップを生成（{n}本×{seconds}秒 / {model} {resolution}）")
        log(f"    ※従量課金の目安: 約${est:.2f}（fast=約$0.15/秒, standard=約$0.40/秒。成功時のみ課金）")
        clips: list[str] = []
        for i, prompt in enumerate(_scene_prompts(cfg, meta, n)):
            out = out_dir / f"clip_{i:02d}.mp4"
            try:
                log(f"      クリップ {i+1}/{n}: 生成リクエスト送信…")
                try:
                    op = client.models.generate_videos(
                        model=model, prompt=prompt,
                        config=gt.GenerateVideosConfig(
                            aspect_ratio=aspect, resolution=resolution,
                            duration_seconds=seconds))
                except (TypeError, ValueError):
                    # SDK/モデルの版差で config パラメタが合わない場合は最小指定で
                    op = client.models.generate_videos(model=model, prompt=prompt)
                end = time.time() + wait_max
                while not op.done and time.time() < end:
                    time.sleep(10)
                    op = client.operations.get(op)
                vids = getattr(getattr(op, "response", None), "generated_videos", None) or []
                if not op.done or not vids:
                    err = getattr(op, "error", None)
                    log(f"      クリップ {i+1} 失敗/タイムアウト{f'（{err}）' if err else ''}。スキップ。")
                    continue
                gv = vids[0]
                client.files.download(file=gv.video)
                gv.video.save(str(out))
                if out.exists() and out.stat().st_size > 0:
                    clips.append(str(out))
                    log(f"      クリップ {i+1} 保存: {out.name}")
            except Exception as e:
                log(f"      クリップ {i+1} エラー（スキップ）: {str(e)[:120]}")
        return {"clips": clips}
