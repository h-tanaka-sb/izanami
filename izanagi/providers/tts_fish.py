"""②Fish Audio の TTS（api.fish.audio/v1/tts）。

APIキー（settings.fish_api_key）で公式REST APIを叩く。日本語も高品質。
声は fish.audio でモデルを選んで得られる「reference_id」で指定（settings.fish_voice）。
未指定ならモデル既定の声。モデル（音質グレード）は fish_model ヘッダ（s1 / s2.1-pro など）。

各行を個別に合成（mp3）→ ffmpegでPCM化して結合（segments同期＝テロップと正確に一致）。
Edge版と同じ堅牢化: 3回リトライ／連続失敗ブレーカ／無音率ガード／⏹中断チェック。
"""
from __future__ import annotations

import time
from pathlib import Path

from .. import util
from .base import TTSProvider, register, write_wav

TTS_URL = "https://api.fish.audio/v1/tts"

# 声は reference_id（fish.audioのモデルID）で指定するため固定リストは無い。
# コンボは編集可にして、ここに「未指定＝既定の声」だけ入れておく。
FISH_VOICES = ["（デフォルトの声）"]

# 音質グレード（fish_model ヘッダ）。空/不正なら s2.1-pro-free。
FISH_MODELS = ["s2.1-pro-free", "s2.1-pro", "s2-pro", "s1"]


def _clean_ref(cfg: dict) -> str:
    ref = (cfg.get("fish_voice") or "").strip()
    return "" if ref in ("", "（デフォルトの声）") else ref


@register("tts", "fish")
class FishTTS(TTSProvider):
    @classmethod
    def available(cls, cfg: dict):
        if not (cfg.get("fish_api_key") or "").strip():
            return False, "Fish Audio の APIキー(fish_api_key)が未設定です"
        return True, ""

    def _synth_one(self, text: str, cfg: dict, sr: int, log) -> bytes:
        """1行を合成 → s16le/mono/sr のPCMで返す。失敗は b''。認証/残高NGは例外。"""
        import httpx
        key = (cfg.get("fish_api_key") or "").strip()
        model = (cfg.get("fish_model") or "s2.1-pro-free").strip()
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                   "model": model}
        # temperature/top_pを低めに＝行ごとのトーンのブレを抑える（既定0.7は揺れやすい）
        body = {"text": text, "format": "mp3", "mp3_bitrate": 128,
                "sample_rate": 44100, "normalize": True,
                "temperature": float(cfg.get("fish_temperature", 0.5)),
                "top_p": float(cfg.get("fish_top_p", 0.5))}
        ref = _clean_ref(cfg)
        if ref:
            body["reference_id"] = ref   # ← 声を固定（未設定だと毎回声が変わる主因）
        ff = util.find_ffmpeg()
        for attempt in range(3):
            try:
                r = httpx.post(TTS_URL, headers=headers, json=body, timeout=120)
                if r.status_code == 200 and r.content:
                    pr = util.run([ff, "-y", "-hide_banner", "-i", "pipe:0",
                                   "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
                                  input=r.content, capture_output=True, timeout=120)
                    if pr.stdout:
                        return pr.stdout
                elif r.status_code in (401, 402):
                    detail = ""
                    try:
                        detail = r.text[:150]
                    except Exception:
                        pass
                    raise RuntimeError(
                        f"Fish Audioの認証/残高エラー(HTTP {r.status_code})。"
                        f"APIキーと残高を確認してください。{detail}")
                elif attempt == 2:
                    try:
                        log(f"      Fish合成失敗(HTTP {r.status_code}): {r.text[:120]}")
                    except Exception:
                        log(f"      Fish合成失敗(HTTP {r.status_code})")
            except RuntimeError:
                raise
            except Exception as e:
                if attempt == 2:
                    log(f"      Fish合成失敗: {str(e)[:100]}")
            time.sleep(1.0 * (attempt + 1))
        return b""

    def synthesize(self, cues: list[str], cfg: dict, out_wav: str, log=print) -> list[dict]:
        from .base import reading_for_tts
        from .. import browser_ai as _ba
        sr = int(cfg.get("tts_sample_rate", 24000))
        gap = 0.25
        gap_pcm = b"\x00\x00" * int(gap * sr)
        model = (cfg.get("fish_model") or "s2.1-pro-free").strip()
        ref = _clean_ref(cfg)
        total = len([c for c in cues if c.strip()])
        if not ref:
            log("    ⚠ Fishの『声ID(reference_id)』が未設定です。既定の声は行ごとに声・トーンが"
                "変わります。fish.audioで声を1つ選び、作成タブの声欄にモデルIDを貼ってください。")
        log(f"    Fish Audio TTSで合成中（{total}行・{model}"
            f"{'・声=' + ref[:10] + '…' if ref else '・既定の声=バラつきます'}）…")
        pcm = bytearray(); timings: list[dict] = []; t = 0.0
        fails = 0; done = 0; consec = 0
        for text in cues:
            if _ba._stopped():
                raise RuntimeError("中断要求により停止（Fish TTS）")
            if not text.strip():
                continue
            done += 1
            body = self._synth_one(reading_for_tts(text, cfg), cfg, sr, log)
            if not body:
                fails += 1; consec += 1
                if consec >= 5:
                    raise RuntimeError("Fish TTSが5行連続で失敗（キー/残高/回線の可能性）。"
                                       "設定を確認するか、②音声をEdge等へ切り替えてください。")
                body = b"\x00\x00" * int(max(0.6, len(text) * 0.12) * sr)
            else:
                consec = 0
            dur = len(body) / (sr * 2)
            timings.append({"text": text, "start": round(t, 3), "end": round(t + dur, 3)})
            pcm += body + gap_pcm
            t += dur + gap
            if done % 30 == 0:
                log(f"      …{done}/{total}行")
        max_ratio = float(cfg.get("tts_max_silent_ratio", 0.15))
        if total and fails / total > max_ratio:
            raise RuntimeError(f"無音行が{fails}/{total}（{fails / total:.0%}）で閾値"
                               f"{max_ratio:.0%}を超過。ゴミ動画を防ぐため中止します。")
        if fails:
            log(f"    ⚠ {fails}/{total}行が無音になりました（一時的な失敗）。再開で作り直せます。")
        write_wav(bytes(pcm), out_wav, sr)
        return timings

    def synthesize_full(self, text: str, cfg: dict, out_wav: str, log=print) -> float | None:
        """align_method=whisper 用の全文一括合成（文単位に分けて合成→結合）。"""
        import re
        from .base import reading_for_tts
        from .. import browser_ai as _ba
        sr = int(cfg.get("tts_sample_rate", 24000))
        sents = [s for s in re.split(r"(?<=[。！？\n])", text) if s.strip()]
        log(f"    Fish Audio 全文合成（{len(sents)}文）…")
        pcm = bytearray(); fails = 0
        for i, s in enumerate(sents):
            if _ba._stopped():
                raise RuntimeError("中断要求により停止（Fish TTS）")
            body = self._synth_one(reading_for_tts(s, cfg), cfg, sr, log)
            if body:
                pcm += body
            else:
                fails += 1
            if (i + 1) % 30 == 0:
                log(f"      …{i + 1}/{len(sents)}文")
        if not pcm:
            return None
        max_ratio = float(cfg.get("tts_max_silent_ratio", 0.15))
        if sents and fails / len(sents) > max_ratio:
            raise RuntimeError(f"{fails}/{len(sents)}文が合成できませんでした（キー/残高/回線）。")
        write_wav(bytes(pcm), out_wav, sr)
        return len(pcm) / (sr * 2)
