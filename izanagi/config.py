"""設定の読み書き。settings.json を扱う（★Bダッシュ config.py 準拠）。

APIキー等はローカルの settings.json に保存（クラウドに上げないこと）。
姉妹ツール（★Bダッシュ / バズ採集）に保存済みのキーがあれば初回は自動で取り込む。
"""
from __future__ import annotations

import json
import os
import shutil
import threading
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = ROOT / "settings.json"
BAK_PATH = ROOT / "settings.json.bak"
BACKUP_DIR = ROOT / "backups"
# 配布に同梱する初期設定（settings.jsonの「種」）。初回起動でsettings.jsonが無い時だけ
# 読まれる。settings.json自体は配布zipに入れない＝既存の人がフルzipを上書きしても
# 自分のAPIキー・世界観設定が消えない（2026-08-03・配布一本化）
SEED_PATH = ROOT / "初期設定.json"
BACKUP_KEEP = 5

# 姉妹ツールの settings.json（APIキー流用元）
_SIBLING_SETTINGS = [
    ROOT.parent.parent / "記事投稿ツール" / "★Bダッシュ" / "settings.json",
    ROOT.parent / "バズ採集" / "settings.json",
]

# 既定値 ───────────────────────────────────────────────────────────────
DEFAULTS = {
    "schema_version": 1,

    # ── 入力（①台本のお題） ──
    "topic": "",                       # 動画のテーマ／お題（youtube=動画URL / research=キーワード）
    "script_source": "topic",          # 作り方: topic=お題から / youtube=文字起こしリライト / research=リサーチ
    "video_mode": "normal",            # 動画タイプ: normal=通常 / neko=猫ミーム（読み上げなし・素材クリップ）
    "script_format": "narration",      # 台本形式: narration=一人語り / dialog=会話（A・B・ナレの掛け合い）
    "video_orientation": "landscape",  # 画面: landscape=横16:9 / portrait=縦9:16（ショート）
    # 会話形式の声（話者A=メインの声。B/ナレはエンジン別に指定）
    "dialog_voices": {
        "b": {"api": "ja-JP-Neural2-D", "gemini": "Puck", "edge": "ja-JP-KeitaNeural"},
        "n": {"api": "ja-JP-Chirp3-HD-Charon", "gemini": "Charon",
              "edge": "en-US-AndrewMultilingualNeural"},
    },
    # 会話形式のテロップ色（ASS BGR）: A=白 / B=水色 / ナレ=黄
    "dialog_color_a": "&H00FFFFFF",
    "dialog_color_b": "&H00FFD37E",
    "dialog_color_n": "&H0000FFFF",
    # 主人公の性別に合わせた声・画像の自動調整（2026-08-04配布先要望）。
    # 台本完成後にAIが主人公の性別を1回だけ判定し（script/meta.jsonに保存）、
    # 声の切替と画像プロンプトの人物指定に使う。auto_enabled=False なら一切何もしない（従来動作）
    "voice_by_gender": {
        "auto_enabled": False,
        "female": {"api": "ja-JP-Chirp3-HD-Aoede", "gemini": "Kore",
                   "edge": "ja-JP-NanamiNeural",
                   "fish": ""},   # Fishは自分の声ID登録時のみ切替（空=いつもの声）
        "male": {"api": "ja-JP-Chirp3-HD-Charon", "gemini": "Charon",
                 "edge": "ja-JP-KeitaNeural",
                 "fish": ""},
    },
    "script_instruction": "",          # 台本生成への追加指示（任意）
    # 概要欄の目次（チャプター）。既定OFF: 従来は必ず挿入され「見出しが文の断片・
    # 時刻が最大68秒ズレ」だった（2026-08-05配布先報告）。ONの時は音声の実時刻＋AI見出し
    "desc_chapters": False,
    "video_length_min": 5,             # 目安の動画尺（分）。台本ボリュームの目安

    # ── エンジン選択（"api"|"browser"|"mock"|vendor） ──
    "script_engine": "api",            # ①台本（api=Claude / claude_web / chatgpt_web / gemini_web / mock）
    "brushup_engine": "api",           # ①ブラッシュアップ
    "tts_engine": "api",               # ②音声（api=GoogleCloudTTS / mock）
    "visual_engine": "api",            # ③画像（api=OpenAI / chatgpt_web / gemini_web / mock）
    "video_engine": "none",            # ③動画（none / flow_web / sora_web / gemini_web / mock）
    "thumb_engine": "api",             # ⑤サムネ（api=OpenAI / chatgpt_web / mock）
    "upload_engine": "none",           # ⑥投稿（none / api=YouTube Data API v3 / studio_web）安全のため既定none

    # ── 編集可能プロンプト（""＝コード内の既定を使用） ──
    # ※実体は channels[active_channel] に保存され、読み込み時にトップレベルへ展開される
    "script_prompt": "",               # 台本生成のシステムプロンプト
    "topic_prompt": "",                # お題の自動生成への指示（例:恋愛系多め・実話ベース）
    "brushup_prompt": "",              # ブラッシュアップのプロンプト
    "gemini_tts_style": "",            # 話し方の指示（Gemini TTSのみ）
    "visual_prompt_template": "",      # 画像生成の世界観テンプレ
    # 冒頭・終了（チャンネルごと。空=無し）2026-07-21
    "intro_video": "",                 # 完成動画の先頭に連結するOP動画（mp4等）
    "outro_video": "",                 # 完成動画の末尾に連結するED動画（mp4等）
    "intro_text": "",                  # 台本の最初に挿入する定型ナレーション
    "outro_text": "",                  # 台本の最後に挿入する定型ナレーション
    # ショート（縦動画）は横用のoutro_textを使わず、こちらの誘導文だけを最後に読む
    # （空=無し。「フォローで続きを…」等ショート特有の導線・2026-08-19ユーザー要望）
    "outro_text_shorts": "",
    "thumbnail_prompt": "",            # サムネ生成プロンプト
    "thumb_font": "",                  # サムネ文字のフォント（表示名 or ファイル名・空=自動）

    # ── チャンネル（世界観プリセット×10。1チャンネル=1つの番組コンセプト） ──
    "channels": [],                    # [{name, script_prompt, brushup_prompt, gemini_tts_style,
                                       #   visual_prompt_template, thumbnail_prompt}] ×10
    "active_channel": 0,               # 現在選択中のチャンネル番号(0-9)

    # ── APIキー/認証（ローカル平文・クラウド同期しない） ──
    "anthropic_api_key": "",
    "openai_api_key": "",
    "gemini_api_key": "",
    "google_tts_credentials_json": "",  # サービスアカウントJSONのパス（空なら環境変数/ADC）
    "youtube_client_secret": str(ROOT / "client_secret.json"),
    "youtube_token": str(ROOT / "token.json"),
    "ai_model": "claude-opus-4-8",
    "openai_text_model": "gpt-5.5",     # ①台本 ChatGPT API（使えない時は gpt-4o に自動フォールバック）
    "gemini_text_model": "gemini-2.5-flash",  # ①台本 Gemini API
    "image_model": "gpt-image-1",
    "gemini_image_model": "gemini-2.5-flash-image",
    "image_size": "1536x1024",          # OpenAI gpt-image-1 横長
    # 動画API（③動画。従量課金・高額になり得るので本数/秒数は控えめに）
    "sora_model": "sora-2",             # sora-2(720p 約$0.10/秒) / sora-2-pro(〜1080p 約$0.30/秒〜)
    "sora_seconds": 8,                  # 1クリップの秒数
    "veo_model": "veo-3.1-fast-generate-preview",  # fast=約$0.15/秒 / veo-3.1-generate-preview=約$0.40/秒
    "veo_seconds": 8,                   # 4/6/8
    "video_api_resolution": "720p",     # 720p / 1080p

    # ── TTS（②） ──
    "tts_lang": "ja-JP",
    "tts_voice": "ja-JP-Neural2-C",     # 女性。男性は -B / -D（Google TTS用）
    "tts_speaking_rate": 1.0,
    "tts_pitch": 0.0,
    "tts_sample_rate": 24000,
    # Gemini / AI Studio TTS（gemini_api_key を使用）
    "gemini_tts_model": "gemini-3.1-flash-tts-preview",  # 旧2.5-flash-preview-ttsはquota枠が狭く429頻発
    "gemini_tts_api_key": "",           # 音声だけ別キーにしたい場合（例: onsei_dev）。空=共通キー
    "gemini_tts_voice": "Kore",
    # 無料TTS（Microsoft Edge・キー/課金不要）
    "edge_voice": "ja-JP-NanamiNeural",
    "edge_rate": "+0%",
    # Fish Audio TTS（高品質・API）: 声は fish.audio のモデルID(reference_id) を貼る
    "fish_api_key": "",
    "fish_model": "s2.1-pro-free",
    "fish_voice": "",                   # reference_id（空=モデル既定の声＝毎回変わる。声を固定するなら必須）
    "fish_voice_book": [],              # 登録済みの声 [{name, id}] → 作成タブの声コンボから選べる
    # 作り直しタブの入力の控え（自動保存で残す。再起動で声コード・枚数が消えない）
    "remake_voice": "",
    "remake_num_clips": "",
    "remake_num_images": "",
    "step_voice": "",                   # ステップ実行タブの声欄の控え（2026-08-20）
    "fish_temperature": 0.5,            # 低いほどトーンが安定（既定0.7は揺れやすい）
    "fish_top_p": 0.5,
    # デスクトップアプリ連携（ChatGPT/Codex・Claude Code のデスクトップ版・Windows）
    "desktop_app_titles": {"codex_desktop": "ChatGPT", "claudecode_desktop": "Claude"},
    "desktop_app_paths": {},            # アプリ未起動時の自動起動に使うexe/lnk（空=自動探索）
    "desktop_launch_wait": 60,          # 自動起動後にウィンドウが出るまで待つ上限（秒）
    "desktop_watch_dir": "",            # 画像の保存先を監視（空=ユーザーのDownloads）
    "desktop_timeout": 480,             # 回答/画像の受け取り待ち上限（秒）
    "desktop_auto_copy": True,          # 回答コピーを自動で押す（False=従来の手動コピー）
    "desktop_copy_hotkey": "ctrl+shift+c",  # 「最後の回答をコピー」のショートカット
    # 送信前に新規チャットを開くキー（エンジン別上書き。既定: Codex=ctrl+shift+o /
    # Claude Code=無効。例 {"codex_desktop": ""} で無効化、キー変更も可）
    "desktop_new_chat_hotkeys": {},
    "codex_auto_capture": True,         # 会話ログから回答/画像を自動取得（Codex/Claude Code共通）
    "codex_sessions_dir": "",           # Codexの会話ログの場所（空=~/.codex/sessions を自動）
    "claude_projects_dir": "",          # Claude Codeのログの場所（空=~/.claude/projects を自動）
    # ローカル音声（VOICEVOX/AivisSpeech）
    "voicevox_url": "http://127.0.0.1:50021",  # AivisSpeechは別ポートの場合あり
    "voicevox_speaker": 3,
    # 録音ナレーションを使う（tts_engine="file"）。テロップはwhisper自動同期
    "narration_file": "",

    # ── テロップ（④） ──
    "telop_preset": "標準（白・黒縁）",     # TELOP_PRESETS のキー。"カスタム"＝下の個別値をそのまま使用
    "telop_border_style": 1,            # 1=縁取り / 3=帯（ノベル風の半透明ボックス）
    "telop_back_color": "&H64000000",   # 帯の色（BorderStyle=3のとき）
    "telop_font": "Yu Gothic UI",
    "telop_fontsize": 72,
    "telop_primary_color": "&H00FFFFFF",   # 白（ASS BGR）
    "telop_outline_color": "&H00000000",   # 黒縁
    "telop_outline": 4,
    "telop_shadow": 2,
    "telop_max_chars": 24,
    "telop_alignment": 2,                  # ASS: 2=下中央
    "telop_margin_v": 90,
    "align_method": "segments",            # segments(行単位合成で同期) | whisper
    "whisper_model": "medium",             # whisper強制アライン時のモデル(tiny/base/small/medium)

    # ── 映像（③/④） ──
    "video_width": 1920,
    "video_height": 1080,
    "video_fps": 30,
    "enc_quality": 20,
    "num_images": 5,                       # 動画内で使う画像の最大枚数（時系列で切替）
    "num_video_clips": 3,                  # 動画内で使う動画クリップの最大本数
    "video_wait_sec": 600,                 # 動画HITL: 人の生成/DL待ち上限
    "studio_wait_sec": 300,                # Studio Web HITL 待ち上限

    # ── BGM（④。Phase3で有効化） ──
    "bgm_library": [],                     # [{path,title,mood(=ジャンル),gain_db,channels(=対応ch索引list・空=全ch)}]
    "bgm_mode": "none",                    # none | select | random
    "bgm_selected": "",
    "bgm_duck_db": -12,                    # （廃止・互換のため残置。2026-07-30ダッキング全廃）
    "bgm_base_db": -8,                     # BGMの音量(dB)。平坦化後の基準(-22LUFS)からの相対
                                           # （既定-8＝ナレの10〜14LU下で一定音量）

    # ── YouTube / 予約（⑥） ──
    "youtube_channel_url": "",
    "youtube_channel_url_global": "",  # 投稿タブの「全体設定」URL（ch別と分離＝相互汚染防止）
    "youtube_privacy": "private",
    "youtube_category_id": "22",           # People & Blogs
    "youtube_made_for_kids": False,
    "default_schedule_slots": ["09:00", "13:00", "18:00", "21:00"],
    "default_publish_offset_days": 1,
    "upload_dry_run": False,               # Trueで投稿本文だけ組み立て（実投稿しない）
    # Studioブラウザ投稿: 予約日時の入力→『スケジュール』確定まで全自動（2026-08-06方針転換。
    # 日付・時刻は読み戻し検証に合格した時だけ確定＝誤予約はフェイルセーフで手動へ落ちる。
    # False=従来どおり詳細画面まで自動・最後は人が押す）
    # Web画像生成の高速モデル自動選択（2026-08-10ユーザー要望）: 画像生成のチャットでは
    # モデルピッカーからこの名前（部分一致）を選ぶ。長考モデル(Thinking/Pro等)だと
    # 画像1枚に数分かかるため。見つからなければ現在のモデルのまま（フェイルオープン）。
    # 空文字にすると触らない。台本・ブラッシュアップのチャットには影響しない
    "web_image_model_chatgpt": "",           # 空=モデルは変更しない（2026-08-25 UI: GPT-5.6 Sol等）
    "web_image_effort_chatgpt": "最速",      # ChatGPTの思考量（最速/中程度/高い・空=変更しない）
    "web_image_model_gemini": "3.7 Flash",   # Geminiのモデル（3.5 Flash-Lite/3.7 Flash/3.1 Pro）
    "studio_auto_publish": True,
    # ↑の一回きり移行マーカー: 旧版(〜2026-08-06e)は既定OFFで保存されていたため、
    # 既存ユーザーのsettings.jsonにはFalseが焼き付いている。マーカー未保存=旧版からの
    # 更新と判定して一度だけTrueへ引き上げる（以後ユーザーがOFFにしたら尊重される）
    "studio_auto_publish_migrated": False,

    # ── 夜間無人運用の自己回復（2026-07-07 最強化会議） ──
    "stage_retries": 2,                 # script/tts/visualsの自動リトライ回数（30秒×n バックオフ）
    "tts_max_silent_ratio": 0.15,       # 無音行がこの割合を超えたら②を失敗にする（ゴミ動画防止）
    "tts_kana_reading": False,          # 全文カナ変換（非推奨: 助詞「は」がハと読まれる。既定は辞書置換のみ）
    "tts_year_kana": True,              # 西暦を読み下してTTSへ渡す（1990年→せんきゅうひゃく…ねん）
    "youtube_rewrite_mode": "auto",     # YouTubeリライト: auto=自動判定 / fact=事実系 / fiction=創作系
    "telop_min_chars": 8,               # 、。で区切っても短すぎるテロップは次に続けて1枚にまとめる
    "script_fallback_chain": ["api", "gemini_api", "chatgpt_api",
                              "claude_web", "gemini_web", "chatgpt_web"],
    "visual_fallback_chain": ["gemini_api", "api", "gemini_web", "chatgpt_web"],
    "tts_fallback_chain": ["edge"],     # 音声: Gemini失敗→Edgeへ自動切替（Google Cloudは撤去済み）
    # ── 映像演出 ──
    "kenburns": True,                   # 静止画にズーム/パン（Ken Burns）を付ける
    "first_minute_max_cut": 8,          # 冒頭60秒はこの秒数以下にカットを割る（画面が死なない）
    "thumb_accent_color": "#FFE600",    # サムネ文字2行目のアクセント色
    "thumb_color_top": "",              # サムネ文字の上段色（プリセット名 or #RRGGBB・空=白）
    "thumb_color_bottom": "",           # サムネ文字の下段色（空=アクセント色）
    # ── 量産スケジュール/夜間バッチ ──
    "max_uploads_per_day": 2,           # 予約自動割当の1日上限（同日に詰め込みすぎない）
    "batch_second_pass": True,          # 量産で失敗した1本を60秒後に続きから再挑戦
    "batch_cooldown_sec": 45,           # 量産の本間クールダウン（レート制限対策）
    "batch_max_consecutive_fails": 3,   # 連続失敗でバッチ中止（無駄課金防止ブレーカ）

    # ── 出力/再開/ブラウザ/デバッグ ──
    "output_dir": str(ROOT / "output"),
    "resume_dir": "",                      # 空＝新規。既存フォルダ指定で途中再開
    "chrome_profile": str(ROOT / "chrome_profile"),
    "browser_exe": "",                     # ""=Chrome自動検出 / "edge" / "brave" / exeフルパス
    "cdp_port": 9222,
    "headless": False,
    "debug_dump": True,

    # 各ステージで「全実行時も停止して人の確認を待つ」か（HITL）
    "require_checkpoint": {"visuals": True, "video": True, "upload": True},

    # ── BGMのAI新規作成（Suno） ──
    "suno_api_key": "",                 # sunoapi.org（サードパーティ）のキー。公式APIは無い
    "suno_api_base": "https://api.sunoapi.org",
    "suno_model": "V5",                 # V4 / V4_5 / V4_5PLUS / V5 / V5_5
    "suno_instrumental": True,          # BGM用途なので既定は歌なし
    "suno_wait_sec": 300,               # 生成待ちの上限秒

    # ── 猫ミームモード ──
    "nekomeme_dir": str(ROOT / "_ref" / "nekomeme"),  # 素材フォルダ（クリップ/BGM/SE）
    "neko_sec_per_char": 0.26,          # 1文字あたりの表示秒（さらにゆったりへ再調整）
    "neko_min_sec": 4.0,                # 1セリフの最低表示秒
    "neko_bgm_db": -14.0,               # 猫ミームBGMの基準音量(dB)。GUI(BGMタブ)で調整可
    "neko_meme_db": -14.0,              # 動物クリップの音量(dB)。旧=ほぼ原音でBGMを圧倒していた
    "neko_se_db": -18.0,                # 場面転換SE(鳩ポッポ)の音量(dB)。旧=無ゲインで最大だった
    "neko_break_sec": 0.6,              # 場面転換の間(秒)。旧1.0秒は「謎の間」と指摘された
    "neko_keep_narrator": False,        # True=「ナレーション」話者を主人公の一人語りに変えない（UI無し）
    "neko_shorts_font_scale": 1.5,      # 猫ミーム縦ショートのセリフ文字倍率（UI無し・2026-08-25）
    "neko_shorts_text_y": 0.0,          # 同・テロップ位置の上下ずらし（高さ比。-0.05=少し上へ）
}

