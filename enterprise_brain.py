#!/usr/bin/env python3
"""
企业大脑私有化 —— 专题文章生成 + WordPress 发布 + 全平台分发

一键完成：
  1. AI 生成高质量长文（企业大脑私有化主题）
  2. 生成配图 & 上传到 WordPress
  3. 自动发布到 WordPress
  4. 生成各平台适配文案（小红书/抖音/头条/知乎/视频号/微博）
  5. 自动发布到全部 6 个平台

用法:
  python enterprise_brain.py                        # 仅发布 WordPress
  python enterprise_brain.py --all                  # WordPress + 全平台
  python enterprise_brain.py --all -y               # 跳过确认直接发布
  python enterprise_brain.py --dry-run              # 仅生成本地预览
  python enterprise_brain.py --all --video          # 带视频嵌入
  python enterprise_brain.py --all --headless       # 无头模式发布
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

from shared.config import get_settings
from shared.llm.client import LLMClient
from shared.llm.xhs import XHSContentGenerator
from shared.llm.douyin import DouyinContentGenerator
from shared.llm.toutiao import ToutiaoContentGenerator
from shared.llm.zhihu import ZhihuContentGenerator
from shared.llm.channels import ChannelsContentGenerator
from shared.llm.weibo import WeiboContentGenerator
from shared.media.image import ImageGenerator
from shared.media.video import VideoGenerator
from shared.utils.helpers import save_json, split_csv
from shared.utils.logger import get_logger
from shared.wp.client import WordPressClient
from wordpress.pipeline import WPPublisher

logger = get_logger("enterprise-brain")

# Windows 控制台编码兼容
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")


# ══════════════════════════════════════════════════════════════
#  企业大脑私有化 —— 专题提示词
# ══════════════════════════════════════════════════════════════

TOPIC = "企业大脑私有化部署"

PROMPT = """
主题：企业大脑私有化部署——为什么越来越多企业选择把AI大脑搬回自己的机房

目标读者：企业CTO、IT负责人、数字化转型团队Leader、信息安全负责人
（懂业务决策、关心数据安全、需要可落地的技术路线图）

文章目标：让读者读完后清楚知道——
  1. 企业大脑私有化到底在解决什么问题（不是赶时髦）
  2. 私有化部署的核心技术架构长什么样
  3. 从评估到上线的完整实施路径和关键里程碑
  4. 投入产出怎么算、效果怎么量化
  5. 踩过的坑和避坑策略

语气风格：像一个做过3个私有化项目的架构师跟同行聊天——
  有技术深度但不堆术语，有真实案例但不夸张，说人话、给干货

内容要求：
  - 标题要有信息增量，不要写成"浅谈xxx"或"关于xxx的思考"
  - 开篇用一个真实场景引入：某企业数据泄露/合规压力/SaaS成本失控等
  - 必须覆盖这些核心板块：
    a) 为什么现在是私有化的最佳窗口期（模型开源生态成熟、推理成本下降、合规要求收紧）
    b) 企业大脑私有化的技术架构：本地大模型（DeepSeek/Qwen/Llama）+ RAG知识库 + Agent工作流 + 统一网关
    c) 数据安全与合规：如何保证数据不出域、满足等保/GDPR/行业监管要求
    d) 实施路径：需求评估→模型选型→基础设施准备→知识库搭建→业务场景对接→灰度上线→持续优化
    e) 成本模型：GPU服务器、运维人力、与SaaS方案的TCO对比（给出具体数字区间）
    f) 常见踩坑：模型幻觉处理、知识库更新滞后、员工不会用、ROI难量化
  - 每个核心观点必须用具体场景或数据支撑（写清楚行业、团队规模、效果数字）
  - 段落要短（每段3-5句），节奏要快，每段结尾有下一步行动建议
  - 包含FAQ部分：至少5个企业决策者最常问的问题
  - 文末有清晰的「下一步」行动清单

绝对禁止：
  - 空话套话、「随着AI的发展」式开头
  - 凭空编造数据（可以给合理的范围区间）
  - 只讲概念不讲落地的内容
  - 把私有化说得完美无缺——要客观讲优劣

期望结果：一篇2500-3500字的中文深度文章，结构清晰、SEO友好、有配图空间，
读完后企业决策者能拿来做内部立项汇报的参考材料。

