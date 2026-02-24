"""
视频生成模块
使用火山引擎（即梦AI）文生视频 API + DeepSeek 生成视频 prompt
流程：文章内容 -> DeepSeek 生成视频描述 -> 火山引擎生成视频 -> 下载到本地
"""

import json
import time
import requests
from pathlib import Path
from typing import Optional

from volcengine.Credentials import Credentials
from volcengine.auth.SignerV4 import SignerV4
from volcengine.base.Request import Request
from openai import OpenAI

from config import settings
from utils.logger import get_logger
from utils.retry import retry

logger = get_logger("video_gen")

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"

# ────────── 视频 prompt 生成 ──────────

VIDEO_PROMPT_SYSTEM = """You are a professional short video creative director.
Based on the article content provided by the user, generate an English prompt for AI text-to-video generation.

## Rules
- Output ONLY in English, no Chinese characters at all
- Maximum 150 words
- Describe ONE coherent visual scene for a 5-10 second video
- Include: subject/action, environment, lighting, camera movement, visual style
- Style: modern tech, business, sleek, futuristic
- AVOID: any text/numbers/words on screen, human faces, specific data, financial figures
- AVOID: any content that could be flagged as sensitive (politics, violence, etc.)
- Focus on abstract visuals: glowing interfaces, flowing data streams, connected nodes, modern cityscapes
- End with "cinematic, 4K, high quality, smooth motion"

## Output
Output ONLY the prompt text. No explanation, no prefix, no quotes."""


