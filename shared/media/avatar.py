"""
数字人视频生成模块
—— 使用 fal.ai AI Avatar API
流程：口播稿 → TTS 音频 → 上传到 fal → 数字人视频 → 下载 → 烧录字幕

支持两种模式：
  1. 音频驱动（audio mode）：本地 edge-tts 生成音频 → fal.ai 生成数字人
  2. 文本驱动（text mode）：直接传文本给 fal.ai，由 fal 内置 TTS + 数字人
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Dict, Optional

import requests

from shared.utils.logger import get_logger

logger = get_logger("avatar")


class AvatarGenerator:
    """fal.ai 数字人视频生成器"""

    # fal.ai 端点
    ENDPOINT_AUDIO = "fal-ai/ai-avatar"              # 照片+音频 → 说话视频
    ENDPOINT_TEXT = "fal-ai/ai-avatar/single-text"    # 照片+文本 → 说话视频

    # 可用的英文 TTS 声音（fal.ai 内置，用于 text mode）
    VOICES = [
        "Aria", "Roger", "Sarah", "Laura", "Charlie", "George",
        "Callum", "River", "Liam", "Charlotte", "Alice", "Matilda",
        "Will", "Jessica", "Eric", "Chris", "Brian", "Daniel", "Lily", "Bill",
    ]

    def __init__(
        self,
        fal_key: str,
        avatar_image_url: str = "",
        avatar_prompt: str = "",
        resolution: str = "480p",
        num_frames: int = 129,
    ) -> None:
        self.fal_key = fal_key.strip()
        self.avatar_image_url = avatar_image_url
        self.avatar_prompt = avatar_prompt or (
            "A professional person talking naturally in front of camera, "
            "neutral background, well lit, business casual"
        )
        self.resolution = resolution
        # fal.ai AI Avatar num_frames 范围 81-129
        self.num_frames = min(max(num_frames, 81), 129)

    @property
    def available(self) -> bool:
        return bool(self.fal_key)

    # ── 上传本地文件到 fal.ai storage ──

    def upload_file(self, local_path: Path) -> str:
        """上传本地文件到 fal.ai CDN，返回可访问的 URL"""
        logger.info("上传文件到 fal.ai storage: %s", local_path.name)

        # 1. 获取上传预签名 URL
        initiate_url = "https://rest.alpha.fal.ai/storage/upload/initiate"
        headers = {
            "Authorization": f"Key {self.fal_key}",
            "Content-Type": "application/json",
        }
        content_type = "audio/mpeg" if local_path.suffix == ".mp3" else "application/octet-stream"
        if local_path.suffix == ".png":
            content_type = "image/png"
        elif local_path.suffix in (".jpg", ".jpeg"):
            content_type = "image/jpeg"

        init_resp = requests.post(
            initiate_url,
            headers=headers,
            json={
                "file_name": local_path.name,
                "content_type": content_type,
            },
            timeout=30,
        )
        init_resp.raise_for_status()
        init_data = init_resp.json()

        upload_url = init_data["upload_url"]
        file_url = init_data["file_url"]

        # 2. 上传文件到预签名 URL
        with open(local_path, "rb") as f:
            put_resp = requests.put(
                upload_url,
                data=f,
                headers={"Content-Type": content_type},
                timeout=120,
            )
            put_resp.raise_for_status()

        logger.info("文件上传成功: %s", file_url)
        return file_url

    # ── 自动解析图片路径 ──

    def _resolve_image_url(self, image_url: Optional[str] = None) -> str:
        """
        解析数字人形象照：
        - 如果是公网 URL → 直接返回
        - 如果是本地文件路径 → 上传到 fal.ai storage → 返回 URL
        - 如果为空 → 使用默认 avatar_image_url
        """
        img = image_url or self.avatar_image_url
        if not img:
            raise ValueError("未提供数字人形象照（avatar_image_url 或 --avatar-image）")

        # 判断是否为本地路径
        p = Path(img)
        if p.exists() and p.is_file():
            logger.info("检测到本地图片，自动上传: %s", p.name)
            return self.upload_file(p)

        # 否则当作 URL
        return img

    # ── 音频驱动模式 ──

    def generate_from_audio(
        self,
        audio_url: str,
        image_url: Optional[str] = None,
        prompt: Optional[str] = None,
    ) -> Optional[str]:
        """
        音频驱动数字人：照片 + 音频 → 说话视频

        Args:
            audio_url: 音频文件 URL（fal storage 或公网 URL）
            image_url: 数字人形象照 URL（None 则用默认）
            prompt: 场景描述

        Returns:
            生成的视频 URL 或 None
        """
        import fal_client

        os.environ["FAL_KEY"] = self.fal_key
        img = self._resolve_image_url(image_url)

        payload = {
            "image_url": img,
            "audio_url": audio_url,
            "prompt": prompt or self.avatar_prompt,
            "resolution": self.resolution,
            "num_frames": self.num_frames,
        }

        logger.info("调用 fal.ai 数字人（音频模式, %s, %d frames）...",
                     self.resolution, self.num_frames)
        t0 = time.monotonic()

        try:
            result = fal_client.subscribe(
                self.ENDPOINT_AUDIO,
                arguments=payload,
                with_logs=True,
                on_queue_update=lambda update: self._on_queue_update(update),
            )
        except Exception as exc:
            logger.error("fal.ai 数字人生成失败: %s", exc)
            return None

        elapsed = time.monotonic() - t0
        video_url = result.get("video", {}).get("url")
        if video_url:
            logger.info("数字人视频生成成功（%.1fs）: %s", elapsed, video_url[:80])
        else:
            logger.error("fal.ai 返回无视频 URL: %s", result)
        return video_url

    # ── 文本驱动模式 ──

    def generate_from_text(
        self,
        text: str,
        voice: str = "Lily",
        image_url: Optional[str] = None,
        prompt: Optional[str] = None,
    ) -> Optional[str]:
        """
        文本驱动数字人：照片 + 文本 → TTS + 说话视频

        Args:
            text: 口播文本
            voice: fal.ai 内置声音名（英文声音，中文建议用 audio 模式）
            image_url: 数字人形象照 URL
            prompt: 场景描述

        Returns:
            生成的视频 URL 或 None
        """
        import fal_client

        os.environ["FAL_KEY"] = self.fal_key
        img = self._resolve_image_url(image_url)

        payload = {
            "image_url": img,
            "text_input": text,
            "voice": voice,
            "prompt": prompt or self.avatar_prompt,
            "resolution": self.resolution,
            "num_frames": self.num_frames,
        }

        logger.info("调用 fal.ai 数字人（文本模式, voice=%s, %s）...", voice, self.resolution)
        t0 = time.monotonic()

        try:
            result = fal_client.subscribe(
                self.ENDPOINT_TEXT,
                arguments=payload,
                with_logs=True,
                on_queue_update=lambda update: self._on_queue_update(update),
            )
        except Exception as exc:
            logger.error("fal.ai 数字人生成失败: %s", exc)
            return None

        elapsed = time.monotonic() - t0
        video_url = result.get("video", {}).get("url")
        if video_url:
            logger.info("数字人视频生成成功（%.1fs）: %s", elapsed, video_url[:80])
        else:
            logger.error("fal.ai 返回无视频 URL: %s", result)
        return video_url

    # ── 完整流程：文章 → 数字人视频（带字幕） ──

    def generate_from_article(
        self,
        title: str,
        body: str,
        save_dir: Path,
        llm=None,
        image_url: Optional[str] = None,
        voice: str = "zh-CN-XiaoyiNeural",
        rate: str = "+0%",
        with_subtitle: bool = True,
        custom_script: Optional[str] = None,
    ) -> Optional[Path]:
        """
        完整流程：文章 → 口播稿 → TTS音频+字幕 → 上传音频 → fal数字人 → 下载 → 烧录字幕

        Args:
            title: 文章标题
            body: 文章正文
            save_dir: 输出目录
            llm: LLM 客户端（用于生成口播稿）
            image_url: 数字人形象照 URL
            voice: edge-tts 中文语音
            rate: 语速
            with_subtitle: 是否烧录字幕
            custom_script: 自定义口播稿

        Returns:
            最终视频路径 或 None
        """
        from shared.media.tts import TTSGenerator

        save_dir = Path(save_dir)
        save_dir.mkdir(parents=True, exist_ok=True)

        # 1. 生成口播稿
        if custom_script:
            script = custom_script
            logger.info("使用自定义口播稿")
        elif llm and llm.available:
            tts_gen = TTSGenerator(llm=llm, voice=voice, rate=rate)
            script = tts_gen.generate_script(title, body)
        else:
            # 简单截取文章摘要作为口播稿
            script = body[:300]
            logger.warning("无 LLM 可用，截取文章前300字作为口播稿")

        logger.info("口播稿（%d字）: %s...", len(script), script[:60])

        # 2. TTS 合成音频 + 字幕
        tts_gen = TTSGenerator(llm=llm, voice=voice, rate=rate)
        audio_path = save_dir / "avatar_audio.mp3"
        sub_path = (save_dir / "avatar_sub.ass") if with_subtitle else None
        audio_path, sub_file = tts_gen.synthesize(script, audio_path, sub_path)

        # 3. 上传音频到 fal.ai storage
        audio_url = self.upload_file(audio_path)

        # 4. 调用 fal.ai 生成数字人视频
        video_url = self.generate_from_audio(
            audio_url=audio_url,
            image_url=image_url,
        )
        if not video_url:
            return None

        # 5. 下载数字人视频
        raw_video_path = save_dir / "avatar_raw.mp4"
        self._download(video_url, raw_video_path)

        # 6. 烧录字幕（如果有）
        if with_subtitle and sub_file and sub_file.exists():
            final_path = save_dir / "avatar_final.mp4"
            TTSGenerator.merge(
                video_path=raw_video_path,
                audio_path=audio_path,  # 使用本地高质量音频替换
                output_path=final_path,
                subtitle_path=sub_file,
            )
            logger.info("数字人视频（含字幕）: %s", final_path)
            return final_path
        else:
            logger.info("数字人视频（无字幕）: %s", raw_video_path)
            return raw_video_path

    # ── 内部方法 ──

    @staticmethod
    def _download(url: str, save_path: Path) -> Path:
        """下载视频到本地"""
        logger.info("下载数字人视频...")
        resp = requests.get(url, timeout=120, stream=True)
        resp.raise_for_status()
        save_path.parent.mkdir(parents=True, exist_ok=True)
        with open(save_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
        size_mb = save_path.stat().st_size / 1024 / 1024
        logger.info("视频已下载: %s (%.1fMB)", save_path.name, size_mb)
        return save_path

    @staticmethod
    def _on_queue_update(update) -> None:
        """fal.ai 队列状态回调"""
        status = getattr(update, "status", str(update))
        if status == "IN_PROGRESS":
            logs = getattr(update, "logs", [])
            for log in logs:
                msg = getattr(log, "message", str(log))
                logger.info("  fal.ai: %s", msg)
        elif status == "IN_QUEUE":
            pos = getattr(update, "queue_position", "?")
            logger.info("  fal.ai 排队中（位置: %s）...", pos)
