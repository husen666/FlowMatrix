"""
视频配音 + 字幕模块
流程：文案 -> LLM 生成口播稿 -> edge-tts 合成语音+字幕 -> ffmpeg 合并到视频

依赖：
    pip install edge-tts imageio-ffmpeg
"""

from __future__ import annotations

import asyncio
import re
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import List, Optional, Tuple

import imageio_ffmpeg

from shared.llm.client import LLMClient
from shared.utils.logger import get_logger

logger = get_logger("tts")

# ffmpeg 可执行文件路径（由 imageio-ffmpeg 提供）
FFMPEG_EXE = imageio_ffmpeg.get_ffmpeg_exe()

# edge-tts 中文语音（自然女声，适合小红书风格）
DEFAULT_VOICE = "zh-CN-XiaoyiNeural"
# 备选：zh-CN-YunxiNeural（男声）、zh-CN-XiaoxiaoNeural（女声）

# 字幕每行最大字符数
_SUB_MAX_CHARS_PORTRAIT = 14   # 竖屏 9:16 适合短句
_SUB_MAX_CHARS_LANDSCAPE = 22  # 横屏 16:9 有更多水平空间
_SUB_MAX_CHARS = _SUB_MAX_CHARS_PORTRAIT  # 兼容别名，用于 TTS 时间边界拆分


# ────────── 口播稿生成 ──────────

_VOICEOVER_SYSTEM = """你是一位专业的短视频口播文案写手，擅长将文章内容改编为适合小红书短视频旁白的口播稿。

## 规则
- 输出纯中文口播稿，不要任何标记、标题、序号
- 控制在 100-150 字，朗读时长约 5-8 秒（匹配短视频时长）
- 语气亲切自然，像朋友聊天，适合小红书年轻用户
- 开头要有吸引力（提问/惊叹/反转），中间讲核心价值，结尾引导互动
- 不要出现"大家好""点赞关注"等过于套路的开场
- 不要出现具体数字、网址、品牌名等
- 适合女声朗读的节奏，句子简短有力
- 不要加标点以外的任何符号（不要加 emoji）

## 输出
只输出口播稿正文，不要任何解释或前缀。"""


# ────────── SRT 工具函数 ──────────

