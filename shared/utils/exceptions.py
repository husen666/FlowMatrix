"""
统一异常体系 —— 合并 WordPress + 小红书两套异常
"""


class AppBaseError(Exception):
    """项目根异常"""

    def __init__(self, message: str = "", detail: str = ""):
        self.detail = detail
        super().__init__(message)


# ── WordPress 相关 ──

class WordPressError(AppBaseError):
    """WordPress API 调用异常"""


class WPAuthError(WordPressError):
    """WordPress 鉴权失败"""


class WPNotFoundError(WordPressError):
    """WordPress 文章不存在"""


# ── 内容生成 ──

class ContentGenError(AppBaseError):
    """内容生成异常"""


class LLMResponseError(ContentGenError):
    """LLM 返回了无法解析的内容"""


# ── 图片生成 ──

class ImageGenError(AppBaseError):
    """图片生成异常"""


# ── 视频生成 ──

class VideoGenError(AppBaseError):
    """视频生成异常"""


# ── 质量 ──

class QualityError(AppBaseError):
    """质量评分未通过"""


# ── 小红书发布相关 ──

class PublishError(AppBaseError):
    """小红书发布异常"""


class LoginError(PublishError):
    """小红书登录失败"""


class LoginTimeoutError(LoginError):
    """登录等待超时"""


class UploadError(PublishError):
    """图片上传失败"""


class CommentError(AppBaseError):
    """小红书评论异常"""


# ── 抖音发布相关 ──

class DouyinPublishError(AppBaseError):
    """抖音发布异常"""


class DouyinLoginTimeoutError(DouyinPublishError):
    """抖音登录等待超时"""


# ── 头条发布相关 ──

class ToutiaoPublishError(AppBaseError):
    """头条发布异常"""


class ToutiaoLoginTimeoutError(ToutiaoPublishError):
    """头条登录等待超时"""


# ── 知乎发布相关 ──

class ZhihuPublishError(AppBaseError):
    """知乎发布异常"""


class ZhihuLoginTimeoutError(ZhihuPublishError):
    """知乎登录等待超时"""


# ── 视频号发布相关 ──

class ChannelsPublishError(AppBaseError):
    """视频号发布异常"""


class ChannelsLoginTimeoutError(ChannelsPublishError):
    """视频号登录等待超时"""


# ── 微博发布相关 ──

class WeiboPublishError(AppBaseError):
    """微博发布异常"""


class WeiboLoginTimeoutError(WeiboPublishError):
    """微博登录等待超时"""


# ── 配置 ──

class ConfigError(AppBaseError):
    """配置缺失或不合法"""
