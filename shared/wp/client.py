"""
WordPress REST API 客户端
—— 合并发布端（上传/发布）与读取端（拉取/解析）的完整能力
—— Session 复用 + term 缓存 + 结构化日志 + 上下文管理器
"""

from __future__ import annotations

import html as html_lib
import mimetypes
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.auth import HTTPBasicAuth

from shared.utils.exceptions import WordPressError, WPAuthError, WPNotFoundError
from shared.utils.helpers import slugify
from shared.utils.logger import get_logger

logger = get_logger("wordpress")

_USER_AGENT = "AineooPublisher/2.0 (WordPress Auto-Publish)"


# ────────── 数据模型（只读端使用） ──────────

@dataclass
class WPPost:
    """WordPress 文章简化模型"""
    id: int
    title: str
    content: str         # 纯文本（已去 HTML 标签 & 实体）
    excerpt: str
    slug: str
    link: str
    date: str
    featured_image_url: Optional[str] = None
    content_image_urls: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    categories: List[str] = field(default_factory=list)

    @property
    def all_image_urls(self) -> List[str]:
        """合并特色图片 + 正文图片，去重"""
        urls: List[str] = []
        seen: set = set()
        if self.featured_image_url:
            urls.append(self.featured_image_url)
            seen.add(self.featured_image_url)
        for u in self.content_image_urls:
            if u not in seen:
                urls.append(u)
                seen.add(u)
        return urls


# ────────── 工具函数 ──────────

