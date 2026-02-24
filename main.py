#!/usr/bin/env python3
"""
Aineoo 内容自动发布工具 —— 统一 CLI 入口

用法:
    # ── WordPress ──
    python main.py wp --topic "AI销售自动化"
    python main.py wp --topic "AI销售自动化" --dry-run
    python main.py wp --topic "AI销售自动化" --status draft

    # ── 小红书 ──
    python main.py xhs list                          # 列出 WordPress 文章
    python main.py xhs generate <post_id>            # 生成文案
    python main.py xhs publish  <post_id>            # 生成并发布
    python main.py xhs local-list                    # 列出共享素材
    python main.py xhs local <slug>                  # 从素材生成文案
    python main.py xhs local <slug> --publish        # 从素材生成并发布
    python main.py xhs video <post_id|json>          # 生成视频（配音+字幕）
    python main.py xhs video <source> --no-audio     # 生成视频不配音
    python main.py xhs video <source> --avatar --avatar-image face.jpg  # 加数字人
    python main.py xhs audio <source>                # 为已有视频添加配音
    python main.py xhs audio <source> --avatar --avatar-image face.jpg  # 配音+数字人
    python main.py xhs republish <json_file>         # 从 JSON 直接发布
    python main.py xhs batch <id1> <id2> ...         # 批量处理

    # ── 评论引流 ──
    python main.py xhs comment "AI工具" --my-note xhs_post_802.json   # 自动评论引流
    python main.py xhs comment "副业" --my-title "标题" --my-summary "摘要" --max-comments 3
    python main.py xhs comment "AI赚钱" --my-note 802 --sort hot --style casual

    # ── 全流程 ──
    python main.py all --topic "AI销售自动化"        # WordPress → 小红书一键发布
"""

from __future__ import annotations

import argparse
import io as _io
import json
import sys
import time
from pathlib import Path

from shared.config import get_settings
from shared.llm.client import LLMClient
from shared.llm.xhs import XHSContent, XHSContentGenerator
from shared.llm.douyin import DouyinContent, DouyinContentGenerator
from shared.llm.toutiao import ToutiaoContent, ToutiaoContentGenerator
from shared.llm.zhihu import ZhihuContent, ZhihuContentGenerator
from shared.llm.channels import ChannelsContent, ChannelsContentGenerator
from shared.llm.weibo import WeiboContent, WeiboContentGenerator
from shared.media.avatar import AvatarGenerator
from shared.media.story_video import run_story_video, StoryVideoResult
from shared.media.image import ImageGenerator
from shared.media.tts import TTSGenerator
from shared.media.video import VideoGenerator
from shared.utils.exceptions import AppBaseError, ConfigError, QualityError
from shared.utils.helpers import resolve_prompt, save_json, split_csv
from shared.utils.logger import get_logger
from shared.wp.client import WordPressClient
from wordpress.pipeline import WPPublisher
from wordpress.html_builder import verify_published_page

logger = get_logger("main")

# Windows 控制台编码兼容
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")


# ══════════════════════════════════════════════════════════════
#  工厂函数 —— 按需创建共享组件
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


def _make_avatar_gen(settings, avatar_image_url: str = "") -> AvatarGenerator:
    return AvatarGenerator(
        fal_key=settings.fal_key,
        avatar_image_url=avatar_image_url,
    )


def _make_tts(llm: LLMClient, voice: str = "zh-CN-XiaoyiNeural", rate: str = "+0%") -> TTSGenerator:
    return TTSGenerator(llm=llm, voice=voice, rate=rate)


# ══════════════════════════════════════════════════════════════
#  WordPress 子命令
# ══════════════════════════════════════════════════════════════

def cmd_wp(args):
    """WordPress 发布流程"""
    settings = get_settings()
    settings.check_or_exit()

    final_prompt = resolve_prompt(
        raw_prompt=args.prompt,
        topic=args.topic,
        prompt_template=settings.prompt_template,
    )

    t_start = time.monotonic()
    llm = _make_llm(settings)
    wp = _make_wp(settings)
    image_gen = _make_image_gen(settings)
    video_gen = _make_video_gen(settings, llm) if getattr(args, "video", False) else None
    avatar_gen = _make_avatar_gen(settings, getattr(args, "avatar_image", "")) if getattr(args, "avatar", False) else None

    publisher = WPPublisher(
        wp_client=wp,
        llm=llm,
        image_gen=image_gen,
        video_gen=video_gen,
        avatar_gen=avatar_gen,
        output_dir=settings.output_dir,
        max_content_images=args.max_images,
        deepseek_enabled=args.use_deepseek,
        site_name=settings.site_name,
    )
    with publisher:
        result = publisher.publish(
            prompt=final_prompt,
            categories=split_csv(args.categories),
            tags=split_csv(args.tags),
            status=args.status,
            custom_slug=args.slug or None,
            custom_title=args.title or None,
            dry_run=args.dry_run,
            min_quality_score=args.min_quality,
            strict_quality=args.strict_quality,
            verify_online=not args.skip_verify,
            related_limit=args.related_limit,
            use_deepseek=args.use_deepseek,
            enable_video=getattr(args, "video", False),
            video_ratio=getattr(args, "video_ratio", "16:9"),
            enable_avatar=getattr(args, "avatar", False),
            avatar_image_url=getattr(args, "avatar_image", "") or None,
        )

    result["elapsed"] = time.monotonic() - t_start
    llm.close()
    _print_wp_report(result)
    return result


def _print_wp_report(result: dict) -> None:
    """格式化输出 WordPress 发布结果"""
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
    lines.append(f"  分类IDs:    {result.get('category_ids', [])}")
    lines.append(f"  标签IDs:    {result.get('tag_ids', [])}")
    lines.append(f"  相关文章:   {result.get('related_count', 0)} 篇")
    lines.append(f"  内容来源:   {result.get('content_source', 'rules')}")
    lines.append(f"  文章视频:   {'有' if result.get('has_video') else '无'}")
    lines.append(f"  数字人:     {'有' if result.get('has_avatar') else '无'}")
    lines.append(f"  耗时:       {result.get('elapsed', 0):.1f}s")

    quality = result.get("quality", {})
    if quality:
        lines.append(f"  质量评分:   {quality.get('score')}")
        failed = quality.get("failed", [])
        if failed:
            lines.append("  未通过项:")
            for item in failed:
                lines.append(f"    - {item['name']} ({item['detail']})")

    verify = result.get("verify")
    if verify is not None:
        status_text = "通过" if verify.get("ok") else "未通过"
        lines.append(f"  在线验收:   {status_text}")

    lines.append(sep)
    logger.info("\n".join(lines))


# ══════════════════════════════════════════════════════════════
#  小红书子命令
# ══════════════════════════════════════════════════════════════

def _xhs_output_dir() -> Path:
    """小红书本地输出目录"""
    return Path(get_settings().output_dir)


def _print_xhs_preview(content: XHSContent) -> None:
    print("\n" + "=" * 50)
    print("  小红书文案预览")
    print("=" * 50)
    print(f"\n  标题: {content.title}")
    print(f"\n  正文:\n{content.body}")
    tags = " ".join(f"#{t}" for t in content.hashtags)
    print(f"\n  话题: {tags}")
    if content.image_urls:
        print(f"\n  配图 ({len(content.image_urls)} 张):")
        for i, url in enumerate(content.image_urls, 1):
            print(f"     {i}. {url}")
    print("\n" + "=" * 50)


def _save_xhs_content(content: XHSContent, post_id: int) -> None:
    output_dir = _xhs_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    data = content.to_dict()
    data["post_id"] = post_id
    filepath = output_dir / f"xhs_post_{post_id}.json"
    save_json(filepath, data)
    logger.info("文案已保存: %s", filepath)
    print(f"\n  文案已保存至: {filepath}")


def _save_xhs_content_local(content: XHSContent, slug: str, asset_dir: Path) -> None:
    data = content.to_dict()
    data["slug"] = slug
    data["source"] = "local"
    filepath = asset_dir / "xhs_content.json"
    save_json(filepath, data)
    logger.info("小红书文案已保存: %s", filepath)
    print(f"\n  文案已保存至: {filepath}")


def _do_xhs_publish(content: XHSContent, headless: bool = False) -> bool:
    from xiaohongshu.publisher import publish_note
    logger.info("开始自动发布到小红书...")
    success = publish_note(content, headless=headless)
    if success:
        print("\n  笔记发布成功！")
    else:
        print("\n  发布可能失败，请查看 logs/run.log 获取详情。")
    return success


def _do_xhs_publish_video(video_path: str, title: str, body: str, headless: bool = False) -> bool:
    from xiaohongshu.publisher import publish_video_note
    logger.info("开始自动发布视频到小红书...")
    success = publish_video_note(video_path, title, body, headless=headless)
    if success:
        print("\n  视频发布成功！")
    else:
        print("\n  视频发布可能失败，请查看 logs/run.log 获取详情。")
    return success


def cmd_xhs_list(args):
    settings = get_settings()
    settings.check_or_exit(require_llm=False)
    wp = _make_wp(settings)
    posts = wp.list_posts(per_page=args.count, search=args.search)
    wp.close()
    if not posts:
        print("没有找到已发布的文章。")
        return
    print(f"\n{'ID':>6}  {'日期':^12}  标题")
    print("-" * 60)
    for p in posts:
        print(f"{p.id:>6}  {p.date[:10]:^12}  {p.title}")
    print(f"\n共 {len(posts)} 篇文章")


def cmd_xhs_generate(args):
    settings = get_settings()
    settings.check_or_exit()
    wp = _make_wp(settings)
    post = wp.get_post(args.post_id)
    wp.close()
    logger.info("已获取文章: [%d] %s", post.id, post.title)

    llm = _make_llm(settings)
    gen = XHSContentGenerator(llm)
    content = gen.generate_from_post(post)
    llm.close()

    _print_xhs_preview(content)
    _save_xhs_content(content, post.id)


def cmd_xhs_publish(args):
    settings = get_settings()
    settings.check_or_exit()
    wp = _make_wp(settings)
    post = wp.get_post(args.post_id)
    wp.close()
    logger.info("已获取文章: [%d] %s", post.id, post.title)

    llm = _make_llm(settings)
    gen = XHSContentGenerator(llm)
    content = gen.generate_from_post(post)
    llm.close()

    _print_xhs_preview(content)
    _save_xhs_content(content, post.id)

    if not args.yes:
        confirm = input("\n确认发布到小红书？(y/N): ").strip().lower()
        if confirm != "y":
            print("已取消发布。")
            return
    _do_xhs_publish(content, args.headless)


def cmd_xhs_republish(args):
    filepath = Path(args.json_file)
    output_dir = _xhs_output_dir()
    if not filepath.exists():
        filepath = output_dir / args.json_file
    if not filepath.exists():
        print(f"  文件不存在: {args.json_file}")
        sys.exit(1)
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    content = XHSContent.from_dict(data)
    _print_xhs_preview(content)
    if not args.yes:
        confirm = input("\n确认发布到小红书？(y/N): ").strip().lower()
        if confirm != "y":
            print("已取消发布。")
            return
    _do_xhs_publish(content, args.headless)


def cmd_xhs_batch(args):
    settings = get_settings()
    settings.check_or_exit()
    wp = _make_wp(settings)
    llm = _make_llm(settings)
    gen = XHSContentGenerator(llm)
    total = len(args.post_ids)
    results = {"success": [], "failed": []}

    print(f"\n  批量任务：共 {total} 篇文章待处理")
    print("=" * 50)
    for idx, post_id in enumerate(args.post_ids, 1):
        print(f"\n[{idx}/{total}] 处理文章 ID: {post_id}")
        print("-" * 40)
        try:
            post = wp.get_post(post_id)
            content = gen.generate_from_post(post)
            _save_xhs_content(content, post.id)
            if args.publish:
                success = _do_xhs_publish(content, headless=args.headless)
                if success:
                    results["success"].append(post_id)
                else:
                    results["failed"].append(post_id)
                if idx < total:
                    delay = settings.xhs_publish_delay
                    print(f"  等待 {delay}s...")
                    time.sleep(delay)
            else:
                results["success"].append(post_id)
                _print_xhs_preview(content)
        except Exception as e:
            results["failed"].append(post_id)
            logger.error("文章 %d 处理失败: %s", post_id, e)
            print(f"  处理失败: {e}")

    wp.close()
    llm.close()
    print("\n" + "=" * 50)
    print(f"  批量任务完成：成功 {len(results['success'])} 篇，失败 {len(results['failed'])} 篇")
    if results["failed"]:
        print(f"   失败 ID: {results['failed']}")


