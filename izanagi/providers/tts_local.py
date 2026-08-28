"""②ローカル/非クラウドの音声。

- VoicevoxTTS (engine="voicevox"): VOICEVOX / AivisSpeech のローカルREST(既定 localhost:50021)。
    無料・ローカルの“収録キャラ音声”。台本を行ごとに合成（segments同期）。
    ※AivisSpeechは既定ポートが違う場合あり→ voicevox_url で変更。VOICEVOXは生成物にクレジット表記が必要。
- FileTTS (engine="file"): 自分で用意した録音ナレーション(narration_file)を音声に使う。
    テロップは whisper 強制アラインで自動同期（align_methodに関わらずwhisper）。
"""
from __future__ import annotations

from pathlib import Path

from .. import util
from .base import TTSProvider, reading_for_tts, register, write_wav, strip_wav_header


@register("tts", "voicevox")
class VoicevoxTTS(TTSProvider):
    @classmethod
    def available(cls, cfg: dict):
        url = (cfg.get("voicevox_url") or "http://127.0.0.1:50021").rstrip("/")
        try:
            import httpx
            httpx.get(f"{url}/version", timeout=3.0)
            return True, ""
        except Exception:
            return False, f"VOICEVOX/AivisSpeechが見つかりません（{url} を起動してください）"

    def synthesize(self, cues: list[str], cfg: dict, out_wav: str, log=print) -> list[dict]:
        import httpx
        url = (cfg.get("voicevox_url") or "http://127.0.0.1:50021").rstrip("/")
        spk = int(cfg.get("voicevox_speaker", 3))
        sr = int(cfg.get("tts_sample_rate", 24000))
        rate = float(cfg.get("tts_speaking_rate", 1.0))
        gap = 0.25
        gap_pcm = b"\x00\x00" * int(gap * sr)
        pcm = bytearray(); timings = []; t = 0.0
        log(f"    VOICEVOX/AivisSpeech で合成中（{len(cues)}行・speaker={spk}）…")
        with httpx.Client(timeout=120.0) as cli:
            for text in cues:
                if not text.strip():
                    continue
                # 読み仮名を適用してから合成（2026-07-24修正: ここだけ生テキストを送っており
                # ローカル音声では読み方辞書・読み仮名マップが全く効いていなかった）
                q = cli.post(f"{url}/audio_query",
                             params={"text": reading_for_tts(text, cfg), "speaker": spk}).json()
                q["speedScale"] = rate
                q["outputSamplingRate"] = sr
                q["outputStereo"] = False
                wav = cli.post(f"{url}/synthesis", params={"speaker": spk}, json=q).content
                body = strip_wav_header(wav)
                dur = len(body) / (sr * 2)
                timings.append({"text": text, "start": round(t, 3), "end": round(t + dur, 3)})
                pcm += body + gap_pcm
                t += dur + gap
        write_wav(bytes(pcm), out_wav, sr)
        return timings


@register("tts", "file")
class FileTTS(TTSProvider):
    """録音済みナレーションファイルをそのまま音声に使う（テロップはwhisperで自動同期）。"""

    @classmethod
    def available(cls, cfg: dict):
        f = (cfg.get("narration_file") or "").strip()
        if f and Path(f).exists():
            return True, ""
        return False, "narration_file（録音した音声ファイル）が未設定です"

    def _convert(self, cfg: dict, out_wav: str) -> float:
        src = cfg["narration_file"]
        sr = int(cfg.get("tts_sample_rate", 24000))
        out_wav = Path(out_wav); out_wav.parent.mkdir(parents=True, exist_ok=True)
        util.run([util.find_ffmpeg(), "-y", "-hide_banner", "-i", str(src),
                  "-ac", "1", "-ar", str(sr), "-c:a", "pcm_s16le", str(out_wav)],
                 capture_output=True)
        from .. import compose
        return compose.media_duration(out_wav)

    def synthesize_full(self, text: str, cfg: dict, out_wav: str, log=print) -> float | None:
        log("    録音ナレーションを読み込み中（whisperでテロップ同期します）…")
        return self._convert(cfg, out_wav)

    def synthesize(self, cues: list[str], cfg: dict, out_wav: str, log=print) -> list[dict]:
        # 通常は stage_tts が file の時 whisper 経路に回す。保険として等分timingを返す。
        dur = self._convert(cfg, out_wav) or 1.0
        n = max(1, len(cues)); per = dur / n
        return [{"text": c, "start": round(i * per, 3), "end": round((i + 1) * per, 3)}
                for i, c in enumerate(cues)]
