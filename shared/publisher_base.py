"""
通用发布器基类
提取各平台 Publisher 的公共逻辑：浏览器管理、截图、Cookie、原创声明、发布按钮等。
各平台继承后只需实现平台特有的逻辑。
"""

import os
import time
from pathlib import Path
from typing import List, Optional

from playwright.sync_api import sync_playwright, Browser, Page, BrowserContext, Locator

from shared.utils.logger import get_logger

__all__ = [
    "BasePublisher",
    "NAV_TIMEOUT",
    "ELEMENT_TIMEOUT",
    "LOGS_DIR",
]

# ─── 通用常量 ───
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
)
DEFAULT_VIEWPORT = {"width": 1280, "height": 800}
NAV_TIMEOUT = 120_000       # 页面导航超时 (ms)
ELEMENT_TIMEOUT = 60_000    # 等待元素超时 (ms)
LOGIN_WAIT_SECONDS = 180    # 登录等待超时 (s)

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"


class BasePublisher:
    """
    各媒体平台发布器的基类。

    子类需要设置的类属性：
        PLATFORM_NAME: str          平台中文名，用于日志
        USER_DATA_DIR: Path         浏览器持久化数据目录
        LOGIN_URL: str              登录页 URL
        PLATFORM_URL: str           平台首页 URL

    子类需要实现的方法：
        _is_logged_in() -> bool     判断是否已登录
    """

    PLATFORM_NAME: str = "未知平台"
    USER_DATA_DIR: Path = Path(".")
    LOGIN_URL: str = ""
    PLATFORM_URL: str = ""

    def __init__(self, headless: bool = False):
        self.headless = headless
        self._pw = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._temp_files: List[str] = []
        self.logger = get_logger(self.__class__.__name__)

    # ────────── 上下文管理 ──────────

    def __enter__(self) -> "BasePublisher":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        self.stop()
        return False

    # ────────── 浏览器生命周期 ──────────

    def start(self) -> None:
        """启动浏览器（持久化上下文，保存完整登录态）"""
        self._pw = sync_playwright().start()
        self.USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._context = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.USER_DATA_DIR),
            channel="msedge",
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
            viewport=DEFAULT_VIEWPORT,
            user_agent=DEFAULT_UA,
        )
        self._context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        if self._context.pages:
            self._page = self._context.pages[0]
        else:
            self._page = self._context.new_page()
        self._page.set_default_navigation_timeout(NAV_TIMEOUT)
        self._page.set_default_timeout(ELEMENT_TIMEOUT)
        self._browser = None
        self.logger.info("浏览器已启动 (headless=%s)", self.headless)

    def stop(self) -> None:
        """关闭浏览器并清理临时文件"""
        for tmp in self._temp_files:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        self._temp_files.clear()
        if self._context:
            try:
                self._context.close()
            except Exception:
                pass
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
        self.logger.info("浏览器已关闭")

    # ────────── 通用工具方法 ──────────

    def _screenshot(self, name: str) -> None:
        """保存页面截图到 logs 目录"""
        LOGS_DIR.mkdir(exist_ok=True)
        try:
            self._page.screenshot(path=str(LOGS_DIR / name), timeout=8000)
        except Exception:
            pass

    def _wait_for_first(self, selectors: List[str],
                        timeout: int = ELEMENT_TIMEOUT) -> Optional[Locator]:
        """轮询等待多个选择器中第一个可见元素"""
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

    def _wait_network_idle(self, timeout_ms: int = 15000) -> None:
        """等待网络空闲（超时后静默继续）"""
        try:
            self._page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:
            pass

    def _scroll_to_bottom(self, steps: int = 5, max_y: int = 1500) -> None:
        """逐步滚动页面到底部，确保懒加载内容可见"""
        page = self._page
        step_y = max_y // steps
        for i in range(1, steps + 1):
            try:
                page.evaluate(f"window.scrollTo(0, {step_y * i})")
                time.sleep(0.3)
            except Exception:
                pass

    def _wait_for_page_ready(self, timeout_ms: int = NAV_TIMEOUT) -> None:
        """
        等待页面完全就绪：网络空闲 + 骨架屏消失。
        大多数平台发布页使用 SPA，需要等待异步加载完成。
        """
        page = self._page
        # 1. 等待网络空闲
        try:
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
            self.logger.info("网络空闲")
        except Exception:
            self.logger.info("网络空闲超时，继续...")

        # 2. 等待骨架屏消失
        try:
            page.wait_for_function(
                """() => {
                    const s = document.querySelectorAll(
                        '[class*="skeleton"],[class*="Skeleton"],'
                        '[class*="loading"],[class*="Loading"],.el-skeleton'
                    );
                    return s.length === 0 || [...s].every(e => e.offsetParent === null);
                }""",
                timeout=15000,
            )
        except Exception:
            pass
        time.sleep(1)

    def _check_publish_success(self, success_urls: Optional[List[str]] = None,
                               success_keywords: Optional[List[str]] = None,
                               failure_keywords: Optional[List[str]] = None,
                               timeout: int = 15) -> bool:
        """
        发布后检测是否成功。

        Args:
            success_urls: URL 中包含这些关键词表示成功（如 "content/manage"）
            success_keywords: 页面包含这些文字表示成功（如 "发布成功"）
            failure_keywords: 页面包含这些文字表示失败（如 "发布失败"）
            timeout: 最大等待秒数

        Returns:
            True 表示发布成功, False 表示失败或不确定
        """
        page = self._page
        s_urls = success_urls or []
        s_kw = success_keywords or ["发布成功", "发表成功", "已发布", "审核中"]
        f_kw = failure_keywords or ["发布失败", "发表失败", "请重试"]

        for _ in range(timeout):
            time.sleep(1)
            try:
                url = page.url
                # URL 跳转检测
                for kw in s_urls:
                    if kw in url:
                        self.logger.info("发布成功（页面已跳转）")
                        return True

                # 页面文字检测
                body = page.text_content("body", timeout=3000) or ""
                for kw in f_kw:
                    if kw in body:
                        self.logger.warning("检测到失败关键词: %s", kw)
                        return False
                for kw in s_kw:
                    if kw in body:
                        self.logger.info("发布成功（检测到: %s）", kw)
                        return True
            except Exception:
                pass

        self.logger.warning("发布结果未确认（超时）")
        return False

    # ────────── 登录通用逻辑 ──────────

    def _wait_for_login(self, timeout_seconds: int = LOGIN_WAIT_SECONDS):
        """等待用户手动登录（扫码等），超时抛出异常"""
        self.logger.info("等待登录...（请在浏览器中完成登录）")
        for i in range(timeout_seconds // 2):
            time.sleep(2)
            if self._is_logged_in():
                self.logger.info("登录成功")
                return True
            if i % 15 == 0 and i > 0:
                self.logger.info("等待登录中... (%ds)", i * 2)
        self.logger.error("登录超时（%d秒）", timeout_seconds)
        return False

    def _is_logged_in(self) -> bool:
        """判断是否已登录（子类必须实现）"""
        raise NotImplementedError

    # ────────── 原创声明（通用多策略） ──────────

    def _declare_original(self) -> None:
        """
        勾选原创声明（通用多策略方法）：
        1. 展开"更多设置"等折叠区域
        2. 滚动页面
        3. Playwright 原生查找"原创声明"开关
        4. JS 深度搜索"原创"并点击关联控件
        5. CSS 兜底选择器
        """
        page = self._page
        self.logger.info("尝试勾选原创声明...")

        # Step 1: 展开折叠区域
        self._expand_more_options()

        # Step 2: 滚动页面
        self._scroll_to_bottom()

        # Step 3: Playwright 原生查找"原创声明"/"原创"开关
        for label_text in ["原创声明", "原创"]:
            try:
                label = page.get_by_text(label_text, exact=False)
                if label.count() == 0 or not label.first.is_visible():
                    continue
                # 在文本附近查找开关控件
                for depth in range(1, 5):
                    xpath = "/".join([".."] * depth)
                    ancestor = label.first.locator(f"xpath={xpath}")
                    switch = ancestor.locator(
                        '[role="switch"], [role="checkbox"], '
                        '[class*="switch"], [class*="Switch"], '
                        '[class*="toggle"], [class*="Toggle"], '
                        'input[type="checkbox"]'
                    )
                    if switch.count() > 0 and switch.first.is_visible():
                        checked = switch.first.get_attribute("aria-checked")
                        if checked == "true":
                            self.logger.info("原创已开启")
                            return
                        switch.first.click(timeout=5000)
                        self.logger.info("已开启原创声明")
                        time.sleep(1)
                        return
            except Exception:
                continue

        # Step 4: JS 深度搜索
        try:
            result = page.evaluate("""() => {
                const walker = document.createTreeWalker(
                    document.body, NodeFilter.SHOW_TEXT, null, false
                );
                while (walker.nextNode()) {
                    const text = walker.currentNode.textContent.trim();
                    if (!text.includes('原创')) continue;
                    if (text.length > 50) continue;
                    const el = walker.currentNode.parentElement;
                    if (!el || el.offsetParent === null) continue;
                    let cur = el;
                    for (let depth = 0; depth < 6 && cur && cur !== document.body; depth++) {
                        const controls = cur.querySelectorAll(
                            'input[type="checkbox"], input[type="radio"], ' +
                            '[role="switch"], [role="checkbox"], ' +
                            '[class*="switch"], [class*="Switch"], ' +
                            '[class*="toggle"], [class*="Toggle"], ' +
                            '[class*="check"], [class*="Check"]'
                        );
                        for (const ctrl of controls) {
                            const rect = ctrl.getBoundingClientRect();
                            if (rect.width === 0 || rect.height === 0) continue;
                            const checked = ctrl.getAttribute('aria-checked') || String(ctrl.checked);
                            if (checked === 'true') return {status: 'already_checked'};
                            ctrl.click();
                            return {status: 'clicked'};
                        }
                        cur = cur.parentElement;
                    }
                    el.click();
                    return {status: 'clicked_text'};
                }
                return {status: 'not_found'};
            }""")
            status = result.get("status", "not_found")
            if status == "already_checked":
                self.logger.info("原创已开启")
                return
            elif status in ("clicked", "clicked_text"):
                self.logger.info("已开启原创声明")
                time.sleep(1)
                return
        except Exception:
            pass

        # Step 5: CSS 兜底
        for sel in ['[class*="original"]', '[class*="Original"]',
                    'label:has-text("原创")', '[class*="yuanchuang"]']:
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click()
                    self.logger.info("已开启原创声明 (选择器)")
                    time.sleep(1)
                    return
            except Exception:
                continue

        self.logger.warning("未找到原创声明选项")

    def _expand_more_options(self) -> None:
        """展开"更多设置"/"高级设置"等折叠区域"""
        page = self._page
        for txt in ["更多设置", "更多选项", "高级设置", "展开更多", "更多配置", "高级选项"]:
            try:
                loc = page.get_by_text(txt, exact=False)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click()
                    self.logger.info("已展开: %s", txt)
                    time.sleep(2)
                    return
            except Exception:
                continue

    # ────────── 发布按钮通用逻辑 ──────────

    def _click_publish_button(self, button_texts: Optional[List[str]] = None):
        """点击发布/发表/投稿按钮"""
        page = self._page
        texts = button_texts or ["发布", "发表", "投稿", "提交"]
        self.logger.info("点击发布按钮...")

        for txt in texts:
            try:
                btn = page.get_by_role("button", name=txt)
                if btn.count() > 0 and btn.first.is_visible() and btn.first.is_enabled():
                    btn.first.click()
                    self.logger.info("已点击'%s'按钮", txt)
                    return True
            except Exception:
                continue

        # JS 兜底
        try:
            clicked = page.evaluate("""(texts) => {
                const btns = document.querySelectorAll('button, [role="button"]');
                for (const btn of btns) {
                    const txt = btn.textContent.trim();
                    for (const t of texts) {
                        if (txt.includes(t) && btn.offsetParent !== null && !btn.disabled) {
                            btn.click();
                            return txt;
                        }
                    }
                }
                return null;
            }""", texts)
            if clicked:
                self.logger.info("已点击'%s'按钮", clicked)
                return True
        except Exception:
            pass

        self.logger.warning("未找到发布按钮")
        self._screenshot(f"{self.PLATFORM_NAME}_no_publish_btn.png")
        return False

    # ────────── 静态工具 ──────────

    @staticmethod
    def guess_image_suffix(url: str, content_type: str = "") -> str:
        """根据 URL 或 Content-Type 推断图片后缀"""
        low = url.lower()
        if "png" in low or "png" in content_type:
            return ".png"
        if "webp" in low or "webp" in content_type:
            return ".webp"
        if "gif" in low:
            return ".gif"
        return ".jpg"
