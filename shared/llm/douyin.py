"""
抖音文案生成器
—— 将 WordPress 文章或本地 article.json 通过 LLM 改写为抖音图文笔记
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional

from shared.llm.client import LLMClient, extract_json_block
from shared.utils.exceptions import ContentGenError, LLMResponseError
from shared.wp.client import WPPost

logger = logging.getLogger("douyin-content")


@dataclass
class DouyinContent:
    """抖音图文笔记内容"""
    title: str                                           # ≤30字
    body: str                                            # 正文描述
    hashtags: List[str] = field(default_factory=list)    # 话题标签
    image_urls: List[str] = field(default_factory=list)  # 配图 URL 或本地路径

    # 抖音图文正文字数上限（抖音「作品描述」字段限制 1000 字符）
    DOUYIN_MAX_BODY_LENGTH = 1000

    def full_text(self) -> str:
        """拼装完整发布文本（正文 + 话题标签），确保不超过抖音字数限制"""
        tags_str = " ".join(f"#{t}" for t in self.hashtags)
        if not tags_str:
            return self.body[:self.DOUYIN_MAX_BODY_LENGTH]
        full = f"{self.body}\n\n{tags_str}"
        if len(full) <= self.DOUYIN_MAX_BODY_LENGTH:
            return full
        # 超长时截断正文部分，保留话题标签
        max_body = self.DOUYIN_MAX_BODY_LENGTH - len(tags_str) - 5
        return f"{self.body[:max_body]}...\n\n{tags_str}"

    def summary(self) -> str:
        return f"[{self.title}] {len(self.body)}字 {len(self.hashtags)}标签 {len(self.image_urls)}图"

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "body": self.body,
            "hashtags": self.hashtags,
            "image_urls": self.image_urls,
            "full_text": self.full_text(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DouyinContent":
        return cls(
            title=data["title"],
            body=data["body"],
            hashtags=data.get("hashtags", []),
            image_urls=data.get("image_urls", []),
        )


# ────────── Prompt ──────────

_DOUYIN_SYSTEM_PROMPT = """你是一位抖音百万级创作者，精通图文笔记的爆款写法，擅长用短内容获取高互动。

## 抖音图文特性

抖音图文是「一张图 + 一段描述」的形式，用户左右滑动看图，描述在下方。
核心逻辑：**图片吸引停留，描述驱动互动**。你的文案是配合图片的「描述文案」。

## 标题公式（任选一种）

1. 悬念反转式：「没想到…竟然…」「千万别…否则…」
2. 数字冲击式：「3 个月从…到…」「99% 的人不知道的…」
3. 对比冲突式：「别人…vs 我…」「用了 X 之后 vs 没用之前」
4. 痛点扎心式：「还在…？难怪你…」「为什么你…？因为…」

### 标题规则
- **不超过 30 个字**
- 带 1-2 个 emoji（前置吸睛）
- 禁止使用引号
- 语气直接、有冲击力，3 秒内让人想点开

## 正文（作品描述）

### 结构：3 段式精炼法
1. **Hook 一句话**（5 秒决定滑走还是留下）
   - 抛出一个反常识观点或扎心问题
   - 示例：「你有没有发现，越努力越忙的人效率反而最低？」
2. **核心干货**（3-5 个要点，简短有力）
   - 用「→」或序号分隔要点
   - 每个要点不超过 2 句话
   - 口语化，像语音转文字的感觉
   - 可以用对比、反差来强化记忆
3. **互动钩子**（必须有！抖音算法看评论量）
   - 抛出选择题 / 争议观点 / 求评论
   - 示例：「你觉得 A 好还是 B 好？」「同意的扣 1，不同意的说说你的看法」

### 字数
- 正文严格控制在 **200-600 字**（抖音描述上限 1000 字符，但短内容完播率更高）

### 节奏感
- 多用短句，少用长句（每句不超过 20 字）
- 适当换行，制造「呼吸感」
- 关键词可以用【】或「」框起来强调

## 话题标签
- 给出 **3-6 个**相关话题标签
- 只输出标签文字，不带 # 号
- 第 1 个为行业大词（如「职场」「AI」），后面为精准长尾词
- 最后 1 个可蹭热点话题（如「2026必看」）

## 语言风格
- 短句为主，有节奏感，像在说话不像在写文章
- 禁止使用：「赋能」「闭环」「综上所述」「不言而喻」等书面词
- 禁止使用：「随着…的发展」「在…背景下」等学术开头
- 可以偶尔用网络热词和口头禅增加亲和力

