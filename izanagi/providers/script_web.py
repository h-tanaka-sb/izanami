"""①台本生成（Web自動操作）: claude.ai / ChatGPT / Gemini。

共有Chromeにログイン済みの前提で、APIキー無し運用を可能にする。出力の解析は
script_claude._parse を共用する。
"""
from __future__ import annotations

from .. import browser_ai
from .base import ScriptProvider, register
from .script_claude import DEFAULT_SYSTEM, build_brushup_prompt, build_user_prompt, _parse


def _build_prompt(cfg: dict) -> str:
    system = (cfg.get("script_prompt") or "").strip() or DEFAULT_SYSTEM
    return f"{system}\n\n{build_user_prompt(cfg)}"


class _WebScript(ScriptProvider):
    _gen = None  # browser_ai の生成関数

    @classmethod
    def needs_browser(cls):
        return True

    def _debug_dir(self, cfg):
        return cfg.get("_debug_dir") if cfg.get("debug_dump") else None

    def _call(self, cfg: dict, system: str, user: str, log=print) -> str:
        """生テキスト依頼（お題生成など、台本テンプレートを通したくない用途）。

        これが無いと topics.generate_topics が generate() に落ち、AIが台本を書いて
        その本文が「お題」としてお題欄に流れ込む（2026-07-08 配布先で実際に発生）。"""
        if self.ctx is None:
            raise RuntimeError("Webセッションなし（設定・キーの「ログイン用ブラウザ」でログインを）")
        kw = {}
        if cfg.get("_web_timeout"):   # 単発の小さな依頼は短いタイムアウトで（既定=各_genの360秒）
            kw["timeout"] = int(cfg["_web_timeout"])
        try:
            out = type(self)._gen(self.ctx, f"{system}\n\n{user}", log=log,
                                  debug_dir=self._debug_dir(cfg), **kw)
        except TypeError:   # timeout引数を持たない_gen実装は従来どおり
            out = type(self)._gen(self.ctx, f"{system}\n\n{user}", log=log,
                                  debug_dir=self._debug_dir(cfg))
        if not out:
            raise RuntimeError("Webからの応答が空でした")
        return out

    def generate(self, cfg: dict, log=print) -> dict:
        if self.ctx is None:
            log("    Webセッションが無いため mock 同等で停止。")
            return {"script": "", "title": "", "description": "", "tags": []}
        raw = type(self)._gen(self.ctx, _build_prompt(cfg), log=log, debug_dir=self._debug_dir(cfg))
        if not raw:
            return {"script": "", "title": "", "description": "", "tags": []}
        return _parse(raw)

    def brushup(self, script: str, cfg: dict, log=print) -> str:
        if self.ctx is None:
            return script
        out = type(self)._gen(self.ctx, build_brushup_prompt(script, cfg), log=log,
                              debug_dir=self._debug_dir(cfg))
        return out.strip() if out else script


@register("script", "claude_web")
class ClaudeWebScript(_WebScript):
    _gen = staticmethod(browser_ai.claude_generate)


@register("script", "chatgpt_web")
class ChatGPTWebScript(_WebScript):
    _gen = staticmethod(browser_ai.chatgpt_text)


@register("script", "gemini_web")
class GeminiWebScript(_WebScript):
    _gen = staticmethod(browser_ai.gemini_text)
