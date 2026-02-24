"""
故事视频生成引擎
—— 传入主题/场景等参数，通过 LLM 生成分镜脚本，再批量生成视频素材和全平台文案素材

核心流程:
  1. LLM 生成分镜脚本（StoryBoard）
  2. 火山引擎即梦AI 逐镜头生成视频片段
  3. ffmpeg 拼接 + edge-tts 配音字幕
  4. LLM 生成全平台文案（小红书/抖音/头条/知乎/视频号/微博）
  5. 统一保存到 output/{slug}/ 供各平台 publisher 直接使用

用法（作为模块）:
    from shared.media.story_video import StoryVideoPipeline
    pipeline = StoryVideoPipeline(vg=vg, tts=tts, llm=llm)
    result = pipeline.run(
        theme="西游记大闹天宫",
        scene="孙悟空大闹蟠桃会，偷吃仙丹",
        style="epic cinematic",
        num_shots=6,
        output_dir=Path("output/xiyouji-pantao"),
    )
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from shared.llm.client import LLMClient, extract_json_block
from shared.media.video import VideoGenerator
from shared.media.tts import TTSGenerator, FFMPEG_EXE
from shared.utils.logger import get_logger

logger = get_logger("story-video")


# ================================================================
#  数据模型
# ================================================================

@dataclass
class StoryShot:
    """单个分镜"""
    id: int
    title: str              # 镜头标题（中文）
    prompt: str             # 英文视频生成 prompt
    voiceover: str          # 中文旁白

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "prompt": self.prompt,
            "voiceover": self.voiceover,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "StoryShot":
        return cls(
            id=data["id"],
            title=data["title"],
            prompt=data["prompt"],
            voiceover=data["voiceover"],
        )


@dataclass
class StoryBoard:
    """完整分镜脚本"""
    project: str                            # 项目名称
    theme: str                              # 主题
    scene: str                              # 场景描述
    style: str                              # 视觉风格
    character_card: str = ""                # 角色外貌设定（英文）
    environment_card: str = ""              # 环境设定（英文）
    style_suffix: str = ""                  # 统一风格后缀（英文）
    cover_prompt: str = ""                  # 封面图 prompt
    shots: List[StoryShot] = field(default_factory=list)

    @property
    def full_voiceover(self) -> str:
        return "".join(s.voiceover for s in self.shots)

    @property
    def duration_sec(self) -> int:
        return len(self.shots) * 5

    def to_dict(self) -> dict:
        return {
            "project": self.project,
            "theme": self.theme,
            "scene": self.scene,
            "style": self.style,
            "character_card": self.character_card,
            "environment_card": self.environment_card,
            "style_suffix": self.style_suffix,
            "cover_prompt": self.cover_prompt,
            "shots": [s.to_dict() for s in self.shots],
            "full_voiceover": self.full_voiceover,
            "duration_sec": self.duration_sec,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "StoryBoard":
        board = cls(
            project=data["project"],
            theme=data["theme"],
            scene=data.get("scene", ""),
            style=data.get("style", ""),
            character_card=data.get("character_card", ""),
            environment_card=data.get("environment_card", ""),
            style_suffix=data.get("style_suffix", ""),
            cover_prompt=data.get("cover_prompt", ""),
        )
        board.shots = [StoryShot.from_dict(s) for s in data.get("shots", [])]
        return board


@dataclass
class PlatformContent:
    """单个平台的文案"""
    platform: str
    content_type: str   # "video" | "article"
    title: str
    body: str
    tags: List[str] = field(default_factory=list)
    video_path: str = ""

    def full_text(self) -> str:
        parts = [self.body]
        if self.tags:
            sep = " "
            # 微博用 #tag# 格式，其他用 #tag
            if self.platform == "weibo":
                parts.append(sep.join(f"#{t}#" for t in self.tags))
            else:
                parts.append(sep.join(f"#{t}" for t in self.tags))
        return "\n\n".join(parts)

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "type": self.content_type,
            "title": self.title,
            "body": self.body,
            "tags": self.tags,
            "video_path": self.video_path,
            "full_text": self.full_text(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PlatformContent":
        return cls(
            platform=data["platform"],
            content_type=data.get("type", "video"),
            title=data.get("title", ""),
            body=data.get("body", ""),
            tags=data.get("tags", data.get("hashtags", [])),
            video_path=data.get("video_path", ""),
        )


@dataclass
class StoryVideoResult:
    """生成结果"""
    storyboard: StoryBoard
    clip_paths: List[Path] = field(default_factory=list)
    concat_path: Optional[Path] = None
    final_video_path: Optional[Path] = None
    platform_contents: Dict[str, PlatformContent] = field(default_factory=dict)
    output_dir: Optional[Path] = None


# ================================================================
#  LLM Prompts
# ================================================================

_STORYBOARD_SYSTEM = """You are a professional short video director and storyboard artist for Chinese social media.
Your job: given a theme and scene description, create a storyboard with multiple shots for a short video (20-40 seconds total).