def cmd_xhs_local(args):
    settings = get_settings()
    settings.check_or_exit()
    output_dir = Path(settings.output_dir)
    slug = args.slug
    asset_dir = output_dir / slug
    article_file = asset_dir / "article.json"
    image_dir = asset_dir / "images"

    if not article_file.exists():
        available = [d.name for d in output_dir.iterdir() if d.is_dir() and (d / "article.json").exists()] if output_dir.exists() else []
        print(f"\n  素材不存在: {article_file}")
        if available:
            print(f"\n  可用的 slug:")
            for s in sorted(available):
                print(f"    - {s}")
        else:
            print(f"  共享素材目录为空或不存在: {output_dir}")
            print("  请先使用 wp 命令生成文章")
        sys.exit(1)

    with open(article_file, "r", encoding="utf-8") as f:
        article = json.load(f)
    logger.info("已加载本地素材: %s", slug)

    image_paths = []
    if image_dir.exists():
        for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
            image_paths.extend(sorted(image_dir.glob(ext)))
    image_paths = [str(p) for p in image_paths[:9]]
    logger.info("本地图片 %d 张", len(image_paths))

    llm = _make_llm(settings)
    gen = XHSContentGenerator(llm)
    content = gen.generate_from_article(article, image_paths)
    llm.close()

    _print_xhs_preview(content)
    _save_xhs_content_local(content, slug, asset_dir)

    if not args.publish:
        return
    if not args.yes:
        confirm = input("\n确认发布到小红书？(y/N): ").strip().lower()
        if confirm != "y":
            print("已取消发布。")
            return
    _do_xhs_publish(content, args.headless)


def cmd_xhs_local_list(args):
    output_dir = Path(get_settings().output_dir)
    if not output_dir.exists():
        print(f"\n  共享素材目录不存在: {output_dir}")
        print("  请先使用 wp 命令生成文章")
        return
    slugs = []
    for d in sorted(output_dir.iterdir()):
        if d.is_dir() and (d / "article.json").exists():
            with open(d / "article.json", "r", encoding="utf-8") as f:
                article = json.load(f)
            title = article.get("title", "-")
            img_count = len(list((d / "images").glob("*"))) if (d / "images").exists() else 0
            has_result = (d / "result.json").exists()
            slugs.append((d.name, title, img_count, has_result))
    if not slugs:
        print("\n  共享素材目录为空，请先使用 wp 命令生成文章")
        return
    print(f"\n{'slug':<40}  {'图片':>4}  {'已发布':>6}  标题")
    print("-" * 100)
    for slug, title, img_count, has_result in slugs:
        status = "是" if has_result else "-"
        print(f"{slug:<40}  {img_count:>4}  {status:>6}  {title}")
    print(f"\n共 {len(slugs)} 篇素材  目录: {output_dir}")


def cmd_xhs_video(args):
    settings = get_settings()
    settings.check_or_exit()
    output_dir = Path(settings.output_dir)
    source = args.source
    filepath = Path(source)

    if filepath.exists() or (output_dir / source).exists():
        if not filepath.exists():
            filepath = output_dir / source
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        post_id = data.get("post_id", 0)
    else:
        try:
            post_id = int(source)
        except ValueError:
            print(f"无效参数: {source}（应为文章 ID 或 JSON 文件路径）")
            sys.exit(1)
        json_path = output_dir / f"xhs_post_{post_id}.json"
        if json_path.exists():
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            wp = _make_wp(settings)
            post = wp.get_post(post_id)
            wp.close()
            llm = _make_llm(settings)
            gen = XHSContentGenerator(llm)
            content = gen.generate_from_post(post)
            llm.close()
            _save_xhs_content(content, post.id)
            data = content.to_dict()
            data["post_id"] = post_id

    title = data.get("title", "")
    body = data.get("body", "")
    ratio = args.ratio
    custom_prompt = args.prompt

    print(f"\n为文章 [{post_id}] 生成视频")
    print(f"标题: {title}")
    print(f"比例: {ratio}")
    print("=" * 50)

    llm = _make_llm(settings)
    vg = _make_video_gen(settings, llm)
    save_dir = output_dir / f"video_{post_id}"
    video_path = vg.generate_from_article(
        title=title, body=body, save_dir=save_dir,
        filename=f"xhs_video_{post_id}.mp4",
        aspect_ratio=ratio, custom_prompt=custom_prompt,
    )

    if not video_path:
        llm.close()
        print("\n视频生成失败")
        sys.exit(1)
    print(f"\n无声视频已生成: {video_path}")

    # 配音（默认开启，--no-audio 关闭）
    no_audio = getattr(args, "no_audio", False)
    if not no_audio:
        voice = getattr(args, "voice", "zh-CN-XiaoyiNeural")
        rate = getattr(args, "rate", "+0%")
        custom_script = getattr(args, "script", None)
        no_subtitle = getattr(args, "no_subtitle", False)
        use_avatar = getattr(args, "avatar", False)
        avatar_image = getattr(args, "avatar_image", None)
        avatar_pos = getattr(args, "avatar_position", "bottom_right")
        avatar_scale = getattr(args, "avatar_scale", 0.3)

        tts = _make_tts(llm, voice=voice, rate=rate)
        final_path = tts.add_voiceover(
            video_path=video_path,
            title=title, body=body,
            save_dir=save_dir,
            filename_prefix=f"xhs_video_{post_id}",
            custom_script=custom_script,
            with_subtitle=not no_subtitle,
            with_avatar=use_avatar,
            avatar_image=Path(avatar_image) if avatar_image else None,
            avatar_position=avatar_pos,
            avatar_scale=avatar_scale,
            fal_key=settings.fal_key,
        )
        if final_path and final_path.exists():
            video_path = final_path
            print(f"最终视频已生成: {video_path}")
        else:
            logger.warning("配音失败，使用无声视频")

        # 更新 JSON
        json_path = output_dir / f"xhs_post_{post_id}.json"
        if json_path.exists():
            import json as _json
            with open(json_path, "r", encoding="utf-8") as f:
                jdata = _json.load(f)
            jdata["final_video_path"] = str(video_path)
            with open(json_path, "w", encoding="utf-8") as f:
                _json.dump(jdata, f, ensure_ascii=False, indent=2)
    llm.close()

    if args.publish:
        hashtags = data.get("hashtags", [])
        full_body = body
        if hashtags:
            tags_text = " ".join(f"#{t}" for t in hashtags)
            full_body = f"{body}\n\n{tags_text}"
        safe_title = title[:20]
        _do_xhs_publish_video(str(video_path), safe_title, full_body, args.headless)


def cmd_xhs_video_publish(args):
    output_dir = Path(get_settings().output_dir)
    source = args.source
    filepath = Path(source)

    if filepath.exists() or (output_dir / source).exists():
        if not filepath.exists():
            filepath = output_dir / source
    else:
        try:
            post_id = int(source)
            filepath = output_dir / f"xhs_post_{post_id}.json"
        except ValueError:
            print(f"无效参数: {source}")
            sys.exit(1)

    if not filepath.exists():
        print(f"文件不存在: {filepath}")
        sys.exit(1)

    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 优先使用有声视频，回退到无声视频
    post_id = data.get("post_id", 0)
    video_path = data.get("final_video_path") or data.get("video_path")
    if not video_path or not Path(video_path).exists():
        # 按优先级查找
        candidates = [
            output_dir / f"video_{post_id}" / f"xhs_video_{post_id}_final.mp4",
            output_dir / f"video_{post_id}" / f"xhs_video_{post_id}.mp4",
        ]
        video_path = None
        for c in candidates:
            if c.exists():
                video_path = str(c)
                break
        if not video_path:
            print("未找到视频文件。请先用 xhs video 命令生成视频。")
            sys.exit(1)

    title = data.get("title", "")[:20]
    body = data.get("body", "")
    hashtags = data.get("hashtags", [])
    full_body = body
    if hashtags:
        full_body = f"{body}\n\n{' '.join(f'#{t}' for t in hashtags)}"

    print(f"\n发布视频到小红书")
    print(f"视频: {video_path}")
    print(f"标题: {title}")
    print("=" * 50)

    if not args.yes:
        confirm = input("\n确认发布到小红书？(y/N): ").strip().lower()
        if confirm != "y":
            print("已取消发布。")
            return
    _do_xhs_publish_video(video_path, title, full_body, args.headless)


def cmd_xhs_audio(args):
    """为已有无声视频添加配音"""
    settings = get_settings()
    output_dir = Path(settings.output_dir)
    source = args.source
    filepath = Path(source)

    if filepath.exists() or (output_dir / source).exists():
        if not filepath.exists():
            filepath = output_dir / source
    else:
        try:
            post_id = int(source)
            filepath = output_dir / f"xhs_post_{post_id}.json"
        except ValueError:
            print(f"无效参数: {source}（应为 JSON 文件路径或文章 ID）")
            sys.exit(1)

    if not filepath.exists():
        print(f"文件不存在: {filepath}")
        sys.exit(1)

    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 获取视频路径
    video_path = data.get("video_path")
    post_id = data.get("post_id", 0)
    if not video_path or not Path(video_path).exists():
        fallback = output_dir / f"video_{post_id}" / f"xhs_video_{post_id}.mp4"
        if fallback.exists():
            video_path = str(fallback)
        else:
            print("未找到视频文件。请先用 xhs video 命令生成视频。")
            sys.exit(1)

    title = data.get("title", "")
    body = data.get("body", "")
    voice = getattr(args, "voice", "zh-CN-XiaoyiNeural")
    rate = getattr(args, "rate", "+0%")
    custom_script = getattr(args, "script", None)

    print(f"\n为视频添加配音")
    print(f"视频: {video_path}")
    print(f"语音: {voice}  语速: {rate}")
    print("=" * 50)

    no_subtitle = getattr(args, "no_subtitle", False)
    use_avatar = getattr(args, "avatar", False)
    avatar_image = getattr(args, "avatar_image", None)
    avatar_pos = getattr(args, "avatar_position", "bottom_right")
    avatar_scale = getattr(args, "avatar_scale", 0.3)

    llm = _make_llm(settings)
    tts = _make_tts(llm, voice=voice, rate=rate)
    save_dir = Path(video_path).parent
    final_path = tts.add_voiceover(
        video_path=Path(video_path),
        title=title, body=body,
        save_dir=save_dir,
        filename_prefix=f"xhs_video_{post_id}",
        custom_script=custom_script,
        with_subtitle=not no_subtitle,
        with_avatar=use_avatar,
        avatar_image=Path(avatar_image) if avatar_image else None,
        avatar_position=avatar_pos,
        avatar_scale=avatar_scale,
        fal_key=settings.fal_key,
    )
    llm.close()

    if final_path:
        # 更新 JSON
        data["final_video_path"] = str(final_path)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"\n有声视频已生成: {final_path}")
    else:
        print("\n配音失败")
        sys.exit(1)