def _strip_html(raw_html: str) -> str:
    """去除 HTML 标签 & 解码 HTML 实体，保留纯文本"""
    text = re.sub(r"<br\s*/?>", "\n", raw_html, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_lib.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def _extract_image_urls(raw_html: str) -> List[str]:
    """从 HTML 正文中提取所有 <img> 的 src 地址"""
    return re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', raw_html, re.IGNORECASE)


# ────────── API 客户端 ──────────

class WordPressClient:
    """封装 WordPress REST API 的调用（读写两用）"""

    def __init__(
        self,
        wp_base: str,
        wp_user: str = "",
        wp_app_password: str = "",
        timeout: int = 40,
    ) -> None:
        self.wp_base = wp_base.rstrip("/")
        self.wp_api = f"{self.wp_base}/wp-json/wp/v2"
        self.timeout = timeout

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
        })
        if wp_user and wp_app_password:
            self.session.auth = HTTPBasicAuth(wp_user, wp_app_password)

        self._term_cache: Dict[str, Dict[str, int]] = {"categories": {}, "tags": {}}

    # ── 上下文管理器 ──

    def __enter__(self) -> "WordPressClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        """关闭 HTTP 连接池"""
        self.session.close()

    # ── 内部请求（带重试） ──

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        expected_status: Tuple[int, ...],
        max_retries: int = 3,
        **kwargs,
    ) -> Any:
        last_error = ""
        backoff = 1.0
        for attempt in range(1, max_retries + 1):
            try:
                resp = self.session.request(method=method, url=url, timeout=self.timeout, **kwargs)
                if resp.status_code in expected_status:
                    return resp.json() if resp.text.strip() else {}
                last_error = f"status={resp.status_code}, body={resp.text[:200]}"
                # 4xx 客户端错误不重试
                if 400 <= resp.status_code < 500:
                    break
            except requests.exceptions.ConnectionError as exc:
                last_error = f"ConnectionError: {exc}"
            except requests.exceptions.Timeout as exc:
                last_error = f"Timeout: {exc}"
            except Exception as exc:
                last_error = str(exc)

            if attempt < max_retries:
                logger.warning(
                    "请求重试 %s %s，第 %d/%d 次失败（%.1fs 后重试）：%s",
                    method.upper(), url, attempt, max_retries, backoff, last_error,
                )
                time.sleep(backoff)
                backoff = min(backoff * 2, 16)

        raise WordPressError(f"请求失败: {method.upper()} {url} => {last_error}")

    # ── 媒体上传 ──

    def upload_media(self, file_path: Path, title: str, alt_text: str) -> Optional[Dict]:
        """上传图片到 WordPress 媒体库"""
        if not file_path.exists():
            logger.warning("文件不存在，跳过上传: %s", file_path)
            return None
        mime = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        headers = {
            "Content-Type": mime,
            "Content-Disposition": f'attachment; filename="{file_path.name}"',
        }
        with open(file_path, "rb") as f:
            try:
                media = self._request_json(
                    "post",
                    f"{self.wp_api}/media",
                    headers=headers,
                    data=f.read(),
                    expected_status=(201,),
                )
            except Exception as exc:
                logger.error("上传失败 %s: %s", file_path.name, exc)
                return None

        media_id = media["id"]
        meta_payload = {
            "title": title,
            "alt_text": alt_text,
            "caption": title,
            "description": alt_text,
        }
        try:
            self._request_json(
                "post",
                f"{self.wp_api}/media/{media_id}",
                json=meta_payload,
                expected_status=(200, 201),
            )
        except Exception as exc:
            logger.warning("媒体元信息更新失败 media_id=%d: %s", media_id, exc)
        return media

    # ── 分类/标签 ──

    def ensure_term(self, endpoint: str, name: str) -> Optional[int]:
        """查找或创建分类/标签，带缓存"""
        cache_key = name.strip().lower()
        cached = self._term_cache.get(endpoint, {}).get(cache_key)
        if cached:
            return cached

        try:
            query_items = self._request_json(
                "get",
                f"{self.wp_api}/{endpoint}",
                params={"search": name, "per_page": 100},
                expected_status=(200,),
            )
        except Exception as exc:
            logger.error("术语查询失败 %s:%s => %s", endpoint, name, exc)
            query_items = []

        if isinstance(query_items, list):
            for item in query_items:
                if item.get("name", "").strip().lower() == name.strip().lower():
                    term_id = item["id"]
                    self._term_cache.setdefault(endpoint, {})[cache_key] = term_id
                    return term_id

        try:
            created = self._request_json(
                "post",
                f"{self.wp_api}/{endpoint}",
                json={"name": name, "slug": slugify(name)},
                expected_status=(200, 201),
            )
            term_id = created["id"]
            self._term_cache.setdefault(endpoint, {})[cache_key] = term_id
            return term_id
        except Exception as exc:
            logger.error("术语创建失败 %s:%s => %s", endpoint, name, exc)
            return None

    # ── 相关文章 ──

    def get_related_posts(self, focus_keyword: str, current_slug: str, limit: int = 3) -> List[Dict]:
        """搜索站内相关文章用于内链"""
        if limit <= 0:
            return []
        try:
            items = self._request_json(
                "get",
                f"{self.wp_api}/posts",
                params={
                    "search": focus_keyword,
                    "per_page": min(10, max(limit * 2, 3)),
                    "status": "publish",
                    "_fields": "id,slug,link,title",
                },
                expected_status=(200,),
            )
            if not isinstance(items, list):
                return []
            related: List[Dict] = []
            for item in items:
                if item.get("slug") == current_slug:
                    continue
                related.append({
                    "title": item.get("title", {}).get("rendered", ""),
                    "link": item.get("link", ""),
                })
                if len(related) >= limit:
                    break
            return related
        except Exception as exc:
            logger.error("相关文章获取失败: %s", exc)
            return []

    # ── 创建文章 ──

    def create_post(self, payload: Dict) -> Dict:
        """创建 WordPress 文章"""
        return self._request_json(
            "post",
            f"{self.wp_api}/posts",
            json=payload,
            expected_status=(201,),
        )

    # ── 读取文章列表（只读端） ──

    def list_posts(
        self,
        per_page: int = 10,
        page: int = 1,
        status: str = "publish",
        search: str = "",
    ) -> List[WPPost]:
        """获取文章列表"""
        params: Dict[str, Any] = {
            "per_page": per_page,
            "page": page,
            "status": status,
            "_embed": 1,
        }
        if search:
            params["search"] = search

        try:
            items = self._request_json(
                "get",
                f"{self.wp_api}/posts",
                params=params,
                expected_status=(200,),
            )
        except Exception as exc:
            logger.error("文章列表获取失败: %s", exc)
            return []

        if not isinstance(items, list):
            return []
        posts = [self._parse_post(item) for item in items]
        logger.info("获取到 %d 篇文章", len(posts))
        return posts

    # ── 读取单篇文章 ──

    def get_post(self, post_id: int) -> WPPost:
        """按 ID 获取单篇文章"""
        data = self._request_json(
            "get",
            f"{self.wp_api}/posts/{post_id}",
            params={"_embed": 1},
            expected_status=(200,),
        )
        return self._parse_post(data)

    # ── 解析响应 ──

    @staticmethod
    def _parse_post(data: dict) -> WPPost:
        """将 WP JSON 响应转为 WPPost 数据模型"""
        featured_url = None
        embedded = data.get("_embedded", {})
        feat_media = embedded.get("wp:featuredmedia", [])
        if feat_media:
            featured_url = feat_media[0].get("source_url")

        terms = embedded.get("wp:term", [])
        categories = [
            t["name"] for group in terms for t in group
            if t.get("taxonomy") == "category"
        ]
        tags = [
            t["name"] for group in terms for t in group
            if t.get("taxonomy") == "post_tag"
        ]

        raw_content = data.get("content", {}).get("rendered", "")
        content_images = _extract_image_urls(raw_content)

        return WPPost(
            id=data["id"],
            title=_strip_html(data.get("title", {}).get("rendered", "")),
            content=_strip_html(raw_content),
            excerpt=_strip_html(data.get("excerpt", {}).get("rendered", "")),
            slug=data.get("slug", ""),
            link=data.get("link", ""),
            date=data.get("date", ""),
            featured_image_url=featured_url,
            content_image_urls=content_images,
            tags=tags,
            categories=categories,
        )
