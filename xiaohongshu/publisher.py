"""
小红书自动发布模块
使用 Playwright 自动化浏览器，在小红书创作者中心发布图文笔记
核心策略：networkidle + skeleton消失检测 + 轮询选择器 + JS兜底
"""

import tempfile
import time
from pathlib import Path
from typing import List, Optional

import requests as req

from shared.config import get_settings
from shared.utils.exceptions import LoginTimeoutError, PublishError
from shared.utils.logger import get_logger
from shared.llm.xhs import XHSContent
from shared.publisher_base import BasePublisher, NAV_TIMEOUT, ELEMENT_TIMEOUT

settings = get_settings()

logger = get_logger("xhs_publisher")

CREATOR_URL = "https://creator.xiaohongshu.com"
LOGIN_URL = f"{CREATOR_URL}/login"
PUBLISH_URL = f"{CREATOR_URL}/publish/publish?source=official&type=normal"

COOKIES_FILE = Path(__file__).resolve().parent / "data" / "xhs_cookies.json"



class XHSPublisher(BasePublisher):
    """小红书自动发布器，支持 with 语句"""

    USER_DATA_DIR = Path(__file__).resolve().parent / "data" / "browser_profile"

    def __init__(self, headless: bool = False):
        super().__init__(headless)

    # ────────── 登录 ──────────

    def login(self):
        """登录小红书创作者中心"""
        page = self._page

        if settings.xhs_cookie and not COOKIES_FILE.exists():
            self._set_cookies_from_string(settings.xhs_cookie)
            logger.info("通过 .env COOKIE 设置登录态")

        logger.info("打开小红书创作者中心...")
        page.goto(CREATOR_URL, wait_until="commit")
        time.sleep(5)

        if self._is_logged_in():
            logger.info("已登录")
            self._save_cookies()
            return

        logger.info("未登录，请在浏览器中扫码...")
        page.goto(LOGIN_URL, wait_until="commit")

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
        url = self._page.url
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
                    "domain": ".xiaohongshu.com",
                    "path": "/",
                })
        if cookies:
            self._context.add_cookies(cookies)

    def _save_cookies(self):
        COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._context.storage_state(path=str(COOKIES_FILE))
        logger.info("Cookie 已保存")

    # ────────── 等待发布页完整加载 ──────────

    def _wait_for_publish_page(self):
        """
        四阶段等待策略：
        1. networkidle — 等所有 JS/CSS/API 请求完成
        2. skeleton 消失 — 等灰色占位块消失，表单已渲染
        3. 切换到"上传图文"tab — 发布页默认是视频上传
        4. 等待图文表单出现
        """
        page = self._page
        logger.info("等待页面完整加载...")

        # 阶段 1：网络空闲
        try:
            page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT)
            logger.info("网络空闲")
        except Exception:
            logger.warning("网络空闲超时，继续...")

        # 阶段 2：skeleton 消失
        logger.info("等待内容渲染...")
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
            logger.info("页面渲染完成")
        except Exception:
            logger.warning("骨架屏检测超时，继续...")

        time.sleep(2)

        # 阶段 3：点击"上传图文"tab（默认是"上传视频"）
        self._switch_to_image_tab()

        # 阶段 4：等待图文上传区域出现
        logger.info("等待图文上传区域...")
        upload_selectors = [
            "input[type='file'][accept*='image']",
            "input[type='file'][accept*='jpg']",
            "input[type='file']",
        ]
        loc = self._wait_for_first(upload_selectors, timeout=30000)
        if not loc:
            # file input 通常是 hidden 的，用 attached 状态等待
            try:
                page.wait_for_selector("input[type='file']", state="attached", timeout=15000)
                logger.info("图文上传区域已就绪")
            except Exception:
                logger.warning("上传区域未找到，尝试继续...")
        else:
            logger.info("图文上传区域已就绪")

        time.sleep(2)
        self._screenshot("page_ready.png")

    def _switch_to_image_tab(self):
        """点击'上传图文'tab 切换到图文笔记发布模式"""
        page = self._page
        logger.info("切换到'上传图文'模式...")

        tab_texts = ["上传图文", "图文"]

        # 策略 1：get_by_text 精确匹配
        for txt in tab_texts:
            try:
                loc = page.get_by_text(txt, exact=True)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click()
                    logger.info("已点击'%s' (get_by_text)", txt)
                    time.sleep(3)
                    return
            except Exception:
                pass

        # 策略 2：role=tab
        for txt in tab_texts:
            try:
                loc = page.get_by_role("tab", name=txt)
                if loc.count() > 0:
                    loc.first.click()
                    logger.info("已点击'%s'标签", txt)
                    time.sleep(3)
                    return
            except Exception:
                pass

        # 策略 3：CSS 选择器匹配 tab 类元素
        tab_css_selectors = [
            "[class*='tab']", "[class*='Tab']", "[role='tab']",
            "[class*='menu-item']", "[class*='nav-item']",
        ]
        for sel in tab_css_selectors:
            try:
                locs = page.locator(sel)
                for i in range(locs.count()):
                    el = locs.nth(i)
                    el_text = el.text_content(timeout=1000) or ""
                    if "上传图文" in el_text.strip() or el_text.strip() == "图文":
                        el.click()
                        logger.info("已点击'%s'标签", el_text.strip())
                        time.sleep(3)
                        return
            except Exception:
                continue

        # 策略 4：JS 深度搜索所有元素文本匹配
        try:
            clicked = page.evaluate("""() => {
                const targets = ['上传图文', '图文'];
                const all = document.querySelectorAll('*');
                for (const el of all) {
                    const txt = el.textContent.trim();
                    if (!targets.includes(txt)) continue;
                    if (el.offsetParent === null) continue;
                    // 只点叶子或近叶子节点（避免点到整个页面容器）
                    if (el.children.length > 3) continue;
                    el.click();
                    return txt;
                }
                return null;
            }""")
            if clicked:
                logger.info("已通过 JS 点击: %s", clicked)
                time.sleep(3)
                return
        except Exception:
            pass

        # 策略 5：坐标点击
        for txt in tab_texts:
            try:
                loc = page.locator(f"text={txt}")
                if loc.count() > 0:
                    box = loc.first.bounding_box()
                    if box:
                        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                        logger.info("已通过坐标点击'%s'", txt)
                        time.sleep(3)
                        return
            except Exception:
                pass

        # 策略 6：强制点击 + 更多诊断
        try:
            # 先诊断页面上所有包含"图文"文字的元素
            diag = page.evaluate("""() => {
                const results = [];
                const all = document.querySelectorAll('*');
                for (const el of all) {
                    const txt = (el.textContent || '').trim();
                    if (txt.includes('图文') && txt.length < 20) {
                        const rect = el.getBoundingClientRect();
                        results.push({
                            tag: el.tagName, text: txt,
                            cls: (el.className || '').toString().slice(0, 80),
                            w: Math.round(rect.width), h: Math.round(rect.height),
                            x: Math.round(rect.x), y: Math.round(rect.y),
                            children: el.children.length,
                            visible: el.offsetParent !== null
                        });
                    }
                }
                return results;
            }""")
            logger.debug("[标签诊断] 包含'图文'的元素 %d 个", len(diag))

            # 尝试点击最匹配的元素坐标
            for d in diag:
                if d["text"] in ("上传图文", "图文") and d["w"] > 0 and d["h"] > 0:
                    cx = d["x"] + d["w"] / 2
                    cy = d["y"] + d["h"] / 2
                    page.mouse.click(cx, cy)
                    logger.info("已通过诊断坐标点击 '%s' @(%d,%d)", d["text"], cx, cy)
                    time.sleep(3)
                    return
        except Exception as e:
            logger.warning("[标签诊断] 失败: %s", e)

        # 策略 7：Playwright force click
        for txt in tab_texts:
            try:
                loc = page.locator(f"text={txt}")
                cnt = loc.count()
                logger.info("定位器 text=%s 匹配 %d 个", txt, cnt)
                if cnt > 0:
                    loc.first.click(force=True, timeout=5000)
                    logger.info("已强制点击 '%s'", txt)
                    time.sleep(3)
                    return
            except Exception as e:
                logger.warning("强制点击 '%s' 失败: %s", txt, e)

        # 策略 8：重新导航到图文上传 URL
        alt_urls = [
            f"{CREATOR_URL}/publish/publish?from=tab_switch&type=normal",
            f"{CREATOR_URL}/publish/publish?tab=1",
        ]
        for url in alt_urls:
            try:
                logger.info("尝试导航到图文上传 URL: %s", url)
                page.goto(url, wait_until="commit")
                time.sleep(5)
                # 检查 file input 是否变成图片类型
                file_inputs = page.locator("input[type='file']")
                for i in range(file_inputs.count()):
                    accept = file_inputs.nth(i).get_attribute("accept") or ""
                    if "image" in accept or "jpg" in accept or "png" in accept:
                        logger.info("成功导航到图文上传页 (accept=%s)", accept)
                        return
            except Exception:
                continue

        logger.warning("未找到'上传图文'标签，当前页面可能已是图文模式")

    # ────────── 发布笔记 ──────────

    def publish(self, content: XHSContent) -> bool:
        """在小红书创作者中心发布图文笔记"""
        page = self._page
        logger.info("开始发布: %s", content.title)

        try:
            # 1. 打开发布页并等待完整加载
            logger.info("打开发布页面...")
            page.goto(PUBLISH_URL, wait_until="commit")
            self._wait_for_publish_page()

            # 2. 上传图片（必须先上传，上传后才出现标题/正文编辑区）
            if content.image_urls:
                self._upload_images(content.image_urls)
            else:
                logger.info("无图片，尝试继续...")

            # 3. 等待编辑表单出现（上传图片后异步加载标题+正文）
            self._wait_for_editor()

            # 4. 填写标题（小红书限制 20 字）
            title = content.title[:20] if len(content.title) > 20 else content.title
            self._fill_title(title)

            # 5. 填写正文
            self._fill_body(content.full_text())
            self._screenshot("before_publish.png")

            # 6. 等待图片处理完成（"加载中"消失）
            self._wait_for_upload_done()
            time.sleep(2)

            # 7. 点击发布
            self._click_publish()
            time.sleep(5)

            # 8. 检查结果
            success = self._check_publish_success()
            if success:
                logger.info("发布成功！")
            else:
                logger.warning("发布结果不确定，请手动检查")
                self._screenshot("result.png")
            return success

        except PublishError:
            self._screenshot("error.png")
            raise
        except Exception as e:
            self._screenshot("error.png")
            raise PublishError(f"发布异常: {e}") from e

    # ────────── 等待编辑表单 ──────────

    def _wait_for_editor(self):
        """上传图片后，等待标题+正文编辑区出现"""
        page = self._page
        logger.info("等待编辑表单加载（标题+正文）...")

        editor_selectors = [
            "input[placeholder*='标题']",
            ".el-input__inner",
            ".c-input_inner",
            "[class*='title'] input",
            "input[type='text']",
            "[contenteditable='true']",
            ".ProseMirror",
            ".ql-editor",
        ]
        loc = self._wait_for_first(editor_selectors, timeout=ELEMENT_TIMEOUT)
        if loc:
            logger.info("编辑表单已加载")
        else:
            self._screenshot("editor_not_found.png")
            logger.warning("编辑表单未加载，尝试继续...")

        time.sleep(2)

    def _wait_for_upload_done(self):
        """等待图片处理完成（'加载中'字样消失）"""
        page = self._page
        logger.info("等待图片处理完成...")
        try:
            page.wait_for_function(
                "() => {"
                "  const body = document.body.innerText;"
                "  return !body.includes('\u52a0\u8f7d\u4e2d');"
                "}",
                timeout=30000,
            )
            logger.info("图片处理完成")
        except Exception:
            logger.warning("图片处理等待超时，继续...")

    # ────────── 上传图片 ──────────

    def _upload_images(self, image_sources: list):
        """
        准备图片文件并上传。
        image_sources 可以是:
        - URL 列表（http(s)://...），会自动下载到临时文件
        - 本地文件路径列表，直接使用
        """
        page = self._page
        logger.info("准备上传 %d 张图片", len(image_sources))

        # 1. 准备本地文件（区分 URL 和本地路径）
        local_files = []
        for src in image_sources:
            src_str = str(src)
            if src_str.startswith("http://") or src_str.startswith("https://"):
                # 远程 URL → 下载到临时文件
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
                # 本地文件路径
                local_path = Path(src_str)
                if local_path.exists():
                    local_files.append(str(local_path))
                    logger.info("使用本地图片: %s", local_path.name)
                else:
                    logger.warning("本地图片不存在，跳过: %s", src_str)

        if not local_files:
            logger.warning("无图片可上传")
            return

        # 2. 等待 file input 出现
        logger.info("等待图片上传控件...")
        file_input = None
        try:
            # 优先找接受图片的 file input
            page.wait_for_selector("input[type='file']", state="attached", timeout=ELEMENT_TIMEOUT)
            all_inputs = page.locator("input[type='file']")
            for i in range(all_inputs.count()):
                inp = all_inputs.nth(i)
                accept = inp.get_attribute("accept") or ""
                if "image" in accept or "jpg" in accept or "png" in accept or not accept:
                    file_input = inp
                    logger.info("找到图片上传控件 (accept=%s)", accept)
                    break
            if not file_input:
                # 没有明确的图片 input，用第一个
                file_input = all_inputs.first
                logger.info("使用第一个文件输入框")
        except Exception:
            logger.warning("上传控件未出现，跳过图片上传")
            return
        try:
            file_input.set_input_files(local_files)
            logger.info("已上传 %d 张图片", len(local_files))
        except Exception as e:
            # 逐张上传作为备选
            logger.warning("批量上传失败，尝试逐张: %s", e)
            for i, f in enumerate(local_files):
                try:
                    file_input.set_input_files(f)
                    time.sleep(2)
                    logger.info("逐张上传 %d/%d", i + 1, len(local_files))
                except Exception as e2:
                    logger.warning("第 %d 张上传失败: %s", i + 1, e2)

        # 等待上传处理完成
        time.sleep(5)

    # ────────── 填写标题 ──────────

    def _fill_title(self, title: str):
        """三策略查找标题输入框并填写"""
        page = self._page
        logger.info("填写标题: %s", title)

        # 策略 1：CSS 选择器（优先带 placeholder 的，再通用）
        css_selectors = [
            "input[placeholder*='标题']",
            "input[placeholder*='填写']",
            ".el-input__inner",
            ".c-input_inner",
            "[class*='title'] input",
            "[class*='Title'] input",
            "input[type='text']",
            "input:not([type='file']):not([type='hidden']):not([type='search'])",
        ]
        loc = self._wait_for_first(css_selectors, timeout=ELEMENT_TIMEOUT)
        if loc:
            loc.first.click()
            loc.first.fill(title)
            logger.info("标题已填写")
            return

        # 策略 2：contenteditable 标题
        ce_selectors = [
            "[contenteditable='true'][class*='title']",
            "[contenteditable='true'][class*='Title']",
        ]
        loc = self._wait_for_first(ce_selectors, timeout=5000)
        if loc:
            loc.first.click()
            loc.first.fill(title)
            logger.info("标题已填写 (内容编辑区)")
            return

        # 策略 3：JS 深度搜索（遍历所有 DOM 找可见 input）
        try:
            handle = page.evaluate_handle(
                "() => {"
                "  for (const inp of document.querySelectorAll('input')) {"
                "    const t = inp.type || 'text';"
                "    if (['file','hidden','search','checkbox','radio'].includes(t)) continue;"
                "    if (inp.offsetParent !== null) return inp;"
                "  }"
                "  const ces = document.querySelectorAll('[contenteditable=\"true\"]');"
                "  return ces.length > 0 ? ces[0] : null;"
                "}"
            )
            el = handle.as_element()
            if el:
                el.click()
                el.fill(title)
                logger.info("标题已填写 (JS)")
                return
        except Exception as e:
            logger.debug("_fill_title JS fallback failed: %s", e)

        self._screenshot("title_not_found.png")
        raise PublishError("未找到标题输入框，截图: logs/title_not_found.png")

    # ────────── 填写正文 ──────────

    def _fill_body(self, text: str):
        """查找正文编辑器并填写"""
        page = self._page
        logger.info("填写正文 (%d 字)", len(text))

        css_selectors = [
            ".ProseMirror",
            ".ql-editor",
            "[contenteditable='true'][class*='editor']",
            "[contenteditable='true'][class*='content']",
            ".el-textarea__inner",
            "textarea",
        ]
        loc = self._wait_for_first(css_selectors, timeout=ELEMENT_TIMEOUT)
        if loc:
            loc.first.click()
            loc.first.fill(text)
            logger.info("正文已填写")
            return

        # 兜底：取最后一个 contenteditable（通常标题在前、正文在后）
        try:
            page.wait_for_selector("[contenteditable='true']", timeout=10000)
            all_ce = page.locator("[contenteditable='true']")
            n = all_ce.count()
            target = all_ce.nth(n - 1) if n >= 2 else all_ce.first
            target.click()
            target.fill(text)
            logger.info("正文已填写 (ce 兜底, 共 %d 个)", n)
            return
        except Exception as e:
            logger.debug("_fill_body fallback failed: %s", e)

        self._screenshot("body_not_found.png")
        raise PublishError("未找到正文编辑器，截图: logs/body_not_found.png")

    # ────────── 点击发布 ──────────

    def _click_publish(self):
        """精确定位并点击底部红色'发布'按钮（非侧边栏的"发布笔记"）"""
        page = self._page
        logger.info("点击发布...")

        # 先让编辑器失焦（点击空白区域 + Escape）
        try:
            page.keyboard.press("Escape")
            time.sleep(0.3)
            page.mouse.click(10, 10)
            time.sleep(0.5)
        except Exception:
            pass

        # 滚动到底部确保按钮可见
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(0.5)
        except Exception:
            pass

        # 策略 1：JS 定位底部提交按钮，返回坐标，用 Playwright 原生鼠标点击
        try:
            js_code = """() => {
                const btns = [...document.querySelectorAll('button')];
                const candidates = btns.filter(b => {
                    const txt = b.textContent.trim();
                    return txt === '发布' && b.offsetParent !== null;
                });
                if (candidates.length === 0) return {found: 0};
                let best = candidates[0];
                let bestY = best.getBoundingClientRect().top;
                for (const c of candidates) {
                    const y = c.getBoundingClientRect().top;
                    if (y > bestY) { best = c; bestY = y; }
                }
                const r = best.getBoundingClientRect();
                return {found: candidates.length, x: r.x + r.width/2, y: r.y + r.height/2,
                    cls: (best.className||'').slice(0,80)};
            }"""
            result = page.evaluate(js_code)
            if result.get("found", 0) > 0:
                x, y = result["x"], result["y"]
                logger.info("找到 %d 个'发布'按钮，点击最底部 (y=%.0f, cls=%s)",
                            result["found"], y, result.get("cls", ""))
                page.mouse.click(x, y)
                logger.info("已点击发布按钮 (坐标 %.0f, %.0f)", x, y)
                time.sleep(3)
                self._handle_publish_dialog()
                self._screenshot("after_click_1.png")
                return
        except Exception as e:
            logger.debug("_click_publish JS strategy failed: %s", e)

        # 策略 2：Playwright get_by_role + force click
        try:
            loc = page.get_by_role("button", name="发布", exact=True)
            if loc.count() > 0:
                target = loc.last if loc.count() > 1 else loc.first
                if target.is_visible():
                    target.scroll_into_view_if_needed()
                    time.sleep(0.3)
                    target.click(force=True)
                    logger.info("已点击发布按钮 (role+force, 共 %d 个)", loc.count())
                    time.sleep(3)
                    self._handle_publish_dialog()
                    self._screenshot("after_click_2.png")
                    return
        except Exception as e:
            logger.warning("role 选择器异常: %s", e)

        # 策略 3：直接用 dispatchEvent 触发 click + pointerdown + pointerup（模拟完整交互链）
        try:
            js_dispatch = """() => {
                const btns = [...document.querySelectorAll('button')];
                const target = btns.filter(b => b.textContent.trim() === '发布' && b.offsetParent !== null)
                    .sort((a,b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top)[0];
                if (!target) return false;
                ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(evtName => {
                    target.dispatchEvent(new MouseEvent(evtName, {bubbles:true, cancelable:true, view:window}));
                });
                return true;
            }"""
            clicked = page.evaluate(js_dispatch)
            if clicked:
                logger.info("已触发发布按钮事件链 (dispatchEvent)")
                time.sleep(3)
                self._handle_publish_dialog()
                self._screenshot("after_click_3.png")
                return
        except Exception as e:
            logger.warning("dispatchEvent 异常: %s", e)

        # 策略 4：CSS 选择器遍历 + force click
        try:
            all_btns = page.locator("button")
            count = all_btns.count()
            for i in range(count - 1, -1, -1):
                btn = all_btns.nth(i)
                try:
                    txt = btn.text_content(timeout=1000)
                    if txt and txt.strip() == "发布" and btn.is_visible():
                        btn.scroll_into_view_if_needed()
                        btn.click(force=True)
                        logger.info("已点击发布按钮 (CSS遍历+force, 第 %d 个)", i)
                        time.sleep(3)
                        self._handle_publish_dialog()
                        return
                except Exception:
                    continue
        except Exception as e:
            logger.warning("CSS 遍历异常: %s", e)

        self._screenshot("btn_not_found.png")
        raise PublishError("未找到发布按钮，截图: logs/btn_not_found.png")

    def _handle_publish_dialog(self):
        """处理点击发布后可能出现的确认对话框"""
        page = self._page
        time.sleep(1)

        # 检查是否弹出对话框（常见：确认发布、内容检测等）
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
                    logger.info("已确认发布对话框: %s", sel)
                    time.sleep(2)
                    return
            except Exception:
                continue

    # ────────── 检查结果 ──────────

    def _check_publish_success(self) -> bool:
        """
        多策略检测发布是否成功：
        1. URL 不再包含 publish（页面跳转了）
        2. 页面出现"发布成功"/"已发布"文字
        3. 页面出现"再发一篇"/"继续发布"等提示
        4. 页面回到上传初始状态（"拖拽视频"/"上传视频"按钮）
        """
        page = self._page

        # 等待更长时间让页面响应
        for wait_sec in (2, 3, 5):
            time.sleep(wait_sec)
            url = page.url.lower()

            # 检查 1：URL 跳转
            if "publish" not in url:
                logger.info("发布成功（页面已跳转: %s）", page.url)
                return True

            # 检查 2：页面文字
            try:
                body_text = page.evaluate("document.body.innerText")

                # 成功提示文字
                for keyword in ("发布成功", "已发布", "再发一篇", "继续发布", "笔记发布成功", "立即返回", "自动返回发布页"):
                    if keyword in body_text:
                        logger.info("发布成功（检测到: %s）", keyword)
                        return True

                # 视频发布成功后页面可能回到上传初始页
                # 特征：出现"拖拽视频到此"或独立的"上传视频"按钮
                if "拖拽视频到此" in body_text:
                    logger.info("发布成功（页面已回到上传初始页）")
                    return True
            except Exception:
                pass

        # 最后一次截图
        self._screenshot("publish_uncertain.png")
        return False

    # ────────── 视频发布 ──────────

    def publish_video(self, video_path: str, title: str, body: str) -> bool:
        """
        在小红书创作者中心发布视频笔记

        Args:
            video_path: 本地视频文件路径
            title: 笔记标题（<=20字）
            body: 笔记正文（含话题标签）
        """
        page = self._page
        video_file = Path(video_path)
        if not video_file.exists():
            raise PublishError(f"视频文件不存在: {video_path}")

        logger.info("开始发布视频笔记: %s", title)
        logger.info("视频文件: %s (%.1fMB)", video_file.name,
                     video_file.stat().st_size / 1024 / 1024)

        try:
            # 1. 打开发布页（默认就是"上传视频"tab）
            logger.info("打开发布页面...")
            page.goto(PUBLISH_URL, wait_until="commit")
            self._wait_for_video_page()

            # 2. 上传视频
            self._upload_video(str(video_file))

            # 3. 等待视频处理 + 编辑表单出现
            self._wait_for_video_processed()
            self._wait_for_editor()

            # 4. 填写标题
            safe_title = title[:20] if len(title) > 20 else title
            self._fill_title(safe_title)

            # 5. 填写正文
            self._fill_body(body)

            # 6. 点击空白关闭话题下拉框
            try:
                page.mouse.click(10, 10)
                time.sleep(1)
            except Exception:
                pass

            # 7. 声明原创
            self._declare_original()

            self._screenshot("video_before_publish.png")

            # 8. 等待封面生成 + 发布按钮可点击
            self._wait_for_publish_button_enabled()
            time.sleep(2)

            # 9. 点击发布
            self._click_publish()
            time.sleep(5)

            # 9. 检查结果
            success = self._check_publish_success()
            if success:
                logger.info("视频发布成功！")
            else:
                logger.warning("视频发布结果不确定，请手动检查")
                self._screenshot("video_result.png")
            return success

        except PublishError:
            self._screenshot("video_error.png")
            raise
        except Exception as e:
            self._screenshot("video_error.png")
            raise PublishError(f"视频发布异常: {e}") from e

    def _declare_original(self):
        """
        勾选原创声明开关。
        小红书页面结构：「原创声明」文字右边有一个 custom-switch 组件。
        DOM 层级: custom-switch-card(308x44 整行) > custom-switch(~40x20 开关) > custom-switch-icon(16x16 圆点)
        需要精确点击 custom-switch（开关本体），不能点 card（整行）也不能点 icon（太小）。
        """
        page = self._page
        logger.info("尝试勾选原创声明...")

        # Step 0: 展开"更多设置"（若有折叠区域）
        self._expand_more_options()

        # Step 1: 滚动让所有选项可见
        page.evaluate("window.scrollTo(0, 400)")
        time.sleep(1)

        for attempt in range(4):
            # Step 2: 诊断 — 收集"原创声明"行附近所有 switch 相关元素
            try:
                diag = page.evaluate("""() => {
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_TEXT, null, false
                    );
                    while (walker.nextNode()) {
                        if (walker.currentNode.textContent.trim() !== '原创声明') continue;
                        const label = walker.currentNode.parentElement;
                        if (!label || label.offsetParent === null) continue;
                        const labelRect = label.getBoundingClientRect();

                        // 收集所有 switch 元素（向上8层搜索）
                        let row = label;
                        const allSwitches = [];
                        for (let d = 0; d < 8 && row; d++) {
                            const sws = row.querySelectorAll(
                                '[class*="switch"], [class*="Switch"], [class*="toggle"], [class*="Toggle"], ' +
                                '[role="switch"], button[role="switch"]'
                            );
                            for (const sw of sws) {
                                const r = sw.getBoundingClientRect();
                                if (r.width < 3 || r.height < 3) continue;
                                const cls = (sw.className || '').toString();
                                allSwitches.push({
                                    cls: cls.slice(0, 80), tag: sw.tagName,
                                    x: Math.round(r.x), y: Math.round(r.y),
                                    w: Math.round(r.width), h: Math.round(r.height),
                                    cx: Math.round(r.x + r.width / 2),
                                    cy: Math.round(r.y + r.height / 2),
                                    aria: sw.getAttribute('aria-checked'),
                                });
                            }
                            row = row.parentElement;
                        }
                        return {
                            found: true,
                            labelX: Math.round(labelRect.x),
                            labelY: Math.round(labelRect.y),
                            labelW: Math.round(labelRect.width),
                            labelH: Math.round(labelRect.height),
                            switches: allSwitches,
                        };
                    }
                    return {found: false, switches: []};
                }""")

                if not diag.get("found"):
                    logger.warning("未找到原创声明文字 (attempt %d)", attempt + 1)
                    page.evaluate("window.scrollBy(0, 200)")
                    time.sleep(1)
                    continue

                switches = diag.get("switches", [])
                logger.info("原创声明 label: (%d,%d) %dx%d, 找到 %d 个 switch 元素",
                            diag["labelX"], diag["labelY"],
                            diag["labelW"], diag["labelH"], len(switches))
                for i, sw in enumerate(switches):
                    logger.info("  switch[%d]: %dx%d at (%d,%d) cls=%s aria=%s",
                                i, sw["w"], sw["h"], sw["cx"], sw["cy"],
                                sw["cls"], sw.get("aria"))

            except Exception as e:
                logger.warning("诊断原创声明失败 (attempt %d): %s", attempt + 1, e)
                time.sleep(1)
                continue

            # Step 3: 选择最佳点击目标
            # 优先选宽度 25-100 的 switch 元素（开关本体），排除 icon（太小）和 card（太大）
            click_x, click_y = None, None
            target_desc = ""

            # 按宽度排序，优先选中等大小的
            sized = sorted(switches, key=lambda s: abs(s["w"] - 40))
            for sw in sized:
                if 20 <= sw["w"] <= 100 and sw["h"] >= 10:
                    click_x, click_y = sw["cx"], sw["cy"]
                    target_desc = f"switch({sw['w']}x{sw['h']}, cls={sw['cls'][:30]})"
                    break

            # 没有合适大小的 switch，用 label 右侧偏移点击
            if click_x is None:
                lx = diag["labelX"] + diag["labelW"]
                ly = diag["labelY"] + diag["labelH"] // 2
                click_x = lx + 30
                click_y = ly
                target_desc = f"label右侧偏移({click_x},{click_y})"

            logger.info("点击目标: %s", target_desc)

            # Step 4: 点击
            page.mouse.click(click_x, click_y)
            time.sleep(1.5)
            self._screenshot("original_after_click.png")

            # Step 5: 验证 — 重新检测开关视觉状态
            # 用背景色/位置变化来判断，而非 class 名（class 名匹配容易误报）
            verify = page.evaluate("""() => {
                const walker = document.createTreeWalker(
                    document.body, NodeFilter.SHOW_TEXT, null, false
                );
                while (walker.nextNode()) {
                    if (walker.currentNode.textContent.trim() !== '原创声明') continue;
                    let row = walker.currentNode.parentElement;
                    for (let d = 0; d < 8 && row; d++) {
                        const sws = row.querySelectorAll(
                            '[class*="switch"], [class*="Switch"], [role="switch"]'
                        );
                        for (const sw of sws) {
                            const r = sw.getBoundingClientRect();
                            if (r.width < 10 || r.height < 8) continue;
                            // 检查 aria-checked
                            if (sw.getAttribute('aria-checked') === 'true') return {on: true, method: 'aria'};
                            // 检查 class 精确匹配（用正则词边界，避免 "icon" 中的 "on" 误判）
                            const cls = ' ' + (sw.className || '').toString() + ' ';
                            const onPatterns = [' is-checked ', ' is-active ', ' is-on ',
                                                ' checked ', ' active ', ' open '];
                            for (const p of onPatterns) {
                                if (cls.includes(p)) return {on: true, method: 'class:' + p.trim()};
                            }
                            // 向上检查父元素
                            let parent = sw.parentElement;
                            for (let u = 0; u < 3 && parent; u++) {
                                if (parent.getAttribute('aria-checked') === 'true')
                                    return {on: true, method: 'parent-aria'};
                                const pcls = ' ' + (parent.className || '').toString() + ' ';
                                for (const p of onPatterns) {
                                    if (pcls.includes(p)) return {on: true, method: 'parent-class:' + p.trim()};
                                }
                                parent = parent.parentElement;
                            }
                            // 检查背景颜色（开启状态通常有彩色背景）
                            const style = window.getComputedStyle(sw);
                            const bg = style.backgroundColor;
                            // 灰色/白色/透明 = OFF; 其他颜色 = ON
                            if (bg && !bg.match(/rgba?\((?:0|128|192|200|204|211|224|230|235|240|245|248|250|255),\s*(?:0|128|192|200|204|211|224|230|235|240|245|248|250|255),\s*(?:0|128|192|200|204|211|224|230|235|240|245|248|250|255)/)) {
                                if (!bg.includes('rgba(0, 0, 0, 0)') && bg !== 'transparent') {
                                    return {on: true, method: 'bgcolor:' + bg};
                                }
                            }
                        }
                        row = row.parentElement;
                    }
                }
                return {on: false, method: 'none'};
            }""")

            is_on = verify.get("on", False)
            logger.info("验证结果: on=%s, method=%s", is_on, verify.get("method"))

            if is_on:
                logger.info("原创声明已成功开启")
                self._screenshot("original_verified.png")
                return

            # Step 6: 其他策略
            logger.warning("点击后仍为关闭 (attempt %d)", attempt + 1)

            # 策略 B: 如果有小元素 (icon 16x16)，尝试直接点它
            for sw in switches:
                if 10 <= sw["w"] <= 25 and sw["h"] >= 10:
                    logger.info("尝试直接点击小元素 (%dx%d at %d,%d)",
                                sw["w"], sw["h"], sw["cx"], sw["cy"])
                    page.mouse.click(sw["cx"], sw["cy"])
                    time.sleep(1.5)
                    break

            # 策略 C: Playwright locator 点击
            try:
                for sel in ["[class*='custom-switch']:not([class*='card']):not([class*='icon'])",
                            "[class*='switch']:not([class*='card']):not([class*='icon'])"]:
                    loc = page.locator(sel)
                    if loc.count() > 0:
                        for i in range(loc.count()):
                            el = loc.nth(i)
                            if el.is_visible():
                                box = el.bounding_box()
                                if box and 20 <= box["width"] <= 100:
                                    el.click(force=True)
                                    logger.info("Playwright click: %s (%dx%d)",
                                                sel, int(box["width"]), int(box["height"]))
                                    time.sleep(1.5)
                                    break
                        break
            except Exception:
                pass

            self._screenshot(f"original_attempt_{attempt + 1}.png")

        self._screenshot("original_final.png")
        logger.warning("原创声明可能未成功勾选，请手动确认")

    def _wait_for_video_page(self):
        """等待视频发布页加载（默认 tab，无需切换）"""
        page = self._page
        logger.info("等待视频发布页加载...")

        # 等待网络空闲
        try:
            page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT)
            logger.info("网络空闲")
        except Exception:
            logger.warning("网络空闲超时，继续...")

        # 等待 skeleton 消失
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

        time.sleep(2)

        # 确认在视频 tab（不切换，默认就是）
        try:
            page.wait_for_selector("input[type='file']", state="attached", timeout=30000)
            logger.info("视频上传区域已就绪")
        except Exception:
            logger.warning("视频上传控件未找到")

        self._screenshot("video_page_ready.png")

    def _upload_video(self, video_path: str):
        """上传视频文件"""
        page = self._page
        logger.info("上传视频...")

        # 找到视频 file input（accept 包含 .mp4）
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
                logger.info("使用第一个文件输入框")

            file_input.set_input_files(video_path)
            logger.info("视频文件已提交上传")
        except Exception as e:
            raise PublishError(f"视频上传失败: {e}") from e

    def _wait_for_video_processed(self):
        """等待视频上传和处理完成（可能需要较长时间）"""
        page = self._page
        logger.info("等待视频处理（最长 3 分钟）...")

        # 视频上传后，页面通常会显示进度或处理状态
        # 等待"上传中"/"转码中"等文字消失，或者标题输入框出现
        start = time.time()
        max_wait = 180  # 3 分钟

        while time.time() - start < max_wait:
            elapsed = int(time.time() - start)

            # 检查编辑表单是否出现（视频处理完成的标志）
            for sel in ["input[placeholder*='标题']", ".el-input__inner",
                        "input[type='text']", "[contenteditable='true']"]:
                try:
                    loc = page.locator(sel)
                    if loc.count() > 0 and loc.first.is_visible():
                        logger.info("视频处理完成，编辑表单已出现 (%ds)", elapsed)
                        return
                except Exception:
                    pass

            # 进度日志
            if elapsed % 15 == 0 and elapsed > 0:
                logger.info("视频处理中... (%ds/%ds)", elapsed, max_wait)
                self._screenshot(f"video_processing_{elapsed}s.png")

            time.sleep(3)

        logger.warning("视频处理等待超时 (%ds)，尝试继续...", max_wait)
        self._screenshot("video_process_timeout.png")

    def _wait_for_publish_button_enabled(self, max_wait: int = 120):
        """
        等待'发布'按钮从 disabled 变为可点击状态
        视频笔记需等待封面生成完成后按钮才会启用
        """
        page = self._page
        logger.info("等待发布按钮可用（封面生成中）...")

        # 使用 wait_for_function 等待：存在 text=发布 的按钮且没有 disabled
        js_check = """() => {
            const btns = [...document.querySelectorAll('button')];
            for (const b of btns) {
                const txt = b.textContent.trim();
                if (txt !== '发布') continue;
                if (b.offsetParent === null) continue;
                const cls = (b.className || '');
                if (b.disabled) continue;
                if (cls.includes('disabled')) continue;
                if (cls.includes('is-disabled')) continue;
                return true;
            }
            return false;
        }"""

        try:
            page.wait_for_function(js_check, timeout=max_wait * 1000)
            logger.info("发布按钮已可用")
        except Exception:
            logger.warning("等待发布按钮超时 (%ds)，尝试继续...", max_wait)
            self._screenshot("btn_disabled_timeout.png")

    # ────────── 诊断 ──────────

    def diagnose(self):
        """诊断发布页，打印所有可交互元素（供 debug 命令调用）"""
        page = self._page
        page.goto(PUBLISH_URL, wait_until="commit")
        self._wait_for_publish_page()

        print("\n" + "=" * 60)
        print("  小红书发布页诊断报告")
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
            "    if(t) r.buttons.push({i, text:t});"
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

        self._screenshot("diagnose.png")
        print(f"\n  截图已保存: logs/diagnose.png")
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

def publish_note(content: XHSContent, headless: bool = False) -> bool:
    """一键发布小红书笔记"""
    with XHSPublisher(headless=headless) as pub:
        pub.login()
        time.sleep(settings.xhs_publish_delay)
        return pub.publish(content)


def publish_video_note(video_path: str, title: str, body: str, headless: bool = False) -> bool:
    """一键发布小红书视频笔记"""
    with XHSPublisher(headless=headless) as pub:
        pub.login()
        time.sleep(settings.xhs_publish_delay)
        return pub.publish_video(video_path, title, body)


def diagnose_page():
    """诊断发布页（debug 命令入口）"""
    with XHSPublisher(headless=False) as pub:
        pub.login()
        pub.diagnose()
        print("\n浏览器保持 15 秒，可手动检查...")
        time.sleep(15)