def cmd_xhs_comment(args):
    """搜索热门笔记并发布引流评论"""
    settings = get_settings()

    keyword = args.keyword
    max_comments = args.max_comments or settings.xhs_comment_max
    comment_delay = args.delay or settings.xhs_comment_delay
    sort = args.sort
    style = args.style

    # 获取自己笔记的信息
    my_title = args.my_title
    my_summary = args.my_summary

    # 如果指定了 JSON 文件，从中读取笔记信息
    if args.my_note:
        note_path = Path(args.my_note)
        output_dir = Path(settings.output_dir)
        if not note_path.exists():
            note_path = output_dir / args.my_note
        if not note_path.exists():
            # 尝试 xhs_post_{id}.json
            note_path = output_dir / f"xhs_post_{args.my_note}.json"
        if not note_path.exists():
            # 尝试 {slug}/xhs_content.json
            note_path = output_dir / args.my_note / "xhs_content.json"
        if note_path.exists():
            with open(note_path, "r", encoding="utf-8") as f:
                note_data = json.load(f)
            if not my_title:
                my_title = note_data.get("title", "")
            if not my_summary:
                my_summary = note_data.get("body", "")[:200]
            logger.info("已加载笔记信息: %s", my_title)
        else:
            logger.warning("未找到笔记文件: %s，将使用手动指定的信息", args.my_note)

    if not my_title or not my_summary:
        print("\n  错误：请提供你的笔记信息（--my-title + --my-summary 或 --my-note <json>）")
        print("  示例：")
        print("    python main.py xhs comment \"AI赚钱\" --my-note xhs_post_802.json")
        print("    python main.py xhs comment \"AI工具\" --my-title \"AI提效秘籍\" --my-summary \"分享3个AI工具...\"")
        sys.exit(1)

    print("\n" + "=" * 60)
    print("  小红书评论引流")
    print("=" * 60)
    print(f"  搜索关键词: {keyword}")
    print(f"  排序方式:   {sort}")
    print(f"  评论数量:   最多 {max_comments} 条")
    print(f"  评论间隔:   {comment_delay}s")
    print(f"  评论风格:   {style}")
    print(f"  我的笔记:   {my_title}")
    print("=" * 60)

    if not args.yes:
        confirm = input("\n确认开始评论引流？(y/N): ").strip().lower()
        if confirm != "y":
            print("已取消。")
            return

    from xiaohongshu.commenter import comment_on_notes

    results = comment_on_notes(
        keyword=keyword,
        my_note_title=my_title,
        my_note_summary=my_summary,
        max_comments=max_comments,
        comment_delay=comment_delay,
        sort=sort,
        style=style,
        headless=args.headless,
    )

    # 保存结果
    output_dir = Path(settings.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    result_file = output_dir / f"comment_results_{keyword}_{int(time.time())}.json"
    save_json(result_file, results)

    # 输出报告
    _print_comment_report(results, keyword, result_file)


def _print_comment_report(results: list, keyword: str, result_file: Path):
    """格式化输出评论引流结果"""
    success_count = sum(1 for r in results if r.get("success"))
    fail_count = len(results) - success_count

    sep = "=" * 60
    print(f"\n{sep}")
    print("  评论引流结果")
    print(sep)
    print(f"  关键词:   {keyword}")
    print(f"  总评论:   {len(results)} 条")
    print(f"  成功:     {success_count} 条")
    print(f"  失败:     {fail_count} 条")

    if results:
        print(f"\n  详情:")
        for i, r in enumerate(results, 1):
            status = "✓" if r.get("success") else "✗"
            title = r.get("note_title", "")[:25] or r.get("note_url", "")[-24:]
            comment = r.get("comment", "")[:40]
            print(f"    {status} [{i}] {title}")
            if comment:
                print(f"        评论: {comment}...")

    print(f"\n  结果已保存: {result_file}")
    print(sep)


def cmd_xhs_debug(args):
    from xiaohongshu.publisher import diagnose_page
    diagnose_page()


# ══════════════════════════════════════════════════════════════
#  抖音 (dy) 子命令
# ══════════════════════════════════════════════════════════════

def _dy_output_dir() -> Path:
    return Path(get_settings().output_dir)


def _print_dy_preview(content: DouyinContent):
    sep = "=" * 60
    print(f"\n{sep}")
    print("  抖音图文笔记 预览")
    print(sep)
    print(f"  标题: {content.title}")
    print(f"  正文 ({len(content.body)} 字):")
    for line in content.body.split("\n")[:8]:
        print(f"    {line}")
    if content.body.count("\n") > 8:
        print("    ...")
    if content.hashtags:
        print(f"  话题: {' '.join('#' + t for t in content.hashtags)}")
    print(f"  图片: {len(content.image_urls)} 张")
    print(sep)


def _save_dy_content(content: DouyinContent, post_id: int):
    output_dir = _dy_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    filepath = output_dir / f"dy_post_{post_id}.json"
    save_json(filepath, content.to_dict())
    logger.info("抖音文案已保存: %s", filepath)


def _save_dy_content_local(content: DouyinContent, slug: str, asset_dir: Path):
    filepath = asset_dir / "dy_content.json"
    save_json(filepath, content.to_dict())
    logger.info("抖音文案已保存: %s", filepath)


def _do_dy_publish(content: DouyinContent, headless: bool = False) -> bool:
    from douyin.publisher import publish_douyin_note
    logger.info("开始自动发布到抖音...")
    success = publish_douyin_note(content, headless=headless)
    if success:
        print("\n  ✓ 抖音发布成功！")
    else:
        print("\n  ✗ 抖音发布失败，请查看日志")
    return success


def cmd_dy_generate(args):
    settings = get_settings()
    settings.check_or_exit()
    wp = _make_wp(settings)
    post = wp.get_post(args.post_id)
    wp.close()
    logger.info("已获取文章: [%d] %s", post.id, post.title)

    llm = _make_llm(settings)
    gen = DouyinContentGenerator(llm)
    content = gen.generate_from_post(post)
    llm.close()

    _print_dy_preview(content)
    _save_dy_content(content, post.id)


def cmd_dy_publish(args):
    settings = get_settings()
    settings.check_or_exit()
    wp = _make_wp(settings)
    post = wp.get_post(args.post_id)
    wp.close()
    logger.info("已获取文章: [%d] %s", post.id, post.title)

    llm = _make_llm(settings)
    gen = DouyinContentGenerator(llm)
    content = gen.generate_from_post(post)
    llm.close()

    _print_dy_preview(content)
    _save_dy_content(content, post.id)

    if not args.yes:
        confirm = input("\n确认发布到抖音？(y/N): ").strip().lower()
        if confirm != "y":
            print("已取消发布。")
            return
    _do_dy_publish(content, args.headless)


def cmd_dy_republish(args):
    filepath = Path(args.json_file)
    output_dir = _dy_output_dir()
    if not filepath.exists():
        filepath = output_dir / args.json_file
    # 如果传入的是目录（slug），自动查找 dy_content.json
    if filepath.is_dir():
        filepath = filepath / "dy_content.json"
    if not filepath.exists():
        print(f"  文件不存在: {args.json_file}")
        print(f"  （尝试路径: {filepath}）")
        sys.exit(1)
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    content = DouyinContent.from_dict(data)
    logger.info("加载抖音文案: %s, 图片 %d 张", content.title, len(content.image_urls))

    # 防护：如果图片为空但 JSON 中有 cover_urls，说明传入了其他平台的文件
    if not content.image_urls and data.get("cover_urls"):
        print(f"\n  ⚠ 警告: 该 JSON 文件包含 cover_urls 但无 image_urls，可能不是抖音文案文件！")
        print(f"  抖音文案文件应为 dy_content.json 或 dy_post_*.json")
        print(f"  正确命令: python main.py dy republish <slug> --no-headless -y")
        sys.exit(1)

    if not content.image_urls:
        print(f"\n  ⚠ 警告: 文案中没有图片！抖音图文笔记需要至少 1 张图片作为封面。")
        print(f"  请使用 'python main.py dy local <slug>' 重新生成文案（会自动包含图片）")
        sys.exit(1)

    _print_dy_preview(content)
    if not args.yes:
        confirm = input("\n确认发布到抖音？(y/N): ").strip().lower()
        if confirm != "y":
            print("已取消发布。")
            return
    _do_dy_publish(content, args.headless)


def cmd_dy_local(args):
    settings = get_settings()
    settings.check_or_exit()
    output_dir = Path(settings.output_dir)
    slug = args.slug
    asset_dir = output_dir / slug
    article_file = asset_dir / "article.json"
    image_dir = asset_dir / "images"

    if not article_file.exists():
        available = [d.name for d in output_dir.iterdir() if d.is_dir() and (d / "article.json").exists()] if output_dir.exists() else []
        print(f"\n  素材不存在: {article_file}")
        if available:
            print(f"\n  可用的 slug:")
            for s in sorted(available):
                print(f"    - {s}")
        sys.exit(1)

    with open(article_file, "r", encoding="utf-8") as f:
        article = json.load(f)
    logger.info("已加载本地素材: %s", slug)

    image_paths = []
    if image_dir.exists():
        for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
            image_paths.extend(sorted(image_dir.glob(ext)))
    image_paths = [str(p) for p in image_paths[:9]]
    logger.info("本地图片 %d 张", len(image_paths))

    llm = _make_llm(settings)
    gen = DouyinContentGenerator(llm)
    content = gen.generate_from_article(article, image_paths)
    llm.close()

    _print_dy_preview(content)
    _save_dy_content_local(content, slug, asset_dir)

    if not args.publish:
        return
    if not args.yes:
        confirm = input("\n确认发布到抖音？(y/N): ").strip().lower()
        if confirm != "y":
            print("已取消发布。")
            return
    _do_dy_publish(content, args.headless)


def cmd_dy_local_list(args):
    output_dir = Path(get_settings().output_dir)
    if not output_dir.exists():
        print(f"  共享素材目录不存在: {output_dir}")
        return
    slugs = sorted([d.name for d in output_dir.iterdir() if d.is_dir() and (d / "article.json").exists()])
    if not slugs:
        print("  暂无共享素材")
        return
    print(f"\n  共 {len(slugs)} 个素材:")
    for s in slugs:
        dy_json = output_dir / s / "dy_content.json"
        status = "✓" if dy_json.exists() else " "
        print(f"  [{status}] {s}")
    print(f"\n  [✓] = 已生成抖音文案")


def cmd_dy_debug(args):
    from douyin.publisher import diagnose_douyin_page
    diagnose_douyin_page()


# ══════════════════════════════════════════════════════════════
#  头条 (toutiao) 子命令
# ══════════════════════════════════════════════════════════════

def _toutiao_output_dir() -> Path:
    return Path(get_settings().output_dir)


def _print_toutiao_preview(content: ToutiaoContent):
    sep = "=" * 60
    print(f"\n{sep}")
    print("  今日头条文章 预览")
    print(sep)
    print(f"  标题: {content.title}")
    print(f"  正文 ({len(content.body)} 字):")
    for line in content.body.split("\n")[:8]:
        print(f"    {line}")
    if content.body.count("\n") > 8:
        print("    ...")
    if content.tags:
        print(f"  标签: {', '.join(content.tags)}")
    print(f"  封面: {len(content.cover_urls)} 张")
    print(sep)


def _save_toutiao_content(content: ToutiaoContent, post_id: int):
    output_dir = _toutiao_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    filepath = output_dir / f"toutiao_post_{post_id}.json"
    save_json(filepath, content.to_dict())
    logger.info("头条文案已保存: %s", filepath)


def _save_toutiao_content_local(content: ToutiaoContent, slug: str, asset_dir: Path):
    filepath = asset_dir / "toutiao_content.json"
    save_json(filepath, content.to_dict())
    logger.info("头条文案已保存: %s", filepath)


def _do_toutiao_publish(content: ToutiaoContent, headless: bool = False) -> bool:
    from toutiao.publisher import publish_toutiao_article
    logger.info("开始自动发布到头条...")
    success = publish_toutiao_article(content, headless=headless)
    if success:
        print("\n  ✓ 头条发布成功！")
    else:
        print("\n  ✗ 头条发布失败，请查看日志")
    return success


def cmd_toutiao_generate(args):
    settings = get_settings()
    settings.check_or_exit()
    wp = _make_wp(settings)
    post = wp.get_post(args.post_id)
    wp.close()
    logger.info("已获取文章: [%d] %s", post.id, post.title)

    llm = _make_llm(settings)
    gen = ToutiaoContentGenerator(llm)
    content = gen.generate_from_post(post)
    llm.close()

    _print_toutiao_preview(content)
    _save_toutiao_content(content, post.id)


def cmd_toutiao_publish(args):
    settings = get_settings()
    settings.check_or_exit()
    wp = _make_wp(settings)
    post = wp.get_post(args.post_id)
    wp.close()

    llm = _make_llm(settings)
    gen = ToutiaoContentGenerator(llm)
    content = gen.generate_from_post(post)
    llm.close()

    _print_toutiao_preview(content)
    _save_toutiao_content(content, post.id)

    if not args.yes:
        confirm = input("\n确认发布到头条？(y/N): ").strip().lower()
        if confirm != "y":
            print("已取消发布。")
            return
    _do_toutiao_publish(content, args.headless)


def cmd_toutiao_republish(args):
    filepath = Path(args.json_file)
    output_dir = _toutiao_output_dir()
    if not filepath.exists():
        filepath = output_dir / args.json_file
    if filepath.is_dir():
        filepath = filepath / "toutiao_content.json"
    if not filepath.exists():
        print(f"  文件不存在: {args.json_file}")
        print(f"  （尝试路径: {filepath}）")
        sys.exit(1)
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    content = ToutiaoContent.from_dict(data)
    logger.info("加载头条文案: %s, 封面 %d 张", content.title, len(content.cover_urls))
    _print_toutiao_preview(content)
    if not args.yes:
        confirm = input("\n确认发布到头条？(y/N): ").strip().lower()
        if confirm != "y":
            print("已取消发布。")
            return
    _do_toutiao_publish(content, args.headless)


def cmd_toutiao_local(args):
    settings = get_settings()
    settings.check_or_exit()
    output_dir = Path(settings.output_dir)
    slug = args.slug
    asset_dir = output_dir / slug
    article_file = asset_dir / "article.json"
    image_dir = asset_dir / "images"

    if not article_file.exists():
        available = [d.name for d in output_dir.iterdir() if d.is_dir() and (d / "article.json").exists()] if output_dir.exists() else []
        print(f"\n  素材不存在: {article_file}")
        if available:
            print(f"\n  可用的 slug:")
            for s in sorted(available):
                print(f"    - {s}")
        sys.exit(1)

    with open(article_file, "r", encoding="utf-8") as f:
        article = json.load(f)
    logger.info("已加载本地素材: %s", slug)

    image_paths = []
    if image_dir.exists():
        for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
            image_paths.extend(sorted(image_dir.glob(ext)))
    image_paths = [str(p) for p in image_paths[:9]]
    logger.info("本地图片 %d 张", len(image_paths))

    llm = _make_llm(settings)
    gen = ToutiaoContentGenerator(llm)
    content = gen.generate_from_article(article, image_paths)
    llm.close()

    _print_toutiao_preview(content)
    _save_toutiao_content_local(content, slug, asset_dir)

    if not args.publish:
        return
    if not args.yes:
        confirm = input("\n确认发布到头条？(y/N): ").strip().lower()
        if confirm != "y":
            print("已取消发布。")
            return
    _do_toutiao_publish(content, args.headless)


def cmd_toutiao_local_list(args):
    output_dir = Path(get_settings().output_dir)
    if not output_dir.exists():
        print(f"  共享素材目录不存在: {output_dir}")
        return
    slugs = sorted([d.name for d in output_dir.iterdir() if d.is_dir() and (d / "article.json").exists()])
    if not slugs:
        print("  暂无共享素材")
        return
    print(f"\n  共 {len(slugs)} 个素材:")
    for s in slugs:
        tt_json = output_dir / s / "toutiao_content.json"
        status = "✓" if tt_json.exists() else " "
        print(f"  [{status}] {s}")
    print(f"\n  [✓] = 已生成头条文案")


def cmd_toutiao_debug(args):
    from toutiao.publisher import diagnose_toutiao_page
    diagnose_toutiao_page()


# ══════════════════════════════════════════════════════════════
#  知乎 (zh) 子命令
# ══════════════════════════════════════════════════════════════

def _zh_output_dir() -> Path:
    return Path(get_settings().output_dir)


def _print_zh_preview(content: ZhihuContent):
    sep = "=" * 60
    print(f"\n{sep}")
    print("  知乎文章 预览")
    print(sep)
    print(f"  标题: {content.title}")
    if content.summary:
        print(f"  摘要: {content.summary[:80]}...")
    print(f"  正文 ({len(content.body)} 字):")
    for line in content.body.split("\n")[:8]:
        print(f"    {line}")
    if content.body.count("\n") > 8:
        print("    ...")
    if content.tags:
        print(f"  标签: {', '.join(content.tags)}")
    print(f"  封面: {len(content.cover_urls)} 张")
    print(sep)


def _save_zh_content(content: ZhihuContent, post_id: int):
    output_dir = _zh_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    filepath = output_dir / f"zh_post_{post_id}.json"
    save_json(filepath, content.to_dict())
    logger.info("知乎文案已保存: %s", filepath)


def _save_zh_content_local(content: ZhihuContent, slug: str, asset_dir: Path):
    filepath = asset_dir / "zh_content.json"
    save_json(filepath, content.to_dict())
    logger.info("知乎文案已保存: %s", filepath)


def _do_zh_publish(content: ZhihuContent, headless: bool = False) -> bool:
    from zhihu.publisher import publish_zhihu_article
    logger.info("开始自动发布到知乎...")
    success = publish_zhihu_article(content, headless=headless)
    if success:
        print("\n  ✓ 知乎发布成功！")
    else:
        print("\n  ✗ 知乎发布失败，请查看日志")
    return success


def cmd_zh_generate(args):
    settings = get_settings()
    settings.check_or_exit()
    wp = _make_wp(settings)
    post = wp.get_post(args.post_id)
    wp.close()
    logger.info("已获取文章: [%d] %s", post.id, post.title)

    llm = _make_llm(settings)
    gen = ZhihuContentGenerator(llm)
    content = gen.generate_from_post(post)
    llm.close()

    _print_zh_preview(content)
    _save_zh_content(content, post.id)


def cmd_zh_publish(args):
    settings = get_settings()
    settings.check_or_exit()
    wp = _make_wp(settings)
    post = wp.get_post(args.post_id)
    wp.close()

    llm = _make_llm(settings)
    gen = ZhihuContentGenerator(llm)
    content = gen.generate_from_post(post)
    llm.close()

    _print_zh_preview(content)
    _save_zh_content(content, post.id)

    if not args.yes:
        confirm = input("\n确认发布到知乎？(y/N): ").strip().lower()
        if confirm != "y":
            print("已取消发布。")
            return
    _do_zh_publish(content, args.headless)


def cmd_zh_republish(args):
    filepath = Path(args.json_file)
    output_dir = _zh_output_dir()
    if not filepath.exists():
        filepath = output_dir / args.json_file
    if filepath.is_dir():
        filepath = filepath / "zh_content.json"
    if not filepath.exists():
        print(f"  文件不存在: {args.json_file}")
        print(f"  （尝试路径: {filepath}）")
        sys.exit(1)
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    content = ZhihuContent.from_dict(data)
    logger.info("加载知乎文案: %s, 封面 %d 张", content.title, len(content.cover_urls))
    _print_zh_preview(content)
    if not args.yes:
        confirm = input("\n确认发布到知乎？(y/N): ").strip().lower()
        if confirm != "y":
            print("已取消发布。")
            return
    _do_zh_publish(content, args.headless)


def cmd_zh_local(args):
    settings = get_settings()
    settings.check_or_exit()
    output_dir = Path(settings.output_dir)
    slug = args.slug
    asset_dir = output_dir / slug
    article_file = asset_dir / "article.json"
    image_dir = asset_dir / "images"

    if not article_file.exists():
        available = [d.name for d in output_dir.iterdir() if d.is_dir() and (d / "article.json").exists()] if output_dir.exists() else []
        print(f"\n  素材不存在: {article_file}")
        if available:
            print(f"\n  可用的 slug:")
            for s in sorted(available):
                print(f"    - {s}")
        sys.exit(1)

    with open(article_file, "r", encoding="utf-8") as f:
        article = json.load(f)
    logger.info("已加载本地素材: %s", slug)

    image_paths = []
    if image_dir.exists():
        for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
            image_paths.extend(sorted(image_dir.glob(ext)))
    image_paths = [str(p) for p in image_paths[:9]]
    logger.info("本地图片 %d 张", len(image_paths))

    llm = _make_llm(settings)
    gen = ZhihuContentGenerator(llm)
    content = gen.generate_from_article(article, image_paths)
    llm.close()

    _print_zh_preview(content)
    _save_zh_content_local(content, slug, asset_dir)

    if not args.publish:
        return
    if not args.yes:
        confirm = input("\n确认发布到知乎？(y/N): ").strip().lower()
        if confirm != "y":
            print("已取消发布。")
            return
    _do_zh_publish(content, args.headless)


def cmd_zh_local_list(args):
    output_dir = Path(get_settings().output_dir)
    if not output_dir.exists():
        print(f"  共享素材目录不存在: {output_dir}")
        return
    slugs = sorted([d.name for d in output_dir.iterdir() if d.is_dir() and (d / "article.json").exists()])
    if not slugs:
        print("  暂无共享素材")
        return
    print(f"\n  共 {len(slugs)} 个素材:")
    for s in slugs:
        zh_json = output_dir / s / "zh_content.json"
        status = "✓" if zh_json.exists() else " "
        print(f"  [{status}] {s}")
    print(f"\n  [✓] = 已生成知乎文案")


def cmd_zh_debug(args):
    from zhihu.publisher import diagnose_zhihu_page
    diagnose_zhihu_page()


# ══════════════════════════════════════════════════════════════
#  视频号 (channels) 子命令
# ══════════════════════════════════════════════════════════════

def _channels_output_dir() -> Path:
    return Path(get_settings().output_dir)


def _print_channels_preview(content: ChannelsContent):
    sep = "=" * 60
    print(f"\n{sep}")
    print("  微信视频号动态 预览")
    print(sep)
    if content.title:
        print(f"  标题: {content.title}")
    print(f"  正文 ({len(content.body)} 字):")
    for line in content.body.split("\n")[:8]:
        print(f"    {line}")
    if content.body.count("\n") > 8:
        print("    ...")
    if content.hashtags:
        print(f"  话题: {' '.join('#' + t for t in content.hashtags)}")
    print(f"  图片: {len(content.image_urls)} 张")
    print(sep)


def _save_channels_content_local(content: ChannelsContent, slug: str, asset_dir: Path):
    filepath = asset_dir / "channels_content.json"
    save_json(filepath, content.to_dict())
    logger.info("视频号文案已保存: %s", filepath)


def _do_channels_publish(content: ChannelsContent, headless: bool = False) -> bool:
    from channels.publisher import publish_channels_text
    logger.info("开始自动发布到视频号...")
    success = publish_channels_text(
        body=content.full_text(),
        image_sources=content.image_urls,
        title=content.title,
        headless=headless,
    )
    if success:
        print("\n  ✓ 视频号发布成功！")
    else:
        print("\n  ✗ 视频号发布失败，请查看日志")
    return success


def cmd_channels_republish(args):
    filepath = Path(args.json_file)
    output_dir = _channels_output_dir()
    if not filepath.exists():
        filepath = output_dir / args.json_file
    if filepath.is_dir():
        filepath = filepath / "channels_content.json"
    if not filepath.exists():
        print(f"  文件不存在: {args.json_file}")
        print(f"  （尝试路径: {filepath}）")
        sys.exit(1)
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    content = ChannelsContent.from_dict(data)
    logger.info("加载视频号文案: %s", content.summary())
    _print_channels_preview(content)
    if not args.yes:
        confirm = input("\n确认发布到视频号？(y/N): ").strip().lower()
        if confirm != "y":
            print("已取消发布。")
            return
    _do_channels_publish(content, args.headless)


def cmd_channels_local(args):
    settings = get_settings()
    settings.check_or_exit()
    output_dir = Path(settings.output_dir)
    slug = args.slug
    asset_dir = output_dir / slug
    article_file = asset_dir / "article.json"
    image_dir = asset_dir / "images"

    if not article_file.exists():
        available = [d.name for d in output_dir.iterdir() if d.is_dir() and (d / "article.json").exists()] if output_dir.exists() else []
        print(f"\n  素材不存在: {article_file}")
        if available:
            print(f"\n  可用的 slug:")
            for s in sorted(available):
                print(f"    - {s}")
        sys.exit(1)

    with open(article_file, "r", encoding="utf-8") as f:
        article = json.load(f)
    logger.info("已加载本地素材: %s", slug)

    image_paths = []
    if image_dir.exists():
        for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
            image_paths.extend(sorted(image_dir.glob(ext)))
    image_paths = [str(p) for p in image_paths[:9]]
    logger.info("本地图片 %d 张", len(image_paths))

    llm = _make_llm(settings)
    gen = ChannelsContentGenerator(llm)
    content = gen.generate_from_article(article, image_paths)
    llm.close()

    _print_channels_preview(content)
    _save_channels_content_local(content, slug, asset_dir)

    if not args.publish:
        return
    if not args.yes:
        confirm = input("\n确认发布到视频号？(y/N): ").strip().lower()
        if confirm != "y":
            print("已取消发布。")
            return
    _do_channels_publish(content, args.headless)


def cmd_channels_local_list(args):
    output_dir = Path(get_settings().output_dir)
    if not output_dir.exists():
        print(f"  共享素材目录不存在: {output_dir}")
        return
    slugs = sorted([d.name for d in output_dir.iterdir() if d.is_dir() and (d / "article.json").exists()])
    if not slugs:
        print("  暂无共享素材")
        return
    print(f"\n  共 {len(slugs)} 个素材:")
    for s in slugs:
        ch_json = output_dir / s / "channels_content.json"
        status = "✓" if ch_json.exists() else " "
        print(f"  [{status}] {s}")
    print(f"\n  [✓] = 已生成视频号文案")


def cmd_channels_debug(args):
    from channels.publisher import diagnose_channels_page
    diagnose_channels_page()


# ══════════════════════════════════════════════════════════════
#  微博 (wb) 子命令
# ══════════════════════════════════════════════════════════════

def _wb_output_dir() -> Path:
    return Path(get_settings().output_dir)


def _print_wb_preview(content: WeiboContent):
    sep = "=" * 60
    print(f"\n{sep}")
    print("  微博文章 预览")
    print(sep)
    print(f"  标题: {content.title}")
    if content.summary:
        print(f"  摘要: {content.summary[:80]}...")
    print(f"  正文 ({len(content.body)} 字):")
    for line in content.body.split("\n")[:8]:
        print(f"    {line}")
    if content.body.count("\n") > 8:
        print("    ...")
    if content.tags:
        print(f"  话题: {' '.join('#' + t + '#' for t in content.tags)}")
    print(f"  封面: {len(content.cover_urls)} 张")
    print(sep)


def _save_wb_content(content: WeiboContent, post_id: int):
    output_dir = _wb_output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    filepath = output_dir / f"wb_post_{post_id}.json"
    save_json(filepath, content.to_dict())
    logger.info("微博文案已保存: %s", filepath)


def _save_wb_content_local(content: WeiboContent, slug: str, asset_dir: Path):
    filepath = asset_dir / "wb_content.json"
    save_json(filepath, content.to_dict())
    logger.info("微博文案已保存: %s", filepath)


def _do_wb_publish(content: WeiboContent, headless: bool = False) -> bool:
    from weibo.publisher import publish_weibo_article
    logger.info("开始自动发布到微博...")
    success = publish_weibo_article(content, headless=headless)
    if success:
        print("\n  ✓ 微博文章发布成功！")
    else:
        print("\n  ✗ 微博文章发布失败，请查看日志")
    return success


def _do_wb_publish_video(video_path: str, title: str, body: str, headless: bool = False) -> bool:
    from weibo.publisher import publish_weibo_video
    logger.info("开始自动发布视频到微博...")
    success = publish_weibo_video(video_path, title, body, headless=headless)
    if success:
        print("\n  ✓ 微博视频发布成功！")
    else:
        print("\n  ✗ 微博视频发布失败，请查看日志")
    return success


def cmd_wb_generate(args):
    settings = get_settings()
    settings.check_or_exit()
    wp = _make_wp(settings)
    post = wp.get_post(args.post_id)
    wp.close()
    logger.info("已获取文章: [%d] %s", post.id, post.title)

    llm = _make_llm(settings)
    gen = WeiboContentGenerator(llm)
    content = gen.generate_from_post(post)
    llm.close()

    _print_wb_preview(content)
    _save_wb_content(content, post.id)


def cmd_wb_publish(args):
    settings = get_settings()
    settings.check_or_exit()
    wp = _make_wp(settings)
    post = wp.get_post(args.post_id)
    wp.close()

    llm = _make_llm(settings)
    gen = WeiboContentGenerator(llm)
    content = gen.generate_from_post(post)
    llm.close()

    _print_wb_preview(content)
    _save_wb_content(content, post.id)

    if not args.yes:
        confirm = input("\n确认发布到微博？(y/N): ").strip().lower()
        if confirm != "y":
            print("已取消发布。")
            return
    _do_wb_publish(content, args.headless)


def cmd_wb_republish(args):
    filepath = Path(args.json_file)
    output_dir = _wb_output_dir()
    if not filepath.exists():
        filepath = output_dir / args.json_file
    if filepath.is_dir():
        filepath = filepath / "wb_content.json"
    if not filepath.exists():
        print(f"  文件不存在: {args.json_file}")
        print(f"  （尝试路径: {filepath}）")
        sys.exit(1)
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)
    content = WeiboContent.from_dict(data)
    logger.info("加载微博文案: %s, 封面 %d 张", content.title, len(content.cover_urls))
    _print_wb_preview(content)
    if not args.yes:
        confirm = input("\n确认发布到微博？(y/N): ").strip().lower()
        if confirm != "y":
            print("已取消发布。")
            return
    _do_wb_publish(content, args.headless)


