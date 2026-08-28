"""②Gemini / Google AI Studio の TTS（gemini-2.5-*-tts）。

AI Studio で発行した API キー（settings.gemini_api_key）で google-genai SDK 経由で合成する。
Web自動操作は不要（公式APIあり）。出力は24kHz/16bit/mono の生PCM。

★重要（レート制限対策）: 台本を「1行ずつ」ではなく「数行をまとめたチャンク」で合成する。
  186行を186回呼ぶと無料枠のレート制限で落ちるため、チャンク（既定~3500バイト）に束ねて
  数回だけ呼ぶ。テロップの時間は各チャンク音声長を、含まれる行の文字数比で分配する
  （whisper不要・高速）。声質を最優先したい場合は align_method=whisper で別途同期も可能。
"""
from __future__ import annotations

import time

from .base import TTSProvider, register, write_wav, strip_wav_header

# よく使うプリセット音声（全リストはドキュメント参照）
GEMINI_VOICES = ["Kore", "Puck", "Charon", "Aoede", "Fenrir", "Leda", "Orus", "Zephyr"]

_CHUNK_BYTES = 3500  # 1回の合成に入れる最大バイト（8000上限に対し安全側・呼び出し回数を抑える）


def _style_prefix(cfg: dict) -> str:
    """話し方の指示（settings.gemini_tts_style）を合成テキストの前置き指示にする。

    Gemini TTS は「◯◯という話し方で: 本文」の形式でスタイル指示を解釈する
    （指示部分は読み上げない）。空なら何も付けない。
    """
    style = (cfg.get("gemini_tts_style") or "").strip()
    if not style:
        return ""
    return f"次の文章を「{style}」という話し方・声のトーンで読み上げてください。指示は読まず、本文だけを読むこと:\n"


