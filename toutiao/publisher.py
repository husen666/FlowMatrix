"""
今日头条（头条号）自动发布模块
使用 Playwright 自动化浏览器，在头条号后台发布文章
核心策略：networkidle + skeleton消失检测 + 轮询选择器 + JS兜底
"""

import os
import tempfile
import time
from pathlib import Path
from typing import List, Optional

import requests as req
from playwright.sync_api import sync_playwright, Browser, Page, BrowserContext, Locator

from shared.config import get_settings
from shared.utils.exceptions import ToutiaoPublishError, ToutiaoLoginTimeoutError
from shared.utils.logger import get_logger
from shared.llm.toutiao import ToutiaoContent

settings = get_settings()

logger = get_logger("toutiao_publisher")

# ── 头条号后台 URL ──
PLATFORM_URL = "https://mp.toutiao.com"
LOGIN_URL = f"{PLATFORM_URL}/auth/page/login"
ARTICLE_URL = f"{PLATFORM_URL}/profile_v4/graphic/publish"

COOKIES_FILE = Path(__file__).resolve().parent / "data" / "toutiao_cookies.json"
LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"

# 超时配置（毫秒）
NAV_TIMEOUT = 120_000
ELEMENT_TIMEOUT = 60_000


class ToutiaoPublisher:
    """今日头条自动发布器，支持 with 语句"""

    def __init__(self, headless: bool = False):
        self.headless = headless
        self._pw = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._temp_files: List[str] = []

    # ────────── 上下文管理器 ──────────

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False

    # ────────── 生命周期 ──────────

    def start(self):
        """启动浏览器（使用系统 Edge）"""
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            channel="msedge",
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx_opts = {
            "viewport": {"width": 1280, "height": 800},
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
        logger.info("浏览器已关闭，临时文件已清理")

    # ────────── 工具方法 ──────────

    def _wait_for_first(self, selectors: List[str], timeout: int = ELEMENT_TIMEOUT) -> Optional[Locator]:
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

    def _screenshot(self, name: str):
        """快速截图"""
        LOGS_DIR.mkdir(exist_ok=True)
        try:
            self._page.screenshot(path=str(LOGS_DIR / name), timeout=8000)
            logger.debug("截图: %s", name)
        except Exception:
            pass

    # ────────── 登录 ──────────

    def login(self):
        """登录头条号后台"""
        page = self._page

        if settings.toutiao_cookie and not COOKIES_FILE.exists():
            self._set_cookies_from_string(settings.toutiao_cookie)
            logger.info("通过 .env COOKIE 设置登录态")

        logger.info("打开头条号后台...")
        page.goto(PLATFORM_URL, wait_until="commit")
        time.sleep(5)

        if self._is_logged_in():
            logger.info("已登录")
            self._save_cookies()
            return

        logger.info("未登录，请在浏览器中扫码或手动登录...")
        page.goto(LOGIN_URL, wait_until="commit")
        time.sleep(3)

        for i in range(60):
            time.sleep(2)
            if self._is_logged_in():
                logger.info("登录成功！")
                self._save_cookies()
                return
            if i % 10 == 0 and i > 0:
                logger.info("等待登录... (%ds)", i * 2)

        raise ToutiaoLoginTimeoutError("头条号登录超时（120秒）")

    def _is_logged_in(self) -> bool:
        url = self._page.url
        if "/login" in url or "/auth/" in url:
            return False
        try:
            body_text = self._page.evaluate("document.body.innerText")
            indicators = ["发布", "内容管理", "数据", "首页", "作品管理"]
            return any(kw in body_text for kw in indicators)
        except Exception:
            return "/login" not in url and "/auth/" not in url

    def _set_cookies_from_string(self, cookie_str: str):
        cookies = []
        for pair in cookie_str.split(";"):
            pair = pair.strip()
            if "=" in pair:
                name, value = pair.split("=", 1)
                cookies.append({
                    "name": name.strip(),
                    "value": value.strip(),
                    "domain": ".toutiao.com",
                    "path": "/",
                })
        if cookies:
            self._context.add_cookies(cookies)

    def _save_cookies(self):
        COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._context.storage_state(path=str(COOKIES_FILE))
        logger.info("Cookies 已保存")

    # ────────── 等待发布页加载 ──────────

    def _wait_for_publish_page(self):
        """等待文章发布页完整加载"""
        page = self._page
        logger.info("等待发布页面加载...")

        try:
            page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT)
            logger.info("网络空闲")
        except Exception:
            logger.warning("networkidle 超时，继续...")

        try:
            page.wait_for_function(
                "() => {"
                "  const s = document.querySelectorAll("
                "    '[class*=\"skeleton\"],[class*=\"Skeleton\"],[class*=\"loading\"],.el-skeleton'"
                "  );"
                "  return s.length === 0 || [...s].every(e => e.offsetParent === null);"
                "}",
                timeout=30000,
            )
        except Exception:
            pass

        time.sleep(3)
        self._screenshot("toutiao_page_ready.png")

    # ────────── 发布文章 ──────────

    def publish(self, content: ToutiaoContent) -> bool:
        """在头条号后台发布文章"""
        page = self._page
        logger.info("开始发布: %s", content.title)

        try:
            # 1. 打开发布页
            logger.info("打开文章发布页面...")
            page.goto(ARTICLE_URL, wait_until="commit")
            self._wait_for_publish_page()

            # 2. 填写标题
            self._fill_title(content.title[:30])

            # 3. 填写正文
            self._fill_body(content.body)

            # 4. 上传封面（如果有）
            if content.cover_urls:
                self._upload_cover(content.cover_urls)

            self._screenshot("toutiao_before_publish.png")
            time.sleep(2)

            # 5. 点击发布
            self._click_publish()
            time.sleep(5)

            # 6. 检查结果
            success = self._check_publish_success()
            if success:
                logger.info("文章发布成功！")
            else:
                logger.warning("发布结果不确定，请手动检查")
                self._screenshot("toutiao_result.png")
            return success

        except ToutiaoPublishError:
            self._screenshot("toutiao_error.png")
            raise
        except Exception as e:
            self._screenshot("toutiao_error.png")
            raise ToutiaoPublishError(f"发布异常: {e}") from e

    def _fill_title(self, title: str):
        """填写文章标题"""
        page = self._page
        logger.info("填写标题: %s", title)

        title_selectors = [
            "input[placeholder*='标题']",
            "input[placeholder*='请输入']",
            "textarea[placeholder*='标题']",
            ".title-input input",
            ".article-title input",
            "[class*='title'] input",
            "[class*='Title'] input",
            "input[type='text']",
        ]
        loc = self._wait_for_first(title_selectors, timeout=ELEMENT_TIMEOUT)
        if loc:
            loc.first.click()
            loc.first.fill(title)
            logger.info("标题已填写")
            return

        # contenteditable 标题
        ce_selectors = [
            "[contenteditable='true'][class*='title']",
            "[contenteditable='true'][class*='Title']",
        ]
        loc = self._wait_for_first(ce_selectors, timeout=5000)
        if loc:
            loc.first.click()
            loc.first.fill(title)
            logger.info("标题已填写 (contenteditable)")
            return

        # JS 兜底
        try:
            handle = page.evaluate_handle(
                "() => {"
                "  for (const inp of document.querySelectorAll('input,textarea')) {"
                "    const ph = inp.placeholder || '';"
                "    if (ph.includes('标题') || ph.includes('请输入')) {"
                "      if (inp.offsetParent !== null) return inp;"
                "    }"
                "  }"
                "  for (const inp of document.querySelectorAll('input')) {"
                "    const t = inp.type || 'text';"
                "    if (['file','hidden','search','checkbox','radio'].includes(t)) continue;"
                "    if (inp.offsetParent !== null) return inp;"
                "  }"
                "  return null;"
                "}"
            )
            el = handle.as_element()
            if el:
                el.click()
                el.fill(title)
                logger.info("标题已填写 (JS)")
                return
        except Exception:
            pass

        self._screenshot("toutiao_title_not_found.png")
        raise ToutiaoPublishError("未找到标题输入框")

    def _fill_body(self, text: str):
        """填写文章正文"""
        page = self._page
        logger.info("填写正文 (%d 字)", len(text))

        body_selectors = [
            ".ProseMirror",
            ".ql-editor",
            "[contenteditable='true'][class*='editor']",
            "[contenteditable='true'][class*='Editor']",
            "[contenteditable='true'][class*='content']",
            ".bf-content [contenteditable='true']",
            ".w-e-text-container [contenteditable='true']",
            "[contenteditable='true']",
            ".el-textarea__inner",
            "textarea",
        ]
        loc = self._wait_for_first(body_selectors, timeout=ELEMENT_TIMEOUT)
        if loc:
            # 跳过标题区域的 contenteditable，优先找编辑器
            all_count = loc.count()
            target = loc.first
            if all_count >= 2:
                # 取最后一个（正文通常在标题下方）
                for i in range(all_count):
                    el = loc.nth(i)
                    cls = el.get_attribute("class") or ""
                    if "editor" in cls.lower() or "prosemirror" in cls.lower() or "ql-editor" in cls.lower():
                        target = el
                        break
                else:
                    target = loc.nth(all_count - 1)
            target.click()
            target.fill(text)
            logger.info("正文已填写")
            return

        self._screenshot("toutiao_body_not_found.png")
        raise ToutiaoPublishError("未找到正文编辑器")

    def _upload_cover(self, cover_urls: list):
        """上传封面图"""
        page = self._page
        logger.info("准备上传封面 (%d 张)", len(cover_urls))

        local_files = []
        for src in cover_urls[:3]:
            src_str = str(src)
            if src_str.startswith("http://") or src_str.startswith("https://"):
                try:
                    resp = req.get(src_str, timeout=30)
                    resp.raise_for_status()
                    suffix = ".jpg"
                    ct = resp.headers.get("content-type", "")
                    if "png" in ct:
                        suffix = ".png"
                    elif "webp" in ct:
                        suffix = ".webp"
                    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
                    tmp.write(resp.content)
                    tmp.close()
                    self._temp_files.append(tmp.name)
                    local_files.append(tmp.name)
                except Exception as e:
                    logger.warning("封面下载失败: %s - %s", src_str, e)
            else:
                p = Path(src_str)
                if p.exists():
                    local_files.append(str(p))

        if not local_files:
            logger.warning("无封面可上传")
            return

        # 查找封面上传控件
        try:
            page.wait_for_selector("input[type='file']", state="attached", timeout=15000)
            all_inputs = page.locator("input[type='file']")
            file_input = None
            for i in range(all_inputs.count()):
                inp = all_inputs.nth(i)
                accept = inp.get_attribute("accept") or ""
                if "image" in accept or "jpg" in accept or "png" in accept:
                    file_input = inp
                    break
            if not file_input and all_inputs.count() > 0:
                file_input = all_inputs.first

            if file_input:
                file_input.set_input_files(local_files[0])
                logger.info("封面已上传")
                time.sleep(3)
        except Exception as e:
            logger.warning("封面上传失败: %s", e)

    def _click_publish(self):
        """点击发布按钮"""
        page = self._page
        logger.info("点击发布...")

        try:
            page.keyboard.press("Escape")
            time.sleep(0.3)
            page.mouse.click(10, 10)
            time.sleep(0.5)
        except Exception:
            pass

        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(0.5)
        except Exception:
            pass

        publish_texts = ["发布", "发表", "发布文章"]

        # 策略 1：JS 定位
        for btn_text in publish_texts:
            try:
                js_code = f"""() => {{
                    const btns = [...document.querySelectorAll('button')];
                    const candidates = btns.filter(b => {{
                        const txt = b.textContent.trim();
                        return txt === '{btn_text}' && b.offsetParent !== null && !b.disabled;
                    }});
                    if (candidates.length === 0) return {{found: 0}};
                    let best = candidates[0];
                    let bestY = best.getBoundingClientRect().top;
                    for (const c of candidates) {{
                        const y = c.getBoundingClientRect().top;
                        if (y > bestY) {{ best = c; bestY = y; }}
                    }}
                    const r = best.getBoundingClientRect();
                    return {{found: candidates.length, x: r.x + r.width/2, y: r.y + r.height/2}};
                }}"""
                result = page.evaluate(js_code)
                if result.get("found", 0) > 0:
                    page.mouse.click(result["x"], result["y"])
                    logger.info("已点击'%s'按钮", btn_text)
                    time.sleep(3)
                    self._handle_publish_dialog()
                    return
            except Exception:
                pass

        # 策略 2：Playwright role
        for btn_text in publish_texts:
            try:
                loc = page.get_by_role("button", name=btn_text, exact=True)
                if loc.count() > 0:
                    target = loc.last if loc.count() > 1 else loc.first
                    if target.is_visible():
                        target.scroll_into_view_if_needed()
                        target.click(force=True)
                        logger.info("已点击'%s'按钮 (role)", btn_text)
                        time.sleep(3)
                        self._handle_publish_dialog()
                        return
            except Exception:
                pass

        # 策略 3：dispatchEvent
        for btn_text in publish_texts:
            try:
                js_dispatch = f"""() => {{
                    const btns = [...document.querySelectorAll('button')];
                    const target = btns.filter(b => b.textContent.trim() === '{btn_text}' && b.offsetParent !== null)
                        .sort((a,b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top)[0];
                    if (!target) return false;
                    ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(evtName => {{
                        target.dispatchEvent(new MouseEvent(evtName, {{bubbles:true, cancelable:true, view:window}}));
                    }});
                    return true;
                }}"""
                if page.evaluate(js_dispatch):
                    logger.info("已触发'%s'按钮事件链", btn_text)
                    time.sleep(3)
                    self._handle_publish_dialog()
                    return
            except Exception:
                pass

        self._screenshot("toutiao_btn_not_found.png")
        raise ToutiaoPublishError("未找到发布按钮")

    def _handle_publish_dialog(self):
        """处理发布后可能出现的确认对话框"""
        page = self._page
        time.sleep(1)

        dialog_btns = [
            "text=确认发布",
            "text=确认",
            "text=确定",
            "text=立即发布",
        ]
        for sel in dialog_btns:
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click()
                    logger.info("已确认对话框: %s", sel)
                    time.sleep(2)
                    return
            except Exception:
                continue

    def _check_publish_success(self) -> bool:
        """多策略检测发布是否成功"""
        page = self._page

        for wait_sec in (2, 3, 5):
            time.sleep(wait_sec)
            url = page.url.lower()

            if "publish" not in url and "graphic" not in url:
                logger.info("发布成功（页面已跳转: %s）", page.url)
                return True

            try:
                body_text = page.evaluate("document.body.innerText")
                success_keywords = [
                    "发布成功", "已发布", "文章发布成功",
                    "作品管理", "内容管理",
                ]
                for keyword in success_keywords:
                    if keyword in body_text:
                        logger.info("发布成功（检测到: %s）", keyword)
                        return True
            except Exception:
                pass

        self._screenshot("toutiao_publish_uncertain.png")
        return False

    # ────────── 诊断 ──────────

    def diagnose(self):
        """诊断发布页面元素"""
        page = self._page
        page.goto(ARTICLE_URL, wait_until="commit")
        self._wait_for_publish_page()

        print("\n" + "=" * 60)
        print("  头条号发布页诊断报告")
        print("=" * 60)
        print(f"  URL: {page.url}")
        print(f"  HTML: {len(page.content()):,} 字符")

        report = page.evaluate(
            "() => {"
            "  const r = {inputs:[], ces:[], buttons:[], files:[]};"
            "  document.querySelectorAll('input').forEach((el,i) => {"
            "    const b = el.getBoundingClientRect();"
            "    r.inputs.push({i, type:el.type, ph:el.placeholder,"
            "      cls:(el.className||'').slice(0,60), vis:b.width>0&&b.height>0});"
            "  });"
            "  document.querySelectorAll('[contenteditable]').forEach((el,i) => {"
            "    const b = el.getBoundingClientRect();"
            "    r.ces.push({i, tag:el.tagName, cls:(el.className||'').slice(0,60),"
            "      ph:el.getAttribute('placeholder')||'', vis:b.width>0&&b.height>0});"
            "  });"
            "  document.querySelectorAll('button').forEach((el,i) => {"
            "    const t = el.textContent.trim().slice(0,30);"
            "    if(t) r.buttons.push({i, text:t, disabled:el.disabled});"
            "  });"
            "  document.querySelectorAll(\"input[type='file']\").forEach((el,i) => {"
            "    r.files.push({i, accept:el.accept});"
            "  });"
            "  return r;"
            "}"
        )

        for key, label in [("inputs", "Input"), ("ces", "ContentEditable"),
                           ("buttons", "Button"), ("files", "FileInput")]:
            items = report.get(key, [])
            print(f"\n  [{label}] ({len(items)} 个)")
            for item in items:
                print(f"    {item}")

        self._screenshot("toutiao_diagnose.png")
        print(f"\n  截图已保存: logs/toutiao_diagnose.png")
        print("=" * 60)


# ────────── 便捷函数 ──────────

def publish_toutiao_article(content: ToutiaoContent, headless: bool = False) -> bool:
    """一键发布今日头条文章"""
    with ToutiaoPublisher(headless=headless) as pub:
        pub.login()
        time.sleep(settings.toutiao_publish_delay)
        return pub.publish(content)


def diagnose_toutiao_page():
    """诊断发布页面（debug 命令入口）"""
    with ToutiaoPublisher(headless=False) as pub:
        pub.login()
        pub.diagnose()
        print("\n浏览器保持 15 秒，可手动检查...")
        time.sleep(15)

