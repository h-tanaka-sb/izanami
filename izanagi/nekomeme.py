"""猫ミームモード（video_mode="neko"）。

参考: _ref/nekomeme/（YMM4元プロジェクト＝主人公セリフ赤・その他白、1セリフ=1猫クリップ、
BGMループ、場面転換SE鳩ポッポ）を IZANAMI パイプラインに移植したもの。

- 台本: 「話者|感情タグ|セリフ」形式で生成（感情タグは素材表 TAG_CLIPS のキーに限定）
- 音声: 読み上げなし。セリフの文字数から表示時間を計算し、無音WAVを全長ぶん作る
- 映像: 各セリフに感情タグの猫クリップを割当て、行の長さに合わせて loop/カット
- 合成: クリップ連結 → 話者色分けASS焼込 + BGM（素材フォルダのmp3をランダム）
"""
from __future__ import annotations

import random
import re
from pathlib import Path

from . import compose as _compose
from . import util
from .providers.base import write_wav

# ── 感情タグ → 素材クリップ（nekomeme_dir 直下のファイル名） ─────────────
TAG_CLIPS = {
    "ハッピー": ["HappyHappy猫.mp4", "踊る子猫＋音楽.mp4"],
    "アンハッピー": ["Nothappy猫.mp4", "ショボーン猫.mp4"],
    "怒り": ["怒る猫.mp4", "文句を言う猫.mp4"],
    "驚き": ["目を見開いて驚く猫.mp4", "二度見する猫.mp4"],
    "悲しい": ["悲しい濡れた猫.mp4", "バナナ猫泣く.mp4"],
    "叫ぶ": ["手を挙げて叫ぶ猫.mp4", "突然叫ぶ猫.mp4", "叫ぶマーモット.mp4"],
    "はぁ？": ["はぁ？猫.mp4"],
    "ぽかん": ["ぽかんとする猫.mp4", "何あれ？猫.mp4"],
    "ドヤ": ["ドヤ顔猫アロハ.mp4", "ひじつき猫.mp4"],
    "笑う": ["げらげら笑う犬.mp4"],
    "うなずく": ["うなずく犬.mp4"],
    "ビビる": ["がちがち震える犬.mp4", "詰め寄る猫とビビる猫.mp4"],
    "気絶": ["気絶する猫.mp4", "頭を抱える猫.mp4"],
    "踊る": ["踊る猫EDM.mp4", "踊る猫ちびちびちゃぱちゃぱ.mp4", "踊る太めの猫（無音）.mp4"],
    "食べる": ["カリカリ噛む猫.mp4", "スナックを食べる猫.mp4", "きゅうり食う猫.mp4"],
    "寝る": ["いびきをかいて寝る猫.mp4", "ゆっくり目を閉じる猫.mp4"],
    "仕事": ["パソコン猫.mp4", "コールセンター猫.mp4"],
    "電話": ["コールセンター猫.mp4"],
    "話す": ["口パクパク猫.mp4", "マイク猫小.mp4", "アップで舌を出してしゃべるヤギ.mp4"],
    "お願い": ["両手でお願い猫.mp4", "見上げて要求する猫.mp4"],
    "嫌がる": ["嫌いな顔をする猫.mp4", "えずく猫.mp4"],
    "にらむ": ["じっと睨む猫とにらまれる猫.mp4", "睨む猫と目を合わさない犬.mp4"],
    "喧嘩": ["喧嘩する猫2匹.mp4", "殴る猫と殴られる猫.mp4"],
    # 「移動」は徒歩系のみ。乗り物は別タグへ隔離（室内シーンで車が出る事故の防止・
    # 2026-07-21 配布先報告H。「乗り物」はAIへの選択肢には出さない）
    "移動": ["バナナ猫ダッシュ.mp4"],
    "乗り物": ["前乗りバイク猫.mp4", "運転する猫.mp4"],
    "冷静": ["冷静な黒猫（爪とぎ）.mp4"],
    "もじもじ": ["手をもじもじ子猫.mp4"],
    "説教": ["説教する猫.mp4", "ちょっとちょっとちょっと犬.mp4"],
}
DEFAULT_TAG = "話す"

# 2匹以上が写っているクリップ（1キャラの担当としては使わない＝画面の頭数が合わなくなる）
_MULTI_ANIMAL = (
    "3匹猫", "猫2匹", "猫と犬", "猫と猫", "にらまれる猫", "目を合わさない犬",
    "殴られる猫", "はたかれる猫", "詰め寄る猫とビビる猫",
)


def _is_multi(name: str) -> bool:
    n = _nfc(Path(name).name)
    return any(_nfc(x) in n for x in _MULTI_ANIMAL)

# ファイル名→タグの自動分類ルール。TAG_CLIPS（厳選）に無い素材も、
# 基本フォルダ＋素材25A の全mp4をこのキーワードでタグへ振り分けて全量取り込む。
TAG_KEYWORDS = {
    "ハッピー": ["Happy", "ハッピー", "ハート", "仲良し", "猫と犬", "猫と猫"],
    "アンハッピー": ["Nothappy", "ショボーン", "しょんぼり", "ぬれて悲しい"],
    "説教": ["説教", "ちょっとちょっと"],
    "怒り": ["怒る", "文句", "機関銃"],
    "驚き": ["驚く", "驚き", "二度見"],
    "悲しい": ["悲しい", "泣く", "濡れた"],
    "叫ぶ": ["叫ぶ", "マーモット", "遠吠え"],
    "はぁ？": ["はぁ？", "はあ？"],
    "ぽかん": ["ぽかん", "何あれ", "感情無"],
    "ドヤ": ["ドヤ", "えらそう", "ひじつき", "モデル", "外国美人", "アロハ", "前髪長い", "付け毛"],
    "笑う": ["笑う"],
    "うなずく": ["うなずく", "首振り"],
    "ビビる": ["ビビる", "震える", "壁チラ", "がちがち"],
    "気絶": ["気絶", "頭をかかえる", "頭を抱える", "倒れる", "天を仰ぐ"],
    "踊る": ["踊る", "ダンス", "ダンシング", "ノリノリ", "DJ", "阿波踊り", "スピン", "揺れる",
             "ネズミ", "ねずみ", "ピアノ", "メキシコ"],
    "食べる": ["食う", "食べる", "カリカリ", "トウモロコシ", "リンゴ", "りんご", "寿司", "料理", "飯要求", "スナック", "草を食う"],
    "寝る": ["寝る", "いびき", "目を閉じる", "毛布", "自撮り", "あくび", "お尻"],
    "仕事": ["パソコン", "バイト", "洗濯", "荷物"],
    "電話": ["コールセンター", "電話", "スマホ"],
    "話す": ["口パク", "パクパク", "しゃべる", "マイク", "ヤギ", "羊", "子羊", "ニャー"],
    "お願い": ["お願い", "要求"],
    "嫌がる": ["嫌", "えずく", "きゅうり", "くしゃみ", "NO！", "動きたくない"],
    "にらむ": ["睨む", "にらま"],
    "喧嘩": ["喧嘩", "殴る", "はたく", "詰め寄る", "威圧"],
    "移動": ["ダッシュ", "歩く", "走る"],
    "乗り物": ["バイク", "運転", "ドライブ", "オランウータン", "オラウータン"],
    "冷静": ["冷静", "爪とぎ"],
    "もじもじ": ["もじもじ"],
}

# ミーム素材として使わないmp4:
#   終了画面   = アウトロとして別途連結する
#   きゅうり食う猫 = きゅうり自体が緑でキーイングすると半透明化する（2026-07-21報告 A-3）
#   壁チラ猫   = 白い壁パネルが矩形として残る（同 A/J）

# 「パソコンをたたく猫p」= 旧素材（背景がマゼンタでキーイング不能・2026-08-10差し替えで
# 廃止）。zip結合更新ではファイル削除が配布先に伝播しないため、名前で恒久除外する
# （後継は パソコンをたたく猫.mp4 / パソコンをたたく猫②.mp4 ＝この部分一致に掛からない）
_EXCLUDE_CLIPS = ("終了画面", "きゅうり食う猫", "壁チラ猫", "パソコンをたたく猫p")

# YMM4素材テンプレート(.ymmt)由来の公式配置: ファイル名(NFC/小文字) → (Zoom%, X, Y)。
# Zoomは元動画ピクセル基準、X/Yは1920x1080キャンバス中心からのオフセット。
# ※テンプレでCropEffect併用のクリップ(ドヤ顔猫アロハ/ノリノリ猫接写/ひじつき猫/
#   運転するオラウータン①等)はZoomだけ適用すると巨大化するため載せない=自動サイズに任せる。
NEKO_PLACEMENT = {
    "happyhappy猫.mp4": (169.3, -0.5, 29.5),
    "うるさいヤギ.mp4": (100.0, 516.5, 169.0),
    "えずく猫.mp4": (182.6, 382.0, 245.0),
    "コールセンター猫.mp4": (142.5, 0.0, 0.0),
    "スナックを食べる猫.mp4": (290.7, 0.0, 0.0),
    "はぁ？猫.mp4": (100.0, 121.0, 210.0),
    "マイク猫プロ.mp4": (55.0, -533.0, -14.0),
    "マイク猫小.mp4": (106.0, 518.0, 0.0),
    "運転するオランウータン.mp4": (100.0, -253.0, 167.0),
    "遠吠えのように叫ぶ猫.mp4": (185.0, 0.0, 0.0),
    "怒る猫.mp4": (100.0, -33.0, 0.0),
    "頭を抱える猫.mp4": (100.0, 407.0, 180.0),
    "悲しい濡れた猫.mp4": (100.0, 0.0, 180.0),
    "踊る猫（赤い服）.mp4": (192.8, 0.0, 0.0),
    "冷静な黒猫（爪とぎ）.mp4": (152.8, -524.0, 94.0),
    "しょんぼり猫②ai.mp4": (76.0, 0.0, 0.0),
    "しょんぼり猫　小声ai.mp4": (66.4, 0.0, 0.0),
}


