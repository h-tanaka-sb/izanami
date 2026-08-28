"""whisper強制アライメント: 生成音声＋既知の台本(cue行) → (行, start, end)。

MVメーカー align_to_ass.py の文字単位 Needleman-Wunsch を関数化。
whisperの「単語時刻ストリーム」に台本cueを対応付ける。文字起こしテキストは時刻のためだけに使い、
表示テキストは常に台本（正本）を使う＝固有名詞の誤認に強い。
align_method="whisper"（Chirp3-HD等 timepoint非対応の声）用。
"""
from __future__ import annotations

import re

import numpy as np

_DROP_RE = re.compile(r"[\s,，、。.\!\?！？–\-—’'\"`()（）\[\]:：;；/\\・]+")


def _kata_to_hira(s: str) -> str:
    out = []
    for ch in s:
        c = ord(ch)
        out.append(chr(c - 0x60) if 0x30A1 <= c <= 0x30F6 else ch)
    return "".join(out)


def _normalize(s: str) -> str:
    return _DROP_RE.sub("", _kata_to_hira(s.lower()))


def transcribe_words(wav_path: str, log=print, model_size: str = "medium") -> tuple[list[dict], float]:
    """faster-whisper で単語タイムスタンプを取得。([{word,start,end}], duration) を返す。"""
    from faster_whisper import WhisperModel
    log(f"    whisper({model_size}) で音声を解析中…")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    # condition_on_previous_text=False: 既定Trueだと長尺の日本語で「繰り返しループ→
    # 以降の単語が丸ごと脱落」が起きやすい（15分超でテロップが止まる報告の引き金）。
    # 文脈引き継ぎを切ると精度は僅かに落ちるが、長尺の安定性を優先する
    segments, info = model.transcribe(
        wav_path, language="ja", word_timestamps=True, vad_filter=False,
        condition_on_previous_text=False, temperature=0.0)
    words: list[dict] = []
    for seg in segments:
        for w in (seg.words or []):
            words.append({"word": w.word, "start": w.start, "end": w.end})
    return words, float(getattr(info, "duration", 0.0) or 0.0)


