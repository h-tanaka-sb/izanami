"""③ビジュアル生成（Web自動操作・画像）: ChatGPT 画像 / Gemini 画像。

共有Chrome上で画像を生成しダウンロードする。プロンプト組み立ては visual_openai を流用。
2026-07-21 配布先報告の対策: 取得画像のMD5を照合し、既取得と同一なら誤取得として破棄
（全シーンが同じ無関係画像のまま「成功」で完走する事故の保険）。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from .. import browser_ai
from .base import VisualProvider, register


class _WebVisual(VisualProvider):
    @classmethod
    def needs_browser(cls):
        return True

    def _debug_dir(self, cfg):
        return cfg.get("_debug_dir") if cfg.get("debug_dump") else None

    def _scene_prompt(self, cfg, scene):
        from .visual_openai import OpenAIVisual
        return OpenAIVisual()._scene_prompt(cfg, scene)

    def _run_scenes(self, gen_fn, cfg: dict, meta: dict, out_dir: str, log,
                    engine_label: str) -> dict:
        from .base import dump_prompts
        if self.ctx is None:
            return {"images": [], "clips": []}
        out_dir = Path(out_dir); out_dir.mkdir(parents=True, exist_ok=True)
        w = int(cfg.get("video_width", 1920)); h = int(cfg.get("video_height", 1080))
        scenes = (meta or {}).get("scenes") or [(cfg.get("topic") or (meta or {}).get("title", ""))]
        prompts = [self._scene_prompt(cfg, s) for s in scenes]
        dump_prompts(str(out_dir), prompts)  # 実際に送るプロンプトを記録（検証用）
        images: list[str] = []
        seen: set[str] = set()

        def digest(p) -> str:
            try:
                return hashlib.md5(Path(p).read_bytes()).hexdigest()
            except Exception:
                return ""

        # ♻再開時の自己修復: 保存済み画像に「完全一致の重複」があれば、それは
        # 旧バグ（既存画像の誤取得）の産物＝そのグループは1枚目も含めて全て作り直す
        counts: dict[str, int] = {}
        for i in range(len(scenes)):
            q = out_dir / f"{i:02d}_img.png"
            if q.exists() and q.stat().st_size > 1000:
                dg = digest(q)
                if dg:
                    counts[dg] = counts.get(dg, 0) + 1
        dup_digests = {dg for dg, c in counts.items() if c >= 2}

        log(f"    {engine_label}で動画内画像を生成（{len(scenes)}枚・人の確認/ログインが要る場合あり）…")
        for i, _scene in enumerate(scenes):
            path = out_dir / f"{i:02d}_img.png"
            if path.exists() and path.stat().st_size > 1000:  # ♻再開時は取得済み画像を再利用
                d = digest(path)
                if d and (d in dup_digests or d in seen):
                    log(f"      画像 {i + 1}: 保存済みが他シーンと同一内容のため作り直します。")
                    try:
                        path.unlink()
                    except Exception:
                        pass
                else:
                    images.append(str(path))
                    seen.add(d)
                    continue
            got = False
            for _attempt in (1, 2):  # 同一画像（誤取得の疑い）は1回だけ作り直す
                ok = gen_fn(self.ctx, prompts[i], path, log=log,
                            size_w=w, size_h=h, debug_dir=self._debug_dir(cfg),
                            model=cfg.get("web_image_model_chatgpt", "")
                            if engine_label == "ChatGPT"
                            else cfg.get("web_image_model_gemini", ""),
                            effort=cfg.get("web_image_effort_chatgpt", "")
                            if engine_label == "ChatGPT" else "")
                if not (ok and path.exists()):
                    break
                d = digest(path)
                if d and d in seen:
                    # 意図して同一構図が続くことは無い＝完全一致は誤取得とみなす
                    log(f"      画像 {i + 1}: 既に取得した画像と同一内容のため破棄します（誤取得の疑い）。")
                    try:
                        path.unlink()
                    except Exception:
                        pass
                    continue
                seen.add(d)
                images.append(str(path))
                got = True
                break
            if not got:
                log(f"      画像 {i + 1}/{len(scenes)} は取得できませんでした（スキップ）。")
        return {"images": images, "clips": []}


@register("visual", "chatgpt_web")
class ChatGPTWebVisual(_WebVisual):
    def generate(self, cfg: dict, meta: dict, out_dir: str, log=print) -> dict:
        return self._run_scenes(browser_ai.chatgpt_image, cfg, meta, out_dir, log, "ChatGPT")


@register("visual", "gemini_web")
class GeminiWebVisual(_WebVisual):
    def generate(self, cfg: dict, meta: dict, out_dir: str, log=print) -> dict:
        return self._run_scenes(browser_ai.gemini_image, cfg, meta, out_dir, log, "Gemini")