## CRITICAL RULES
- Each shot is exactly 5 seconds of video, so plan accordingly
- Video prompts MUST be in pure English (for AI text-to-video generation)
- Voiceover MUST be in Chinese (旁白)
- Each voiceover line should be 15-25 Chinese characters (readable in ~5 seconds)

## STORYBOARD STRUCTURE

For each shot, provide:
1. **title**: Short Chinese title for the shot (2-6 characters)
2. **prompt**: Detailed English visual description (80-150 words) including:
   - Subject and action (what's happening)
   - Environment (where it happens)
   - Camera movement (dolly, crane, tracking, orbit, etc.)
   - Lighting (key light, fill light, rim light, volumetric)
   - Atmosphere (particles, fog, bokeh, etc.)
   - NO text/words/letters visible in the video
3. **voiceover**: Chinese narration for this shot (15-25 chars, punchy and engaging)

## CHARACTER CONSISTENCY
If the user provides a character card, you MUST reference it in EVERY shot's prompt to maintain visual consistency.
If no character card is provided, describe characters consistently across all shots.

## VISUAL STYLE
End each prompt with the style suffix provided by the user, or use:
"cinematic color grading, volumetric lighting, 8K resolution, smooth camera movement, no visible text"

## COVER IMAGE
Also generate a cover_prompt (English, for generating a static poster/thumbnail image).

## NARRATIVE ARC
Shots should follow a story arc:
- Shot 1-2: Setup / establishment
- Middle shots: Rising action / conflict / spectacle
- Final shot: Climax / resolution / punchline

## OUTPUT FORMAT (strict JSON, no markdown)
{
  "project": "项目名称",
  "character_card": "English character description for visual consistency",
  "environment_card": "English environment description",
  "style_suffix": "English style keywords for visual consistency",
  "cover_prompt": "English cover image prompt (no text in image)",
  "shots": [
    {"id": 1, "title": "镜头标题", "prompt": "English visual description...", "voiceover": "中文旁白"},
    {"id": 2, "title": "镜头标题", "prompt": "English visual description...", "voiceover": "中文旁白"}
  ]
}"""


_PLATFORM_CONTENT_SYSTEM = """你是一位全平台短视频运营专家，擅长为同一个视频写出各平台最优文案。

根据视频的分镜脚本信息，为以下 6 个平台生成发布文案：

## 平台特性

1. **小红书 (xhs)**: 年轻女性为主，标题要吸引眼球，正文口语化，200-400字，话题标签 6-8 个
2. **抖音 (douyin)**: 短平快，标题≤20字，正文100-200字，话题标签 5-7 个
3. **视频号 (channels)**: 微信生态，正文300-500字，稍正式但不枯燥，话题标签 4-6 个
4. **知乎 (zhihu)**: 深度内容，标题要有信息量，正文500-800字，标签 3-5 个
5. **头条 (toutiao)**: 标题≤30字吸引点击，正文300-500字，标签 3-5 个
6. **微博 (weibo)**: 标题≤20字，正文200-400字，话题用 #话题# 格式，话题标签 5-7 个

## 通用规则
- 每个平台的文案风格要匹配该平台的调性
- 禁止使用「赋能」「闭环」等空洞词汇
- 适当使用 emoji 增加阅读体验（知乎除外）
- 结尾引导互动（提问/投票/讨论）
- 标签只输出文字，不带 # 号

## 输出格式（严格 JSON，不要 markdown 代码块）
{
  "xhs": {"title": "...", "body": "...", "tags": ["..."]},
  "douyin": {"title": "...", "body": "...", "tags": ["..."]},
  "channels": {"title": "...", "body": "...", "tags": ["..."]},
  "zhihu": {"title": "...", "body": "...", "tags": ["..."]},
  "toutiao": {"title": "...", "body": "...", "tags": ["..."]},
  "weibo": {"title": "...", "body": "...", "tags": ["..."]}
}"""


# ================================================================
#  核心引擎
# ================================================================

class StoryVideoPipeline:
    """
    故事视频一站式生成管线

    用法:
        pipeline = StoryVideoPipeline(vg=vg, tts=tts, llm=llm)
        result = pipeline.run(
            theme="西游记大闹天宫",
            scene="悟空大闹蟠桃会",
            style="epic cinematic",
            num_shots=6,
            output_dir=Path("output/xiyouji-pantao"),
        )
    """

    def __init__(
        self,
        vg: VideoGenerator,
        tts: TTSGenerator,
        llm: LLMClient,
        video_ratio: str = "16:9",
        video_frames: int = 121,       # 121≈5s, 241≈10s
        voice: str = "zh-CN-YunxiNeural",
        voice_rate: str = "+0%",
    ) -> None:
        self.vg = vg
        self.tts = tts
        self.llm = llm
        self.video_ratio = video_ratio
        self.video_frames = video_frames
        self.voice = voice
        self.voice_rate = voice_rate

    # ────────── 完整流程 ──────────

    def run(
        self,
        theme: str,
        output_dir: Path,
        scene: str = "",
        style: str = "epic cinematic",
        num_shots: int = 6,
        character_desc: str = "",
        publish_ready: bool = True,
        storyboard_json: Optional[Path] = None,
    ) -> StoryVideoResult:
        """
        一键生成故事视频 + 全平台文案

        Args:
            theme: 主题（如 "西游记大闹天宫"）
            output_dir: 输出目录
            scene: 场景描述（如 "悟空偷吃蟠桃，大闹天宫"）
            style: 视觉风格（如 "epic cinematic", "comedy parody"）
            num_shots: 分镜数量（4-8）
            character_desc: 角色描述（中文，可选，LLM 会翻译为英文角色卡）
            publish_ready: 是否生成各平台文案 JSON
            storyboard_json: 如已有分镜脚本 JSON，跳过 LLM 生成

        Returns:
            StoryVideoResult
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        result = StoryVideoResult(storyboard=StoryBoard(
            project=theme, theme=theme, scene=scene, style=style
        ), output_dir=output_dir)

        t0 = time.time()

        # Step 1: 生成 / 加载分镜脚本
        if storyboard_json and storyboard_json.exists():
            logger.info("从文件加载分镜脚本: %s", storyboard_json)
            with open(storyboard_json, "r", encoding="utf-8") as f:
                result.storyboard = StoryBoard.from_dict(json.load(f))
        else:
            logger.info("Step 1: LLM 生成分镜脚本...")
            result.storyboard = self.generate_storyboard(
                theme=theme, scene=scene, style=style,
                num_shots=num_shots, character_desc=character_desc,
            )
        self._save_json(output_dir / "storyboard.json", result.storyboard.to_dict())
        self._preview_storyboard(result.storyboard)

        # Step 2: 逐镜头生成视频
        logger.info("Step 2: 生成视频片段...")
        result.clip_paths = self.generate_clips(result.storyboard, output_dir)
        if len(result.clip_paths) < len(result.storyboard.shots):
            logger.warning("仅生成 %d/%d 个片段，可重新运行断点续传",
                          len(result.clip_paths), len(result.storyboard.shots))

        # Step 3: 拼接 + 配音
        if len(result.clip_paths) >= 2:
            logger.info("Step 3: 拼接 + 配音...")
            result.concat_path = output_dir / "concat.mp4"
            self.concat_videos(result.clip_paths, result.concat_path)

            final = self.add_narration(
                result.storyboard, result.concat_path, output_dir
            )
            if final and final.exists():
                result.final_video_path = final
            else:
                result.final_video_path = result.concat_path
        elif len(result.clip_paths) == 1:
            result.final_video_path = result.clip_paths[0]
        else:
            logger.error("无视频片段可用")

        # Step 4: 生成全平台文案
        if publish_ready and result.final_video_path:
            logger.info("Step 4: 生成全平台文案...")
            video_path_str = str(result.final_video_path)
            result.platform_contents = self.generate_platform_content(
                result.storyboard, video_path_str
            )
            self._save_platform_content(output_dir, result.platform_contents)

            # 更新所有 JSON 中的 video_path
            self._update_video_paths(output_dir, video_path_str)

        elapsed = int(time.time() - t0)
        self._print_summary(result, elapsed)
        return result

    # ────────── Step 1: 分镜脚本生成 ──────────

    def generate_storyboard(
        self,
        theme: str,
        scene: str = "",
        style: str = "epic cinematic",
        num_shots: int = 6,
        character_desc: str = "",
    ) -> StoryBoard:
        """通过 LLM 生成分镜脚本"""
        user_parts = [
            f"主题: {theme}",
            f"镜头数量: {num_shots} 个（每个 5 秒，共 {num_shots * 5} 秒）",
            f"视觉风格: {style}",
        ]
        if scene:
            user_parts.append(f"场景/剧情: {scene}")
        if character_desc:
            user_parts.append(f"主要角色: {character_desc}")

        user_msg = "\n".join(user_parts)
        logger.info("调用 LLM 生成分镜脚本...\n%s", user_msg)

        raw = self.llm.chat(
            system_prompt=_STORYBOARD_SYSTEM,
            user_prompt=user_msg,
            temperature=0.8,
        )
        if not raw:
            raise RuntimeError("LLM 生成分镜脚本失败：无返回")

        data = extract_json_block(raw)
        if not data:
            raise RuntimeError(f"无法解析分镜脚本 JSON:\n{raw[:500]}")

        board = StoryBoard(
            project=data.get("project", theme),
            theme=theme,
            scene=scene,
            style=style,
            character_card=data.get("character_card", ""),
            environment_card=data.get("environment_card", ""),
            style_suffix=data.get("style_suffix",
                "cinematic color grading, volumetric lighting, 8K resolution, "
                "smooth camera movement, no visible text"
            ),
            cover_prompt=data.get("cover_prompt", ""),
        )

        for s in data.get("shots", []):
            board.shots.append(StoryShot(
                id=s.get("id", len(board.shots) + 1),
                title=s.get("title", f"镜头{len(board.shots)+1}"),
                prompt=s.get("prompt", ""),
                voiceover=s.get("voiceover", ""),
            ))

        if not board.shots:
            raise RuntimeError("LLM 未生成任何分镜")

        logger.info("分镜脚本生成完成: %d 个镜头", len(board.shots))
        return board

    # ────────── Step 2: 视频生成 ──────────

    def generate_clips(self, board: StoryBoard, output_dir: Path) -> List[Path]:
        """逐镜头生成视频，支持断点续传"""
        paths = []
        total = len(board.shots)

        for shot in board.shots:
            safe_title = shot.title.replace(" ", "_").replace("/", "_")
            save_path = output_dir / f"shot{shot.id:02d}_{safe_title}.mp4"

            # 断点续传：已有文件且大于 100KB 跳过
            if save_path.exists() and save_path.stat().st_size > 100_000:
                logger.info("[Shot %d/%d: %s] 已存在, 跳过", shot.id, total, shot.title)
                paths.append(save_path)
                continue

            # 拼接完整 prompt
            full_prompt = shot.prompt
            if board.style_suffix:
                full_prompt = f"{shot.prompt} {board.style_suffix}"

            logger.info("[Shot %d/%d: %s] 提交生成...", shot.id, total, shot.title)

            try:
                task_id = self.vg.submit_task(
                    full_prompt,
                    aspect_ratio=self.video_ratio,
                    frames=self.video_frames,
                )
                video_url = self.vg.poll_result(task_id, max_wait=300, interval=10)
                if not video_url:
                    logger.error("[Shot %d] 生成失败!", shot.id)
                    continue
                self.vg.download(video_url, save_path)
                paths.append(save_path)
                logger.info("[Shot %d] 完成: %s", shot.id, save_path.name)

                # 避免 API 限频
                if shot.id < total:
                    time.sleep(3)
            except Exception as e:
                logger.error("[Shot %d] 异常: %s", shot.id, e)
                continue

        logger.info("视频生成完成: %d/%d 个片段", len(paths), total)
        return paths

    # ────────── Step 3: 拼接 + 配音 ──────────

    def concat_videos(self, clip_paths: List[Path], output_path: Path) -> Path:
        """拼接多个视频片段"""
        list_file = output_path.parent / "_concat_list.txt"
        with open(list_file, "w", encoding="utf-8") as f:
            for p in clip_paths:
                f.write(f"file '{str(p).replace(chr(92), '/')}'\n")

        cmd = [FFMPEG_EXE, "-f", "concat", "-safe", "0",
               "-i", str(list_file), "-c", "copy", "-y", str(output_path)]
        result = subprocess.run(cmd, capture_output=True, text=False, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f"视频拼接失败: {result.stderr}")

        list_file.unlink(missing_ok=True)
        logger.info("拼接完成: %s", output_path.name)
        return output_path

    def add_narration(
        self, board: StoryBoard, video_path: Path, output_dir: Path
    ) -> Optional[Path]:
        """为拼接后的视频添加旁白 + 字幕"""
        voiceover = board.full_voiceover
        if not voiceover:
            logger.warning("无旁白文本，跳过配音")
            return None

        return self.tts.add_voiceover(
            video_path=video_path,
            title=board.project,
            body=voiceover,
            save_dir=output_dir,
            filename_prefix="final",
            custom_script=voiceover,
            with_subtitle=True,
            loop_video=False,
        )

    # ────────── Step 4: 全平台文案 ──────────

    def generate_platform_content(
        self, board: StoryBoard, video_path: str = ""
    ) -> Dict[str, PlatformContent]:
        """通过 LLM 生成 6 个平台的发布文案"""
        # 构建分镜摘要
        shots_summary = []
        for s in board.shots:
            shots_summary.append(f"镜头{s.id}「{s.title}」旁白: {s.voiceover}")

        user_msg = (
            f"视频项目: {board.project}\n"
            f"主题: {board.theme}\n"
            f"场景: {board.scene}\n"
            f"风格: {board.style}\n"
            f"时长: {board.duration_sec} 秒 ({len(board.shots)} 个镜头)\n\n"
            f"分镜内容:\n" + "\n".join(shots_summary)
        )

        logger.info("调用 LLM 生成全平台文案...")
        raw = self.llm.chat(
            system_prompt=_PLATFORM_CONTENT_SYSTEM,
            user_prompt=user_msg,
            temperature=0.7,
        )
        if not raw:
            logger.error("LLM 生成文案失败")
            return {}

        data = extract_json_block(raw)
        if not data:
            logger.error("无法解析文案 JSON:\n%s", raw[:500])
            return {}

        contents = {}
        platform_map = {
            "xhs": "xiaohongshu",
            "douyin": "douyin",
            "channels": "channels",
            "zhihu": "zhihu",
            "toutiao": "toutiao",
            "weibo": "weibo",
        }

        for key, platform_name in platform_map.items():
            pdata = data.get(key, {})
            if not pdata:
                continue
            contents[key] = PlatformContent(
                platform=platform_name,
                content_type="video",
                title=pdata.get("title", ""),
                body=pdata.get("body", ""),
                tags=pdata.get("tags", []),
                video_path=video_path,
            )

        logger.info("全平台文案生成完成: %s", ", ".join(contents.keys()))
        return contents

    # ────────── 保存 ──────────

    def _save_platform_content(
        self, output_dir: Path, contents: Dict[str, PlatformContent]
    ):
        """保存各平台文案到 JSON 文件"""
        file_map = {
            "xhs": "xhs_content.json",
            "douyin": "dy_content.json",
            "channels": "channels_content.json",
            "zhihu": "zh_content.json",
            "toutiao": "toutiao_content.json",
            "weibo": "wb_content.json",
        }
        for key, filename in file_map.items():
            pc = contents.get(key)
            if pc:
                self._save_json(output_dir / filename, pc.to_dict())

    def _update_video_paths(self, output_dir: Path, video_path: str):
        """更新所有 JSON 文件中的 video_path"""
        json_files = [
            "storyboard.json", "xhs_content.json", "dy_content.json",
            "channels_content.json", "zh_content.json", "toutiao_content.json",
            "wb_content.json",
        ]
        for name in json_files:
            fp = output_dir / name
            if fp.exists():
                data = json.loads(fp.read_text(encoding="utf-8"))
                data["video_path"] = video_path
                self._save_json(fp, data)

    @staticmethod
    def _save_json(path: Path, data: dict):
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("  -> %s", path.name)

    # ────────── 预览 ──────────

    def _preview_storyboard(self, board: StoryBoard):
        """打印分镜脚本预览"""
        sep = "=" * 62
        print(f"\n{sep}")
        print(f"  {board.project}")
        print(f"  {len(board.shots)} shots x 5s = ~{board.duration_sec}s")
        print(sep)

        for s in board.shots:
            print(f"\n  Shot {s.id}: {s.title}")
            print(f"    旁白 ({len(s.voiceover)}字): {s.voiceover}")
            first_line = s.prompt.split(". ")[0] + "..."
            print(f"    画面: {first_line[:80]}")

        vo = board.full_voiceover
        est_sec = len(vo) / 3.8
        print(f"\n  旁白总长: {len(vo)} 字 ~{est_sec:.0f}s")
        print(f"  视频总长: {board.duration_sec}s")
        print(sep)

    def _print_summary(self, result: StoryVideoResult, elapsed: int):
        """打印生成结果摘要"""
        sep = "=" * 60
        print(f"\n{sep}")
        print("  生成完成!")
        print(sep)
        print(f"  项目:   {result.storyboard.project}")
        print(f"  镜头:   {len(result.clip_paths)}/{len(result.storyboard.shots)}")
        if result.final_video_path:
            size_mb = result.final_video_path.stat().st_size / 1024 / 1024
            print(f"  视频:   {result.final_video_path} ({size_mb:.1f}MB)")
        print(f"  文案:   {', '.join(result.platform_contents.keys()) or '无'}")
        print(f"  耗时:   {elapsed // 60}m{elapsed % 60}s")
        print(f"  目录:   {result.output_dir}")
        if result.output_dir:
            for f in sorted(result.output_dir.glob("*.json")):
                print(f"    {f.name}")
        print(sep)


# ================================================================
#  便捷函数 —— 供 main.py 和外部脚本直接调用
# ================================================================

def create_pipeline(
    settings,
    llm: LLMClient,
    video_ratio: str = "16:9",
    video_frames: int = 121,
    voice: str = "zh-CN-YunxiNeural",
    voice_rate: str = "+0%",
) -> StoryVideoPipeline:
    """从 settings 创建 Pipeline 实例"""
    vg = VideoGenerator(
        volc_ak=settings.volc_ak,
        volc_sk=settings.volc_sk,
        volc_host=settings.volc_host,
        volc_service=settings.volc_service,
        volc_region=settings.volc_region,
    )
    tts = TTSGenerator(llm=None, voice=voice, rate=voice_rate)
    return StoryVideoPipeline(
        vg=vg, tts=tts, llm=llm,
        video_ratio=video_ratio,
        video_frames=video_frames,
        voice=voice,
        voice_rate=voice_rate,
    )


def run_story_video(
    settings,
    llm: LLMClient,
    theme: str,
    output_dir: Path,
    scene: str = "",
    style: str = "epic cinematic",
    num_shots: int = 6,
    character_desc: str = "",
    video_ratio: str = "16:9",
    video_frames: int = 121,
    voice: str = "zh-CN-YunxiNeural",
    voice_rate: str = "+0%",
    storyboard_json: Optional[Path] = None,
) -> StoryVideoResult:
    """
    一站式便捷函数: 传入参数直接生成故事视频 + 全平台文案

    Returns:
        StoryVideoResult 包含视频路径和各平台文案
    """
    pipeline = create_pipeline(
        settings, llm,
        video_ratio=video_ratio,
        video_frames=video_frames,
        voice=voice,
        voice_rate=voice_rate,
    )
    return pipeline.run(
        theme=theme,
        output_dir=output_dir,
        scene=scene,
        style=style,
        num_shots=num_shots,
        character_desc=character_desc,
        storyboard_json=storyboard_json,
    )
