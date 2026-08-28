"""モック実装。APIキー無し/オフラインでもパイプライン全体を疎通確認できる。

- MockScript : 固定の日本語台本
- MockTTS    : 文字数に比例した無音を生成（テロップ同期の仕組みを検証できる）
- MockVisual : PILで背景画像を1枚生成
"""
from __future__ import annotations

from pathlib import Path

from .base import (ScriptProvider, TTSProvider, VisualProvider, VideoProvider,
                   ThumbnailProvider, UploadProvider, register, write_wav)

_SAMPLE_SCRIPT = (
    "今日はサーキット走行が劇的に速くなる、たった一つの考え方をお伝えします。\n"
    "多くのドライバーが、コーナーの入口でブレーキを残せていません。\n"
    "じつは、ブレーキを少しだけ引きずることで、フロントタイヤが食い込みます。\n"
    "これがトレイルブレーキングと呼ばれるテクニックです。\n"
    "今日から意識するだけで、あなたのタイムは確実に縮みます。\n"
    "チャンネル登録で、次の走行がもっと速くなる情報をお見逃しなく。"
)


@register("script", "mock")
class MockScript(ScriptProvider):
    def generate(self, cfg: dict, log=print) -> dict:
        topic = (cfg.get("topic") or "サーキット走行のコツ").strip()
        log(f"    [mock] 台本を生成（お題: {topic}）")
        cands = [{"pattern": "結果先出し", "title": f"{topic}を試した結果…"},
                 {"pattern": "伏せ字", "title": f"まさかの●●だった{topic}"},
                 {"pattern": "数字", "title": f"3分で分かる{topic}"},
                 {"pattern": "立場逆転", "title": f"{topic}で全てが逆転した話"},
                 {"pattern": "疑問形", "title": f"{topic}、本当に正しい？"}]
        from .script_claude import score_title
        return {
            "script": _SAMPLE_SCRIPT,
            "title": max(cands, key=lambda c: score_title(c["title"]))["title"],
            "title_candidates": cands,
            "thumb_text": "まさかの結末/衝撃の真相",
            "description": f"{topic} について解説します。",
            "tags": ["サーキット", "走行会", "ドライビング", "車好き"],
        }


@register("tts", "mock")
class MockTTS(TTSProvider):
    def synthesize(self, cues: list[str], cfg: dict, out_wav: str, log=print) -> list[dict]:
        sr = int(cfg.get("tts_sample_rate", 24000))
        rate = float(cfg.get("tts_speaking_rate", 1.0)) or 1.0
        sec_per_char = 0.16 / rate
        gap = 0.18
        pcm = bytearray()
        timings: list[dict] = []
        t = 0.0
        log(f"    [mock] 無音ナレーションを生成（{len(cues)}行）")
        for text in cues:
            dur = max(1.0, len(text) * sec_per_char)
            nsamp = int(dur * sr)
            pcm += b"\x00\x00" * nsamp
            timings.append({"text": text, "start": round(t, 3), "end": round(t + dur, 3)})
            t += dur
            # 行間の無音
            pcm += b"\x00\x00" * int(gap * sr)
            t += gap
        write_wav(bytes(pcm), out_wav, sr)
        return timings

    def synthesize_full(self, text: str, cfg: dict, out_wav: str, log=print) -> float | None:
        sr = int(cfg.get("tts_sample_rate", 24000))
        dur = max(1.0, len(text) * 0.12)
        write_wav(b"\x00\x00" * int(dur * sr), out_wav, sr)
        return dur


@register("visual", "mock")
class MockVisual(VisualProvider):
    def generate(self, cfg: dict, meta: dict, out_dir: str, log=print) -> dict:
        from PIL import Image, ImageDraw, ImageFont
        w = int(cfg.get("video_width", 1920))
        h = int(cfg.get("video_height", 1080))
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        scenes = (meta or {}).get("scenes") or [(meta or {}).get("title", "IZANAMI")]
        try:
            font = ImageFont.truetype("C:\\Windows\\Fonts\\YuGothB.ttc", 64)
        except Exception:
            font = ImageFont.load_default()
        images: list[str] = []
        log(f"    [mock] 背景画像を{len(scenes)}枚生成")
        for i, scene in enumerate(scenes):
            base = 30 + (i * 40) % 160
            img = Image.new("RGB", (w, h), (12, 28, 64))
            px = img.load()
            for y in range(h):
                t = y / h
                r = int(12 + base * t); g = int(28 + 60 * t); b = int(64 + 120 * t)
                for x in range(0, w, 4):
                    for dx in range(4):
                        if x + dx < w:
                            px[x + dx, y] = (r, g, b)
            draw = ImageDraw.Draw(img)
            draw.text((80, 80), f"[mock scene {i+1}/{len(scenes)}]", fill=(180, 200, 255), font=font)
            draw.text((80, 180), str(scene)[:24], fill=(255, 255, 255), font=font)
            path = out_dir / f"{i:02d}_mock.png"
            img.save(str(path), "PNG")
            images.append(str(path))
        return {"images": images, "clips": []}


@register("video", "mock")
class MockVideo(VideoProvider):
    def generate(self, cfg: dict, meta: dict, out_dir: str, log=print) -> dict:
        log("    [mock] 動画クリップ生成はスキップ（画像のみで合成）。")
        return {"clips": []}


@register("upload", "mock")
class MockUpload(UploadProvider):
    def schedule_upload(self, video, meta, thumb, cfg, log=print) -> dict:
        log("    [mock] 投稿はしません（dry扱い）。")
        return {"ok": True, "dry_run": True, "publish_at": "", "note": "mock"}


@register("thumb", "mock")
class MockThumbnail(ThumbnailProvider):
    def generate(self, cfg: dict, meta: dict, out_png: str, log=print) -> bool:
        from PIL import Image, ImageDraw, ImageFont
        out_png = Path(out_png)
        out_png.parent.mkdir(parents=True, exist_ok=True)
        img = Image.new("RGB", (1280, 720), (8, 20, 48))
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype("C:\\Windows\\Fonts\\YuGothB.ttc", 72)
        except Exception:
            font = ImageFont.load_default()
        draw.text((60, 300), (meta or {}).get("title", "IZANAMI")[:16],
                  fill=(255, 255, 255), font=font)
        img.save(str(out_png), "PNG")
        return True
