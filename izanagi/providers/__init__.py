"""プロバイダ抽象レイヤ。

capability（script/tts/visual/video/thumb/upload）ごとに ABC を定義し、
(capability, engine) → 実装クラス を REGISTRY で引く。
settings[f"{cap}_engine"] で "api"|"browser"|"mock"|vendor を選択。
必要なキー/認証が無ければ自動で mock にフォールバックする。
"""
from __future__ import annotations

from .base import (  # noqa: F401
    REGISTRY,
    BROWSER_DEFAULT,
    ScriptProvider,
    TTSProvider,
    VisualProvider,
    VideoProvider,
    ThumbnailProvider,
    UploadProvider,
    any_engine_needs_browser,
    engine_needs_browser,
    get_provider,
    register,
    write_wav,
)

# 実装の登録（import 副作用で REGISTRY に入る）
from . import mock           # noqa: F401,E402
from . import script_claude  # noqa: F401,E402
from . import script_openai  # noqa: F401,E402
from . import script_gemini  # noqa: F401,E402
from . import script_web     # noqa: F401,E402
from . import tts_google     # noqa: F401,E402
from . import tts_gemini     # noqa: F401,E402
from . import tts_edge       # noqa: F401,E402
from . import tts_fish       # noqa: F401,E402
from . import tts_local      # noqa: F401,E402
from . import visual_openai  # noqa: F401,E402
from . import visual_gemini  # noqa: F401,E402
from . import visual_web     # noqa: F401,E402
from . import video_web      # noqa: F401,E402
from . import video_api      # noqa: F401,E402
from . import thumb          # noqa: F401,E402
from . import upload_youtube  # noqa: F401,E402
from . import desktop        # noqa: F401,E402  デスクトップアプリ連携（Windows）