def _td_to_srt(td: timedelta) -> str:
    """timedelta → SRT 时间格式 HH:MM:SS,mmm"""
    total_sec = td.total_seconds()
    h = int(total_sec // 3600)
    m = int((total_sec % 3600) // 60)
    s = total_sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def _group_words(
    words: List[Tuple[timedelta, timedelta, str]],
    max_chars: int = _SUB_MAX_CHARS_PORTRAIT,
) -> List[Tuple[timedelta, timedelta, str]]:
    """
    将逐字/逐词时间戳分组为每行 max_chars 的字幕段

    Args:
        words: [(start, end, text), ...]
        max_chars: 每行最大字符数

    Returns:
        分组后的 [(start, end, text), ...]
    """
    if not words:
        return []

    groups: List[Tuple[timedelta, timedelta, str]] = []
    buf_text = ""
    buf_start = words[0][0]
    buf_end = words[0][1]

    for start, end, text in words:
        # 遇到标点或达到最大长度时切分
        if len(buf_text) + len(text) > max_chars and buf_text:
            groups.append((buf_start, buf_end, buf_text))
            buf_text = text
            buf_start = start
            buf_end = end
        else:
            buf_text += text
            buf_end = end

        # 遇到句号、问号等自然断句标点时也切分
        if buf_text and buf_text[-1] in "。！？；，、…":
            groups.append((buf_start, buf_end, buf_text))
            buf_text = ""
            buf_start = end

    # 处理剩余
    if buf_text:
        groups.append((buf_start, buf_end, buf_text))

    return groups


def _build_srt(groups: List[Tuple[timedelta, timedelta, str]]) -> str:
    """将分组字幕转为 SRT 格式"""
    lines = []
    for i, (start, end, text) in enumerate(groups, 1):
        lines.append(str(i))
        lines.append(f"{_td_to_srt(start)} --> {_td_to_srt(end)}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


def _get_chinese_font() -> str:
    """检测系统可用的中文字体，返回最佳选择

    优先级：
    1. 思源黑体（跨平台，开源）
    2. 微软雅黑（Windows）
    3. PingFang SC（macOS）
    4. Noto Sans CJK SC（Linux）
    5. 回退到 sans-serif
    """
    import platform
    import subprocess

    os_name = platform.system()

    # 优先尝试的字体列表（按优先级）
    font_candidates = []
    if os_name == "Windows":
        font_candidates = [
            "Microsoft YaHei",     # 微软雅黑
            "SimHei",              # 黑体
            "Source Han Sans SC",  # 思源黑体
            "Noto Sans CJK SC",   # Noto 中文
            "DengXian",            # 等线
        ]
    elif os_name == "Darwin":
        font_candidates = [
            "PingFang SC",
            "Hiragino Sans GB",
            "Source Han Sans SC",
            "Noto Sans CJK SC",
            "STHeiti",
        ]
    else:  # Linux
        font_candidates = [
            "Noto Sans CJK SC",
            "Source Han Sans SC",
            "WenQuanYi Zen Hei",
            "WenQuanYi Micro Hei",
            "Droid Sans Fallback",
        ]

    # 使用 fc-list 检测可用字体（跨平台工具）
    try:
        result = subprocess.run(
            ["fc-list", ":lang=zh", "family"],
            capture_output=True, text=True, timeout=5,
        )
        available_fonts = result.stdout
        for font in font_candidates:
            if font.lower() in available_fonts.lower():
                logger.info("检测到中文字体: %s", font)
                return font
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # Windows 直接检查注册表中的字体目录
    if os_name == "Windows":
        import os
        fonts_dir = os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts")
        font_file_map = {
            "Microsoft YaHei": "msyh.ttc",
            "SimHei": "simhei.ttf",
            "DengXian": "Deng.ttf",
        }
        for font, filename in font_file_map.items():
            if os.path.exists(os.path.join(fonts_dir, filename)):
                logger.info("检测到中文字体(文件): %s", font)
                return font

    # 默认回退
    default = font_candidates[0] if font_candidates else "Microsoft YaHei"
    logger.warning("未检测到已安装的中文字体，使用默认: %s", default)
    return default


def _build_ass(groups: List[Tuple[timedelta, timedelta, str]], width: int = 1080, height: int = 1920) -> str:
    """
    将分组字幕转为 ASS 格式（支持精细样式控制）
    白字黑描边，居中偏下，清晰中文渲染。
    自动适配横屏（16:9）和竖屏（9:16）分辨率。

    改进：
    - 自动检测系统中文字体，避免字体缺失导致乱码
    - 增大字体描边和阴影，提升可读性
    - 设置正确的编码（1=中文简体 GBK 编码标识，但 ASS 文件本身用 UTF-8）
    """
    font_name = _get_chinese_font()

    # 字体大小根据分辨率自适应（竖屏 1080 宽度下约 80px，短视频标准大字幕）
    font_size = max(48, int(width * 0.074))
    margin_bottom = int(height * 0.10)  # 底部留白约 10%
    outline = max(3, int(font_size * 0.06))  # 描边跟随字号

    header = f"""[Script Info]
Title: Video Subtitles
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Default,{font_name},{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,2,0,1,{outline},2,2,30,30,{margin_bottom},134

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
"""
    events = []
    for start, end, text in groups:
        s = _td_to_ass(start)
        e = _td_to_ass(end)
        events.append(f"Dialogue: 0,{s},{e},Default,,0,0,0,,{text}")

    return header + "\n".join(events) + "\n"


def _td_to_ass(td: timedelta) -> str:
    """timedelta → ASS 时间格式 H:MM:SS.cc"""
    total = td.total_seconds()
    h = int(total // 3600)
    m = int((total % 3600) // 60)
    s = total % 60
    return f"{h}:{m:02d}:{s:05.2f}"


# ────────── 主类 ──────────

class TTSGenerator:
    """视频配音 + 字幕生成器"""

    def __init__(
        self,
        llm: Optional[LLMClient] = None,
        voice: str = DEFAULT_VOICE,
        rate: str = "+0%",
    ) -> None:
        self.llm = llm
        self.voice = voice
        self.rate = rate

    # ── 口播稿 ──

    def generate_script(self, title: str, body: str) -> str:
        """用 LLM 将文案转为口播稿"""
        if not self.llm or not self.llm.available:
            raise RuntimeError("LLM 客户端不可用，无法生成口播稿")
        user_msg = f"文案标题：{title}\n\n文案正文（前800字）：\n{body[:800]}"
        logger.info("调用 LLM 生成口播稿...")
        raw = self.llm.chat(
            system_prompt=_VOICEOVER_SYSTEM,
            user_prompt=user_msg,
            temperature=0.7,
        )
        if not raw:
            raise RuntimeError("LLM 生成口播稿失败")
        script = raw.strip()
        logger.info("口播稿 (%d字): %s...", len(script), script[:60])
        return script

    # ── TTS 合成（音频 + 字幕） ──

    def synthesize(
        self,
        text: str,
        audio_path: Path,
        subtitle_path: Optional[Path] = None,
        video_width: int = 1080,
        video_height: int = 1920,
    ) -> Tuple[Path, Optional[Path]]:
        """
        将文本转为语音文件，同时生成字幕文件

        Args:
            text: 口播稿文本
            audio_path: 输出音频路径（.mp3）
            subtitle_path: 输出字幕路径（.ass），None 则不生成
            video_width: 视频宽度，用于字幕排版（默认 1080 竖屏）
            video_height: 视频高度，用于字幕排版（默认 1920 竖屏）

        Returns:
            (音频路径, 字幕路径 或 None)
        """
        audio_path = Path(audio_path)
        audio_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info("edge-tts 合成语音+字幕 (voice=%s, rate=%s)...", self.voice, self.rate)
        word_boundaries = self._tts_with_fallback(text, str(audio_path))

        size_kb = audio_path.stat().st_size / 1024
        logger.info("语音已生成: %s (%.1fKB)", audio_path.name, size_kb)

        # 生成字幕文件
        sub_file = None
        if subtitle_path and word_boundaries:
            subtitle_path = Path(subtitle_path)
            # 根据视频方向选择每行字数：横屏有更多水平空间
            is_landscape = video_width > video_height
            max_chars = _SUB_MAX_CHARS_LANDSCAPE if is_landscape else _SUB_MAX_CHARS_PORTRAIT
            groups = _group_words(word_boundaries, max_chars=max_chars)
            ass_content = _build_ass(groups, width=video_width, height=video_height)
            subtitle_path.write_text(ass_content, encoding="utf-8")
            logger.info("字幕已生成: %s (%d 条, %dx%d, 每行%d字)",
                        subtitle_path.name, len(groups), video_width, video_height, max_chars)
            sub_file = subtitle_path

        return audio_path, sub_file

    def _tts_with_fallback(
        self, text: str, output_path: str
    ) -> List[Tuple[timedelta, timedelta, str]]:
        """
        先尝试 asyncio.run 调用 edge-tts Python API，
        如果挂起（Python 3.14 兼容性问题），回退到 subprocess 调用 CLI。
        """
        import concurrent.futures
        import sys

        def _run_async():
            return asyncio.run(self._tts_with_subs(text, output_path))

        # 尝试 asyncio 方式，设置超时防止挂起
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(_run_async)
                return future.result(timeout=60)
        except (concurrent.futures.TimeoutError, Exception) as e:
            logger.warning("asyncio 方式超时或失败 (%s)，回退到 CLI 方式", type(e).__name__)

        # 回退：通过 subprocess 调用 edge-tts CLI
        vtt_path = output_path.replace(".mp3", ".vtt")
        cmd = [
            sys.executable, "-m", "edge_tts",
            "--voice", self.voice,
            "--rate", self.rate,
            "--text", text,
            "--write-media", output_path,
            "--write-subtitles", vtt_path,
        ]
        logger.info("使用 CLI 方式合成: edge-tts subprocess")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f"edge-tts CLI 失败: {result.stderr[:300]}")

        # 解析 VTT 字幕为 word_boundaries 格式
        return self._parse_vtt(vtt_path)

    @staticmethod
    def _parse_vtt(vtt_path: str) -> List[Tuple[timedelta, timedelta, str]]:
        """解析 VTT/SRT 字幕文件为 (start, end, text) 元组列表"""
        boundaries: List[Tuple[timedelta, timedelta, str]] = []
        try:
            with open(vtt_path, "r", encoding="utf-8") as f:
                content = f.read()
        except FileNotFoundError:
            logger.warning("VTT 文件不存在: %s", vtt_path)
            return boundaries

        # 匹配 SRT/VTT 时间戳格式: HH:MM:SS,mmm --> HH:MM:SS,mmm
        pattern = re.compile(
            r"(\d{2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[.,]\d{3})"
        )
        blocks = content.strip().split("\n\n")
        for block in blocks:
            lines = block.strip().split("\n")
            for i, line in enumerate(lines):
                match = pattern.search(line)
                if match:
                    start_str = match.group(1).replace(",", ".")
                    end_str = match.group(2).replace(",", ".")
                    text_lines = [l.strip() for l in lines[i+1:] if l.strip()]
                    text = " ".join(text_lines)
                    if text:
                        start = TTSGenerator._parse_timestamp(start_str)
                        end = TTSGenerator._parse_timestamp(end_str)
                        boundaries.append((start, end, text))
                    break

        logger.info("从 VTT 解析出 %d 个时间边界", len(boundaries))
        return boundaries

    @staticmethod
    def _parse_timestamp(ts: str) -> timedelta:
        """解析 HH:MM:SS.mmm 格式时间戳"""
        parts = ts.split(":")
        h = int(parts[0])
        m = int(parts[1])
        s_parts = parts[2].split(".")
        s = int(s_parts[0])
        ms = int(s_parts[1]) if len(s_parts) > 1 else 0
        return timedelta(hours=h, minutes=m, seconds=s, milliseconds=ms)

    async def _tts_with_subs(
        self, text: str, output_path: str
    ) -> List[Tuple[timedelta, timedelta, str]]:
        """
        edge-tts 流式合成：同时收集音频和时间边界事件

        edge-tts v7+ 默认返回 SentenceBoundary（句级别）。
        对于长句，按字符均匀拆分为短字幕段。
        """
        import edge_tts

        communicate = edge_tts.Communicate(text, self.voice, rate=self.rate)

        audio_data = bytearray()
        boundaries: List[Tuple[timedelta, timedelta, str]] = []

        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_data.extend(chunk["data"])
            elif chunk["type"] in ("WordBoundary", "SentenceBoundary"):
                offset = chunk["offset"]       # 100-nanosecond units
                duration = chunk["duration"]
                start = timedelta(microseconds=offset / 10)
                end = timedelta(microseconds=(offset + duration) / 10)
                boundaries.append((start, end, chunk["text"]))

        # 写入音频文件
        with open(output_path, "wb") as f:
            f.write(bytes(audio_data))

        logger.info("收集到 %d 个时间边界", len(boundaries))

        # 如果是句级别（SentenceBoundary），拆分长句为短段
        result: List[Tuple[timedelta, timedelta, str]] = []
        for start, end, sentence in boundaries:
            # 去除句子首尾空白
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) <= _SUB_MAX_CHARS:
                result.append((start, end, sentence))
            else:
                # 按标点或 max_chars 拆分，均匀分配时间
                segments = self._split_sentence(sentence)
                total_chars = sum(len(s) for s in segments)
                total_dur = (end - start).total_seconds()
                cursor = start
                for seg in segments:
                    seg_dur = total_dur * len(seg) / total_chars if total_chars > 0 else 0
                    seg_end = cursor + timedelta(seconds=seg_dur)
                    result.append((cursor, seg_end, seg))
                    cursor = seg_end

        logger.info("字幕分段: %d 条", len(result))
        return result

    @staticmethod
    def _split_sentence(sentence: str, max_chars: int = _SUB_MAX_CHARS) -> List[str]:
        """
        将长句拆分为短段，优先在标点处断开

        Returns:
            拆分后的文本列表
        """
        # 先按主要标点拆分
        parts = re.split(r"([，。！？；、…：])", sentence)

        segments: List[str] = []
        buf = ""
        for part in parts:
            if not part:
                continue
            # 标点附着到前面的文本
            if part in "，。！？；、…：":
                buf += part
                continue
            if len(buf) + len(part) > max_chars and buf:
                segments.append(buf)
                buf = part
            else:
                buf += part
        if buf:
            segments.append(buf)

        # 二次拆分：如果仍有超长段，强制按 max_chars 切
        result: List[str] = []
        for seg in segments:
            while len(seg) > max_chars:
                result.append(seg[:max_chars])
                seg = seg[max_chars:]
            if seg:
                result.append(seg)

        return result

    # ── 音视频合并 ──

    @staticmethod
    def get_duration(file_path: str) -> float:
        """用 ffmpeg 获取媒体文件时长（秒）"""
        cmd = [FFMPEG_EXE, "-i", file_path, "-f", "null", "-"]
        try:
            result = subprocess.run(
                cmd, capture_output=True, timeout=30,
                text=False,  # read as bytes to avoid GBK decode errors on Windows
            )
            stderr = result.stderr.decode("utf-8", errors="replace")
            for line in stderr.split("\n"):
                if "Duration:" in line:
                    dur_str = line.split("Duration:")[1].split(",")[0].strip()
                    parts = dur_str.split(":")
                    return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        except Exception as e:
            logger.warning("获取时长失败: %s", e)
        return 0.0

    @staticmethod
    def get_video_resolution(file_path: str) -> Tuple[int, int]:
        """用 ffmpeg 获取视频分辨率 (width, height)，默认返回 (1080, 1920)"""
        cmd = [FFMPEG_EXE, "-i", file_path, "-f", "null", "-"]
        try:
            result = subprocess.run(
                cmd, capture_output=True, timeout=30, text=False,
            )
            stderr = result.stderr.decode("utf-8", errors="replace")
            # 匹配 "1920x1080" 或 "1080x1920" 等格式
            match = re.search(r'(\d{3,4})x(\d{3,4})', stderr)
            if match:
                w, h = int(match.group(1)), int(match.group(2))
                logger.info("视频分辨率: %dx%d", w, h)
                return w, h
        except Exception as e:
            logger.warning("获取视频分辨率失败: %s", e)
        return 1080, 1920  # 默认竖屏

    @staticmethod
    def merge(
        video_path: Path,
        audio_path: Path,
        output_path: Path,
        subtitle_path: Optional[Path] = None,
        loop_video: bool = True,
    ) -> Path:
        """
        将音频（+可选字幕）合并到视频中

        策略：
        - loop_video=True (默认): 音频比视频长时循环视频画面
        - loop_video=False: 不循环，取较短的那个自然截止（-shortest）
        - 有字幕 → 烧录到画面（需重编码视频）
        """
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        video_dur = TTSGenerator.get_duration(str(video_path))
        audio_dur = TTSGenerator.get_duration(str(audio_path))
        logger.info("视频时长: %.1fs, 音频时长: %.1fs", video_dur, audio_dur)

        need_loop = loop_video and audio_dur > video_dur and video_dur > 0
        has_subs = subtitle_path and subtitle_path.exists()

        if need_loop:
            logger.info("音频更长，循环视频画面以匹配...")
        elif audio_dur > video_dur and not loop_video:
            logger.info("音频更长，以视频时长为准截止（不循环）")

        # 构建 ffmpeg 命令
        cmd = [FFMPEG_EXE]

        # 输入：视频（可能循环）
        if need_loop:
            cmd += ["-stream_loop", "-1"]
        cmd += ["-i", str(video_path)]

        # 输入：音频
        cmd += ["-i", str(audio_path)]

        # 字幕滤镜（ASS 格式，直接烧录到画面）
        if has_subs:
            # Windows 路径需要转义（ass 滤镜需要正斜杠和冒号转义）
            sub_path_escaped = str(subtitle_path).replace("\\", "/").replace(":", "\\:")
            # 设置字体目录，确保 ffmpeg 能找到中文字体
            import platform, os
            fontsdir = ""
            if platform.system() == "Windows":
                win_fonts = os.path.join(
                    os.environ.get("WINDIR", "C:\\Windows"), "Fonts"
                ).replace("\\", "/").replace(":", "\\:")
                fontsdir = f":fontsdir='{win_fonts}'"
            vf = f"ass='{sub_path_escaped}'{fontsdir}"
            cmd += [
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-vf", vf,
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-c:a", "aac", "-b:a", "192k",
            ]
            logger.info("烧录字幕: %s (字体目录: %s)", subtitle_path.name, fontsdir or "系统默认")
        else:
            cmd += [
                "-map", "0:v:0",
                "-map", "1:a:0",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "192k",
            ]

        cmd += ["-shortest", "-y", str(output_path)]

        logger.debug("ffmpeg: %s", " ".join(cmd))

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=False, timeout=300,
            )
            if result.returncode != 0:
                stderr = result.stderr.decode("utf-8", errors="replace")
                logger.error("ffmpeg 失败:\n%s", stderr[-800:])
                raise RuntimeError(f"ffmpeg 合并失败 (code={result.returncode})")
        except subprocess.TimeoutExpired:
            raise RuntimeError("ffmpeg 合并超时")

        size_mb = output_path.stat().st_size / 1024 / 1024
        logger.info("合并完成: %s (%.1fMB)", output_path.name, size_mb)
        return output_path

    # ── 完整流程 ──

    def add_voiceover(
        self,
        video_path: Path,
        title: str,
        body: str,
        save_dir: Optional[Path] = None,
        filename_prefix: str = "video",
        custom_script: Optional[str] = None,
        with_subtitle: bool = True,
        loop_video: bool = True,
        with_avatar: bool = False,
        avatar_image: Optional[Path] = None,
        avatar_position: str = "bottom_right",
        avatar_scale: float = 0.3,
        fal_key: str = "",
    ) -> Optional[Path]:
        """
        完整配音+字幕+数字人流程：
        文案 → 口播稿 → TTS+字幕 → 合并视频 → (可选)数字人叠加

        Args:
            video_path: 原始无声视频路径
            title: 文案标题
            body: 文案正文
            save_dir: 输出目录（默认和视频同目录）
            filename_prefix: 输出文件名前缀
            custom_script: 自定义口播稿（跳过 LLM 生成）
            with_subtitle: 是否生成字幕（默认 True）
            loop_video: 音频比视频长时是否循环视频（默认 True）
            with_avatar: 是否添加数字人（默认 False）
            avatar_image: 数字人参考人像图路径
            avatar_position: 数字人叠加位置
            avatar_scale: 数字人缩放比例（0.0-1.0）
            fal_key: fal.ai API Key

        Returns:
            最终视频文件路径 或 None
        """
        out_dir = save_dir or video_path.parent

        # 1. 生成口播稿
        if custom_script:
            script = custom_script
            logger.info("使用自定义口播稿")
        else:
            script = self.generate_script(title, body)
        print(f"\n口播稿:\n{script}\n")

        # 2. TTS 合成语音 + 字幕（自动适配视频分辨率）
        audio_path = out_dir / f"{filename_prefix}_audio.mp3"
        sub_path = (out_dir / f"{filename_prefix}_sub.ass") if with_subtitle else None
        vid_w, vid_h = self.get_video_resolution(str(video_path))
        audio_path, sub_file = self.synthesize(
            script, audio_path, sub_path,
            video_width=vid_w, video_height=vid_h,
        )

        # 3. 合并音视频（+字幕）
        final_path = out_dir / f"{filename_prefix}_final.mp4"
        self.merge(video_path, audio_path, final_path, subtitle_path=sub_file, loop_video=loop_video)
        print(f"有声视频: {final_path}")

        # 4. (可选) 数字人叠加
        if with_avatar and avatar_image and fal_key:
            from shared.media.avatar import AvatarGenerator
            avatar_gen = AvatarGenerator(fal_key)
            avatar_output = out_dir / f"{filename_prefix}_avatar.mp4"
            result = avatar_gen.add_avatar(
                main_video=final_path,
                image_path=Path(avatar_image),
                audio_path=audio_path,
                output_path=avatar_output,
                position=avatar_position,
                scale=avatar_scale,
            )
            if result and result.exists():
                final_path = result
                print(f"数字人视频: {final_path}")
            else:
                logger.warning("数字人生成失败，使用无数字人版本")
        elif with_avatar and not fal_key:
            logger.warning("FAL_KEY 未配置，跳过数字人生成")
        elif with_avatar and not avatar_image:
            logger.warning("未指定人像图 (--avatar-image)，跳过数字人生成")

        return final_path
