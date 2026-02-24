"""
今日头条文案生成器
—— 将 WordPress 文章或本地 article.json 通过 LLM 改写为头条号文章
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from shared.llm.client import LLMClient, extract_json_block
from shared.utils.exceptions import ContentGenError, LLMResponseError
from shared.utils.logger import get_logger
from shared.wp.client import WPPost

logger = get_logger("toutiao-content")


# ────────── 数据模型 ──────────

@dataclass
class ToutiaoContent:
    """今日头条文章内容"""
    title: str                                           # ≤30字，吸引点击
    body: str                                            # 正文，段落分明
    tags: List[str] = field(default_factory=list)        # 文章标签
    cover_urls: List[str] = field(default_factory=list)  # 封面图 URL 或本地路径（1~3张）

    # 头条正文无严格字数限制，但推荐 800-2000 字
    TOUTIAO_TITLE_MAX = 30

    def full_text(self) -> str:
        """拼装完整发布文本"""
        return self.body

    def summary(self) -> str:
        return f"[{self.title}] {len(self.body)}字 {len(self.tags)}标签 {len(self.cover_urls)}图"

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "body": self.body,
            "tags": self.tags,
            "cover_urls": self.cover_urls,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ToutiaoContent":
        return cls(
            title=data["title"],
            body=data["body"],
            tags=data.get("tags", []),
            cover_urls=data.get("cover_urls", []),
        )


# ────────── Prompt ──────────

_TOUTIAO_SYSTEM_PROMPT = """你是一位头条号十万粉丝级科技财经领域创作者，擅长写出高阅读、高互动的深度科普文章。

## 头条平台特性

头条用户画像：25-55 岁，偏好有信息量的深度内容，厌恶纯标题党。
算法核心：点击率 × 完读率 × 互动率。标题要吸引点击，正文要留住读者读完。

## 标题公式（任选一种）

1. 数字+利益式：「X 个方法让你的…提升 X 倍」「2026 年最值得关注的 X 个…」
2. 权威背书式：「XX 报告揭示：…的 X 大趋势」「从业 X 年才明白的…」
3. 反常识式：「你以为…其实…」「90% 的人都搞错了的…」
4. 时效+痛点式：「2026 年了，还在用…的人要注意了」

### 标题规则
- **不超过 30 个字**
- 信息量要足，让人看标题就知道能获得什么
- 可以用数字、问号增加吸引力
- 禁止使用引号包裹标题
- 不做低质量标题党（「震惊」「太可怕了」等禁止出现）

## 正文结构

### 开篇（第 1 段）
- 直接抛出核心观点或一个有冲击力的事实/数据
- 让读者明确「读完这篇文章我能获得什么」
- 禁止用「随着…的发展」「近年来」等空洞开头

### 主体（3-5 个小节）
- 每个小节用 **加粗小标题** 分隔（用「**标题**」格式）
- 小标题要有信息量，不要用「第一部分」「首先」这种空标题
- 每段 3-5 句话，逻辑清晰
- 必须包含：
  - 至少 2 个具体案例（写明行业、规模、做了什么、效果如何）
  - 至少 2 处数据引用（标明来源，如「据 Gartner 报告」「根据工信部数据」）
  - 与读者相关的「你可以怎么做」建议
- 善用对比（前/后、有/没有、A方案/B方案）增强说服力

### 结尾
- 用 2-3 句话总结核心观点
- 抛出一个开放性问题引导评论（头条算法重视评论互动）
- 示例：「对此你怎么看？你觉得…会不会…？欢迎在评论区分享你的看法。」

### 字数
- 正文控制在 **1000-2000 字**

## 标签
- 给出 **3-5 个**相关标签
- 只输出标签文字，不带 # 号
- 第 1 个为核心主题词，后面为长尾词和行业词
- 考虑头条搜索热度

## 语言风格
- 专业但不枯燥，像一位懂行的朋友在给你讲事情
- 用第二人称「你」与读者对话
- 适当使用短句和反问增加节奏感
- 禁止使用：「赋能」「闭环」「抓手」「助力」「数智化」等空洞词汇
- 禁止使用：「不言而喻」「毋庸置疑」「众所周知」等学术套话

## 重要
- 不要虚构任何事实，完全基于原文信息改写
- 案例和数据必须基于原文，不可凭空编造
- 不要输出任何解释说明，只输出 JSON

## 输出格式

严格输出以下 JSON，不要包裹在 markdown 代码块中：
{"title": "标题文字", "body": "正文内容", "tags": ["标签1", "标签2"]}"""


# ────────── 生成器 ──────────

class ToutiaoContentGenerator:
    """调用 LLM 将 WP 文章或本地文章转化为头条号文章"""

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def generate_from_post(self, post: WPPost) -> ToutiaoContent:
        """输入 WordPress 文章，输出头条号文章对象"""
        user_msg = self._build_user_message_from_post(post)
        logger.info("生成头条文案，文章: [%s] %s", post.id, post.title)

        raw = self._call_llm(user_msg)
        cover_urls = post.all_image_urls[:3] if hasattr(post, 'all_image_urls') else []
        content = self._parse_response(raw, fallback_title=post.title, cover_urls=cover_urls)
        logger.info("文案生成完成: %s", content.summary())
        return content

    def generate_from_article(self, article: dict, image_paths: Optional[list] = None) -> ToutiaoContent:
        """从本地 article.json 生成头条号文章"""
        user_msg = self._build_user_message_from_article(article)
        logger.info("生成头条文案（本地素材），标题: %s", article.get("title", ""))

        raw = self._call_llm(user_msg)
        covers = (image_paths or [])[:3]
        content = self._parse_response(raw, fallback_title=article.get("title", "")[:30], cover_urls=covers)
        logger.info("文案生成完成（本地素材）: %s", content.summary())
        return content

    def _call_llm(self, user_msg: str) -> str:
        """调用 LLM"""
        raw = self.llm.chat(
            system_prompt=_TOUTIAO_SYSTEM_PROMPT,
            user_prompt=user_msg,
            temperature=0.7,
        )
        if not raw:
            raise ContentGenError("LLM 调用失败，未返回内容")
        return raw

    @staticmethod
    def _parse_response(raw: str, fallback_title: str, cover_urls: list) -> ToutiaoContent:
        """解析 LLM 返回的 JSON"""
        data = extract_json_block(raw)
        if data is None:
            raise LLMResponseError(f"无法从 LLM 输出中提取 JSON: {raw[:200]}")

        title = data.get("title", "").strip() or fallback_title
        body = data.get("body", "").strip()
        if not body:
            raise LLMResponseError("LLM 返回的正文为空")

        tags = data.get("tags", [])
        if not isinstance(tags, list):
            tags = []
        tags = [str(t).strip() for t in tags if t]

        return ToutiaoContent(
            title=title,
            body=body,
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
