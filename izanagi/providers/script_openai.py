"""①台本生成: OpenAI（ChatGPT API）。

プロンプト・出力形式（TITLE/DESCRIPTION/TAGS/---本文）は script_claude と共通。
モデルは settings.openai_text_model（既定 gpt-5.5。無ければ gpt-4o に自動フォールバック）。
"""
from __future__ import annotations

from .base import ScriptProvider, register
from .script_claude import DEFAULT_SYSTEM, build_brushup_prompt, build_user_prompt, _parse

_FALLBACK_MODEL = "gpt-4o"


@register("script", "chatgpt_api")
class OpenAIScript(ScriptProvider):
    @classmethod
    def available(cls, cfg: dict):
        if not cfg.get("openai_api_key"):
            return False, "openai_api_key 未設定"
        return True, ""

    def _call(self, cfg, system: str, user: str, log) -> str:
        from openai import OpenAI
        client = OpenAI(api_key=cfg["openai_api_key"])
        model = cfg.get("openai_text_model") or "gpt-5.5"
        msgs = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
        try:
            resp = client.chat.completions.create(model=model, messages=msgs)
        except Exception as e:
            if "model" in str(e).lower() and model != _FALLBACK_MODEL:
                log(f"      モデル {model} が使えないため {_FALLBACK_MODEL} で再試行…")
                resp = client.chat.completions.create(model=_FALLBACK_MODEL, messages=msgs)
            else:
                raise
        return (resp.choices[0].message.content or "").strip()

    def generate(self, cfg: dict, log=print) -> dict:
        system = (cfg.get("script_prompt") or "").strip() or DEFAULT_SYSTEM
        log("    ChatGPT(API)で台本を生成中…")
        return _parse(self._call(cfg, system, build_user_prompt(cfg), log))

    def brushup(self, script: str, cfg: dict, log=print) -> str:
        log("    ChatGPT(API)で台本をブラッシュアップ中…")
        return self._call(cfg, DEFAULT_SYSTEM, build_brushup_prompt(script, cfg), log).strip()