def cmd_wb_local(args):
    settings = get_settings()
    settings.check_or_exit()
    output_dir = Path(settings.output_dir)
    slug = args.slug
    asset_dir = output_dir / slug
    article_file = asset_dir / "article.json"
    image_dir = asset_dir / "images"

    if not article_file.exists():
        available = [d.name for d in output_dir.iterdir() if d.is_dir() and (d / "article.json").exists()] if output_dir.exists() else []
        print(f"\n  素材不存在: {article_file}")
        if available:
            print(f"\n  可用的 slug:")
            for s in sorted(available):
                print(f"    - {s}")
        sys.exit(1)

    with open(article_file, "r", encoding="utf-8") as f:
        article = json.load(f)
    logger.info("已加载本地素材: %s", slug)

    image_paths = []
    if image_dir.exists():
        for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
            image_paths.extend(sorted(image_dir.glob(ext)))
    image_paths = [str(p) for p in image_paths[:9]]
    logger.info("本地图片 %d 张", len(image_paths))

    llm = _make_llm(settings)
    gen = WeiboContentGenerator(llm)
    content = gen.generate_from_article(article, image_paths)
    llm.close()

    _print_wb_preview(content)
    _save_wb_content_local(content, slug, asset_dir)

    if not args.publish:
        return
    if not args.yes:
        confirm = input("\n确认发布到微博？(y/N): ").strip().lower()
        if confirm != "y":
            print("已取消发布。")
            return
    _do_wb_publish(content, args.headless)


