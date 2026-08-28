"""プロバイダの抽象基底・レジストリ・共通ヘルパ。"""
from __future__ import annotations

import re
import struct
from abc import ABC, abstractmethod
from pathlib import Path

# (capability, engine) -> provider class
REGISTRY: dict[tuple[str, str], type] = {}


def register(capability: str, engine: str):
    def deco(cls):
        REGISTRY[(capability, engine)] = cls
        cls.capability = capability
        cls.engine = engine
        return cls
    return deco


class Provider(ABC):
    capability: str = ""
    engine: str = ""

    def __init__(self, ctx=None):
        self.ctx = ctx  # 共有 Playwright context（Webプロバイダ用。API/mockはNone）

    @classmethod
    def available(cls, cfg: dict) -> tuple[bool, str]:
        """このエンジンが今すぐ使えるか（キー/認証の有無）。(可否, 理由)。"""
        return True, ""

    @classmethod
    def needs_browser(cls) -> bool:
        return False


class ScriptProvider(Provider):
    @abstractmethod
    def generate(self, cfg: dict, log=print) -> dict:
        """戻り値: {"script": str, "title": str, "description": str, "tags": list[str]}"""

    def brushup(self, script: str, cfg: dict, log=print) -> str:
        """既定はそのまま返す（ブラッシュアップ未対応プロバイダ用）。"""
        return script


class TTSProvider(Provider):
    @abstractmethod
    def synthesize(self, cues: list[str], cfg: dict, out_wav: str, log=print) -> list[dict]:
        """cues（テロップ行）を行単位で合成し out_wav を書く。戻り値: [{text,start,end}]。
        各行の音声長＝表示時間（align_method="segments"）。"""

    def synthesize_full(self, text: str, cfg: dict, out_wav: str, log=print) -> float | None:
        """全文を一括合成して out_wav を書き、長さ(秒)を返す。timepointは取らない。
        align_method="whisper"（timepoint非対応の声）用。未対応なら None。"""
        return None


class VisualProvider(Provider):
    @abstractmethod
    def generate(self, cfg: dict, meta: dict, out_dir: str, log=print) -> dict:
        """戻り値: {"images": [path,...], "clips": [path,...]}"""


class VideoProvider(Provider):
    @abstractmethod
    def generate(self, cfg: dict, meta: dict, out_dir: str, log=print) -> dict:
        """戻り値: {"clips": [path,...]}（動画クリップ）"""


class ThumbnailProvider(Provider):
    @abstractmethod
    def generate(self, cfg: dict, meta: dict, out_png: str, log=print) -> bool:
        ...


class UploadProvider(Provider):
    @abstractmethod
    def schedule_upload(self, video: str, meta: dict, thumb: str | None,
                        cfg: dict, log=print) -> dict:
        ...


# "browser" という汎用指定が来たときの capability 別デフォルト・ベンダー
BROWSER_DEFAULT = {
    "script": "claude_web",
    "visual": "chatgpt_web",
    "thumb": "chatgpt_web",
    "video": "flow_web",
    "upload": "studio_web",
}


def get_provider(capability: str, cfg: dict, log=print, ctx=None):
    """capability の実装インスタンスを返す。使えないエンジンは mock にフォールバック。

    ctx: 共有 Playwright context（Webプロバイダが使う）。
    """
    engine = (cfg.get(f"{capability}_engine") or "api").strip()
    if engine == "browser":
        engine = BROWSER_DEFAULT.get(capability, "mock")
    cls = REGISTRY.get((capability, engine))
    if cls is None:
        log(f"    [{capability}] エンジン '{engine}' は未実装 → mock で代替します。")
        cls = REGISTRY.get((capability, "mock"))
        return cls(ctx=ctx) if cls else None
    ok, reason = cls.available(cfg)
    if not ok and engine != "mock":
        log(f"    [{capability}] '{engine}' が使えません（{reason}）→ mock で代替します。")
        mock_cls = REGISTRY.get((capability, "mock"))
        if mock_cls is not None:
            return mock_cls(ctx=ctx)
    return cls(ctx=ctx)