def _nfc(s: str) -> str:
    import unicodedata
    return unicodedata.normalize("NFC", s).lower()


def build_clip_library(base: Path) -> dict[str, list[str]]:
    """基本フォルダ＋素材25A の全クリップをタグ別に分類して返す（絶対パス）。

    優先順: TAG_CLIPS（厳選・ファイル名一致）→ TAG_KEYWORDS（キーワード）→ 未分類は「話す」。
    """
    import unicodedata

    def _n(s: str) -> str:  # Mac製zipのNFDファイル名（濁点分離）対策
        return unicodedata.normalize("NFC", s).lower()

    lib: dict[str, list[str]] = {t: [] for t in TAG_CLIPS}
    files = sorted(base.glob("*.mp4")) + sorted((base / "素材25A").glob("*.mp4"))
    curated = {_n(n): t for t, names in TAG_CLIPS.items() for n in names}
    for f in files:
        name = _n(f.name)
        if any(_n(x) in name for x in _EXCLUDE_CLIPS):
            continue
        tag = curated.get(name)
        if tag is None:
            for t, kws in TAG_KEYWORDS.items():
                if any(_n(k) in name for k in kws):
                    tag = t
                    break
        lib.setdefault(tag or DEFAULT_TAG, []).append(str(f))
    return {t: v for t, v in lib.items() if v}

TXN_SOUND = "場面転換用鳩ポッポ.mp3"  # 場面転換SE（--- 行）

NEKO_SYSTEM = (
    "あなたは猫ミーム動画（猫のミーム映像＋テロップ会話で進むストーリー動画）の构成作家です。"
    "実体験風の話を、テロップだけで伝わる短い会話・モノローグで書きます。"
)

# 「乗り物」は室内シーンで車が出る誤爆の元なのでAIの選択肢に出さない（素材分類専用）
_TAGS_TEXT = "、".join(t for t in TAG_CLIPS.keys() if t != "乗り物")

NEKO_TEMPLATE = (
    "次のお題で猫ミーム動画の台本を作ってください。\n\n"
    "【お題】{topic}\n【追加指示】{instruction}\n【目安の尺】約{length}分（セリフ{lines}本前後）\n\n"
    "ルール:\n"
    "・1行=1セリフ。形式は「話者|感情タグ|セリフ」（半角パイプ区切り）。\n"
    "・話者名はそのまま画面の名前テロップになります。誰のことか一目で分かる呼び名にする。\n"
    "  - 主人公にも必ず日本語の名前を付ける（例: 美咲、さやか）。「主人公」という語は使わない。\n"
    "  - 他の登場人物は関係が分かる呼び名にする（例: 義父、ママ友、ママ友の夫、鑑定士、警察官）。\n"
    "  - 「相手夫」「相手妻」のような分かりにくい略語は使わない。8文字以内。\n"
    "  - 主人公のセリフを一番多く。\n"
    "・セリフ本文の前に、1行だけ「MAIN: 主人公の名前」を書く（主人公判定に使います）。\n"
    "・ナレーター（「ナレーション」という話者）は登場させない。いつ・どこで・何が起きているかの状況説明は、"
    "各場面の頭に**主人公の一人語り（独白）**として主人公のセリフで1〜2行入れる"
    "（例: 美咲|話す|転職初日、ドキドキしながら出社した）。初見でも話についていけるように丁寧に。\n"
    f"・感情タグは次から選ぶ: {_TAGS_TEXT}\n"
    "・感情タグは「表情・仕草」を表すものを選ぶ。セリフに移動や乗り物の話が出ても"
    "「移動」タグにはしない（室内なのに車の画が出てしまうため）。\n"
    "・セリフは20文字以内の話し言葉。テロップだけで伝わるように。\n"
    "・場面が変わるところには「---」だけの行を必ず入れる（動画全体で3〜8箇所。"
    "無いと不自然な無音の間が生まれます）。\n"
    "・「---」の直後の行に「@場所: 玄関」のように、その場面の場所を1行で書く"
    "（背景画像の生成に使います。部屋は1つだけ）。\n"
    "・お約束（理不尽→我慢→逆転→スカッと 等）の展開に。ただし急ぎすぎず、状況説明を挟みながら。\n\n"
    "出力形式（厳密に・他の文章は付けない）:\n"
    "TITLE: 動画タイトル（32文字以内）\n"
    "DESCRIPTION: 概要欄（2〜3行＋ハッシュタグ #猫ミーム 含む）\n"
    "TAGS: タグをカンマ区切りで5〜10個\n"
    "---\n"
    "MAIN: 美咲\n"
    "@場所: 会社のオフィス\n"
    "美咲|話す|転職初日、ドキドキしながら出社した\n"
    "美咲|ハッピー|今日から新しい職場、がんばるぞ!\n"
    "店長|にらむ|・・・（無言で睨んでくる）\n"
    "美咲|困る|えっ、初日から無視？\n"
    "（↑この形式でセリフを続ける）"
)


def build_neko_user_prompt(cfg: dict) -> str:
    # 尺→セリフ数: 1セリフ≒5秒（ゆったりテンポ）→ 1分あたり約12本
    lines = max(20, int(cfg.get("video_length_min", 3)) * 12)
    topic = (cfg.get("topic") or "").strip() or "（お題未設定）"
    mode = (cfg.get("script_source") or "topic").strip()
    if mode == "youtube":
        topic = "下の【元動画の文字起こし】を題材にした、新しいオリジナルストーリー"
    base = NEKO_TEMPLATE.format(
        topic=topic,
        instruction=(cfg.get("script_instruction") or "特になし").strip(),
        length=cfg.get("video_length_min", 3), lines=lines)
    src = (cfg.get("_source_context") or "").strip()
    return f"{src}\n\n{base}" if src else base


# ── 台本パース ────────────────────────────────────────────────────────
def parse_lines(script: str) -> list[dict]:
    """script.txt → [{speaker, tag, text}]（"---"は {scene_break:True}）。

    メタ行: 「MAIN: 名前」→ {main_name}（主人公判定）、
           「@場所: 玄関」→ 直前の場面区切りに place を刻む（背景生成に使用）。"""
    out: list[dict] = []
    for raw in (script or "").splitlines():
        s = raw.strip()
        if not s:
            continue
        if re.fullmatch(r"-{3,}", s):
            if out and not out[-1].get("scene_break"):
                out.append({"scene_break": True})
            continue
        m = re.match(r"^MAIN\s*[:：]\s*(\S.*)$", s, flags=re.IGNORECASE)
        if m:
            out.append({"main_name": m.group(1).strip()[:12]})
            continue
        m = re.match(r"^[@＠]\s*場所\s*[:：]\s*(\S.*)$", s)
        if m:
            place = m.group(1).strip()[:20]
            if out and out[-1].get("scene_break"):
                out[-1]["place"] = place
            else:
                out.append({"scene_place": place})  # 冒頭シーンの場所
            continue
        from .telop import UI_JUNK_LINES
        if s in UI_JUNK_LINES:   # チャットUIのボタン文字（ChatGPT Webの「編集」等）の単独行
            continue             # （猫ミームは独自パーサ＝clean_narration非経由でここで防ぐ）
        # AIが末尾に付けるハッシュタグ行（#猫ミーム #スカッと）やTITLE/DESCRIPTION/TAGSの見出し行は
        # セリフではない＝テロップに出さない・時間も取らない（2026-08-21配布先報告）
        if "|" not in s and (re.match(r"^[#＃]", s)   # 「# 見出し」「#」単独行も（パイプ無し行に限る）
                             or re.match(r"^(TITLE|DESCRIPTION|TAGS)\s*[:：]", s, flags=re.IGNORECASE)):
            continue
        parts = [p.strip() for p in s.split("|")]
        if len(parts) >= 3:
            speaker, tag, text = parts[0], parts[1], "|".join(parts[2:])
        elif len(parts) == 2:
            speaker, tag, text = parts[0], DEFAULT_TAG, parts[1]
        else:
            speaker, tag, text = "ナレーション", DEFAULT_TAG, s
        if not text:
            continue
        if tag not in TAG_CLIPS:
            tag = DEFAULT_TAG
        if speaker in ("ナレ", "ナレーター", "N", "n"):
            speaker = "ナレーション"
        out.append({"speaker": speaker or "ナレーション", "tag": tag, "text": text})
    while out and out[-1].get("scene_break"):
        out.pop()
    return out


def _ensure_scene_breaks(lines: list[dict], log=print) -> list[dict]:
    """台本に「---」場面区切りが1つも無い時の保険。

    区切りゼロだと全編が1シーン扱い＝背景が固定・場面転換SEも鳴らない
    （2026-07-16 配布先報告③）。ナレーション行（=場面の状況説明）を新しい
    場面の頭とみなして区切りを挿入し、ナレが無ければ一定本数ごとに区切る。"""
    if any(l.get("scene_break") for l in lines):
        return lines
    spoken = [l for l in lines if l.get("text")]
    if len(spoken) < 12:  # 短い動画は1シーンで十分
        return lines
    # メタ行(MAIN:/@場所)はカウントに入れない＝先頭に区切りが入って動画が
    # 「転換SE＋さかのぼるカット」から始まる事故を防ぐ（レビュー指摘）
    out: list[dict] = []
    since = 999
    content_seen = False
    for l in lines:
        if (l.get("speaker") == "ナレーション" and content_seen and since >= 5):
            out.append({"scene_break": True})
            since = 0
        out.append(l)
        if l.get("text"):
            content_seen = True
            since += 1
    if not any(l.get("scene_break") for l in out):  # ナレ行なし → 8本ごとに機械区切り
        out = []
        k = 0
        for l in lines:  # linesベースで列挙＝メタ行を捨てない（レビュー指摘）
            if l.get("text"):
                if k and k % 8 == 0:
                    out.append({"scene_break": True})
                k += 1
            out.append(l)
    n = sum(1 for l in out if l.get("scene_break"))
    if n:
        log(f"    台本に場面区切り(---)が無いため、自動で{n}箇所に挿入しました（背景の場面転換用）。")
    return out