# ── テロップのデザインプリセット（同梱フォントは izanagi/fonts.py が自動登録） ──
TELOP_PRESETS = {
    "標準（白・黒縁）": {
        "telop_font": "Yu Gothic UI", "telop_fontsize": 72,
        "telop_primary_color": "&H00FFFFFF", "telop_outline_color": "&H00000000",
        "telop_outline": 4, "telop_shadow": 2, "telop_border_style": 1},
    "ポップ（けいふぉんと）": {
        "telop_font": "Keifont", "telop_fontsize": 78,
        "telop_primary_color": "&H00FFFFFF", "telop_outline_color": "&H00000000",
        "telop_outline": 5, "telop_shadow": 0, "telop_border_style": 1},
    "黄色バラエティ（ポップル）": {
        "telop_font": "GenEi POPle", "telop_fontsize": 80,
        "telop_primary_color": "&H0000FFFF", "telop_outline_color": "&H00000000",
        "telop_outline": 5, "telop_shadow": 2, "telop_border_style": 1},
    "極太インパクト（丸ゴシック）": {
        "telop_font": "Rounded-X M+ 1c", "telop_fontsize": 84,
        "telop_primary_color": "&H00FFFFFF", "telop_outline_color": "&H00000000",
        "telop_outline": 6, "telop_shadow": 3, "telop_border_style": 1},
    "シネマ明朝（ちくご明朝）": {
        "telop_font": "GenEi Chikugo Mincho v3", "telop_fontsize": 66,
        "telop_primary_color": "&H00FFFFFF", "telop_outline_color": "&H00000000",
        "telop_outline": 2, "telop_shadow": 3, "telop_border_style": 1},
    "和風毛筆": {
        "telop_font": "mouhituP", "telop_fontsize": 82,
        "telop_primary_color": "&H00FFFFFF", "telop_outline_color": "&H00000000",
        "telop_outline": 4, "telop_shadow": 2, "telop_border_style": 1},
    "レトロアンティーク": {
        "telop_font": "GenEi Antique v6", "telop_fontsize": 70,
        "telop_primary_color": "&H00F0FFFF", "telop_outline_color": "&H00202020",
        "telop_outline": 3, "telop_shadow": 2, "telop_border_style": 1},
    "ノベル風（半透明帯・明朝）": {
        "telop_font": "GenEi Chikugo Mincho v3", "telop_fontsize": 62,
        "telop_primary_color": "&H00FFFFFF", "telop_outline_color": "&H00000000",
        "telop_outline": 10, "telop_shadow": 0, "telop_border_style": 3,
        "telop_back_color": "&HA0000000"},
    "カスタム": {},  # 個別設定をそのまま使う
}

