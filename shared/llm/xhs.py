"""
小红书文案生成器
—— 将 WordPress 文章或本地 article.json 通过 LLM 改写为小红书笔记
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from shared.llm.client import LLMClient, extract_json_block
from shared.utils.exceptions import ContentGenError, LLMResponseError
from shared.utils.logger import get_logger
from shared.wp.client import WPPost

logger = get_logger("xhs-content")


# ────────── 数据模型 ──────────

@dataclass
class XHSContent:
    """小红书笔记内容"""
    title: str                                           # ≤20字，带 emoji
    body: str                                            # 正文，段落分明，带 emoji
    hashtags: List[str] = field(default_factory=list)    # 话题标签
    image_urls: List[str] = field(default_factory=list)  # 配图 URL 或本地路径

    # 小红书正文字数上限
    XHS_MAX_BODY_LENGTH = 1000

    def full_text(self) -> str:
        """拼装完整发布文本（正文 + 话题标签），确保不超过小红书字数限制"""
        tags_str = " ".join(f"#{t}" for t in self.hashtags)
        if not tags_str:
            return self.body[:self.XHS_MAX_BODY_LENGTH]
        full = f"{self.body}\n\n{tags_str}"
        if len(full) <= self.XHS_MAX_BODY_LENGTH:
            return full
        # 超长时截断正文部分，保留话题标签
        max_body = self.XHS_MAX_BODY_LENGTH - len(tags_str) - 5
        return f"{self.body[:max_body]}...\n\n{tags_str}"

    def summary(self) -> str:
        return f"[{self.title}] {len(self.body)}字 {len(self.hashtags)}标签 {len(self.image_urls)}图"

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "body": self.body,
            "hashtags": self.hashtags,
            "full_text": self.full_text(),
            "image_urls": self.image_urls,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "XHSContent":
        return cls(
            title=data["title"],
            body=data["body"],
            hashtags=data.get("hashtags", []),
            image_urls=data.get("image_urls", []),
        )


# ────────── Prompt ──────────

_XHS_SYSTEM_PROMPT = """你是一位全网粉丝 50w+ 的小红书头部博主，精通爆款笔记的写作技巧。

## 标题公式（任选一种）

1. 数字清单式：「X 个…让你…」「这 X 招…我后悔没早知道」
2. 反差/颠覆式：「别再…了！试试这个方法」「原来…这么简单」
3. 痛点共鸣式：「…的人一定要看」「为什么你的…总是不行？」
4. 好奇驱动式：「我靠这招…效果惊人」「终于找到…的正确打开方式」

### 标题规则
- **不超过 20 个字**（硬性限制）
- 带 1-2 个 emoji（放标题前面或结尾，不要夹在文字中间）
- 禁止使用引号、书名号
- 用口语化表达，不要像新闻标题

## 正文结构

### 开头（Hook）— 前 2 行决定生死
- 用一句话制造「共鸣感」或「好奇心」
- 示例句式：「姐妹们！…的时候是不是也…」「说真的，用了…之后我整个人都不一样了」「搞了X年…终于整明白了」

### 中间（干货主体）
- **必须使用分点/序号结构**：用 ①②③ 或 1️⃣2️⃣3️⃣ 列出要点（小红书用户最爱收藏清单型内容）
- 每个要点 1-2 句话，说人话，不要用书面语
- 穿插真实场景/使用体验描述，增加代入感
- 如有数据或案例，用「举个例子」「就拿…来说」等口语化引入
- 每段末尾可加 emoji 点缀（🔥💡✅🎯📌），每段最多 1-2 个

### 结尾（互动引导）
- 用一句话总结核心价值
- 自然引导互动（二选一式提问效果最好）
- 示例：「你们觉得…还是…更好用？评论区聊聊～」「觉得有用就收藏起来💫 下次用得上！」

### 字数
- 正文严格控制在 **400-800 字**（小红书限制 1000 字，需预留话题标签空间）

## 话题标签
- 给出 **5-8 个**相关话题标签
- 只输出标签文字，不带 # 号
- 前 2 个为热门大话题（如「职场干货」「效率工具」），后面为长尾精准话题
- 最后 1 个可放品牌/工具名称相关话题

## 语言风格
- 像跟好朋友聊天，用「我」「你」「咱」「姐妹」
- 可以用「！」表达情绪，但不要每句都用
- 禁止使用：「赋能」「闭环」「抓手」「助力」「不言而喻」等官方词汇
- 禁止使用：「随着…的发展」「在…背景下」「综上所述」等学术开头
- 允许偶尔用网络热梗，但不要硬凹

## 重要
- 不要虚构任何事实，完全基于原文信息改写
- 不要输出任何解释说明，只输出 JSON

## 输出格式

严格输出以下 JSON，不要包裹在 markdown 代码块中：
{"title": "标题文字", "body": "正文内容", "hashtags": ["标签1", "标签2"]}"""


_COMMENT_SYSTEM_PROMPT = """你是一位小红书资深用户，擅长在热门笔记下留下有价值的评论，帮助自己的笔记获得曝光。

## 评论规则

### 基本要求
- 评论必须与热门笔记内容相关，提供真正有价值的见解或补充
- 语气自然亲切，像普通用户在交流，绝对不能有广告嫌疑
- 评论长度 30-100 字，不宜过长也不能过短
- 可以适当使用 1-2 个 emoji，但不要堆砌