def align(cues: list[str], words: list[dict], total_duration: float) -> list[dict]:
    """cue行（表示テキスト）を whisper words の時刻に対応付け、[{text,start,end}] を返す。"""
    singable = [(i, c, _normalize(c)) for i, c in enumerate(cues) if _normalize(c)]
    if not singable or not words:
        # 取れない場合は均等割り（保険）
        n = max(1, len(cues)); per = (total_duration or n) / n
        return [{"text": c, "start": round(i * per, 3), "end": round((i + 1) * per, 3)}
                for i, c in enumerate(cues)]

    lyric_chars = []  # (char, line_idx within singable)
    for li, (_, _, ntext) in enumerate(singable):
        for ch in ntext:
            lyric_chars.append((ch, li))

    whisper_chars = []  # (char, t_mid)
    for w in words:
        wt = _normalize(w["word"])
        if not wt:
            continue
        ws, we = float(w["start"]), float(w["end"])
        n = len(wt)
        for k, ch in enumerate(wt):
            whisper_chars.append((ch, ws + (we - ws) * (k + 0.5) / n))

    N, M = len(whisper_chars), len(lyric_chars)
    if N == 0 or M == 0:
        n = max(1, len(cues)); per = (total_duration or n) / n
        return [{"text": c, "start": round(i * per, 3), "end": round((i + 1) * per, 3)}
                for i, c in enumerate(cues)]

    MATCH, MISMATCH, GAP = 2, -1, -1
    S = np.zeros((M + 1, N + 1), dtype=np.int32)
    T = np.zeros((M + 1, N + 1), dtype=np.int8)
    S[:, 0] = np.arange(M + 1) * GAP
    S[0, :] = np.arange(N + 1) * GAP
    T[1:, 0] = 1
    T[0, 1:] = 2
    lyric_arr = np.array([ord(c) for c, _ in lyric_chars], dtype=np.int32)
    whisper_arr = np.array([ord(c) for c, _ in whisper_chars], dtype=np.int32)
    for i in range(1, M + 1):
        eq = (whisper_arr == lyric_arr[i - 1])
        sub = np.where(eq, MATCH, MISMATCH)
        diag = S[i - 1, :-1] + sub
        up = S[i - 1, 1:] + GAP
        cur_left = S[i, 0]
        for j in range(N):
            d = diag[j]; u = up[j]; l = cur_left + GAP
            if d >= u and d >= l:
                S[i, j + 1] = d; T[i, j + 1] = 0
            elif u >= l:
                S[i, j + 1] = u; T[i, j + 1] = 1
            else:
                S[i, j + 1] = l; T[i, j + 1] = 2
            cur_left = S[i, j + 1]

    line_to_times = [[] for _ in singable]
    i, j = M, N
    while i > 0 or j > 0:
        op = T[i, j]
        if i == 0:
            op = 2
        elif j == 0:
            op = 1
        if op == 0:
            line_to_times[lyric_chars[i - 1][1]].append(whisper_chars[j - 1][1])
            i -= 1; j -= 1
        elif op == 1:
            i -= 1
        else:
            j -= 1

    assigns = []
    for li, (_, display, ntext) in enumerate(singable):
        ts = sorted(line_to_times[li])
        if not ts:
            assigns.append(None); continue
        lo = ts[len(ts) // 10] if len(ts) >= 5 else ts[0]
        hi = ts[-(len(ts) // 10 + 1)] if len(ts) >= 5 else ts[-1]
        assigns.append({"text": display, "start": lo, "end": hi})

    # ── 異常マッチの無害化（2026-08-04・配布先の実動画18.6分で発生）──
    # 文字起こしが**中盤**で乱れると、DPが1つのcueを数分に引き伸ばすことがある
    # （実例: 20文字の一文が3分40秒表示され続け、その間の本文テロップが全滅。
    #   音声は正常に先へ進んでいるのにテロップだけ止まって見える）。
    # 中盤の乱れはカバレッジ検査（末尾の途切れ用）では検出できないため、
    # 「文字数から見て明らかに長すぎる」「時刻が大きく逆行する」割り当てを
    # 対応不能（None）へ格下げし、後段の文字数比フィルで前後の正常アンカー間へ
    # 並べ直す。
    total_chars = sum(max(1, len(s[2])) for s in singable) or 1
    est = (float(total_duration) / total_chars) if total_duration else 0.5
    prev_end = 0.0
    for k, a in enumerate(assigns):
        if a is None:
            continue
        chars = max(1, len(singable[k][2]))
        max_span = max(10.0, chars * est * 3.0)
        if (a["end"] - a["start"]) > max_span or a["start"] < prev_end - 5.0:
            assigns[k] = None
            continue
        prev_end = a["end"]

    # ── 文字起こし途切れの救済（2026-08-03・「テロップが15分超で止まる」の本修正）──
    # whisperが長尺の途中で脱落すると（繰り返しループ等）、それ以降のcueが全て
    # 脱落時刻付近に押し込まれ、映像と音声は続くのにテロップだけ止まる動画になる。
    # カバレッジ（最後のword時刻）が音声全長より30秒以上短い時は、そこに張り付いた
    # 末尾のcue群を [正常だった所のend, 音声全長] へ文字数比で並べ直す。
    # ※閾値は絶対秒。割合(全長×10%等)だと16分動画の60秒欠落を見逃す
    cov = max((float(w["end"]) for w in words if w.get("end") is not None), default=0.0)
    if total_duration and (total_duration - cov) > 30.0:
        idx = len(assigns)
        while idx > 0:
            a = assigns[idx - 1]
            if a is not None and a["start"] < cov - 5.0:
                break
            idx -= 1
        if idx < len(assigns):
            base_t = assigns[idx - 1]["end"] if idx > 0 and assigns[idx - 1] else cov
            tail = list(range(idx, len(assigns)))
            chars = [max(1, len(singable[k][2])) for k in tail]
            span = max(1.0, float(total_duration) - base_t)
            t = base_t
            for k, c in zip(tail, chars):
                d = span * c / sum(chars)
                assigns[k] = {"text": singable[k][1], "start": t, "end": t + d}
                t += d

    # None（対応取れず）は前後アンカー間へ文字数比で配分
    # （旧実装の「直前end+固定1.0秒」の連鎖は、脱落が続くと後半が押し潰れる）
    n_ = len(assigns)
    k = 0
    while k < n_:
        if assigns[k] is not None:
            k += 1
            continue
        j0 = k
        while k < n_ and assigns[k] is None:
            k += 1
        left = assigns[j0 - 1]["end"] if j0 > 0 and assigns[j0 - 1] else 0.0
        right = (assigns[k]["start"] if k < n_ and assigns[k]
                 else (float(total_duration) if total_duration else left + (k - j0)))
        if right <= left:
            right = left + (k - j0) * 1.0
        chars = [max(1, len(singable[m][2])) for m in range(j0, k)]
        t = left
        for m, c in zip(range(j0, k), chars):
            d = (right - left) * c / sum(chars)
            assigns[m] = {"text": singable[m][1], "start": t, "end": t + d}
            t += d

    # 単調性＆終端をのばす（ここまでで None は無い）
    for k, a in enumerate(assigns):
        if k > 0 and a["start"] < assigns[k - 1]["end"]:
            a["start"] = assigns[k - 1]["end"] + 0.02
    for k, a in enumerate(assigns):
        ns = assigns[k + 1]["start"] if k + 1 < len(assigns) else \
            (total_duration or a["end"] + 1.0)
        a["end"] = max(a["start"] + 0.6, min(a["end"], ns - 0.05))

    return [{"text": a["text"], "start": round(a["start"], 3),
             "end": round(a["end"], 3)} for a in assigns]
