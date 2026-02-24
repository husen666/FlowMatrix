"""
视频生成模块
—— 使用火山引擎（即梦AI）文生视频 API + DeepSeek 生成视频 prompt
流程：文章内容 -> DeepSeek 生成视频描述 -> 火山引擎生成视频 -> 下载到本地
从 xiaohongshu/video/generator.py 迁移
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

import requests
from volcengine.Credentials import Credentials
from volcengine.auth.SignerV4 import SignerV4
from volcengine.base.Request import Request

from shared.llm.client import LLMClient
from shared.utils.logger import get_logger
from shared.utils.retry import retry

logger = get_logger("video-gen")


# ────────── 视频 prompt 系统提示词 ──────────

_VIDEO_PROMPT_SYSTEM = """You are a professional short video creative director specializing in AI text-to-video generation.
Based on the article content provided by the user, generate a precise English prompt for AI video generation.

## CRITICAL RULES (must follow strictly)
- Output ONLY in English. ABSOLUTELY NO Chinese characters, Japanese characters, or any CJK characters
- Maximum 120 words, every word must add visual value
- Describe ONE coherent visual scene for a 5-10 second seamless loop video

## CONTENT RULES
- Focus on ABSTRACT, NON-HUMAN visuals: geometric shapes, data streams, glowing nodes, tech interfaces
- NEVER include: any text, words, letters, numbers, symbols, watermarks, logos on screen
- NEVER include: human faces, human bodies, hands, recognizable people or characters
- NEVER include: specific brand logos, product screenshots, UI mockups with text
- NEVER include: any content that could be flagged as sensitive (politics, violence, religion)

## VISUAL DESCRIPTION STRUCTURE
1. Main subject: abstract geometric or technological visual element
2. Environment: clean, minimal background with depth
3. Motion: smooth camera movement (slow dolly, gentle orbit, or subtle zoom)
4. Lighting: volumetric, rim lighting, or ambient glow
5. Atmosphere: particles, bokeh, light rays, or subtle fog

## STYLE
- Modern tech aesthetic: glassmorphism, holographic, neon accents on dark backgrounds
- Consistent color palette: deep blue/purple base with cyan/teal/orange accents
- End the prompt with: "cinematic quality, 4K resolution, smooth 60fps motion, professional color grading"

