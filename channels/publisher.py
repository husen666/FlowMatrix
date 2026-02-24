"""
微信视频号自动发布模块
使用 Playwright 自动化浏览器，在微信视频号创作者中心发布内容
支持图文动态和视频发布两种模式
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
from shared.utils.exceptions import ChannelsLoginTimeoutError, ChannelsPublishError
from shared.utils.logger import get_logger

settings = get_settings()

logger = get_logger("channels_publisher")

# ── 视频号创作者平台 URL ──
PLATFORM_URL = "https://channels.weixin.qq.com/platform"
LOGIN_URL = f"{PLATFORM_URL}/login"
POST_CREATE_URL = f"{PLATFORM_URL}/post/create"

COOKIES_FILE = Path(__file__).resolve().parent / "data" / "channels_cookies.json"
BROWSER_PROFILE_DIR = Path(__file__).resolve().parent / "data" / "browser_profile"
LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"  # code/logs/

# 超时配置（毫秒）
NAV_TIMEOUT = 120_000      # 页面导航超时（含 SPA 加载）
ELEMENT_TIMEOUT = 60_000   # 等待元素出现超时


class ChannelsPublisher:
    """微信视频号自动发布器，支持 with 语句"""

    def __init__(self, headless: bool = False):
        self.headless = headless
        self._pw = None
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
        """启动浏览器（使用系统 Edge + 持久化上下文保持登录态）"""
        BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self._context = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_PROFILE_DIR),
            channel="msedge",
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0"
            ),
        )
        self._context.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
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
        """快速截图（忽略字体加载超时）"""
        LOGS_DIR.mkdir(exist_ok=True)
        filepath = str(LOGS_DIR / name)
        try:
            self._page.screenshot(path=filepath, timeout=8000)
            logger.info("截图已保存: %s", filepath)
        except Exception as e:
            logger.warning("截图失败 %s: %s", name, e)

    # ────────── 登录 ──────────

    def login(self):
        """登录微信视频号创作者中心（微信扫码）"""
        page = self._page

        if settings.channels_cookie and not COOKIES_FILE.exists():
            self._set_cookies_from_string(settings.channels_cookie)
            logger.info("通过 .env COOKIE 设置登录态")

        logger.info("打开视频号创作者中心...")
        page.goto(PLATFORM_URL, wait_until="commit")
        time.sleep(5)

        if self._is_logged_in():
            logger.info("已登录")
            self._save_cookies()
            return

        logger.info("未登录，请在浏览器中用微信扫码...")
        # 视频号平台会自动跳转到登录页，显示二维码
        page.goto(LOGIN_URL, wait_until="commit")
        time.sleep(3)

        # 等待扫码（最多 120 秒）
        for i in range(60):
            time.sleep(2)
            if self._is_logged_in():
                logger.info("扫码登录成功！")
                self._save_cookies()
                return
            if i % 10 == 0 and i > 0:
                logger.info("等待微信扫码... (%ds)", i * 2)

        raise ChannelsLoginTimeoutError("微信视频号登录超时（120秒）")

    def _is_logged_in(self) -> bool:
        """判断是否已登录：URL 不含 login，且页面有创作者特征"""
        url = self._page.url
        if "/login" in url:
            return False
        # 已登录的页面通常包含"发表动态"/"首页"/"创作者"等关键词
        try:
            body_text = self._page.evaluate("document.body.innerText")
            login_indicators = ["发表动态", "首页", "内容管理", "数据中心", "创作者"]
            return any(kw in body_text for kw in login_indicators)
        except Exception:
            return "/login" not in url

    def _set_cookies_from_string(self, cookie_str: str):
        cookies = []
        for pair in cookie_str.split(";"):
            pair = pair.strip()
            if "=" in pair:
                name, value = pair.split("=", 1)
                cookies.append({
                    "name": name.strip(),
                    "value": value.strip(),
                    "domain": ".channels.weixin.qq.com",
                    "path": "/",
                })
        if cookies:
            self._context.add_cookies(cookies)

    def _save_cookies(self):
        COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._context.storage_state(path=str(COOKIES_FILE))
        logger.info("Cookies 已保存")

    # ════════════════════════════════════════════════════════════
    #  图文动态发布
    # ════════════════════════════════════════════════════════════

    def _wait_for_create_page(self):
        """等待发表动态页面完整加载"""
        page = self._page
        logger.info("等待发表页面加载...")

        # 阶段 1：网络空闲
        try:
            page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT)
            logger.info("网络空闲")
        except Exception:
            logger.warning("networkidle 超时，继续...")

        # 阶段 2：skeleton 消失
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
        self._screenshot("channels_page_ready.png")

    def _is_imagetext_create_page(self) -> bool:
        """判断当前页面是否为图文发表页（不是视频发表页，也不是管理列表页）"""
        page = self._page
        url = page.url.lower()
        if "imagetext" in url:
            return True
        if "findernewlifecreate" in url:
            return True
        try:
            body_text = page.evaluate("document.body.innerText.slice(0, 1000)")
            if "视频简述" in body_text or "上传时长" in body_text:
                logger.info("当前是视频发表页，不是图文发表页")
                return False
            if "你还没有发表过图文" in body_text or "图文管理" in body_text:
                logger.info("当前是图文管理页，不是图文发表页")
                return False
        except Exception:
            pass
        for sel in ["[contenteditable='true']", "textarea"]:
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    return True
            except Exception:
                continue
        return False

    def _navigate_to_create(self):
        """导航到图文发表页面"""
        page = self._page
        logger.info("导航到图文发表页面...")

        # 策略 1：直接访问图文发表 URL（包含新旧两种 URL 格式）
        imagetext_urls = [
            f"{PLATFORM_URL}/post/finderNewLifeCreate",
            f"{PLATFORM_URL}/post/imagetext/create",
            f"{PLATFORM_URL}/post/imagetext",
            POST_CREATE_URL,
        ]
        for url in imagetext_urls:
            page.goto(url, wait_until="commit")
            self._wait_for_create_page()
            if self._is_imagetext_create_page():
                logger.info("已到达图文发表页: %s", page.url)
                return

        # 策略 2：通过侧边栏导航
        logger.info("直接 URL 未到图文发表页，尝试侧边栏导航...")
        try:
            sidebar_link = page.get_by_text("图文", exact=True)
            if sidebar_link.count() > 0 and sidebar_link.first.is_visible():
                sidebar_link.first.click()
                time.sleep(3)
        except Exception:
            pass

        # 检查是否需要点击"发表图文"按钮
        for txt in ["发表图文", "发表", "新建图文"]:
            try:
                loc = page.get_by_text(txt, exact=False)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click()
                    logger.info("已点击'%s'进入图文发表页", txt)
                    time.sleep(3)
                    self._wait_for_create_page()
                    if self._is_imagetext_create_page():
                        return
            except Exception:
                continue

        logger.info("导航结束，当前页面: %s", page.url)

    def publish_text_image(self, body: str, image_sources: List[str] = None,
                           title: str = "") -> bool:
        """
        在微信视频号发布图文动态

        Args:
            body: 动态正文内容
            image_sources: 图片来源列表（URL 或本地路径），可为空
            title: 图文标题（新版视频号图文必填）
        """
        page = self._page
        logger.info("开始发布图文动态 (%d字, 标题: %s)", len(body), title or "(无)")

        try:
            # 1. 导航到发表页面
            self._navigate_to_create()

            # 2. 切换到图文模式（如果有 tab 切换）
            self._switch_to_text_image_mode()

            # 3. 上传图片（如果有）
            if image_sources:
                self._upload_images(image_sources)
                time.sleep(3)

            # 4. 填写标题（如果有）
            if title:
                self._fill_title(title)

            # 5. 填写正文
            self._fill_content(body)
            self._screenshot("channels_before_publish.png")

            # 5. 等待上传完成
            if image_sources:
                self._wait_for_upload_done()
            time.sleep(2)

            # 6. 点击发表
            self._click_publish()
            time.sleep(5)

            # 7. 检查结果
            success = self._check_publish_success()
            if success:
                logger.info("图文动态发布成功！")
            else:
                logger.warning("发布结果不确定，请手动检查")
                self._screenshot("channels_result.png")
            return success

        except ChannelsPublishError:
            self._screenshot("channels_error.png")
            raise
        except Exception as e:
            self._screenshot("channels_error.png")
            raise ChannelsPublishError(f"发布异常: {e}") from e

    def _switch_to_text_image_mode(self):
        """如果有视频/图文切换 tab，切换到图文模式
        注意：只匹配 tab/radio 类元素，避免误点击侧边栏导航链接"""
        page = self._page
        logger.info("检查是否需要切换到图文模式...")

        if self._is_imagetext_create_page():
            logger.info("当前已是图文发表页，无需切换")
            return

        # 策略 1：JS 精确匹配 tab/radio 类元素
        try:
            clicked = page.evaluate("""() => {
                const tabSelectors = [
                    '[role="tab"]', '[role="tablist"] *', '[role="radio"]',
                    '[class*="tab"]', '[class*="Tab"]', '[class*="segment"]',
                    '[class*="Segment"]', '[class*="switch"]', '[class*="Switch"]',
                ];
                for (const sel of tabSelectors) {
                    const els = document.querySelectorAll(sel);
                    for (const el of els) {
                        const txt = el.textContent.trim();
                        if ((txt === '图文' || txt === '图文动态') && el.offsetParent !== null) {
                            el.click();
                            return txt;
                        }
                    }
                }
                return null;
            }""")
            if clicked:
                logger.info("已切换到'%s'模式 (tab)", clicked)
                time.sleep(2)
                return
        except Exception:
            pass

        # 策略 2：如果仍在视频页，通过侧边栏导航到图文
        try:
            body_text = page.evaluate("document.body.innerText.slice(0, 1000)")
            if "视频简述" in body_text or "上传时长" in body_text:
                logger.info("当前在视频发表页，需通过侧边栏导航到图文...")
                sidebar_link = page.get_by_text("图文", exact=True)
                if sidebar_link.count() > 0 and sidebar_link.first.is_visible():
                    sidebar_link.first.click()
                    time.sleep(3)
                for txt in ["发表图文", "发表", "新建图文"]:
                    loc = page.get_by_text(txt, exact=False)
                    if loc.count() > 0 and loc.first.is_visible():
                        loc.first.click()
                        logger.info("已点击'%s'进入图文发表页", txt)
                        time.sleep(3)
                        return
        except Exception:
            pass

        logger.info("未找到图文切换 tab，当前可能已是图文模式")

    def _upload_images(self, image_sources: list):
        """上传图片到视频号"""
        page = self._page
        logger.info("准备上传 %d 张图片", len(image_sources))

        # 准备本地文件
        local_files = []
        for src in image_sources:
            src_str = str(src)
            if src_str.startswith("http://") or src_str.startswith("https://"):
                try:
                    resp = req.get(src_str, timeout=30)
                    resp.raise_for_status()
                    suffix = self._guess_suffix(src_str, resp.headers.get("content-type", ""))
                    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
                    tmp.write(resp.content)
                    tmp.close()
                    self._temp_files.append(tmp.name)
                    local_files.append(tmp.name)
                    logger.info("已下载: %s", src_str.split("/")[-1])
                except Exception as e:
                    logger.warning("下载失败，跳过: %s - %s", src_str, e)
            else:
                local_path = Path(src_str)
                if local_path.exists():
                    local_files.append(str(local_path))
                    logger.info("使用本地图片: %s", local_path.name)
                else:
                    logger.warning("本地图片不存在，跳过: %s", src_str)

        if not local_files:
            logger.warning("无图片可上传")
            return

        # 策略 1：等待 input[type='file'] 出现
        logger.info("等待图片上传控件...")
        file_input = self._find_file_input_for_image()

        if file_input:
            try:
                file_input.set_input_files(local_files)
                logger.info("已上传 %d 张图片", len(local_files))
                time.sleep(5)
                return
            except Exception as e:
                logger.warning("set_input_files 失败: %s", e)

        # 策略 2：点击上传区域触发 file input 出现
        logger.info("尝试点击上传区域触发 file input...")
        upload_area_selectors = [
            "[class*='upload']", "[class*='Upload']",
            "[class*='add-image']", "[class*='addImage']",
            "[class*='photo']", "[class*='Photo']",
            "[class*='image-picker']", "[class*='ImagePicker']",
            "text=上传图片", "text=添加图片", "text=选择图片",
        ]
        for sel in upload_area_selectors:
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click()
                    logger.info("已点击上传区域: %s", sel)
                    time.sleep(3)
                    break
            except Exception:
                continue

        # 重新查找 file input
        file_input = self._find_file_input_for_image()
        if file_input:
            try:
                file_input.set_input_files(local_files)
                logger.info("已上传 %d 张图片（点击后）", len(local_files))
                time.sleep(5)
                return
            except Exception as e:
                logger.warning("点击后 set_input_files 仍失败: %s", e)

        # 策略 3：JS 注入 file input
        logger.info("尝试 JS 注入 file input...")
        try:
            page.evaluate("""() => {
                const existing = document.querySelector("input[type='file'][data-injected]");
                if (existing) existing.remove();
                const inp = document.createElement('input');
                inp.type = 'file';
                inp.accept = 'image/*';
                inp.multiple = true;
                inp.setAttribute('data-injected', 'true');
                inp.style.position = 'fixed';
                inp.style.top = '0';
                inp.style.left = '0';
                inp.style.opacity = '0.01';
                document.body.appendChild(inp);
            }""")
            time.sleep(1)
            injected = page.locator("input[data-injected='true']")
            if injected.count() > 0:
                injected.first.set_input_files(local_files)
                logger.info("已通过注入 input 上传 %d 张图片", len(local_files))
                time.sleep(5)
                return
        except Exception as e:
            logger.warning("JS 注入上传失败: %s", e)

        # 所有策略失败，截图诊断
        self._screenshot("channels_upload_failed.png")
        self._diagnose_upload_elements()
        logger.warning("图片上传失败：所有策略均未成功，继续尝试发布纯文本")

    def _find_file_input_for_image(self) -> Optional[Locator]:
        """查找用于上传图片的 file input"""
        page = self._page
        try:
            # 先等 5 秒看 file input 是否出现（不用等完整 60 秒）
            page.wait_for_selector("input[type='file']", state="attached", timeout=10000)
        except Exception:
            # 也尝试隐藏的 file input
            try:
                count = page.locator("input[type='file']").count()
                if count == 0:
                    logger.info("页面上未找到任何 file input")
                    return None
            except Exception:
                return None

        all_inputs = page.locator("input[type='file']")
        count = all_inputs.count()
        logger.info("找到 %d 个 file input", count)

        # 优先找接受图片的
        for i in range(count):
            inp = all_inputs.nth(i)
            try:
                accept = inp.get_attribute("accept") or ""
                logger.info("  file input[%d] accept=%s", i, accept)
                if "image" in accept or "jpg" in accept or "png" in accept:
                    return inp
            except Exception:
                continue

        # 没有明确图片类型的，找没有 accept 或 accept=* 的
        for i in range(count):
            inp = all_inputs.nth(i)
            try:
                accept = inp.get_attribute("accept") or ""
                if not accept or accept == "*" or accept == "*/*":
                    return inp
            except Exception:
                continue

        # 兜底：返回第一个
        if count > 0:
            return all_inputs.first
        return None

    def _diagnose_upload_elements(self):
        """诊断页面上的上传相关元素"""
        page = self._page
        try:
            report = page.evaluate("""() => {
                const r = {fileInputs: [], uploadAreas: [], url: location.href};
                document.querySelectorAll("input[type='file']").forEach((el, i) => {
                    r.fileInputs.push({
                        i, accept: el.accept,
                        display: getComputedStyle(el).display,
                        visibility: getComputedStyle(el).visibility,
                        parentCls: (el.parentElement?.className || '').slice(0, 60)
                    });
                });
                const uploadSelectors = ['[class*="upload"]', '[class*="Upload"]',
                    '[class*="add"]', '[class*="photo"]', '[class*="image"]'];
                for (const sel of uploadSelectors) {
                    document.querySelectorAll(sel).forEach(el => {
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {
                            r.uploadAreas.push({
                                tag: el.tagName, cls: (el.className || '').slice(0, 80),
                                text: el.textContent.trim().slice(0, 30),
                                w: Math.round(rect.width), h: Math.round(rect.height)
                            });
                        }
                    });
                }
                return r;
            }""")
            logger.info("[上传诊断] URL: %s", report.get("url"))
            logger.info("[上传诊断] file inputs: %s", report.get("fileInputs"))
            for area in report.get("uploadAreas", [])[:10]:
                logger.info("[上传诊断] 上传区域: %s", area)
        except Exception as e:
            logger.warning("[上传诊断] 获取信息失败: %s", e)

    def _fill_title(self, title: str):
        """填写图文标题"""
        page = self._page
        logger.info("填写标题: %s", title)

        # 视频号图文标题通常是 input 或 contenteditable
        title_selectors = [
            "input[placeholder*='标题']",
            "input[placeholder*='输入标题']",
            "input[placeholder*='请输入标题']",
            "input[placeholder*='添加标题']",
            "[contenteditable='true'][placeholder*='标题']",
            "[class*='title'] input",
            "[class*='Title'] input",
            "[class*='title'] [contenteditable='true']",
        ]

        for sel in title_selectors:
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click()
                    loc.first.fill(title)
                    logger.info("标题已填写 (selector: %s)", sel)
                    return
            except Exception:
                continue

        # 兜底：JS 查找标题输入框（通常是第一个 input 或第一个 contenteditable）
        try:
            filled = page.evaluate("""(title) => {
                // 查找 placeholder 包含"标题"的 input
                const inputs = document.querySelectorAll('input');
                for (const inp of inputs) {
                    if (inp.placeholder && inp.placeholder.includes('标题')
                        && inp.offsetParent !== null) {
                        inp.focus();
                        inp.value = title;
                        inp.dispatchEvent(new Event('input', {bubbles: true}));
                        inp.dispatchEvent(new Event('change', {bubbles: true}));
                        return 'input:' + inp.placeholder;
                    }
                }
                // 查找 contenteditable 中 placeholder 含"标题"的
                const ces = document.querySelectorAll('[contenteditable="true"]');
                for (const ce of ces) {
                    const ph = ce.getAttribute('placeholder') || '';
                    if (ph.includes('标题') && ce.offsetParent !== null) {
                        ce.focus();
                        ce.innerText = title;
                        ce.dispatchEvent(new Event('input', {bubbles: true}));
                        return 'ce:' + ph;
                    }
                }
                return null;
            }""", title)
            if filled:
                logger.info("标题已填写 (JS: %s)", filled)
                return
        except Exception as e:
            logger.warning("JS 填写标题失败: %s", e)

        # 标题填写失败不阻止发布，但记录警告
        self._screenshot("channels_title_not_found.png")
        logger.warning("未找到标题输入框，可能导致发布失败")

    def _fill_content(self, text: str):
        """填写动态正文"""
        page = self._page
        logger.info("填写正文 (%d 字)", len(text))

        # 视频号发表页的正文编辑区通常是 contenteditable 或 textarea
        content_selectors = [
            "[contenteditable='true']",
            ".ProseMirror",
            ".ql-editor",
            "[class*='editor']",
            "[class*='Editor']",
            "[class*='content'] [contenteditable]",
            "textarea[placeholder*='说点什么']",
            "textarea[placeholder*='添加描述']",
            "textarea[placeholder*='描述']",
            "textarea",
        ]

        loc = self._wait_for_first(content_selectors, timeout=ELEMENT_TIMEOUT)
        if loc:
            loc.first.click()
            time.sleep(0.5)
            loc.first.fill(text)
            logger.info("正文已填写")
            return

        # 兜底：JS 查找可编辑元素
        try:
            handle = page.evaluate_handle(
                "() => {"
                "  const ces = document.querySelectorAll('[contenteditable=\"true\"]');"
                "  if (ces.length > 0) return ces[0];"
                "  const tas = document.querySelectorAll('textarea');"
                "  if (tas.length > 0) return tas[0];"
                "  return null;"
                "}"
            )
            el = handle.as_element()
            if el:
                el.click()
                el.fill(text)
                logger.info("正文已填写 (JS)")
                return
        except Exception:
            pass

        self._screenshot("channels_content_not_found.png")
        raise ChannelsPublishError("未找到正文编辑区，截图: logs/channels_content_not_found.png")

    def _wait_for_upload_done(self):
        """等待图片/视频上传处理完成"""
        page = self._page
        logger.info("等待上传处理完成...")
        try:
            page.wait_for_function(
                "() => {"
                "  const body = document.body.innerText;"
                "  return !body.includes('\u4e0a\u4f20\u4e2d') && !body.includes('\u52a0\u8f7d\u4e2d')"
                "    && !body.includes('\u5904\u7406\u4e2d');"
                "}",
                timeout=60000,
            )
            logger.info("上传处理完成")
        except Exception:
            logger.warning("上传处理等待超时，继续...")

    def _click_publish(self):
        """点击'发表'按钮"""
        page = self._page
        logger.info("点击发表...")

        # 先让编辑器失焦
        try:
            page.keyboard.press("Escape")
            time.sleep(0.3)
            page.mouse.click(10, 10)
            time.sleep(0.5)
        except Exception:
            pass

        # 滚动到底部
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(0.5)
        except Exception:
            pass

        # 视频号的发表按钮文字可能是"发表"、"发布"
        publish_texts = ["发表", "发布"]

        # 策略 1：JS 定位提交按钮，用坐标点击
        for btn_text in publish_texts:
            try:
                js_code = f"""() => {{
                    const btns = [...document.querySelectorAll('button')];
                    const candidates = btns.filter(b => {{
                        const txt = b.textContent.trim();
                        return txt === '{btn_text}' && b.offsetParent !== null;
                    }});
                    if (candidates.length === 0) return {{found: 0}};
                    let best = candidates[0];
                    let bestY = best.getBoundingClientRect().top;
                    for (const c of candidates) {{
                        const y = c.getBoundingClientRect().top;
                        if (y > bestY) {{ best = c; bestY = y; }}
                    }}
                    const r = best.getBoundingClientRect();
                    return {{found: candidates.length, x: r.x + r.width/2, y: r.y + r.height/2,
                        cls: (best.className||'').slice(0,80)}};
                }}"""
                result = page.evaluate(js_code)
                if result.get("found", 0) > 0:
                    x, y = result["x"], result["y"]
                    logger.info("找到 %d 个'%s'按钮，点击最底部 (y=%.0f)",
                                result["found"], btn_text, y)
                    page.mouse.click(x, y)
                    logger.info("已点击'%s'按钮", btn_text)
                    time.sleep(3)
                    self._handle_publish_dialog()
                    self._screenshot("channels_after_click.png")
                    return
            except Exception as e:
                logger.warning("JS 定位'%s'按钮异常: %s", btn_text, e)

        # 策略 2：Playwright get_by_role
        for btn_text in publish_texts:
            try:
                loc = page.get_by_role("button", name=btn_text, exact=True)
                if loc.count() > 0:
                    target = loc.last if loc.count() > 1 else loc.first
                    if target.is_visible():
                        target.scroll_into_view_if_needed()
                        time.sleep(0.3)
                        target.click(force=True)
                        logger.info("已点击'%s'按钮 (role)", btn_text)
                        time.sleep(3)
                        self._handle_publish_dialog()
                        return
            except Exception as e:
                logger.warning("role 选择器'%s'异常: %s", btn_text, e)

        # 策略 3：dispatchEvent 模拟完整交互链
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
                clicked = page.evaluate(js_dispatch)
                if clicked:
                    logger.info("已触发'%s'按钮事件链 (dispatchEvent)", btn_text)
                    time.sleep(3)
                    self._handle_publish_dialog()
                    return
            except Exception as e:
                logger.warning("dispatchEvent '%s' 异常: %s", btn_text, e)

        # 策略 4：遍历所有按钮
        for btn_text in publish_texts:
            try:
                all_btns = page.locator("button")
                count = all_btns.count()
                for i in range(count - 1, -1, -1):
                    btn = all_btns.nth(i)
                    try:
                        txt = btn.text_content(timeout=1000)
                        if txt and txt.strip() == btn_text and btn.is_visible():
                            btn.scroll_into_view_if_needed()
                            btn.click(force=True)
                            logger.info("已点击'%s'按钮 (CSS遍历, 第 %d 个)", btn_text, i)
                            time.sleep(3)
                            self._handle_publish_dialog()
                            return
                    except Exception:
                        continue
            except Exception:
                continue

        self._screenshot("channels_btn_not_found.png")
        raise ChannelsPublishError("未找到发表按钮，截图: logs/channels_btn_not_found.png")

    def _handle_publish_dialog(self):
        """处理发表后可能出现的确认对话框"""
        page = self._page
        time.sleep(1)

        dialog_btns = [
            "text=确认发表",
            "text=确认发布",
            "text=确认",
            "text=确定",
            "text=立即发表",
        ]
        for sel in dialog_btns:
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click()
                    logger.info("已确认发表对话框: %s", sel)
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

            # 检查 1：URL 跳转到内容管理列表页（确定发布成功）
            if "postlist" in url or "finderpostlist" in url or "finderNewLifePostList" in url.replace(" ", ""):
                logger.info("发布成功（已跳转到内容列表: %s）", page.url)
                return True

            # 检查 2：URL 离开了 create 页，但不是回到 create 页本身
            # finderNewLifeCreate / imagetext/create / post/create 都算仍在创建页
            create_patterns = ["create", "imagetext"]
            still_on_create = any(p in url for p in create_patterns)
            if not still_on_create and "/login" not in url:
                logger.info("发布成功（页面已跳转: %s）", page.url)
                return True

            # 检查 3：页面出现成功提示文字
            try:
                body_text = page.evaluate("document.body.innerText")
                success_keywords = [
                    "发表成功", "发布成功", "已发表", "已发布",
                    "再发一条", "继续发表", "动态发布成功",
                ]
                for keyword in success_keywords:
                    if keyword in body_text:
                        logger.info("发布成功（检测到: %s）", keyword)
                        return True

                # 检查 4：页面出现失败/错误提示
                fail_keywords = [
                    "发表失败", "发布失败", "内容不能为空",
                    "请添加图片", "至少上传", "请上传",
                ]
                for keyword in fail_keywords:
                    if keyword in body_text:
                        logger.warning("发布失败（检测到: %s）", keyword)
                        return False
            except Exception:
                pass

        # 最终状态诊断
        try:
            final_url = page.url
            logger.warning("发布结果不确定，最终 URL: %s", final_url)
            body_snippet = page.evaluate("document.body.innerText.slice(0, 500)")
            logger.warning("页面内容前500字: %s", body_snippet.replace("\n", " "))
        except Exception:
            pass

        self._screenshot("channels_publish_uncertain.png")
        return False

    # ════════════════════════════════════════════════════════════
    #  视频发布
    # ════════════════════════════════════════════════════════════

    def publish_video(self, video_path: str, body: str) -> bool:
        """
        在微信视频号发布视频动态

        Args:
            video_path: 本地视频文件路径
            body: 视频描述文案（含话题标签）
        """
        page = self._page
        video_file = Path(video_path)
        if not video_file.exists():
            raise ChannelsPublishError(f"视频文件不存在: {video_path}")

        logger.info("开始发布视频到视频号")
        logger.info("视频文件: %s (%.1fMB)", video_file.name,
                     video_file.stat().st_size / 1024 / 1024)

        try:
            # 1. 导航到视频发布页面（不使用图文发表页）
            self._navigate_to_video_create()

            # 2. 确保在视频模式
            self._switch_to_video_mode()

            # 3. 上传视频
            self._upload_video(str(video_file))

            # 4. 等待视频处理
            self._wait_for_video_processed()

            # 5. 填写描述
            self._fill_content(body)

            # 6. 等待视频处理完成 + 发表按钮可用
            self._wait_for_publish_button_enabled()
            time.sleep(2)

            self._screenshot("channels_video_before_publish.png")

            # 7. 点击发表
            self._click_publish()
            time.sleep(5)

            # 8. 检查结果
            success = self._check_publish_success()
            if success:
                logger.info("视频发布成功！")
            else:
                logger.warning("视频发布结果不确定，请手动检查")
                self._screenshot("channels_video_result.png")
            return success

        except ChannelsPublishError:
            self._screenshot("channels_video_error.png")
            raise
        except Exception as e:
            self._screenshot("channels_video_error.png")
            raise ChannelsPublishError(f"视频发布异常: {e}") from e

    def _navigate_to_video_create(self):
        """导航到视频发布页面：首页 → 点击'发表视频'按钮"""
        page = self._page
        logger.info("导航到视频发布页面...")

        # 策略 1：先到首页，点击"发表视频"按钮
        page.goto(PLATFORM_URL, wait_until="commit")
        self._wait_for_create_page()
        time.sleep(2)

        # 关闭可能的弹窗
        self._dismiss_dialogs()

        # 点击首页的"发表视频"按钮
        publish_btn_texts = ["发表视频", "发布视频", "发表动态"]
        for txt in publish_btn_texts:
            try:
                btn = page.get_by_text(txt, exact=False)
                if btn.count() > 0 and btn.first.is_visible():
                    btn.first.click()
                    logger.info("已点击首页 '%s' 按钮", txt)
                    time.sleep(3)
                    self._wait_for_create_page()
                    if self._is_video_create_page():
                        logger.info("已到达视频发布页: %s", page.url)
                        return
            except Exception:
                continue

        # 策略 2：通过 JS 查找按钮
        try:
            clicked = page.evaluate("""() => {
                const btns = document.querySelectorAll('button, a, div[class*="btn"], span');
                for (const b of btns) {
                    const t = b.textContent.trim();
                    if ((t.includes('发表视频') || t.includes('发布视频'))
                        && b.offsetParent !== null && t.length < 20) {
                        b.click();
                        return t;
                    }
                }
                return null;
            }""")
            if clicked:
                logger.info("通过 JS 点击: '%s'", clicked)
                time.sleep(3)
                self._wait_for_create_page()
                if self._is_video_create_page():
                    return
        except Exception:
            pass

        # 策略 3：直接访问 /post/create
        logger.info("首页按钮未找到，尝试直接访问 /post/create ...")
        page.goto(POST_CREATE_URL, wait_until="commit")
        self._wait_for_create_page()
        self._dismiss_dialogs()
        time.sleep(2)

        if self._is_video_create_page():
            logger.info("已到达视频发布页: %s", page.url)
            return

        # 策略 4：通过侧边栏 内容管理 > 视频 > 发表视频
        logger.info("尝试侧边栏导航...")
        self._navigate_via_sidebar()

        self._screenshot("channels_video_nav.png")

    def _dismiss_dialogs(self):
        """关闭可能出现的弹窗"""
        page = self._page
        for txt in ["我知道了", "知道了", "确定", "关闭"]:
            try:
                btn = page.get_by_text(txt, exact=True)
                if btn.count() > 0 and btn.first.is_visible():
                    btn.first.click()
                    logger.info("关闭弹窗: '%s'", txt)
                    time.sleep(1)
            except Exception:
                continue

    def _is_video_create_page(self) -> bool:
        """判断当前是否在视频发布页（有视频上传区域）"""
        try:
            body_text = self._page.evaluate("document.body.innerText.slice(0, 3000)") or ""
            # 视频发布页特征：有"视频简述"或"上传时长"等字样
            video_create_indicators = ["视频简述", "上传时长", "MP4/H.264", "H.264"]
            # 图文页特征
            imagetext_indicators = ["图文标题", "图文描述", "图文管理"]

            is_video_create = any(kw in body_text for kw in video_create_indicators)
            is_imagetext = any(kw in body_text for kw in imagetext_indicators)

            if is_video_create and not is_imagetext:
                return True
            if is_imagetext:
                logger.info("当前在图文发布页，需要切换到视频")
                return False

            # 也可以通过 file input 的 accept 属性判断
            file_inputs = self._page.locator("input[type='file']")
            for i in range(file_inputs.count()):
                accept = file_inputs.nth(i).get_attribute("accept") or ""
                if "video" in accept or "mp4" in accept:
                    return True
        except Exception:
            pass
        return False

    def _navigate_via_sidebar(self):
        """通过侧边栏导航到视频发布页"""
        page = self._page
        try:
            # 点击 "内容管理" 展开子菜单
            content_mgmt = page.get_by_text("内容管理", exact=True)
            if content_mgmt.count() > 0:
                content_mgmt.first.click()
                time.sleep(1)

            # 点击 "视频" 子菜单
            video_link = page.locator(
                "li.finder-ui-desktop-sub-menu__item a:has(span:text-is('视频'))"
            )
            if video_link.count() > 0 and video_link.first.is_visible():
                video_link.first.click()
                logger.info("已点击侧边栏 '视频'")
                time.sleep(3)
                self._wait_for_create_page()

            # 到了视频管理列表页后，找"发表视频"按钮
            for txt in ["发表视频", "发表动态", "发布视频"]:
                try:
                    btn = page.get_by_text(txt, exact=False)
                    if btn.count() > 0 and btn.first.is_visible():
                        btn.first.click()
                        logger.info("点击 '%s' 按钮", txt)
                        time.sleep(3)
                        self._wait_for_create_page()
                        return
                except Exception:
                    continue

            # JS 查找按钮
            clicked = page.evaluate("""() => {
                const els = document.querySelectorAll('button, a, div, span');
                for (const el of els) {
                    const t = el.textContent.trim();
                    if ((t === '发表视频' || t === '发表动态') && el.offsetParent !== null && t.length < 10) {
                        el.click();
                        return t;
                    }
                }
                return null;
            }""")
            if clicked:
                logger.info("通过 JS 点击: '%s'", clicked)
                time.sleep(3)
                self._wait_for_create_page()

        except Exception as e:
            logger.warning("侧边栏导航失败: %s", e)

    def _switch_to_video_mode(self):
        """确认在视频模式（供 publish_video 调用）"""
        if self._is_video_create_page():
            logger.info("当前已在视频发布模式")
            return
        logger.warning("未在视频发布页，尝试侧边栏导航...")
        self._navigate_via_sidebar()

    def _upload_video(self, video_path: str):
        """上传视频文件"""
        page = self._page
        logger.info("上传视频...")

        try:
            page.wait_for_selector("input[type='file']", state="attached", timeout=ELEMENT_TIMEOUT)
            all_inputs = page.locator("input[type='file']")
            file_input = None
            for i in range(all_inputs.count()):
                inp = all_inputs.nth(i)
                accept = inp.get_attribute("accept") or ""
                if "mp4" in accept or "mov" in accept or "video" in accept:
                    file_input = inp
                    logger.info("找到视频上传控件 (accept=%s)", accept[:60])
                    break
            if not file_input:
                file_input = all_inputs.first
                logger.info("使用第一个 file input")

            file_input.set_input_files(video_path)
            logger.info("视频文件已提交上传")
        except Exception as e:
            raise ChannelsPublishError(f"视频上传失败: {e}") from e

    def _wait_for_video_processed(self):
        """等待视频上传和处理完成（最长 5 分钟）"""
        page = self._page
        logger.info("等待视频处理（最长 5 分钟）...")

        start = time.time()
        max_wait = 300  # 5 分钟

        while time.time() - start < max_wait:
            elapsed = int(time.time() - start)

            # 检查正文编辑区是否出现
            for sel in ["[contenteditable='true']", "textarea",
                        "input[placeholder*='描述']", ".ProseMirror"]:
                try:
                    loc = page.locator(sel)
                    if loc.count() > 0 and loc.first.is_visible():
                        logger.info("视频处理完成，编辑区已出现 (%ds)", elapsed)
                        return
                except Exception:
                    pass

            # 检查"上传中"/"转码中"是否消失
            try:
                body_text = page.evaluate("document.body.innerText")
                still_processing = any(kw in body_text for kw in [
                    "上传中", "转码中", "处理中", "正在上传"
                ])
                if not still_processing and elapsed > 10:
                    logger.info("视频处理可能已完成 (%ds)", elapsed)
                    return
            except Exception:
                pass

            if elapsed % 15 == 0 and elapsed > 0:
                logger.info("视频处理中... (%ds/%ds)", elapsed, max_wait)
                self._screenshot(f"channels_video_processing_{elapsed}s.png")

            time.sleep(3)

        logger.warning("视频处理等待超时 (%ds)，尝试继续...", max_wait)

    def _wait_for_publish_button_enabled(self, max_wait: int = 120):
        """等待发表按钮可用"""
        page = self._page
        logger.info("等待发表按钮可用...")

        publish_texts_js = "['发表','发布']"
        js_check = f"""() => {{
            const btns = [...document.querySelectorAll('button')];
            for (const b of btns) {{
                const txt = b.textContent.trim();
                if (!{publish_texts_js}.includes(txt)) continue;
                if (b.offsetParent === null) continue;
                if (b.disabled) continue;
                const cls = (b.className || '');
                if (cls.includes('disabled') || cls.includes('is-disabled')) continue;
                return true;
            }}
            return false;
        }}"""

        try:
            page.wait_for_function(js_check, timeout=max_wait * 1000)
            logger.info("发表按钮已可用")
        except Exception:
            logger.warning("等待发表按钮超时 (%ds)，尝试继续...", max_wait)

    # ════════════════════════════════════════════════════════════
    #  诊断
    # ════════════════════════════════════════════════════════════

    def diagnose(self):
        """诊断发表页面元素（供 debug 命令调用）"""
        page = self._page
        page.goto(POST_CREATE_URL, wait_until="commit")
        self._wait_for_create_page()

        print("\n" + "=" * 60)
        print("  微信视频号发表页诊断报告")
        print("=" * 60)
        print(f"  URL: {page.url}")
        print(f"  HTML: {len(page.content()):,} 字符")
        print(f"  Frames: {len(page.frames)} 个")

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

        self._screenshot("channels_diagnose.png")
        print(f"\n  截图已保存: logs/channels_diagnose.png")
        print("=" * 60)

    # ────────── 工具 ──────────

    @staticmethod
    def _guess_suffix(url: str, content_type: str) -> str:
        low = url.lower()
        if "png" in low or "png" in content_type:
            return ".png"
        if "webp" in low or "webp" in content_type:
            return ".webp"
        if "gif" in low:
            return ".gif"
        return ".jpg"


# ────────── 便捷函数 ──────────

def publish_channels_text(body: str, image_sources: List[str] = None,
                          title: str = "", headless: bool = False) -> bool:
    """一键发布微信视频号图文动态"""
    with ChannelsPublisher(headless=headless) as pub:
        pub.login()
        time.sleep(settings.channels_publish_delay)
        return pub.publish_text_image(body, image_sources, title=title)


def publish_channels_video(video_path: str, body: str,
                           headless: bool = False) -> bool:
    """一键发布微信视频号视频动态"""
    with ChannelsPublisher(headless=headless) as pub:
        pub.login()
        time.sleep(settings.channels_publish_delay)
        return pub.publish_video(video_path, body)


def diagnose_channels_page():
    """诊断发表页面（debug 命令入口）"""
    with ChannelsPublisher(headless=False) as pub:
        pub.login()
        pub.diagnose()
        print("\n浏览器保持 15 秒，可手动检查...")
        time.sleep(15)
