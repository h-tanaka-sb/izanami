# -*- coding: utf-8 -*-
"""オフライン疎通確認（API課金なし）。

全ステージを mock エンジンで実行し、テロップ焼き込み済み final.mp4 が出力できるか、
そして再開（2回目はスキップ）が効くかを検証する。

  uv run --python 3.12 python _smoke_test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

# コンソール(cp932)でも記号付きログを出せるようUTF-8化
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from izanagi import config, pipeline, util  # noqa: E402


def main() -> int:
    cfg = dict(config.DEFAULTS)
    cfg["topic"] = "サーキットで速くなるブレーキング術"
    cfg["output_dir"] = str(ROOT / "output")
    for cap in ("script", "brushup", "tts", "visual", "thumb"):
        cfg[f"{cap}_engine"] = "mock"
    cfg["upload_engine"] = "mock"

    logs: list[str] = []
    def log(m): logs.append(str(m)); print(m, flush=True)

    print("=== 1回目（新規実行） ===")
    base = pipeline.run(cfg, log=log, stop_flag=lambda: False)
    final = Path(base) / "compose" / "final.mp4"
    probe = util.probe_ok(final)
    print("\n--- 検証 ---")
    print("final.mp4 存在:", final.exists())
    print("ffprobe:", probe)

    print("\n=== 2回目（同フォルダを再開＝全スキップ確認） ===")
    base2 = pipeline.run(cfg, log=log, stop_flag=lambda: False, resume_dir=base)
    skipped = sum(1 for line in logs if "スキップ" in line)

    ok = final.exists() and probe.get("ok") and base2 == base and skipped >= 5
    print("\n結果:", "PASS ✅" if ok else "FAIL ❌")
    print("出力:", base)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