## 重要
- 不要虚构任何事实，完全基于原文信息改写
- 不要输出任何解释说明，只输出 JSON

## 输出格式

严格输出以下 JSON，不要包裹在 markdown 代码块中：
{"title": "标题文字", "body": "正文内容", "hashtags": ["标签1", "标签2"]}"""


# ────────── 生成器 ──────────

class DouyinContentGenerator:
    """调用 LLM 将 WP 文章或本地文章转化为抖音图文文案"""

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def generate_from_post(self, post: WPPost) -> DouyinContent:
        """输入 WordPress 文章，输出抖音图文笔记对象"""
        user_msg = self._build_user_message_from_post(post)
        logger.info("生成抖音文案，文章: [%s] %s", post.id, post.title)

        raw = self._call_llm(user_msg)
        content = self._parse_response(raw, fallback_title=post.title, image_urls=post.all_image_urls[:9])
        logger.info("文案生成完成: %s", content.summary())
        return content

    def generate_from_article(self, article: dict, image_paths: Optional[list] = None) -> DouyinContent:
        """从本地 article.json 生成抖音文案"""
        user_msg = self._build_user_message_from_article(article)
        logger.info("生成抖音文案（本地素材），标题: %s", article.get("title", ""))

        raw = self._call_llm(user_msg)
        images = (image_paths or [])[:9]
        content = self._parse_response(raw, fallback_title=article.get("title", "")[:30], image_urls=images)
        logger.info("文案生成完成（本地素材）: %s", content.summary())
        return content

    def _call_llm(self, user_msg: str) -> str:
        """调用 LLM"""
        raw = self.llm.chat(
            system_prompt=_DOUYIN_SYSTEM_PROMPT,
            user_prompt=user_msg,
            temperature=0.7,
        )
        if not raw:
            raise ContentGenError("LLM 调用失败，未返回内容")
        return raw

    @staticmethod
    def _parse_response(raw: str, fallback_title: str, image_urls: list) -> DouyinContent:
        """解析 LLM 返回的 JSON"""
        data = extract_json_block(raw)
        if data is None:
            raise LLMResponseError(f"无法从 LLM 输出中提取 JSON: {raw[:200]}")

        title = data.get("title", "").strip() or fallback_title
        body = data.get("body", "").strip()
        if not body:
            raise LLMResponseError("LLM 返回的正文为空")

        hashtags = data.get("hashtags", [])
        if not isinstance(hashtags, list):
            hashtags = []
        hashtags = [str(t).strip() for t in hashtags if t]

        return DouyinContent(
            title=title,
            body=body,
            hashtags=hashtags,
            image_urls=image_urls,
        )

    @staticmethod
    def _build_user_message_from_post(post: WPPost) -> str:
        parts = [f"文章标题：{post.title}"]
        if post.excerpt:
            parts.append(f"文章摘要：{post.excerpt}")
        content_text = post.content[:3000]
        parts.append(f"文章正文：\n{content_text}")
        if post.categories:
            parts.append(f"文章分类：{', '.join(post.categories)}")
        if post.tags:
            parts.append(f"文章标签：{', '.join(post.tags)}")
        return "\n\n".join(parts)

    @staticmethod
    def _build_user_message_from_article(article: dict) -> str:
        parts = [f"文章标题：{article.get('title', '')}"]
        excerpt = article.get("excerpt", "")
        if excerpt:
            parts.append(f"文章摘要：{excerpt}")
        # 优先从 sections 构建正文，兼容 body/content 字段
        sections = article.get("sections", [])
        if sections:
            body_parts = []
            for sec in sections:
                heading = sec.get("title", sec.get("heading", ""))
                paragraphs = sec.get("paragraphs", [])
                if heading:
                    body_parts.append(heading)
                body_parts.extend(paragraphs)
            body_text = "\n".join(body_parts)
        else:
            body_text = article.get("body", "") or article.get("content", "")
        if len(body_text) > 3000:
            body_text = body_text[:3000] + "\n...(正文已截断)"
        parts.append(f"文章正文：\n{body_text}")
        takeaways = article.get("key_takeaways", [])
        if takeaways:
            parts.append(f"关键要点：{', '.join(takeaways)}")
        tags = article.get("tags", [])
        if tags:
            parts.append(f"文章标签：{', '.join(tags)}")
        return "\n\n".join(parts)
