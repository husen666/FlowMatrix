"""
知乎文案生成器
—— 将 WordPress 文章或本地 article.json 通过 LLM 改写为知乎回答/文章
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from shared.llm.client import LLMClient, extract_json_block
from shared.utils.exceptions import ContentGenError, LLMResponseError
from shared.utils.logger import get_logger
from shared.wp.client import WPPost

logger = get_logger("zhihu-content")


# ────────── 数据模型 ──────────

@dataclass
class ZhihuContent:
    """知乎文章内容"""
    title: str                                           # 文章标题
    body: str                                            # 正文（Markdown 格式）
    summary: str = ""                                    # 文章摘要
    tags: List[str] = field(default_factory=list)        # 话题标签
    cover_urls: List[str] = field(default_factory=list)  # 封面图 URL 或本地路径

    def full_text(self) -> str:
        """拼装完整发布文本"""
        return self.body

    def summary_text(self) -> str:
        return f"[{self.title}] {len(self.body)}字 {len(self.tags)}标签 {len(self.cover_urls)}图"

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "body": self.body,
            "summary": self.summary,
            "tags": self.tags,
            "cover_urls": self.cover_urls,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ZhihuContent":
        return cls(
            title=data["title"],
            body=data["body"],
            summary=data.get("summary", ""),
            tags=data.get("tags", []),
            cover_urls=data.get("cover_urls", []),
        )


# ────────── Prompt ──────────

_ZHIHU_SYSTEM_PROMPT = """你是一位知乎万赞答主，在科技商业领域有深度思考能力，擅长写出让读者「收藏 + 点赞 + 关注」三连的高质量专栏文章。

## 知乎平台特性

知乎用户特征：高学历、理性、重逻辑，厌恶水文和标题党，尊重有原创见解和深度分析的内容。
核心指标：收藏率 > 点赞率 > 评论率。用户愿意收藏的内容 = 有深度 + 有体系 + 有行动指南。

## 标题

### 标题公式（任选一种）
1. 深度分析式：「从 X 角度看…：Y 的 Z 个关键问题」
2. 经验总结式：「做了 X 年…后，我总结出这 Y 条认知」
3. 解构式：「…到底是什么？一篇文章讲清楚」
4. 观点输出式：「为什么我认为…比…更重要？」

### 标题规则
- 长度 **15-40 字**
- 清晰表达核心观点或问题，不搞标题党
- 让人看标题就知道文章的信息密度和专业度
- 可以用问号引发思考

## 摘要
- 2-3 句话概括文章核心观点和你能获得什么
- 控制在 **50-120 字**
- 第一句点明核心论点，第二句说明文章价值

## 正文结构

### 开篇：建立认知锚点
- 开头用一个「反直觉观点」或「行业痛点」切入
- 明确告诉读者：这篇文章要解决什么问题 / 传递什么认知
- 示例：「大多数人对…的理解是错的。」「如果你还在…，那说明你可能忽略了一个关键变量。」
- 禁止用「随着…的发展」「近年来」「众所周知」开头

### 主体：论证链式写作（核心）
- 用 Markdown 标题（## / ###）组织 3-5 个核心论点
- **每个论点必须有完整的论证链**：
  - 观点（你要表达什么）
  - 论据（数据/案例/引用/逻辑推演来支撑）
  - 推论（所以呢？对读者意味着什么）
- 案例要具体：写明行业、规模、背景、做了什么、结果如何
- 数据要有出处：「据 XX 机构 20XX 年报告」「根据公开数据」
- 善用类比让抽象概念具象化
- 论点之间要有逻辑递进关系，不是简单并列

### 结尾：升华 + 讨论
- 回顾核心观点，但要有更高层次的提炼（不是简单重复）
- 给出 1-2 条具体可执行的行动建议
- 用一个开放性问题收尾，引发读者深度评论
- 示例：「你在实际工作中遇到过…的困境吗？你的解决方案是什么？」

### 字数
- 正文 **1500-3000 字**（知乎用户对深度文章有耐心）

## 话题标签
- 给出 **3-5 个**知乎话题
- 只输出标签文字，不带 # 号
- 选择知乎上真实存在的热门话题（如「人工智能」「商业」「职场」等）
- 优先选择关注量大的话题

## 语言风格
- 理性、有条理，像一位资深从业者的深度分析
- 用「你」与读者对话，但保持专业感
- 允许有明确的观点和判断，不要面面俱到地和稀泥
- 适当使用短句和反问增加可读性
- 禁止使用：「赋能」「闭环」「抓手」「助力」「数智化」「全链路」等空洞词汇
- 禁止使用：「不言而喻」「毋庸置疑」「综上所述」「日新月异」等套话

## 格式说明
- 正文支持 Markdown 格式
- 用 ## 作为一级小标题，### 作为二级小标题
- 重点内容可用 **加粗** 强调
- 引用可用 > 引用格式
- 列表用 - 或 1. 2. 3. 格式

## 重要
- 不要虚构任何事实，完全基于原文信息改写
- 知乎用户对内容质量要求极高，每一段都要有信息量
- 不要输出任何解释说明，只输出 JSON

## 输出格式

严格输出以下 JSON，不要包裹在 markdown 代码块中：
{"title": "标题文字", "summary": "文章摘要", "body": "正文内容（支持Markdown）", "tags": ["话题1", "话题2"]}"""


# ────────── 生成器 ──────────

class ZhihuContentGenerator:
    """调用 LLM 将 WP 文章或本地文章转化为知乎文章"""

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def generate_from_post(self, post: WPPost) -> ZhihuContent:
        """输入 WordPress 文章，输出知乎文章对象"""
        user_msg = self._build_user_message_from_post(post)
        logger.info("生成知乎文案，文章: [%s] %s", post.id, post.title)

        raw = self._call_llm(user_msg)
        cover_urls = post.all_image_urls[:1] if hasattr(post, 'all_image_urls') else []
        content = self._parse_response(raw, fallback_title=post.title, cover_urls=cover_urls)
        logger.info("文案生成完成: %s", content.summary_text())
        return content

    def generate_from_article(self, article: dict, image_paths: Optional[list] = None) -> ZhihuContent:
        """从本地 article.json 生成知乎文章"""
        user_msg = self._build_user_message_from_article(article)
        logger.info("生成知乎文案（本地素材），标题: %s", article.get("title", ""))

        raw = self._call_llm(user_msg)
        covers = (image_paths or [])[:1]
        content = self._parse_response(raw, fallback_title=article.get("title", ""), cover_urls=covers)
        logger.info("文案生成完成（本地素材）: %s", content.summary_text())
        return content

    def _call_llm(self, user_msg: str) -> str:
        """调用 LLM"""
        raw = self.llm.chat(
            system_prompt=_ZHIHU_SYSTEM_PROMPT,
            user_prompt=user_msg,
            temperature=0.7,
        )
        if not raw:
            raise ContentGenError("LLM 调用失败，未返回内容")
        return raw

    @staticmethod
    def _parse_response(raw: str, fallback_title: str, cover_urls: list) -> ZhihuContent:
        """解析 LLM 返回的 JSON"""
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

        return ZhihuContent(
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
