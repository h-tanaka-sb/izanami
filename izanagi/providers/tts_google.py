"""②ナレーションTTS: Google Cloud Text-to-Speech。

Phase1の同期方式 align_method="segments":
  テロップ行（cue）ごとに個別合成し、各行の音声長＝その行の表示時間にする。
  → 行と音声が必ず一致し、SSML<mark>連結の落とし穴を避けられる。
認証: settings.google_tts_credentials_json があればサービスアカウント、無ければ ADC。
"""
from __future__ import annotations

import os
from pathlib import Path

from .base import TTSProvider, reading_for_tts, register, strip_wav_header, write_wav
from .. import browser_ai as _ba  # ⏹停止をAPIループにも伝える


@register("tts", "api")
class GoogleCloudTTS(TTSProvider):
    @classmethod
    def available(cls, cfg: dict):
        cred = (cfg.get("google_tts_credentials_json") or "").strip()
        if cred and Path(cred).exists():
            return True, ""
        if os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
            return True, ""
        return False, "google_tts_credentials_json 未設定（ADCも無し）"

    def _client(self, cfg):
        from google.cloud import texttospeech
        cred = (cfg.get("google_tts_credentials_json") or "").strip()
        if cred and Path(cred).exists():
            from google.oauth2 import service_account
            creds = service_account.Credentials.from_service_account_file(cred)
            return texttospeech.TextToSpeechClient(credentials=creds)
        return texttospeech.TextToSpeechClient()

    def synthesize(self, cues: list[str], cfg: dict, out_wav: str, log=print) -> list[dict]:
        from google.cloud import texttospeech as tts
        client = self._client(cfg)
        sr = int(cfg.get("tts_sample_rate", 24000))
        voice = tts.VoiceSelectionParams(
            language_code=cfg.get("tts_lang", "ja-JP"),
            name=cfg.get("tts_voice", "ja-JP-Neural2-C"),
        )
        audio_config = tts.AudioConfig(
            audio_encoding=tts.AudioEncoding.LINEAR16,
            sample_rate_hertz=sr,
            speaking_rate=float(cfg.get("tts_speaking_rate", 1.0)),
            pitch=float(cfg.get("tts_pitch", 0.0)),
        )
        gap = 0.25  # 行間の間（秒）
        gap_pcm = b"\x00\x00" * int(gap * sr)

        import time
        pcm = bytearray()
        timings: list[dict] = []
        t = 0.0
        total = len([c for c in cues if c.strip()])
        fails = 0; done = 0
        log(f"    Google TTSで合成中（{total}行・{voice.name}）…")
        for i, text in enumerate(cues):
            if _ba._stopped():
                raise RuntimeError("中断要求により停止（Google TTS）")
            if not text.strip():
                continue
            done += 1
            body = b""
            for attempt in range(3):
                try:
                    # timeout必須: 未指定だと通信が固まった時に無期限で待ち「停止」に見える
                    # 読みはカタカナ変換して渡す（読み間違い対策・テロップは原文）
                    resp = client.synthesize_speech(
                        input=tts.SynthesisInput(text=reading_for_tts(text, cfg)),
                        voice=voice, audio_config=audio_config, timeout=45,
                    )
                    body = strip_wav_header(resp.audio_content)
                    if body:
                        break
                except Exception as e:
                    if attempt == 0:
                        log(f"      {done}行目 リトライ中: {str(e)[:80]}")
                    if attempt == 2:
                        log(f"      {done}行目 失敗: {str(e)[:80]}")
                    time.sleep(1.5 * (attempt + 1))
            if not body:  # 失敗行は無音で継続
                fails += 1
                body = b"\x00\x00" * int(max(0.6, len(text) * 0.12) * sr)
            dur = len(body) / (sr * 2)
            timings.append({"text": text, "start": round(t, 3), "end": round(t + dur, 3)})
            pcm += body + gap_pcm
            t += dur + gap
            if done % 40 == 0:
                log(f"      …{done}/{total}行")
        max_ratio = float(cfg.get("tts_max_silent_ratio", 0.15))
        if total and fails / total > max_ratio:
            raise RuntimeError(f"無音行が{fails}/{total}（{fails / total:.0%}）で閾値"
                               f"{max_ratio:.0%}を超過。中止します（無音動画の防止）。")
        if fails:
            log(f"    ⚠ {fails}/{total}行が無音になりました。再開で作り直せます。")
        write_wav(bytes(pcm), out_wav, sr)
        return timings

    def synthesize_full(self, text: str, cfg: dict, out_wav: str, log=print) -> float | None:
        """全文を文単位チャンク（5000バイト制限回避）で合成し1本のwavにする。"""
        import re
        from google.cloud import texttospeech as tts
        client = self._client(cfg)
        sr = int(cfg.get("tts_sample_rate", 24000))
        voice = tts.VoiceSelectionParams(language_code=cfg.get("tts_lang", "ja-JP"),
                                         name=cfg.get("tts_voice", "ja-JP-Neural2-C"))
        audio_config = tts.AudioConfig(
            audio_encoding=tts.AudioEncoding.LINEAR16, sample_rate_hertz=sr,
            speaking_rate=float(cfg.get("tts_speaking_rate", 1.0)),
            pitch=float(cfg.get("tts_pitch", 0.0)))
        sentences = [s for s in re.split(r"(?<=[。！？\n])", text) if s.strip()]
        # 4500バイト以内のチャンクへ詰める
        chunks, cur = [], ""
        for s in sentences:
            if len((cur + s).encode("utf-8")) > 4500 and cur:
                chunks.append(cur); cur = s
            else:
                cur += s
        if cur:
            chunks.append(cur)
        log(f"    Google TTS 全文合成（{len(chunks)}チャンク）…")
        pcm = bytearray()
        for c in chunks:
            if _ba._stopped():
                raise RuntimeError("中断要求により停止（Google TTS）")
            # 全文合成でも読み仮名を適用する（2026-07-24修正: ここだけ生テキストを
            # 送っており、whisperアライン利用時に読み間違い対策が丸ごと無効だった）
            resp = client.synthesize_speech(
                input=tts.SynthesisInput(text=reading_for_tts(c, cfg)),
                voice=voice, audio_config=audio_config, timeout=120)
            pcm += strip_wav_header(resp.audio_content)
        write_wav(bytes(pcm), out_wav, sr)
        return len(pcm) / (sr * 2)
