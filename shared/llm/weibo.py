"""
微博文案生成器
—— 将 WordPress 文章或本地 article.json 通过 LLM 改写为微博文章
支持两种发布形式：
  1. 微博头条文章（长文章，card.weibo.com）
  2. 微博短内容（配视频/图片的普通微博）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from shared.llm.client import LLMClient, extract_json_block
from shared.utils.exceptions import ContentGenError, LLMResponseError
from shared.utils.logger import get_logger
from shared.wp.client import WPPost

logger = get_logger("weibo-content")


# ────────── 数据模型 ──────────

@dataclass
class WeiboContent:
    """微博发布内容"""
    title: str                                              # 文章标题 / 视频标题
    body: str                                               # 正文（长文章）或微博文案（短内容）
    summary: str = ""                                       # 文章摘要
    tags: List[str] = field(default_factory=list)           # 话题标签
    cover_urls: List[str] = field(default_factory=list)     # 封面图 / 配图（URL 或本地路径）

    # 微博话题用 #话题# 格式
    WEIBO_TITLE_MAX = 40

    def full_text(self) -> str:
        """拼装完整微博文案（短内容模式，含话题标签）"""
        parts = [self.body]
        if self.tags:
            tags_text = " ".join(f"#{t}#" for t in self.tags)
            parts.append(tags_text)
        return "\n\n".join(parts)

    def brief(self) -> str:
        return f"[{self.title}] {len(self.body)}字 {len(self.tags)}话题 {len(self.cover_urls)}图"

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "body": self.body,
            "summary": self.summary,
            "tags": self.tags,
            "cover_urls": self.cover_urls,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WeiboContent":
        return cls(
            title=data["title"],
            body=data["body"],
            summary=data.get("summary", ""),
            tags=data.get("tags", []),
            cover_urls=data.get("cover_urls", []),
        )


# ────────── Prompt（长文章模式） ──────────

_WEIBO_ARTICLE_SYSTEM_PROMPT = """你是一位微博百万粉丝科技大V，擅长写出高转发、高互动的深度文章。

## 微博头条文章特性

微博头条文章是微博的长文功能，支持富文本编辑，展示在用户首页信息流中。
用户画像：18-45 岁，偏好有深度但易读的内容，重视观点和态度。
算法核心：互动率（转发 + 评论 + 点赞）决定曝光量。

## 标题公式（任选一种）

1. 观点式：直接亮出核心观点，让人一看就想点进来
2. 数据式：「最新数据：…」「报告显示…」
3. 痛点式：「你踩过这些坑吗？…」
4. 趋势式：「2026 年…的 X 个关键变化」

### 标题规则
- **不超过 40 个字**
- 有信息量，不做低质量标题党
- 禁止使用引号包裹标题

## 正文结构

### 开篇（第 1 段）
- 直接抛出核心观点或一个有冲击力的事实
- 让读者明确「读完能获得什么」
- 禁止用「随着…的发展」「近年来」等空洞开头

### 主体（3-5 个小节）
- 每个小节用 **加粗小标题** 分隔
- 小标题要有信息量
- 每段 3-5 句话
- 必须包含：
  - 至少 2 个具体案例（行业、规模、效果）
  - 至少 2 处数据引用（标明来源）
  - 「你可以怎么做」的实操建议
- 善用对比增强说服力

### 结尾
- 用 2-3 句话总结核心观点
- 抛出一个开放性问题引导评论和转发
- 示例：「你怎么看？欢迎转发讨论。」

### 字数
- 正文 **800-2000 字**

## 摘要
- 提供一段 50-100 字的摘要，用于微博信息流展示

## 话题标签
- 给出 **3-5 个**相关话题标签
- 只输出标签文字（不带 # 号，系统会自动加）
- 第 1 个为核心主题词

## 语言风格
- 专业但不枯燥，有态度有观点
- 用第二人称「你」与读者对话
- 适当使用短句和反问
- 禁止使用：「赋能」「闭环」「抓手」等空洞词汇

## 重要
- 不要虚构任何事实，完全基于原文信息改写
- 不要输出任何解释说明，只输出 JSON

## 输出格式

严格输出以下 JSON，不要包裹在 markdown 代码块中：
{"title": "标题文字", "body": "正文内容", "summary": "摘要文字", "tags": ["标签1", "标签2"]}"""


# ────────── Prompt（短内容 / 视频配文模式） ──────────

_WEIBO_SHORT_SYSTEM_PROMPT = """你是一位微博百万粉丝科技大V，擅长写出高转发、高互动的短内容微博。

## 微博短内容特性

微博正文限制 2000 字，但最佳阅读体验在 300-800 字之间。
用户喜欢：有观点、有信息量、有互动感的内容。
算法核心：互动率决定曝光量，转发 > 评论 > 点赞。

## 内容结构

### 开头（1-2 句）
- 直接亮出最核心的观点或信息
- 用「你知道吗？」「一个被忽视的事实：」等吸引注意

### 主体（3-5 段）
- 每段 2-3 句话，信息密度要高
- 包含具体案例和数据
- 层层递进，逻辑清晰
- 可以用「→」「▶」等符号增强视觉层次

### 结尾（1-2 句）
- 总结核心观点
- 引导互动：「你觉得呢？」「欢迎评论区聊聊」

### 字数
- 控制在 **300-800 字**

## 话题标签
- 给出 **3-5 个**相关话题标签
- 只输出标签文字（不带 # 号）
- 考虑微博热搜热度

## 语言风格
- 像发朋友圈一样自然，但有专业度
- 语气要有态度，避免四平八稳

## 重要
- 不要虚构事实
- 不要输出解释说明，只输出 JSON

## 输出格式

