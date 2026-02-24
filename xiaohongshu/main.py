"""
小红书推广助手 —— CLI 入口
用法:
    python main.py list                         # 列出最近 WordPress 文章
    python main.py generate <post_id>           # 根据文章 ID 生成小红书文案
    python main.py publish  <post_id>           # 生成文案 + 自动发布到小红书
    python main.py republish <json_file>        # 从已保存的 JSON 文案直接发布
    python main.py batch    <id1> <id2> ...     # 批量生成并发布多篇
    python main.py local-list                   # 列出共享素材目录中可用的文章
    python main.py local   <slug>               # 从共享素材目录生成小红书文案
    python main.py local   <slug> --publish     # 从共享素材目录生成并发布
    python main.py video   <post_id|json_file>   # 生成短视频（自动配音）
    python main.py video   <source> --no-audio  # 生成视频不配音
    python main.py video   <source> --publish   # 生成视频后自动发布到小红书
    python main.py audio   <source>             # 为已有无声视频添加配音
    python main.py video-publish <source>       # 将已有视频发布到小红书
    python main.py debug                        # 诊断发布页面元素
"""

import argparse
import json
import sys
import time
from pathlib import Path

from config import settings
from utils.exceptions import XHSBaseError, ConfigError
from utils.logger import get_logger
from wordpress.client import WordPressClient
from xiaohongshu.content_generator import ContentGenerator, XHSContent
from xiaohongshu.publisher import publish_note, publish_video_note

logger = get_logger("main")

# Windows 控制台 GBK 编码兼容：遇到无法编码的字符（如 emoji）用 ? 替换
import io as _io
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")

OUTPUT_DIR = Path(__file__).resolve().parent / "output"
# 共享素材目录（wordpress 项目生成的文章和图片）
SHARED_OUTPUT_DIR = Path(settings.paths.OUTPUT_DIR)


# ──────────────────────────────────────────────
#  子命令实现
# ──────────────────────────────────────────────

def cmd_list(args):
    """列出最近 WordPress 文章"""
    settings.check_or_exit(require_llm=False)

    client = WordPressClient()
    posts = client.list_posts(per_page=args.count, search=args.search)

    if not posts:
        print("没有找到已发布的文章。")
        return

    print(f"\n{'ID':>6}  {'日期':^12}  标题")
    print("-" * 60)
    for p in posts:
        print(f"{p.id:>6}  {p.date[:10]:^12}  {p.title}")
    print(f"\n共 {len(posts)} 篇文章")


def cmd_generate(args):
    """生成小红书文案"""
    settings.check_or_exit()

    wp = WordPressClient()
    post = wp.get_post(args.post_id)
    logger.info("已获取文章: [%d] %s", post.id, post.title)

    gen = ContentGenerator()
    content = gen.generate(post)

    _print_preview(content)
    _save_content(content, post.id)


def cmd_publish(args):
    """生成文案并发布到小红书"""
    settings.check_or_exit()

    wp = WordPressClient()
    post = wp.get_post(args.post_id)
    logger.info("已获取文章: [%d] %s", post.id, post.title)

    gen = ContentGenerator()
    content = gen.generate(post)

    _print_preview(content)
    _save_content(content, post.id)

    if not args.yes:
        confirm = input("\n确认发布到小红书？(y/N): ").strip().lower()
        if confirm != "y":
            print("已取消发布。")
            return

    _do_publish(content, args.headless)


def cmd_republish(args):
    """从已保存的 JSON 文案直接发布到小红书（不需要重新生成）"""
    filepath = Path(args.json_file)
    if not filepath.exists():
        # 尝试在 output 目录下查找
        filepath = OUTPUT_DIR / args.json_file
    if not filepath.exists():
        print(f"❌ 文件不存在: {args.json_file}")
        sys.exit(1)

    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    content = XHSContent.from_dict(data)
    _print_preview(content)

    if not args.yes:
        confirm = input("\n确认发布到小红书？(y/N): ").strip().lower()
        if confirm != "y":
            print("已取消发布。")
            return

    _do_publish(content, args.headless)


