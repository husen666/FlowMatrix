"""
WordPress 发布流水线
—— 串联 shared 模块完成：内容生成 -> 配图 -> 上传 -> SEO -> 发布
素材统一保存到共享 output 目录，供 xiaohongshu 等项目复用
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from shared.llm.article import ArticleGenerator
from shared.llm.client import LLMClient
from shared.media.avatar import AvatarGenerator
from shared.media.image import ImageGenerator
from shared.media.video import VideoGenerator
from shared.utils.exceptions import QualityError
from shared.utils.helpers import merge_unique, save_json, slugify_chinese
from shared.utils.logger import get_logger
from shared.wp.client import WordPressClient
from wordpress.html_builder import build_content_html, evaluate_quality, verify_published_page

logger = get_logger("wp-pipeline")


class WPPublisher:
    """WordPress 自动发布器"""

    def __init__(
        self,
        wp_client: WordPressClient,
        llm: Optional[LLMClient] = None,
        image_gen: Optional[ImageGenerator] = None,
        video_gen: Optional[VideoGenerator] = None,
        avatar_gen: Optional[AvatarGenerator] = None,
        output_dir: str = "",
        max_content_images: int = 4,
        deepseek_enabled: bool = True,
        site_name: str = "Aineoo",
    ) -> None:
        self.wp = wp_client
        self.llm = llm
        self.site_name = site_name
        self.output_dir = Path(output_dir) if output_dir else Path(__file__).resolve().parent.parent / "output"

        self.article_gen = ArticleGenerator(
            llm=llm,
            max_content_images=max_content_images,
            deepseek_enabled=deepseek_enabled,
        )
        self.image_gen = image_gen
        self.video_gen = video_gen
        self.avatar_gen = avatar_gen

    def __enter__(self) -> "WPPublisher":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self.wp.close()

    # ── 发布主流程 ──

    def publish(
        self,
        prompt: str,
        categories: Sequence[str],
        tags: Sequence[str],
        status: str = "publish",
        custom_slug: Optional[str] = None,
        custom_title: Optional[str] = None,
        dry_run: bool = False,
        min_quality_score: int = 75,
        strict_quality: bool = False,
        verify_online: bool = True,
        related_limit: int = 3,
        use_deepseek: Optional[bool] = None,
        enable_video: bool = False,
        video_ratio: str = "16:9",
        enable_avatar: bool = False,
        avatar_image_url: Optional[str] = None,
    ) -> Dict:
        # ── Step 1: 生成文章内容 ──
        t_start = time.monotonic()
        logger.info("Step 1/7  生成文章内容...")
        article = self.article_gen.generate(prompt=prompt, use_deepseek=use_deepseek)
        if custom_title:
            article["title"] = custom_title
        logger.info("  内容生成完成（%.1fs, 来源: %s）", time.monotonic() - t_start, article.get("content_source", "rules"))

        # slug 三级优先：custom > DeepSeek 生成 > 拼音转换
        slug = custom_slug or article.get("slug") or slugify_chinese(article["title"])
        canonical_url = f"{self.wp.wp_base}/{slug}/"
        article["canonical_url"] = canonical_url
        logger.info("  标题: %s", article["title"])
        logger.info("  slug: %s", slug)

        # 素材目录：output/{slug}/
        asset_dir = self.output_dir / slug
        image_dir = asset_dir / "images"

        # 保存 article.json
        save_json(asset_dir / "article.json", article)
        logger.info("  素材目录: %s", asset_dir)

        # ── Step 2: 生成 & 上传配图 ──
        t_img = time.monotonic()
        logger.info("Step 2/7  生成 & 上传配图...")
        img_specs = self.article_gen.image_prompts(article)

        featured_media_id: Optional[int] = None
        content_images: List[Dict] = []
        local_images: List[Dict] = []

        # 统一种子：同一篇文章的所有图片使用相同基础 seed，确保视觉一致性
        base_seed = sum(ord(c) for c in slug) % 2147483647

        for i, spec in enumerate(img_specs):
            local_path = image_dir / f"{spec['role']}_{i:02d}.png"

            # 生成图片（传入基于文章的 seed 以保证风格一致）
            generated = None
            if self.image_gen:
                generated = self.image_gen.generate(spec["prompt"], local_path, seed=base_seed + i)
            if not generated:
                continue

            local_images.append({
                "role": spec["role"],
                "file": str(local_path.relative_to(asset_dir)),
                "alt_text": spec.get("alt_text", ""),
                "caption": spec.get("caption", ""),
            })

            # 上传到 WordPress
            media = self.wp.upload_media(
                generated,
                title=spec.get("alt_text", f"{article['focus_keyword']} image {i}"),
                alt_text=spec.get("alt_text", article["focus_keyword"]),
            )
            if not media:
                continue

            source_url = media.get("source_url", "")
            media_id = media.get("id")

            if spec["role"] == "featured" and featured_media_id is None:
                featured_media_id = media_id
                logger.info("  特色图上传成功 media_id=%s", media_id)
            else:
                content_images.append({
                    "url": source_url,
                    "alt_text": spec.get("alt_text", ""),
                    "caption": spec.get("caption", ""),
                    "role": "content",
                })

        media_count = (1 if featured_media_id else 0) + len(content_images)
        logger.info("  配图完成 %d 张（%.1fs）", media_count, time.monotonic() - t_img)

        article["images"] = local_images
        save_json(asset_dir / "article.json", article)

        # ── Step 2.5: 生成视频（可选）──
        video_info: Optional[Dict] = None
        if enable_video and self.video_gen:
            t_vid = time.monotonic()
            logger.info("Step 2.5  生成文章视频（比例 %s）...", video_ratio)
            try:
                # 从文章正文拼接内容用于视频 prompt 生成
                body_text = "\n".join(
                    p for sec in article.get("sections", []) for p in sec.get("paragraphs", [])
                )
                video_path = self.video_gen.generate_from_article(
                    title=article["title"],
                    body=body_text,
                    save_dir=asset_dir,
                    filename="video.mp4",
                    aspect_ratio=video_ratio,
                )
                if video_path and video_path.exists():
                    # 上传到 WordPress 媒体库
                    video_media = self.wp.upload_media(
                        video_path,
                        title=f"{article['title']} - 视频",
                        alt_text=article["focus_keyword"],
                    )
                    if video_media:
                        video_url = video_media.get("source_url", "")
                        video_info = {
                            "url": video_url,
                            "media_id": video_media.get("id"),
                            "local_path": str(video_path.relative_to(asset_dir)),
                        }
                        media_count += 1
                        logger.info("  视频上传成功 url=%s（%.1fs）", video_url[:60], time.monotonic() - t_vid)
                    else:
                        logger.warning("  视频生成成功但上传失败")
                else:
                    logger.warning("  视频生成失败，跳过")
            except Exception as exc:
                logger.error("  视频生成异常（不影响发布）: %s", exc)

            # 保存视频信息到 article.json
            if video_info:
                article["video"] = video_info
                save_json(asset_dir / "article.json", article)

        # ── Step 2.8: 生成数字人视频（可选）──
        avatar_info: Optional[Dict] = None
        if enable_avatar and self.avatar_gen and self.avatar_gen.available:
            t_avatar = time.monotonic()
            logger.info("Step 2.8  生成数字人视频...")
            try:
                body_text = "\n".join(
                    p for sec in article.get("sections", []) for p in sec.get("paragraphs", [])
                )
                avatar_path = self.avatar_gen.generate_from_article(
                    title=article["title"],
                    body=body_text,
                    save_dir=asset_dir / "avatar",
                    llm=self.llm,
                    image_url=avatar_image_url,
                )
                if avatar_path and avatar_path.exists():
                    # 上传到 WordPress 媒体库
                    avatar_media = self.wp.upload_media(
                        avatar_path,
                        title=f"{article['title']} - 数字人解读",
                        alt_text=article["focus_keyword"],
                    )
                    if avatar_media:
                        avatar_url = avatar_media.get("source_url", "")
                        avatar_info = {
                            "url": avatar_url,
                            "media_id": avatar_media.get("id"),
                            "local_path": str(avatar_path.relative_to(asset_dir)),
                        }
                        media_count += 1
                        logger.info("  数字人视频上传成功（%.1fs）: %s", time.monotonic() - t_avatar, avatar_url[:60])
                    else:
                        logger.warning("  数字人视频生成成功但上传失败")
                else:
                    logger.warning("  数字人视频生成失败，跳过")
            except Exception as exc:
                logger.error("  数字人视频异常（不影响发布）: %s", exc)

            if avatar_info:
                article["avatar"] = avatar_info
                save_json(asset_dir / "article.json", article)

        # 选择嵌入文章的视频：数字人优先 > 普通视频
        embed_video_url = None
        if avatar_info:
            embed_video_url = avatar_info.get("url")
        elif video_info:
            embed_video_url = video_info.get("url")

        # ── Step 3: 分类 & 标签 ──
        logger.info("Step 3/7  处理分类 & 标签...")
        category_ids: List[int] = []
        for c in categories:
            term_id = self.wp.ensure_term("categories", c)
            if term_id:
                category_ids.append(term_id)

        auto_tags = article.get("tags", [article["focus_keyword"]])
        final_tags = [t for t in merge_unique(list(tags) + auto_tags) if len(t) <= 15]
        tag_ids: List[int] = []
        for t in final_tags:
            term_id = self.wp.ensure_term("tags", t)
            if term_id:
                tag_ids.append(term_id)
        logger.info("  分类: %s  标签: %s", category_ids, list(final_tags))

        # ── Step 4: 构建 HTML + 质量评分 ──
        logger.info("Step 4/7  构建 HTML & 质量评分...")
        related_posts = self.wp.get_related_posts(article["focus_keyword"], slug, limit=related_limit)
        content_html = build_content_html(
            article,
            images=content_images,
            related_posts=related_posts,
            video_url=embed_video_url,
        )
        quality_report = evaluate_quality(
            article=article,
            content_html=content_html,
            image_count=len(content_images),
            category_count=len(category_ids),
            tag_count=len(tag_ids),
        )
        logger.info("  质量评分: %d  相关文章: %d", quality_report["score"], len(related_posts))

        if strict_quality and quality_report["score"] < min_quality_score:
            raise QualityError(f"质量评分过低: {quality_report['score']} < {min_quality_score}，已阻止发布。")

        # ── Step 5: 构建发布 payload ──
        logger.info("Step 5/7  构建发布数据...")
        seo_title = f"{article['title']} | {self.site_name}"
        post_payload = {
            "title": article["title"],
            "slug": slug,
            "content": content_html,
            "excerpt": article["excerpt"],
            "status": status,
            "categories": category_ids,
            "tags": tag_ids,
            "meta": {
                "rank_math_title": seo_title,
                "rank_math_description": article["seo_description"],
                "rank_math_focus_keyword": article["focus_keyword"],
                "rank_math_canonical_url": canonical_url,
                "rank_math_robots": "index,follow,max-snippet:-1,max-image-preview:large,max-video-preview:-1",
                "rank_math_twitter_title": seo_title,
                "rank_math_twitter_description": article["seo_description"],
                "rank_math_facebook_title": seo_title,
                "rank_math_facebook_description": article["seo_description"],
                "rank_math_schema_type": "Article",
            },
        }
        if featured_media_id:
            post_payload["featured_media"] = featured_media_id

        # ── Step 6: dry-run 或实际发布 ──
        if dry_run:
            preview_file = asset_dir / "preview.html"
            preview_file.write_text(content_html, encoding="utf-8")
            logger.info("Step 6/7  dry-run 预览已保存: %s", preview_file)
            result = {
                "dry_run": True,
                "slug": slug,
                "asset_dir": str(asset_dir),
                "preview_file": str(preview_file),
                "media_count": media_count,
                "category_ids": category_ids,
                "tag_ids": tag_ids,
                "quality": quality_report,
                "related_count": len(related_posts),
                "content_source": article.get("content_source", "rules"),
                "has_video": video_info is not None,
                "has_avatar": avatar_info is not None,
            }
            save_json(asset_dir / "result.json", result)
            return result

        logger.info("Step 6/7  发布到 WordPress...")
        post = self.wp.create_post(post_payload)
        logger.info("  发布成功 id=%s link=%s", post.get("id"), post.get("link"))
        result = {
            "dry_run": False,
            "post_id": post.get("id"),
            "slug": post.get("slug"),
            "link": post.get("link"),
            "asset_dir": str(asset_dir),
            "media_count": media_count,
            "category_ids": category_ids,
            "tag_ids": tag_ids,
            "quality": quality_report,
            "related_count": len(related_posts),
            "content_source": article.get("content_source", "rules"),
            "has_video": video_info is not None,
            "has_avatar": avatar_info is not None,
        }

        if verify_online and result.get("link"):
            logger.info("  在线验收中...")
            result["verify"] = verify_published_page(result["link"])
            v_status = "通过" if result["verify"].get("ok") else "未通过"
            logger.info("  在线验收: %s", v_status)

        save_json(asset_dir / "result.json", result)
        logger.info("  素材已保存: %s", asset_dir)
        return result