def engine_needs_browser(cap: str, engine: str) -> bool:
    """capability×engine が共有Chromeを要するか（"browser"エイリアスも解決）。"""
    engine = (engine or "").strip()
    if engine == "browser":
        engine = BROWSER_DEFAULT.get(cap, "")
    cls = REGISTRY.get((cap, engine))
    return bool(cls is not None and cls.needs_browser())


def any_engine_needs_browser(cfg: dict) -> bool:
    """**主エンジン**のいずれかが Web 自動操作（共有Chrome）を要するか。

    起動時に共有Chromeを先に開くかの判定に使う。フォールバック連鎖のWeb版は
    「実際にそこへ落ちた時」に pipeline 側で遅延接続する（APIだけで完走する時に
    Chromeを無駄に開かない＝毎回ウィンドウが出る回帰を避けるため）。"""
    for cap in ("script", "brushup", "visual", "video", "thumb", "upload"):
        if engine_needs_browser(cap, cfg.get(f"{cap}_engine") or ""):
            return True
    return False


# ── 共通: 漢字→カタカナ読み（TTS読み間違い対策） ─────────────────────────
_KKS = None
_READ_FIXES = None
_READ_FIXES_MTIME = None

# 変換前に置き換える頻出トラップ（pykakasi自体の誤読対策）。
# 追加は data/reading_fixes_user.txt に「語=よみ」で1行ずつ。
_READ_FIXES_BUILTIN = {
    "今日は": "きょうは",   # こんにちは と誤読される
    "今日も": "きょうも",
    "明日": "あした",
    "昨日": "きのう",
    "一人": "ひとり",
    "二人": "ふたり",
    "大人気": "だいにんき",
    "何で": "なんで",
    # ── 金融・経済の専門用語（一般的な音読みで読まれて意味が変わるもの）──
    # 2026-07-25追加: 実測で「終値」がFishに『しゅわんりょう』相当で読まれた
    # （whisper転写が「手腕量」）。「〜値」は業界読みの ね が正しい語族。
    "終値": "おわりね",
    "始値": "はじめね",
    "高値": "たかね",
    "安値": "やすね",
    "半値": "はんね",
    "呼値": "よびね",
    "指値": "さしね",
    "成行": "なりゆき",
    "出来高": "できだか",
    "建玉": "たてぎょく",
    "約定": "やくじょう",
    "空売り": "からうり",
    "逆日歩": "ぎゃくひぶ",
    "利鞘": "りざや",
    "元本": "がんぽん",
    "貸方": "かしかた",
    "借方": "かりかた",
    "手形": "てがた",
    "為替": "かわせ",
    "目論見書": "もくろみしょ",
    "定款": "ていかん",
    "収賄": "しゅうわい",
    "贈賄": "ぞうわい",
    "背任": "はいにん",
    "地上げ": "じあげ",
    "住専": "じゅうせん",
    "仕手": "して",
    "含み益": "ふくみえき",
    "含み損": "ふくみそん",
    # ── 文脈依存語のうち、前後込みキーで安全に固定できるもの（2026-08-04配布先報告）──
    # 「額」単体は がく/ひたい の両読みがあるため登録しない。額装の文脈だけ固定する
    "額に入れ": "がくにいれ",
    "額に飾": "がくにかざ",
}