def _narrator_to_main(lines: list[dict], cfg: dict, log=print) -> list[dict]:
    """「ナレーション」話者の行を主人公のセリフ（一人語り）にする（2026-08-21配布先要望）。

    猫ミームにナレーターは不要＝主人公の独白に入れる方針。MAIN: が無く主人公が分からない
    時は変えない。neko_keep_narrator=True で従来どおり（設定ファイルのみ・UI無し）。"""
    if cfg.get("neko_keep_narrator"):
        return lines
    main = next((l["main_name"] for l in lines if l.get("main_name")), "")
    if not main:
        return lines
    n = 0
    for l in lines:
        if l.get("text") and l.get("speaker") == "ナレーション":
            l["speaker"] = main
            n += 1
    if n:
        log(f"    ナレーション{n}行を主人公「{main}」の一人語りにしました（猫ミームにナレーターは置かない方針）。")
    return lines


def _bundled_dir() -> Path:
    """同梱の猫ミーム素材フォルダ（nekomeme_dir を自分の素材に変えた人の不足分の保険）。"""
    return Path(__file__).resolve().parent.parent / "_ref" / "nekomeme"


def _bundled_outro(base: Path) -> Path | None:
    """同梱の終了画面（自分の素材フォルダ優先→同梱フォルダ）。無ければ None。"""
    for cand in ([base / "終了画面250418.mp4"] + sorted(base.glob("終了画面*.mp4"))
                 + [_bundled_dir() / "終了画面250418.mp4"] + sorted(_bundled_dir().glob("終了画面*.mp4"))):
        if cand.exists():
            return cand
    return None


def _media_dur(p) -> float:
    try:
        pr = util.run([util.find_ffprobe(), "-v", "error", "-show_entries", "format=duration",
                       "-of", "csv=p=0", str(p)], capture_output=True, text=True,
                      encoding="utf-8", errors="replace", timeout=30)
        return float((pr.stdout or "0").strip() or 0)
    except Exception:
        return 0.0


def _channel_ed_for(cfg: dict) -> str:
    """チャンネルのED動画（冒頭・終了タブ）が使える時はそのパス、無ければ空。"""
    ed = (cfg.get("outro_video") or "").strip()
    return ed if ed and Path(ed).exists() else ""


def line_duration(text: str, cfg: dict) -> float:
    per = float(cfg.get("neko_sec_per_char", 0.26))
    mn = float(cfg.get("neko_min_sec", 4.0))
    return max(mn, round(len(text) * per + 0.8, 3))


# ── ②音声の代替: 無音WAV＋タイミング ─────────────────────────────────
def stage_tts_neko(proj, cfg, log) -> bool:
    """全行の長さをフレーム格子にスナップして連続配置する。

    行間の「隙間」は作らない（映像はセグメント連結＝隙間が存在しないため、
    タイミング側に隙間を足すと後半ほどテロップ/音がズレる）。
    """
    script = proj.path("script/script.txt").read_text(encoding="utf-8")
    lines = _narrator_to_main(_ensure_scene_breaks(parse_lines(script), log), cfg, log)
    spoken = [l for l in lines if l.get("text")]
    if not spoken:
        log("    セリフ行が作れませんでした（台本形式を確認）。")
        return False
    sr = int(cfg.get("tts_sample_rate", 24000))
    fps = int(cfg.get("video_fps", 30))
    break_sec = float(cfg.get("neko_break_sec", 0.6))  # 場面転換の間（旧1.0秒=長すぎた）

    def snap(sec: float) -> float:  # フレーム数の整数倍へ丸める
        return max(1, round(sec * fps)) / fps

    t = 0.0
    timings = []
    neko_lines = []
    for l in lines:
        if l.get("main_name") or l.get("scene_place"):
            neko_lines.append(dict(l))  # メタ情報（時間を持たない）
            continue
        d = snap(break_sec) if l.get("scene_break") else snap(line_duration(l["text"], cfg))
        item = {"start": round(t, 6), "end": round(t + d, 6)}
        if l.get("scene_break"):
            neko_lines.append({**l, **item})  # placeも引き継ぐ
        else:
            timings.append({"text": l["text"], **item})
            neko_lines.append({**l, **item})
        t += d
    total = t
    log(f"    猫ミーム: セリフ{len(spoken)}本 / 全長 約{total:.1f}秒（読み上げなし）")
    # sentinel(voice.wav)は必ず最後に書く（途中クラッシュで「tts完了・タイミング無し」を作らない）
    util.save_json_atomic(proj.path("audio/timings.json"), timings)
    util.save_json_atomic(proj.path("audio/neko_lines.json"), neko_lines)
    write_wav(b"\x00\x00" * int(total * sr), proj.path("audio/voice.wav"), sr)
    return True


# ── ③映像の代替: 感情タグ→クリップ割当て＋シーン背景 ────────────────────
def _scene_backgrounds(base: Path) -> dict[int, list[str]]:
    """シーンNカットM.png を {scene: [cut1, cut2, ...]} に整理。"""
    out: dict[int, list[tuple[int, str]]] = {}
    for p in base.glob("シーン*カット*.png"):
        m = re.match(r"シーン(\d+)カット(\d+)", p.stem)
        if m:
            out.setdefault(int(m.group(1)), []).append((int(m.group(2)), str(p)))
    return {k: [f for _, f in sorted(v)] for k, v in out.items()}


NEKO_BG_STYLE = (
    "Clean anime background art for a Japanese meme video, soft colors, simple and "
    "uncluttered. Absolutely NO people, NO animals, NO text, NO letters, NO writing "
    "of any kind — any signboards or screens must be completely blank. "
    "Pick exactly ONE room or place and paint only that single location; "
    "never merge two different rooms into one image. "
    "Infer only the LOCATION from the following story context and paint just that "
    "empty location (do not illustrate the events or words): ")


def _ai_scene_backgrounds(proj, cfg, lines, log, ctx=None) -> list[str]:
    """シーンごとの背景をAI画像で生成する。

    2026-07-16 配布先報告の修正: 旧実装は api/gemini_api 限定で、gemini_web 等の
    Web系エンジンでは黙って空を返し、ガイド文字入りプレースホルダー背景が
    最終動画に焼き込まれていた。Web系も（共有ブラウザ経由で）生成する。"""
    eng = (cfg.get("visual_engine") or "none")
    if eng in ("none", "", "mock"):
        return []
    # 場面ごとの説明: そのシーンのセリフ冒頭2〜3本をつなぐ
    scenes: list[list[str]] = [[]]
    places = [None]
    for l in lines:
        if l.get("scene_break"):
            scenes.append([])
            places.append(l.get("place"))
        elif l.get("scene_place"):
            places[-1] = places[-1] or l["scene_place"]  # 冒頭シーンの@場所
        elif l.get("main_name"):
            continue
        elif l.get("text") and len(scenes[-1]) < 3:
            scenes[-1].append(l["text"])
    if not any(scenes):
        return []
    # 空シーンも1枚生成して「シーン番号→背景」の1:1対応を守る
    # （空シーンを飛ばすと以降の全シーンの背景が1つずつズレる＝レビュー指摘）
    descs = []
    for s, pl in zip(scenes, places):
        loc = f"Location: {pl}. " if pl else ""  # 台本の「@場所」を最優先で使う
        body = " / ".join(s) if s else "a calm everyday Japanese interior"
        descs.append(NEKO_BG_STYLE + loc + body)
    from . import providers
    try:
        cfg2 = dict(cfg)
        cfg2["visual_prompt_template"] = " "  # 通常の世界観テンプレは使わない（背景専用）
        if ctx is None and providers.engine_needs_browser("visual", eng):
            opener = cfg.get("_ensure_browser")  # Web系は共有ブラウザへ遅延接続
            if callable(opener):
                ctx = opener()
            if ctx is None:
                log("    ⚠ シーン背景: ブラウザに接続できないためAI生成できません。")
                return []
        vprov = providers.get_provider("visual", cfg2, log, ctx)
        if getattr(vprov, "engine", "") == "mock":
            log("    ⚠ シーン背景: ③のエンジンが使えない（キー/ログイン無し）ため生成しません。")
            return []
        log(f"    シーン背景をAI生成中（{len(descs)}枚）…")
        res = vprov.generate(cfg2, {"scenes": descs}, str(proj.path("visuals")), log)
        return [p for p in res.get("images", []) if p and Path(p).exists()]
    except Exception as e:
        log(f"    シーン背景のAI生成は省略（{str(e)[:80]}）。")
        return []


def _fallback_backgrounds(proj, cfg, n_scenes: int) -> list[str]:
    """AI背景が作れない時のクリーンな無地グラデ背景（シーンごとに色違い）。

    同梱の「シーン*カット*.png」は差し替え前提のガイド枠で、巨大なグレー文字が
    そのまま最終動画に焼き込まれていた（2026-07-16 配布先報告・最重要）
    → ガイドPNGへはフォールバックせず、文字の無い無地背景を生成して使う。"""
    from PIL import Image
    w = int(cfg.get("video_width", 1920)); h = int(cfg.get("video_height", 1080))
    palettes = [((52, 72, 112), (14, 22, 42)), ((84, 56, 108), (26, 16, 42)),
                ((44, 92, 84), (12, 30, 32)), ((112, 86, 48), (40, 28, 14)),
                ((96, 48, 62), (32, 14, 22)), ((48, 82, 112), (14, 26, 42)),
                ((72, 72, 60), (24, 24, 18)), ((58, 96, 60), (18, 30, 20))]
    vis = proj.path("visuals"); vis.mkdir(parents=True, exist_ok=True)
    out: list[str] = []
    for i in range(max(1, n_scenes)):
        top, bottom = palettes[i % len(palettes)]
        grad = Image.new("RGB", (1, h))
        px = grad.load()
        for y in range(h):
            t = y / max(1, h - 1)
            px[0, y] = tuple(int(a + (b - a) * t) for a, b in zip(top, bottom))
        p = vis / f"bg_fallback_{i:02d}.png"
        grad.resize((w, h)).save(str(p), "PNG")
        out.append(str(p))
    return out