def cmd_batch(args):
    """批量生成并发布多篇文章"""
    settings.check_or_exit()

    wp = WordPressClient()
    gen = ContentGenerator()
    total = len(args.post_ids)
    results = {"success": [], "failed": []}

    print(f"\n📋 批量任务：共 {total} 篇文章待处理")
    print("=" * 50)

    for idx, post_id in enumerate(args.post_ids, 1):
        print(f"\n[{idx}/{total}] 处理文章 ID: {post_id}")
        print("-" * 40)

        try:
            post = wp.get_post(post_id)
            content = gen.generate(post)
            _save_content(content, post.id)

            if args.publish:
                success = publish_note(content, headless=args.headless)
                if success:
                    results["success"].append(post_id)
                    print(f"  ✅ 发布成功: {content.title}")
                else:
                    results["failed"].append(post_id)
                    print(f"  ❌ 发布失败: {content.title}")

                # 篇间延迟，避免频率限制
                if idx < total:
                    delay = settings.xhs.PUBLISH_DELAY
                    print(f"  ⏳ 等待 {delay}s...")
                    time.sleep(delay)
            else:
                results["success"].append(post_id)
                _print_preview(content)

        except Exception as e:
            results["failed"].append(post_id)
            logger.error("文章 %d 处理失败: %s", post_id, e)
            print(f"  ❌ 处理失败: {e}")

    # 汇总
    print("\n" + "=" * 50)
    print(f"📊 批量任务完成：成功 {len(results['success'])} 篇，失败 {len(results['failed'])} 篇")
    if results["failed"]:
        print(f"   失败 ID: {results['failed']}")


def cmd_local(args):
    """从共享素材目录读取文章，生成小红书文案并可选发布"""
    settings.check_or_exit()

    slug = args.slug
    asset_dir = SHARED_OUTPUT_DIR / slug
    article_file = asset_dir / "article.json"
    image_dir = asset_dir / "images"

    if not article_file.exists():
        # 尝试模糊匹配（列出可用的 slug）
        available = [d.name for d in SHARED_OUTPUT_DIR.iterdir() if d.is_dir() and (d / "article.json").exists()] if SHARED_OUTPUT_DIR.exists() else []
        print(f"\n  素材不存在: {article_file}")
        if available:
            print(f"\n  可用的 slug:")
            for s in sorted(available):
                print(f"    - {s}")
        else:
            print(f"  共享素材目录为空或不存在: {SHARED_OUTPUT_DIR}")
            print("  请先使用 wordpress 项目生成文章")
        sys.exit(1)

    # 读取 article.json
    with open(article_file, "r", encoding="utf-8") as f:
        article = json.load(f)
    logger.info("已加载本地素材: %s", slug)

    # 收集本地图片
    image_paths = []
    if image_dir.exists():
        for ext in ("*.png", "*.jpg", "*.jpeg", "*.webp"):
            image_paths.extend(sorted(image_dir.glob(ext)))
    image_paths = [str(p) for p in image_paths[:9]]
    logger.info("本地图片 %d 张", len(image_paths))

    # 生成小红书文案
    gen = ContentGenerator()
    content = gen.generate_from_local(article, image_paths)

    _print_preview(content)
    _save_content_local(content, slug, asset_dir)

    if not args.publish:
        return

    if not args.yes:
        confirm = input("\n确认发布到小红书？(y/N): ").strip().lower()
        if confirm != "y":
            print("已取消发布。")
            return

    _do_publish(content, args.headless)


def cmd_local_list(args):
    """列出共享素材目录中可用的文章"""
    if not SHARED_OUTPUT_DIR.exists():
        print(f"\n  共享素材目录不存在: {SHARED_OUTPUT_DIR}")
        print("  请先使用 wordpress 项目生成文章")
        return

    slugs = []
    for d in sorted(SHARED_OUTPUT_DIR.iterdir()):
        if d.is_dir() and (d / "article.json").exists():
            with open(d / "article.json", "r", encoding="utf-8") as f:
                article = json.load(f)
            title = article.get("title", "-")
            img_count = len(list((d / "images").glob("*"))) if (d / "images").exists() else 0
            has_result = (d / "result.json").exists()
            slugs.append((d.name, title, img_count, has_result))

    if not slugs:
        print("\n  共享素材目录为空，请先使用 wordpress 项目生成文章")
        return

    print(f"\n{'slug':<40}  {'图片':>4}  {'已发布':>6}  标题")
    print("-" * 100)
    for slug, title, img_count, has_result in slugs:
        status = "是" if has_result else "-"
        print(f"{slug:<40}  {img_count:>4}  {status:>6}  {title}")
    print(f"\n共 {len(slugs)} 篇素材  目录: {SHARED_OUTPUT_DIR}")


