"""②無料TTS: Microsoft Edge の読み上げ（edge-tts）。

APIキー・課金・別アプリ不要（ネット接続のみ）。日本語ニューラル音声（Nanami女/Keita男）。
各行を個別にmp3合成→ffmpegでPCM化して結合（segments同期）。
※Microsoftの非公開エンドポイント利用のため、稀に仕様変更でedge-ttsの更新が要る。
"""
from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

from .. import util
from .base import TTSProvider, register, write_wav


def _run_coro(coro):
    """新規イベントループを専用スレッドで回してコルーチンを実行する。

    ①台本=ブラウザ無料（Playwright sync API）を使うと、同じワーカースレッドに
    イベントループが常駐し、asyncio.run() が
    'cannot be called from a running event loop' で100%失敗する
    （2026-07-16 配布先報告: ブラウザ台本＋Edge音声の無料構成が全滅）。
    別スレッドの新規ループで回せば競合しない。"""
    box: dict = {}

    def _runner():
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            box["value"] = loop.run_until_complete(coro)
        except BaseException as e:  # 呼び出し側へそのまま伝播させる
            box["error"] = e
        finally:
            try:
                loop.close()
            finally:
                asyncio.set_event_loop(None)

    th = threading.Thread(target=_runner, daemon=True)
    th.start()
    th.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")

# 多言語(Multilingual)ボイスは日本語もそのまま話せて、日本語専用より自然に聞こえることが多い
EDGE_VOICES = [
    "ja-JP-NanamiNeural (女)",
    "ja-JP-KeitaNeural (男)",
    "en-US-AvaMultilingualNeural (女・自然)",
    "en-US-EmmaMultilingualNeural (女・自然)",
    "en-US-AndrewMultilingualNeural (男・自然)",
    "en-US-BrianMultilingualNeural (男・自然)",
]


@register("tts", "edge")
class EdgeTTS(TTSProvider):
    @classmethod
    def available(cls, cfg: dict):
        try:
            import edge_tts  # noqa: F401
            return True, ""
        except Exception:
            return False, "edge-tts 未インストール（uv sync で入ります）"

    def synthesize(self, cues: list[str], cfg: dict, out_wav: str, log=print) -> list[dict]:
        import edge_tts
        voice = (cfg.get("edge_voice") or "ja-JP-NanamiNeural").split(" ")[0]
        rate = cfg.get("edge_rate") or "+0%"
        sr = int(cfg.get("tts_sample_rate", 24000))
        gap = 0.25
        gap_pcm = b"\x00\x00" * int(gap * sr)
        ff = util.find_ffmpeg()
        tmpdir = Path(out_wav).parent
        pcm = bytearray(); timings = []; t = 0.0
        fails = 0
        total = len([c for c in cues if c.strip()])
        log(f"    Edge(無料)TTSで合成中（{total}行・{voice}）…")
        done = 0
        consec_fails = 0
        for i, text in enumerate(cues):
            from .. import browser_ai as _ba
            if _ba._stopped():
                raise RuntimeError("中断要求により停止（Edge TTS）")
            if not text.strip():
                continue
            done += 1
            body = b""
            for attempt in range(3):
                try:
                    mp3 = tmpdir / f"_edge_{i:03d}.mp3"
                    # wait_for必須: websocketハングで朝まで固まるのを防ぐ
                    # 読みはカタカナ変換して渡す（読み間違い対策・テロップは原文）
                    from .base import reading_for_tts
                    async def _save(_t=reading_for_tts(text, cfg), _p=mp3):
                        await asyncio.wait_for(
                            edge_tts.Communicate(_t, voice, rate=rate).save(str(_p)),
                            timeout=90)
                    _run_coro(_save())  # asyncio.run はPlaywright常駐ループと競合するため不可
                    pr = util.run([ff, "-y", "-hide_banner", "-i", str(mp3),
                                   "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
                                  capture_output=True, timeout=120)
                    body = pr.stdout or b""
                    try:
                        mp3.unlink()
                    except Exception:
                        pass
                    if body:
                        break
                except Exception as e:
                    if "running event loop" in str(e):
                        # 環境要因ではなく内部競合＝リトライ/時間経過では直らない。
                        # 「回線/仕様変更」と誤案内せず即座に正しい原因で止める
                        raise RuntimeError(
                            "内部エラー（イベントループ競合）: Edge TTSの実行方式の不具合です。"
                            "最新版へ更新してください（回線やキーの問題ではありません）。") from e
                    if attempt == 2:
                        log(f"      {done}行目 合成失敗: {str(e)[:80]}")
                time.sleep(0.6 * (attempt + 1))
            if not body:  # 失敗行は無音で継続（全体を止めない）
                fails += 1
                consec_fails += 1
                if consec_fails >= 5:
                    # 回線断/仕様変更で全滅コース → 無音動画を作らずステージ失敗にする
                    raise RuntimeError("Edge TTSが5行連続で失敗（回線/仕様変更の可能性）。"
                                       "時間を置くか②音声をGoogle Cloudに切り替えてください。")
                body = b"\x00\x00" * int(max(0.6, len(text) * 0.12) * sr)
            else:
                consec_fails = 0
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
            log(f"    ⚠ {fails}/{total}行が無音になりました（回線/一時的な失敗）。再開で作り直せます。")
        write_wav(bytes(pcm), out_wav, sr)
        return timings