# チャンネル（世界観）として保存するキー
CHANNEL_KEYS = ("script_prompt", "topic_prompt", "brushup_prompt", "gemini_tts_style",
                "visual_prompt_template", "thumbnail_prompt",
                "intro_video", "outro_video", "intro_text", "outro_text",  # 冒頭・終了
                "outro_text_shorts",    # ショート用しめ誘導文（2026-08-19）
                # ── 2026-07-07 最強化会議で追加（すべて文字列・空=無効） ──
                "title_prefix",         # タイトル接頭辞（例:【スカッと】）
                "desc_footer",          # 概要欄の定型フッター（登録導線など）
                "fixed_tags",           # 固定タグ（カンマ区切り・meta.tagsに追記）
                "youtube_channel_url",  # このチャンネルの投稿先URL（誤爆投稿防止）
                "schedule_slots")       # ch別の投稿時刻スロット（例 09:00,18:00。空=全体設定）
NUM_CHANNELS = 10


def _init_channels(cfg: dict) -> None:
    """channels を10枠に正規化。初回は既存のトップレベル・プロンプトをチャンネル1へ移行。"""
    chs = cfg.get("channels")
    if not isinstance(chs, list):
        chs = []
    chs = [c for c in chs if isinstance(c, dict)][:NUM_CHANNELS]
    first_time = not chs
    while len(chs) < NUM_CHANNELS:
        chs.append({"name": f"チャンネル{len(chs) + 1}",
                    **{k: "" for k in CHANNEL_KEYS}})
    if first_time:
        for k in CHANNEL_KEYS:
            chs[0][k] = cfg.get(k, "") or ""
    for i, c in enumerate(chs):
        c.setdefault("name", f"チャンネル{i + 1}")
        for k in CHANNEL_KEYS:
            c.setdefault(k, "")
        if not isinstance(c.get("topic_queue"), list):  # お題キュー（list型・トップレベル展開しない）
            c["topic_queue"] = []
    cfg["channels"] = chs
    try:
        idx = int(cfg.get("active_channel", 0))
    except Exception:
        idx = 0
    cfg["active_channel"] = min(max(idx, 0), NUM_CHANNELS - 1)
    # アクティブチャンネルの内容をトップレベルへ展開（pipeline側は従来キーのまま動く）
    for k in CHANNEL_KEYS:
        v = chs[cfg["active_channel"]].get(k, "")
        if k == "youtube_channel_url" and not v:
            # chに投稿先URLが無い時は「全体設定」(youtube_channel_url_global)を使う
            cfg[k] = cfg.get("youtube_channel_url_global") or cfg.get(k, "") or ""
            continue
        cfg[k] = v


