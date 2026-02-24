"""
微信视频号文案生成器
—— 将 WordPress 文章或本地 article.json 通过 LLM 改写为视频号动态文案
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from shared.llm.client import LLMClient, extract_json_block
from shared.utils.exceptions import ContentGenError, LLMResponseError
from shared.utils.logger import get_logger
from shared.wp.client import WPPost

logger = get_logger("channels-content")


# ────────── 数据模型 ──────────

@dataclass
class ChannelsContent:
    """微信视频号动态内容"""
    body: str                                            # 正文/描述
    title: str = ""                                      # 图文标题（新版图文必填）
    hashtags: List[str] = field(default_factory=list)    # 话题标签
    image_urls: List[str] = field(default_factory=list)  # 配图 URL 或本地路径

    # 视频号字数上限
    CHANNELS_MAX_TITLE_LENGTH = 22
    CHANNELS_MAX_BODY_LENGTH = 1000

    def __post_init__(self):
        """确保标题不超限"""
        if self.title and len(self.title) > self.CHANNELS_MAX_TITLE_LENGTH:
            self.title = self.title[:self.CHANNELS_MAX_TITLE_LENGTH]

    def full_text(self) -> str:
        """拼装完整发布文本（正文 + 话题标签），确保不超过字数限制"""
        tags_str = " ".join(f"#{t}" for t in self.hashtags)
        if not tags_str:
            return self.body[:self.CHANNELS_MAX_BODY_LENGTH]
        full = f"{self.body}\n\n{tags_str}"
        if len(full) <= self.CHANNELS_MAX_BODY_LENGTH:
            return full
        # 超长时截断正文部分，保留话题标签
        max_body = self.CHANNELS_MAX_BODY_LENGTH - len(tags_str) - 5
        return f"{self.body[:max_body]}...\n\n{tags_str}"

    def summary(self) -> str:
        title_info = f" 标题:{self.title[:20]}" if self.title else ""
        return f"[视频号]{title_info} {len(self.body)}字 {len(self.hashtags)}标签 {len(self.image_urls)}图"

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "body": self.body,
            "hashtags": self.hashtags,
            "full_text": self.full_text(),
            "image_urls": self.image_urls,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ChannelsContent":
        title = data.get("title", "")
        body = data["body"]
        # 兼容旧版本无 title 的情况：从正文第一行提取
        if not title and body:
            first_line = body.split("\n")[0].strip()
            # 去掉 emoji 等前缀，截取前 22 字作为标题
            clean = first_line.lstrip("0123456789️⃣.!！?？·•#🔥💡🌟🚀📢")
            title = clean[:22] if clean else first_line[:22]
        return cls(
            body=body,
            title=title,
            hashtags=data.get("hashtags", []),
            image_urls=data.get("image_urls", []),
        )


# ────────── Prompt ──────────

_CHANNELS_SYSTEM_PROMPT = """你是一位微信视频号万粉创作者，精通微信生态内容运营，擅长制作高转发率的图文动态。

## 视频号图文特性

视频号流量来源：朋友圈转发 > 好友点赞推荐 > 搜索 > 推荐流。
核心逻辑：**内容要让人愿意转发到朋友圈**。微信用户转发的心理：「这篇说得对」「这对朋友有用」「转发体现我的品味/见识」。

## 标题

### 标题公式（任选一种）
1. 价值提炼式：「X 个…值得收藏」「一文讲清…」
2. 痛点直击式：「为什么你的…总是不行？」「…的人必看」
3. 数字清单式：「X 条…建议，条条实用」

### 标题规则
- **不超过 22 个字**（硬性限制！），最佳 10-18 字
- 简洁有力，一眼能看明白
- 不用 emoji（视频号标题不适合放 emoji）
- 体现专业性和实用性，让人愿意点开

## 正文结构

### 开篇
- 用一句话点明核心价值或抛出一个共鸣感强的问题
- 微信用户偏成熟理性，不要太浮夸
- 示例：「最近和几位创业朋友聊天，发现大家都在关心一个问题：…」

