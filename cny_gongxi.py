"""
财神爷的 2026 新春直播间 - 恭喜发财爆笑短片
传统神仙 x 现代直播 x 新春祝福 = 爆款贺岁短视频

创意核心：
  财神爷为了跟上时代潮流，在天宫搞了个直播间给全天下人送祝福，
  结果红包雨失控、摇钱树暴走、门神乱入、锦鲤成精……
  一切都翻车了，但最后依然用最炸裂的方式送出新年祝福！

6 个分镜 -> 逐镜头配音+字幕 -> 拼接 ~60s -> 全平台发布

用法:
  python cny_gongxi.py --dry-run    # 预览脚本 + 生成素材 JSON
  python cny_gongxi.py              # 生成视频 + 素材
  python cny_gongxi.py --publish    # 生成后发布全平台
  python cny_gongxi.py --publish-only  # 直接发布已有视频
  python cny_gongxi.py --retry-failed  # 仅重试上次失败平台
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

logger = get_logger("cny-gongxi")

# ================================================================
#  角色卡 — 影片级人物设定
#  每个角色的外貌、材质、配色严格固定，保证跨镜头一致性
#
#  一致性核心：每人独占一套配色
#    财神 = 大红+纯金 + 金冠 + 元宝
#    门神左(秦琼) = 深红甲 + 黄铜锏
#    门神右(尉迟) = 墨黑甲 + 银鞭
#    锦鲤 = 辉煌金鳞 + 红色鳍 + 珍珠白腹
# ================================================================

GOD_OF_WEALTH = (
    # 色系: 大红 + 纯金 | 轮廓锚点: 金冠、金元宝、大笑脸
    "a jovial rotund elderly Chinese deity with a huge beaming smile, "
    "round rosy cheeks, long flowing white beard, kind crescent-shaped eyes, "
    "wearing magnificent layered crimson-red and pure-gold silk robes "
    "with embroidered golden dragon-and-phoenix patterns, "
    "a tall ornate golden crown studded with rubies and jade, "
    "holding a massive shining golden ingot in one hand, "
    "a golden ruyi scepter in the other hand"
)

DOOR_GOD_LEFT = (
    # 色系: 深红甲 + 黄铜 | 轮廓锚点: 红脸大胡子、铜杖
    "a towering muscular ancient Chinese guardian with a bold red face, "
    "thick black eyebrows, expressive eyes, a magnificent long black beard, "
    "wearing heavy deep-crimson lacquered ceremonial armor with golden trim, "
    "a red festive cape, holding a tall golden ceremonial staff"
)

DOOR_GOD_RIGHT = (
    # 色系: 墨黑甲 + 银色 | 轮廓锚点: 黑脸壮目、银杖
    "a towering muscular ancient Chinese guardian with a dark bronze face, "
    "strong expressive eyes, a prominent jaw, thick sideburns, "
    "wearing heavy matte-black lacquered ceremonial armor with silver trim, "
    "a dark cape, holding a long silver ceremonial staff"
)

GOLDEN_KOI = (
    # 色系: 辉煌金鳞 + 红鳍 + 珍珠白
    "a magnificent giant golden koi fish with dazzling metallic gold scales, "
    "flowing red-and-gold translucent fins like silk ribbons, "
    "pearl-white underbelly, large wise glowing amber eyes, "
    "surrounded by swirling golden light particles and tiny floating coins"
)

# 直播间场景卡 — 每个镜头共享的视觉锚点
STUDIO_ENV = (
    "a magnificent celestial palace transformed into a luxury livestream studio, "
    "floating among luminous golden clouds at sunset, "
    "polished jade floor with golden inlay patterns, "
    "towering red lacquered pillars wrapped in golden dragon sculptures, "
    "massive floating holographic screens showing viewer counts, "
    "professional ring lights with golden halos, modern camera equipment, "
    "red silk banners with golden tassels hanging everywhere, "
    "hundreds of floating red lanterns and golden paper-cut decorations, "
    "red and gold Chinese New Year ornaments"
)

# ================================================================
#  全局风格 — 影片级视觉标准
# ================================================================

STYLE_SUFFIX = (
    "cinematic color grading, warm red-gold festive palette, rich deep shadows, "
    "volumetric god rays through red lanterns, golden rim lighting on characters, "
    "atmospheric haze with floating golden particles, "
    "anamorphic bokeh, warm lens flare from golden surfaces, "
    "photorealistic materials, subsurface scattering on skin, "
    "metallic specular on gold surfaces, visible silk fabric folds, "
    "floating red petals and golden confetti particles, "
    "absolutely no text, no words, no letters, no numbers, no signs, no watermarks, "
    "8K, 24fps smooth cinematic motion, "
    "epic Chinese New Year celebration fantasy film, VFX quality"
)

# ================================================================
#  封面图 prompt
# ================================================================

COVER_IMAGE_PROMPT = (
    "A spectacular Chinese New Year celebration poster scene. "
    "Center: a jovial rotund God of Wealth in crimson-gold robes "
    "holding a giant golden ingot, standing on a jade stage with golden inlays. "
    "Behind him: a massive glowing golden money tree showering gold coins. "
    "Left: a towering red-armored ceremonial guardian with a golden staff. "
    "Right: a magnificent giant golden koi fish leaping through golden clouds. "
    "Thousands of red envelopes and gold coins raining from the sky, "
    "spectacular fireworks in red and gold exploding above, "
    "floating red lanterns, golden dragon decorations, "
    "polished jade floor, crimson pillars with golden dragons, "
    "warm golden volumetric lighting, anamorphic lens flare, "
    "subsurface scattering on skin, metallic specular on gold, "
    "absolutely no text, no words, no letters, "
    "epic cinematic poster composition, 16:9, 8K, feature film VFX quality"
)

# ================================================================
#  分镜脚本 — 财神爷的 2026 新春直播间 (影片级)
#
#  导演理念：
#  - 创意核心: 传统神仙+现代直播=极致反差喜剧
#  - 叙事弧: 华丽开播 → 红包翻车 → 摇钱树暴走
#    → 门神乱入 → 锦鲤成精 → 恭喜发财终极大招
#  - 色彩情绪: 喜庆金红(1) → 红包红(2) → 金币金(3)
#    → 怒红墨黑(4) → 辉煌金(5) → 终极金红(6)
#
#  看点/噱头：
#  1. 财神爷搞直播，手忙脚乱调设备 (反差萌)
#  2. 红包雨功能失控，红包淹没整个直播间 (视觉奇观)
#  3. 摇钱树Demo翻车，金树冲破天宫屋顶 (荒诞喜剧)
#  4. 门神兄弟乱入当助播，太大挤爆直播间 (爆笑冲突)
#  5. 锦鲤祈福环节，锦鲤暴长引发金色海啸 (震撼场面)
#  6. 财神骑巨型锦鲤冲上云霄撒金币送祝福 (华丽收尾)
# ================================================================

SHOTS = [
    {
        "id": 1,
        "title": "财神开播",
        "prompt": (
            f"{STUDIO_ENV}. "
            f"{GOD_OF_WEALTH} sitting on a magnificent golden throne behind a jade desk, "
            "fumbling with a modern smartphone mounted on a golden tripod, "
            "accidentally knocking over a stack of golden ingots that cascade across the desk, "
            "his golden crown tilting comically to one side. "
            "A massive holographic screen behind him flickers to life showing rising viewer numbers. "
            "Professional ring lights with golden halos illuminate his rosy cheerful face. "
            "Red lanterns bob gently in the warm air. "
            "Crane shot descending from ceiling to desk level, "
            "warm golden key light, soft red fill from lanterns, "
            "golden dust particles floating in volumetric light beams."
        ),
        "voiceover": "二零二六新春吉时到！财神爷为了跟上潮流开了个直播间，手机都拿反了，这排面也是没谁了！",
    },
    {
        "id": 2,
        "title": "红包雨失控",
        "prompt": (
            f"Inside {STUDIO_ENV}. "
            f"{GOD_OF_WEALTH} standing up from his golden throne, "
            "waving his golden ruyi scepter dramatically upward, "
            "triggering an enormous avalanche of thousands of bright red envelopes "
            "pouring from a magical rift in the ceiling, "
            "completely burying his jade desk and golden equipment, "
            "red envelopes piling up to his waist, his golden crown barely visible, "
            "struggling with arms waving above the red envelope flood. "
            "Medium wide shot, dynamic camera slightly tilting, "
            "warm red light from the glowing envelopes, golden sparkle particles, "
            "dramatic volumetric red-tinted god rays from above."
        ),
        "voiceover": "第一个福利——红包雨！财神一挥如意，好家伙，红包跟不要钱似的往下砸，直播间都淹了！",
    },
    {
        "id": 3,
        "title": "摇钱树暴走",
        "prompt": (
            f"Inside {STUDIO_ENV}. "
            "A small golden bonsai money tree on the jade desk suddenly grows rapidly to enormous size, "
            "its trunk expanding with bark of solid gleaming gold, "
            "golden branches reaching upward through the ornate palace ceiling opening, "
            "pushing aside jade tiles and red roof beams with magical golden energy, "
            "thousands of shining gold coins cascading down from its branches "
            "like a magnificent golden waterfall. "
            f"{GOD_OF_WEALTH} stumbles backward from his golden throne in amazement, "
            "crimson robes flowing, golden crown sliding to one side, "
            "arms raised in astonishment at the coin rainfall, "
            "his expression of pure comedic surprise and wonder. "
            "Low angle looking up at the magnificent growing tree, "
            "brilliant golden light from coins, warm volumetric rays, "
            "golden leaves and coins filling the air."
        ),
        "voiceover": "接下来展示镇店之宝——摇钱树，结果这树太争气了，直接长穿了天宫的屋顶，金币暴雨倾盆！",
    },
    {
        "id": 4,
        "title": "门神乱入",
        "prompt": (
            f"Inside {STUDIO_ENV} with a giant golden tree trunk growing through the ceiling. "
            f"Two enormous warrior generals stride through the wide palace doors: "
            f"on the left {DOOR_GOD_LEFT}, on the right {DOOR_GOD_RIGHT}. "
            "They are far too large for the room, their helmets touching the ceiling, "
            "their massive armored shoulders bumping into the red pillars, "
            "accidentally tipping over ring lights and camera equipment. "
            "One sits on the jade desk, which collapses under his weight. "
            "The other waves enthusiastically at the floating holographic camera with a big grin. "
            "Wide shot capturing the cramped comedic scene, "
            "mixed red and dark dramatic lighting, golden sparkles from toppled equipment, "
            "golden dust swirling around the enormous cheerful warriors."
        ),
        "voiceover": "门神兄弟不请自来要当助播，一进门直接挤爆直播间，桌子坐碎了，设备全砸了，太能耐了！",
    },
    {
        "id": 5,
        "title": "锦鲤显灵",
        "prompt": (
            "On the celestial livestream stage surrounded by scattered gold coins and red envelopes, "
            "a tiny ornate golden fish bowl sits on a jade pedestal. "
            f"From within, {GOLDEN_KOI} leaps out of the tiny bowl, "
            "growing to colossal size in a magical burst of golden water and light, "
            "its magnificent golden body now enormous, filling the entire palace hall, "
            "its flowing red-gold fins sweeping gracefully through the grand space, "
            "creating a spectacular golden wave that lifts "
            "red envelopes, gold coins, and decorations "
            "into a swirling magical vortex of golden water and treasure. "
            f"{GOD_OF_WEALTH} rides the wave on a golden ingot like a surfboard, laughing joyfully. "
            "Dramatic low angle, brilliant golden light from the koi's scales, "
            "golden water droplets frozen in mid-air, epic volumetric rays."
        ),
        "voiceover": "转运锦鲤环节，小鱼碗里的锦鲤突然暴长成精了！金色海啸掀翻全场，财神踩着元宝冲浪！",
    },
    {
        "id": 6,
        "title": "恭喜发财",
        "prompt": (
            "High above magnificent golden-red sunset clouds, a spectacular aerial celebration scene. "
            f"{GOD_OF_WEALTH} rides joyfully on the back of {GOLDEN_KOI} "
            "gliding through the luminous sky. "
            "He raises his golden ruyi scepter high, sending cascading streams "
            "of golden coins, red envelopes, and sparkling golden blessings "
            "showering down through the clouds gracefully. "
            "Brilliant red and gold festive lights bloom all around them, "
            "golden Chinese dragons made of pure light spiral upward, "
            "thousands of floating red lanterns illuminate the twilight sky. "
            "Sweeping aerial cinematic shot, epic scale, "
            "warm golden-red sunset backlighting, volumetric god rays, "
            "golden particles and red petals filling every inch of sky."
        ),
        "voiceover": "财神骑着巨型锦鲤冲上云霄，漫天金币红包从天而降——恭喜发财，二零二六，大吉大利！",
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
        "title": "财神爷直播间翻车现场笑死我了哈哈哈",
        "body": (
            "用AI做了个新春贺岁恶搞短片\n"
            "财神爷为了跟上潮流搞了个直播间\n\n"
            "手机拿反了开播 红包雨直接淹了直播间\n"
            "摇钱树长穿屋顶 金币暴雨倾盆\n"
            "门神兄弟不请自来挤爆现场\n"
            "锦鲤成精掀起金色海啸\n"
            "最后财神骑着锦鲤上天撒钱\n\n"
            "恭喜发财！新的一年暴富暴美\n"
            "你们觉得哪个场面最炸裂"
        ),
        "hashtags": [
            "恭喜发财", "2026新春", "财神爷", "AI视频",
            "新年快乐", "搞笑视频", "春节", "国风",
        ],
    },

    "douyin": {
        "title": "财神直播间翻车 红包雨淹了现场",
        "body": (
            "财神爷开直播送祝福\n"
            "红包雨把直播间淹了\n"
            "摇钱树长穿天宫屋顶\n"
            "门神兄弟挤爆直播间\n"
            "锦鲤成精掀翻全场\n"
            "恭喜发财 新年暴富"
        ),
        "hashtags": [
            "恭喜发财", "财神爷", "2026新春", "AI视频",
            "搞笑", "新年快乐", "春节",
        ],
    },

    "channels": {
        "title": "财神爷的2026新春直播间恭喜发财",
        "body": (
            "如果财神爷搞直播会怎样？\n\n"
            "新年到了 财神为了跟上时代开了个直播间送祝福\n"
            "结果手机拿反了好不容易开播\n"
            "第一个福利红包雨 如意一挥红包直接淹了整个直播间\n"
            "展示摇钱树 金树暴长冲穿天宫屋顶 金币倾盆而下\n"
            "门神兄弟不请自来当助播 太大了挤爆直播间\n"
            "最绝的是锦鲤祈福 小鱼碗里的锦鲤直接成精掀起金色海啸\n"
            "最后财神骑着巨型锦鲤上天 漫天金币红包从天而降\n\n"
            "恭喜发财 二零二六大吉大利"
        ),
        "hashtags": [
            "恭喜发财", "2026新春", "搞笑视频", "AI视频", "财神爷",
        ],
    },

    "zhihu": {
        "title": "用AI做了个恭喜发财贺岁短片：如果财神爷搞直播会怎样？",
        "body": (
            "## 创意起点\n\n"
            "2026春节做了一个实验——"
            "如果财神爷为了跟上时代潮流开了个直播间送祝福，会发生什么？"
            "用即梦AI文生视频，生成了一个 60 秒的恶搞贺岁短片。\n\n"
            "## 核心创意\n\n"
            "「传统神仙 x 现代直播」的极致反差：\n"
            "- 财神爷手忙脚乱开播 → 手机都拿反了\n"
            "- 红包雨福利 → 如意一挥红包淹没整个直播间\n"
            "- 摇钱树产品展示 → 树暴长冲穿天宫屋顶，金币暴雨\n"
            "- 门神兄弟乱入当助播 → 太大了把直播间挤爆\n"
            "- 锦鲤祈福环节 → 锦鲤成精掀起金色海啸\n"
            "- 终极场面 → 财神骑锦鲤上天撒金币红包\n\n"
            "每个镜头都是「过年传统元素 + 现代翻车」的组合，"
            "既有视觉冲击力，又有密集笑点，最后落回恭喜发财的新年祝福。\n\n"
            "## 技术方案\n\n"
            "- **视频生成**: 即梦AI 3.0 Pro（6 个 10 秒镜头）\n"
            "- **风格**: 新春喜庆 x 现代直播间\n"
            "- **语音**: edge-tts 男声旁白（欢快吐槽风）\n"
            "- **后期**: ffmpeg 拼接 + ASS 字幕烧录\n\n"
            "## Prompt 工程\n\n"
            "关键是统一「天宫直播间」的视觉语言——"
            "红色灯笼、金色装饰、玉石地板、全息屏幕等元素贯穿每个镜头。"
            "财神爷用详细的外貌卡固定形象：大红金袍、金冠、金元宝、如意。\n\n"
            "## 效果\n\n"
            "AI 在大场景（红包雨、金币暴雨、金色海啸）表现很惊艳，"
            "「传统 x 现代」的碰撞制造了天然笑点。"
            "作为贺岁短视频，喜庆、搞笑、祝福三合一，非常适合春节传播。\n\n"
            "恭喜发财，新年快乐！有兴趣的评论区聊。"
        ),
        "tags": ["AI视频", "恭喜发财", "春节", "AIGC", "创意视频"],
    },

    "toutiao": {
        "title": "AI恶搞：财神爷的2026新春直播间",
        "body": (
            "用AI做了一个恭喜发财贺岁短片——"
            "如果财神爷为了跟上时代搞了个直播间送祝福。\n\n"
            "财神在天宫里搭了个豪华直播间，结果手机拿反了才开播。"
            "第一个福利红包雨，如意一挥，红包跟不要钱似的往下砸，"
            "直接把直播间淹了。\n\n"
            "展示镇店之宝摇钱树，结果树太争气了，"
            "暴长冲穿天宫屋顶，金币倾盆而下。\n\n"
            "门神兄弟不请自来要当助播，"
            "太大了把直播间挤爆，桌子坐碎设备全砸。\n\n"
            "最绝的是转运锦鲤环节——小鱼碗里的锦鲤突然成精了，"
            "掀起金色海啸掀翻全场。\n\n"
            "最后财神骑着巨型锦鲤冲上云霄，"
            "漫天金币红包从天而降——恭喜发财，大吉大利！\n\n"
            "全片60秒，6个AI生成镜头，你最喜欢哪个场面？"
        ),
        "tags": ["AI视频", "恭喜发财", "春节", "搞笑", "人工智能"],
    },

    "weibo": {
        "title": "财神爷直播间2026新春恭喜发财版",
        "body": (
            "用AI做了个恭喜发财贺岁短片——财神爷搞直播送祝福\n\n"
            "手机拿反了才开播，红包雨直接淹了直播间\n"
            "摇钱树暴长冲穿屋顶，金币倾盆而下\n"
            "门神兄弟乱入当助播，太大了挤爆现场\n"
            "锦鲤成精掀起金色海啸掀翻全场\n\n"
            "最后财神骑着巨型锦鲤冲上云霄\n"
            "漫天金币红包从天而降\n\n"
            "恭喜发财！二零二六大吉大利\n"
            "60秒6个镜头 全AI生成 你打几分？"
        ),
        "hashtags": [
            "AI视频", "恭喜发财", "财神爷直播", "2026新春",
            "搞笑视频", "AIGC", "新年快乐",
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
        "project": "财神爷的2026新春直播间",
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
    logger.info("  财神爷的 2026 新春直播间 - 脚本预览")
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
    logger.info("    1. 财神爷搞直播，手机拿反了 (反差萌)")
    logger.info("    2. 红包雨失控，直播间被红包淹了 (视觉奇观)")
    logger.info("    3. 摇钱树暴走冲穿天宫屋顶 (荒诞喜剧)")
    logger.info("    4. 门神兄弟乱入挤爆直播间 (爆笑冲突)")
    logger.info("    5. 锦鲤成精掀起金色海啸 (震撼名场面)")
    logger.info("    6. 骑锦鲤上天撒钱恭喜发财 (华丽终幕)")

    logger.info("")
    logger.info("  各平台标题:")
    for k, v in PLATFORM_CONTENT.items():
        title = v.get("title", v.get("body", "")[:30])
        logger.info("    %-10s %s", k, title)

    save_content_json(output_dir)
    logger.info("")
    logger.info("  素材已保存到: %s", output_dir)
    logger.info("  执行 python cny_gongxi.py 开始生成视频")
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
    """
    dubbed_clips: list[Path] = []
    total = len(SHOTS)

    vid_w, vid_h = tts.get_video_resolution(str(silent_clips[0]))

    for i, (shot, silent_path) in enumerate(zip(SHOTS, silent_clips)):
        dubbed_path = output_dir / f"shot{shot['id']}_{shot['title']}_dubbed.mp4"

        if dubbed_path.exists() and dubbed_path.stat().st_size > 100_000:
            logger.info("镜头 %d/%d [%s] 配音已存在，跳过", shot["id"], total, shot["title"])
            dubbed_clips.append(dubbed_path)
            continue

        logger.info("镜头 %d/%d [%s] 配音中...", shot["id"], total, shot["title"])
        voiceover = shot["voiceover"]

        audio_path = output_dir / f"shot{shot['id']}_audio.mp3"
        sub_path = output_dir / f"shot{shot['id']}_sub.ass"
        audio_path, sub_file = tts.synthesize(
            voiceover, audio_path, sub_path,
            video_width=vid_w, video_height=vid_h,
        )

        tts.merge(
            video_path=silent_path,
            audio_path=audio_path,
            output_path=dubbed_path,
            subtitle_path=sub_file,
            loop_video=False,
        )

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
    """使用 ffmpeg concat 拼接多个视频片段"""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [FFMPEG_EXE]
    for p in clip_paths:
        cmd += ["-i", str(p)]
    n = len(clip_paths)
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
    """统一的平台发布配置"""
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
    """执行发布流程的核心逻辑"""
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

    retry_platforms = [
        (name, loader, fn) for name, loader, fn in _get_platform_config()
        if name in failed
    ]
    results = dict(last_results)
    _run_publish(output_dir, video_path, retry_platforms, results)
    _print_results(results, "重试发布结果")
    return results