def cmd_video(args):
    """为文章生成短视频（火山引擎即梦AI），可选自动发布到小红书"""
    from video.generator import generate_video_from_article

    source = args.source
    filepath = Path(source)

    # 判断输入是 post_id 还是 json 文件
    if filepath.exists() or (OUTPUT_DIR / source).exists():
        if not filepath.exists():
            filepath = OUTPUT_DIR / source
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        post_id = data.get("post_id", 0)
    else:
        # 当作 post_id 处理
        try:
            post_id = int(source)
        except ValueError:
            print(f"无效参数: {source}（应为文章 ID 或 JSON 文件路径）")
            sys.exit(1)

        json_path = OUTPUT_DIR / f"xhs_post_{post_id}.json"
        if json_path.exists():
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            # 需要先生成文案
            settings.check_or_exit()
            wp = WordPressClient()
            post = wp.get_post(post_id)
            gen = ContentGenerator()
            content = gen.generate(post)
            _save_content(content, post.id)
            data = content.to_dict()
            data["post_id"] = post_id

    title = data.get("title", "")
    body = data.get("body", "")
    ratio = args.ratio if hasattr(args, "ratio") else "9:16"
    prompt = args.prompt if hasattr(args, "prompt") and args.prompt else None

    print(f"\n为文章 [{post_id}] 生成视频")
    print(f"标题: {title}")
    print(f"比例: {ratio}")
    print("=" * 50)

    with_audio = not getattr(args, "no_audio", False)
    voice = getattr(args, "voice", "zh-CN-XiaoyiNeural")
    rate = getattr(args, "rate", "+0%")
    script = getattr(args, "script", None)

    video_path = generate_video_from_article(
        title=title,
        body=body,
        post_id=post_id,
        aspect_ratio=ratio,
        custom_prompt=prompt,
        with_audio=with_audio,
        voice=voice,
        rate=rate,
        custom_script=script,
    )

    if not video_path:
        print("\n视频生成失败")
        sys.exit(1)

    print(f"\n视频已生成: {video_path}")

    # ── 自动发布 ──
    if args.publish:
        # 构建正文（body + hashtags）
        hashtags = data.get("hashtags", [])
        full_body = body
        if hashtags:
            tags_text = " ".join(f"#{t}" for t in hashtags)
            full_body = f"{body}\n\n{tags_text}"

        safe_title = title[:20] if len(title) > 20 else title
        print(f"\n准备发布视频到小红书...")
        print(f"标题: {safe_title}")
        print(f"正文: {full_body[:80]}...")
        print("=" * 50)

        _do_publish_video(str(video_path), safe_title, full_body, args.headless)


def cmd_video_publish(args):
    """将已生成的视频发布到小红书"""
    source = args.source
    filepath = Path(source)

    # 加载 JSON 文件获取视频路径和文案
    if filepath.exists() or (OUTPUT_DIR / source).exists():
        if not filepath.exists():
            filepath = OUTPUT_DIR / source
    else:
        # 尝试当作 post_id
        try:
            post_id = int(source)
            filepath = OUTPUT_DIR / f"xhs_post_{post_id}.json"
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
    if not video_path or not Path(video_path).exists():
        # 尝试根据 post_id 推测
        post_id = data.get("post_id", 0)
        fallback = OUTPUT_DIR / f"xhs_video_{post_id}.mp4"
        if fallback.exists():
            video_path = str(fallback)
        else:
            print(f"未找到视频文件。请先用 video 命令生成视频。")
            print(f"  查找路径: {video_path or '(未记录)'}")
            if post_id:
                print(f"  备选路径: {fallback}")
            sys.exit(1)

    title = data.get("title", "")[:20]
    body = data.get("body", "")
    hashtags = data.get("hashtags", [])
    full_body = body
    if hashtags:
        tags_text = " ".join(f"#{t}" for t in hashtags)
        full_body = f"{body}\n\n{tags_text}"

    print(f"\n发布视频到小红书")
    print(f"视频: {video_path}")
    print(f"标题: {title}")
    print(f"正文: {full_body[:80]}...")
    print("=" * 50)

    if not args.yes:
        confirm = input("\n确认发布到小红书？(y/N): ").strip().lower()
        if confirm != "y":
            print("已取消发布。")
            return

    _do_publish_video(video_path, title, full_body, args.headless)