def _import_sibling_keys(cfg: dict) -> None:
    """姉妹ツールの settings.json から未設定のAPIキーだけ補完する。"""
    wanted = ("anthropic_api_key", "openai_api_key", "gemini_api_key")
    for sp in _SIBLING_SETTINGS:
        try:
            if not sp.exists():
                continue
            data = json.loads(sp.read_text(encoding="utf-8"))
        except Exception:
            continue
        for k in wanted:
            if not cfg.get(k) and data.get(k):
                cfg[k] = data[k]


def _heal_internal_paths(cfg: dict) -> dict:
    """フォルダを改名/移動しても内部パス（chrome_profile / output）が迷子にならないよう補正。"""
    try:
        cp = cfg.get("chrome_profile") or ""
        p = Path(cp) if cp else None
        if (p is None) or (p.name == "chrome_profile" and p.parent != ROOT):
            cfg["chrome_profile"] = str(ROOT / "chrome_profile")
    except Exception:
        cfg["chrome_profile"] = str(ROOT / "chrome_profile")
    try:
        od = cfg.get("output_dir") or ""
        if od:
            p = Path(od)
            if p.name == "output" and p != (ROOT / "output"):
                # 付け替えるのは「ツール自身を移動/改名した」と分かる時だけ。
                # 「存在しない」だけで書き換えると、外付けHDD/NASが未接続なだけでも
                # ツール直下\output へ黙って書き換わり settings.json に保存される
                # （ドライブを繋ぎ直しても設定が失われたまま＝動画が想定外の場所に出る）
                drive_ok = True
                try:
                    if p.anchor:
                        drive_ok = Path(p.anchor).exists()   # 未接続ドライブ判定
                except Exception:
                    pass
                looks_install = (p.parent.name == ROOT.name
                                 or p.parent.parent == ROOT.parent)
                if looks_install and drive_ok and not p.exists():
                    cfg["output_dir"] = str(ROOT / "output")
    except Exception:
        pass
    # 別PCへ引っ越した settings.json 対策: 保存パスが存在せず、同名のものが
    # アプリの真横（ROOT基準の定位置）にあるならそちらへ付け替える
    try:
        nd = cfg.get("nekomeme_dir") or ""
        if nd and not Path(nd).exists() and (ROOT / "_ref" / "nekomeme").exists():
            cfg["nekomeme_dir"] = str(ROOT / "_ref" / "nekomeme")
        for k in ("youtube_client_secret", "google_tts_credentials_json", "youtube_token"):
            v = cfg.get(k) or ""
            if v and not Path(v).exists():
                cand = ROOT / Path(v).name
                if cand.exists():
                    cfg[k] = str(cand)
    except Exception:
        pass
    return cfg


