"""台本から N 個のシーン（画像/動画生成用プロンプト）を作る＝絵コンテ。

動画の最後まで画像/動画を切り替えるため、台本を時系列で N 分割し、各区間に合う
ビジュアルのプロンプトを用意する。
- Anthropic キーがあれば Claude で「英語の視覚シーン記述 × N」を生成（高品質）。
- 無ければ台本を N 等分し、各区間の要約文をシーンに使う（API不要フォールバック）。
"""
from __future__ import annotations

import json
import re


def build_scenes(script: str, n: int, cfg: dict, log=print) -> list[str]:
    n = max(1, int(n or 1))
    script = (script or "").strip()
    if not script:
        return [(cfg.get("topic") or "an engaging scene")] * n
    if n == 1:
        return [(cfg.get("topic") or script[:80])]
    # 絵コンテはAnthropicキーがあれば台本エンジンに関係なく使う（2026-07-16修正:
    # 旧来は script_engine=="api" 限定で、claude_web等では日本語台本の断片が
    # そのまま画像プロンプトになっていた＝「画像プロンプトが反映されない」の一因）
    if cfg.get("anthropic_api_key"):
        try:
            scenes = _claude_storyboard(script, n, cfg, log)
            if scenes and len(scenes) >= 1:
                # 足りなければ最後を複製、超えれば切る
                scenes = (scenes + [scenes[-1]] * n)[:n]
                return scenes
        except Exception as e:
            log(f"    絵コンテ生成に失敗→台本分割で代替（{e}）")
    return _chunk_scenes(script, n)


def _claude_storyboard(script: str, n: int, cfg: dict, log=print) -> list[str]:
    import anthropic
    client = anthropic.Anthropic(api_key=cfg["anthropic_api_key"])
    model = cfg.get("ai_model") or "claude-opus-4-8"
    style_hint = (cfg.get("visual_prompt_template") or "").strip()
    sys_p = ("You are a professional storyboard artist for narrated short videos. "
             "Given a Japanese narration script, break it into chronological scenes and write a "
             "concrete English image-generation prompt for EACH scene that VISUALLY MATCHES what "
             "that part of the script is saying (the events, characters, place, action and emotion).")
    portrait = int(cfg.get("video_height", 1080)) > int(cfg.get("video_width", 1920))
    aspect = "9:16（縦動画）" if portrait else "16:9"
    user = (
        f"次の日本語ナレーション台本を読み、内容に沿って時系列で {n} 場面に分け、"
        f"各場面に『その台詞の内容と一致する絵』の画像生成プロンプトを英語で1つずつ作ってください。\n"
        "要件:\n"
        "・各プロンプトはその場面で“何が起きているか”を具体的に描写（登場人物・表情・動作・場所・時間帯・構図）。\n"
        "・全場面で画風・キャラクター・世界観を一貫させる（同じ作品の連続カットに見えるように）。\n"
        f"・文字/テロップ/ロゴ/透かしは描かない。{aspect}。\n"
        + (f"・共通の画風指定: {style_hint}\n" if style_hint else "")
        + f"出力は厳密に JSON 配列（英語文字列を{n}個、時系列順）だけ。前後に説明文やコードフェンスを付けない。\n\n"
        f"--- 台本 ---\n{script}"
    )
    log(f"    Claudeで絵コンテ（{n}場面）を作成中…")
    resp = client.messages.create(
        model=model, max_tokens=2000, system=sys_p,
        messages=[{"role": "user", "content": user}])
    text = "".join(getattr(b, "text", "") for b in resp.content).strip()
    return _extract_json_list(text)


def _extract_json_list(text: str) -> list[str]:
    # ```json ブロックや前後ノイズに強く JSON 配列を取り出す
    m = re.search(r"\[.*\]", text, flags=re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(0))
            return [str(x).strip() for x in data if str(x).strip()]
        except Exception:
            pass
    # 配列で取れなければ行分割
    lines = [re.sub(r'^\s*[\d\-\."]+\s*', "", ln).strip().strip('",')
             for ln in text.splitlines() if ln.strip()]
    return [ln for ln in lines if len(ln) > 4]


def build_chapter_pairs(script: str, n: int) -> list[tuple[str, str]]:
    """概要欄チャプターの (見出し, 章の先頭文) を返す（決定論・API不要）。

    先頭文はそのまま返す＝時刻を audio/timings.json の実発話時刻と突き合わせるための鍵
    （2026-08-05配布先報告: 画像グリッド時刻だと後半で最大68秒ズレていた）。"""
    n = max(1, int(n or 1))
    sents = [s.strip() for s in re.split(r"(?<=[。！？\n])", (script or "")) if s.strip()]
    if not sents:
        return []
    per = max(1, len(sents) // n)
    out = []
    for i in range(n):
        part = sents[i * per:(i + 1) * per] if i < n - 1 else sents[i * per:]
        sent = (part[0] if part else sents[-1]).strip()
        head = re.sub(r"[「」『』()（）。、！!？?…\s]", "", sent)[:12]
        out.append((head or f"チャプター{i + 1}", sent))
    return out


def build_chapters(script: str, n: int) -> list[str]:
    """概要欄チャプター用の日本語見出し（互換API・manifest保存用）。"""
    return [h for h, _ in build_chapter_pairs(script, n)]


def _chunk_scenes(script: str, n: int) -> list[str]:
    sents = [s.strip() for s in re.split(r"(?<=[。！？\n])", script) if s.strip()]
    if not sents:
        return [script[:80]] * n
    per = max(1, len(sents) // n)
    scenes = []
    for i in range(n):
        part = sents[i * per:(i + 1) * per] if i < n - 1 else sents[i * per:]
        text = "".join(part) or sents[min(i, len(sents) - 1)]
        scenes.append(text[:120])
    return scenes[:n]