def generate_video_prompt(title: str, body: str) -> str:
    """用 DeepSeek 将文案转为视频 prompt"""
    client = OpenAI(
        api_key=settings.llm.API_KEY,
        base_url=settings.llm.BASE_URL,
    )

    user_msg = f"文案标题：{title}\n\n文案正文（前500字）：\n{body[:500]}"

    logger.info("调用 DeepSeek 生成视频 prompt...")
    response = client.chat.completions.create(
        model=settings.llm.MODEL,
        messages=[
            {"role": "system", "content": VIDEO_PROMPT_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.8,
    )
    prompt = response.choices[0].message.content.strip()
    logger.info("视频 prompt: %s", prompt[:100])
    return prompt


# ────────── 火山引擎 API ──────────

def _build_credentials():
    """创建火山引擎认证对象"""
    cfg = settings.volc
    return Credentials(cfg.AK, cfg.SK, cfg.SERVICE, cfg.REGION)


def _sign_and_post(action: str, body: dict) -> dict:
    """签名并发送请求到火山引擎"""
    cfg = settings.volc
    creds = _build_credentials()

    request = Request()
    request.set_shema("https")
    request.set_method("POST")
    request.set_host(cfg.HOST)
    request.set_path("/")
    request.set_query({"Action": action, "Version": "2022-08-31"})
    request.set_headers({"Content-Type": "application/json"})
    request.set_body(json.dumps(body))

    SignerV4.sign(request, creds)

    url = f"https://{cfg.HOST}/?Action={action}&Version=2022-08-31"
    resp = requests.post(url, headers=request.headers, data=request.body, timeout=30)
    return resp.json()


@retry(max_retries=1, delay=3.0, exceptions=(requests.RequestException,))
def submit_video_task(prompt: str, aspect_ratio: str = "9:16") -> str:
    """
    提交文生视频任务

    Args:
        prompt: 视频描述（英文）
        aspect_ratio: 画面比例，小红书竖屏用 9:16

    Returns:
        task_id
    """
    body = {
        "req_key": "jimeng_ti2v_v30_pro",
        "prompt": prompt,
        "seed": -1,
        "frames": 121,
        "aspect_ratio": aspect_ratio,
    }

    logger.info("提交视频生成任务 (比例=%s)...", aspect_ratio)
    result = _sign_and_post("CVSync2AsyncSubmitTask", body)
    logger.debug("提交返回: %s", result)

    if result.get("code") != 10000:
        msg = result.get("message", "未知错误")
        raise RuntimeError(f"提交视频任务失败: {msg}")

    task_id = result["data"]["task_id"]
    logger.info("任务已提交, task_id=%s", task_id)
    return task_id


def poll_video_result(task_id: str, max_wait: int = 300, interval: int = 10) -> Optional[str]:
    """
    轮询视频生成结果

    Args:
        task_id: 任务 ID
        max_wait: 最大等待秒数（默认 5 分钟）
        interval: 轮询间隔秒数

    Returns:
        视频 URL 或 None
    """
    body = {
        "req_key": "jimeng_ti2v_v30_pro",
        "task_id": task_id,
    }

    start = time.time()
    logger.info("开始轮询视频结果 (最长 %ds)...", max_wait)

    while time.time() - start < max_wait:
        result = _sign_and_post("CVSync2AsyncGetResult", body)

        if result.get("code") != 10000:
            logger.error("查询异常: %s", result.get("message"))
            return None

        status = result["data"].get("status", "")
        elapsed = int(time.time() - start)

        if status == "done":
            # 视频可能在 resp_data 或 video_url
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
            logger.error("任务未找到或已过期")
            return None

        else:
            logger.warning("未知状态: %s", status)
            time.sleep(interval)

    logger.error("视频生成超时 (%ds)", max_wait)
    return None


def download_video(url: str, save_path: Path) -> Path:
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


# ────────── 完整流程 ──────────

def generate_video_from_article(
    title: str,
    body: str,
    post_id: int,
    aspect_ratio: str = "9:16",
    custom_prompt: Optional[str] = None,
    with_audio: bool = True,
    voice: str = "zh-CN-XiaoyiNeural",
    rate: str = "+0%",
    custom_script: Optional[str] = None,
) -> Optional[Path]:
    """
    完整流程：文章 -> 视频 prompt -> 提交任务 -> 轮询 -> 下载 -> 配音

    Args:
        title: 文案标题
        body: 文案正文
        post_id: 文章 ID（用于文件命名）
        aspect_ratio: 画面比例
        custom_prompt: 自定义视频 prompt（跳过 DeepSeek 生成）
        with_audio: 是否自动配音（默认 True）
        voice: TTS 语音名称
        rate: TTS 语速调整
        custom_script: 自定义口播稿（跳过 AI 生成）

    Returns:
        最终视频文件路径 或 None
    """
    # 1. 生成视频 prompt
    if custom_prompt:
        prompt = custom_prompt
        logger.info("使用自定义 prompt")
    else:
        prompt = generate_video_prompt(title, body)

    print(f"\n视频 Prompt:\n{prompt}\n")

    # 2. 提交任务
    task_id = submit_video_task(prompt, aspect_ratio=aspect_ratio)

    # 3. 轮询结果
    video_url = poll_video_result(task_id)
    if not video_url:
        logger.error("视频生成失败")
        return None

    print(f"视频 URL: {video_url}")

    # 4. 下载视频
    OUTPUT_DIR.mkdir(exist_ok=True)
    raw_path = OUTPUT_DIR / f"xhs_video_{post_id}.mp4"
    download_video(video_url, raw_path)

    # 5. 更新 JSON 文件（添加 video_path）
    json_path = OUTPUT_DIR / f"xhs_post_{post_id}.json"
    if json_path.exists():
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["video_url"] = video_url
        data["video_path"] = str(raw_path)
        data["video_prompt"] = prompt
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info("已更新 JSON: %s", json_path)

    # 6. 配音（可选）
    if with_audio:
        from video.tts import add_voiceover

        logger.info("开始为视频配音...")
        final_path = add_voiceover(
            video_path=raw_path,
            title=title,
            body=body,
            post_id=post_id,
            voice=voice,
            rate=rate,
            custom_script=custom_script,
        )
        if final_path and final_path.exists():
            return final_path
        else:
            logger.warning("配音失败，返回无声视频")
            return raw_path
    else:
        return raw_path