# 文脈依存語のうち「位置」で読みが決まるもの（2026-08-04配布先報告）。
# 単語だけの辞書登録では誤爆する（君=きみ は 田中君 を壊す）ため、正規表現で文脈を固定する。
# 適用は辞書置換の後（自分の辞書やAIマップが前後込みキーで直した箇所には触れない）
_CONTEXT_FIXES = [
    # 文頭・文末句読点直後の感動詞「は？」→ 助詞と誤解釈され wa と読まれる。
    # 「彼は？」「『大丈夫』は？」の は は助詞なので、直前が文字や閉じ括弧なら触らない
    # ※短い行はテロップ結合で前の文に続くため、行頭だけでなく「。の直後」too（実測）
    (re.compile(r"(?:(?<=[。！!？?])|^)は(?=[？?])", re.MULTILINE), "はっ"),
    # 文頭・文末直後の単独「入れ。」＝入る(はいる)の命令形（入れる の命令形は「入れろ」、
    # 「カバンに入れ。」は直前が助詞＝どちらも触らない）
    (re.compile(r"(?:(?<=[。！!？?])|^)入れ(?=[。！!？?]|$)", re.MULTILINE), "はいれ"),
    # 代名詞の「君」: 語の途中でなく、直後に助詞が続く時だけ きみ（田中君の/たかし君は は不変）
    (re.compile(r"(?<![一-龥ぁ-んァ-ヶA-Za-zＡ-Ｚａ-ｚ0-9０-９])君(?=[のがはをにへとも])"), "きみ"),
]


# 短くて「別語の一部になりやすい」語は、直後にこの文字が続く時だけ置換を見送る。
# （例: 一昨日→"一きのう"、一人前→"ひとり前"、明日香村→"あした香村" の防止）
_NO_BREAK = {
    "明日": "香",          # 明日香村
    "昨日": "",            # 一昨日は「一昨日」側で個別対応
    "一人": "前称",        # 一人前・一人称
    "二人": "称",
    "大人気": "な",        # 大人気ない
    "何で": "も",          # 何でも
}
# 前に文字が付くと別語になるもの（前方ガード）
_NO_PREFIX = {"昨日": "一", "作日": "一"}


def _user_fixes() -> dict:
    """自分で登録した辞書だけを返す（AIの自動マップより優先させるため）。

    _load_read_fixes と同じ文字コード自動判別を使う。ここだけUTF-8直読みだと、
    メモ帳ANSI(cp932)保存の辞書が「同梱辞書には勝つのにAIマップには負ける」という
    文字コード次第で優先順位が変わる不整合になる。"""
    try:
        return parse_user_dict(read_user_dict_text(_data_dir() / "reading_fixes_user.txt"))
    except Exception:
        return {}


def _data_dir():
    from pathlib import Path as _P
    return _P(__file__).resolve().parent.parent.parent / "data"


def read_user_dict_text(path) -> str:
    """ユーザー辞書を読む。文字コードを自動判別する。

    メモ帳の「ANSI」保存（日本語WindowsではShift_JIS/cp932）だと UTF-8 として
    読めず、以前は例外を握りつぶして**登録した語が全部消えたように見えていた**。
    さらにGUIで開くと空リストになり、保存すると空ファイルで上書きされて実際に失われた。"""
    from pathlib import Path as _P
    p = _P(path)
    if not p.exists():
        return ""
    raw = p.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp932", "euc_jp"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")   # 最後の手段（読める行だけ拾う）


def parse_user_dict(text: str) -> dict:
    """「語=よみ」形式のテキストを辞書にする（#はコメント）。"""
    out = {}
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if "=" in ln and not ln.startswith("#"):
            k, v = ln.split("=", 1)
            if k.strip() and v.strip():
                out[k.strip()] = v.strip()
    return out


