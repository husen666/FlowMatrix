"""
图片生成模块
—— 使用火山引擎 VisualService 生成图片
从 wordpress/publisher/pipeline.py 提取
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Optional

from volcengine.visual.VisualService import VisualService

from shared.utils.logger import get_logger

logger = get_logger("image-gen")


class ImageGenerator:
    """火山引擎图片生成客户端"""

    MAX_RETRIES = 2

    def __init__(self, volc_ak: str, volc_sk: str) -> None:
        self.visual = VisualService()
        self.visual.set_ak(volc_ak)
        self.visual.set_sk(volc_sk)

    def generate(
        self,
        prompt: str,
        save_path: Path,
        width: int = 1344,
        height: int = 768,
        seed: int = -1,
    ) -> Optional[Path]:
        """调用火山引擎生成图片并保存到本地（自动重试）

        Args:
            prompt: 图片描述（纯英文）
            save_path: 保存路径
            width: 图片宽度
            height: 图片高度
            seed: 随机种子（同一 seed 生成风格更一致，-1 为随机）
        """
        params = {
            "req_key": "high_aes_general_v30l_zt2i",
            "prompt": prompt,
            "use_pre_llm": True,
            "width": width,
            "height": height,
            "seed": seed,
        }
        last_error = ""
        for attempt in range(1, self.MAX_RETRIES + 2):
            t0 = time.monotonic()
            try:
                result = self.visual.cv_process(params)
                if result.get("code") != 10000:
                    last_error = f"code={result.get('code')} msg={result.get('message')}"
                    if attempt <= self.MAX_RETRIES:
                        logger.warning("图片生成第 %d 次失败（重试中）：%s", attempt, last_error)
                        time.sleep(2 * attempt)
                        continue
                    logger.error("图片生成最终失败: %s", last_error)
                    return None

                image_base64 = result.get("data", {}).get("binary_data_base64", [None])[0]
                if not image_base64:
                    logger.error("图片生成失败：响应中无图片数据")
                    return None

                save_path.parent.mkdir(parents=True, exist_ok=True)
                with open(save_path, "wb") as f:
                    f.write(base64.b64decode(image_base64))
                elapsed = time.monotonic() - t0
                logger.info("图片生成成功 %s（%.1fs）", save_path.name, elapsed)
                return save_path
            except Exception as exc:
                last_error = str(exc)
                if attempt <= self.MAX_RETRIES:
                    logger.warning("图片生成第 %d 次异常（重试中）：%s", attempt, last_error)
                    time.sleep(2 * attempt)
                    continue
                logger.error("图片生成异常: %s", exc)
                return None
        return None