# ================================================================
#  主入口
# ================================================================

def main():
    """CLI 主入口"""
    parser = argparse.ArgumentParser(description="财神爷的2026新春直播间 - 恭喜发财贺岁短片")
    parser.add_argument("--dry-run", action="store_true", help="预览脚本 + 仅生成素材 JSON")
    parser.add_argument("--publish", action="store_true", help="生成视频后自动发布全平台")
    parser.add_argument("--publish-only", action="store_true", help="直接发布已有视频（不重新生成）")
    parser.add_argument("--retry-failed", action="store_true", help="仅重试上次发布失败的平台")
    args = parser.parse_args()

    settings = get_settings()
    output_dir = Path(settings.output_dir) / "cny" / "gongxi_2026"
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.dry_run:
        preview(output_dir)
        return

    if args.retry_failed:
        final = output_dir / "gongxi_final.mp4"
        if not final.exists():
            logger.error("视频不存在: %s", final)
            sys.exit(1)
        publish_failed(output_dir, str(final))
        return

    if args.publish_only:
        final = output_dir / "gongxi_final.mp4"
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
    logger.info("  财神爷的 2026 新春直播间 · 恭喜发财")
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
    final = output_dir / "gongxi_final.mp4"
    concat_videos(dubbed_clips, final)
    update_video_path(output_dir, str(final))

    elapsed = int(time.time() - t0)
    logger.info("")
    logger.info("=" * 60)
    logger.info("  恭喜发财! 视频生成完成!")
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
        logger.info("  发布命令: python cny_gongxi.py --publish")


if __name__ == "__main__":
    main()
