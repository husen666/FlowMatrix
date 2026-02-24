"""
通用工具函数 —— slugify / escape / prompt 解析等
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime
from typing import Any, Dict, List, Sequence


def slugify(text: str) -> str:
    """将文本转为 URL 友好的 slug（保留 Unicode）"""
    normalized = text.strip().lower()
    normalized = re.sub(r"[^\w\s-]", " ", normalized, flags=re.UNICODE)
    normalized = re.sub(r"[\s_]+", "-", normalized, flags=re.UNICODE)
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-")
    if not normalized or normalized == "-":
        normalized = f"article-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    return normalized[:90]


def slugify_chinese(text: str) -> str:
    """中文转拼音 + 英文保留，生成纯英文 slug"""
    try:
        from pypinyin import lazy_pinyin
    except ImportError:
        import warnings
        warnings.warn(
            "pypinyin 未安装，slug 将保留中文。请运行: pip install pypinyin",
            stacklevel=2,
        )
        return slugify(text)

    cleaned = text.strip()
    if not cleaned:
        return f"article-{datetime.now().strftime('%Y%m%d-%H%M%S')}"

    tokens: List[str] = []
    for segment in re.findall(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]+", cleaned):
        if re.match(r"[a-zA-Z0-9]+", segment):
            tokens.append(segment.lower())
        else:
            tokens.extend(lazy_pinyin(segment))

    result = "-".join(tokens)
    result = re.sub(r"-{2,}", "-", result).strip("-")
    return result[:80] if result else f"article-{datetime.now().strftime('%Y%m%d-%H%M%S')}"


def split_csv(value: str) -> List[str]:
    """按中英文逗号分割字符串"""
    if not value:
        return []
    parts = re.split(r"[,，]", value)
    return [p.strip() for p in parts if p.strip()]


def escape(text: str) -> str:
    """HTML 转义"""
    return html.escape(text, quote=True)


def merge_unique(items: Sequence[str]) -> List[str]:
    """去重合并字符串列表，保持顺序"""
    result: List[str] = []
    for item in items:
        value = (item or "").strip()
        if not value:
            continue
        if value not in result:
            result.append(value)
    return result


def resolve_prompt(raw_prompt: str, topic: str, prompt_template: str) -> str:
    """解析提示词：优先使用完整 prompt，否则将 topic 套入模板"""
    if raw_prompt and raw_prompt.strip():
        return raw_prompt.strip()
    if topic and topic.strip():
        return prompt_template.format(theme=topic.strip())
    raise ValueError("必须提供 --prompt 或 --topic")


def parse_prompt_context(prompt: str) -> Dict[str, str]:
    """从模板化提示词中提取主题等字段"""
    text = (prompt or "").strip()
    fields: Dict[str, str] = {}
    if not text:
        return {"theme": "", "raw": ""}

    for key, value in re.findall(r"([^\s:：；;\n]{1,20})\s*[：:]\s*([^；;\n]+)", text):
        fields[key.strip().lower()] = value.strip()

    theme = fields.get("主题") or fields.get("theme") or fields.get("topic") or ""
    if not theme:
        first_line = re.split(r"[；;\n。]", text)[0].strip()
        theme = re.sub(r"^(主题|theme|topic)\s*[：:]\s*", "", first_line, flags=re.IGNORECASE)
    return {"theme": theme.strip(), "raw": text}


def json_for_script(data: Dict[str, Any]) -> str:
    """JSON 序列化，安全嵌入 <script> 标签"""
    return json.dumps(data, ensure_ascii=False).replace("</", "<\\/")


def make_anchor_id(text: str, idx: int) -> str:
    """生成 HTML 锚点 ID"""
    token = re.sub(r"[^\w\s-]", " ", text, flags=re.UNICODE).strip().lower()
    token = re.sub(r"[\s_]+", "-", token)
    token = re.sub(r"-{2,}", "-", token).strip("-")
    if not token:
        return f"section-{idx + 1}"
    return f"section-{idx + 1}-{token[:30]}"


def save_json(path, data: Any) -> None:
    """保存 JSON 文件（自动创建父目录）"""
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