### 引流策略
- 在评论中自然地提到你也研究/实践过相关话题
- 可以分享一个自己相关的小经验或补充观点
- 引发好奇心，让人想看你的主页了解更多
- 绝对不要直接说"看我的笔记"或放链接
- 不要直接提及自己发了相关笔记

### 风格说明
- professional（专业）：以专业视角补充见解，体现专业度
- casual（随意）：像朋友聊天一样轻松回复，有共鸣感
- enthusiastic（热情）：表达强烈认同并补充自己的实践经验

### 绝对禁止
- 广告词、营销话术
- 贬低原笔记内容
- 无关内容或纯表情
- 直接引导到自己的笔记/主页
- "互关"、"回关"等低质量互动

## 输出
只输出一条评论文本，不要加任何解释、引号或格式标记。"""


# ────────── 生成器 ──────────

class XHSContentGenerator:
    """调用 LLM 将 WP 文章或本地文章转化为小红书文案"""

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def generate_from_post(self, post: WPPost) -> XHSContent:
        """输入 WordPress 文章，输出小红书笔记对象"""
        user_msg = self._build_user_message_from_post(post)
        logger.info("生成小红书文案，文章: [%s] %s", post.id, post.title)

        raw = self._call_llm(user_msg)
        content = self._parse_response(raw, fallback_title=post.title, image_urls=post.all_image_urls[:9])
        logger.info("文案生成完成: %s", content.summary())
        return content

    def generate_from_article(self, article: dict, image_paths: Optional[list] = None) -> XHSContent:
        """从本地 article.json 生成小红书文案"""
        user_msg = self._build_user_message_from_article(article)
        logger.info("生成小红书文案（本地素材），标题: %s", article.get("title", ""))

        raw = self._call_llm(user_msg)
        images = (image_paths or [])[:9]
        content = self._parse_response(raw, fallback_title=article.get("title", "")[:20], image_urls=images)
        logger.info("文案生成完成（本地素材）: %s", content.summary())
        return content

    def _call_llm(self, user_msg: str) -> str:
        """调用 LLM"""
        raw = self.llm.chat(
            system_prompt=_XHS_SYSTEM_PROMPT,
            user_prompt=user_msg,
            temperature=0.7,
        )
        if not raw:
            raise ContentGenError("LLM 调用失败，未返回内容")
        return raw

    @staticmethod
    def _parse_response(raw: str, fallback_title: str, image_urls: list) -> XHSContent:
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

        return XHSContent(
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
        if len(post.content) > 3000:
            content_text += "\n...(正文已截断)"
        parts.append(f"文章正文：\n{content_text}")
        if post.tags:
            parts.append(f"文章标签：{', '.join(post.tags)}")
        if post.categories:
            parts.append(f"文章分类：{', '.join(post.categories)}")
        return "\n\n".join(parts)

    # ── 评论生成 ──

    def generate_comment(
        self,
        note_title: str,
        note_body: str,
        my_note_title: str,
        my_note_summary: str,
        style: str = "professional",
    ) -> str:
        """
        基于热门笔记内容和自己的笔记信息，生成一条自然、有价值的引流评论。

        Args:
            note_title: 热门笔记标题
            note_body: 热门笔记正文片段
            my_note_title: 自己发布的笔记标题
            my_note_summary: 自己笔记的简短摘要/核心卖点
            style: 评论风格（professional / casual / enthusiastic）
        """
        logger.info("生成引流评论，目标笔记: %s", note_title[:30])
        user_msg = (
            f"## 我要评论的热门笔记\n"
            f"标题：{note_title}\n"
            f"正文片段：{note_body[:500]}\n\n"
            f"## 我自己的笔记信息\n"
            f"标题：{my_note_title}\n"
            f"核心内容：{my_note_summary}\n\n"
            f"## 评论风格要求\n"
            f"风格：{style}"
        )
        raw = self.llm.chat(
            system_prompt=_COMMENT_SYSTEM_PROMPT,
            user_prompt=user_msg,
            temperature=0.8,
        )
        if not raw:
            raise ContentGenError("LLM 评论生成失败")

        # 提取纯文本评论（去除引号和多余空白）
        comment = raw.strip().strip('"').strip("'").strip()
        # 如果 LLM 返回了 JSON，提取 comment 字段
        parsed = extract_json_block(raw)
        if parsed and "comment" in parsed:
            comment = parsed["comment"].strip()

        logger.info("评论生成完成 (%d字): %s", len(comment), comment[:50])
        return comment

    def generate_comments_batch(
        self,
        notes: list,
        my_note_title: str,
        my_note_summary: str,
        style: str = "professional",
    ) -> list:
        """
        为多条热门笔记批量生成评论。

        Args:
            notes: [{"title": ..., "body": ...}, ...]
            my_note_title: 自己的笔记标题
            my_note_summary: 自己笔记的摘要
            style: 评论风格

        Returns:
            [{"note_title": ..., "comment": ...}, ...]
        """
        results = []
        for note in notes:
            try:
                comment = self.generate_comment(
                    note_title=note.get("title", ""),
                    note_body=note.get("body", ""),
                    my_note_title=my_note_title,
                    my_note_summary=my_note_summary,
                    style=style,
                )
                results.append({
                    "note_title": note.get("title", ""),
                    "comment": comment,
                })
            except Exception as e:
                logger.warning("评论生成失败，跳过: %s - %s", note.get("title", "")[:20], e)
        return results

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