def cmd_audio(args):
    """为已有无声视频添加配音"""
    from video.tts import add_voiceover

    source = args.source
    filepath = Path(source)

    # 加载 JSON 获取文案和视频信息
    if filepath.exists() or (OUTPUT_DIR / source).exists():
        if not filepath.exists():
            filepath = OUTPUT_DIR / source
    else:
        try:
            post_id = int(source)
            filepath = OUTPUT_DIR / f"xhs_post_{post_id}.json"
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
        fallback = OUTPUT_DIR / f"xhs_video_{post_id}.mp4"
        if fallback.exists():
            video_path = str(fallback)
        else:
            print(f"未找到视频文件，请先用 video 命令生成视频。")
            sys.exit(1)

    title = data.get("title", "")
    body = data.get("body", "")
    voice = getattr(args, "voice", "zh-CN-XiaoyiNeural")
    rate = getattr(args, "rate", "+0%")
    script = getattr(args, "script", None)

    print(f"\n为视频添加配音")
    print(f"视频: {video_path}")
    print(f"语音: {voice}  语速: {rate}")
    print("=" * 50)

    final_path = add_voiceover(
        video_path=Path(video_path),
        title=title,
        body=body,
        post_id=post_id,
        voice=voice,
        rate=rate,
        custom_script=script,
    )

    if final_path:
        print(f"\n有声视频已生成: {final_path}")
    else:
        print("\n配音失败")
        sys.exit(1)


def cmd_debug(args):
    """诊断小红书发布页面元素（排查问题用）"""
    from xiaohongshu.publisher import diagnose_page
    diagnose_page()


# ──────────────────────────────────────────────
#  辅助函数
# ──────────────────────────────────────────────

def _do_publish_video(video_path: str, title: str, body: str, headless: bool = False):
    """执行视频发布并打印结果"""
    logger.info("开始自动发布视频到小红书...")
    success = publish_video_note(video_path, title, body, headless=headless)
    if success:
        print("\n视频发布成功！")
    else:
        print("\n视频发布可能失败，请查看 logs/run.log 获取详情。")


def _do_publish(content: XHSContent, headless: bool = False):
    """执行发布并打印结果"""
    logger.info("开始自动发布到小红书...")
    success = publish_note(content, headless=headless)
    if success:
        print("\n✅ 笔记发布成功！")
    else:
        print("\n❌ 发布可能失败，请查看 logs/run.log 获取详情。")


def _print_preview(content: XHSContent):
    """在终端打印文案预览"""
    print("\n" + "=" * 50)
    print("  📱 小红书文案预览")
    print("=" * 50)
    print(f"\n📌 标题: {content.title}")
    print(f"\n📝 正文:\n{content.body}")
    tags = " ".join(f"#{t}" for t in content.hashtags)
    print(f"\n🏷️  话题: {tags}")
    if content.image_urls:
        print(f"\n🖼️  配图 ({len(content.image_urls)} 张):")
        for i, url in enumerate(content.image_urls, 1):
            print(f"     {i}. {url}")
    print("\n" + "=" * 50)


def _save_content(content: XHSContent, post_id: int):
    """将生成的文案保存为 JSON 文件"""
    OUTPUT_DIR.mkdir(exist_ok=True)

    data = content.to_dict()
    data["post_id"] = post_id

    filepath = OUTPUT_DIR / f"xhs_post_{post_id}.json"
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info("文案已保存: %s", filepath)
    print(f"\n  文案已保存至: {filepath}")


def _save_content_local(content: XHSContent, slug: str, asset_dir: Path):
    """将从本地素材生成的文案保存到素材目录"""
    data = content.to_dict()
    data["slug"] = slug
    data["source"] = "local"

    filepath = asset_dir / "xhs_content.json"
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info("小红书文案已保存: %s", filepath)
    print(f"\n  文案已保存至: {filepath}")


