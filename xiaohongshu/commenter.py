"""
小红书热门笔记评论引流模块
使用 Playwright 自动化浏览器：
  1. 按关键词搜索热门笔记
  2. 提取笔记内容
  3. 用 LLM 生成有价值的引流评论
  4. 自动发布评论

核心策略：搜索 → 提取 → AI 生成 → 评论，模拟真人操作节奏
"""

import json
import time
from pathlib import Path
from typing import List, Optional, Dict

from playwright.sync_api import sync_playwright, Browser, Page, BrowserContext, Locator

from shared.config import get_settings
from shared.utils.exceptions import CommentError, LoginTimeoutError
from shared.utils.logger import get_logger

settings = get_settings()
logger = get_logger("xhs_commenter")

XHS_URL = "https://www.xiaohongshu.com"
SEARCH_URL = "https://www.xiaohongshu.com/search_result"
EXPLORE_URL = "https://www.xiaohongshu.com/explore"

COOKIES_FILE = Path(__file__).resolve().parent / "data" / "xhs_cookies.json"
LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"

# 超时配置（毫秒）
NAV_TIMEOUT = 120_000
ELEMENT_TIMEOUT = 60_000


class XHSCommenter:
    """小红书评论引流器"""

    def __init__(self, headless: bool = False):
        self.headless = headless
        self._pw = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    # ────────── 上下文管理器 ──────────

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False

    # ────────── 生命周期 ──────────

    def start(self):
        """启动浏览器"""
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            channel="msedge",
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx_opts = {
            "viewport": {"width": 1280, "height": 900},
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
            ),
        }
        if COOKIES_FILE.exists():
            ctx_opts["storage_state"] = str(COOKIES_FILE)
            logger.info("从本地加载 cookies")

        self._context = self._browser.new_context(**ctx_opts)
        self._context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        self._page = self._context.new_page()
        self._page.set_default_navigation_timeout(NAV_TIMEOUT)
        self._page.set_default_timeout(ELEMENT_TIMEOUT)
        logger.info("浏览器已启动 (headless=%s)", self.headless)

    def stop(self):
        """关闭浏览器"""
        if self._context:
            try:
                self._context.close()
            except Exception:
                pass
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
        logger.info("浏览器已关闭")

    # ────────── 工具方法 ──────────

    def _screenshot(self, name: str):
        LOGS_DIR.mkdir(exist_ok=True)
        try:
            self._page.screenshot(path=str(LOGS_DIR / name), timeout=8000)
            logger.debug("截图: %s", name)
        except Exception:
            pass

    def _wait_for_first(self, selectors: List[str], timeout: int = ELEMENT_TIMEOUT) -> Optional[Locator]:
        page = self._page
        deadline = time.time() + timeout / 1000
        while time.time() < deadline:
            for sel in selectors:
                try:
                    loc = page.locator(sel)
                    if loc.count() > 0 and loc.first.is_visible():
                        return loc
                except Exception:
                    pass
            time.sleep(0.5)
        return None

    # ────────── 登录 ──────────

    def login(self):
        """登录小红书（使用主站 cookies，非创作者中心）"""
        page = self._page

        if settings.xhs_cookie and not COOKIES_FILE.exists():
            self._set_cookies_from_string(settings.xhs_cookie)
            logger.info("通过 .env COOKIE 设置登录态")

        logger.info("打开小红书主站...")
        page.goto(XHS_URL, wait_until="commit")
        time.sleep(5)

        if self._is_logged_in():
            logger.info("已登录")
            self._save_cookies()
            return

        logger.info("未登录，请在浏览器中扫码...")
        for i in range(60):
            time.sleep(2)
            if self._is_logged_in():
                logger.info("扫码登录成功！")
                self._save_cookies()
                return
            if i % 10 == 0 and i > 0:
                logger.info("等待扫码... (%ds)", i * 2)

        raise LoginTimeoutError("登录超时（120秒）")

    def _is_logged_in(self) -> bool:
        """检测是否已登录（主站通过检查用户头像元素）"""
        page = self._page
        try:
            # 主站登录后通常会显示用户头像
            login_indicators = [
                ".user-avatar",
                "[class*='avatar']",
                ".sidebar-user",
                "a[href*='/user/profile']",
            ]
            for sel in login_indicators:
                try:
                    loc = page.locator(sel)
                    if loc.count() > 0 and loc.first.is_visible():
                        return True
                except Exception:
                    pass

            # 检查登录弹窗是否存在（存在说明未登录）
            login_modal_selectors = [
                "[class*='login-modal']",
                "[class*='LoginModal']",
                "text=登录",
            ]
            for sel in login_modal_selectors:
                try:
                    loc = page.locator(sel)
                    if loc.count() > 0 and loc.first.is_visible():
                        return False
                except Exception:
                    pass

            # 兜底：检查 cookie 中是否有关键认证信息
            cookies = self._context.cookies()
            for c in cookies:
                if c["name"] in ("web_session", "galaxy_creator_session_id", "customer-sso-sid"):
                    return True
        except Exception:
            pass
        # 如果 cookie 文件存在且已加载，假设已登录
        return COOKIES_FILE.exists()

    def _set_cookies_from_string(self, cookie_str: str):
        cookies = []
        for pair in cookie_str.split(";"):
            pair = pair.strip()
            if "=" in pair:
                name, value = pair.split("=", 1)
                cookies.append({
                    "name": name.strip(),
                    "value": value.strip(),
                    "domain": ".xiaohongshu.com",
                    "path": "/",
                })
        if cookies:
            self._context.add_cookies(cookies)

    def _save_cookies(self):
        COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._context.storage_state(path=str(COOKIES_FILE))
        logger.info("Cookies 已保存")

    # ────────── 搜索笔记 ──────────

    def search_notes(self, keyword: str, max_notes: int = 10, sort: str = "general") -> List[Dict]:
        """
        搜索小红书笔记，返回笔记列表。

        Args:
            keyword: 搜索关键词
            max_notes: 最多返回几条笔记
            sort: 排序方式（general=综合, hot=最热, new=最新）

        Returns:
            [{"note_id": ..., "title": ..., "url": ..., "author": ..., "likes": ...}, ...]
        """
        page = self._page
        logger.info("搜索笔记: keyword=%s, max=%d, sort=%s", keyword, max_notes, sort)

        # 构造搜索 URL
        sort_map = {"general": "general", "hot": "popularity_descending", "new": "time_descending"}
        sort_param = sort_map.get(sort, "general")
        search_url = f"{SEARCH_URL}?keyword={keyword}&source=web_search_result_notes&type=1&sort={sort_param}"

        page.goto(search_url, wait_until="commit")
        time.sleep(5)

        # 等待搜索结果加载
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            logger.warning("搜索页 networkidle 超时，继续...")

        time.sleep(3)
        self._screenshot("search_result.png")

        # 提取笔记卡片信息
        notes = self._extract_note_cards(max_notes)
        logger.info("搜索到 %d 条笔记", len(notes))
        return notes

    def _extract_note_cards(self, max_notes: int) -> List[Dict]:
        """从搜索结果页提取笔记卡片信息"""
        page = self._page

        # 向下滚动加载更多内容
        for _ in range(3):
            page.evaluate("window.scrollBy(0, 800)")
            time.sleep(1.5)

        # 用 JS 提取笔记卡片数据
        js_extract = """(maxNotes) => {
            const results = [];
            // 搜索结果中的笔记卡片
            const cards = document.querySelectorAll('[class*="note-item"], section[class*="note"], a[class*="cover"], [class*="feeds-page"] section');
            const links = document.querySelectorAll('a[href*="/explore/"], a[href*="/search_result/"]');

            // 收集所有笔记链接
            const seen = new Set();
            const allLinks = document.querySelectorAll('a');

            for (const a of allLinks) {
                if (results.length >= maxNotes) break;
                const href = a.href || '';
                // 匹配笔记链接格式
                const match = href.match(/\\/explore\\/([a-f0-9]{24})/);
                if (!match) continue;

                const noteId = match[1];
                if (seen.has(noteId)) continue;
                seen.add(noteId);

                // 获取标题（卡片上的文字）
                let title = '';
                // 查找卡片内的标题文字
                const titleEl = a.querySelector('[class*="title"], [class*="desc"], span, p');
                if (titleEl) {
                    title = titleEl.textContent.trim();
                }
                if (!title) {
                    // 用邻近元素的文字
                    const parent = a.closest('section, [class*="note"], [class*="card"]');
                    if (parent) {
                        const tEl = parent.querySelector('[class*="title"], [class*="desc"]');
                        if (tEl) title = tEl.textContent.trim();
                    }
                }

                // 获取作者
                let author = '';
                const parent2 = a.closest('section, [class*="note"], [class*="card"]');
                if (parent2) {
                    const authorEl = parent2.querySelector('[class*="author"], [class*="name"], [class*="nick"]');
                    if (authorEl) author = authorEl.textContent.trim();
                }

                // 获取点赞数
                let likes = '';
                if (parent2) {
                    const likeEl = parent2.querySelector('[class*="like"], [class*="count"]');
                    if (likeEl) likes = likeEl.textContent.trim();
                }

                results.push({
                    note_id: noteId,
                    title: title.slice(0, 60),
                    url: 'https://www.xiaohongshu.com/explore/' + noteId,
                    author: author.slice(0, 30),
                    likes: likes,
                });
            }
            return results;
        }"""

        try:
            notes = page.evaluate(js_extract, max_notes)
            return notes or []
        except Exception as e:
            logger.warning("提取笔记卡片失败: %s", e)
            return []

    # ────────── 打开笔记并提取内容 ──────────

    def open_note_and_extract(self, note_url: str) -> Dict:
        """
        打开笔记详情页并提取标题和正文。

        Returns:
            {"title": ..., "body": ..., "author": ..., "url": ...}
        """
        page = self._page
        logger.info("打开笔记: %s", note_url)

        page.goto(note_url, wait_until="commit")
        time.sleep(5)

        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except Exception:
            pass

        time.sleep(3)

        # 提取笔记内容
        js_extract_content = """() => {
            const result = {title: '', body: '', author: ''};

            // 标题
            const titleSelectors = [
                '#detail-title',
                '[class*="title"][class*="note"]',
                '.note-content .title',
                '[class*="noteDetail"] [class*="title"]',
            ];
            for (const sel of titleSelectors) {
                const el = document.querySelector(sel);
                if (el && el.textContent.trim()) {
                    result.title = el.textContent.trim();
                    break;
                }
            }

            // 正文
            const bodySelectors = [
                '#detail-desc',
                '[class*="desc"][class*="note"]',
                '.note-content .desc',
                '[class*="noteDetail"] [class*="desc"]',
                '[class*="note-text"]',
                '[class*="content"] [class*="desc"]',
            ];
            for (const sel of bodySelectors) {
                const el = document.querySelector(sel);
                if (el && el.textContent.trim()) {
                    result.body = el.textContent.trim();
                    break;
                }
            }

            // 如果上面没找到，用更宽泛的搜索
            if (!result.body) {
                const allSpans = document.querySelectorAll('span[class*="desc"], span[class*="note"]');
                const texts = [...allSpans]
                    .map(el => el.textContent.trim())
                    .filter(t => t.length > 20);
                if (texts.length > 0) {
                    result.body = texts.sort((a, b) => b.length - a.length)[0];
                }
            }

            // 作者
            const authorSelectors = [
                '[class*="author"] [class*="name"]',
                '[class*="user-info"] [class*="name"]',
                '.author-wrapper .name',
                '[class*="username"]',
            ];
            for (const sel of authorSelectors) {
                const el = document.querySelector(sel);
                if (el && el.textContent.trim()) {
                    result.author = el.textContent.trim();
                    break;
                }
            }

            return result;
        }"""

        try:
            content = page.evaluate(js_extract_content)
            content["url"] = note_url
            logger.info("提取笔记内容: title=%s, body=%d字",
                        content.get("title", "")[:30],
                        len(content.get("body", "")))
            self._screenshot("note_detail.png")
            return content
        except Exception as e:
            logger.warning("提取笔记内容失败: %s", e)
            self._screenshot("note_extract_error.png")
            return {"title": "", "body": "", "author": "", "url": note_url}

    # ────────── 发布评论 ──────────

    def post_comment(self, comment_text: str) -> bool:
        """
        在当前打开的笔记页面发布评论。
        调用前需确保已调用 open_note_and_extract() 打开了笔记详情页。

        Returns:
            True 发布成功, False 发布失败
        """
        page = self._page
        logger.info("发布评论 (%d字): %s...", len(comment_text), comment_text[:30])

        try:
            # 1. 滚动到评论区
            self._scroll_to_comments()
            time.sleep(2)

            # 2. 找到评论输入框并点击激活
            if not self._activate_comment_input():
                logger.error("未找到评论输入框")
                self._screenshot("comment_input_not_found.png")
                return False

            time.sleep(1)

            # 3. 输入评论内容
            if not self._type_comment(comment_text):
                logger.error("输入评论失败")
                self._screenshot("comment_type_error.png")
                return False

            time.sleep(1)

            # 4. 点击发送
            if not self._click_send_comment():
                logger.error("点击发送失败")
                self._screenshot("comment_send_error.png")
                return False

            time.sleep(3)

            # 5. 检查是否成功
            success = self._check_comment_success(comment_text)
            if success:
                logger.info("评论发布成功！")
            else:
                logger.warning("评论发布结果不确定")
                self._screenshot("comment_result.png")
            return success

        except Exception as e:
            logger.error("评论发布异常: %s", e)
            self._screenshot("comment_error.png")
            return False

    def _scroll_to_comments(self):
        """滚动到评论区域"""
        page = self._page
        logger.debug("滚动到评论区...")

        # 多次小幅滚动，模拟真人
        for _ in range(5):
            page.evaluate("window.scrollBy(0, 300)")
            time.sleep(0.5)

    def _activate_comment_input(self) -> bool:
        """找到并激活评论输入框"""
        page = self._page
        logger.debug("查找评论输入框...")

        # 策略 1：点击评论占位区域激活输入框
        placeholder_selectors = [
            "[class*='comment'] [class*='input']",
            "[class*='comment'] [class*='placeholder']",
            "[class*='reply'] [class*='input']",
            "[placeholder*='说点什么']",
            "[placeholder*='评论']",
            "[placeholder*='留言']",
            "input[class*='comment']",
            "[class*='comment-input']",
            "[class*='commentInput']",
            "[class*='comment'] input",
            "[class*='comment'] textarea",
        ]
        loc = self._wait_for_first(placeholder_selectors, timeout=15000)
        if loc:
            loc.first.click()
            logger.debug("评论输入区已激活 (placeholder)")
            time.sleep(1)
            return True

        # 策略 2：JS 查找评论区相关元素
        try:
            clicked = page.evaluate("""() => {
                // 查找包含"说点什么"等文字的元素
                const keywords = ['说点什么', '评论', '留言', '写评论'];
                const allEls = document.querySelectorAll('*');
                for (const el of allEls) {
                    if (el.children.length > 2) continue;
                    const text = el.textContent.trim();
                    for (const kw of keywords) {
                        if (text.includes(kw) && text.length < 20) {
                            el.click();
                            return text;
                        }
                    }
                }
                return null;
            }""")
            if clicked:
                logger.debug("评论输入区已激活 (JS): %s", clicked)
                time.sleep(1)
                return True
        except Exception:
            pass

        # 策略 3：查找 contenteditable 元素（评论框可能是富文本）
        try:
            ce_selectors = [
                "[class*='comment'] [contenteditable='true']",
                "[contenteditable='true'][class*='reply']",
            ]
            loc = self._wait_for_first(ce_selectors, timeout=5000)
            if loc:
                loc.first.click()
                logger.debug("评论输入区已激活 (contenteditable)")
                time.sleep(1)
                return True
        except Exception:
            pass

        return False

    def _type_comment(self, text: str) -> bool:
        """在已激活的评论框中输入文字"""
        page = self._page
        logger.debug("输入评论文字...")

        # 策略 1：找到当前焦点的输入框（textarea 或 contenteditable）
        input_selectors = [
            "[class*='comment'] textarea:focus",
            "[class*='comment'] input:focus",
            "[class*='comment'] [contenteditable='true']",
            "textarea[class*='comment']",
            "[class*='reply'] textarea",
            "[class*='commentInput'] textarea",
            "[class*='comment-input'] textarea",
            "[contenteditable='true'][class*='comment']",
            "[contenteditable='true'][class*='reply']",
        ]

        for sel in input_selectors:
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click()
                    time.sleep(0.3)
                    # 对于 textarea 用 fill，对于 contenteditable 用 type
                    tag = loc.first.evaluate("el => el.tagName.toLowerCase()")
                    if tag in ("textarea", "input"):
                        loc.first.fill(text)
                    else:
                        loc.first.type(text, delay=50)  # 模拟打字
                    logger.debug("评论已输入 (selector: %s)", sel)
                    return True
            except Exception:
                continue

        # 策略 2：用 keyboard 直接打字（假设输入框已获得焦点）
        try:
            page.keyboard.type(text, delay=50)
            logger.debug("评论已输入 (keyboard)")
            return True
        except Exception as e:
            logger.warning("键盘输入失败: %s", e)

        return False

    def _click_send_comment(self) -> bool:
        """点击发送评论按钮"""
        page = self._page
        logger.debug("点击发送评论...")

        # 策略 1：查找发送按钮
        send_selectors = [
            "[class*='comment'] button[class*='submit']",
            "[class*='comment'] [class*='send']",
            "[class*='comment'] button:has-text('发送')",
            "button:has-text('发送')",
            "[class*='submit'][class*='comment']",
            "[class*='comment'] [class*='btn']",
        ]
        for sel in send_selectors:
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click()
                    logger.debug("已点击发送 (selector: %s)", sel)
                    return True
            except Exception:
                continue

        # 策略 2：JS 查找发送按钮
        try:
            clicked = page.evaluate("""() => {
                const btns = [...document.querySelectorAll('button, [role="button"], [class*="btn"]')];
                for (const b of btns) {
                    const txt = b.textContent.trim();
                    if ((txt === '发送' || txt === '发布') && b.offsetParent !== null) {
                        b.click();
                        return txt;
                    }
                }
                return null;
            }""")
            if clicked:
                logger.debug("已点击发送 (JS): %s", clicked)
                return True
        except Exception:
            pass

        # 策略 3：用快捷键提交（Enter 或 Ctrl+Enter）
        try:
            page.keyboard.press("Enter")
            logger.debug("已按 Enter 发送评论")
            return True
        except Exception:
            pass

        return False

    def _check_comment_success(self, comment_text: str) -> bool:
        """检查评论是否发送成功"""
        page = self._page
        time.sleep(2)

        try:
            body_text = page.evaluate("document.body.innerText")

            # 检查评论文字是否出现在页面中
            if comment_text[:15] in body_text:
                return True

            # 检查是否有错误提示
            error_keywords = ["评论失败", "发送失败", "请稍后再试", "操作频繁", "内容违规"]
            for kw in error_keywords:
                if kw in body_text:
                    logger.warning("评论失败: 检测到 '%s'", kw)
                    return False
        except Exception:
            pass

        # 默认认为成功（无明确错误信号）
        return True

    # ────────── 完整引流流程 ──────────

    def comment_on_hot_notes(
        self,
        keyword: str,
        comment_gen_func,
        max_comments: int = 5,
        comment_delay: int = 30,
        sort: str = "general",
    ) -> List[Dict]:
        """
        完整引流流程：搜索 → 打开 → 生成评论 → 发布评论

        Args:
            keyword: 搜索关键词
            comment_gen_func: 评论生成函数，签名 (note_title, note_body) -> str
            max_comments: 最多评论几条
            comment_delay: 每条评论之间的间隔（秒）
            sort: 搜索排序

        Returns:
            评论结果列表 [{"note_url": ..., "note_title": ..., "comment": ..., "success": bool}, ...]
        """
        results = []

        # 1. 搜索笔记
        notes = self.search_notes(keyword, max_notes=max_comments * 2, sort=sort)
        if not notes:
            logger.warning("未搜索到任何笔记")
            return results

        logger.info("找到 %d 条笔记，准备评论（最多 %d 条）", len(notes), max_comments)

        commented = 0
        for i, note_info in enumerate(notes):
            if commented >= max_comments:
                break

            note_url = note_info.get("url", "")
            if not note_url:
                continue

            logger.info("\n[%d/%d] 处理笔记: %s", commented + 1, max_comments,
                        note_info.get("title", "")[:30] or note_url)

            try:
                # 2. 打开笔记并提取内容
                note_content = self.open_note_and_extract(note_url)
                note_title = note_content.get("title", "")
                note_body = note_content.get("body", "")

                if not note_body:
                    logger.warning("笔记内容为空，跳过")
                    continue

                # 3. 生成评论
                comment = comment_gen_func(note_title, note_body)
                if not comment:
                    logger.warning("评论生成为空，跳过")
                    continue

                # 4. 发布评论
                success = self.post_comment(comment)
                result = {
                    "note_url": note_url,
                    "note_title": note_title,
                    "note_author": note_content.get("author", ""),
                    "comment": comment,
                    "success": success,
                }
                results.append(result)
                commented += 1

                logger.info("评论 %s: %s", "成功" if success else "失败", comment[:40])

                # 5. 间隔等待（防风控）
                if commented < max_comments and i < len(notes) - 1:
                    logger.info("等待 %ds 后继续...", comment_delay)
                    time.sleep(comment_delay)

            except Exception as e:
                logger.warning("处理笔记失败: %s - %s", note_url, e)
                results.append({
                    "note_url": note_url,
                    "note_title": note_info.get("title", ""),
                    "comment": "",
                    "success": False,
                    "error": str(e),
                })

        return results


