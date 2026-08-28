"""⑤サムネ生成: OpenAI画像API(1280×720) / ChatGPT画像Web。

YouTubeサムネは 1280×720・16:9・<=2MB。タイトル文字を画像内に焼き込む（gpt-image-1は日本語可）。
"""
from __future__ import annotations

import base64
from pathlib import Path

from .. import browser_ai, util
from .base import ThumbnailProvider, register

THUMB_W, THUMB_H = 1280, 720

DEFAULT_THUMB_STYLE = (
    "A high-CTR YouTube thumbnail BACKGROUND image, 16:9, bold and punchy, dramatic lighting, "
    "strong single focal point on the right side, high contrast, vivid colors. "
    "STRICTLY AVOID: clutter, watermark, distorted faces."
)


def _build_prompt(cfg: dict, meta: dict) -> str:
    """背景専用プロンプト（文字はAIに焼かせない＝日本語字形崩れ・配置暴れの根絶）。

    キャッチコピーは後段の thumb_text.draw_thumb_text がPILで焼き込む。
    """
    style = (cfg.get("thumbnail_prompt") or "").strip() or DEFAULT_THUMB_STYLE
    title = (meta or {}).get("title", "") or (cfg.get("topic") or "")
    return (f"{style} Absolutely NO text, NO letters, NO logo, NO watermark of any kind. "
            f"Leave clean, low-detail space in the upper-left third for a headline overlay. "
            f"Depict the mood of this story (do not write it): {title[:40]}. "
            f"Theme: {cfg.get('topic','')}.")


@register("thumb", "api")
class OpenAIThumbnail(ThumbnailProvider):
    @classmethod
    def available(cls, cfg: dict):
        if not cfg.get("openai_api_key"):
            return False, "openai_api_key 未設定"
        return True, ""

    def generate(self, cfg: dict, meta: dict, out_png: str, log=print) -> bool:
        from openai import OpenAI
        out_png = Path(out_png); out_png.parent.mkdir(parents=True, exist_ok=True)
        client = OpenAI(api_key=cfg["openai_api_key"])
        model = cfg.get("image_model") or "gpt-image-1"
        log("    OpenAIでサムネを生成中…")
        kwargs = dict(model=model, prompt=_build_prompt(cfg, meta), size="1536x1024", n=1)
        if str(model).startswith("gpt-image"):
            kwargs["quality"] = "high"
        try:
            resp = client.images.generate(**kwargs)
        except Exception:
            kwargs.pop("quality", None)
            resp = client.images.generate(**kwargs)
        b64 = getattr(resp.data[0], "b64_json", None)
        if b64:
            out_png.write_bytes(base64.b64decode(b64))
        else:
            url = getattr(resp.data[0], "url", None)
            if not url:
                return False
            import httpx
            out_png.write_bytes(httpx.get(url, timeout=60).content)
        util.resize_cover(out_png, THUMB_W, THUMB_H)
        return out_png.exists()


@register("thumb", "gemini_api")
class GeminiThumbnail(ThumbnailProvider):
    """Gemini（nano banana = gemini-2.5-flash-image）でサムネ生成。日本語文字も焼込可。"""

    @classmethod
    def available(cls, cfg: dict):
        if not cfg.get("gemini_api_key"):
            return False, "gemini_api_key（AIスタジオのAPIキー）が未設定です"
        return True, ""

    def generate(self, cfg: dict, meta: dict, out_png: str, log=print) -> bool:
        import base64 as _b64
        from google import genai
        from google.genai import types as gt
        out_png = Path(out_png); out_png.parent.mkdir(parents=True, exist_ok=True)
        model = cfg.get("gemini_image_model") or "gemini-2.5-flash-image"
        client = genai.Client(api_key=cfg["gemini_api_key"])
        log("    Geminiでサムネを生成中…")
        data = None
        for attempt in range(1, 4):  # 画像を返さないことがあるためリトライ
            try:
                resp = client.models.generate_content(
                    model=model, contents=_build_prompt(cfg, meta),
                    config=gt.GenerateContentConfig(response_modalities=["IMAGE", "TEXT"]))
                for cand in (resp.candidates or []):
                    for part in (getattr(getattr(cand, "content", None), "parts", None) or []):
                        inline = getattr(part, "inline_data", None)
                        if inline and getattr(inline, "data", None):
                            data = inline.data
                            break
                    if data:
                        break
                if data:
                    break
            except Exception as e:
                log(f"      サムネ生成エラー({attempt}/3): {str(e)[:80]}")
        if not data:
            return False
        out_png.write_bytes(_b64.b64decode(data) if isinstance(data, str) else data)
        util.resize_cover(out_png, THUMB_W, THUMB_H)
        return out_png.exists()


@register("thumb", "chatgpt_web")
class ChatGPTWebThumbnail(ThumbnailProvider):
    @classmethod
    def needs_browser(cls):
        return True

    def generate(self, cfg: dict, meta: dict, out_png: str, log=print) -> bool:
        if self.ctx is None:
            return False
        out_png = Path(out_png)
        debug = cfg.get("_debug_dir") if cfg.get("debug_dump") else None
        ok = browser_ai.chatgpt_image(self.ctx, _build_prompt(cfg, meta), out_png, log=log,
                                      size_w=THUMB_W, size_h=THUMB_H, debug_dir=debug,
                                      model=cfg.get("web_image_model_chatgpt", ""),
                                      effort=cfg.get("web_image_effort_chatgpt", ""))
        return ok and out_png.exists()


@register("thumb", "gemini_web")
class GeminiWebThumbnail(ThumbnailProvider):
    @classmethod
    def needs_browser(cls):
        return True

    def generate(self, cfg: dict, meta: dict, out_png: str, log=print) -> bool:
        if self.ctx is None:
            return False
        out_png = Path(out_png)
        debug = cfg.get("_debug_dir") if cfg.get("debug_dump") else None
        ok = browser_ai.gemini_image(self.ctx, _build_prompt(cfg, meta), out_png, log=log,
                                     size_w=THUMB_W, size_h=THUMB_H, debug_dir=debug,
                                     model=cfg.get("web_image_model_gemini", ""))
        return ok and out_png.exists()