@register("tts", "gemini")
class GeminiTTS(TTSProvider):
    @classmethod
    def available(cls, cfg: dict):
        if not cfg.get("gemini_api_key"):
            return False, "gemini_api_key（AIスタジオのAPIキー）が未設定です"
        return True, ""

    def _client_cfg(self, cfg):
        from google import genai
        from google.genai import types as gt
        # 音声専用キー（例: AIスタジオのonsei_dev）があればそれを優先。空なら共通キー
        key = (cfg.get("gemini_tts_api_key") or "").strip() or cfg["gemini_api_key"]
        client = genai.Client(api_key=key)
        model = cfg.get("gemini_tts_model") or "gemini-3.1-flash-tts-preview"
        voice = cfg.get("gemini_tts_voice") or "Kore"
        speech = gt.SpeechConfig(voice_config=gt.VoiceConfig(
            prebuilt_voice_config=gt.PrebuiltVoiceConfig(voice_name=voice)))
        return client, gt, model, voice, speech

    def _synth_chunk(self, client, gt, model, speech, text, log) -> bytes:
        """1チャンクを合成（レート制限を長めのバックオフでリトライ）。失敗はb''。"""
        for attempt in range(4):
            try:
                resp = client.models.generate_content(
                    model=model, contents=text,
                    config=gt.GenerateContentConfig(
                        response_modalities=["AUDIO"], speech_config=speech))
                data = _extract_audio(resp)
                if data:
                    return strip_wav_header(data)
            except Exception as e:
                msg = str(e)
                rate = ("429" in msg or "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower())
                wait = (20.0 if rate else 4.0) * (attempt + 1)
                if attempt == 3:
                    log(f"      チャンク合成失敗: {msg[:100]}")
                else:
                    log(f"      {'レート制限' if rate else 'エラー'}→{wait:.0f}秒待って再試行（{attempt+1}/4）")
                time.sleep(wait)
                continue
            time.sleep(3.0)  # 無音応答時も少し待つ
        return b""

    def synthesize(self, cues: list[str], cfg: dict, out_wav: str, log=print) -> list[dict]:
        client, gt, model, voice, speech = self._client_cfg(cfg)
        sr = 24000
        gap = 0.3
        gap_pcm = b"\x00\x00" * int(gap * sr)

        # 行を「チャンク（数行の束）」にまとめる
        chunks: list[list[int]] = []
        cur: list[int] = []; cur_b = 0
        for i, c in enumerate(cues):
            if not c.strip():
                continue
            b = len(c.encode("utf-8"))
            if cur and cur_b + b > _CHUNK_BYTES:
                chunks.append(cur); cur = [i]; cur_b = b
            else:
                cur.append(i); cur_b += b
        if cur:
            chunks.append(cur)

        n_lines = sum(len(ch) for ch in chunks)
        prefix = _style_prefix(cfg)
        log(f"    Gemini TTS で合成中（{n_lines}行を{len(chunks)}チャンクに束ねて呼び出し・{voice}"
            f"{'・話し方指示あり' if prefix else ''}）…")
        pcm = bytearray(); timings: list[dict] = []; t = 0.0
        fails = 0
        for ci, idxs in enumerate(chunks):
            from .. import browser_ai as _ba
            if _ba._stopped():
                raise RuntimeError("中断要求により停止（Gemini TTS）")
            from .base import reading_for_tts
            text = "".join(reading_for_tts(cues[i], cfg) for i in idxs)
            body = self._synth_chunk(client, gt, model, speech, prefix + text, log)
            if not body:  # チャンク失敗→その分を無音で継続
                fails += len(idxs)
                body = b"\x00\x00" * int(max(0.6, len(text) * 0.13) * sr)
            chunk_dur = len(body) / (sr * 2)
            # チャンク内の各行を文字数比で時間配分
            total_chars = sum(len(cues[i]) for i in idxs) or 1
            ct = t
            for i in idxs:
                d = chunk_dur * (len(cues[i]) / total_chars)
                timings.append({"text": cues[i], "start": round(ct, 3), "end": round(ct + d, 3)})
                ct += d
            pcm += body + gap_pcm
            t += chunk_dur + gap
            log(f"      …{ci+1}/{len(chunks)}チャンク")
        max_ratio = float(cfg.get("tts_max_silent_ratio", 0.15))
        if n_lines and fails / n_lines > max_ratio:
            # 半分無音の動画を完成させない（→ pipeline側のTTSフォールバックが発動する）
            raise RuntimeError(f"無音行が{fails}/{n_lines}（{fails / n_lines:.0%}）で閾値"
                               f"{max_ratio:.0%}を超過（quota/レート制限）。")
        if fails:
            log(f"    ⚠ {fails}行分が無音になりました（レート制限等）。api(Google Cloud)/edge も検討を。")
        write_wav(bytes(pcm), out_wav, sr)
        return timings

    def synthesize_full(self, text: str, cfg: dict, out_wav: str, log=print) -> float | None:
        """align_method=whisper 用の全文一括合成（チャンク結合・timepoint無）。"""
        client, gt, model, voice, speech = self._client_cfg(cfg)
        import re
        sr = 24000
        sents = [s for s in re.split(r"(?<=[。！？\n])", text) if s.strip()]
        chunks: list[str] = []; cur = ""
        for s in sents:
            if cur and len((cur + s).encode("utf-8")) > _CHUNK_BYTES:
                chunks.append(cur); cur = s
            else:
                cur += s
        if cur:
            chunks.append(cur)
        prefix = _style_prefix(cfg)
        log(f"    Gemini TTS 全文合成（{len(chunks)}チャンク・{voice}{'・話し方指示あり' if prefix else ''}）…")
        pcm = bytearray()
        fails = 0
        from .base import reading_for_tts
        from .. import browser_ai as _ba
        for i, chunk in enumerate(chunks):
            if _ba._stopped():
                raise RuntimeError("中断要求により停止（Gemini TTS）")
            body = self._synth_chunk(client, gt, model, speech,
                                     prefix + reading_for_tts(chunk, cfg), log)
            if body:
                pcm += body
            else:
                fails += 1
            log(f"      …{i+1}/{len(chunks)}チャンク")
        if not pcm:
            return None
        if fails:
            # チャンク欠けはwhisperアラインしても該当区間の音声自体が無い＝作り直しが正解
            raise RuntimeError(f"{fails}/{len(chunks)}チャンクが合成できませんでした"
                               "（quota/レート制限）。")
        write_wav(bytes(pcm), out_wav, sr)
        return len(pcm) / (sr * 2)


def _extract_audio(resp) -> bytes:
    try:
        for cand in (resp.candidates or []):
            cont = getattr(cand, "content", None)
            for part in (getattr(cont, "parts", None) or []):
                inline = getattr(part, "inline_data", None)
                data = getattr(inline, "data", None) if inline else None
                if data:
                    if isinstance(data, str):
                        import base64
                        return base64.b64decode(data)
                    return data
    except Exception:
        pass
    return b""