### 主体
- 用分段和序号（1. 2. 3. 或一、二、三）组织核心要点
- 每个要点 2-3 句话，清晰实用
- 语气像在微信群里给朋友分享一个有用的信息
- 适当使用 emoji 点缀（📌💡✅🔑），每段最多 1 个
- 穿插一两个具体场景或数据，增加说服力
- 内容要有「分享价值」——读者看完觉得「这个有用，转给朋友看看」

### 结尾
- 一句话总结核心观点
- 自然引导互动：点赞/转发/关注
- 微信风格的互动引导更内敛：
  - 「觉得有用就转发给需要的朋友」
  - 「你在工作中遇到过类似的问题吗？」
  - 「关注我，持续分享…领域的实用干货」

### 字数
- 正文控制在 **300-700 字**（短而精，方便朋友圈阅读）

## 话题标签
- 给出 **3-5 个**相关话题标签
- 只输出标签文字，不带 # 号
- 选择微信生态内有搜索热度的标签
- 第 1 个为大行业词，后面为精准话题词

## 语言风格
- 像在微信群里分享干货，专业但不学术
- 语气成熟、稳重，不要太活泼或太浮夸
- 用「你」「我们」与读者对话
- 禁止使用：「赋能」「闭环」「抓手」「全链路」等空洞词汇
- 禁止使用：「随着…的发展」「综上所述」等空洞开头

## 重要
- 不要虚构任何事实，完全基于原文信息改写
- 内容质量 > 营销话术，微信用户对低质内容容忍度极低
- 不要输出任何解释说明，只输出 JSON

## 输出格式

严格输出以下 JSON，不要包裹在 markdown 代码块中：
{"title": "图文标题", "body": "正文内容", "hashtags": ["标签1", "标签2"]}"""


# ────────── 生成器 ──────────

class ChannelsContentGenerator:
    """调用 LLM 将 WP 文章或本地文章转化为视频号文案"""

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def generate_from_post(self, post: WPPost) -> ChannelsContent:
        """输入 WordPress 文章，输出视频号动态内容"""
        user_msg = self._build_user_message_from_post(post)
        logger.info("生成视频号文案，文章: [%s] %s", post.id, post.title)

        raw = self._call_llm(user_msg)
        content = self._parse_response(raw, image_urls=post.all_image_urls[:9])
        logger.info("文案生成完成: %s", content.summary())
        return content

    def generate_from_article(self, article: dict, image_paths: Optional[list] = None) -> ChannelsContent:
        """从本地 article.json 生成视频号文案"""
        user_msg = self._build_user_message_from_article(article)
        logger.info("生成视频号文案（本地素材），标题: %s", article.get("title", ""))

        raw = self._call_llm(user_msg)
        images = (image_paths or [])[:9]
        content = self._parse_response(raw, image_urls=images)
        logger.info("文案生成完成（本地素材）: %s", content.summary())
        return content

    def _call_llm(self, user_msg: str) -> str:
        """调用 LLM"""
        raw = self.llm.chat(
            system_prompt=_CHANNELS_SYSTEM_PROMPT,
            user_prompt=user_msg,
            temperature=0.7,
        )
        if not raw:
            raise ContentGenError("LLM 调用失败，未返回内容")
        return raw

    @staticmethod
    def _parse_response(raw: str, image_urls: list) -> ChannelsContent:
        """解析 LLM 返回的 JSON"""
        data = extract_json_block(raw)
        if data is None:
            raise LLMResponseError(f"无法从 LLM 输出中提取 JSON: {raw[:200]}")

        body = data.get("body", "").strip()
        if not body:
            raise LLMResponseError("LLM 返回的正文为空")

        title = data.get("title", "").strip()

        hashtags = data.get("hashtags", [])
        if not isinstance(hashtags, list):
            hashtags = []
        hashtags = [str(t).strip() for t in hashtags if t]

        return ChannelsContent(
            body=body,
            title=title,
            hashtags=hashtags,
            image_urls=image_urls,
        )

    @staticmethod
    def _build_user_message_from_post(post: WPPost) -> str:
        parts = [f"文章标题：{post.title}"]
        if post.excerpt:
            parts.append(f"文章摘要：{post.excerpt}")
        content_text = post.content[:3000]
        if len(post.content) > 3000:
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
