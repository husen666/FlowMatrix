"""
西游记 - 为已有的原始视频添加配音 + 字幕
直接处理 output/xiyouji/ 下已下载的 5 秒视频，无需再调 API
旁白精简到 ~5 秒（约 20-25 字），和视频时长严格匹配
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from shared.config import get_settings
from shared.media.tts import TTSGenerator
from shared.utils.logger import get_logger

logger = get_logger("xiyouji")

OUTPUT_DIR = Path(get_settings().output_dir) / "xiyouji"

# ================================================================
# 5 个场景：对应已有的 raw 视频 + 精简旁白 (~5 秒)
# 每段约 20-25 个汉字，配合 rate=-5% 刚好 ~5 秒
# ================================================================

SCENES = [
    {
        "name": "大闹天宫",
        "raw_file": "xiyouji_大闹天宫.mp4",
        "voiceover": "齐天大圣孙悟空，金箍棒横扫天宫！凌霄宝殿，天地变色！",
    },
    {
        "name": "三打白骨精",
        "raw_file": "xiyouji_三打白骨精.mp4",
        "voiceover": "白骨精三番变化，火眼金睛看穿一切！一棒打碎伪装！",
    },
    {
        "name": "师徒四人取经路",
        "raw_file": "xiyouji_师徒四人取经路.mp4",
        "voiceover": "师徒四人，踏遍千山万水，夕阳古道，从未回头。",
    },
    {
        "name": "火焰山",
        "raw_file": "xiyouji_火焰山.mp4",
        "voiceover": "八百里火焰山，烈焰遮天！芭蕉扇一挥，火海退散！",
    },
    {
        "name": "取得真经",
        "raw_file": "xiyouji_取得真经.mp4",
        "voiceover": "历尽磨难终登灵山，真经在手，西游圆满！",
    },
]


def process_scene(tts: TTSGenerator, scene: dict) -> Path | None:
    """给已有视频添加配音+字幕"""
    name = scene["name"]
    raw_path = OUTPUT_DIR / scene["raw_file"]
    voiceover = scene["voiceover"]
    safe_name = name.replace(" ", "_")

    if not raw_path.exists():
        logger.error("[%s] Raw video not found: %s", name, raw_path)
        return None

    raw_size = raw_path.stat().st_size / 1024 / 1024
    logger.info("[%s] Raw video: %s (%.1fMB)", name, raw_path.name, raw_size)
    logger.info("[%s] Voiceover (%d chars): %s", name, len(voiceover), voiceover)

    try:
        final_path = tts.add_voiceover(
            video_path=raw_path,
            title=name,
            body=voiceover,
            save_dir=OUTPUT_DIR,
            filename_prefix=f"xiyouji_{safe_name}",
            custom_script=voiceover,
            with_subtitle=True,
            loop_video=False,       # 不循环，以视频时长为准
        )
        if final_path and final_path.exists():
            size_mb = final_path.stat().st_size / 1024 / 1024
            logger.info("[%s] Final: %s (%.1fMB)", name, final_path.name, size_mb)
            return final_path
        else:
            logger.error("[%s] Merge failed", name)
            return None
    except Exception as e:
        logger.error("[%s] Error: %s", name, e, exc_info=True)
        return None


def main():
    print()
    print("=" * 56)
    print("  Xi You Ji - Add Voiceover to Existing Videos")
    print("  edge-tts (YunxiNeural) + ASS subtitles")
    print("=" * 56)
    print()

    # Check raw videos exist
    missing = []
    for s in SCENES:
        p = OUTPUT_DIR / s["raw_file"]
        status = "OK" if p.exists() else "MISSING"
        if not p.exists():
            missing.append(s["name"])
        print(f"  [{status}] {s['raw_file']}")
    print()

    if missing:
        logger.error("Missing videos: %s", ", ".join(missing))
        print("  Some raw videos are missing. Only existing ones will be processed.\n")

    available = [s for s in SCENES if (OUTPUT_DIR / s["raw_file"]).exists()]
    if not available:
        logger.error("No raw videos found in %s", OUTPUT_DIR)
        sys.exit(1)

    # TTS: male voice, slightly slow for epic feel
    tts = TTSGenerator(
        llm=None,
        voice="zh-CN-YunxiNeural",
        rate="-5%",
    )

    results = []
    total = len(available)
    t0 = time.time()

    for i, scene in enumerate(available, 1):
        print(f"\n  [{i}/{total}] {scene['name']}")
        print("  " + "-" * 40)
        path = process_scene(tts, scene)
        results.append({"name": scene["name"], "path": path})

    elapsed = int(time.time() - t0)
    print("\n" + "=" * 60)
    print("  Results")
    print("=" * 60)
    ok = 0
    for r in results:
        tag = "[OK]  " if r["path"] else "[FAIL]"
        s = str(r["path"]) if r["path"] else "FAILED"
        print(f"  {tag} {r['name']} -> {s}")
        if r["path"]:
            ok += 1
    print(f"\n  {ok}/{total} OK | {elapsed}s | {OUTPUT_DIR}")
    print("=" * 60)


if __name__ == "__main__":
    main()
