"""
天庭选秀大赛 2026 春节特别版 - 恶搞短片
经典西游记 x 2026 春节 x 选秀综艺 = 爆款短视频

6 个分镜 -> 逐镜头配音+字幕 -> 拼接 ~60s -> 全平台发布

用法:
  python xiyouji_havoc.py --dry-run    # 预览脚本 + 生成素材 JSON
  python xiyouji_havoc.py              # 生成视频 + 素材
  python xiyouji_havoc.py --publish    # 生成后发布全平台
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from shared.config import get_settings
from shared.media.video import VideoGenerator
from shared.media.tts import TTSGenerator, FFMPEG_EXE
from shared.utils.logger import get_logger

logger = get_logger("havoc")

# ================================================================
#  角色卡 — 影片级人物设定
#  每个角色的外貌、材质、配色严格固定，保证跨镜头一致性
#
#  一致性核心：每人独占一套配色，绝不撞色
#    悟空 = 金铜色皮毛 + 暗金甲 + 深红披风
#    玉帝 = 纯白 + 淡金
#    八戒 = 粉红皮肤 + 银亮片甲 + 亮粉披风  ← 银甲与悟空金甲区分
#    红孩儿 = 火焰红 + 橙色
#    唐僧 = 藏红 + 深紫 + 黑墨镜          ← 与悟空金红区分
# ================================================================

WUKONG = (
    # 色系: 金铜 + 暗金 + 深红 | 轮廓锚点: 凤翼冠、铁棒
    "a muscular anthropomorphic monkey warrior with short sleek golden-bronze fur, "
    "bright amber eyes with vertical slit pupils, pronounced brow ridge, "
    "wearing polished brushed-gold plate armor with raised coiling dragon engravings, "
    "crimson silk sash at the waist, a long flowing deep crimson cape, "
    "a golden phoenix-wing crown with two upswept prongs on his head, "
    "wielding a long dark iron staff banded with glowing golden rings"
)

JADE_EMPEROR = (
    # 色系: 纯白 + 淡金 | 轮廓锚点: 长白胡须、高冠
    "a dignified elderly man with long flowing pure white beard reaching his chest, "
    "calm expression, wearing layered pearl-white and pale-gold silk celestial robes "
    "with silver cloud embroidery, a tall golden imperial crown with hanging jade bead curtains"
)

BAJIE = (
    # 色系: 粉红皮肤 + 镜面银 + 亮粉 | 轮廓锚点: 猪头大肚、银色亮片
    "a large rotund pig-man with smooth pink skin, large floppy ears, "
    "small squinting eyes, wide flat snout, and an enormous protruding round belly, "
    "wearing a tight mirror-silver sequin-covered armor suit that barely contains his massive frame, "
    "a flowing hot-pink silk cape, a jeweled belt straining around his waist"
)

RED_BOY = (
    # 色系: 火焰红 + 橙 | 轮廓锚点: 竖立红发、赤膊火焰纹、火焰光环
    "a fierce young boy with wild flame-red spiky hair standing straight up, "
    "intense crimson glowing eyes, lean bare upper body covered in red-orange flame tattoo patterns, "
    "wearing loose red silk pants with a golden sash, "
    "surrounded by a swirling aura of bright orange-red flames"
)

TANG_MONK = (
    # 色系: 藏红 + 深紫 + 黑 | 轮廓锚点: 光头、黑墨镜、珠串
    "a serene bald ancient Chinese scholar with smooth pale skin and refined gentle face, "
    "wearing flowing layered saffron-and-burgundy ceremonial robes with gold lotus patterns, "
    "oversized reflective jet-black sunglasses, a large golden bead necklace"
)

# 选秀舞台环境卡 — 每个镜头共享的场景锚点，保持视觉一致
STAGE_ENV = (
    "a celestial talent show stage floating among luminous white clouds, "
    "massive circular platform of polished gold with ornate edge carvings, "
    "tall jade-green stone pillars wrapped in crimson silk ribbons, "
    "floating glowing red paper lanterns, golden spotlights on rotating towers, "
    "cloud-shaped audience seats filled with celestial beings in colorful silk robes"
)

# ================================================================
#  全局风格 — 影片级视觉标准
#  关键词：电影镜头语言 + 色彩分级 + 光影细节 + 材质渲染
# ================================================================

STYLE_SUFFIX = (
    # 色彩与光影
    "cinematic color grading, golden-teal palette, deep shadows, film grain, "
    "volumetric god rays, rim lighting on characters, atmospheric haze, "
    "anamorphic bokeh, subtle lens flare, "
    # 材质渲染 — 精致度关键（SSS + 金属高光 + 丝绸纹理）
    "photorealistic materials, subsurface scattering on skin, "
    "metallic specular on armor, visible silk fabric folds, floating particles, "
    # 禁止文字 — 关键！
    "absolutely no text, no words, no letters, no numbers, no signs, no watermarks, "
    # 技术参数
    "8K, 24fps smooth cinematic motion, "
    "epic Chinese mythology fantasy film, VFX quality"
)

# ================================================================
#  封面图 prompt
# ================================================================

COVER_IMAGE_PROMPT = (
    # 封面图：用简化角色特征避免过长，聚焦视觉冲击力
    "A spectacular celestial talent show poster scene floating among clouds. "
    "Center stage: a golden-furred monkey warrior in crimson cape wielding an iron staff heroically. "
    "Left: a rotund pink pig-man in mirror-silver sequin armor mid-dance. "
    "Right: a fierce flame-haired boy surrounded by dramatic orange fire. "
    "Front: an elderly white-bearded judge in white-gold robes looking shocked. "
    "A giant glowing golden trophy descends from above, "
    "fireworks and golden confetti exploding across the sky, "
    "polished gold stage, jade pillars, floating red lanterns, "
    "volumetric spotlights, anamorphic lens flare, "
    "subsurface scattering on skin, metallic specular on armor, "
    "absolutely no text, no words, no letters, "
    "epic cinematic poster composition, 16:9, 8K, feature film VFX quality"
)

# ================================================================
#  分镜脚本 — 天庭选秀大赛 2026 春节版 (影片级)
#
#  导演理念：
#  - 镜头节奏遵循 宏大建立 → 喜剧表演 → 震撼群舞
#    → 火爆灾难 → 魔性催眠 → 终幕大翻车 的叙事弧
#  - 每个镜头指定: 机位运动、光源方向、景深、氛围粒子
#  - 色彩情绪: 华丽金红(1) → 闪亮粉金(2) → 热烈金(3)
#    → 火红灾难(4) → 冷静蓝紫(5) → 混乱金(6)
#
#  看点/噱头：
#  1. 天宫搞选秀，评委比选手还紧张 (反差)
#  2. 八戒穿亮片战袍热舞把舞台跳塌 (视觉冲击)
#  3. 悟空七十二变百人群舞 (震撼名场面)
#  4. 红孩儿喷火烧了评委席 (爆笑意外)
#  5. 唐僧念经 rap 催眠全场 (魔性反差)
#  6. 巨型奖杯砸穿舞台全员升天 (终极翻车)
# ================================================================

SHOTS = [
    {
        "id": 1,
        "title": "华丽开场",
        "prompt": (
            # 镜头: 极远景crane俯冲, 深景深, 金红主调
            # 要点: 建立宏大场景，只需舞台+玉帝评委
            f"{STAGE_ENV}. "
            f"{JADE_EMPEROR} sits at the judges desk, "
            "white beard over pearl-white robes, jade crown glinting. "
            "Golden spotlights sweep across the vast amphitheater. "
            "Red silk banners flutter, fireworks burst above the clouds. "
            "Crane shot descending from clouds to stage level, "
            "deep focus, warm golden spotlights, cool blue fill, "
            "golden confetti and red petals drifting through the air."
        ),
        "voiceover": "二零二六春节，天庭搞起了选秀大赛，玉帝亲自坐镇评委席，这排场豪华到天都亮了！",
    },
    {
        "id": 2,
        "title": "八戒热舞",
        "prompt": (
            # 镜头: 中景环绕跟拍, 仰拍, 粉银主调
            # 要点: 聚焦八戒，强化银色亮片+粉红皮肤的独特视觉
            f"{BAJIE} center stage on a massive polished gold circular platform. "
            "He strikes a dramatic dance pose with one arm high, then spins wildly, "
            "his mirror-silver sequin armor catching every spotlight "
            "sending dazzling disco-ball reflections across the audience. "
            "Pink skin glistening with sweat, enormous belly bouncing as he breakdance spins. "
            "The golden stage cracks beneath his heavy stomping, "
            "spiderweb fractures spreading rapidly. "
            "Medium tracking shot circling the dancer, "
            "warm magenta and silver spotlights, lens flares from sequins, "
            "golden sparks and debris rising from the cracking stage."
        ),
        "voiceover": "八戒穿上亮片战袍第一个登台，一段热舞抖得天宫地震，舞台直接塌了，评委集体石化！",
    },
    {
        "id": 3,
        "title": "七十二变群舞",
        "prompt": (
            # 镜头: 俯拍全景, 深景深, 金色主调
            # 要点: 聚焦"大量相同猴王"，强化金铜色毛皮+深红披风一致性
            f"On a polished gold circular stage, {WUKONG} multiplies into dozens of identical copies "
            "in a burst of golden smoke. All with matching golden-bronze fur and crimson capes, "
            "performing synchronized formation dance, iron staffs raised in unison, "
            "creating geometric patterns on the stage. "
            "Golden energy trails behind their movements. "
            "Overhead shot looking down at the formation, "
            "deep focus, warm golden light, cool teal backlight, sparkle particles."
        ),
        "voiceover": "悟空不服气，七十二变直接变出一百个分身，整齐划一跳群舞，玉帝看懵了下巴都掉了！",
    },
    {
        "id": 4,
        "title": "三昧真火秀",
        "prompt": (
            # 镜头: 正面中景, 暖红灾难色调
            # 要点: 聚焦红孩儿，强化火焰红发+赤膊纹身+火焰光环
            f"{RED_BOY} on the golden stage performing a dramatic fire-breathing act. "
            "His flame-red spiky hair stands on end, flame tattoo patterns glowing brighter "
            "as he inhales deeply, cheeks puffed, then unleashes a massive torrent of "
            "brilliant orange-red fire from his mouth in a spectacular wide arc. "
            "The enormous fire stream engulfs the judges desk in roaring flames. "
            "An elderly white-bearded judge scrambles backward, beard singed. "
            "Medium frontal shot, intense orange-red firelight, dark smoke billowing, "
            "embers filling the air, dramatic rim lighting on his bare tattooed silhouette."
        ),
        "voiceover": "红孩儿上台表演吞火秀，三昧真火一口喷出来，好家伙，评委席烧着了，玉帝眉毛都没了！",
    },
    {
        "id": 5,
        "title": "唐僧说唱",
        "prompt": (
            # 镜头: 中近景慢推, 冷紫蓝催眠氛围
            # 要点: 聚焦唐僧，强化光头+黑墨镜+藏红长袍的独特造型
            # 注意: 避免 Buddhist/sutra 等宗教词汇，触发内容审核
            f"{TANG_MONK} on stage with cool swagger, "
            "jet-black sunglasses perfectly reflecting purple spotlights, "
            "saffron-and-burgundy ceremonial robes flowing elegantly. "
            "He holds a golden microphone, bobbing his smooth bald head rhythmically, "
            "one hand making hip-hop gestures while singing in a hypnotic cadence. "
            "Hypnotic purple and blue concentric waves ripple outward from him. "
            "Every audience member has fallen completely asleep, slumped in cloud seats. "
            "Slow dolly-in from medium to close-up on his stoic face, "
            "cool purple-blue lighting, soft bokeh, concentric light rings radiating."
        ),
        "voiceover": "唐僧压轴登场戴着墨镜打节拍，一开口念经说唱，魔性催眠技能拉满，观众全睡着了！",
    },
    {
        "id": 6,
        "title": "颁奖翻车",
        "prompt": (
            # 镜头: 仰拍 → 极远景, 混乱金色
            # 要点: 聚焦巨型奖杯 + 舞台碎裂 + 角色剪影
            "A colossal golden trophy the size of a mountain descends from a rift in the heavens, "
            "surface covered in dragon-and-phoenix engravings, radiating blinding golden light. "
            "It crashes through the polished gold stage platform with tremendous impact, "
            "shattering it into thousands of golden fragments. "
            "Several colorful mythological warriors — a golden-furred monkey in crimson cape, "
            "a pink pig-man in silver sequins, a flame-haired boy, a bald scholar in dark robes — "
            "are sent flying upward in slow motion among golden debris, arms flailing. "
            "Extreme low-angle, brilliant golden light, volumetric god rays, "
            "golden fragments, red confetti and petals in slow motion."
        ),
        "voiceover": "终于到颁奖环节，超大金杯从天而降，太重了直接砸穿舞台，全体选手和评委原地升天！",
    },
]

FULL_VOICEOVER = "".join(s["voiceover"] for s in SHOTS)

VIDEO_FRAMES = 241       # 即梦 AI 24fps x 10秒 ≈ 241帧
VIDEO_RATIO = "16:9"
SPEECH_RATE_CPS = 3.8    # 中文旁白语速 (字/秒), 用于预估时长

# ================================================================
#  各平台发布素材
# ================================================================

PLATFORM_CONTENT = {
    "xhs": {
        "title": "天庭选秀翻车现场笑死我了哈哈哈",
        "body": (
            "用AI拍了个西游记恶搞短片\n"
            "天庭搞了个选秀大赛\n\n"
            "八戒穿亮片战袍热舞 舞台直接塌了\n"
            "悟空七十二变 变出百人舞团\n"
            "红孩儿喷火把评委席烧了\n"
            "唐僧念经rap催眠全场\n"
            "最后颁奖 奖杯太大砸穿舞台 全员升天\n\n"
            "笑死 你们觉得谁的表演最炸裂"
        ),
        "hashtags": [
            "西游记恶搞", "2026春节", "天庭选秀", "AI视频",
            "孙悟空", "春节快乐", "搞笑视频", "国风",
        ],
    },

    "douyin": {
        "title": "天庭选秀名场面 八戒热舞舞台塌了",
        "body": (
            "天庭搞选秀大赛\n"
            "八戒热舞把舞台跳塌\n"
            "悟空变出百人群舞\n"
            "红孩儿喷火烧了评委席\n"
            "唐僧念经rap全场秒睡\n"
            "你说离不离谱"
        ),
        "hashtags": [
            "西游记", "天庭选秀", "2026春节", "AI视频",
            "搞笑", "孙悟空", "春节",
        ],
    },

    "channels": {
        "title": "天庭选秀大赛2026春节恶搞版",
        "body": (
            "如果天庭搞选秀大赛会怎样？\n\n"
            "玉帝亲自当评委\n"
            "八戒穿亮片战袍第一个上 热舞把舞台跳裂了\n"
            "悟空七十二变 变出一百个分身跳群舞\n"
            "红孩儿表演吞火 三昧真火把评委席烧了\n"
            "唐僧戴墨镜压轴 念经rap催眠全场\n"
            "最后颁奖 超大金杯砸穿舞台 全体原地升天\n\n"
            "哈哈哈 你们觉得谁能拿冠军"
        ),
        "hashtags": [
            "西游记", "2026春节", "搞笑视频", "AI视频", "天庭选秀",
        ],
    },

    "zhihu": {
        "title": "如果天庭搞选秀大赛，会是什么名场面？一次AI视频创作实验",
        "body": (
            "## 创意起点\n\n"
            "春节期间做了一个实验：如果天庭举办才艺选秀大赛会怎样？"
            "用即梦AI文生视频，生成了一个 60 秒的恶搞短片。\n\n"
            "## 核心创意\n\n"
            "把经典西游角色放进「选秀综艺」的框架里，制造反差笑点：\n"
            "- 八戒穿亮片战袍热舞 → 舞台直接被跳塌\n"
            "- 悟空七十二变 → 变出百人舞团整齐划一\n"
            "- 红孩儿表演吞火 → 三昧真火烧了评委席\n"
            "- 唐僧戴墨镜念经 rap → 催眠全场观众\n"
            "- 颁奖典礼 → 巨型奖杯砸穿舞台，全员升天\n\n"
            "每个镜头都是「经典角色技能 + 选秀翻车」的组合，"
            "既有画面冲击力，又有密集笑点。\n\n"
            "## 技术方案\n\n"
            "- **视频生成**: 即梦AI 3.0 Pro（6 个 10 秒镜头）\n"
            "- **风格**: 古风 x 华丽选秀舞台\n"
            "- **语音**: edge-tts 男声旁白（吐槽风）\n"
            "- **后期**: ffmpeg 拼接 + ASS 字幕烧录\n\n"
            "## Prompt 工程\n\n"
            "关键是统一「天宫选秀舞台」的视觉语言，"
            "用 golden circular stage + floating lanterns + cloud seats 等描述词，"
            "让 6 个镜头的场景保持一致。角色用详细的外貌卡固定形象。\n\n"
            "## 效果\n\n"
            "AI 在大场景（百人群舞、火焰特效、舞台崩塌）表现很惊艳，"
            "角色表情和动态细节还有提升空间。"
            "但作为短视频，节奏快、笑点密、画面华丽，传播效果很好。\n\n"
            "有兴趣的可以评论区交流创作心得。"
        ),
        "tags": ["AI视频", "西游记", "春节", "AIGC", "创意视频"],
    },

    "toutiao": {
        "title": "AI恶搞：天庭选秀大赛2026春节特别版",
        "body": (
            "用AI技术做了一个西游记恶搞短片——天庭选秀大赛。\n\n"
            "创意是这样的：天庭在2026春节搞了个才艺选秀大赛，"
            "玉帝亲自当评委。\n\n"
            "八戒穿亮片战袍第一个上台热舞，太重了直接把舞台跳裂。"
            "悟空不甘示弱，七十二变变出一百个分身跳群舞，评委看懵了。\n\n"
            "红孩儿表演吞火秀，三昧真火一口喷出来，把评委席烧着了，"
            "玉帝眉毛都没了。唐僧戴墨镜压轴，念经说唱催眠全场。\n\n"
            "最搞笑的是结局——颁奖时超大金杯从天而降，"
            "太重了砸穿舞台，全体选手和评委原地升天。\n\n"
            "全片60秒，6个AI生成镜头，纯文字驱动，从脚本到成片全自动。\n\n"
            "你觉得谁的表演最炸裂？"
        ),
        "tags": ["AI视频", "西游记", "春节", "搞笑", "人工智能"],
    },

    "weibo": {
        "title": "天庭选秀大赛2026春节恶搞版",
        "body": (
            "用AI做了个西游记恶搞短片——天庭搞选秀大赛会怎样？\n\n"
            "八戒穿亮片战袍热舞，舞台直接被跳塌。\n"
            "悟空七十二变，百人群舞整齐划一，评委看懵了。\n"
            "红孩儿喷三昧真火表演吞火，把评委席烧着了。\n"
            "唐僧戴墨镜压轴，念经rap催眠全场。\n\n"
            "最后颁奖——超大金杯砸穿舞台，"
            "全体选手和评委原地升天。\n\n"
            "60秒6个镜头，从脚本到成片全AI生成，你打几分？"
        ),
        "hashtags": [
            "AI视频", "西游记", "天庭选秀", "2026春节",
            "搞笑视频", "AIGC", "孙悟空",
        ],
    },
}


# ================================================================
#  素材生成 / 更新
# ================================================================

def save_content_json(output_dir: Path, video_path: Optional[str] = None):
    """生成各平台发布素材 JSON"""
    output_dir.mkdir(parents=True, exist_ok=True)

    master = {
        "project": "天庭选秀大赛 2026 春节恶搞版",
        "video_path": video_path or "(pending)",
        "duration_sec": len(SHOTS) * (VIDEO_FRAMES // 24),
        "shots": len(SHOTS),
        "cover_image_prompt": COVER_IMAGE_PROMPT,
        "full_voiceover": FULL_VOICEOVER,
        "storyboard": [
            {
                "shot": s["id"],
                "title": s["title"],
                "prompt": s["prompt"] + " " + STYLE_SUFFIX,
                "voiceover": s["voiceover"],
            }
            for s in SHOTS
        ],
    }
    _write_json(output_dir / "master.json", master)

    # 小红书
    xhs = PLATFORM_CONTENT["xhs"]
    _write_json(output_dir / "xhs_content.json", {
        "platform": "xiaohongshu", "type": "video_note",
        "video_path": video_path or "(pending)",
        "title": xhs["title"], "body": xhs["body"], "hashtags": xhs["hashtags"],
        "full_text": xhs["body"] + "\n\n" + " ".join(f"#{t}" for t in xhs["hashtags"]),
    })

    # 抖音
    dy = PLATFORM_CONTENT["douyin"]
    _write_json(output_dir / "dy_content.json", {
        "platform": "douyin", "type": "video",
        "video_path": video_path or "(pending)",
        "title": dy["title"], "body": dy["body"], "hashtags": dy["hashtags"],
        "full_text": dy["body"] + "\n\n" + " ".join(f"#{t}" for t in dy["hashtags"]),
    })

    # 视频号
    ch = PLATFORM_CONTENT["channels"]
    _write_json(output_dir / "channels_content.json", {
        "platform": "channels", "type": "video",
        "video_path": video_path or "(pending)",
        "title": ch.get("title", ""),
        "body": ch["body"], "hashtags": ch["hashtags"],
        "full_text": ch["body"] + "\n\n" + " ".join(f"#{t}" for t in ch["hashtags"]),
    })

    # 知乎（视频）
    zh = PLATFORM_CONTENT["zhihu"]
    _write_json(output_dir / "zh_content.json", {
        "platform": "zhihu", "type": "video",
        "video_path": video_path or "(pending)",
        "title": zh["title"], "body": zh["body"], "tags": zh["tags"],
    })

    # 头条（视频）
    tt = PLATFORM_CONTENT["toutiao"]
    _write_json(output_dir / "toutiao_content.json", {
        "platform": "toutiao", "type": "video",
        "video_path": video_path or "(pending)",
        "title": tt["title"], "body": tt["body"], "tags": tt["tags"],
    })

    # 微博（视频）
    wb = PLATFORM_CONTENT["weibo"]
    _write_json(output_dir / "weibo_content.json", {
        "platform": "weibo", "type": "video",
        "video_path": video_path or "(pending)",
        "title": wb["title"], "body": wb["body"], "hashtags": wb["hashtags"],
        "full_text": wb["body"] + "\n\n" + " ".join(f"#{t}" for t in wb["hashtags"]),
    })

    logger.info("已生成 %d 个平台素材文件", len(PLATFORM_CONTENT))


def update_video_path(output_dir: Path, video_path: str):
    """更新所有 JSON 素材文件中的视频路径"""
    for name in ["master.json", "xhs_content.json", "dy_content.json",
                  "channels_content.json", "zh_content.json",
                  "toutiao_content.json", "weibo_content.json"]:
        fp = output_dir / name
        if fp.exists():
            data = json.loads(fp.read_text(encoding="utf-8"))
            data["video_path"] = video_path
            _write_json(fp, data)
    logger.info("已更新 JSON 文件中的视频路径")


def _write_json(path: Path, data: dict):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("  -> %s", path.name)


# ================================================================
#  预览
# ================================================================

def preview(output_dir: Path):
    """预览脚本内容并生成素材 JSON"""
    logger.info("")
    logger.info("=" * 62)
    logger.info("  天庭选秀大赛 2026 春节恶搞版 - 脚本预览")
    logger.info("  %d 个镜头 x %ds = ~%ds", len(SHOTS), VIDEO_FRAMES // 24, len(SHOTS) * (VIDEO_FRAMES // 24))
    logger.info("=" * 62)

    for s in SHOTS:
        logger.info("")
        logger.info("  镜头 %d: %s", s["id"], s["title"])
        logger.info("    旁白 (%d字): %s", len(s["voiceover"]), s["voiceover"])

    est_sec = len(FULL_VOICEOVER) / SPEECH_RATE_CPS
    video_sec = len(SHOTS) * (VIDEO_FRAMES // 24)
    match_status = "匹配" if abs(est_sec - video_sec) < 5 else "需调整"
    logger.info("")
    logger.info("  旁白: %d字 约%.0f秒 | 视频: %ds | %s", len(FULL_VOICEOVER), est_sec, video_sec, match_status)

    logger.info("")
    logger.info("  看点/噱头:")
    logger.info("    1. 天宫搞选秀，评委比选手还紧张 (反差)")
    logger.info("    2. 八戒亮片战袍热舞把舞台跳塌 (视觉冲击)")
    logger.info("    3. 悟空七十二变百人群舞 (震撼名场面)")
    logger.info("    4. 红孩儿喷火烧了评委席 (爆笑意外)")
    logger.info("    5. 唐僧念经 rap 催眠全场 (魔性反差)")
    logger.info("    6. 巨型奖杯砸穿舞台全员升天 (终极翻车)")

    logger.info("")
    logger.info("  各平台标题:")
    for k, v in PLATFORM_CONTENT.items():
        title = v.get("title", v.get("body", "")[:30])
        logger.info("    %-10s %s", k, title)

    save_content_json(output_dir)
    logger.info("")
    logger.info("  素材已保存到: %s", output_dir)
    logger.info("  执行 python xiyouji_havoc.py 开始生成视频")
    logger.info("=" * 62)


# ================================================================
#  视频生成 (断点续传)
# ================================================================

def generate_shots(vg: VideoGenerator, output_dir: Path) -> list[Path]:
    """逐个生成分镜视频片段，支持断点续传"""
    paths = []
    total = len(SHOTS)
    for s in SHOTS:
        save_path = output_dir / f"shot{s['id']}_{s['title']}.mp4"

        if save_path.exists() and save_path.stat().st_size > 100_000:
            logger.info("镜头 %d/%d [%s] 已存在，跳过", s["id"], total, s["title"])
            paths.append(save_path)
            continue

        full_prompt = s["prompt"] + " " + STYLE_SUFFIX
        logger.info("镜头 %d/%d [%s] 提交生成...", s["id"], total, s["title"])

        try:
            task_id = vg.submit_task(full_prompt, aspect_ratio=VIDEO_RATIO, frames=VIDEO_FRAMES)
            # 10s 视频生成时间更长，超时设为 8 分钟
            video_url = vg.poll_result(task_id, max_wait=480, interval=10)
            if not video_url:
                logger.error("镜头 %d [%s] 生成失败", s["id"], s["title"])
                return paths
            vg.download(video_url, save_path)
            paths.append(save_path)
            logger.info("镜头 %d/%d [%s] 生成完成", s["id"], total, s["title"])
            if s["id"] < total:
                time.sleep(3)
        except Exception as e:
            logger.error("镜头 %d [%s] 异常: %s", s["id"], s["title"], e)
            return paths
    return paths


def narrate_shots(tts: TTSGenerator, silent_clips: list[Path],
                   output_dir: Path) -> list[Path]:
    """
    为每个分镜头独立配音+烧录字幕，确保音画精准同步。

    流程（每个镜头）：
      1. TTS 合成该镜头的旁白 → 音频 + ASS 字幕
      2. ffmpeg 将音频+字幕合并到该镜头的无声视频
      3. 输出带配音的片段

    Returns:
        带配音的视频片段路径列表
    """
    dubbed_clips: list[Path] = []
    total = len(SHOTS)

    # 检测视频分辨率（所有镜头相同，只检测一次）
    vid_w, vid_h = tts.get_video_resolution(str(silent_clips[0]))

    for i, (shot, silent_path) in enumerate(zip(SHOTS, silent_clips)):
        dubbed_path = output_dir / f"shot{shot['id']}_{shot['title']}_dubbed.mp4"

        # 断点续传：已配音的跳过
        if dubbed_path.exists() and dubbed_path.stat().st_size > 100_000:
            logger.info("镜头 %d/%d [%s] 配音已存在，跳过", shot["id"], total, shot["title"])
            dubbed_clips.append(dubbed_path)
            continue

        logger.info("镜头 %d/%d [%s] 配音中...", shot["id"], total, shot["title"])
        voiceover = shot["voiceover"]

        # 1) TTS 合成音频 + 字幕
        audio_path = output_dir / f"shot{shot['id']}_audio.mp3"
        sub_path = output_dir / f"shot{shot['id']}_sub.ass"
        audio_path, sub_file = tts.synthesize(
            voiceover, audio_path, sub_path,
            video_width=vid_w, video_height=vid_h,
        )

        # 2) 合并音视频+字幕（不循环，以较短者为准）
        tts.merge(
            video_path=silent_path,
            audio_path=audio_path,
            output_path=dubbed_path,
            subtitle_path=sub_file,
            loop_video=False,
        )

        # 验证输出
        dubbed_dur = tts.get_duration(str(dubbed_path))
        audio_dur = tts.get_duration(str(audio_path))
        video_dur = tts.get_duration(str(silent_path))
        logger.info(
            "  镜头 %d 同步: 视频=%.1fs 音频=%.1fs 输出=%.1fs",
            shot["id"], video_dur, audio_dur, dubbed_dur,
        )
        dubbed_clips.append(dubbed_path)

    return dubbed_clips


def concat_videos(clip_paths: list[Path], output_path: Path) -> Path:
    """使用 ffmpeg concat 拼接多个视频片段（已含音频+字幕的片段）"""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # 用 ffmpeg 逐个输入+filter_complex concat（避免 concat list 中文路径问题）
    cmd = [FFMPEG_EXE]
    for p in clip_paths:
        cmd += ["-i", str(p)]
    n = len(clip_paths)
    # filter_complex: concat 所有流
    filter_str = "".join(f"[{i}:v:0][{i}:a:0]" for i in range(n))
    filter_str += f"concat=n={n}:v=1:a=1[outv][outa]"
    cmd += [
        "-filter_complex", filter_str,
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k",
        "-y", str(output_path),
    ]
    logger.info("拼接 %d 个片段...", n)
    result = subprocess.run(cmd, capture_output=True, text=False, timeout=300)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        logger.error("拼接失败: %s", stderr[-500:])
        raise RuntimeError(f"视频拼接失败: {stderr[-200:]}")
    logger.info("视频拼接完成: %s", output_path.name)
    return output_path


# ================================================================
#  发布
# ================================================================

def _get_platform_config():
    """统一的平台发布配置（单一来源，避免重复定义）"""
    from xiaohongshu.publisher import publish_video_note
    from douyin.publisher import publish_douyin_video
    from channels.publisher import publish_channels_video
    from zhihu.publisher import publish_zhihu_video
    from toutiao.publisher import publish_toutiao_video
    from weibo.publisher import publish_weibo_video

    return [
        ("XiaoHongShu", "xhs_content.json",
         lambda d, vp: publish_video_note(vp, d["title"], d["full_text"], headless=False)),
        ("Douyin", "dy_content.json",
         lambda d, vp: publish_douyin_video(vp, d["title"], d["full_text"], headless=False)),
        ("Channels", "channels_content.json",
         lambda d, vp: publish_channels_video(vp, d["full_text"], title=d.get("title", ""), headless=False)),
        ("Zhihu", "zh_content.json",
         lambda d, vp: publish_zhihu_video(vp, d["title"], d["body"][:500], headless=False)),
        ("Toutiao", "toutiao_content.json",
         lambda d, vp: publish_toutiao_video(vp, d["title"], d["body"][:500], headless=False)),
        ("Weibo", "weibo_content.json",
         lambda d, vp: publish_weibo_video(vp, d["title"], d["full_text"], headless=False)),
    ]


def _run_publish(output_dir: Path, video_path: str, platforms: list,
                 results: dict) -> dict:
    """
    执行发布流程的核心逻辑（publish_all / publish_failed 共用）。
    platforms: [(平台名, 素材文件, 发布函数), ...]
    results:   已有结果 dict，会原地更新
    """
    for platform, loader, publisher in platforms:
        content_file = output_dir / loader
        if not content_file.exists():
            results[platform] = "SKIP (无素材文件)"
            continue
        try:
            data = json.loads(content_file.read_text(encoding="utf-8"))
            logger.info("正在发布到 %s ...", platform)
            ok = publisher(data, video_path)
            if ok:
                results[platform] = "OK"
                logger.info("%s 发布成功", platform)
            else:
                results[platform] = "UNCERTAIN"
                logger.warning("%s 发布结果待确认", platform)
        except Exception as e:
            results[platform] = f"FAIL: {e}"
            logger.error("%s 发布失败: %s", platform, e)
        time.sleep(5)

    # 保存结果
    _write_json(output_dir / "publish_results.json", results)
    return results


def _print_results(results: dict, title: str = "发布结果"):
    """打印发布汇总报告"""
    logger.info("")
    logger.info("=" * 50)
    logger.info("  %s", title)
    logger.info("=" * 50)
    for platform, status in results.items():
        icon = "[OK]" if status == "OK" else "[??]" if status == "UNCERTAIN" else "[!!]"
        logger.info("  %s %s: %s", icon, platform, status)
    ok_count = sum(1 for s in results.values() if s == "OK")
    logger.info("  %d/%d 个平台发布成功", ok_count, len(results))
    logger.info("=" * 50)


def publish_all(output_dir: Path, video_path: str):
    """发布到所有平台"""
    logger.info("开始发布到全部平台...")
    results = _run_publish(output_dir, video_path, _get_platform_config(), {})
    _print_results(results, "全量发布结果")
    return results


def publish_failed(output_dir: Path, video_path: str):
    """读取上次发布结果，只重发失败的平台"""
    result_file = output_dir / "publish_results.json"
    if not result_file.exists():
        logger.warning("无上次发布记录，将重新发布全部平台")
        return publish_all(output_dir, video_path)

    last_results = json.loads(result_file.read_text(encoding="utf-8"))
    failed = {k: v for k, v in last_results.items() if v != "OK"}
    if not failed:
        logger.info("所有平台均已发布成功，无需重试")
        return last_results

    logger.info("重新发布 %d 个失败平台: %s", len(failed), ", ".join(failed.keys()))

    # 只保留失败平台的配置
    retry_platforms = [
        (name, loader, fn) for name, loader, fn in _get_platform_config()
        if name in failed
    ]
    results = dict(last_results)  # 保留之前成功的
    _run_publish(output_dir, video_path, retry_platforms, results)
    _print_results(results, "重试发布结果")
    return results


# ================================================================
#  主入口
# ================================================================

def main():
    """CLI 主入口"""
    parser = argparse.ArgumentParser(description="天庭选秀大赛 2026 春节恶搞版")
    parser.add_argument("--dry-run", action="store_true", help="预览脚本 + 仅生成素材 JSON")
    parser.add_argument("--publish", action="store_true", help="生成视频后自动发布全平台")
    parser.add_argument("--publish-only", action="store_true", help="直接发布已有视频（不重新生成）")
    parser.add_argument("--retry-failed", action="store_true", help="仅重试上次发布失败的平台")
    args = parser.parse_args()

    settings = get_settings()
    output_dir = Path(settings.output_dir) / "xiyouji" / "talent_show"
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        preview(output_dir)
        return

    if args.retry_failed:
        final = output_dir / "talent_show_final.mp4"
        if not final.exists():
            logger.error("视频不存在: %s", final)
            sys.exit(1)
        publish_failed(output_dir, str(final))
        return

    if args.publish_only:
        # 直接发布已有视频
        final = output_dir / "talent_show_final.mp4"
        if not final.exists():
            logger.error("视频不存在: %s", final)
            sys.exit(1)
        publish_all(output_dir, str(final))
        return

    if not settings.volc_ak or not settings.volc_sk:
        logger.error("请先配置 VOLC_AK / VOLC_SK 环境变量")
        sys.exit(1)

    logger.info("")
    logger.info("=" * 56)
    logger.info("  天庭选秀大赛 2026 春节恶搞版")
    logger.info("  生成 -> 逐镜头配音 -> 拼接 -> 发布")
    logger.info("=" * 56)

    vg = VideoGenerator(
        volc_ak=settings.volc_ak, volc_sk=settings.volc_sk,
        volc_host=settings.volc_host, volc_service=settings.volc_service,
        volc_region=settings.volc_region,
    )
    tts = TTSGenerator(llm=None, voice="zh-CN-YunxiNeural", rate="+0%")

    t0 = time.time()

    logger.info("  步骤 0/4: 生成素材 JSON...")
    save_content_json(output_dir)

    logger.info("  步骤 1/4: 生成分镜无声视频...")
    silent_clips = generate_shots(vg, output_dir)
    if len(silent_clips) < len(SHOTS):
        logger.error("  %d/%d 已完成，请重新运行以续传", len(silent_clips), len(SHOTS))
        sys.exit(1)

    logger.info("  步骤 2/4: 逐镜头配音+字幕（确保音画同步）...")
    dubbed_clips = narrate_shots(tts, silent_clips, output_dir)
    if len(dubbed_clips) < len(SHOTS):
        logger.error("  配音未全部完成，请重新运行以续传")
        sys.exit(1)

    logger.info("  步骤 3/4: 拼接所有配音片段...")
    final = output_dir / "talent_show_final.mp4"
    concat_videos(dubbed_clips, final)
    update_video_path(output_dir, str(final))

    elapsed = int(time.time() - t0)
    logger.info("")
    logger.info("=" * 60)
    logger.info("  生成完成!")
    logger.info("  视频: %s", final)
    logger.info("  大小: %.1f MB", final.stat().st_size / 1024 / 1024)
    logger.info("  耗时: %dm%ds", elapsed // 60, elapsed % 60)
    logger.info("  素材: %s", output_dir)
    for f in sorted(output_dir.glob("*.json")):
        logger.info("    %s", f.name)
    logger.info("=" * 60)

    if args.publish:
        publish_all(output_dir, str(final))
    else:
        logger.info("  发布命令: python xiyouji_havoc.py --publish")


if __name__ == "__main__":
    main()
