"""
WordPress 文章 HTML 构建器
—— 从 content_engine.py 提取的纯 HTML 生成逻辑
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from shared.utils.helpers import escape, json_for_script, make_anchor_id


def build_content_html(
    article: Dict,
    images: Sequence[Dict],
    related_posts: Optional[Sequence[Dict]] = None,
    video_url: Optional[str] = None,
) -> str:
    """
    构建文章 HTML
    布局：H1 > 导言 > 视频 > TOC > 快速回答 > 正文 > 总结+要点+CTA > FAQ > 相关文章 > JSON-LD
    """
    title = escape(article["title"])
    intro = escape(article["excerpt"])
    focus = escape(article["focus_keyword"])
    quick_answer = escape(article.get("quick_answer", ""))
    canonical_url = article.get("canonical_url", "")
    blocks: List[str] = []

    content_images = [img for img in images if img.get("role") == "content"]

    blocks.append('<div class="aineoo-ai-article" style="max-width:1080px;margin:0 auto;line-height:1.85;color:#1f2937;">')

    # H1 + 导言
    blocks.append(f'<h1 style="font-size:34px;line-height:1.35;margin-bottom:20px;">{title}</h1>')
    blocks.append(f'<p style="font-size:18px;color:#4b5563;margin-bottom:26px;">{intro}</p>')

    # 视频（导言之后、目录之前）
    if video_url:
        escaped_video = escape(video_url)
        blocks.append(
            '<section style="margin-bottom:28px;">'
            '<div style="position:relative;width:100%;max-width:920px;margin:0 auto;'
            'border-radius:12px;overflow:hidden;box-shadow:0 4px 16px rgba(0,0,0,0.08);">'
            f'<video controls preload="metadata" playsinline '
            f'style="width:100%;display:block;border-radius:12px;" '
            f'poster="">'
            f'<source src="{escaped_video}" type="video/mp4">'
            '您的浏览器不支持视频播放。'
            '</video>'
            '</div>'
            f'<p style="text-align:center;margin-top:10px;color:#6b7280;font-size:13px;">'
            f'{title} — 视频解读</p>'
            '</section>'
        )

    # TOC
    sections = article["sections"]
    toc_entries: List[Dict[str, str]] = [
        {"title": s["title"], "anchor": make_anchor_id(s["title"], i)}
        for i, s in enumerate(sections)
    ]
    if toc_entries:
        blocks.append(
            '<nav style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:10px;padding:16px 20px;margin-bottom:24px;">'
            '<h2 style="font-size:20px;margin:0 0 12px;">目录</h2>'
            '<ol style="margin:0 0 0 20px;padding:0;">'
        )
        for item in toc_entries:
            blocks.append(
                f'<li style="margin:0 0 6px;">'
                f'<a href="#{escape(item["anchor"])}" style="color:#2563eb;text-decoration:none;">'
                f'{escape(item["title"])}</a></li>'
            )
        blocks.append("</ol></nav>")

    # 快速回答
    if quick_answer:
        blocks.append(
            '<section class="quick-answer" style="background:#eef6ff;border:1px solid #cfe3ff;border-radius:10px;padding:18px 20px;margin-bottom:24px;">'
            '<h2 style="font-size:20px;margin:0 0 10px;">一段话回答</h2>'
        )
        blocks.append(f'<p style="margin:0;font-size:16px;">{quick_answer}</p></section>')

    # 正文小节 + 图片穿插
    for idx, sec in enumerate(sections):
        anchor_id = toc_entries[idx]["anchor"] if idx < len(toc_entries) else make_anchor_id(sec["title"], idx)
        blocks.append('<section style="margin-bottom:28px;">')
        blocks.append(f'<h2 id="{escape(anchor_id)}" style="font-size:26px;margin:0 0 16px;padding-top:8px;">{escape(sec["title"])}</h2>')
        for paragraph in sec["paragraphs"]:
            blocks.append(f'<p style="margin:0 0 14px;font-size:16px;">{escape(paragraph)}</p>')
        if idx < len(content_images):
            img = content_images[idx]
            img_url = escape(img.get("url", ""))
            alt = escape(img.get("alt_text", f"{focus} illustration"))
            cap = escape(img.get("caption", sec["title"]))
            if img_url:
                blocks.append('<figure style="margin:20px 0 10px;text-align:center;">')
                blocks.append(f'<img src="{img_url}" alt="{alt}" style="width:100%;max-width:920px;border-radius:10px;" loading="lazy" />')
                blocks.append(f'<figcaption style="margin-top:8px;color:#6b7280;font-size:13px;">{cap}</figcaption></figure>')
        blocks.append("</section>")

    # 总结 + 关键要点 + CTA
    key_takeaways = article.get("key_takeaways", [])
    cta = article.get("cta", {})

    blocks.append(
        '<section style="background:linear-gradient(135deg,#f8fafc 0%,#eef2ff 100%);'
        'border:1px solid #c7d2fe;border-radius:12px;padding:24px 28px;margin-bottom:28px;">'
    )
    blocks.append(f'<h2 style="font-size:24px;margin:0 0 16px;color:#1e293b;">总结</h2>')
    blocks.append(f'<p style="margin:0 0 20px;font-size:16px;line-height:1.9;color:#334155;">{escape(article["conclusion"])}</p>')

    if key_takeaways:
        blocks.append('<div style="background:#ffffff;border-radius:8px;padding:16px 20px;margin-bottom:20px;">')
        blocks.append('<h3 style="font-size:18px;margin:0 0 12px;color:#1e293b;">关键要点</h3>')
        for item in key_takeaways:
            blocks.append(
                f'<div style="display:flex;align-items:flex-start;margin:0 0 10px;font-size:15px;color:#374151;">'
                f'<span style="color:#22c55e;font-weight:bold;margin-right:8px;flex-shrink:0;">&#10003;</span>'
                f'<span>{escape(item)}</span></div>'
            )
        blocks.append('</div>')

    if cta:
        cta_heading = escape(cta.get("heading", "下一步行动"))
        cta_text = escape(cta.get("text", ""))
        if cta_text:
            blocks.append(
                '<div style="background:#2563eb;color:#ffffff;border-radius:8px;padding:16px 20px;">'
                f'<h3 style="font-size:18px;margin:0 0 8px;color:#ffffff;">{cta_heading}</h3>'
                f'<p style="margin:0;font-size:15px;line-height:1.7;color:#e0e7ff;">{cta_text}</p>'
                '</div>'
            )
    blocks.append("</section>")

    # FAQ
    faq_items = article.get("faq", [])
    if faq_items:
        blocks.append(
            '<section style="margin-top:28px;margin-bottom:28px;">'
            '<h2 style="font-size:24px;margin:0 0 18px;">常见问题（FAQ）</h2>'
        )
        for item in faq_items:
            blocks.append(f'<h3 style="font-size:18px;margin:16px 0 8px;">{escape(item.get("question", ""))}</h3>')
            blocks.append(f'<p style="margin:0 0 12px;font-size:15px;color:#374151;">{escape(item.get("answer", ""))}</p>')
        blocks.append("</section>")

    # 相关文章
    if related_posts:
        blocks.append(
            '<section style="border-top:1px solid #e5e7eb;padding-top:24px;margin-top:28px;">'
            '<h2 style="font-size:22px;margin:0 0 14px;">相关文章</h2>'
            '<ul style="margin:0 0 0 18px;padding:0;">'
        )
        for post in related_posts:
            blocks.append(
                f'<li style="margin:0 0 8px;">'
                f'<a href="{escape(post.get("link", "#"))}" style="color:#2563eb;text-decoration:none;">'
                f'{escape(post.get("title", "相关文章"))}</a></li>'
            )
        blocks.append("</ul></section>")

    # JSON-LD 结构化数据
    if faq_items:
        faq_schema = {
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": [
                {
                    "@type": "Question",
                    "name": item.get("question", ""),
                    "acceptedAnswer": {"@type": "Answer", "text": item.get("answer", "")},
                }
                for item in faq_items
            ],
        }
        blocks.append(f'<script type="application/ld+json">{json_for_script(faq_schema)}</script>')

    article_schema = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": article.get("title", ""),
        "description": article.get("seo_description", ""),
        "keywords": ",".join(article.get("tags", [article.get("focus_keyword", "")])),
        "datePublished": datetime.now().isoformat(timespec="seconds"),
        "mainEntityOfPage": canonical_url or "",
    }
    blocks.append(f'<script type="application/ld+json">{json_for_script(article_schema)}</script>')

    # VideoObject 结构化数据
    if video_url:
        now_iso = datetime.now().isoformat(timespec="seconds")
        video_schema = {
            "@context": "https://schema.org",
            "@type": "VideoObject",
            "name": article.get("title", ""),
            "description": article.get("seo_description", ""),
            "contentUrl": video_url,
            "uploadDate": now_iso,
            "duration": "PT10S",
        }
        # 如果有特色图，用作视频缩略图
        featured_imgs = [img for img in images if img.get("role") == "featured"]
        if featured_imgs:
            video_schema["thumbnailUrl"] = featured_imgs[0].get("url", "")
        blocks.append(f'<script type="application/ld+json">{json_for_script(video_schema)}</script>')

    blocks.append("</div>")
    return "\n".join(blocks)


def evaluate_quality(
    article: Dict,
    content_html: str,
    image_count: int,
    category_count: int,
    tag_count: int,
) -> Dict:
    """SEO / 内容质量评分"""
    checks: List[Dict[str, Any]] = []

    def add_check(name: str, passed: bool, weight: int, detail: str) -> None:
        checks.append({"name": name, "passed": passed, "weight": weight, "detail": detail})

    title_len = len(article.get("title", ""))
    desc_len = len(article.get("seo_description", ""))
    excerpt_len = len(article.get("excerpt", ""))
    section_count = len(article.get("sections", []))
    faq_count = len(article.get("faq", []))
    has_ld_json = '"application/ld+json"' in content_html
    has_faq_section = "FAQ" in content_html
    has_quick_answer = "quick-answer" in content_html
    has_focus_keyword = article.get("focus_keyword", "") in content_html

    add_check("标题长度", 10 <= title_len <= 65, 10, f"title_len={title_len}")
    add_check("SEO描述长度", 60 <= desc_len <= 160, 12, f"seo_desc_len={desc_len}")
    add_check("摘要长度", 40 <= excerpt_len <= 160, 8, f"excerpt_len={excerpt_len}")
    add_check("章节数量", section_count >= 3, 12, f"sections={section_count}")
    add_check("FAQ数量", faq_count >= 3, 10, f"faq={faq_count}")
    add_check("结构化数据", has_ld_json, 12, f"ld_json={has_ld_json}")
    add_check("FAQ区块", has_faq_section, 8, f"faq_section={has_faq_section}")
    add_check("快速回答", has_quick_answer, 8, f"quick_answer={has_quick_answer}")
    add_check("主题词覆盖", has_focus_keyword, 8, f"focus_keyword={article.get('focus_keyword', '')}")
    add_check("正文图片数", image_count >= 2, 6, f"images={image_count}")
    add_check("分类数量", category_count >= 1, 3, f"categories={category_count}")
    add_check("标签数量", tag_count >= 3, 3, f"tags={tag_count}")

    total = sum(c["weight"] for c in checks)
    got = sum(c["weight"] for c in checks if c["passed"])
    score = int((got / total) * 100) if total else 0
    failed = [c for c in checks if not c["passed"]]
    return {"score": score, "checks": checks, "failed": failed}


def verify_published_page(link: str, timeout: int = 40) -> Dict:
    """在线验收：检查已发布文章页面"""
    import requests as req
    try:
        resp = req.get(link, timeout=timeout)
        if resp.status_code != 200:
            return {"ok": False, "status_code": resp.status_code, "detail": "页面访问失败"}
        html_text = resp.text
        checks = {
            "has_ld_json": '"application/ld+json"' in html_text,
            "has_faq": "FAQ" in html_text,
            "has_img": "<img" in html_text,
        }
        ok = all(checks.values())
        return {"ok": ok, "status_code": 200, "checks": checks}
    except Exception as exc:
        return {"ok": False, "status_code": 0, "detail": str(exc)}
