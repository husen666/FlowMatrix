"""
DeepSeek / LLM 统一客户端
—— Session 复用 + JSON 提取 + 自动重试
被 article.py / xhs.py / video.py 共用
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional

import requests

from shared.utils.logger import get_logger

logger = get_logger("llm-client")


class LLMClient:
    """DeepSeek / OpenAI 兼容的聊天补全客户端"""

    MAX_RETRIES = 2

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-chat",
        timeout: int = 40,
    ) -> None:
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._session = requests.Session()
        if self.api_key:
            self._session.headers.update({
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            })

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def close(self) -> None:
        """释放 HTTP 连接池"""
        self._session.close()

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ── 核心调用 ──

    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.7,
        json_mode: bool = False,
    ) -> Optional[str]:
        """
        调用聊天补全 API，返回 assistant 回复的文本。
        失败时返回 None（已自动重试）。
        """
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        api_url = f"{self.base_url}/chat/completions"
        # 连接超时 15s，读取超时 150s（LLM 生成长文本需要较长时间）
        api_timeout = (15, max(self.timeout * 3, 150))

        last_error = ""
        backoff = 2.0
        for attempt in range(1, self.MAX_RETRIES + 2):
            t0 = time.monotonic()
            try:
                resp = self._session.post(api_url, json=payload, timeout=api_timeout)
                elapsed = time.monotonic() - t0
                if resp.status_code == 200:
                    data = resp.json()
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    if content:
                        logger.info("LLM 调用成功（耗时 %.1fs）", elapsed)
                        return content
                    last_error = "响应内容为空"
                elif 400 <= resp.status_code < 500:
                    logger.error("LLM 客户端错误 status=%d body=%s", resp.status_code, resp.text[:200])
                    return None  # 客户端错误不重试
                else:
                    last_error = f"status={resp.status_code}, body={resp.text[:200]}"
            except requests.exceptions.ConnectTimeout:
                last_error = f"连接超时 ({api_timeout[0]}s)"
            except requests.exceptions.ReadTimeout:
                elapsed = time.monotonic() - t0
                last_error = f"读取超时（已等待 {elapsed:.0f}s，上限 {api_timeout[1]}s）"
            except requests.exceptions.ConnectionError as exc:
                last_error = f"连接异常: {exc}"
            except Exception as exc:
                last_error = str(exc)

            if attempt <= self.MAX_RETRIES:
                logger.warning(
                    "LLM 第 %d/%d 次失败（%.1fs 后重试）：%s",
                    attempt, self.MAX_RETRIES + 1, backoff, last_error,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)

        logger.error("LLM 调用失败，已达最大重试次数: %s", last_error)
        return None

    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.7,
    ) -> Optional[Dict[str, Any]]:
        """
        调用聊天补全 API，自动提取并解析 JSON 返回。
        失败时返回 None。
        """
        raw = self.chat(system_prompt, user_prompt, temperature, json_mode=True)
        if not raw:
            return None
        parsed = extract_json_block(raw)
        if parsed is None:
            logger.warning("LLM 返回内容无法解析为 JSON")
        return parsed


# ── JSON 提取工具函数 ──

def extract_json_block(text: str) -> Optional[Dict[str, Any]]:
    """从 LLM 输出中提取 JSON 对象（兼容 markdown 代码块、前后多余文字等）"""
    if not text:
        return None

    # 尝试 1：提取 ```json ... ``` 代码块
    fenced = re.search(r"```json\s*(\{.*?\})\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    candidate = fenced.group(1) if fenced else None

    # 尝试 2：直接查找 { ... }
    if not candidate:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = text[start : end + 1]

    if not candidate:
        return None

    try:
        parsed = json.loads(candidate)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None
