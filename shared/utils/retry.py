"""
通用重试装饰器 —— 支持指数退避、可选异常过滤
"""

import functools
import time
from typing import Tuple, Type

from shared.utils.logger import get_logger

logger = get_logger("retry")


def retry(
    max_retries: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: Tuple[Type[BaseException], ...] = (Exception,),
):
    """
    重试装饰器

    Args:
        max_retries: 最大重试次数（不含首次）
        delay: 初始等待秒数
        backoff: 退避倍数（每次重试等待 = delay * backoff^n）
        exceptions: 需要重试的异常类型元组
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            wait = delay
            for attempt in range(1, max_retries + 2):  # 首次 + max_retries 次重试
                try:
                    return func(*args, **kwargs)
                except exceptions as e:
                    last_exc = e
                    if attempt > max_retries:
                        logger.error(
                            "[%s] 第 %d 次调用失败，已达最大重试次数: %s",
                            func.__name__, attempt, e,
                        )
                        raise
                    logger.warning(
                        "[%s] 第 %d 次调用失败，%.1fs 后重试: %s",
                        func.__name__, attempt, wait, e,
                    )
                    time.sleep(wait)
                    wait *= backoff
            raise last_exc  # 理论上不会到这里
        return wrapper
    return decorator