def cmd_wb_local_list(args):
    output_dir = Path(get_settings().output_dir)
    if not output_dir.exists():
        print(f"  共享素材目录不存在: {output_dir}")
        return
    slugs = sorted([d.name for d in output_dir.iterdir() if d.is_dir() and (d / "article.json").exists()])
    if not slugs:
        print("  暂无共享素材")
        return
    print(f"\n  共 {len(slugs)} 个素材:")
    for s in slugs:
        wb_json = output_dir / s / "wb_content.json"
        status = "✓" if wb_json.exists() else " "
        print(f"  [{status}] {s}")
    print(f"\n  [✓] = 已生成微博文案")


def cmd_wb_video(args):
    """微博视频发布：从已有 JSON 文案 + 已有视频文件发布"""
    output_dir = Path(get_settings().output_dir)
    source = args.source
    filepath = Path(source)

    if filepath.exists() or (output_dir / source).exists():
        if not filepath.exists():
            filepath = output_dir / source
    else:
        try:
            post_id = int(source)
            filepath = output_dir / f"wb_post_{post_id}.json"
        except ValueError:
            print(f"无效参数: {source}")
            sys.exit(1)

    if not filepath.exists():
        print(f"文件不存在: {filepath}")
        sys.exit(1)

    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 查找视频文件
    post_id = data.get("post_id", 0)
    video_path = data.get("final_video_path") or data.get("video_path")
    if not video_path or not Path(video_path).exists():
        candidates = [
            output_dir / f"video_{post_id}" / f"wb_video_{post_id}_final.mp4",
            output_dir / f"video_{post_id}" / f"wb_video_{post_id}.mp4",
            output_dir / f"video_{post_id}" / f"xhs_video_{post_id}_final.mp4",
            output_dir / f"video_{post_id}" / f"xhs_video_{post_id}.mp4",
        ]
        video_path = None
        for c in candidates:
            if c.exists():
                video_path = str(c)
                break
        if not video_path:
            print("未找到视频文件。请先用 xhs video 或其他命令生成视频。")
            sys.exit(1)

    title = data.get("title", "")[:40]
    body = data.get("body", "")
    tags = data.get("tags", [])
    full_body = body
    if tags:
        full_body = f"{body}\n\n{' '.join(f'#{t}#' for t in tags)}"

    print(f"\n发布视频到微博")
    print(f"视频: {video_path}")
    print(f"标题: {title}")
    print("=" * 50)

    if not args.yes:
        confirm = input("\n确认发布到微博？(y/N): ").strip().lower()
        if confirm != "y":
            print("已取消发布。")
            return
    _do_wb_publish_video(video_path, title, full_body, args.headless)


def cmd_wb_debug(args):
    from weibo.publisher import diagnose_weibo_page
    diagnose_weibo_page()


# ══════════════════════════════════════════════════════════════
#  故事视频 (story) 子命令
# ══════════════════════════════════════════════════════════════

def cmd_story_generate(args):
    """通过 LLM 生成分镜脚本 + 视频 + 全平台文案"""
    settings = get_settings()
    settings.check_or_exit()

    if not settings.volc_ak or not settings.volc_sk:
        print("  错误: VOLC_AK / VOLC_SK 未配置")
        sys.exit(1)

    theme = args.theme
    scene = args.scene or ""
    style = args.style or "epic cinematic"
    num_shots = args.shots
    character = args.character or ""
    slug = args.slug or theme.replace(" ", "-")
    output_dir = Path(settings.output_dir) / slug

    storyboard_json = None
    if args.storyboard:
        storyboard_json = Path(args.storyboard)
        if not storyboard_json.exists():
            storyboard_json = output_dir / args.storyboard
        if not storyboard_json.exists():
            print(f"  分镜脚本文件不存在: {args.storyboard}")
            sys.exit(1)

    video_ratio = args.ratio
    video_frames = 121 if args.duration == 5 else 241   # 5s or 10s per shot
    voice = args.voice
    voice_rate = args.rate

    print("\n" + "=" * 60)
    print("  故事视频生成器")
    print("=" * 60)
    print(f"  主题:     {theme}")
    if scene:
        print(f"  场景:     {scene}")
    print(f"  风格:     {style}")
    print(f"  镜头:     {num_shots} x {args.duration}s = {num_shots * args.duration}s")
    print(f"  比例:     {video_ratio}")
    if character:
        print(f"  角色:     {character}")
    print(f"  输出:     {output_dir}")
    print("=" * 60)

    llm = _make_llm(settings)

    t0 = time.monotonic()
    result = run_story_video(
        settings=settings,
        llm=llm,
        theme=theme,
        output_dir=output_dir,
        scene=scene,
        style=style,
        num_shots=num_shots,
        character_desc=character,
        video_ratio=video_ratio,
        video_frames=video_frames,
        voice=voice,
        voice_rate=voice_rate,
        storyboard_json=storyboard_json,
    )
    llm.close()

    elapsed = time.monotonic() - t0
    logger.info("总耗时: %.1fs", elapsed)

    # 发布
    if args.publish and result.final_video_path:
        if not args.yes:
            confirm = input("\n确认发布到所有平台？(y/N): ").strip().lower()
            if confirm != "y":
                print("已取消发布。文案已保存，可使用各平台 republish 命令单独发布。")
                return

        _story_publish_all(result, args.headless)