def _load_read_fixes() -> dict:
    """辞書を読む（ファイルの更新時刻が変わったら読み直す）。

    優先順（後のものが勝つ）:
      組込 < data/reading_dict.json（全ジャンル辞書・同梱） < data/reading_fixes_user.txt（自分用）
    ※さらに上位に「動画ごとのAI読み仮名マップ」が reading_for_tts で被さる。

    2026-07-24修正: 旧実装はプロセス常駐キャッシュで、GUIの「次の合成から反映」の
    案内に反して**アプリを再起動するまで辞書の追加が効かなかった**。"""
    global _READ_FIXES, _READ_FIXES_MTIME
    d = _data_dir()
    shipped, user = d / "reading_dict.json", d / "reading_fixes_user.txt"
    stamp = []
    for f in (shipped, user):
        try:
            stamp.append(f.stat().st_mtime)
        except Exception:
            stamp.append(0.0)
    key = tuple(stamp)
    if _READ_FIXES is not None and key == _READ_FIXES_MTIME:
        return _READ_FIXES
    fixes = dict(_READ_FIXES_BUILTIN)
    try:  # 同梱の全ジャンル辞書
        import json as _json
        for k, v in (_json.loads(shipped.read_text(encoding="utf-8")) or {}).items():
            if k and v:
                fixes[str(k).strip()] = str(v).strip()
    except Exception:
        pass
    try:  # ユーザー辞書（最優先・GUIから編集）
        fixes.update(parse_user_dict(read_user_dict_text(user)))
    except Exception:
        pass
    _READ_FIXES, _READ_FIXES_MTIME = fixes, key
    return fixes


def _int_kana(n: int) -> str:
    """整数 → 日本語の数詞（読み）。1990 → せんきゅうひゃくきゅうじゅう。"""
    if n == 0:
        return "ゼロ"
    ones = ["", "いち", "に", "さん", "よん", "ご", "ろく", "なな", "はち", "きゅう"]
    sen_x = {1: "せん", 3: "さんぜん", 8: "はっせん"}
    hyaku_x = {1: "ひゃく", 3: "さんびゃく", 6: "ろっぴゃく", 8: "はっぴゃく"}

    def under4(x: int) -> str:
        s = ""
        q, x = divmod(x, 1000)
        if q:
            s += sen_x.get(q, ones[q] + "せん")
        q, x = divmod(x, 100)
        if q:
            s += hyaku_x.get(q, ones[q] + "ひゃく")
        q, x = divmod(x, 10)
        if q:
            s += "じゅう" if q == 1 else ones[q] + "じゅう"
        return s + ones[x]

    out = ""
    for unit, name in ((10 ** 12, "ちょう"), (10 ** 8, "おく"), (10 ** 4, "まん")):
        q, n = divmod(n, unit)
        if q:
            # 1兆/1億は「いっちょう」「いちおく」、1万は「いちまん」
            head = under4(q)
            if q == 1 and name == "ちょう":
                head = "いっ"
            out += head + name
    if n:
        out += under4(n)
    return out


# 西暦: 桁区切りコンマを含む数字の「途中」にマッチしないよう、数字列を丸ごと捉える。
# （旧実装は「1,000年」の "000年" に食いつき『ゼロねん』と読ませていた）
_YEAR_RE = re.compile(r"(?<![0-9０-９,，.．])([0-9０-９][0-9０-９,，]*)\s*年")


def _year_kana(m: "re.Match") -> str:
    """「1990年」→「せんきゅうひゃくきゅうじゅうねん」。

    2026-07-25追加: Fish等のTTSは西暦を1桁ずつ「いちきゅうきゅうぜろねん」と
    読むことがある（ユーザー報告）。年だけは読み下して渡す（テロップは原文のまま）。"""
    z = str.maketrans("０１２３４５６７８９", "0123456789")
    digits = m.group(1).translate(z).replace(",", "").replace("，", "")
    if not digits.isdigit():
        return m.group(0)
    y = int(digits)
    # 「3年」「12年」等の短い年数はTTSが正しく読むので触らない。
    # 3桁以上（西暦・○万年前など）だけ読み下す。
    if not (100 <= y <= 99999999):
        return m.group(0)
    s = _int_kana(y)
    if s.endswith("よん"):      # 1994年 = …きゅうじゅうよねん
        s = s[:-2] + "よ"
    elif s.endswith("しち"):
        s = s[:-2] + "なな"
    return s + "ねん"