# ──────────────────────────────────────────────
#  CLI 入口
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="小红书推广助手 —— WordPress 文章 → 小红书爆款笔记（DeepSeek 驱动）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # list
    p_list = subparsers.add_parser("list", help="列出最近的 WordPress 文章")
    p_list.add_argument("-n", "--count", type=int, default=10, help="获取数量（默认10）")
    p_list.add_argument("-s", "--search", type=str, default="", help="搜索关键词")
    p_list.set_defaults(func=cmd_list)

    # generate（别名 preview）
    for name in ("generate", "preview"):
        p = subparsers.add_parser(name, help="根据文章 ID 生成小红书文案（不发布）")
        p.add_argument("post_id", type=int, help="WordPress 文章 ID")
        p.set_defaults(func=cmd_generate)

    # publish
    p_pub = subparsers.add_parser("publish", help="生成文案并自动发布到小红书")
    p_pub.add_argument("post_id", type=int, help="WordPress 文章 ID")
    p_pub.add_argument("-y", "--yes", action="store_true", help="跳过确认直接发布")
    p_pub.add_argument("--headless", action="store_true", help="无头模式运行浏览器")
    p_pub.set_defaults(func=cmd_publish)

    # republish
    p_repub = subparsers.add_parser("republish", help="从已保存的 JSON 文案直接发布")
    p_repub.add_argument("json_file", type=str, help="JSON 文案文件路径（如 xhs_post_123.json）")
    p_repub.add_argument("-y", "--yes", action="store_true", help="跳过确认直接发布")
    p_repub.add_argument("--headless", action="store_true", help="无头模式运行浏览器")
    p_repub.set_defaults(func=cmd_republish)

    # batch
    p_batch = subparsers.add_parser("batch", help="批量处理多篇文章")
    p_batch.add_argument("post_ids", type=int, nargs="+", help="WordPress 文章 ID 列表")
    p_batch.add_argument("--publish", action="store_true", help="生成后自动发布（默认仅生成）")
    p_batch.add_argument("--headless", action="store_true", help="无头模式运行浏览器")
    p_batch.set_defaults(func=cmd_batch)

    # local（从共享素材目录生成文案）
    p_local = subparsers.add_parser("local", help="从共享素材目录读取文章，生成小红书文案")
    p_local.add_argument("slug", type=str, help="文章 slug（对应 output/{slug}/ 目录）")
    p_local.add_argument("--publish", action="store_true", help="生成后自动发布到小红书")
    p_local.add_argument("-y", "--yes", action="store_true", help="跳过确认直接发布")
    p_local.add_argument("--headless", action="store_true", help="无头模式运行浏览器")
    p_local.set_defaults(func=cmd_local)

    # local-list（列出共享素材目录）
    p_local_list = subparsers.add_parser("local-list", help="列出共享素材目录中可用的文章")
    p_local_list.set_defaults(func=cmd_local_list)

    # video
    p_video = subparsers.add_parser("video", help="为文章生成短视频（自动配音）")
    p_video.add_argument("source", type=str, help="文章 ID 或 JSON 文件路径")
    p_video.add_argument("--ratio", type=str, default="9:16", help="画面比例（默认 9:16 竖屏）")
    p_video.add_argument("--prompt", type=str, default=None, help="自定义视频 prompt（跳过 AI 生成）")
    p_video.add_argument("--no-audio", action="store_true", help="不配音（仅生成无声视频）")
    p_video.add_argument("--voice", type=str, default="zh-CN-XiaoyiNeural",
                         help="TTS 语音（默认 zh-CN-XiaoyiNeural 女声）")
    p_video.add_argument("--rate", type=str, default="+0%", help="语速调整（如 +10%%, -5%%）")
    p_video.add_argument("--script", type=str, default=None, help="自定义口播稿（跳过 AI 生成）")
    p_video.add_argument("--publish", action="store_true", help="生成后自动发布到小红书")
    p_video.add_argument("--headless", action="store_true", help="无头模式运行浏览器")
    p_video.set_defaults(func=cmd_video)

    # video-publish（仅发布已有视频）
    p_vpub = subparsers.add_parser("video-publish", help="将已生成的视频发布到小红书")
    p_vpub.add_argument("source", type=str, help="JSON 文件路径或文章 ID（需含 video_path）")
    p_vpub.add_argument("-y", "--yes", action="store_true", help="跳过确认直接发布")
    p_vpub.add_argument("--headless", action="store_true", help="无头模式运行浏览器")
    p_vpub.set_defaults(func=cmd_video_publish)

    # audio（为已有视频配音）
    p_audio = subparsers.add_parser("audio", help="为已有无声视频添加配音")
    p_audio.add_argument("source", type=str, help="JSON 文件路径或文章 ID")
    p_audio.add_argument("--voice", type=str, default="zh-CN-XiaoyiNeural",
                         help="TTS 语音（默认 zh-CN-XiaoyiNeural 女声）")
    p_audio.add_argument("--rate", type=str, default="+0%", help="语速调整（如 +10%%, -5%%）")
    p_audio.add_argument("--script", type=str, default=None, help="自定义口播稿（跳过 AI 生成）")
    p_audio.set_defaults(func=cmd_audio)

    # debug
    p_debug = subparsers.add_parser("debug", help="诊断发布页面元素（排查问题用）")
    p_debug.set_defaults(func=cmd_debug)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    try:
        main()
    except ConfigError as e:
        print(f"\n⚠️  配置错误: {e}", file=sys.stderr)
        print("请检查上一级目录的 .env 文件，参考 .env.example 填写配置", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n\n已中断。")
        sys.exit(130)
    except XHSBaseError as e:
        logger.error("运行错误: %s", e, exc_info=True)
        print(f"\n❌ 错误: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        logger.error("未预期错误: %s", e, exc_info=True)
        print(f"\n❌ 未预期错误: {e}", file=sys.stderr)
        print("详情请查看 logs/run.log", file=sys.stderr)
        sys.exit(1)