## OUTPUT
Output ONLY the prompt text. No explanation, no prefix, no quotes, no markdown."""


# ────────── 火山引擎签名请求 ──────────

def _sign_and_post(
    action: str,
    body: dict,
    volc_ak: str,
    volc_sk: str,
    host: str = "visual.volcengineapi.com",
    service: str = "cv",
    region: str = "cn-north-1",
) -> dict:
    """签名并发送请求到火山引擎"""
    creds = Credentials(volc_ak, volc_sk, service, region)

    request = Request()
    request.set_shema("https")
    request.set_method("POST")
    request.set_host(host)
    request.set_path("/")
    request.set_query({"Action": action, "Version": "2022-08-31"})
    request.set_headers({"Content-Type": "application/json"})
    request.set_body(json.dumps(body))

    SignerV4.sign(request, creds)

    url = f"https://{host}/?Action={action}&Version=2022-08-31"
    resp = requests.post(url, headers=request.headers, data=request.body, timeout=30)
    return resp.json()


# ────────── 公共 API ──────────

class VideoGenerator:
    """视频生成器，封装完整流程"""

    def __init__(
        self,
        volc_ak: str,
        volc_sk: str,
        llm: Optional[LLMClient] = None,
        volc_host: str = "visual.volcengineapi.com",
        volc_service: str = "cv",
        volc_region: str = "cn-north-1",
    ) -> None:
        self.volc_ak = volc_ak
        self.volc_sk = volc_sk
        self.llm = llm
        self.volc_host = volc_host
        self.volc_service = volc_service
        self.volc_region = volc_region

    def generate_video_prompt(self, title: str, body: str) -> str:
        """用 LLM 将文案转为视频 prompt（确保纯英文输出）"""
        if not self.llm or not self.llm.available:
            raise RuntimeError("LLM 客户端不可用，无法生成视频 prompt")
        user_msg = f"文案标题：{title}\n\n文案正文（前500字）：\n{body[:500]}"
        logger.info("调用 LLM 生成视频 prompt...")
        raw = self.llm.chat(
            system_prompt=_VIDEO_PROMPT_SYSTEM,
            user_prompt=user_msg,
            temperature=0.7,
        )
        if not raw:
            raise RuntimeError("LLM 生成视频 prompt 失败")
        prompt = raw.strip()
        # 安全检查：移除任何非 ASCII 字符（防止中文/CJK 字符泄漏到 prompt）
        prompt = self._sanitize_prompt(prompt)
        logger.info("视频 prompt: %s", prompt[:120])
        return prompt

    @staticmethod
    def _sanitize_prompt(prompt: str) -> str:
        """清理 prompt，确保纯英文输出，移除所有中文/CJK字符"""
        import re
        # 移除所有 CJK 统一表意文字及相关符号
        cleaned = re.sub(r'[\u4e00-\u9fff\u3400-\u4dbf\u3000-\u303f\uff00-\uffef]', '', prompt)
        # 移除引号包裹（LLM 可能包裹在引号中）
        cleaned = cleaned.strip('"\'`')
        # 压缩多余空格
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()
        if cleaned != prompt.strip():
            logger.warning("视频 prompt 包含非英文字符，已自动清理")
        return cleaned

    @retry(max_retries=1, delay=3.0, exceptions=(requests.RequestException,))
    def submit_task(self, prompt: str, aspect_ratio: str = "9:16", frames: int = 121) -> str:
        """提交文生视频任务，返回 task_id

        Args:
            prompt: 视频描述
            aspect_ratio: 画面比例
            frames: 帧数 (121≈5s, 241≈10s)
        """
        body = {
            "req_key": "jimeng_ti2v_v30_pro",
            "prompt": prompt,
            "seed": -1,
            "frames": frames,
            "aspect_ratio": aspect_ratio,
        }
        logger.info("提交视频生成任务 (比例=%s, 帧数=%d)...", aspect_ratio, frames)
        result = _sign_and_post(
            "CVSync2AsyncSubmitTask", body,
            self.volc_ak, self.volc_sk,
            self.volc_host, self.volc_service, self.volc_region,
        )
        logger.debug("提交返回: %s", result)
        if result.get("code") != 10000:
            msg = result.get("message", "未知错误")
            raise RuntimeError(f"提交视频任务失败: {msg}")
        task_id = result["data"]["task_id"]
        logger.info("任务已提交, task_id=%s", task_id)
        return task_id

    def poll_result(self, task_id: str, max_wait: int = 300, interval: int = 10) -> Optional[str]:
        """轮询视频生成结果，返回视频 URL 或 None"""
        body = {
            "req_key": "jimeng_ti2v_v30_pro",
            "task_id": task_id,
        }
        start = time.time()
        logger.info("开始轮询视频结果 (最长 %ds)...", max_wait)

        while time.time() - start < max_wait:
            result = _sign_and_post(
                "CVSync2AsyncGetResult", body,
                self.volc_ak, self.volc_sk,
                self.volc_host, self.volc_service, self.volc_region,
            )

            if result.get("code") != 10000:
                logger.error("查询异常: %s", result.get("message"))
                return None

            status = result["data"].get("status", "")
            elapsed = int(time.time() - start)

            if status == "done":
                data = result["data"]
                video_url = data.get("video_url")
                if not video_url:
                    resp_data = data.get("resp_data", [])
                    if resp_data and isinstance(resp_data, list):
                        video_url = resp_data[0].get("video_url")
                logger.info("视频生成完成! (%ds)", elapsed)
                return video_url
            elif status in ("in_queue", "generating"):
                logger.info("生成中... (%ds/%ds)", elapsed, max_wait)
                time.sleep(interval)
            elif status == "not_found":
                logger.info("任务未找到或已过期")
                return None
            else:
                logger.warning("未知状态: %s", status)
                time.sleep(interval)

        logger.error("视频生成超时 (%ds)", max_wait)
        return None

    @staticmethod
    def download(url: str, save_path: Path) -> Path:
        """下载视频到本地"""
        logger.info("下载视频: %s", url[:80])
        resp = requests.get(url, timeout=120, stream=True)
        resp.raise_for_status()
        save_path.parent.mkdir(parents=True, exist_ok=True)
        with open(save_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        size_mb = save_path.stat().st_size / 1024 / 1024
        logger.info("视频已保存: %s (%.1fMB)", save_path, size_mb)
        return save_path

    def generate_from_article(
        self,
        title: str,
        body: str,
        save_dir: Path,
        filename: str = "video.mp4",
        aspect_ratio: str = "9:16",
        custom_prompt: Optional[str] = None,
    ) -> Optional[Path]:
        """完整流程：文章 -> 视频 prompt -> 提交任务 -> 轮询 -> 下载"""
        # 1. 生成视频 prompt
        if custom_prompt:
            prompt = custom_prompt
            logger.info("使用自定义 prompt")
        else:
            prompt = self.generate_video_prompt(title, body)
        logger.info("视频 Prompt:\n%s", prompt)

        # 2. 提交任务
        task_id = self.submit_task(prompt, aspect_ratio=aspect_ratio)

        # 3. 轮询结果
        video_url = self.poll_result(task_id)
        if not video_url:
            logger.error("视频生成失败")
            return None
        logger.info("视频 URL: %s", video_url)

        # 4. 下载视频
        save_path = save_dir / filename
        self.download(video_url, save_path)
        return save_path