def reading_for_tts(text: str, cfg: dict) -> str:
    """TTSへ渡す文の「読み間違いやすい単語だけ」を読み仮名に置き換える（テロップは原文のまま）。

    2026-07-07 教訓: **全文カタカナ変換は禁止**。助詞の「は」がカタカナ「ハ」になると
    TTSは文脈を失い「ワ」でなく「ハ」と読む・句読点の扱いも壊れる（実走で発生）。
    置換の優先順: この動画専用マップ(cfg["_reading_map"]＝台本からAIが自動抽出)
    ＞ data/reading_fixes_user.txt ＞ 組込辞書。長い語から置換（部分一致の誤爆防止）。
    全文カナ変換は tts_kana_reading=True を明示した時だけ（既定False・非推奨）。

    2026-07-24 実測で再確認（Edge音声＋whisper転写でA/B）:
      「彼女は市場で三人の大人に会いました。」
        原文     → 「彼女は市場で3人の大人に会いました」（正しい）
        全文カナ → 「彼女**橋上**で…」＝ カノジョ**ハシジョウ**デ と読まれ、助詞「は」が
                    次の語とつながって“橋上”という別語になった（読み間違いが増える）
      「一日で千円を…」は全文カナだと pykakasi が「ツイタチ」と誤変換して誤読を固定する
      （原文なら TTS が文脈から「イチニチ」と正しく読めていた）。
    さらに各社の仕様も「かなだけでは日本語の発音は決まらない」ことを示す:
      Google Cloud TTS の yomigana はアクセント記号(^ !)を併記する仕様、
      Amazon Polly は通常かな(yomigana)と発音カナ(pron-kana)を別物として分離、
      AWS公式は「助詞『は』は発音仮名では『ワ』」と明記。
    → 読み間違い対策の本線は「誤読する語だけ辞書/マップで直す」であり、全文カナ化ではない。
    """
    try:
        fixes = dict(_load_read_fixes())
        user = _user_fixes()          # 自分の辞書は動画別マップより強い
        amap = dict(cfg.get("_reading_map") or {})
        if user:
            # 前後込みキー（例: 額に入れて／組込の 額に入れ）がユーザー登録語（例: 額）を
            # 含むと、「長いキーから置換」のせいでユーザーの読みが実質無視される
            # （2026-08-06報告「辞書に登録しても反映されない」・実再現）。
            # ユーザー語を内包する他ソースのキーは**組込/同梱/AIマップ問わず**捨てる
            # ＝その文字列を含む区間はユーザーの読みが正
            def _keep(d: dict) -> dict:
                return {k: v for k, v in d.items()
                        if k in user or not any(uk and uk in k for uk in user)}
            fixes = _keep(fixes)
            amap = _keep(amap)
        fixes.update(amap)
        fixes.update(user)
        for k in sorted(fixes, key=len, reverse=True):
            if not k or k not in text:
                continue
            guard, pre = _NO_BREAK.get(k), _NO_PREFIX.get(k)
            if guard or pre:
                # 「一人」→「一人前」のように、前後に続くと別語になるものは置換しない
                pat = (f"(?<![{pre}])" if pre else "") + re.escape(k) \
                      + (f"(?![{guard}])" if guard else "")
                text = re.sub(pat, fixes[k], text)
            else:
                text = text.replace(k, fixes[k])
        for pat, rep in _CONTEXT_FIXES:   # 位置で読みが決まる語（文頭の は？/入れ、代名詞の君）
            text = pat.sub(rep, text)
    except Exception:
        pass
    if cfg.get("tts_year_kana", True):  # 西暦の1桁読み（いちきゅうきゅうぜろねん）対策
        try:
            text = _YEAR_RE.sub(_year_kana, text)
        except Exception:
            pass
    # 記号の無害化（TTSのみ・テロップは原文のまま）:
    # ｜/| はTTSが「パイプ」と読み上げる（Markdown表の混入等・2026-08-03配布先報告）。
    # 表自体は台本整形(clean_narration)で除去するが、作成済み台本の♻再開や
    # タイトル区切りの単発｜のためにここでも読点へ落とす
    if "|" in text or "｜" in text:
        try:
            text = re.sub(r"\s*[|｜]+\s*", "、", text)
            text = re.sub(r"、{2,}", "、", text).strip("、 ")
        except Exception:
            pass
    if not cfg.get("tts_kana_reading", False):  # 既定=辞書置換＋西暦読み下し（安全）
        return text
    global _KKS
    try:
        if _KKS is None:
            import pykakasi
            _KKS = pykakasi.kakasi()
        out = "".join((i.get("kana") or i.get("orig") or "") for i in _KKS.convert(text))
        return out or text
    except Exception:
        return text