def cmd_story_publish(args):
    """发布已有的故事视频到所有平台"""
    settings = get_settings()
    output_dir = Path(args.dir)
    if not output_dir.exists():
        output_dir = Path(settings.output_dir) / args.dir
    if not output_dir.exists():
        print(f"  目录不存在: {args.dir}")
        sys.exit(1)

    # 查找视频文件
    video_path = None
    for name in ["final_final.mp4", "final.mp4", "concat.mp4"]:
        candidate = output_dir / name
        if candidate.exists():
            video_path = candidate
            break
    # 尝试从 JSON 读取
    if not video_path:
        storyboard_file = output_dir / "storyboard.json"
        if storyboard_file.exists():
            with open(storyboard_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            vp = data.get("video_path", "")
            if vp and Path(vp).exists():
                video_path = Path(vp)

    if not video_path or not video_path.exists():
        print("  未找到视频文件。请先运行 story generate 生成视频。")
        sys.exit(1)

    print(f"\n  视频: {video_path}")
    print(f"  目录: {output_dir}")

    # 构建 result 对象
    from shared.media.story_video import StoryVideoResult, StoryBoard, PlatformContent
    storyboard_file = output_dir / "storyboard.json"
    if storyboard_file.exists():
        with open(storyboard_file, "r", encoding="utf-8") as f:
            board = StoryBoard.from_dict(json.load(f))
    else:
        board = StoryBoard(project="", theme="", scene="", style="")

    result = StoryVideoResult(
        storyboard=board,
        final_video_path=video_path,
        output_dir=output_dir,
    )

    # 加载平台文案
    file_map = {
        "xhs": "xhs_content.json",
        "douyin": "dy_content.json",
        "channels": "channels_content.json",
        "zhihu": "zh_content.json",
        "toutiao": "toutiao_content.json",
        "weibo": "wb_content.json",
    }
    for key, filename in file_map.items():
        fp = output_dir / filename
        if fp.exists():
            with open(fp, "r", encoding="utf-8") as f:
                result.platform_contents[key] = PlatformContent.from_dict(json.load(f))

    if not result.platform_contents:
        print("  未找到平台文案 JSON。请先运行 story generate。")
        sys.exit(1)

    if not args.yes:
        confirm = input("\n确认发布到所有平台？(y/N): ").strip().lower()
        if confirm != "y":
            print("已取消发布。")
            return

    _story_publish_all(result, args.headless)


def cmd_story_list(args):
    """列出已生成的故事视频"""
    output_dir = Path(get_settings().output_dir)
    if not output_dir.exists():
        print(f"  输出目录不存在: {output_dir}")
        return
    projects = []
    for d in sorted(output_dir.iterdir()):
        sb = d / "storyboard.json"
        if d.is_dir() and sb.exists():
            with open(sb, "r", encoding="utf-8") as f:
                data = json.load(f)
            has_video = any(
                (d / n).exists() for n in ["final_final.mp4", "final.mp4", "concat.mp4"]
            ) or (data.get("video_path", "") and Path(data.get("video_path", "")).exists())
            has_content = any((d / f).exists() for f in ["xhs_content.json", "dy_content.json"])
            projects.append((d.name, data.get("project", "-"), len(data.get("shots", [])), has_video, has_content))

    if not projects:
        print("  暂无故事视频项目")
        return

    print(f"\n{'slug':<35}  {'镜头':>4}  {'视频':>4}  {'文案':>4}  项目名称")
    print("-" * 90)
    for slug, project, shots, has_video, has_content in projects:
        v = "✓" if has_video else " "
        c = "✓" if has_content else " "
        print(f"{slug:<35}  {shots:>4}  {v:>4}  {c:>4}  {project}")
    print(f"\n共 {len(projects)} 个项目  目录: {output_dir}")


def _story_publish_all(result: StoryVideoResult, headless: bool = False):
    """将故事视频发布到所有平台"""
    if not result.final_video_path or not result.final_video_path.exists():
        print("  无视频文件可发布")
        return

    video_path = str(result.final_video_path)
    results = {}

    platform_publishers = {
        "xhs": ("小红书", lambda pc, vp: _do_xhs_publish_video(vp, pc.title[:20], pc.full_text())),
        "douyin": ("抖音", lambda pc, vp: _story_publish_dy_video(vp, pc.title[:30], pc.full_text())),
        "channels": ("视频号", lambda pc, vp: _story_publish_channels_video(vp, pc.full_text())),
        "zhihu": ("知乎", lambda pc, vp: _story_publish_zh_video(vp, pc.title, pc.body[:500])),
        "toutiao": ("头条", lambda pc, vp: _story_publish_toutiao_video(vp, pc.title, pc.body[:500])),
        "weibo": ("微博", lambda pc, vp: _do_wb_publish_video(vp, pc.title[:40], pc.full_text())),
    }

    print("\n" + "=" * 60)
    print("  发布到全平台")
    print("=" * 60)

    for key, (name, publish_fn) in platform_publishers.items():
        pc = result.platform_contents.get(key)
        if not pc:
            logger.info("[%s] 无文案，跳过", name)
            continue
        try:
            logger.info("[%s] 开始发布...", name)
            ok = publish_fn(pc, video_path)
            results[name] = ok
            status = "✓" if ok else "?"
            print(f"  [{status}] {name}")
        except Exception as e:
            results[name] = False
            logger.error("[%s] 发布失败: %s", name, e, exc_info=True)
            print(f"  [✗] {name}: {e}")
        time.sleep(5)

    success = sum(1 for v in results.values() if v)
    print(f"\n  总计: {success}/{len(results)} 个平台发布成功")
    print("=" * 60)


def _story_publish_dy_video(video_path: str, title: str, body: str) -> bool:
    from douyin.publisher import publish_douyin_video
    return publish_douyin_video(video_path, title, body, headless=False)


def _story_publish_channels_video(video_path: str, body: str) -> bool:
    from channels.publisher import publish_channels_video
    return publish_channels_video(video_path, body, headless=False)


def _story_publish_zh_video(video_path: str, title: str, body: str) -> bool:
    from zhihu.publisher import publish_zhihu_video
    return publish_zhihu_video(video_path, title, body, headless=False)


def _story_publish_toutiao_video(video_path: str, title: str, body: str) -> bool:
    from toutiao.publisher import publish_toutiao_video
    return publish_toutiao_video(video_path, title, body, headless=False)


# ══════════════════════════════════════════════════════════════
#  全流程子命令（WordPress → 多平台）
# ══════════════════════════════════════════════════════════════

def _publish_to_platform(name: str, publish_fn, content, headless: bool) -> bool:
    """安全地发布到单个平台，返回成功与否"""
    try:
        return publish_fn(content, headless)
    except Exception as e:
        logger.error("%s 发布失败: %s", name, e, exc_info=True)
        print(f"\n  ✗ {name} 发布失败: {e}")
        return False


def _print_all_report(results: dict):
    sep = "=" * 60
    print(f"\n{sep}")
    print("  全平台发布结果")
    print(sep)
    for platform, success in results.items():
        status = "✓" if success else "✗"
        print(f"  [{status}] {platform}")
    success_count = sum(1 for v in results.values() if v)
    total = len(results)
    print(f"\n  总计: {success_count}/{total} 个平台发布成功")
    print(sep)


def cmd_all(args):
    """WordPress 发布 → 多平台发布全流程"""
    settings = get_settings()
    settings.check_or_exit()

    # Step 1: WordPress 发布
    print("\n" + "=" * 60)
    print("  Step 1: 发布到 WordPress")
    print("=" * 60)
    wp_result = cmd_wp(args)

    slug = wp_result.get("slug", "")
    asset_dir = Path(wp_result.get("asset_dir", ""))
    if not slug or not asset_dir.exists():
        logger.error("WordPress 发布结果异常，无法继续多平台流程")
        return

    article_file = asset_dir / "article.json"
    with open(article_file, "r", encoding="utf-8") as f:
        article = json.load(f)

    image_dir = asset_dir / "images"
    image_paths = []        # 16:9 横版（WordPress/头条/知乎）
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
            # 统一种子：同一篇文章的所有图片使用相同基础 seed，确保视觉一致性
            base_seed = sum(ord(c) for c in article.get("slug", article.get("title", ""))) % 2147483647
            logger.info("生成竖版配图（3:4, 1080x1440, seed=%d）...", base_seed)
            for i, spec in enumerate(v_specs):
                vp = v_image_dir / f"{spec['role']}_{i:02d}.png"
                # 替换 prompt 中的 16:9 为 3:4 竖版描述
                v_prompt = spec["prompt"].replace("16:9", "3:4 vertical portrait").replace("Wide ", "Vertical portrait ")
                gen_path = img_gen.generate(v_prompt, vp, width=1080, height=1440, seed=base_seed + i)
                if gen_path:
                    vertical_image_paths.append(str(gen_path))
            logger.info("竖版配图生成完成: %d 张", len(vertical_image_paths))
        else:
            logger.info("未配置火山引擎密钥，竖版平台使用横版图片")
    except Exception as e:
        logger.warning("竖版配图生成失败，使用横版图片: %s", e)

    # 竖版平台使用竖版图（如果有），否则回退到横版
    v_images = vertical_image_paths if vertical_image_paths else image_paths

    llm = _make_llm(settings)
    results = {}

    # Step 2: 生成各平台文案
    print("\n" + "=" * 60)
    print("  Step 2: 生成各平台文案")
    print("=" * 60)

    # 小红书（竖版 3:4）
    try:
        xhs_gen = XHSContentGenerator(llm)
        xhs_content = xhs_gen.generate_from_article(article, v_images)
        _print_xhs_preview(xhs_content)
        _save_xhs_content_local(xhs_content, slug, asset_dir)
    except Exception as e:
        logger.error("小红书文案生成失败: %s", e)
        xhs_content = None

    # 抖音（竖版 3:4）
    try:
        dy_gen = DouyinContentGenerator(llm)
        dy_content = dy_gen.generate_from_article(article, v_images)
        _print_dy_preview(dy_content)
        _save_dy_content_local(dy_content, slug, asset_dir)
    except Exception as e:
        logger.error("抖音文案生成失败: %s", e)
        dy_content = None

    # 头条（横版 16:9）
    try:
        tt_gen = ToutiaoContentGenerator(llm)
        tt_content = tt_gen.generate_from_article(article, image_paths)
        _print_toutiao_preview(tt_content)
        _save_toutiao_content_local(tt_content, slug, asset_dir)
    except Exception as e:
        logger.error("头条文案生成失败: %s", e)
        tt_content = None

    # 知乎（横版 16:9）
    try:
        zh_gen = ZhihuContentGenerator(llm)
        zh_content = zh_gen.generate_from_article(article, image_paths)
        _print_zh_preview(zh_content)
        _save_zh_content_local(zh_content, slug, asset_dir)
    except Exception as e:
        logger.error("知乎文案生成失败: %s", e)
        zh_content = None

    # 视频号（竖版 3:4）
    try:
        ch_gen = ChannelsContentGenerator(llm)
        ch_content = ch_gen.generate_from_article(article, v_images)
        _print_channels_preview(ch_content)
        _save_channels_content_local(ch_content, slug, asset_dir)
    except Exception as e:
        logger.error("视频号文案生成失败: %s", e)
        ch_content = None

    # 微博（横版 16:9）
    try:
        wb_gen = WeiboContentGenerator(llm)
        wb_content = wb_gen.generate_from_article(article, image_paths)
        _print_wb_preview(wb_content)
        _save_wb_content_local(wb_content, slug, asset_dir)
    except Exception as e:
        logger.error("微博文案生成失败: %s", e)
        wb_content = None

    llm.close()

    # Step 3: 发布到各平台
    if not args.yes:
        confirm = input("\n确认发布到所有平台？(y/N): ").strip().lower()
        if confirm != "y":
            print("已取消发布。文案已保存，可使用各平台的 republish 命令单独发布。")
            return

    print("\n" + "=" * 60)
    print("  Step 3: 发布到各平台")
    print("=" * 60)

    headless = args.headless

    if xhs_content:
        results["小红书"] = _publish_to_platform("小红书", _do_xhs_publish, xhs_content, headless)
    if dy_content:
        results["抖音"] = _publish_to_platform("抖音", _do_dy_publish, dy_content, headless)
    if tt_content:
        results["头条"] = _publish_to_platform("头条", _do_toutiao_publish, tt_content, headless)
    if zh_content:
        results["知乎"] = _publish_to_platform("知乎", _do_zh_publish, zh_content, headless)
    if ch_content:
        results["视频号"] = _publish_to_platform("视频号", _do_channels_publish, ch_content, headless)
    if wb_content:
        results["微博"] = _publish_to_platform("微博", _do_wb_publish, wb_content, headless)

    _print_all_report(results)


# ══════════════════════════════════════════════════════════════
#  CLI 解析
# ══════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        description="Aineoo 内容自动发布工具 —— WordPress + 多平台统一入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # ── wp 子命令 ──
    p_wp = subparsers.add_parser("wp", help="生成文章并发布到 WordPress")
    p_wp.add_argument("--prompt", default="", help="完整提示词")
    p_wp.add_argument("--topic", default="", help="主题（自动套用模板）")
    p_wp.add_argument("--title", default="", help="自定义标题")
    p_wp.add_argument("--slug", default="", help="自定义 URL 别名")
    p_wp.add_argument("--categories", default=settings.default_categories, help="分类，逗号分隔")
    p_wp.add_argument("--tags", default=settings.default_tags, help="标签，逗号分隔")
    p_wp.add_argument("--status", default="publish", choices=["publish", "draft"])
    p_wp.add_argument("--dry-run", action="store_true", help="仅生成本地预览")
    p_wp.add_argument("--min-quality", type=int, default=75)
    p_wp.add_argument("--strict-quality", action="store_true")
    p_wp.add_argument("--skip-verify", action="store_true")
    p_wp.add_argument("--related-limit", type=int, default=3)
    p_wp.set_defaults(use_deepseek=settings.deepseek_enabled)
    p_wp.add_argument("--use-deepseek", dest="use_deepseek", action="store_true")
    p_wp.add_argument("--no-deepseek", dest="use_deepseek", action="store_false")
    p_wp.add_argument("--timeout", type=int, default=settings.request_timeout)
    p_wp.add_argument("--max-images", type=int, default=settings.max_content_images)
    p_wp.add_argument("--video", action="store_true", help="同时生成视频并嵌入文章（火山引擎即梦AI）")
    p_wp.add_argument("--video-ratio", type=str, default="16:9", help="视频画面比例（默认 16:9 横屏）")
    p_wp.add_argument("--avatar", action="store_true", help="生成数字人解读视频（fal.ai）")
    p_wp.add_argument("--avatar-image", type=str, default="", help="数字人形象照 URL（公网可访问的正面照片）")
    p_wp.set_defaults(func=cmd_wp)

    # ── xhs 子命令 ──
    p_xhs = subparsers.add_parser("xhs", help="小红书相关操作")
    xhs_sub = p_xhs.add_subparsers(dest="xhs_command", help="小红书子命令")

    # xhs list
    p_xl = xhs_sub.add_parser("list", help="列出 WordPress 文章")
    p_xl.add_argument("-n", "--count", type=int, default=10)
    p_xl.add_argument("-s", "--search", type=str, default="")
    p_xl.set_defaults(func=cmd_xhs_list)

    # xhs generate
    for name in ("generate", "preview"):
        p = xhs_sub.add_parser(name, help="生成小红书文案")
        p.add_argument("post_id", type=int)
        p.set_defaults(func=cmd_xhs_generate)

    # xhs publish
    p_xp = xhs_sub.add_parser("publish", help="生成并发布到小红书")
    p_xp.add_argument("post_id", type=int)
    p_xp.add_argument("-y", "--yes", action="store_true")
    p_xp.add_argument("--headless", action="store_true")
    p_xp.set_defaults(func=cmd_xhs_publish)

    # xhs republish
    p_xrp = xhs_sub.add_parser("republish", help="从 JSON 直接发布")
    p_xrp.add_argument("json_file", type=str)
    p_xrp.add_argument("-y", "--yes", action="store_true")
    p_xrp.add_argument("--headless", action="store_true")
    p_xrp.set_defaults(func=cmd_xhs_republish)

    # xhs batch
    p_xb = xhs_sub.add_parser("batch", help="批量处理")
    p_xb.add_argument("post_ids", type=int, nargs="+")
    p_xb.add_argument("--publish", action="store_true")
    p_xb.add_argument("--headless", action="store_true")
    p_xb.set_defaults(func=cmd_xhs_batch)

    # xhs local
    p_xlo = xhs_sub.add_parser("local", help="从共享素材生成文案")
    p_xlo.add_argument("slug", type=str)
    p_xlo.add_argument("--publish", action="store_true")
    p_xlo.add_argument("-y", "--yes", action="store_true")
    p_xlo.add_argument("--headless", action="store_true")
    p_xlo.set_defaults(func=cmd_xhs_local)

    # xhs local-list
    p_xll = xhs_sub.add_parser("local-list", help="列出共享素材")
    p_xll.set_defaults(func=cmd_xhs_local_list)

    # xhs video
    p_xv = xhs_sub.add_parser("video", help="生成视频（自动配音）")
    p_xv.add_argument("source", type=str, help="文章 ID 或 JSON 文件路径")
    p_xv.add_argument("--ratio", type=str, default="9:16", help="画面比例")
    p_xv.add_argument("--prompt", type=str, default=None, help="自定义视频 prompt")
    p_xv.add_argument("--no-audio", action="store_true", help="不配音（仅生成无声视频）")
    p_xv.add_argument("--no-subtitle", action="store_true", help="不加字幕（仅配音无字幕）")
    p_xv.add_argument("--voice", type=str, default="zh-CN-XiaoyiNeural",
                       help="TTS 语音（默认 zh-CN-XiaoyiNeural 女声）")
    p_xv.add_argument("--rate", type=str, default="+0%", help="语速（如 +10%%, -5%%）")
    p_xv.add_argument("--script", type=str, default=None, help="自定义口播稿")
    p_xv.add_argument("--avatar", action="store_true", help="添加数字人（需配置 FAL_KEY）")
    p_xv.add_argument("--avatar-image", type=str, default=None,
                       help="数字人参考人像图路径")
    p_xv.add_argument("--avatar-position", type=str, default="bottom_right",
                       choices=["bottom_right", "bottom_left", "bottom_center", "center_right"],
                       help="数字人叠加位置（默认 bottom_right）")
    p_xv.add_argument("--avatar-scale", type=float, default=0.3,
                       help="数字人缩放比例 0.0-1.0（默认 0.3 即 30%%）")
    p_xv.add_argument("--publish", action="store_true", help="生成后自动发布")
    p_xv.add_argument("--headless", action="store_true")
    p_xv.set_defaults(func=cmd_xhs_video)

    # xhs audio（为已有视频配音）
    p_xa = xhs_sub.add_parser("audio", help="为已有视频添加配音")
    p_xa.add_argument("source", type=str, help="JSON 文件路径或文章 ID")
    p_xa.add_argument("--no-subtitle", action="store_true", help="不加字幕")
    p_xa.add_argument("--voice", type=str, default="zh-CN-XiaoyiNeural",
                       help="TTS 语音（默认 zh-CN-XiaoyiNeural 女声）")
    p_xa.add_argument("--rate", type=str, default="+0%", help="语速（如 +10%%, -5%%）")
    p_xa.add_argument("--script", type=str, default=None, help="自定义口播稿")
    p_xa.add_argument("--avatar", action="store_true", help="添加数字人（需配置 FAL_KEY）")
    p_xa.add_argument("--avatar-image", type=str, default=None,
                       help="数字人参考人像图路径")
    p_xa.add_argument("--avatar-position", type=str, default="bottom_right",
                       choices=["bottom_right", "bottom_left", "bottom_center", "center_right"],
                       help="数字人叠加位置")
    p_xa.add_argument("--avatar-scale", type=float, default=0.3,
                       help="数字人缩放比例 0.0-1.0")
    p_xa.set_defaults(func=cmd_xhs_audio)

    # xhs video-publish
    p_xvp = xhs_sub.add_parser("video-publish", help="发布已有视频")
    p_xvp.add_argument("source", type=str)
    p_xvp.add_argument("-y", "--yes", action="store_true")
    p_xvp.add_argument("--headless", action="store_true")
    p_xvp.set_defaults(func=cmd_xhs_video_publish)

    # xhs comment
    p_xc = xhs_sub.add_parser("comment", help="搜索热门笔记并发布引流评论")
    p_xc.add_argument("keyword", type=str, help="搜索关键词（如：AI工具、副业赚钱）")
    p_xc.add_argument("--my-note", type=str, default=None,
                       help="你的笔记 JSON 文件路径、slug 或文章 ID（自动读取标题和摘要）")
    p_xc.add_argument("--my-title", type=str, default="",
                       help="你的笔记标题（手动指定，优先级高于 --my-note）")
    p_xc.add_argument("--my-summary", type=str, default="",
                       help="你的笔记核心内容摘要（手动指定）")
    p_xc.add_argument("--max-comments", type=int, default=None,
                       help=f"最多评论数（默认 {settings.xhs_comment_max}）")
    p_xc.add_argument("--delay", type=int, default=None,
                       help=f"评论间隔秒数（默认 {settings.xhs_comment_delay}s）")
    p_xc.add_argument("--sort", type=str, default="general",
                       choices=["general", "hot", "new"],
                       help="搜索排序（general=综合, hot=最热, new=最新）")
    p_xc.add_argument("--style", type=str, default="professional",
                       choices=["professional", "casual", "enthusiastic"],
                       help="评论风格（professional=专业, casual=随意, enthusiastic=热情）")
    p_xc.add_argument("-y", "--yes", action="store_true", help="跳过确认")
    p_xc.add_argument("--headless", action="store_true", help="无头浏览器模式")
    p_xc.set_defaults(func=cmd_xhs_comment)

    # xhs debug
    p_xd = xhs_sub.add_parser("debug", help="诊断发布页面")
    p_xd.set_defaults(func=cmd_xhs_debug)

    # ── dy 子命令 ──
    p_dy = subparsers.add_parser("dy", help="抖音相关操作")
    dy_sub = p_dy.add_subparsers(dest="dy_command", help="抖音子命令")

    p_dyg = dy_sub.add_parser("generate", help="从 WP 文章生成抖音文案")
    p_dyg.add_argument("post_id", type=int)
    p_dyg.set_defaults(func=cmd_dy_generate)

    p_dyp = dy_sub.add_parser("publish", help="生成并发布到抖音")
    p_dyp.add_argument("post_id", type=int)
    p_dyp.add_argument("-y", "--yes", action="store_true")
    p_dyp.add_argument("--headless", action="store_true")
    p_dyp.add_argument("--no-headless", dest="headless", action="store_false")
    p_dyp.set_defaults(func=cmd_dy_publish)

    p_dyrp = dy_sub.add_parser("republish", help="从 JSON 直接发布到抖音")
    p_dyrp.add_argument("json_file", type=str)
    p_dyrp.add_argument("-y", "--yes", action="store_true")
    p_dyrp.add_argument("--headless", action="store_true")
    p_dyrp.add_argument("--no-headless", dest="headless", action="store_false")
    p_dyrp.set_defaults(func=cmd_dy_republish)

    p_dylo = dy_sub.add_parser("local", help="从共享素材生成抖音文案")
    p_dylo.add_argument("slug", type=str)
    p_dylo.add_argument("--publish", action="store_true")
    p_dylo.add_argument("-y", "--yes", action="store_true")
    p_dylo.add_argument("--headless", action="store_true")
    p_dylo.add_argument("--no-headless", dest="headless", action="store_false")
    p_dylo.set_defaults(func=cmd_dy_local)

    p_dyll = dy_sub.add_parser("local-list", help="列出共享素材（抖音）")
    p_dyll.set_defaults(func=cmd_dy_local_list)

    p_dyd = dy_sub.add_parser("debug", help="诊断抖音发布页面")
    p_dyd.set_defaults(func=cmd_dy_debug)

    # ── toutiao 子命令 ──
    p_tt = subparsers.add_parser("toutiao", help="今日头条相关操作")
    tt_sub = p_tt.add_subparsers(dest="toutiao_command", help="头条子命令")

    p_ttg = tt_sub.add_parser("generate", help="从 WP 文章生成头条文案")
    p_ttg.add_argument("post_id", type=int)
    p_ttg.set_defaults(func=cmd_toutiao_generate)

    p_ttp = tt_sub.add_parser("publish", help="生成并发布到头条")
    p_ttp.add_argument("post_id", type=int)
    p_ttp.add_argument("-y", "--yes", action="store_true")
    p_ttp.add_argument("--headless", action="store_true")
    p_ttp.add_argument("--no-headless", dest="headless", action="store_false")
    p_ttp.set_defaults(func=cmd_toutiao_publish)

    p_ttrp = tt_sub.add_parser("republish", help="从 JSON 直接发布到头条")
    p_ttrp.add_argument("json_file", type=str)
    p_ttrp.add_argument("-y", "--yes", action="store_true")
    p_ttrp.add_argument("--headless", action="store_true")
    p_ttrp.add_argument("--no-headless", dest="headless", action="store_false")
    p_ttrp.set_defaults(func=cmd_toutiao_republish)

    p_ttlo = tt_sub.add_parser("local", help="从共享素材生成头条文案")
    p_ttlo.add_argument("slug", type=str)
    p_ttlo.add_argument("--publish", action="store_true")
    p_ttlo.add_argument("-y", "--yes", action="store_true")
    p_ttlo.add_argument("--headless", action="store_true")
    p_ttlo.add_argument("--no-headless", dest="headless", action="store_false")
    p_ttlo.set_defaults(func=cmd_toutiao_local)

    p_ttll = tt_sub.add_parser("local-list", help="列出共享素材（头条）")
    p_ttll.set_defaults(func=cmd_toutiao_local_list)

    p_ttd = tt_sub.add_parser("debug", help="诊断头条发布页面")
    p_ttd.set_defaults(func=cmd_toutiao_debug)

    # ── zh 子命令 ──
    p_zh = subparsers.add_parser("zh", help="知乎相关操作")
    zh_sub = p_zh.add_subparsers(dest="zh_command", help="知乎子命令")

    p_zhg = zh_sub.add_parser("generate", help="从 WP 文章生成知乎文案")
    p_zhg.add_argument("post_id", type=int)
    p_zhg.set_defaults(func=cmd_zh_generate)

    p_zhp = zh_sub.add_parser("publish", help="生成并发布到知乎")
    p_zhp.add_argument("post_id", type=int)
    p_zhp.add_argument("-y", "--yes", action="store_true")
    p_zhp.add_argument("--headless", action="store_true")
    p_zhp.add_argument("--no-headless", dest="headless", action="store_false")
    p_zhp.set_defaults(func=cmd_zh_publish)

    p_zhrp = zh_sub.add_parser("republish", help="从 JSON 直接发布到知乎")
    p_zhrp.add_argument("json_file", type=str)
    p_zhrp.add_argument("-y", "--yes", action="store_true")
    p_zhrp.add_argument("--headless", action="store_true")
    p_zhrp.add_argument("--no-headless", dest="headless", action="store_false")
    p_zhrp.set_defaults(func=cmd_zh_republish)

    p_zhlo = zh_sub.add_parser("local", help="从共享素材生成知乎文案")
    p_zhlo.add_argument("slug", type=str)
    p_zhlo.add_argument("--publish", action="store_true")
    p_zhlo.add_argument("-y", "--yes", action="store_true")
    p_zhlo.add_argument("--headless", action="store_true")
    p_zhlo.add_argument("--no-headless", dest="headless", action="store_false")
    p_zhlo.set_defaults(func=cmd_zh_local)

    p_zhll = zh_sub.add_parser("local-list", help="列出共享素材（知乎）")
    p_zhll.set_defaults(func=cmd_zh_local_list)

    p_zhd = zh_sub.add_parser("debug", help="诊断知乎发布页面")
    p_zhd.set_defaults(func=cmd_zh_debug)

    # ── channels 子命令 ──
    p_ch = subparsers.add_parser("channels", help="微信视频号相关操作")
    ch_sub = p_ch.add_subparsers(dest="channels_command", help="视频号子命令")

    p_chrp = ch_sub.add_parser("republish", help="从 JSON 直接发布到视频号")
    p_chrp.add_argument("json_file", type=str)
    p_chrp.add_argument("-y", "--yes", action="store_true")
    p_chrp.add_argument("--headless", action="store_true")
    p_chrp.add_argument("--no-headless", dest="headless", action="store_false")
    p_chrp.set_defaults(func=cmd_channels_republish)

    p_chlo = ch_sub.add_parser("local", help="从共享素材生成视频号文案")
    p_chlo.add_argument("slug", type=str)
    p_chlo.add_argument("--publish", action="store_true")
    p_chlo.add_argument("-y", "--yes", action="store_true")
    p_chlo.add_argument("--headless", action="store_true")
    p_chlo.add_argument("--no-headless", dest="headless", action="store_false")
    p_chlo.set_defaults(func=cmd_channels_local)

    p_chll = ch_sub.add_parser("local-list", help="列出共享素材（视频号）")
    p_chll.set_defaults(func=cmd_channels_local_list)

    p_chd = ch_sub.add_parser("debug", help="诊断视频号发布页面")
    p_chd.set_defaults(func=cmd_channels_debug)

    # ── wb 子命令 ──
    p_wb = subparsers.add_parser("wb", help="微博相关操作")
    wb_sub = p_wb.add_subparsers(dest="wb_command", help="微博子命令")

    p_wbg = wb_sub.add_parser("generate", help="从 WP 文章生成微博文案")
    p_wbg.add_argument("post_id", type=int)
    p_wbg.set_defaults(func=cmd_wb_generate)

    p_wbp = wb_sub.add_parser("publish", help="生成并发布到微博")
    p_wbp.add_argument("post_id", type=int)
    p_wbp.add_argument("-y", "--yes", action="store_true")
    p_wbp.add_argument("--headless", action="store_true")
    p_wbp.add_argument("--no-headless", dest="headless", action="store_false")
    p_wbp.set_defaults(func=cmd_wb_publish)

    p_wbrp = wb_sub.add_parser("republish", help="从 JSON 直接发布到微博")
    p_wbrp.add_argument("json_file", type=str)
    p_wbrp.add_argument("-y", "--yes", action="store_true")
    p_wbrp.add_argument("--headless", action="store_true")
    p_wbrp.add_argument("--no-headless", dest="headless", action="store_false")
    p_wbrp.set_defaults(func=cmd_wb_republish)

    p_wblo = wb_sub.add_parser("local", help="从共享素材生成微博文案")
    p_wblo.add_argument("slug", type=str)
    p_wblo.add_argument("--publish", action="store_true")
    p_wblo.add_argument("-y", "--yes", action="store_true")
    p_wblo.add_argument("--headless", action="store_true")
    p_wblo.add_argument("--no-headless", dest="headless", action="store_false")
    p_wblo.set_defaults(func=cmd_wb_local)

    p_wbll = wb_sub.add_parser("local-list", help="列出共享素材（微博）")
    p_wbll.set_defaults(func=cmd_wb_local_list)

    p_wbv = wb_sub.add_parser("video", help="发布视频到微博")
    p_wbv.add_argument("source", type=str, help="JSON 文件路径或文章 ID")
    p_wbv.add_argument("-y", "--yes", action="store_true")
    p_wbv.add_argument("--headless", action="store_true")
    p_wbv.add_argument("--no-headless", dest="headless", action="store_false")
    p_wbv.set_defaults(func=cmd_wb_video)

    p_wbd = wb_sub.add_parser("debug", help="诊断微博发布页面")
    p_wbd.set_defaults(func=cmd_wb_debug)

    # ── story 子命令 ──
    p_story = subparsers.add_parser("story", help="故事视频生成（LLM 分镜脚本 + AI 视频 + 全平台文案）")
    story_sub = p_story.add_subparsers(dest="story_command", help="故事视频子命令")

    p_sg = story_sub.add_parser("generate", help="生成故事视频 + 全平台文案")
    p_sg.add_argument("theme", type=str, help="主题（如 '西游记大闹天宫'）")
    p_sg.add_argument("--scene", type=str, default="", help="场景描述（如 '悟空偷吃蟠桃'）")
    p_sg.add_argument("--style", type=str, default="epic cinematic",
                       help="视觉风格（默认 epic cinematic）")
    p_sg.add_argument("--shots", type=int, default=6, help="分镜数量（默认 6）")
    p_sg.add_argument("--character", type=str, default="", help="角色描述（中文）")
    p_sg.add_argument("--slug", type=str, default="", help="输出目录名（默认用主题）")
    p_sg.add_argument("--ratio", type=str, default="16:9", help="视频比例（默认 16:9）")
    p_sg.add_argument("--duration", type=int, default=5, choices=[5, 10],
                       help="每镜头秒数（默认 5s）")
    p_sg.add_argument("--voice", type=str, default="zh-CN-YunxiNeural",
                       help="TTS 语音（默认 YunxiNeural 男声）")
    p_sg.add_argument("--rate", type=str, default="+0%", help="语速")
    p_sg.add_argument("--storyboard", type=str, default="",
                       help="已有分镜脚本 JSON 路径（跳过 LLM 生成）")
    p_sg.add_argument("--publish", action="store_true", help="生成后自动发布全平台")
    p_sg.add_argument("-y", "--yes", action="store_true", help="跳过确认")
    p_sg.add_argument("--headless", action="store_true")
    p_sg.add_argument("--no-headless", dest="headless", action="store_false")
    p_sg.set_defaults(func=cmd_story_generate)

    p_sp = story_sub.add_parser("publish", help="发布已有故事视频到全平台")
    p_sp.add_argument("dir", type=str, help="故事视频目录（slug 或完整路径）")
    p_sp.add_argument("-y", "--yes", action="store_true")
    p_sp.add_argument("--headless", action="store_true")
    p_sp.add_argument("--no-headless", dest="headless", action="store_false")
    p_sp.set_defaults(func=cmd_story_publish)

    p_sl = story_sub.add_parser("list", help="列出已生成的故事视频")
    p_sl.set_defaults(func=cmd_story_list)

    # ── all 子命令 ──
    p_all = subparsers.add_parser("all", help="WordPress + 全平台发布")
    p_all.add_argument("--prompt", default="", help="完整提示词")
    p_all.add_argument("--topic", default="", help="主题")
    p_all.add_argument("--title", default="", help="自定义标题")
    p_all.add_argument("--slug", default="", help="自定义 URL 别名")
    p_all.add_argument("--categories", default=settings.default_categories)
    p_all.add_argument("--tags", default=settings.default_tags)
    p_all.add_argument("--status", default="publish", choices=["publish", "draft"])
    p_all.add_argument("--dry-run", action="store_true")
    p_all.add_argument("--min-quality", type=int, default=75)
    p_all.add_argument("--strict-quality", action="store_true")
    p_all.add_argument("--skip-verify", action="store_true")
    p_all.add_argument("--related-limit", type=int, default=3)
    p_all.set_defaults(use_deepseek=settings.deepseek_enabled)
    p_all.add_argument("--use-deepseek", dest="use_deepseek", action="store_true")
    p_all.add_argument("--no-deepseek", dest="use_deepseek", action="store_false")
    p_all.add_argument("--timeout", type=int, default=settings.request_timeout)
    p_all.add_argument("--max-images", type=int, default=settings.max_content_images)
    p_all.add_argument("--video", action="store_true", help="同时生成视频并嵌入文章")
    p_all.add_argument("--video-ratio", type=str, default="16:9", help="视频画面比例")
    p_all.add_argument("--avatar", action="store_true", help="生成数字人解读视频（fal.ai）")
    p_all.add_argument("--avatar-image", type=str, default="", help="数字人形象照 URL")
    p_all.add_argument("-y", "--yes", action="store_true")
    p_all.add_argument("--headless", action="store_true")
    p_all.add_argument("--no-headless", dest="headless", action="store_false")
    p_all.set_defaults(func=cmd_all)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # 检查有子命令的平台是否缺少子命令
    sub_commands = {
        "xhs": "xhs_command",
        "dy": "dy_command",
        "toutiao": "toutiao_command",
        "zh": "zh_command",
        "channels": "channels_command",
        "wb": "wb_command",
        "story": "story_command",
    }
    if args.command in sub_commands:
        sub_attr = sub_commands[args.command]
        if not getattr(args, sub_attr, None):
            parser.parse_args([args.command, "--help"])
            sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except ConfigError as e:
        print(f"\n  配置错误: {e}", file=sys.stderr)
        print("请检查 .env 文件，参考 .env.example 填写配置", file=sys.stderr)
        sys.exit(1)
    except QualityError as e:
        logger.error(str(e))
        sys.exit(1)
    except AppBaseError as e:
        logger.error("运行错误: %s", e, exc_info=True)
        print(f"\n  错误: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n\n已中断。")
        sys.exit(130)
    except Exception as e:
        logger.error("未预期错误: %s", e, exc_info=True)
        print(f"\n  未预期错误: {e}", file=sys.stderr)
        print("详情请查看 logs/run.log", file=sys.stderr)
        sys.exit(1)