SEO 关键词建议：企业大脑私有化, 大模型私有化部署, 企业AI私有化, 私有化大模型,
DeepSeek私有化, 企业知识库, RAG企业应用, AI数据安全
""".strip()

CATEGORIES = "AI,企业数字化"
TAGS = "企业大脑,私有化部署,大模型,DeepSeek,RAG,数据安全,企业AI"


# ══════════════════════════════════════════════════════════════
#  工厂函数
# ══════════════════════════════════════════════════════════════

def _make_llm(settings) -> LLMClient:
    return LLMClient(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        timeout=settings.request_timeout,
    )


def _make_wp(settings) -> WordPressClient:
    return WordPressClient(
        wp_base=settings.wp_base,
        wp_user=settings.wp_user,
        wp_app_password=settings.wp_app_password,
        timeout=settings.request_timeout,
    )


def _make_image_gen(settings) -> ImageGenerator:
    return ImageGenerator(volc_ak=settings.volc_ak, volc_sk=settings.volc_sk)


def _make_video_gen(settings, llm: LLMClient) -> VideoGenerator:
    return VideoGenerator(
        volc_ak=settings.volc_ak,
        volc_sk=settings.volc_sk,
        llm=llm,
        volc_host=settings.volc_host,
        volc_service=settings.volc_service,
        volc_region=settings.volc_region,
    )


# ══════════════════════════════════════════════════════════════
#  Step 1: WordPress 发布
# ══════════════════════════════════════════════════════════════

def publish_to_wordpress(args, settings) -> dict:
    """生成文章并发布到 WordPress"""
    sep = "=" * 60
    print(f"\n{sep}")
    print("  Step 1: 生成文章 & 发布到 WordPress")
    print(sep)

    t_start = time.monotonic()
    llm = _make_llm(settings)
    wp = _make_wp(settings)
    image_gen = _make_image_gen(settings)
    video_gen = _make_video_gen(settings, llm) if args.video else None

    publisher = WPPublisher(
        wp_client=wp,
        llm=llm,
        image_gen=image_gen,
        video_gen=video_gen,
        output_dir=settings.output_dir,
        max_content_images=args.max_images,
        deepseek_enabled=True,
        site_name=settings.site_name,
    )

    with publisher:
        result = publisher.publish(
            prompt=PROMPT,
            categories=split_csv(CATEGORIES),
            tags=split_csv(TAGS),
            status="draft" if args.dry_run else "publish",
            dry_run=args.dry_run,
            min_quality_score=args.min_quality,
            strict_quality=False,
            verify_online=not args.dry_run,
            related_limit=3,
            use_deepseek=True,
            enable_video=args.video,
            video_ratio="16:9",
        )

    result["elapsed"] = time.monotonic() - t_start
    llm.close()

    _print_wp_report(result)
    return result


def _print_wp_report(result: dict):
    sep = "=" * 60
    lines = [f"\n{sep}", "  WordPress 发布结果", sep]
    if result.get("dry_run"):
        lines.append(f"  模式:       dry-run")
        lines.append(f"  本地预览:   {result.get('preview_file', '-')}")
    else:
        lines.append(f"  模式:       publish")
        lines.append(f"  文章ID:     {result.get('post_id', '-')}")
        lines.append(f"  文章链接:   {result.get('link', '-')}")
    lines.append(f"  slug:       {result.get('slug', '-')}")
    lines.append(f"  素材目录:   {result.get('asset_dir', '-')}")
    lines.append(f"  上传图片:   {result.get('media_count', 0)} 张")
    lines.append(f"  内容来源:   {result.get('content_source', 'rules')}")
    lines.append(f"  文章视频:   {'有' if result.get('has_video') else '无'}")
    lines.append(f"  耗时:       {result.get('elapsed', 0):.1f}s")
    quality = result.get("quality", {})
    if quality:
        lines.append(f"  质量评分:   {quality.get('score')}")
    lines.append(sep)
    logger.info("\n".join(lines))


# ══════════════════════════════════════════════════════════════
#  Step 2 & 3: 多平台文案生成 & 发布
# ══════════════════════════════════════════════════════════════

def publish_to_all_platforms(wp_result: dict, args, settings):
    """生成各平台文案并发布"""
    slug = wp_result.get("slug", "")
    asset_dir = Path(wp_result.get("asset_dir", ""))
    if not slug or not asset_dir.exists():
        logger.error("WordPress 发布结果异常，无法继续多平台流程")
        return

    article_file = asset_dir / "article.json"
    with open(article_file, "r", encoding="utf-8") as f:
        article = json.load(f)

    # 收集图片
    image_dir = asset_dir / "images"
    image_paths = []
    if image_dir.exists():
        for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
            image_paths.extend(sorted(image_dir.glob(ext)))
    image_paths = [str(p) for p in image_paths[:9]]

    # 为竖版平台（小红书/抖音/视频号）生成 3:4 图片
    vertical_image_paths = []
    try:
        from shared.llm.article import ArticleGenerator as _AG
        v_image_dir = asset_dir / "images_vertical"
        v_image_dir.mkdir(parents=True, exist_ok=True)
        if settings.volc_ak and settings.volc_sk:
            img_gen = ImageGenerator(settings.volc_ak, settings.volc_sk)
            _art_gen = _AG(llm=None, max_content_images=settings.max_content_images)
            v_specs = _art_gen.image_prompts(article)
            base_seed = sum(ord(c) for c in slug) % 2147483647
            logger.info("生成竖版配图（3:4, 1080x1440, seed=%d）...", base_seed)
            for i, spec in enumerate(v_specs):
                vp = v_image_dir / f"{spec['role']}_{i:02d}.png"
                v_prompt = spec["prompt"].replace("16:9", "3:4 vertical portrait").replace("Wide ", "Vertical portrait ")
                gen_path = img_gen.generate(v_prompt, vp, width=1080, height=1440, seed=base_seed + i)
                if gen_path:
                    vertical_image_paths.append(str(gen_path))
            logger.info("竖版配图生成完成: %d 张", len(vertical_image_paths))
    except Exception as e:
        logger.warning("竖版配图生成失败，使用横版图片: %s", e)

    v_images = vertical_image_paths if vertical_image_paths else image_paths

    # 生成各平台文案
    sep = "=" * 60
    print(f"\n{sep}")
    print("  Step 2: 生成各平台文案")
    print(sep)

    llm = _make_llm(settings)
    platform_contents = {}

    generators = [
        ("小红书", "xhs", XHSContentGenerator, v_images, "xhs_content.json"),
        ("抖音", "douyin", DouyinContentGenerator, v_images, "dy_content.json"),
        ("头条", "toutiao", ToutiaoContentGenerator, image_paths, "toutiao_content.json"),
        ("知乎", "zhihu", ZhihuContentGenerator, image_paths, "zh_content.json"),
        ("视频号", "channels", ChannelsContentGenerator, v_images, "channels_content.json"),
        ("微博", "weibo", WeiboContentGenerator, image_paths, "wb_content.json"),
    ]

    for name, key, gen_class, imgs, filename in generators:
        try:
            gen = gen_class(llm)
            content = gen.generate_from_article(article, imgs)
            platform_contents[key] = content
            filepath = asset_dir / filename
            save_json(filepath, content.to_dict())
            logger.info("[%s] 文案生成成功，已保存: %s", name, filepath)
            print(f"  [ok] {name}: {content.title[:40] if hasattr(content, 'title') and content.title else '(已生成)'}")
        except Exception as e:
            logger.error("[%s] 文案生成失败: %s", name, e)
            print(f"  [fail] {name}: {e}")

    llm.close()

    # 发布到各平台
    if not args.yes:
        confirm = input("\n确认发布到所有平台？(y/N): ").strip().lower()
        if confirm != "y":
            print("已取消发布。文案已保存，可用各平台 republish 命令单独发布。")
            return

    print(f"\n{sep}")
    print("  Step 3: 发布到各平台")
    print(sep)

    results = {}
    headless = args.headless

    # 小红书
    xhs_content = platform_contents.get("xhs")
    if xhs_content:
        try:
            from xiaohongshu.publisher import publish_note
            logger.info("[小红书] 开始发布...")
            ok = publish_note(xhs_content, headless=headless)
            results["小红书"] = ok
            print(f"  [{'ok' if ok else '??'}] 小红书")
        except Exception as e:
            results["小红书"] = False
            logger.error("[小红书] 发布失败: %s", e, exc_info=True)
            print(f"  [fail] 小红书: {e}")

    # 抖音
    dy_content = platform_contents.get("douyin")
    if dy_content:
        try:
            from douyin.publisher import publish_douyin_note
            logger.info("[抖音] 开始发布...")
            ok = publish_douyin_note(dy_content, headless=headless)
            results["抖音"] = ok
            print(f"  [{'ok' if ok else '??'}] 抖音")
        except Exception as e:
            results["抖音"] = False
            logger.error("[抖音] 发布失败: %s", e, exc_info=True)
            print(f"  [fail] 抖音: {e}")

    # 头条
    tt_content = platform_contents.get("toutiao")
    if tt_content:
        try:
            from toutiao.publisher import publish_toutiao_article
            logger.info("[头条] 开始发布...")
            ok = publish_toutiao_article(tt_content, headless=headless)
            results["头条"] = ok
            print(f"  [{'ok' if ok else '??'}] 头条")
        except Exception as e:
            results["头条"] = False
            logger.error("[头条] 发布失败: %s", e, exc_info=True)
            print(f"  [fail] 头条: {e}")

    # 知乎
    zh_content = platform_contents.get("zhihu")
    if zh_content:
        try:
            from zhihu.publisher import publish_zhihu_article
            logger.info("[知乎] 开始发布...")
            ok = publish_zhihu_article(zh_content, headless=headless)
            results["知乎"] = ok
            print(f"  [{'ok' if ok else '??'}] 知乎")
        except Exception as e:
            results["知乎"] = False
            logger.error("[知乎] 发布失败: %s", e, exc_info=True)
            print(f"  [fail] 知乎: {e}")

    # 视频号
    ch_content = platform_contents.get("channels")
    if ch_content:
        try:
            from channels.publisher import publish_channels_text
            logger.info("[视频号] 开始发布...")
            ok = publish_channels_text(
                body=ch_content.full_text(),
                image_sources=ch_content.image_urls,
                title=ch_content.title if hasattr(ch_content, "title") else "",
                headless=headless,
            )
            results["视频号"] = ok
            print(f"  [{'ok' if ok else '??'}] 视频号")
        except Exception as e:
            results["视频号"] = False
            logger.error("[视频号] 发布失败: %s", e, exc_info=True)
            print(f"  [fail] 视频号: {e}")

    # 微博
    wb_content = platform_contents.get("weibo")
    if wb_content:
        try:
            from weibo.publisher import publish_weibo_article
            logger.info("[微博] 开始发布...")
            ok = publish_weibo_article(wb_content, headless=headless)
            results["微博"] = ok
            print(f"  [{'ok' if ok else '??'}] 微博")
        except Exception as e:
            results["微博"] = False
            logger.error("[微博] 发布失败: %s", e, exc_info=True)
            print(f"  [fail] 微博: {e}")

    # 结果汇总
    _print_all_report(results)
    return results


def _print_all_report(results: dict):
    sep = "=" * 60
    print(f"\n{sep}")
    print("  全平台发布结果")
    print(sep)
    for platform, success in results.items():
        status = "ok" if success else "fail"
        print(f"  [{status}] {platform}")
    success_count = sum(1 for v in results.values() if v)
    total = len(results)
    print(f"\n  总计: {success_count}/{total} 个平台发布成功")
    print(sep)


# ══════════════════════════════════════════════════════════════
#  CLI 入口
# ══════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="企业大脑私有化 —— 专题文章生成 + WordPress 发布 + 全平台分发",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--all", action="store_true",
                        help="WordPress + 全平台发布（默认仅 WordPress）")
    parser.add_argument("--dry-run", action="store_true",
                        help="仅生成本地预览，不实际发布")
    parser.add_argument("--video", action="store_true",
                        help="同时生成视频嵌入文章")
    parser.add_argument("--max-images", type=int, default=4,
                        help="最大配图数量（默认 4）")
    parser.add_argument("--min-quality", type=int, default=75,
                        help="最低质量评分（默认 75）")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="跳过确认直接发布")
    parser.add_argument("--headless", action="store_true",
                        help="无头浏览器模式")
    parser.add_argument("--no-headless", dest="headless", action="store_false")
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    settings = get_settings()
    settings.check_or_exit()

    sep = "=" * 60
    print(f"\n{sep}")
    print("  企业大脑私有化 —— 专题文章自动发布")
    print(sep)
    print(f"  主题:     {TOPIC}")
    print(f"  分类:     {CATEGORIES}")
    print(f"  标签:     {TAGS}")
    print(f"  模式:     {'dry-run' if args.dry_run else 'publish'}")
    print(f"  全平台:   {'是' if args.all else '否（仅 WordPress）'}")
    print(f"  视频:     {'是' if args.video else '否'}")
    print(sep)

    if not args.yes and not args.dry_run:
        confirm = input("\n确认开始？(y/N): ").strip().lower()
        if confirm != "y":
            print("已取消。")
            return

    t0 = time.monotonic()

    # Step 1: WordPress
    wp_result = publish_to_wordpress(args, settings)

    # Step 2 & 3: 全平台（可选）
    if args.all and not args.dry_run:
        publish_to_all_platforms(wp_result, args, settings)

    elapsed = time.monotonic() - t0
    print(f"\n  总耗时: {elapsed:.1f}s")
    print(f"  素材目录: {wp_result.get('asset_dir', '-')}")
    if wp_result.get("link"):
        print(f"  WordPress: {wp_result['link']}")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n已中断。")
        sys.exit(130)
    except Exception as e:
        logger.error("错误: %s", e, exc_info=True)
        print(f"\n  错误: {e}", file=sys.stderr)
        print("详情请查看 logs/run.log", file=sys.stderr)
        sys.exit(1)