# ────────── 便捷函数 ──────────

def comment_on_notes(
    keyword: str,
    my_note_title: str,
    my_note_summary: str,
    max_comments: int = 5,
    comment_delay: int = 30,
    sort: str = "general",
    style: str = "professional",
    headless: bool = False,
) -> List[Dict]:
    """
    一键引流：搜索热门笔记并发布引流评论。

    Args:
        keyword: 搜索关键词
        my_note_title: 自己发布的笔记标题
        my_note_summary: 自己笔记的核心内容摘要
        max_comments: 最多评论数
        comment_delay: 评论间隔（秒）
        sort: 搜索排序
        style: 评论风格
        headless: 无头模式

    Returns:
        评论结果列表
    """
    from shared.config import get_settings
    from shared.llm.client import LLMClient
    from shared.llm.xhs import XHSContentGenerator

    settings = get_settings()
    llm = LLMClient(
        api_key=settings.deepseek_api_key,
        base_url=settings.deepseek_base_url,
        model=settings.deepseek_model,
        timeout=settings.request_timeout,
    )
    gen = XHSContentGenerator(llm)

    def gen_comment(note_title: str, note_body: str) -> str:
        return gen.generate_comment(
            note_title=note_title,
            note_body=note_body,
            my_note_title=my_note_title,
            my_note_summary=my_note_summary,
            style=style,
        )

    with XHSCommenter(headless=headless) as commenter:
        commenter.login()
        time.sleep(3)
        results = commenter.comment_on_hot_notes(
            keyword=keyword,
            comment_gen_func=gen_comment,
            max_comments=max_comments,
            comment_delay=comment_delay,
            sort=sort,
        )

    llm.close()
    return results
