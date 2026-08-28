"""③ビジュアル生成（画像・API）: Gemini（nano banana = gemini-2.5-flash-image）。

gemini_api_key（AIスタジオのキー）で google-genai 経由で生成。台本の場面ごとに1枚。
プロンプト組み立ては visual_openai を流用。
"""
from __future__ import annotations

import base64
from pathlib import Path

from .base import VisualProvider, register
from .visual_openai import OpenAIVisual, _resize


@register("visual", "gemini_api")
class GeminiVisual(VisualProvider):
    @classmethod
    def available(cls, cfg: dict):
        if not cfg.get("gemini_api_key"):
            return False, "gemini_api_key（AIスタジオのAPIキー）が未設定です"
        return True, ""

    def _scene_prompt(self, cfg, scene):
        return OpenAIVisual()._scene_prompt(cfg, scene)

    def generate(self, cfg: dict, meta: dict, out_dir: str, log=print) -> dict:
        from google import genai
        from google.genai import types as gt
        from .base import dump_prompts
        out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
        w = int(cfg.get("video_width", 1920)); h = int(cfg.get("video_height", 1080))
        model = cfg.get("gemini_image_model") or "gemini-2.5-flash-image"
        client = genai.Client(api_key=cfg["gemini_api_key"])
        # 縦動画は生成時点で 9:16 を指定する（横長→中央クロップで7割捨てる事故の防止。
        # 古いSDKで ImageConfig が無い場合はプロンプト側のアスペクト文言だけで続行）
        try:
            gen_config = gt.GenerateContentConfig(
                response_modalities=["IMAGE", "TEXT"],
                image_config=gt.ImageConfig(aspect_ratio="9:16" if h > w else "16:9"))
        except Exception:
            gen_config = gt.GenerateContentConfig(response_modalities=["IMAGE", "TEXT"])
        scenes = (meta or {}).get("scenes") or [(cfg.get("topic") or (meta or {}).get("title", ""))]
        prompts = [self._scene_prompt(cfg, s) for s in scenes]
        dump_prompts(str(out_dir), prompts)  # 実際に送るプロンプトを記録（検証用）
        images: list[str] = []
        log(f"    Geminiで動画内画像を生成中（{len(scenes)}枚 / {model}）…")
        for i, scene in enumerate(scenes):
            fn0 = out_dir / f"{i:02d}_img.png"
            if fn0.exists() and fn0.stat().st_size > 1000:  # リトライ時は成功済み画像へ再課金しない
                images.append(str(fn0))
                continue
            log(f"      画像 {i + 1}/{len(scenes)} を生成中…")
            prompt = prompts[i]
            data = None
            last_err = ""
            for attempt in range(1, 4):  # 画像を返さないことがあるためリトライ
                # 2回目以降は安全フィルタ対策の言い換えを付ける（事件/暴力系の場面で
                # 「文章で断られて画像なし」になるのを、間接表現の指示で救う）
                p = prompt if attempt == 1 else (
                    prompt + " IMPORTANT: depict this scene in a completely safe, non-graphic "
                    "way. No violence, no blood, no weapons, no injuries shown. Use symbolic, "
                    "indirect composition instead (shadows, distance, aftermath mood, "
                    "empty scene, dramatic lighting).")
                try:
                    resp = client.models.generate_content(
                        model=model, contents=p, config=gen_config)
                    txt = ""
                    for cand in (resp.candidates or []):
                        cont = getattr(cand, "content", None)
                        for part in (getattr(cont, "parts", None) or []):
                            inline = getattr(part, "inline_data", None)
                            if inline and getattr(inline, "data", None):
                                data = inline.data
                                break
                            if getattr(part, "text", None):
                                txt = part.text
                        if data:
                            break
                    if data:
                        break
                    # 画像なし＝安全フィルタ等で断られた可能性。理由を見えるように
                    log(f"      画像{i+1}: 画像なし応答({attempt}/3)"
                        + (f" 「{txt[:60]}…」" if txt else "")
                        + ("→ 間接表現に言い換えて再挑戦" if attempt < 3 else ""))
                except Exception as e:
                    last_err = str(e)
                    log(f"      画像{i+1} エラー({attempt}/3): {last_err[:80]}")
            if data:
                fn = out_dir / f"{i:02d}_img.png"
                fn.write_bytes(base64.b64decode(data) if isinstance(data, str) else data)
                _resize(fn, w, h)
                images.append(str(fn))
            else:
                log(f"      画像{i+1} は生成できませんでした（スキップ）。")
                # quota/残高切れ(429)は待っても直らない＝1枚も出来ていないなら即打ち切り
                # （15枚×3回を空振りせず、すぐ次のエンジンへ替わる）
                if not images and ("429" in last_err or "RESOURCE_EXHAUSTED" in last_err):
                    log("      ⚠ Geminiのクレジット/枠切れ(429)のため残りを中止します。")
                    break
        return {"images": images, "clips": []}
