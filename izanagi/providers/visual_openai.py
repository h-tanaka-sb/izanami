"""③ビジュアル生成（画像）: OpenAI 画像API（gpt-image-1）。

Phase1は背景静止画を1枚生成して合成の下地にする（複数枚/動画クリップはPhase4）。
世界観テンプレは settings.visual_prompt_template で上書き可能。
画像生成プロンプトの組み立ては ★Bダッシュ imagegen.py の思想を踏襲。
"""
from __future__ import annotations

import base64
from pathlib import Path

from .base import VisualProvider, register

# アスペクト比は _scene_prompt が縦横設定に合わせて付ける（ここに 16:9 を書くと
# 縦ショートでも横長で生成されてしまう＝2026-07-16監査で修正）
DEFAULT_WORLD_STYLE = (
    "A cinematic, high-quality background still for a YouTube video. "
    "Dynamic and premium, with depth and atmospheric lighting, strong focal point, "
    "plenty of clean negative space at the lower third for subtitles. "
    "Modern, stylish, professional color grading. "
    "STRICTLY AVOID: any text, letters, captions, watermark, logo, busy clutter, "
    "low-quality clip-art, distorted faces."
)


def _resize(path, w: int, h: int) -> None:
    try:
        from PIL import Image
        im = Image.open(path).convert("RGB")
        sw, sh = im.size
        tr, sr = w / h, sw / sh
        if sr > tr:
            nw = int(sh * tr); x = (sw - nw) // 2
            im = im.crop((x, 0, x + nw, sh))
        elif sr < tr:
            nh = int(sw / tr); y = (sh - nh) // 2
            im = im.crop((0, y, sw, y + nh))
        im = im.resize((w, h), Image.LANCZOS)
        im.save(str(path), "PNG")
    except Exception:
        pass


@register("visual", "api")
class OpenAIVisual(VisualProvider):
    @classmethod
    def available(cls, cfg: dict):
        if not cfg.get("openai_api_key"):
            return False, "openai_api_key 未設定"
        return True, ""

    def _scene_prompt(self, cfg: dict, scene: str) -> str:
        style = (cfg.get("visual_prompt_template") or "").strip() or DEFAULT_WORLD_STYLE
        scene = (scene or "").strip() or "an engaging scene matching the video theme"
        portrait = int(cfg.get("video_height", 1080)) > int(cfg.get("video_width", 1920))
        aspect = "9:16 vertical (portrait)" if portrait else "16:9 wide (landscape)"
        # 主人公プロファイル（pipeline.stage_visualsが判定結果から組み立てる）:
        # 全画像に同一の人物指定を入れ、画像ごとに主人公の性別・人物像が変わるのを防ぐ
        prot = (cfg.get("_protagonist_line") or "").strip()
        prot = f" {prot}" if prot else ""
        return f"{style} Scene/subject: {scene}.{prot} Aspect ratio: {aspect}."

    def generate(self, cfg: dict, meta: dict, out_dir: str, log=print) -> dict:
        from openai import OpenAI
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        w = int(cfg.get("video_width", 1920))
        h = int(cfg.get("video_height", 1080))
        model = cfg.get("image_model") or "gpt-image-1"
        portrait = h > w  # 縦動画（ショート）なら縦長で生成
        size = cfg.get("image_size") or ("1024x1536" if portrait else "1536x1024")
        if str(model).startswith("gpt-image"):
            if size not in ("1024x1024", "1536x1024", "1024x1536", "auto"):
                size = "1024x1536" if portrait else "1536x1024"
            elif portrait and size == "1536x1024":
                size = "1024x1536"
            elif not portrait and size == "1024x1536":
                size = "1536x1024"

        scenes = (meta or {}).get("scenes") or [(cfg.get("topic") or (meta or {}).get("title", ""))]
        client = OpenAI(api_key=cfg["openai_api_key"])
        images: list[str] = []
        from .base import dump_prompts
        prompts = [self._scene_prompt(cfg, s) for s in scenes]
        dump_prompts(str(out_dir), prompts)  # 実際に送るプロンプトを記録（検証用）
        log(f"    OpenAIで動画内画像を生成中（{len(scenes)}枚 / {model} {size}）…")
        for i, scene in enumerate(scenes):
            path = out_dir / f"{i:02d}_img.png"
            if path.exists() and path.stat().st_size > 1000:  # リトライ時は成功済み画像へ再課金しない
                images.append(str(path))
                continue
            kwargs = dict(model=model, prompt=prompts[i], size=size, n=1)
            if str(model).startswith("gpt-image"):
                kwargs["quality"] = "high"
            try:
                log(f"      画像 {i+1}/{len(scenes)}: {str(scene)[:40]}…")
                try:
                    resp = client.images.generate(**kwargs)
                except Exception:
                    kwargs.pop("quality", None)
                    resp = client.images.generate(**kwargs)
                b64 = getattr(resp.data[0], "b64_json", None)
                if b64:
                    path.write_bytes(base64.b64decode(b64))
                else:
                    url = getattr(resp.data[0], "url", None)
                    if not url:
                        raise RuntimeError("画像データが空")
                    import httpx
                    path.write_bytes(httpx.get(url, timeout=60).content)
                _resize(path, w, h)
                images.append(str(path))
            except Exception as e:
                log(f"      画像 {i+1} 生成失敗（スキップ）: {e}")
        return {"images": images, "clips": []}