def _heal_slots(cfg: dict) -> None:
    """予約スロットの汚染（str(list)保存の名残 "['09:00'" 等）を HH:MM に正規化する。"""
    import re
    raw = cfg.get("default_schedule_slots", [])
    if not isinstance(raw, (list, tuple)):
        raw = [str(raw)]
    out = []
    for s in raw:
        m = re.search(r"(\d{1,2}):(\d{2})", str(s))
        if m:
            hh, mm = int(m.group(1)), m.group(2)
            if 0 <= hh <= 23:
                out.append(f"{hh:02d}:{mm}")
    cfg["default_schedule_slots"] = out or list(DEFAULTS["default_schedule_slots"])


def _read_json(path) -> dict | None:
    """JSONとして読めたら dict、壊れていたら None（破損検知に使う）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _safe_print(msg: str) -> None:
    """コンソールがcp932/無しでも起動を壊さないprint（破損復元の告知用）。"""
    try:
        print(msg)
    except Exception:
        try:
            print(msg.encode("ascii", "ignore").decode("ascii"))
        except Exception:
            pass


def load_settings() -> dict:
    cfg = dict(DEFAULTS)
    first_time = not SETTINGS_PATH.exists()
    if SETTINGS_PATH.exists():
        saved = _read_json(SETTINGS_PATH)
        if saved is None:  # 破損 → 黙って初期値にせず .bak から復元を試みる
            saved = _read_json(BAK_PATH)
            if saved is not None:
                _safe_print("[IZANAMI] settings.json が壊れていたため settings.json.bak から復元しました。")
            else:
                _safe_print("[IZANAMI] settings.json が壊れており .bak も読めません。初期設定で起動します"
                            "（backups/ の世代バックアップから手動復元できます）。")
        if saved:
            cfg.update({k: v for k, v in saved.items() if k in DEFAULTS})
    else:
        # 初回起動: 同梱の「初期設定.json」を種に settings.json を作る。
        # 新規の人はチャンネルの世界観・エンジン等が設定済みの状態で始められ、
        # 既存の人はフルzipで上書き更新しても settings.json が配布物に無いので安全
        seed = _read_json(SEED_PATH)
        if seed:
            cfg.update({k: v for k, v in seed.items() if k in DEFAULTS})
            _safe_print("[IZANAMI] 初回起動: 同梱の初期設定から settings.json を作成しました。")
    # 旧形式からの移行: 全体設定URLが未分離ならトップレベル値を引き継ぐ
    if not cfg.get("youtube_channel_url_global") and cfg.get("youtube_channel_url"):
        cfg["youtube_channel_url_global"] = cfg["youtube_channel_url"]
    # 初回 or キー未設定なら姉妹ツールから取り込む
    if not all(cfg.get(k) for k in ("anthropic_api_key", "openai_api_key")):
        _import_sibling_keys(cfg)
    _heal_internal_paths(cfg)
    _heal_slots(cfg)
    _init_channels(cfg)
    # 会話（対談）形式は機能廃止（2026-07-07）: 残っていたら一人語りへ移行
    if cfg.get("script_format") == "dialog":
        cfg["script_format"] = "narration"
    # 予約確定の全自動化（2026-08-06方針転換）: 旧版の既定OFFが焼き付いた既存settingsを
    # 一度だけONへ引き上げる。マーカー保存後はユーザーのOFF選択を尊重する
    if not cfg.get("studio_auto_publish_migrated"):
        cfg["studio_auto_publish"] = True
        cfg["studio_auto_publish_migrated"] = True
    # 主人公の性別自動調整: 型汚染（文字列等）で起動不能にならないよう既定へ戻す
    if not isinstance(cfg.get("voice_by_gender"), dict):
        cfg["voice_by_gender"] = json.loads(json.dumps(DEFAULTS["voice_by_gender"]))
    # 旧Gemini TTSモデルはquota枠が狭く429頻発 → 新モデルへ自動移行（2026-07-07実測）
    if cfg.get("gemini_tts_model") == "gemini-2.5-flash-preview-tts":
        cfg["gemini_tts_model"] = "gemini-3.1-flash-tts-preview"
    # Google Cloud TTSはGUI撤去（音声はGemini一本化）: 残っていたらGeminiへ
    if cfg.get("tts_engine") == "api":
        cfg["tts_engine"] = "gemini"
    if "api" in (cfg.get("tts_fallback_chain") or []):
        cfg["tts_fallback_chain"] = [e for e in cfg["tts_fallback_chain"] if e != "api"] or ["edge"]
    # ブラウザ手動(HITL)の動画生成は廃止（2026-07-07）: 残っていたら「使わない」へ
    # sora_api はOpenAIのサービス終了（2026-09-24 API廃止）に伴い撤去
    # ※ gemini_web は 2026-07-25 に完全自動化してGUIへ復活させたので**ここから外す**
    #   （外し忘れて、選んで保存しても再起動のたびに none へ戻る不具合になっていた）
    if cfg.get("video_engine") in ("flow_web", "sora_web", "sora_api"):
        cfg["video_engine"] = "none"
    # 旧既定値のままの猫ミームテンポを新既定へ移行（0.11/1.6→0.22/3.2→0.26/4.0）
    if cfg.get("neko_sec_per_char") in (0.11, 0.22):
        cfg["neko_sec_per_char"] = DEFAULTS["neko_sec_per_char"]
    if cfg.get("neko_min_sec") in (1.6, 3.2):
        cfg["neko_min_sec"] = DEFAULTS["neko_min_sec"]
    # 猫ミームBGM -30dB は下げすぎ（BGMがほぼ聞こえない・2026-07-21配布先報告I）→ -20へ是正
    if cfg.get("neko_bgm_db") == -30.0:
        cfg["neko_bgm_db"] = -20.0
    # 画像モデル欄の旧値（2026-08-10導入時の名前）→ 2026-08-25 UI（モデル/思考量の2軸）へ移行
    _old_cg = {"Instant": "最速", "Medium": "中程度", "High": "高い",
               "Extra High": "高い", "Pro": "高い"}
    if cfg.get("web_image_model_chatgpt") in _old_cg:
        cfg["web_image_effort_chatgpt"] = _old_cg[cfg["web_image_model_chatgpt"]]
        cfg["web_image_model_chatgpt"] = ""     # モデル自体は変更しない（思考量だけ速く）
    _old_gm = {"Flash": "3.7 Flash", "Flash-Lite": "3.5 Flash-Lite", "Pro": "3.1 Pro"}
    if cfg.get("web_image_model_gemini") in _old_gm:
        cfg["web_image_model_gemini"] = _old_gm[cfg["web_image_model_gemini"]]
    if first_time:
        try:
            save_settings(cfg)
        except Exception:
            pass
    return cfg


_SAVE_LOCK = threading.Lock()

# ── 設定ファイルの書き出し/読み込み（バックアップ・引っ越し・人に渡す） ──────
# 「人に渡す用」で空にするもの＝秘密（キー/認証）と、このPCでしか意味を持たないパス
EXPORT_SECRET_KEYS = ("anthropic_api_key", "openai_api_key", "gemini_api_key",
                      "gemini_tts_api_key", "fish_api_key", "suno_api_key",
                      "youtube_client_secret", "youtube_token",
                      "google_tts_credentials_json",
                      # fish.audioのreference_id＝自分で登録したクローン声の識別子
                      # （remake_voiceは作り直しタブの控え＝同じくクローン声IDが入り得る）
                      "fish_voice", "remake_voice", "step_voice")
EXPORT_MACHINE_KEYS = ("output_dir", "chrome_profile", "nekomeme_dir", "browser_exe",
                       # デスクトップ連携などのPC固有パス（Windowsユーザー名が入る）
                       "codex_sessions_dir", "claude_projects_dir", "desktop_watch_dir",
                       "narration_file", "resume_dir",
                       # 匿名運営チャンネルの特定につながるURL
                       "youtube_channel_url", "youtube_channel_url_global")
# 渡した先には存在しないファイルを指すパス（OP/ED動画・BGM）
_EXPORT_PATH_KEYS = ("intro_video", "outro_video", "bgm_selected")
# 空にする入れ子構造（キー: 空にした後の値）
_EXPORT_BLANK_STRUCTS = {"bgm_library": [], "fish_voice_book": [],
                         "desktop_app_paths": {}}


def export_settings(cfg: dict, include_secrets: bool) -> dict:
    """設定をファイル書き出し用に整形して返す（settings.jsonと同じ形）。

    include_secrets=True : 自分のバックアップ用＝そのまま全部。
    include_secrets=False: 人に渡す用＝APIキー・認証・このPC固有のパス・
      OP/ED動画やBGMのファイルパスを空にする（お題/エンジン/世界観プロンプト/
      テロップ/枚数/冒頭終了の文などの「作り方」は全部入る）。"""
    out = {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}
    out = json.loads(json.dumps(out, ensure_ascii=False))   # 深いコピー（元を汚さない）
    if not include_secrets:
        for k in (*EXPORT_SECRET_KEYS, *EXPORT_MACHINE_KEYS, *_EXPORT_PATH_KEYS):
            if k in out:
                out[k] = ""
        for k, empty in _EXPORT_BLANK_STRUCTS.items():
            if k in out:
                out[k] = json.loads(json.dumps(empty))
        for ch in (out.get("channels") or []):
            if isinstance(ch, dict):
                ch["intro_video"] = ""
                ch["outro_video"] = ""
                ch["youtube_channel_url"] = ""   # ch別の投稿先URL＝運営者の特定情報
        # 性別自動切替のFish声IDも配布者のクローン声＝渡さない（2026-08-17でfish欄追加）
        vbg = out.get("voice_by_gender")
        if isinstance(vbg, dict):
            for g in ("female", "male"):
                if isinstance(vbg.get(g), dict):
                    vbg[g]["fish"] = ""
    return out


def merge_imported(incoming: dict, current: dict) -> dict:
    """読み込んだ設定ファイルを今の設定に重ねる。

    基本はファイル側が勝つ（＝設定を取り込むのが目的）。ただし秘密キーと
    PC固有パス・OP/ED・BGMは、**ファイル側が空なら今の値を残す**＝
    「人に渡す用」ファイルを読み込んでも自分のAPIキーや保存先が消えない。

    さらに2つの防波堤（2026-08-03敵対検証）:
    - 型ゲート: DEFAULTSと型が合わない値は取り込まない。手編集で壊れた値
      （例: bgm_libraryが文字列）がsettings.jsonに固定化されると、
      **次回から起動のたびにクラッシュ＝実質起動不能**になるため。
    - channels保護: ファイル側が空/型違い/要素数不足でも、自分の10チャンネル分の
      世界観・プロンプトを失わない。"""
    def _type_ok(k, v):
        d = DEFAULTS[k]
        if d is None:
            return True
        if isinstance(d, bool):
            return isinstance(v, bool)
        if isinstance(d, (int, float)):
            return isinstance(v, (int, float)) and not isinstance(v, bool)
        return isinstance(v, type(d))

    cur = {k: current.get(k, DEFAULTS[k]) for k in DEFAULTS}
    inc = {k: v for k, v in (incoming or {}).items()
           if k in DEFAULTS and _type_ok(k, v)}
    # channelsの特別扱い: 空→取り込まない／要素数不足→不足分は自分のchを残す／
    # dictでない要素→自分のchで差し替え
    curch = cur.get("channels") or []
    incch = inc.get("channels")
    if isinstance(incch, list):
        if not incch:
            inc.pop("channels", None)
        else:
            fixed = [ch if isinstance(ch, dict)
                     else (curch[i] if i < len(curch) and isinstance(curch[i], dict)
                           else {})
                     for i, ch in enumerate(incch)]
            if len(fixed) < len(curch):
                fixed += curch[len(fixed):]
            inc["channels"] = fixed
    merged = dict(cur)
    merged.update(inc)
    for k in (*EXPORT_SECRET_KEYS, *EXPORT_MACHINE_KEYS, *_EXPORT_PATH_KEYS,
              *_EXPORT_BLANK_STRUCTS):
        if not inc.get(k) and cur.get(k):
            merged[k] = cur[k]
    # チャンネル別のOP/ED・投稿先URLも同じ（世界観は取り込みつつ、自分のものは残す）
    try:
        curch = cur.get("channels") or []
        for i, ch in enumerate(merged.get("channels") or []):
            if not isinstance(ch, dict):
                continue
            for k in ("intro_video", "outro_video", "youtube_channel_url"):
                if not ch.get(k) and i < len(curch) and (curch[i] or {}).get(k):
                    ch[k] = curch[i][k]
    except Exception:
        pass
    return merged


def looks_like_settings(data) -> bool:
    """IZANAMIの設定ファイルらしいか（無関係なJSONの誤読込を防ぐ）。

    ※動画フォルダ内の run_cfg.json はDEFAULTSのキーを50個以上含むため、
      キー数だけでは誤受理する（読み込むと動画1本の実行条件が全体設定を
      上書きしてしまう）。設定ファイルにしか無いキー（channels等）も要求する。"""
    if not isinstance(data, dict):
        return False
    if sum(1 for k in data if k in DEFAULTS) < 10:
        return False
    return any(k in data for k in ("channels", "active_channel", "anthropic_api_key"))


_KNOWN_MTIME = None   # このプロセスが最後に読んだ/書いた settings.json の更新時刻


def _note_mtime() -> None:
    global _KNOWN_MTIME
    try:
        _KNOWN_MTIME = SETTINGS_PATH.stat().st_mtime_ns
    except Exception:
        _KNOWN_MTIME = None


def note_settings_seen() -> None:
    """起動時に「この内容を画面に読み込んだ」印を付ける（以後、別プロセスの保存を検知し、
    force 無しの保存は見送る）。※load_settings() の中では付けない（量産完了ごとの再読込で
    検知がリセットされないように）。"""
    global _GUARD_ON
    _note_mtime()
    _GUARD_ON = True


def settings_changed_externally() -> bool:
    """このプロセスが最後に読み書きした後に、別のプロセス（二重起動した古い/新しい窓など）が
    settings.json を更新したか。True の時に無条件保存すると相手の設定を巻き戻す
    （2026-08-20「アップデート後に終了画面の設定が消えた」の有力機構）。"""
    if _KNOWN_MTIME is None:
        return False
    try:
        return SETTINGS_PATH.stat().st_mtime_ns != _KNOWN_MTIME
    except Exception:
        return False


_GUARD_ON = False          # note_settings_seen() で有効化（GUIプロセスだけ）
ON_SAVE_SKIPPED = None     # 外部更新で保存を見送った時に呼ぶ（App がログ用に設定）
LAST_SAVE_SKIPPED = False


def save_settings(cfg: dict, force: bool = False) -> bool:
    """settings.json へ保存。戻り値 True=書いた / False=外部更新を検知して見送った。

    GUI起動後（note_settings_seen 済み）に、別プロセス（二重起動した窓など）が settings.json を
    更新していたら、force=False の保存は見送る＝相手の設定を古い画面値で巻き戻さない。
    💾（明示保存）と×閉じの「はい」は force=True で書く。"""
    global LAST_SAVE_SKIPPED
    if _GUARD_ON and not force and settings_changed_externally():
        LAST_SAVE_SKIPPED = True
        try:
            if ON_SAVE_SKIPPED:
                ON_SAVE_SKIPPED()
        except Exception:
            pass
        return False
    LAST_SAVE_SKIPPED = False
    out = {k: cfg.get(k, DEFAULTS[k]) for k in DEFAULTS}
    data = json.dumps(out, ensure_ascii=False, indent=2)
    with _SAVE_LOCK:  # GUIスレッドと量産ワーカーの同時保存で壊さない
        try:
            # .bak は「読めるJSONだった時だけ」更新（破損を.bakへ伝播させない）
            if SETTINGS_PATH.exists() and _read_json(SETTINGS_PATH) is not None:
                shutil.copy2(SETTINGS_PATH, BAK_PATH)
        except Exception:
            pass
        tmp = SETTINGS_PATH.with_suffix(".json.tmp")
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, SETTINGS_PATH)  # atomic: 書き込み途中の電源断/クラッシュで壊さない
        _note_mtime()
    return True


# ── バックアップ/復元（★Bダッシュ準拠） ──────────────────────────────
def backup_settings(keep: int = BACKUP_KEEP, label: str = "") -> str:
    try:
        if not SETTINGS_PATH.exists() or SETTINGS_PATH.stat().st_size == 0:
            return ""
        if _read_json(SETTINGS_PATH) is None:
            return ""  # 壊れたファイルを世代登録しない（正常世代の押し出し防止）
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        suffix = f"_{label}" if label else ""
        dest = BACKUP_DIR / f"settings_{stamp}{suffix}.json"
        n = 1
        while dest.exists():
            dest = BACKUP_DIR / f"settings_{stamp}{suffix}_{n}.json"
            n += 1
        shutil.copy2(SETTINGS_PATH, dest)
        _prune_backups(keep)
        return str(dest)
    except Exception:
        return ""


def _prune_backups(keep: int = BACKUP_KEEP) -> None:
    try:
        files = sorted(BACKUP_DIR.glob("settings_*.json"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
        # 設定ファイル読み込みの「読込前」控えは別枠で3世代守る。
        # 通常枠に混ぜると起動毎バックアップ(keep=5)に押し出され、
        # 約5回起動しただけで読込前の状態へ戻せなくなる（2026-08-03敵対検証）
        important = [p for p in files if "読込前" in p.name]
        normal = [p for p in files if "読込前" not in p.name]
        for old in normal[keep:] + important[3:]:
            try:
                old.unlink()
            except Exception:
                pass
    except Exception:
        pass
