"""
统一配置加载器 —— 从 .env 读取配置，支持环境变量覆盖
加载优先级：系统环境变量 > code/.env > 代码默认值
合并 wordpress 和 xiaohongshu 两套配置到一处
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List

from dotenv import load_dotenv

# 加载 .env（项目根目录 code/.env）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent  # code/
load_dotenv(_PROJECT_ROOT / ".env")


def _get_env(key: str, default: str = "") -> str:
    """安全读取环境变量，去除首尾空白"""
    return os.getenv(key, default).strip()


# ── 默认提示词模板 ──

_DEFAULT_PROMPT_TEMPLATE = (
    "主题：{theme}\n"
    "目标读者：企业管理者、业务负责人、团队Leader（懂业务但不一定懂技术）\n"
    "文章目标：让读者读完后知道「下一步该做什么」，而不是「这个东西很重要」\n"
    "语气风格：像跟朋友聊天一样专业——有态度、有细节、说人话\n"
    "内容要求：\n"
    "  - 每个核心观点用具体案例或场景数据支撑（不要写「某企业」）\n"
    "  - 包含实施路径、效果指标、常见坑和应对方案\n"
    "  - 段落要短，节奏要快，每段结尾有行动建议\n"
    "绝对禁止：空话套话、「随着…的发展」式开头、没有结论的描述、凭空编造的数据\n"
    "期望结果：一篇2000-3000字的中文文章，结构清晰、SEO/GEO友好、有配图空间。"
)

# 共享素材目录默认值：code/output/
_DEFAULT_OUTPUT_DIR = str(_PROJECT_ROOT / "output")


@dataclass(frozen=True)
class Settings:
    """全局统一配置"""

    # WordPress
    wp_base: str
    wp_user: str
    wp_app_password: str

    # 火山引擎（图片 & 视频）
    volc_ak: str
    volc_sk: str
    volc_host: str
    volc_service: str
    volc_region: str

    # DeepSeek / LLM
    deepseek_api_key: str
    deepseek_base_url: str
    deepseek_model: str
    deepseek_enabled: bool

    # fal.ai（数字人 OmniHuman）
    fal_key: str

    # 站点
    site_name: str

    # 共享素材目录
    output_dir: str

    # 小红书
    xhs_cookie: str
    xhs_auto_publish: bool
    xhs_publish_delay: int
    xhs_comment_delay: int       # 每条评论之间的间隔（秒）
    xhs_comment_max: int         # 单次任务最多评论数

    # 知乎
    zhihu_cookie: str
    zhihu_publish_delay: int

    # 微信视频号
    channels_cookie: str
    channels_publish_delay: int

    # 今日头条
    toutiao_cookie: str
    toutiao_publish_delay: int

    # 抖音
    douyin_cookie: str
    douyin_publish_delay: int

    # 微博
    weibo_cookie: str
    weibo_publish_delay: int

    # 发布默认值
    default_categories: str
    default_tags: str
    request_timeout: int
    max_content_images: int
    prompt_template: str

    def validate(self, require_wp: bool = True, require_llm: bool = True) -> List[str]:
        """校验必要配置，返回错误信息列表（空列表表示全部通过）"""
        errors = []
        if require_wp:
            if not self.wp_base:
                errors.append("WP_BASE 未配置")
            if not self.wp_user:
                errors.append("WP_USER 未配置")
            if not self.wp_app_password:
                errors.append("WP_APP_PASSWORD 未配置")
        if require_llm and not self.deepseek_api_key:
            errors.append("DEEPSEEK_API_KEY 未配置")
        if not self.volc_ak:
            errors.append("VOLC_AK 未配置")
        if not self.volc_sk:
            errors.append("VOLC_SK 未配置")
        return errors

    def check_or_exit(self, **kwargs) -> None:
        """校验配置，失败则抛出 ConfigError"""
        from shared.utils.exceptions import ConfigError

        errors = self.validate(**kwargs)
        if errors:
            msg = "配置检查未通过:\n" + "\n".join(f"  - {e}" for e in errors)
            raise ConfigError(msg)


def get_settings() -> Settings:
    """读取配置，环境变量优先。"""
    return Settings(
        # WordPress
        wp_base=_get_env("WP_BASE"),
        wp_user=_get_env("WP_USER"),
        wp_app_password=_get_env("WP_APP_PASSWORD"),
        # 火山引擎
        volc_ak=_get_env("VOLC_AK"),
        volc_sk=_get_env("VOLC_SK"),
        volc_host=_get_env("VOLC_HOST", "visual.volcengineapi.com"),
        volc_service=_get_env("VOLC_SERVICE", "cv"),
        volc_region=_get_env("VOLC_REGION", "cn-north-1"),
        # DeepSeek / LLM
        deepseek_api_key=_get_env("DEEPSEEK_API_KEY"),
        deepseek_base_url=_get_env("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/"),
        deepseek_model=_get_env("DEEPSEEK_MODEL", "deepseek-chat"),
        deepseek_enabled=_get_env("DEEPSEEK_ENABLED", "true").lower() in ("1", "true", "yes", "on"),
        # fal.ai
        fal_key=_get_env("FAL_KEY"),
        # 站点
        site_name=_get_env("SITE_NAME", "Aineoo"),
        # 共享素材
        output_dir=_get_env("OUTPUT_DIR") or _DEFAULT_OUTPUT_DIR,
        # 小红书
        xhs_cookie=_get_env("XHS_COOKIE"),
        xhs_auto_publish=_get_env("AUTO_PUBLISH", "false").lower() == "true",
        xhs_publish_delay=int(_get_env("PUBLISH_DELAY", "5") or "5"),
        xhs_comment_delay=int(_get_env("XHS_COMMENT_DELAY", "30") or "30"),
        xhs_comment_max=int(_get_env("XHS_COMMENT_MAX", "5") or "5"),
        # 知乎
        zhihu_cookie=_get_env("ZHIHU_COOKIE"),
        zhihu_publish_delay=int(_get_env("ZHIHU_PUBLISH_DELAY", "5") or "5"),
        # 微信视频号
        channels_cookie=_get_env("CHANNELS_COOKIE"),
        channels_publish_delay=int(_get_env("CHANNELS_PUBLISH_DELAY", "5") or "5"),
        # 今日头条
        toutiao_cookie=_get_env("TOUTIAO_COOKIE"),
        toutiao_publish_delay=int(_get_env("TOUTIAO_PUBLISH_DELAY", "5") or "5"),
        # 抖音
        douyin_cookie=_get_env("DOUYIN_COOKIE"),
        douyin_publish_delay=int(_get_env("DOUYIN_PUBLISH_DELAY", "5") or "5"),
        # 微博
        weibo_cookie=_get_env("WEIBO_COOKIE"),
        weibo_publish_delay=int(_get_env("WEIBO_PUBLISH_DELAY", "5") or "5"),
        # 发布默认值
        default_categories=_get_env("DEFAULT_CATEGORIES", "AI"),
        default_tags=_get_env("DEFAULT_TAGS", "AI"),
        request_timeout=int(_get_env("REQUEST_TIMEOUT", "40") or "40"),
        max_content_images=max(1, int(_get_env("MAX_CONTENT_IMAGES", "4") or "4")),
        prompt_template=_get_env("PROMPT_TEMPLATE") or _DEFAULT_PROMPT_TEMPLATE,
    )