严格输出以下 JSON，不要包裹在 markdown 代码块中：
{"title": "标题/主题（≤20字）", "body": "微博正文", "tags": ["标签1", "标签2"]}"""


# ────────── 生成器 ──────────

class WeiboContentGenerator:
    """调用 LLM 将 WP 文章或本地文章转化为微博内容"""

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    # ── 长文章模式 ──

    def generate_from_post(self, post: WPPost) -> WeiboContent:
        """输入 WordPress 文章，输出微博头条文章"""
        user_msg = self._build_user_message_from_post(post)
        logger.info("生成微博文案，文章: [%s] %s", post.id, post.title)

        raw = self._call_llm(user_msg, system_prompt=_WEIBO_ARTICLE_SYSTEM_PROMPT)
        cover_urls = post.all_image_urls[:3] if hasattr(post, "all_image_urls") else []
        content = self._parse_response(raw, fallback_title=post.title, cover_urls=cover_urls)
        logger.info("微博文案生成完成: %s", content.brief())
        return content

    def generate_from_article(self, article: dict, image_paths: Optional[list] = None) -> WeiboContent:
        """从本地 article.json 生成微博头条文章"""
        user_msg = self._build_user_message_from_article(article)
        logger.info("生成微博文案（本地素材），标题: %s", article.get("title", ""))

        raw = self._call_llm(user_msg, system_prompt=_WEIBO_ARTICLE_SYSTEM_PROMPT)
        covers = (image_paths or [])[:3]
        content = self._parse_response(raw, fallback_title=article.get("title", "")[:40], cover_urls=covers)
        logger.info("微博文案生成完成（本地素材）: %s", content.brief())
        return content

    # ── 短内容 / 视频配文模式 ──

    def generate_short_from_post(self, post: WPPost) -> WeiboContent:
        """输入 WordPress 文章，输出微博短内容（适合配视频）"""
        user_msg = self._build_user_message_from_post(post)
        logger.info("生成微博短内容，文章: [%s] %s", post.id, post.title)

        raw = self._call_llm(user_msg, system_prompt=_WEIBO_SHORT_SYSTEM_PROMPT)
        cover_urls = post.all_image_urls[:3] if hasattr(post, "all_image_urls") else []
        content = self._parse_response(raw, fallback_title=post.title, cover_urls=cover_urls)
        logger.info("微博短内容生成完成: %s", content.brief())
        return content

    def generate_short_from_article(self, article: dict, image_paths: Optional[list] = None) -> WeiboContent:
        """从本地素材生成微博短内容（适合配视频）"""
        user_msg = self._build_user_message_from_article(article)
        logger.info("生成微博短内容（本地素材），标题: %s", article.get("title", ""))

        raw = self._call_llm(user_msg, system_prompt=_WEIBO_SHORT_SYSTEM_PROMPT)
        covers = (image_paths or [])[:3]
        content = self._parse_response(raw, fallback_title=article.get("title", "")[:20], cover_urls=covers)
        logger.info("微博短内容生成完成（本地素材）: %s", content.brief())
        return content

    # ── 内部方法 ──

    def _call_llm(self, user_msg: str, system_prompt: str) -> str:
        raw = self.llm.chat(
            system_prompt=system_prompt,
            user_prompt=user_msg,
            temperature=0.7,
        )
        if not raw:
            raise ContentGenError("LLM 调用失败，未返回内容")
        return raw

    @staticmethod
    def _parse_response(raw: str, fallback_title: str, cover_urls: list) -> WeiboContent:
        data = extract_json_block(raw)
        if data is None:
            raise LLMResponseError(f"无法从 LLM 输出中提取 JSON: {raw[:200]}")

        title = data.get("title", "").strip() or fallback_title
        body = data.get("body", "").strip()
        if not body:
            raise LLMResponseError("LLM 返回的正文为空")

        summary = data.get("summary", "").strip()
        tags = data.get("tags", [])
        if not isinstance(tags, list):
            tags = []
        tags = [str(t).strip() for t in tags if t]

        return WeiboContent(
            title=title,
            body=body,
            summary=summary,
            tags=tags,
            cover_urls=cover_urls,
        )

    @staticmethod
    def _build_user_message_from_post(post: WPPost) -> str:
        parts = [f"文章标题：{post.title}"]
        if post.excerpt:
            parts.append(f"文章摘要：{post.excerpt}")
        content_text = post.content[:5000]
        if len(post.content) > 5000:
            content_text += "\n...(正文已截断)"
        parts.append(f"文章正文：\n{content_text}")
        if post.tags:
            parts.append(f"文章标签：{', '.join(post.tags)}")
        if post.categories:
            parts.append(f"文章分类：{', '.join(post.categories)}")
        return "\n\n".join(parts)

    @staticmethod
    def _build_user_message_from_article(article: dict) -> str:
        parts = [f"文章标题：{article.get('title', '')}"]
        excerpt = article.get("excerpt", "")
        if excerpt:
            parts.append(f"文章摘要：{excerpt}")
        sections = article.get("sections", [])
        body_parts = []
        for sec in sections:
            heading = sec.get("title", sec.get("heading", ""))
            paragraphs = sec.get("paragraphs", [])
            if heading:
                body_parts.append(heading)
            body_parts.extend(paragraphs)
        body_text = "\n".join(body_parts)
        if len(body_text) > 5000:
            body_text = body_text[:5000] + "\n...(正文已截断)"
        parts.append(f"文章正文：\n{body_text}")
        takeaways = article.get("key_takeaways", [])
        if takeaways:
            parts.append(f"关键要点：{', '.join(takeaways)}")
        tags = article.get("tags", [])
        if tags:
            parts.append(f"文章标签：{', '.join(tags)}")
        return "\n\n".join(parts)
