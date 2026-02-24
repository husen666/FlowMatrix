"""
西游记经典画面 AI 视频生成器
-- 调用火山引擎（即梦AI）文生视频 3.0 Pro (10s)
-- edge-tts 中文配音 + ASS 字幕，音视频时长对齐，不循环
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from shared.config import get_settings
from shared.media.video import VideoGenerator
from shared.media.tts import TTSGenerator
from shared.utils.logger import get_logger

logger = get_logger("xiyouji")

# ================================================================
# 西游记五大经典镜头
# frames=241 => ~10 秒视频
# 旁白控制在 ~10 秒朗读量（40-55 个汉字，rate=-5%）
# ================================================================

SCENES = [
    {
        "name": "大闹天宫",
        "prompt": (
            "Epic scene from Journey to the West: The Monkey King Sun Wukong, "
            "wearing golden armor and a phoenix-feather cap, wields a glowing golden staff "
            "in the heavenly palace. He leaps through clouds of gold and crimson, "
            "smashing through celestial guards. Divine lightning crackles around him, "
            "his eyes burn with golden fire. The Jade Emperor's palace crumbles in the background. "
            "Ancient Chinese mythology style, dramatic lighting, sweeping camera movement, "
            "cinematic, 4K, high quality, smooth motion"
        ),
        "voiceover": (
            "齐天大圣孙悟空，身披黄金战甲，手持金箍棒，"
            "一个筋斗翻上九重天！"
            "凌霄宝殿之上，金光四射，天地变色！"
        ),
    },
    {
        "name": "三打白骨精",
        "prompt": (
            "Cinematic scene from Journey to the West: Sun Wukong stands protectively "
            "before the monk Tang Sanzang on a misty mountain path. A beautiful woman "
            "transforms into a white skeleton demon, swirling white bone fragments and ghostly mist. "
            "Sun Wukong raises his golden staff high, golden light radiating from his body. "
            "Dark clouds gather overhead, autumn leaves scatter in the wind. "
            "Chinese ink painting aesthetic meets cinematic realism, "
            "dramatic shadow and light contrast, slow-motion action, "
            "cinematic, 4K, high quality, smooth motion"
        ),
        "voiceover": (
            "荒山迷雾，白骨精三番变化蒙骗唐僧。"
            "火眼金睛看穿一切，金箍棒高举，一棒打碎伪装！"
            "即使被误解，悟空也绝不手软。"
        ),
    },
    {
        "name": "师徒四人取经路",
        "prompt": (
            "Majestic scene from Journey to the West: Four travelers walk along a winding mountain path "
            "at golden hour. The monk Tang Sanzang rides a white dragon horse, "
            "Sun Wukong scouts ahead on a cloud with his golden staff, "
            "Zhu Bajie carries a nine-tooth rake, and Sha Wujing shoulders heavy luggage. "
            "Behind them stretches an endless landscape of misty Chinese mountains, "
            "waterfalls, and ancient pine trees. Warm golden sunlight filters through clouds. "
            "Traditional Chinese landscape painting style, aerial sweeping camera, "
            "cinematic, 4K, high quality, smooth motion"
        ),
        "voiceover": (
            "十万八千里取经路，师徒四人踏遍千山万水。"
            "夕阳洒在古道上，四个身影越走越远，"
            "从未回头。"
        ),
    },
    {
        "name": "火焰山",
        "prompt": (
            "Spectacular scene from Journey to the West: A massive mountain of raging flames "
            "stretches across the horizon, fire reaching into the crimson sky. "
            "Sun Wukong stands on a cliff edge facing the inferno, "
            "holding a giant banana leaf fan that summons powerful winds. "
            "Waves of fire dance and spiral upward, creating fire tornadoes. "
            "The ground cracks with volcanic heat, embers float in the scorching air. "
            "Epic scale, dramatic fire VFX, intense warm color palette, "
            "cinematic, 4K, high quality, smooth motion"
        ),
        "voiceover": (
            "八百里火焰山，烈焰遮天蔽日。"
            "悟空借来芭蕉扇，奋力一挥！"
            "狂风呼啸，火浪退散，挡不住取经人的决心。"
        ),
    },
    {
        "name": "取得真经",
        "prompt": (
            "Glorious finale scene from Journey to the West: The four pilgrims arrive at "
            "the golden Thunder Monastery on Vulture Peak. Radiant Buddha sits on a lotus throne "
            "surrounded by thousands of golden bodhisattvas. "
            "Tang Sanzang kneels reverently receiving glowing sacred scriptures. "
            "Sun Wukong, Zhu Bajie, and Sha Wujing stand behind him. "
            "Golden light beams stream down from heaven, celestial flowers rain from the sky, "
            "ethereal music visualized as flowing golden ribbons. "
            "Sacred, magnificent, divine atmosphere, Chinese Buddhist art style, "
            "cinematic, 4K, high quality, smooth motion"
        ),
        "voiceover": (
            "九九八十一难，师徒四人终登灵山。"
            "金光之中，唐僧跪拜接过真经。"
            "一路磨难，终成正果。西游记，圆满落幕。"
        ),
    },
]

# 全局参数
VIDEO_FRAMES = 241       # ~10 秒视频
VIDEO_RATIO = "16:9"     # 16:9 宽屏


def generate_scene(
    vg: VideoGenerator,
    tts: TTSGenerator,
    scene: dict,
    output_dir: Path,
) -> Optional[Path]:
    """生成单个场景: 10s 视频 + 配音 + 字幕，不循环"""
    name = scene["name"]
    prompt = scene["prompt"]
    voiceover = scene["voiceover"]
    safe_name = name.replace(" ", "_")

    logger.info("=" * 60)
    logger.info("[%s] Start (frames=%d, ratio=%s)", name, VIDEO_FRAMES, VIDEO_RATIO)
    logger.info("[%s] Voiceover (%d chars): %s", name, len(voiceover), voiceover[:60])
    logger.info("=" * 60)

    try:
        # Step 1: 生成 10 秒无声视频
        task_id = vg.submit_task(prompt, aspect_ratio=VIDEO_RATIO, frames=VIDEO_FRAMES)
        logger.info("[%s] Task submitted: %s", name, task_id)

        video_url = vg.poll_result(task_id, max_wait=300, interval=10)
        if not video_url:
            logger.error("[%s] Video generation failed", name)
            return None

        raw_path = output_dir / f"xiyouji_{safe_name}_raw.mp4"
        vg.download(video_url, raw_path)

        # Step 2: 配音 + 字幕，不循环视频
        logger.info("[%s] Adding voiceover (no loop)...", name)
        final_path = tts.add_voiceover(
            video_path=raw_path,
            title=f"Xi You Ji - {name}",
            body=voiceover,
            save_dir=output_dir,
            filename_prefix=f"xiyouji_{safe_name}",
            custom_script=voiceover,
            with_subtitle=True,
            loop_video=False,           # <-- 不循环，以视频时长为准
        )

        if final_path and final_path.exists():
            size_mb = final_path.stat().st_size / 1024 / 1024
            logger.info("[%s] Done: %s (%.1fMB)", name, final_path, size_mb)
            return final_path
        else:
            logger.warning("[%s] Voiceover merge failed, returning raw", name)
            return raw_path

    except Exception as e:
        logger.error("[%s] Error: %s", name, e, exc_info=True)
        return None


def main():
    print()
    print("=" * 56)
    print("  Xi You Ji - Journey to the West Video Generator")
    print("  JiMeng AI 10s video + edge-tts voiceover")
    print("=" * 56)
    print()

    settings = get_settings()
    if not settings.volc_ak or not settings.volc_sk:
        logger.error("VOLC_AK / VOLC_SK not configured")
        sys.exit(1)

    vg = VideoGenerator(
        volc_ak=settings.volc_ak,
        volc_sk=settings.volc_sk,
        volc_host=settings.volc_host,
        volc_service=settings.volc_service,
        volc_region=settings.volc_region,
    )

    # YunxiNeural = male voice, epic narration style
    tts = TTSGenerator(
        llm=None,
        voice="zh-CN-YunxiNeural",
        rate="-5%",
    )

    output_dir = Path(settings.output_dir) / "xiyouji"
    output_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Output: %s", output_dir)

    # Scene selection
    print("  Scenes:")
    for i, scene in enumerate(SCENES, 1):
        print(f"    {i}. {scene['name']}")
    print(f"    0. Generate all ({len(SCENES)} scenes)")
    print()

    choice = input("  Select (default=0, all): ").strip()

    if choice == "" or choice == "0":
        selected = SCENES
    else:
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(SCENES):
                selected = [SCENES[idx]]
            else:
                logger.error("Invalid: %s", choice)
                sys.exit(1)
        except ValueError:
            logger.error("Invalid: %s", choice)
            sys.exit(1)

    print(f"\n  Generating {len(selected)} scene(s)...\n")

    results = []
    total = len(selected)
    t0 = time.time()

    for i, scene in enumerate(selected, 1):
        print(f"\n  [{i}/{total}] {scene['name']}")
        print("  " + "-" * 50)
        path = generate_scene(vg, tts, scene, output_dir)
        results.append({"name": scene["name"], "path": path})
        if i < total:
            time.sleep(5)

    elapsed = int(time.time() - t0)
    print("\n" + "=" * 60)
    print("  Results")
    print("=" * 60)
    ok = 0
    for r in results:
        s = str(r["path"]) if r["path"] else "FAILED"
        tag = "[OK]  " if r["path"] else "[FAIL]"
        print(f"  {tag} {r['name']} -> {s}")
        if r["path"]:
            ok += 1
    print(f"\n  {ok}/{total} OK | {elapsed // 60}m{elapsed % 60}s | {output_dir}")
    print("=" * 60)


if __name__ == "__main__":
    main()