def dict_catalog() -> list[tuple[str, str, str]]:
    """登録済み辞書の一覧 [(語, よみ, 出どころ)]（GUIの「一覧を見る」用）。

    組込辞書＋同梱の全ジャンル辞書(data/reading_dict.json)。ユーザー辞書は含まない
    （そちらは編集画面の一覧で見える）。同じ語は同梱が組込を上書き＝実際の適用と同じ。"""
    merged: dict[str, tuple[str, str]] = {}
    for k, v in _READ_FIXES_BUILTIN.items():
        merged[k] = (v, "組込")
    try:
        import json as _json
        d = _json.loads((_data_dir() / "reading_dict.json").read_text(encoding="utf-8"))
        for k, v in (d or {}).items():
            if k and v:
                merged[str(k).strip()] = (str(v).strip(), "同梱")
    except Exception:
        pass
    rows = sorted((k, v, s) for k, (v, s) in merged.items())
    # 文脈規則（正規表現）も一覧に出す（読み＝適用条件つきで表示）
    rows += [("は？（文頭のみ）", "はっ？", "文脈規則"),
             ("入れ。（文頭のみ）", "はいれ。", "文脈規則"),
             ("君＋助詞（田中君などの名前は不変）", "きみ", "文脈規則")]
    return rows


# ── 共通: 実際に送った画像プロンプトの記録 ─────────────────────────────
def dump_prompts(out_dir, prompts) -> None:
    """visuals/prompts.json に「実際に送ったプロンプト」を保存する。

    「画像プロンプトが反映されない」系のトラブル時に、何が送られたかを
    後から検証できるようにする（失敗しても本処理は止めない）。"""
    try:
        from .. import util
        util.save_json_atomic(Path(out_dir) / "prompts.json", list(prompts))
    except Exception:
        pass


# ── 共通: 16bit mono PCM → WAV ────────────────────────────────────────
def write_wav(pcm: bytes, path, sample_rate: int = 24000) -> None:
    """WAVを一時ファイル経由で書き、完成した時だけ本番パスへ置き換える。

    voice.wav は②のsentinel。直接書くと、ディスク不足/クラッシュで途中まで
    書かれたファイルが「②完了」と誤認され、♻再開が壊れた音声のまま合成へ進む。"""
    import os as _os
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    n = len(pcm)
    tmp = path.with_name(path.stem + "_part" + path.suffix)
    try:
        with open(tmp, "wb") as f:
            f.write(b"RIFF" + struct.pack("<I", 36 + n) + b"WAVE")
            f.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, sample_rate,
                                          sample_rate * 2, 2, 16))
            f.write(b"data" + struct.pack("<I", n) + pcm)
        _os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def strip_wav_header(audio: bytes) -> bytes:
    """WAV（RIFF）なら44バイトのヘッダを除去して生PCMを返す。"""
    if audio[:4] == b"RIFF" and audio[8:12] == b"WAVE":
        # data チャンクを正しく探す
        i = 12
        while i + 8 <= len(audio):
            cid = audio[i:i + 4]
            size = struct.unpack("<I", audio[i + 4:i + 8])[0]
            if cid == b"data":
                return audio[i + 8:i + 8 + size]
            i += 8 + size + (size & 1)
        return audio[44:]
    return audio