def stage_visuals_neko(proj, cfg, log, ctx=None) -> bool:
    lines = util.load_json(proj.path("audio/neko_lines.json"), []) or []
    if not lines:
        log("    【エラー】audio/neko_lines.json がありません/空です。"
            "audio/voice.wav を削除して②からやり直してください。")
        return False
    base = Path(cfg.get("nekomeme_dir") or "")
    if not base.exists():
        log(f"    【エラー】猫ミーム素材フォルダが見つかりません: {base}")
        return False
    clip_lib = build_clip_library(base)
    n_clips = sum(len(v) for v in clip_lib.values())
    if not clip_lib:
        log("    【エラー】猫ミームのクリップが見つかりません。素材フォルダを確認してください。")
        return False
    log(f"    素材ライブラリ: {n_clips}クリップ / {len(clip_lib)}タグ（素材25A含む全量）")
    ai_bgs = _ai_scene_backgrounds(proj, cfg, lines, log, ctx)
    if not ai_bgs and str(cfg.get("neko_bg_mode", "")).lower() != "bundled":
        # ガイド文字入りの同梱PNGを最終動画に使わない（使いたい場合のみ
        # settings.json に neko_bg_mode="bundled" を指定＝差し替え済みの人向け）
        n_scenes = 1 + sum(1 for l in lines if l.get("scene_break"))
        log("    ⚠ シーン背景のAI生成が使えないため、無地グラデ背景で代替します"
            "（③のエンジンとキー/ログインを設定するとAI背景になります）。")
        ai_bgs = _fallback_backgrounds(proj, cfg, n_scenes)
    bgs = _scene_backgrounds(base)
    bg_scenes = sorted(bgs.keys())
    rng = random.Random(str(proj.base))  # プロジェクトごとに再現可能な乱数

    # 主人公の判定: 「主人公」表記 > MAIN:メタ行 > 最初に登場した人物（2026-07-21報告F）
    speakers = [l.get("speaker") for l in lines if l.get("text")]
    if "主人公" in speakers:
        main_name = "主人公"
    else:
        main_name = next((l.get("main_name") for l in lines if l.get("main_name")), "") \
            or next((s for s in speakers if s and s != "ナレーション"), "主人公")
    log(f"    主人公: {main_name}")

    # 場面転換カット用の「さかのぼる」画像（無ければ次の場面の背景を流用）。
    # 同梱のものは「シーン２ 〇〇にさかのぼる 白黒にして使う」というガイド文字が
    # 焼き込まれた**差し替え前提のテンプレ**＝そのまま本番動画に出すと
    # 「テンプレ画像が1秒表示される」事故になる（2026-08-03配布先報告）。
    # → 同梱テンプレ（MD5一致）は使わない。ユーザーが本物の転換画像に
    #   差し替えていればMD5が変わるので、その時だけ従来どおり使う
    sakanoboru = next(iter(sorted(base.glob("*さかのぼる*.png"))), None)
    if sakanoboru is not None:
        try:
            import hashlib as _hl
            _PLACEHOLDER_MD5 = "5066d429725e80b9188d75fcdbfd3931"
            if _hl.md5(Path(sakanoboru).read_bytes()).hexdigest() == _PLACEHOLDER_MD5:
                sakanoboru = None   # ガイド画像は使わない→転換は次の場面の背景+SE
        except Exception:
            sakanoboru = None

    timeline = []
    missing = set()
    last_file = ""
    recent: list[str] = []  # 直近に使ったクリップ（使い回し防止・2026-07-21報告J）
    scene_i = 0   # 何場面目か（---で+1）
    cut_i = 0     # 場面内のセリフ番号（カット1→2→3をローテ）

    # YMM4本家は「1セリフ=1背景カット(約5秒)」で最後まで画が変わり続ける。
    # AI背景(1シーン1枚)や同梱カットが尽きた長い場面では、同じ絵の
    # ズーム/クロップ違い(bg_var 0/1/2)をカット代わりに使う。
    def _bg() -> tuple[str | None, int]:
        if ai_bgs:  # AI生成背景（1シーン=1枚→カットはズーム違いで作る）
            return ai_bgs[scene_i % len(ai_bgs)], cut_i % 3
        if not bg_scenes:
            return None, 0
        sc = bg_scenes[scene_i % len(bg_scenes)]
        cuts = bgs[sc]
        return cuts[cut_i % len(cuts)], (cut_i // len(cuts)) % 3

    for l in lines:
        if l.get("main_name") or l.get("scene_place"):
            continue  # メタ情報（時間を持たない）
        if l.get("scene_break"):
            scene_i += 1
            cut_i = 0
            if sakanoboru is not None:
                b, v = str(sakanoboru), 0  # 本家の「話は　さかのぼる…」転換カット
            else:
                b, v = _bg()
            timeline.append({"scene_break": True, "start": l["start"], "end": l["end"],
                             "bg": b, "bg_var": v})
            continue
        cands = clip_lib.get(l["tag"]) or []
        if not cands:
            missing.add(l["tag"])
            cands = clip_lib.get(DEFAULT_TAG) or next(iter(clip_lib.values()))
        # 2匹以上が写る素材は1キャラの担当にしない（頭数が合わなくなる）。
        # 候補が枯渇するタグ（にらむ等）は「怒り」→既定タグへ寄せる
        pool = [c for c in cands if not _is_multi(c)]
        if not pool:
            alt = (clip_lib.get("怒り") or []) + (clip_lib.get(DEFAULT_TAG) or [])
            pool = [c for c in alt if not _is_multi(c)] or cands
        pick = rng.choice([c for c in pool if c not in recent]
                          or [c for c in pool if c != last_file] or pool)
        last_file = pick
        recent.append(pick)
        del recent[:-6]  # 直近6本を避ける
        b, v = _bg()
        timeline.append({"path": pick, "start": l["start"], "end": l["end"],
                         "speaker": l["speaker"], "tag": l["tag"], "text": l["text"],
                         "bg": b, "bg_var": v, "scene": scene_i})
        cut_i += 1
    if missing:
        log(f"    ⚠ 素材が無いタグ（既定で代替）: {'、'.join(sorted(missing))}")

    # 会話の掛け合い（同一シーン内で隣のセリフが別キャラ）は本家の左右キャラ構成
    # =2匹同時表示にする。主人公は常に左、その他同士は先に登場した方が左。
    lines_idx = [k for k, x in enumerate(timeline) if not x.get("scene_break")]
    order: dict[int, dict[str, int]] = {}
    for k in lines_idx:
        sp = timeline[k].get("speaker")
        if sp and sp != "ナレーション":
            d = order.setdefault(timeline[k]["scene"], {})
            d.setdefault(sp, len(d))
    for pos, k in enumerate(lines_idx):
        it = timeline[k]
        sp = it.get("speaker")
        if not sp or sp == "ナレーション":
            continue

        def _nearest(rng_: range):
            for j in rng_:  # ナレ行は跨いで探すが、シーンを跨いだら打ち切り
                o = timeline[lines_idx[j]]
                if o.get("scene") != it["scene"]:
                    return None
                osp = o.get("speaker")
                if osp == sp:
                    return None  # 同キャラの連続=独白なので2匹にしない
                if osp and osp != "ナレーション":
                    return o
            return None

        partner = _nearest(range(pos - 1, -1, -1)) or _nearest(range(pos + 1, len(lines_idx)))
        if not partner:
            continue
        psp = partner["speaker"]
        if sp == main_name:
            side = "L"
        elif psp == main_name:
            side = "R"
        else:
            side = "L" if order[it["scene"]].get(sp, 0) <= order[it["scene"]].get(psp, 0) else "R"
        it["path2"] = partner["path"]
        it["speaker2"] = psp
        it["spk_side"] = side
    duo = sum(1 for x in timeline if x.get("path2"))
    if duo:
        log(f"    掛け合い2匹並び: {duo}セリフ（主人公=左）")

    # 本家どおり最後に「終了画面」(音声付きアウトロ)を連結。
    # 縦ショートは終了画面不要（2026-08-10配布先報告）なので付けない
    outro = None
    if int(cfg.get("video_height", 1080)) > int(cfg.get("video_width", 1920)):
        log("    縦ショートのため、終了画面は付けません。")
    elif _channel_ed_for(cfg):
        # 冒頭・終了タブのED動画が④の後に連結される＝同梱の終了画面も付けると2つ並ぶ
        # （2026-08-21配布先報告「猫ミームのみ終了画面が2つ入る」）
        log(f"    チャンネルのED動画（{Path(_channel_ed_for(cfg)).name}）を使うため、"
            "猫ミーム同梱の終了画面は付けません。")
    else:
        outro = _bundled_outro(base)
    if outro is not None and timeline:
        odur = _media_dur(outro)
        if odur > 0.5:
            t_end = timeline[-1]["end"]
            timeline.append({"outro": True, "path": str(outro),
                             "start": t_end, "end": round(t_end + odur, 6)})
            log(f"    アウトロ: {outro.name}（{odur:.1f}秒）")
    n = len([x for x in timeline if not x.get('scene_break') and not x.get('outro')])
    log(f"    クリップ割当て: {n}本 / 背景シーン: {len(bg_scenes)}種")
    util.save_json_atomic(proj.path("visuals/manifest.json"),
                          {"images": [], "clips": [], "neko_timeline": timeline,
                           "neko_main": main_name})  # ← sentinel
    return True


# ── ④合成の代替: タイムライン合成（クリップ×ASS×BGM×転換SE） ────────────
# YMM4元プロジェクト準拠のレイアウト（1920x1080キャンバス中心基準の実測値）:
#   主人公セリフ = 下段 (960-68, 540+217) 赤・メイリオ太字
#   その他セリフ = 上段 (960-68, 540-232) 白・メイリオ太字
#   名前プレート = 白背景ボックス＋黒字メイリオ太字（テンプレ「左キャラ名」「右キャラ名」の実測。
#                 左=主人公 (960-719, 540+340) / 右=相手 (960+280, 540+340)、LeftTop基準・常時表示）
#   ナレーション = 下段・黄（元プロジェクトに定義は無いが従来からの読みやすい配色を維持）
_NEKO_FONT = "メイリオ"


# 主人公以外の話者に登場順で割り当てる色（ASSは&HAABBGGRR）。
# セリフ文字色と名前プレートの箱色を揃えて「誰の発言か」を色でも判別できるようにする
_SPK_COLORS = ["&H00FFFFFF",  # 白
               "&H00FAC687",  # 水色
               "&H0090EE90",  # 薄緑
               "&H0050B4FF",  # オレンジ
               "&H00C896FF",  # ピンク
               "&H00FFA0C8",  # 薄紫
               "&H0078FFB4",  # 黄緑
               "&H0096DCFF"]  # ベージュ


def build_neko_ass(timeline: list[dict], cfg: dict, main_name: str = "主人公") -> str:
    w = int(cfg.get("video_width", 1920)); h = int(cfg.get("video_height", 1080))
    sx, sy = w / 1920.0, h / 1080.0
    size = int(cfg.get("telop_fontsize", 72) * 1.1 * sx)  # 縦動画/低解像度でも幅に収まるようsx比例
    wrap_limit = 18
    dy = 0
    if h > w:
        # 縦ショート: sx比例だけだと約44px相当で読めない（2026-08-10配布先報告
        # 「ショートだとより小さくなってしまう」）。全画面視聴前提でフォントを1.5倍へ
        # 引き上げ、折返しは「実際に幅へ収まる文字数」で行う＝はみ出しゼロを保証。
        # 倍率と縦位置は設定で調整可（2026-08-25配布先要望「文字位置とサイズを直したい」:
        # settings.json の neko_shorts_font_scale=倍率 / neko_shorts_text_y=画面の高さ比の
        # 上下ずらし。例 1.8 と -0.05＝文字を大きく・少し上へ）
        try:
            _scale = float(cfg.get("neko_shorts_font_scale", 1.5) or 1.5)
        except (TypeError, ValueError):
            _scale = 1.5
        size = int(size * max(0.5, min(3.0, _scale)))
        wrap_limit = max(6, int(w * 0.85) // max(size, 1))
        try:
            # ±0.15まで（それ以上ずらすと名前プレート・セリフが画面外に出る・敵対検証2026-08-25）
            dy = int(h * max(-0.15, min(0.15, float(cfg.get("neko_shorts_text_y", 0.0) or 0.0))))
        except (TypeError, ValueError):
            dy = 0
    name_size = int(size * 0.8)   # 旧0.6は小さすぎた（2026-07-21報告F-1）
    y_low = int((540 + 217) * sy) + dy   # 主人公・ナレ（下段）
    y_high = int((540 - 232) * sy) + dy  # その他（上段）
    x_center = int((960 - 68) * sx)
    x_left = int(w * 0.26)          # 2匹並びの左キャラ位置に合わせる（報告G-1）
    x_right = int(w * 0.74)
    plate_l = (int((960 - 719) * sx), int((540 + 340) * sy) + dy)  # 左キャラ名（主人公側）
    plate_r = (int((960 + 280) * sx), int((540 + 340) * sy) + dy)  # 右キャラ名（相手側）
    pad = max(6, int(12 * sx))      # 名前プレートの余白（旧8→12・報告F-1）

    def _t(sec: float) -> str:
        # 床関数にする: round()だとフレーム境界(k/30秒)の1/3で切り上がり、
        # テロップの切替がカットより1フレーム遅れる（1cs<1フレームなので床側の誤差は無害）
        cs = int(sec * 100)
        return f"{cs//360000}:{(cs//6000)%60:02d}:{(cs//100)%60:02d}.{cs%100:02d}"

    def _wrap(text: str, limit: int | None = None) -> str:
        # {}はASSオーバーライド構文と衝突するため全角へ退避
        text = text.replace("{", "｛").replace("}", "｝").replace("\n", "\\N")
        if "\\N" in text:
            return text
        if h > w:
            # 縦: フォントを引き上げた分、収まる文字数で均等にn行へ分割
            # （limit=2匹並び等で配置位置から算出した1行の上限。無指定は中央配置の上限）
            lim = limit or wrap_limit
            if len(text) > lim:
                n = -(-len(text) // lim)
                step = -(-len(text) // n)
                text = "\\N".join(text[i:i + step] for i in range(0, len(text), step))
        elif len(text) > 18:  # 横: 従来どおり中央で2行に（\pos指定は自動折返しが効かない）
            m = len(text) // 2
            text = text[:m] + "\\N" + text[m:]
        return text

    # 主人公・ナレ以外の話者に登場順で色を割当て
    sidx: dict[str, int] = {}
    for it in timeline:
        sp = it.get("speaker")
        for nm in (sp, it.get("speaker2")):
            if nm and nm != main_name and nm != "ナレーション" and nm not in sidx:
                sidx[nm] = len(sidx)

    def _spk_color(nm: str) -> str:
        return _SPK_COLORS[sidx.get(nm, 0) % len(_SPK_COLORS)]

    # セリフは半透明の座布団つき(BorderStyle=3)＝顔に被っても読める（報告G-2）
    _BOX = "&H78000000"

    def _dlg_style(name: str, color: str) -> str:
        return (f"Style: {name},{_NEKO_FONT},{size},{color},{color},{_BOX},&H96000000,"
                f"-1,0,0,0,100,100,0,0,3,{max(4, int(8 * sx))},0,5,0,0,0,1")

    def _plate_style(name: str, box: str) -> str:  # 黒字＋色付き箱
        return (f"Style: {name},{_NEKO_FONT},{name_size},&H00000000,&H00000000,{box},{box},"
                f"-1,0,0,0,100,100,0,0,3,{pad},0,7,0,0,0,1")

    styles = [
        _dlg_style("Main", "&H000000FF"),      # 主人公=赤
        _dlg_style("Naration", "&H0000FFFF"),  # ナレ=黄
        _plate_style("NameM", "&H00FFFFFF"),   # 主人公プレート=白箱（本家準拠）
        _plate_style("NameN", "&H0000FFFF"),   # ナレプレート=黄箱（一目で分かる・報告E）
    ]
    for nm, i in sidx.items():
        styles.append(_dlg_style(f"Other{i % len(_SPK_COLORS)}", _spk_color(nm)))
        styles.append(_plate_style(f"NameC{i % len(_SPK_COLORS)}", _spk_color(nm)))
    # 同色インデックスの重複定義を除去（話者9人以上で色が一周した場合）
    seen_style = set()
    styles = [s for s in styles
              if not (s.split(",")[0] in seen_style or seen_style.add(s.split(",")[0]))]

    def _plate_name(nm: str) -> str:
        if nm == main_name:
            return "NameM"
        if nm == "ナレーション":
            return "NameN"
        return f"NameC{sidx.get(nm, 0) % len(_SPK_COLORS)}"

    ev = []
    for it in timeline:
        if it.get("scene_break") or it.get("outro") or not it.get("text"):
            continue
        sp = it.get("speaker") or "ナレーション"
        s, e = _t(it["start"]), _t(it["end"])
        if sp == main_name:
            style, y = "Main", y_low
        elif sp == "ナレーション":
            style, y = "Naration", y_low
        else:
            style, y = f"Other{sidx.get(sp, 0) % len(_SPK_COLORS)}", y_high
        # 文字のX位置は話者の立ち位置に寄せる（2匹並び時。1匹・ナレは中央）
        if it.get("path2"):
            side = it.get("spk_side") or ("L" if sp == main_name else "R")
            x = x_left if side == "L" else x_right
        else:
            x = x_center
        # 縦の2匹並びは左右に寄るぶん使える幅が半分以下になるため、
        # その配置位置で実際に収まる文字数で折り返す（中央前提のwrap_limitだと端が切れる）
        limit = None
        if h > w and it.get("path2"):
            limit = max(4, int(2 * min(x, w - x) * 0.9) // max(size, 1))
        text = _wrap(it["text"], limit)
        ev.append(f"Dialogue: 0,{s},{e},{style},,0,0,0,,"
                  f"{{\\pos({x},{y})}}{text}")
        # 名前プレート（本家: 左=主人公 / 右=相手。ナレーションも左に表示＝報告E）
        plates = []
        if sp == "ナレーション":
            plates.append(("ナレーション", "L"))
        elif it.get("path2"):
            side = it.get("spk_side") or ("L" if sp == main_name else "R")
            plates.append((sp, side))
            if it.get("speaker2"):
                plates.append((it["speaker2"], "R" if side == "L" else "L"))
        else:
            plates.append((sp, "L" if sp == main_name else "R"))
        for nm, sd in plates:
            px_, py_ = plate_l if sd == "L" else plate_r
            ev.append(f"Dialogue: 1,{s},{e},{_plate_name(nm)},,0,0,0,,"
                      f"{{\\an7\\pos({px_},{py_})}}{_wrap(nm)}")
    return (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {w}\nPlayResY: {h}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        + "\n".join(styles) + "\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        + "\n".join(ev) + "\n")


def pick_bgm(cfg: dict, log=print) -> str | None:
    base = Path(cfg.get("nekomeme_dir") or "")
    cands = sorted(base.glob("*.mp3"))
    cands = [c for c in cands if "エンド" not in c.name and "場面転換" not in c.name
             and "エンディング" not in c.name]
    if not cands:
        return None
    pick = random.choice(cands)
    log(f"    BGM: {pick.stem}")
    return str(pick)


def _has_audio(path: str) -> bool:
    try:
        pr = util.run([util.find_ffprobe(), "-v", "error", "-select_streams", "a",
                       "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
                      capture_output=True, text=True, encoding="utf-8", errors="replace",
                      timeout=30)
        return "audio" in (pr.stdout or "")
    except Exception:
        return False


# ── クロマキー（2026-07-21 配布先報告A で全面刷新） ─────────────────────
# 旧実装はキー色を 0x00FF00（純緑）に決め打ち＋強despill(mix=0.7)で、
#   ・暗い緑/淡い緑の素材（素材25AのAI生成分等）が抜けきらず矩形が残る
#   ・白い動物から緑成分が引かれてピンク/薄紫に変色する（全編）
# → クリップごとに背景のキー色を縁部から実測し、colorkey(RGB距離)で抜く。
#   despillは「純緑の背景」の時だけ、ごく弱く掛ける。


def _greenish(p) -> bool:
    r, g, b = p
    return g > 60 and g >= r * 1.15 and g >= b * 1.15


def _detect_key_color(path: str, ffmpeg: str, cache: dict):
    """グリーンバックのキー色を実測する（緑背景と判断できなければNone）。

    1段目=外周リングの緑画素、2段目=フレーム全体の緑画素（黒帯/ビネット付き素材対応。
    レビューで実測: ひじつき猫等は縁が黒でリング判定だけだと見逃す）。
    「支配色バケツが緑か」ではなく「緑画素の中の支配色」を取る＝縁の黒に負けない。"""
    key = ("keycol", path)
    if key in cache:
        return cache[key]
    col = None
    try:
        import io
        from PIL import Image
        pr = util.run([ffmpeg, "-hide_banner", "-ss", "0.3", "-i", path,
                       "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                      capture_output=True, timeout=60)
        im = Image.open(io.BytesIO(pr.stdout)).convert("RGB")
        im.thumbnail((120, 120))
        px = im.load()
        W, H = im.size
        m = max(2, int(min(W, H) * 0.12))

        def _dominant_green(pts):
            greens = [p for p in pts if _greenish(p)]
            if not pts or len(greens) / len(pts) < 0.18:
                return None
            buckets: dict = {}
            for p in greens:
                b = (p[0] >> 5, p[1] >> 5, p[2] >> 5)
                cnt, sr_, sg_, sb_ = buckets.get(b, (0, 0, 0, 0))
                buckets[b] = (cnt + 1, sr_ + p[0], sg_ + p[1], sb_ + p[2])
            cnt, sr_, sg_, sb_ = max(buckets.values(), key=lambda v: v[0])
            return (sr_ // cnt, sg_ // cnt, sb_ // cnt)

        ring = [px[x, y] for y in range(H) for x in range(W)
                if x < m or y < m or x >= W - m or y >= H - m]
        col = _dominant_green(ring)
        if col is None:  # 黒帯/ビネットで縁が緑でない素材 → 全面で再判定
            allp = [px[x, y] for y in range(H) for x in range(W)]
            col = _dominant_green(allp)
    except Exception:
        col = None
    cache[key] = col
    return col


def _key_filter(path: str, ffmpeg: str, cache: dict, log=print) -> str:
    """クリップごとのクロマキー用フィルタを返す。

    2026-07-23レビューで再設計:
    - chromakey(YUV色差)を実測キー色で使う。RGB距離のcolorkeyは暗いキー色だと
      黒毛・瞳まで許容半径に入り透過する（実素材31本で実証）ため不採用。
    - similarityは「キー色の彩度の半分」を上限にする＝無彩色(黒/白/灰)はキー色との
      色差が必ず彩度ぶん離れているので、構造的にキャラの黒が抜けない。
    - 実測に失敗したら旧来の純緑キーにフェイルセーフ（キーイング無しにはしない）。"""
    key = ("keyf", path)
    if key in cache:
        return cache[key]
    col = _detect_key_color(path, ffmpeg, cache)
    if col is None:
        # 検出できない=キーイングを外すと緑矩形が焼き込まれる方が実害大 → 純緑で維持
        filt = "chromakey=0x00FF00:0.26:0.10"
        log(f"      キー色を実測できず既定の純緑キーで続行: {Path(path).name}")
    else:
        r, g, b = col
        hexc = "0x%02X%02X%02X" % col
        # キー色の彩度（YUV色差平面での無彩色からの距離。chromakeyのsimilarityと同じ
        # 255√2 正規化）。similarity+blend をその半分以下に抑える＝黒/白/灰（無彩色）は
        # キー色から必ず彩度ぶん離れているので、構造的にキャラの黒毛が抜けない
        cb = 128 - 0.169 * r - 0.331 * g + 0.5 * b
        cr = 128 + 0.5 * r - 0.419 * g - 0.081 * b
        chroma = ((cb - 128) ** 2 + (cr - 128) ** 2) ** 0.5 / (255.0 * 2 ** 0.5)
        sim = max(0.04, min(0.26, chroma * 0.5))
        blend = min(0.08, sim * 0.5)
        filt = f"chromakey={hexc}:{sim:.3f}:{blend:.3f}"
        if g > 200 and r < 60 and b < 60:
            # 純緑背景の時だけ、縁のスピル除去をごく弱く（白い動物のピンク化を防ぐ）
            filt += ",despill=type=green:mix=0.25:expand=0.15"
        elif hexc != "0x00FF00":
            log(f"      キー色を実測: {hexc}（{Path(path).name}）")
    cache[key] = filt
    return filt


# 素材の顔の向き: "L"=画面左を向いている / "R"=右向き / 未登録・"F"=正面（反転しない）
# 掛け合いで会話相手に背を向けないよう、置く側と逆を向いていたら hflip で反転する
# （2026-07-21 配布先報告B。素材に文字は写っていないため反転しても違和感なし。
#   小道具の向きが意味を持つ素材=パソコン猫等は登録しない=F扱い）
# 2026-07-23 実フレーム目視で分類（確信のあるものだけ登録。曖昧な素材は未登録=F）
NEKO_FACING: dict[str, str] = {
    "目を見開いて驚く猫.mp4": "L",
    "怒る猫.mp4": "R",
    "冷静な黒猫（爪とぎ）.mp4": "L",
    "左を向く羊.mp4": "L",
    "文句を言う猫.mp4": "L",
    "二度見する猫.mp4": "L",
    "はぁ？猫.mp4": "L",
    "ひじつき猫.mp4": "L",
    "うなずく犬.mp4": "R",
    "頭を抱える猫.mp4": "L",
    "口パクパク猫.mp4": "L",
    "両手でお願い猫.mp4": "R",
    "見上げて要求する猫.mp4": "R",
    "嫌いな顔をする猫.mp4": "L",
    "外国美人猫.mp4": "L",
    "遠吠えのように叫ぶ猫.mp4": "L",
    "くしゃみする猫②.mp4": "R",
}


def _face_flip(path: str, place_side: str) -> str:
    """置く側(place_side)に対して顔が外を向くクリップは左右反転する。"""
    face = NEKO_FACING.get(_nfc(Path(path).name), "F")
    if face not in ("L", "R"):
        return ""
    want = "R" if place_side == "L" else "L"  # 左に置くなら右向き（内側=会話相手）がほしい
    return ",hflip" if face != want else ""


def _cat_height(path: str, ffmpeg: str, w: int, h: int, cache: dict) -> int:
    """クリップの「緑以外＝キャラ実体」の占有率を1フレーム解析し、
    実体の見た目が画面高の約60%になる表示高さを返す（幅85%上限つき）。"""
    if path in cache:
        return cache[path]
    ch = int(h * 0.66)  # 解析不能時の既定
    try:
        import io
        from PIL import Image
        pr = util.run([ffmpeg, "-hide_banner", "-ss", "0.3", "-i", path,
                       "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
                      capture_output=True, timeout=60)
        im = Image.open(io.BytesIO(pr.stdout)).convert("RGB")
        src_w, src_h = im.size
        im.thumbnail((160, 160))
        px = im.load()
        xs, ys = [], []
        for y in range(im.height):
            for x in range(im.width):
                r, g, b = px[x, y]
                if not (g > 90 and g > r * 1.5 and g > b * 1.5):  # 緑背景以外
                    xs.append(x); ys.append(y)
        if len(ys) > 20:
            ratio_h = max(0.15, (max(ys) - min(ys) + 1) / im.height)
            ratio_w = max(0.15, (max(xs) - min(xs) + 1) / im.width)
            ch = int(h * 0.60 / ratio_h)
            # 幅のはみ出しを抑える（横長クリップ・2匹モノ対策）
            disp_w = src_w * ch / src_h * ratio_w
            if disp_w > w * 0.85:
                ch = int(ch * w * 0.85 / disp_w)
            ch = max(int(h * 0.35), min(int(h * 0.92), ch))
    except Exception:
        pass
    ch -= ch % 2  # 偶数に（エンコーダ制約）
    cache[path] = ch
    return ch


def _src_size(path: str, cache: dict) -> tuple[int, int] | None:
    key = ("sz", path)
    if key in cache:
        return cache[key]
    sz = None
    try:
        pr = util.run([util.find_ffprobe(), "-v", "error", "-select_streams", "v:0",
                       "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
                      capture_output=True, text=True, encoding="utf-8", errors="replace",
                      timeout=30)
        a, b = (pr.stdout or "").strip().splitlines()[0].split(",")[:2]
        sz = (int(a), int(b))
    except Exception:
        pass
    cache[key] = sz
    return sz


def _cat_layout(path: str, ffmpeg: str, w: int, h: int, cache: dict) -> tuple[str, str, str]:
    """クリップの scaleフィルタ / overlay座標(ox,oy) を返す。

    YMM4素材テンプレの公式配置(NEKO_PLACEMENT)があればそれを再現し、
    無ければ実体bboxからの自動サイズ（画面高60%・中央）にする。"""
    plc = NEKO_PLACEMENT.get(_nfc(Path(path).name))
    if plc:
        src = _src_size(path, cache)
        if src and src[0] > 0 and src[1] > 0:
            z, px, py = plc
            sx, sy = w / 1920.0, h / 1080.0
            f = z / 100.0 * sx
            over = max(src[0] * f / (w * 0.98), src[1] * f / (h * 0.95))
            if over > 1.0:  # はみ出しすぎは等比で抑える
                f /= over
            ox = f"(main_w-overlay_w)/2+{px * sx:.0f}"
            oy = f"(main_h-overlay_h)/2+{py * sy:.0f}"
            return (f"scale=trunc(iw*{f:.4f}/2)*2:trunc(ih*{f:.4f}/2)*2", ox, oy)
    ch = _cat_height(path, ffmpeg, w, h, cache)
    # 1匹表示はやや上へ（-30→-90）: 下段テロップが顔に被るのを避ける（2026-07-21報告G-2）
    return (f"scale=-2:{ch}", "(main_w-overlay_w)/2", "(main_h-overlay_h)/2-90")


def _duo_height(path: str, ffmpeg: str, w: int, h: int, cache: dict) -> int:
    """2匹並び用の表示高さ: 通常の自動サイズをやや縮め、幅が画面の半分に収まるよう抑える。"""
    ch = int(_cat_height(path, ffmpeg, w, h, cache) * 0.82)
    src = _src_size(path, cache)
    if src and src[1]:
        disp_w = src[0] * ch / src[1]
        if disp_w > w * 0.46:
            ch = int(ch * w * 0.46 / disp_w)
    ch = max(int(h * 0.26), min(int(h * 0.62), ch))
    return ch - ch % 2


def compose_neko(proj, cfg, log) -> dict:
    manifest = util.load_json(proj.path("visuals/manifest.json"), {}) or {}
    timeline = manifest.get("neko_timeline") or []
    if not timeline:
        return {"ok": False, "error": "neko_timeline がありません"}
    w = int(cfg.get("video_width", 1920)); h = int(cfg.get("video_height", 1080))
    fps = int(cfg.get("video_fps", 30)); q = int(cfg.get("enc_quality", 20))
    ffmpeg = util.find_ffmpeg(); enc = util.pick_encoder(ffmpeg)
    compose_dir = proj.path("compose"); compose_dir.mkdir(parents=True, exist_ok=True)

    # 縦ショート: 終了画面は不要（2026-08-10）。③のガードはmanifestに焼き込まれるため、
    # 修正前に作った縦プロジェクトの「④だけ作り直し」/♻再開でも効くよう合成側でも除外する
    # （start/endは直後の正規化で引き直され、鳴き声・BGMもtimeline駆動なので除外だけで整合）
    if h > w and any(it.get("outro") for it in timeline):
        timeline = [it for it in timeline if not it.get("outro")]
        log("    縦ショートのため、終了画面は付けません（既存タイムラインから除外）。")
        if not timeline:
            return {"ok": False, "error": "neko_timeline が終了画面のみです"}
    # チャンネルのED動画が続く時も同梱の終了画面は外す（修正前に作った動画の「④だけ作り直し」でも
    # 二重にならないように合成側でも判定・2026-08-21）
    ch_ed = _channel_ed_for(cfg) if h <= w else ""
    if ch_ed and any(it.get("outro") for it in timeline):
        timeline = [it for it in timeline if not it.get("outro")]
        log(f"    チャンネルのED動画（{Path(ch_ed).name}）を使うため、猫ミーム同梱の終了画面は外しました。")
        if not timeline:
            return {"ok": False, "error": "neko_timeline が終了画面のみです"}
    elif h <= w and not ch_ed and not any(it.get("outro") for it in timeline):
        # ③の時点ではチャンネルEDがあって同梱を外したが、④の時点でEDが消えた/移動した
        # （または③時に終了画面ファイルが無かった）→ 終了画面ゼロにせず同梱を戻す
        _o = _bundled_outro(Path(cfg.get("nekomeme_dir") or ""))
        _od = _media_dur(_o) if _o else 0.0
        if _o and _od > 0.5 and timeline:
            _t = timeline[-1]["end"]
            timeline.append({"outro": True, "path": str(_o), "start": _t, "end": round(_t + _od, 6)})
            log(f"    チャンネルのED動画が無いため、猫ミーム同梱の終了画面を付けます（{_o.name}）。")

    # 0) タイムライン正規化: 各区間をフレーム整数個にし、位置を「区間長の累積」で
    #    引き直す（旧タイムラインの隙間や丸め誤差を除去＝ズレの根本対策）。
    pos = 0.0
    frames = []
    for it in timeline:
        n = max(1, round((it["end"] - it["start"]) * fps))
        frames.append(n)
        it["start"] = round(pos, 6)
        pos = round(pos + n / fps, 6)
        it["end"] = pos
    total = pos

    # 1) セリフごとに「背景×クロマキー猫」の映像のみセグメントを作る
    #    （音声はセグメントに入れない: セグメント毎のAACパディングが積もって
    #    後半で音がズレるため、鳴き声は後段でサンプル単位に連結する）
    seg_dir = compose_dir / "_segments"; seg_dir.mkdir(exist_ok=True)
    for old in seg_dir.glob("seg_*.mp4"):  # 前回の残骸が exists() 判定をすり抜けないよう掃除
        try:
            old.unlink()
        except OSError:
            pass
    seg_paths = []
    scale_cache: dict = {}
    log(f"    背景×クロマキー猫を {len(timeline)}区間で合成中…（キャラ大きさ自動調整）")
    # 背景の擬似カット（同じ絵でもズーム/寄りを変えて画変わりさせる=本家のカット割り再現）
    _BG_VAR = {1: (0.72, 0.04, 0.20), 2: (0.72, 0.24, 0.04)}
    for i, it in enumerate(timeline):
        n = frames[i]
        seg = seg_dir / f"seg_{i:03d}.mp4"
        if it.get("outro"):  # 終了画面: そのまま全画面で
            fc = (f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                  f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}[v]")
            cmd = [ffmpeg, "-y", "-hide_banner", "-i", it["path"],
                   "-filter_complex", fc, "-map", "[v]", "-frames:v", str(n), "-an",
                   *util.enc_args(enc, q), "-pix_fmt", "yuv420p", str(seg)]
            pr = util.run(cmd, capture_output=True, timeout=600)
            if pr.returncode != 0 or not seg.exists():
                tail = (pr.stderr or b"").decode("utf-8", "replace")[-600:]
                log(f"    【エラー】アウトロの合成に失敗: {it['path']}")
                return {"ok": False, "error": "アウトロ合成失敗", "log_tail": tail}
            seg_paths.append(seg)
            continue
        bg = it.get("bg")
        # -framerate必須: 無いとimage2既定25fpsが主入力になり、overlayが猫を25Hzで
        # サンプリング→後段fps=30の複製でジャダーになる（レビューで実測確認済み）
        bg_in = (["-framerate", str(fps), "-loop", "1", "-i", bg] if bg
                 else ["-f", "lavfi", "-i", f"color=c=0x1c1c2e:s={w}x{h}:r={fps}"])
        bgf = (f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
               f"crop={w}:{h},setsar=1")
        var = _BG_VAR.get(int(it.get("bg_var") or 0))
        if bg and var:
            z, cx, cy = var
            bgf += f",crop=iw*{z}:ih*{z}:iw*{cx}:ih*{cy},scale={w}:{h},setsar=1"
        bgf += "[bg]"
        if it.get("scene_break"):  # 場面転換=背景のみ
            cmd = [ffmpeg, "-y", "-hide_banner", *bg_in,
                   "-filter_complex", f"{bgf};[bg]fps={fps}[v]",
                   "-map", "[v]", "-frames:v", str(n), "-an",
                   *util.enc_args(enc, q), "-pix_fmt", "yuv420p", str(seg)]
        elif it.get("path2"):  # 会話の掛け合い=2匹並び（話者側を手前に重ねる）
            ch_s = _duo_height(it["path"], ffmpeg, w, h, scale_cache)
            ch_p = _duo_height(it["path2"], ffmpeg, w, h, scale_cache)
            side_s = it.get("spk_side", "L")
            cx_s = 0.26 if side_s == "L" else 0.74
            cx_p = 1.0 - cx_s
            kf_s = _key_filter(it["path"], ffmpeg, scale_cache, log)
            kf_p = _key_filter(it["path2"], ffmpeg, scale_cache, log)
            flip_s = _face_flip(it["path"], side_s)   # 会話相手の方を向かせる（報告B）
            flip_p = _face_flip(it["path2"], "R" if side_s == "L" else "L")
            fc = (f"{bgf};"
                  f"[1:v]{kf_s}{flip_s},scale=-2:{ch_s}[catS];"
                  f"[2:v]{kf_p}{flip_p},scale=-2:{ch_p}[catP];"
                  f"[bg][catP]overlay=main_w*{cx_p:.2f}-overlay_w/2:(main_h-overlay_h)/2-30[tw];"
                  f"[tw][catS]overlay=main_w*{cx_s:.2f}-overlay_w/2:(main_h-overlay_h)/2-30,"
                  f"fps={fps}[v]")
            cmd = [ffmpeg, "-y", "-hide_banner", *bg_in,
                   "-stream_loop", "-1", "-i", it["path"],
                   "-stream_loop", "-1", "-i", it["path2"],
                   "-filter_complex", fc, "-map", "[v]", "-frames:v", str(n), "-an",
                   *util.enc_args(enc, q), "-pix_fmt", "yuv420p", str(seg)]
        else:
            scalef, ox, oy = _cat_layout(it["path"], ffmpeg, w, h, scale_cache)
            kf = _key_filter(it["path"], ffmpeg, scale_cache, log)
            fc = (f"{bgf};"
                  f"[1:v]{kf},{scalef}[cat];"
                  f"[bg][cat]overlay={ox}:{oy},fps={fps}[v]")
            cmd = [ffmpeg, "-y", "-hide_banner", *bg_in,
                   "-stream_loop", "-1", "-i", it["path"],
                   "-filter_complex", fc, "-map", "[v]", "-frames:v", str(n), "-an",
                   *util.enc_args(enc, q), "-pix_fmt", "yuv420p", str(seg)]
        pr = util.run(cmd, capture_output=True, timeout=600)
        # exists()だけだと途中失敗の短いファイルが成功扱いになり音ズレが再発するため returncode も見る
        if pr.returncode == 0 and seg.exists():
            seg_paths.append(seg)
        else:
            tail = (pr.stderr or b"").decode("utf-8", "replace")[-600:]
            log(f"    【エラー】区間{i}の合成に失敗: {it.get('path', '(bg)')}")
            log(f"    ffmpeg: …{tail[-300:]}")
            return {"ok": False, "error": f"クリップ切出し失敗: {it.get('path','(bg)')}",
                    "log_tail": tail}
    listf = seg_dir / "list.txt"
    listf.write_text("".join(f"file '{p.name}'\n" for p in seg_paths), encoding="utf-8")
    base_mp4 = compose_dir / "base.mp4"
    pr = util.run([ffmpeg, "-y", "-hide_banner", "-f", "concat", "-safe", "0",
                   "-i", "list.txt", "-c", "copy", str(base_mp4.resolve())],
                  capture_output=True, cwd=str(seg_dir), timeout=600)
    if pr.returncode != 0 or not base_mp4.exists():
        return {"ok": False, "error": "基礎動画の連結に失敗",
                "log_tail": (pr.stderr or b"").decode("utf-8", "replace")[-600:]}

    # 2) 鳴き声トラック: 各区間の音をPCMで切り出し、サンプル数ぴったりで連結
    #    （映像フレーム数と完全一致＝累積ズレゼロ）
    log("    鳴き声トラックを生成中…")
    sr_a = 48000
    # 動物クリップの音量: 旧volume=0.9(ほぼ原音)はBGMより約23dB大きかった（報告I）
    meme_gain = 10 ** (float(cfg.get("neko_meme_db", -14.0)) / 20.0)
    voice = bytearray()
    for i, it in enumerate(timeline):
        n_bytes = int(round(frames[i] / fps * sr_a)) * 4  # 16bit×2ch
        chunk = b""
        if not it.get("scene_break") and _has_audio(it["path"]):
            # 終了画面(アウトロ)はBGMフェード後の主役音声＝動物クリップの減衰を適用しない
            gain = 0.9 if it.get("outro") else meme_gain
            pr = util.run([ffmpeg, "-hide_banner", "-stream_loop", "-1", "-i", it["path"],
                           "-vn", "-af", f"aresample=48000,volume={gain:.4f}",
                           "-t", f"{frames[i] / fps:.6f}",
                           "-f", "s16le", "-ac", "2", "-ar", str(sr_a), "-"],
                          capture_output=True, timeout=120)
            chunk = pr.stdout or b""
        voice += chunk[:n_bytes].ljust(n_bytes, b"\x00")

    # エンディング直前0.4秒をフェードアウト（前クリップの音の余韻がEDに残る感じを消す・報告L）
    # チャンネルのED動画が後ろに連結される時（同梱アウトロ無し）は本編の末尾をフェード
    outro_i = next((k for k, it in enumerate(timeline) if it.get("outro")), None)
    if outro_i is None and ch_ed:
        outro_i = len(timeline)
    if outro_i is not None and outro_i > 0:
        off = sum(int(round(frames[k] / fps * sr_a)) * 4 for k in range(outro_i))
        fade_bytes = min(off, int(0.4 * sr_a) * 4)
        start = off - fade_bytes
        n_s = fade_bytes // 4
        for si in range(n_s):
            g = 1.0 - (si + 1) / n_s
            for chn in (0, 2):
                o = start + si * 4 + chn
                v = int.from_bytes(voice[o:o + 2], "little", signed=True)
                voice[o:o + 2] = int(v * g).to_bytes(2, "little", signed=True)

    import wave
    meme_wav = compose_dir / "memevoice.wav"
    with wave.open(str(meme_wav), "wb") as wf:
        wf.setnchannels(2); wf.setsampwidth(2); wf.setframerate(sr_a)
        wf.writeframes(bytes(voice))

    # 3) ASS（話者色分け＋名前ラベル。正規化後のタイムラインで生成）
    ass_path = compose_dir / "subs.ass"
    ass_path.write_text(
        build_neko_ass(timeline, cfg, manifest.get("neko_main", "主人公")), encoding="utf-8")

    # 4) 鳴き声＋BGM＋転換SEを合わせて最終合成
    bgm = pick_bgm(cfg, log)
    se = Path(cfg.get("nekomeme_dir") or "") / TXN_SOUND
    if not se.exists() and (_bundled_dir() / TXN_SOUND).exists():
        se = _bundled_dir() / TXN_SOUND   # 自分の素材フォルダに無ければ同梱のSEを使う（2026-08-21報告「音源が無い」）
    breaks = [it["start"] for it in timeline if it.get("scene_break")]
    if breaks and not se.exists():
        log(f"    ⚠ 場面転換SE（{TXN_SOUND}）が素材フォルダに無いため、転換の音は鳴りません: {se}")
    inputs = ["-y", "-hide_banner", "-i", str(base_mp4.resolve()),
              "-i", str(meme_wav.resolve())]
    filters = ["[1:a]anull[sega]"]
    amix_in = ["[sega]"]
    idx = 2
    if bgm:
        inputs += ["-stream_loop", "-1", "-i", bgm]
        # BGMは混ぜる前に平坦化（2026-07-30・通常合成と同じ。曲の盛り上がりで
        # 音量が揺れない）。-10dBは平坦化後の基準合わせ＝従来の平均音量を維持
        db = float(cfg.get("neko_bgm_db", -14.0)) - 10.0
        bgmf = f"[{idx}:a]aresample=48000,{_compose.BGM_FLATTEN},volume={db:.1f}dB"
        outro_start = next((it["start"] for it in timeline if it.get("outro")), None)
        if outro_start is None and ch_ed and timeline:
            outro_start = timeline[-1]["end"]   # 同梱アウトロ無し＝本編の末尾でフェード（EDへ音を残さない）
        if outro_start and outro_start > 2.0:  # 本編終わりでBGMをフェードアウト
            bgmf += f",afade=t=out:st={outro_start - 1.0:.3f}:d=1.0"
        filters.append(bgmf + "[bgm]")
        amix_in.append("[bgm]")
        idx += 1
    if se.exists() and breaks:
        inputs += ["-i", str(se)]
        # 転換SEは無ゲインだと動物より大きく鳴っていた（報告I）→ 専用ゲインを適用
        se_gain = 10 ** (float(cfg.get("neko_se_db", -18.0)) / 20.0)
        for bi, bstart in enumerate(breaks):
            filters.append(
                f"[{idx}:a]aresample=48000,volume={se_gain:.4f},"
                f"adelay={int(bstart*1000)}|{int(bstart*1000)}[se{bi}]")
        amix_in += [f"[se{bi}]" for bi in range(len(breaks))]
        idx += 1
    out_mp4 = compose_dir / "final.mp4"
    # 通常合成と同じく、sentinel(final.mp4)へは直接書かず成功時にだけ昇格させる
    out_tmp = compose_dir / "final_wip.mp4"
    from .compose import ass_vf
    vchain = f"[0:v]{ass_vf('subs.ass')}[v]"   # fontsdir=同梱フォント直指定（歯抜け対策）
    if len(amix_in) > 1:
        achain = ";".join(filters) + ";" + "".join(amix_in) + \
            f"amix=inputs={len(amix_in)}:normalize=0:dropout_transition=0[a]"
        fc = vchain + ";" + achain
        maps = ["-map", "[v]", "-map", "[a]"]
    else:
        fc = vchain
        maps = ["-map", "[v]", "-map", "1:a"]
    cmd = [ffmpeg, *inputs, "-filter_complex", fc, *maps,
           *util.enc_args(enc, q), "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-t", f"{total:.3f}",
           "-movflags", "+faststart", str(out_tmp.resolve())]
    logf = compose_dir / "ffmpeg.log"
    with open(logf, "wb") as fl:
        rc = util.run_watched(cmd, timeout_sec=max(900, total * 6),
                              stdout=fl, stderr=fl, cwd=str(compose_dir))
    res = util.probe_ok(out_tmp)
    res["returncode"] = rc; res["log"] = str(logf)
    if res.get("ok"):
        import os as _os
        _os.replace(out_tmp, out_mp4)
        # 中間生成物（切り出しセグメント・基礎動画・ミックス済み音声）を掃除する。
        # 残すと1本あたり数百MBが積み上がる（失敗時は原因調査用に残す）
        try:
            import shutil as _sh
            _sh.rmtree(compose_dir / "_segments", ignore_errors=True)
            (compose_dir / "base.mp4").unlink(missing_ok=True)
            (compose_dir / "memevoice.wav").unlink(missing_ok=True)
        except Exception:
            pass
        try:
            log(f"    完成: {out_mp4}  ({float(res.get('duration') or 0):.1f}秒)")
        except (TypeError, ValueError):
            log(f"    完成: {out_mp4}")
    else:
        try:
            out_tmp.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            res["log_tail"] = logf.read_text(encoding="utf-8", errors="replace")[-1500:]
        except Exception:
            pass
    return res
