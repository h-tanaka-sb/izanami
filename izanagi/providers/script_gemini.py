"""①台本生成: Gemini API（AIスタジオの gemini_api_key を使用）。

プロンプト・出力形式（TITLE/DESCRIPTION/TAGS/---本文）は script_claude と共通。
モデルは settings.gemini_text_model（既定 gemini-2.5-flash）。
"""
from __future__ import annotations

from .base import ScriptProvider, register
from .script_claude import DEFAULT_SYSTEM, build_brushup_prompt, build_user_prompt, _parse


@register("script", "gemini_api")
class GeminiScript(ScriptProvider):
    @classmethod
    def available(cls, cfg: dict):
        if not cfg.get("gemini_api_key"):
            return False, "gemini_api_key（AIスタジオのAPIキー）が未設定です"
        return True, ""

    def _call(self, cfg, system: str, user: str, log) -> str:
        from google import genai
        from google.genai import types as gt
        client = genai.Client(api_key=cfg["gemini_api_key"])
        model = cfg.get("gemini_text_model") or "gemini-2.5-flash"
        resp = client.models.generate_content(
            model=model, contents=user,
            config=gt.GenerateContentConfig(system_instruction=system))
        return (getattr(resp, "text", None) or "").strip()

    def generate(self, cfg: dict, log=print) -> dict:
        system = (cfg.get("script_prompt") or "").strip() or DEFAULT_SYSTEM
        log("    Gemini(API)で台本を生成中…")
        return _parse(self._call(cfg, system, build_user_prompt(cfg), log))

    def brushup(self, script: str, cfg: dict, log=print) -> str:
        log("    Gemini(API)で台本をブラッシュアップ中…")
        return self._call(cfg, DEFAULT_SYSTEM, build_brushup_prompt(script, cfg), log).strip()
