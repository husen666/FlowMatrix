"""
抖音创作者中心自动发布模块
使用 Playwright 自动化浏览器，在抖音创作者中心发布图文笔记
核心策略：networkidle + skeleton消失检测 + 轮询选择器 + JS兜底
"""

import tempfile
import time
from pathlib import Path
from typing import List, Optional

import requests as req

from shared.config import get_settings
from shared.utils.exceptions import DouyinPublishError, DouyinLoginTimeoutError
from shared.utils.logger import get_logger
from shared.llm.douyin import DouyinContent
from shared.publisher_base import BasePublisher, NAV_TIMEOUT, ELEMENT_TIMEOUT

settings = get_settings()

logger = get_logger("douyin_publisher")

# ── 抖音创作者平台 URL ──
CREATOR_URL = "https://creator.douyin.com"
LOGIN_URL = f"{CREATOR_URL}/creator-micro/home"
PUBLISH_URL = f"{CREATOR_URL}/creator-micro/content/upload"
# 文章发布页（比图文封面页简单很多，无需处理竖封面模态）
ARTICLE_PUBLISH_URL = (
    f"{CREATOR_URL}/creator-micro/content/post/article"
    "?default-tab=5&enter_from=publish_page&media_type=article&type=new"
)
IMAGE_PUBLISH_URL = ARTICLE_PUBLISH_URL  # 兼容旧引用

COOKIES_FILE = Path(__file__).resolve().parent / "data" / "douyin_cookies.json"



class DouyinPublisher(BasePublisher):
    """抖音自动发布器，支持 with 语句"""

    USER_DATA_DIR = Path(__file__).resolve().parent / "data" / "browser_profile"

    def __init__(self, headless: bool = False):
        super().__init__(headless)

    # ────────── 登录 ──────────

    def login(self):
        """登录抖音创作者中心"""
        page = self._page

        if settings.douyin_cookie and not COOKIES_FILE.exists():
            self._set_cookies_from_string(settings.douyin_cookie)
            logger.info("通过 .env COOKIE 设置登录态")

        logger.info("打开抖音创作者中心...")
        page.goto(CREATOR_URL, wait_until="commit")
        time.sleep(5)

        if self._is_logged_in():
            logger.info("已登录")
            self._save_cookies()
            return

        logger.info("未登录，请在浏览器中扫码...")
        page.goto(LOGIN_URL, wait_until="commit")
        time.sleep(3)

        for i in range(60):
            time.sleep(2)
            if self._is_logged_in():
                logger.info("登录成功！")
                self._save_cookies()
                return
            if i % 10 == 0 and i > 0:
                logger.info("等待扫码... (%ds)", i * 2)

        raise DouyinLoginTimeoutError("抖音登录超时（120秒）")

    def _is_logged_in(self) -> bool:
        url = self._page.url
        if "/login" in url:
            return False
        try:
            body_text = self._page.evaluate("document.body.innerText")
            indicators = ["发布", "首页", "内容管理", "数据中心", "创作者"]
            return any(kw in body_text for kw in indicators)
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
                    "domain": ".douyin.com",
                    "path": "/",
                })
        if cookies:
            self._context.add_cookies(cookies)

    def _save_cookies(self):
        COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._context.storage_state(path=str(COOKIES_FILE))
        logger.info("Cookie 已保存")

    # ────────── 关闭引导弹窗 ──────────

    def _dismiss_cover_dialogs(self):
        """
        处理封面相关弹窗：
        - '设置竖封面' / '设置横封面' 弹窗 → 选择横封面（16:9 视频用横封面）
        - 封面选择/裁剪弹窗 → 确认或跳过
        """
        page = self._page
        time.sleep(2)

        body_text = ""
        try:
            body_text = page.evaluate("document.body.innerText")
        except Exception:
            return

        # ── 处理封面方向选择弹窗 ──
        has_cover_dialog = ("设置竖封面" in body_text or "设置横封面" in body_text
                            or "封面设置" in body_text or "选择封面" in body_text)
        if not has_cover_dialog:
            return

        logger.info("检测到封面设置弹窗...")
        self._screenshot("douyin_cover_dialog.png")

        # 策略 1：选择"横封面"选项（16:9 视频适合横封面）
        for kw in ["横封面", "横版", "16:9"]:
            try:
                loc = page.get_by_text(kw, exact=False)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click()
                    logger.info("已选择'%s'", kw)
                    time.sleep(1)
                    break
            except Exception:
                continue

        # 策略 2：点击确认/完成按钮
        for btn_text in ["确定", "完成", "确认", "使用", "保存"]:
            try:
                btn = page.get_by_role("button", name=btn_text, exact=True)
                if btn.count() > 0 and btn.first.is_visible():
                    btn.first.click()
                    logger.info("已点击封面弹窗'%s'按钮", btn_text)
                    time.sleep(2)
                    self._screenshot("douyin_cover_done.png")
                    return
            except Exception:
                continue

        # 策略 3：跳过/取消封面弹窗
        for close_text in ["跳过", "取消", "稍后设置"]:
            try:
                loc = page.get_by_text(close_text, exact=False)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click()
                    logger.info("已跳过封面弹窗: %s", close_text)
                    time.sleep(2)
                    return
            except Exception:
                continue

        # 策略 4：关闭弹窗 (X 按钮)
        for close_sel in ["[class*='close']", "[class*='Close']", "[aria-label='Close']",
                          "[aria-label='关闭']"]:
            try:
                loc = page.locator(close_sel)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click()
                    logger.info("已关闭封面弹窗")
                    time.sleep(2)
                    return
            except Exception:
                continue

        logger.warning("封面弹窗处理未完成，尝试 Escape 关闭")
        try:
            page.keyboard.press("Escape")
            time.sleep(1)
        except Exception:
            pass

    def _dismiss_guide_popups(self):
        """关闭抖音创作者中心可能弹出的引导弹窗/气泡"""
        page = self._page
        dismiss_texts = ["我知道了", "知道了", "好的", "确定", "跳过", "关闭"]
        for txt in dismiss_texts:
            try:
                loc = page.get_by_text(txt, exact=True)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click()
                    logger.info("已关闭引导弹窗: '%s'", txt)
                    time.sleep(1)
            except Exception:
                continue
        try:
            page.evaluate("""() => {
                const selectors = [
                    '[class*="tooltip"]', '[class*="Tooltip"]',
                    '[class*="popover"]', '[class*="Popover"]',
                    '[class*="guide"]', '[class*="Guide"]',
                    '[class*="tip-"]', '[class*="newbie"]',
                ];
                for (const sel of selectors) {
                    document.querySelectorAll(sel).forEach(el => {
                        if (el.offsetParent !== null) el.style.display = 'none';
                    });
                }
            }""")
        except Exception:
            pass

    # ────────── 等待发布页加载 ──────────

    def _wait_for_publish_page(self):
        """等待发布页完整加载"""
        page = self._page
        logger.info("等待发布页面加载...")

        try:
            page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT)
            logger.info("网络空闲")
        except Exception:
            logger.warning("网络空闲超时，继续...")

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

        # 尝试切换到图文发布模式
        self._switch_to_image_mode()

        self._screenshot("douyin_page_ready.png")

    def _switch_to_image_mode(self):
        """切换到图文发布模式"""
        page = self._page
        logger.info("切换到图文发布模式...")

        tab_texts = ["上传图文", "图文", "发布图文"]
        for txt in tab_texts:
            try:
                loc = page.get_by_text(txt, exact=True)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click()
                    logger.info("已切换到'%s'模式", txt)
                    time.sleep(3)
                    return
            except Exception:
                continue

        # JS 兜底
        try:
            clicked = page.evaluate(
                "() => {"
                "  const all = document.querySelectorAll('*');"
                "  for (const el of all) {"
                "    if (el.children.length === 0 || el.childElementCount === 0) {"
                "      const txt = el.textContent.trim();"
                "      if (txt === '\u4e0a\u4f20\u56fe\u6587' || txt === '\u56fe\u6587') {"
                "        el.click();"
                "        return txt;"
                "      }"
                "    }"
                "  }"
                "  return null;"
                "}"
            )
            if clicked:
                logger.info("已通过 JS 切换: %s", clicked)
                time.sleep(3)
                return
        except Exception:
            pass

        logger.info("未找到图文切换标签，当前可能已是图文模式")

    # ────────── 发布文章（使用文章发布页） ──────────

    def publish(self, content: DouyinContent) -> bool:
        """在抖音创作者中心发布文章
        使用文章发布页（/content/post/article），比旧的图文封面页简单很多
        """
        page = self._page
        logger.info("开始发布文章: %s", content.title)

        try:
            # 1. 打开文章发布页
            logger.info("打开文章发布页...")
            page.goto(ARTICLE_PUBLISH_URL, wait_until="commit")
            self._wait_for_article_page()

            # 1.5. 关闭引导弹窗
            self._dismiss_guide_popups()

            self._screenshot("douyin_page_ready.png")

            # 2. 填写标题
            title = content.title[:30] if len(content.title) > 30 else content.title
            self._fill_article_title(title)

            # 3. 填写正文
            body_text = content.full_text()
            self._fill_article_body(body_text)

            # 4. 上传封面图片（文章页有简单的封面上传区）
            if content.image_urls:
                self._upload_article_cover(content.image_urls[0])

            self._screenshot("douyin_before_publish.png")
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
                self._screenshot("douyin_result.png")
            return success

        except DouyinPublishError:
            self._screenshot("douyin_error.png")
            raise
        except Exception as e:
            self._screenshot("douyin_error.png")
            raise DouyinPublishError(f"文章发布异常: {e}") from e

    # ────────── 文章页等待 ──────────

    def _wait_for_article_page(self):
        """等待文章发布页加载"""
        page = self._page
        logger.info("等待文章发布页加载...")

        try:
            page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT)
            logger.info("网络空闲")
        except Exception:
            logger.warning("网络空闲超时，继续...")

        # 等待骨架屏消失
        try:
            page.wait_for_function(
                "() => {"
                "  const s = document.querySelectorAll("
                "    '[class*=\"skeleton\"],[class*=\"Skeleton\"],[class*=\"loading\"]'"
                "  );"
                "  return s.length === 0 || [...s].every(e => e.offsetParent === null);"
                "}",
                timeout=30000,
            )
        except Exception:
            pass

        time.sleep(3)
        logger.info("文章发布页已就绪")

    # ────────── 文章标题 ──────────

    def _fill_article_title(self, title: str):
        """填写文章标题"""
        page = self._page
        logger.info("填写文章标题: %s", title)

        # 文章页标题输入框
        title_selectors = [
            "input[placeholder*='标题']",
            "input[placeholder*='文章标题']",
            "input[placeholder*='填写']",
            "textarea[placeholder*='标题']",
            "[class*='title'] input",
            "[class*='title'] textarea",
            "input[type='text']",
        ]

        loc = self._wait_for_first(title_selectors, timeout=ELEMENT_TIMEOUT)
        if loc:
            try:
                loc.first.click()
                loc.first.fill(title)
                logger.info("文章标题已填写")
                return
            except Exception as e:
                logger.warning("标题填写被阻挡: %s，尝试 force", e)
                try:
                    loc.first.click(force=True)
                    loc.first.fill(title)
                    logger.info("文章标题已填写 (force)")
                    return
                except Exception:
                    pass

        # JS 兜底
        try:
            page.evaluate(f"""() => {{
                const inputs = document.querySelectorAll('input, textarea');
                for (const inp of inputs) {{
                    const ph = (inp.placeholder || '').toLowerCase();
                    if (ph.includes('标题') || ph.includes('填写')) {{
                        inp.focus();
                        inp.value = '';
                        document.execCommand('insertText', false, {repr(title)});
                        inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                        return true;
                    }}
                }}
                return false;
            }}""")
            logger.info("文章标题已填写 (JS)")
            return
        except Exception:
            pass

        self._screenshot("douyin_title_not_found.png")
        raise DouyinPublishError("未找到文章标题输入框")

    # ────────── 文章正文 ──────────

    def _fill_article_body(self, text: str):
        """填写文章正文（富文本编辑器）"""
        page = self._page
        logger.info("填写文章正文 (%d 字)", len(text))

        # 文章页通常有富文本编辑器
        body_selectors = [
            ".ProseMirror",
            ".ql-editor",
            "[contenteditable='true']",
            ".DraftEditor-root",
            "[class*='editor'] [contenteditable]",
            "[class*='content'] [contenteditable]",
            ".el-textarea__inner",
            "textarea",
        ]

        loc = self._wait_for_first(body_selectors, timeout=ELEMENT_TIMEOUT)
        if loc:
            try:
                loc.first.click()
                loc.first.fill(text)
                logger.info("文章正文已填写")
                return
            except Exception as e:
                logger.warning("正文填充失败: %s，尝试逐字输入", e)
                try:
                    loc.first.click()
                    page.keyboard.press("Control+a")
                    page.keyboard.type(text, delay=5)
                    logger.info("文章正文已填写 (type)")
                    return
                except Exception:
                    pass

        # JS 兜底
        try:
            page.evaluate(f"""() => {{
                const editors = document.querySelectorAll(
                    '.ProseMirror, .ql-editor, [contenteditable="true"], textarea'
                );
                for (const ed of editors) {{
                    if (ed.offsetParent === null) continue;
                    ed.focus();
                    if (ed.tagName === 'TEXTAREA') {{
                        ed.value = {repr(text)};
                        ed.dispatchEvent(new Event('input', {{bubbles: true}}));
                    }} else {{
                        ed.innerHTML = {repr(text)}.split('\\n').map(l => '<p>' + l + '</p>').join('');
                        ed.dispatchEvent(new Event('input', {{bubbles: true}}));
                    }}
                    return true;
                }}
                return false;
            }}""")
            logger.info("文章正文已填写 (JS)")
            return
        except Exception:
            pass

        self._screenshot("douyin_body_not_found.png")
        raise DouyinPublishError("未找到文章正文编辑器")

    # ────────── 文章封面上传 ──────────

    def _upload_article_cover(self, image_source):
        """为文章上传封面图片"""
        page = self._page
        logger.info("上传文章封面...")

        # 准备本地文件
        local_files = self._prepare_local_files([image_source])
        if not local_files:
            logger.info("无封面图片可上传")
            return

        local_file = local_files[0]

        # ── 方法 1: 点击上传封面区域，通过 file chooser 上传 ──
        upload_triggers = [
            "上传封面", "选择封面", "添加封面",
            "上传图片", "添加图片",
        ]
        for txt in upload_triggers:
            try:
                btn = page.get_by_text(txt)
                if btn.count() > 0 and btn.first.is_visible():
                    logger.info("找到「%s」按钮", txt)
                    with page.expect_file_chooser(timeout=10000) as fc_info:
                        btn.first.click()
                    fc = fc_info.value
                    fc.set_files(local_file)
                    logger.info("已通过「%s」上传封面", txt)
                    time.sleep(5)
                    # 处理「编辑封面」对话框：点击「完成」
                    self._handle_cover_edit_dialog()
                    return
            except Exception as e:
                logger.warning("通过「%s」上传失败: %s", txt, e)

        # ── 方法 2: 直接找 file input ──
        try:
            img_inputs = page.locator("input[type='file'][accept*='image']")
            if img_inputs.count() > 0:
                img_inputs.first.set_input_files(local_file)
                logger.info("已通过文件输入框上传封面")
                time.sleep(5)
                self._handle_cover_edit_dialog()
                return
        except Exception as e:
            logger.warning("文件输入框上传失败: %s", e)

        # ── 方法 3: 任意 file input ──
        try:
            all_inputs = page.locator("input[type='file']")
            if all_inputs.count() > 0:
                all_inputs.first.set_input_files(local_file)
                logger.info("已通过通用文件输入框上传封面")
                time.sleep(5)
                self._handle_cover_edit_dialog()
                return
        except Exception as e:
            logger.warning("通用文件输入框上传失败: %s", e)

        logger.warning("封面上传失败，继续发布...")

    def _handle_cover_edit_dialog(self):
        """处理文章页上传封面后弹出的「编辑封面」对话框，点击「完成」确认"""
        page = self._page
        logger.info("检测「编辑封面」对话框...")
        time.sleep(2)

        # 查找「完成」按钮
        for btn_text in ["完成", "确认", "确定", "保存"]:
            try:
                btn = page.get_by_role("button", name=btn_text)
                if btn.count() > 0 and btn.first.is_visible():
                    btn.first.click()
                    logger.info("已点击「编辑封面」对话框中的'%s'", btn_text)
                    time.sleep(3)
                    return
            except Exception:
                continue

        # JS 兜底：查找带红色背景的按钮
        try:
            result = page.evaluate("""() => {
                const buttons = document.querySelectorAll('button');
                for (const btn of buttons) {
                    const text = btn.textContent.trim();
                    if (['完成', '确认', '确定', '保存'].includes(text) && btn.offsetParent !== null) {
                        btn.click();
                        return text;
                    }
                }
                return null;
            }""")
            if result:
                logger.info("已通过 JS 点击'%s'按钮", result)
                time.sleep(3)
                return
        except Exception:
            pass

        # 没有对话框或无法处理，按 Escape 关闭
        try:
            page.keyboard.press("Escape")
            time.sleep(1)
            logger.info("已按 Escape 关闭对话框")
        except Exception:
            pass

    # ────────── 等待编辑表单 ──────────

    def _wait_for_editor(self):
        """上传图片后，等待标题+正文编辑区出现"""
        page = self._page
        logger.info("等待编辑表单加载...")

        editor_selectors = [
            "input[placeholder*='标题']",
            "input[placeholder*='作品标题']",
            ".el-input__inner",
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
            self._screenshot("douyin_editor_not_found.png")
            logger.warning("编辑表单未加载，尝试继续...")

        time.sleep(2)

    def _wait_for_upload_done(self):
        """等待图片处理完成"""
        page = self._page
        logger.info("等待图片处理完成...")
        try:
            page.wait_for_function(
                "() => {"
                "  const body = document.body.innerText;"
                "  return !body.includes('\u52a0\u8f7d\u4e2d') && !body.includes('\u4e0a\u4f20\u4e2d');"
                "}",
                timeout=30000,
            )
            logger.info("图片处理完成")
        except Exception:
            logger.warning("图片处理等待超时，继续...")

    # ────────── 准备本地图片文件 ──────────

    def _prepare_local_files(self, image_sources: list) -> list:
        """将图片源（URL 或本地路径）统一转为本地文件列表"""
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
                except Exception as e:
                    logger.warning("下载失败，跳过: %s - %s", src_str, e)
            else:
                local_path = Path(src_str)
                if local_path.exists():
                    local_files.append(str(local_path))
                else:
                    logger.warning("本地图片不存在，跳过: %s", src_str)
        return local_files

    # ────────── 上传图片 ──────────

    def _upload_images(self, image_sources: list):
        """通过「选择封面」→「设置竖封面」模态 →「上传封面」流程上传图片"""
        page = self._page
        logger.info("准备上传 %d 张图片", len(image_sources))

        local_files = self._prepare_local_files(image_sources)
        if not local_files:
            logger.warning("无图片可上传")
            return

        uploaded_count = 0
        # 最多设置 2 张封面（抖音限制）
        max_covers = min(len(local_files), 2)

        for i in range(max_covers):
            logger.info("上传第 %d/%d 张封面...", i + 1, max_covers)

            # ── 步骤 1: 点击「选择封面」按钮打开「设置竖封面」模态 ──
            modal_opened = self._open_cover_modal()
            if not modal_opened:
                logger.warning("无法打开封面设置模态，跳过第 %d 张", i + 1)
                continue

            time.sleep(2)

            # ── 步骤 2: 在模态中点击「+上传封面」并通过 file chooser 上传 ──
            upload_ok = self._upload_cover_in_modal(local_files[i])
            if not upload_ok:
                logger.warning("第 %d 张封面上传失败", i + 1)
                self._close_any_modal()
                continue

            time.sleep(3)

            # ── 步骤 3: 点击红色保存按钮 ──
            saved = self._click_cover_save_button()
            if saved:
                uploaded_count += 1
                logger.info("第 %d 张封面已保存", i + 1)
            else:
                logger.warning("第 %d 张封面保存失败", i + 1)
                self._close_any_modal()

            time.sleep(3)
            # 确保模态已关闭
            self._close_any_modal()
            time.sleep(2)

        logger.info("封面上传完成: 成功 %d/%d 张", uploaded_count, max_covers)

    def _open_cover_modal(self) -> bool:
        """点击「选择封面」按钮打开「设置竖封面」模态框"""
        page = self._page

        # 先检查是否已经有模态框打开
        if self._is_cover_modal_open():
            logger.info("封面模态框已打开")
            return True

        # 查找并点击「选择封面」按钮
        try:
            cover_btns = page.get_by_text("选择封面")
            for i in range(cover_btns.count()):
                btn = cover_btns.nth(i)
                if btn.is_visible():
                    btn.click()
                    logger.info("已点击「选择封面」按钮")
                    time.sleep(3)

                    # 等待模态框出现
                    for _ in range(10):
                        if self._is_cover_modal_open():
                            return True
                        time.sleep(1)

                    logger.warning("点击「选择封面」后模态框未出现")
                    return False
        except Exception as e:
            logger.warning("点击「选择封面」失败: %s", e)

        return False

    def _is_cover_modal_open(self) -> bool:
        """检测封面设置模态框是否打开（同时检测 Semi 和抖音自有模态系统）"""
        page = self._page
        try:
            # Semi 设计系统的模态
            semi_modal = page.locator('[role="modal"], .semi-modal-wrap, [class*="semi-modal"]')
            if semi_modal.count() > 0 and semi_modal.first.is_visible():
                return True
            # 抖音自有模态系统
            dy_modal = page.locator('.dy-creator-content-modal-wrap, [class*="dy-creator-content-modal"]')
            if dy_modal.count() > 0 and dy_modal.first.is_visible():
                return True
        except Exception:
            pass
        return False

    def _upload_cover_in_modal(self, local_file: str) -> bool:
        """在「设置竖封面」模态框内点击「+上传封面」上传图片"""
        page = self._page
        logger.info("在模态框中上传封面图片...")

        # ── 方法 1: 点击「+上传封面」/「上传封面」按钮 ──
        upload_texts = ["上传封面", "+上传封面", "+ 上传封面"]
        for txt in upload_texts:
            try:
                btn = page.get_by_text(txt)
                if btn.count() > 0 and btn.first.is_visible():
                    logger.info("找到「%s」按钮，点击上传...", txt)
                    with page.expect_file_chooser(timeout=10000) as fc_info:
                        btn.first.click()
                    file_chooser = fc_info.value
                    file_chooser.set_files(local_file)
                    logger.info("已通过「%s」上传图片", txt)
                    time.sleep(5)
                    return True
            except Exception as e:
                logger.warning("通过「%s」上传失败: %s", txt, e)

        # ── 方法 2: 在模态框内查找 file input ──
        try:
            # 在模态相关的 portal 中查找 file input
            modal_file_inputs = page.locator(
                '.dy-creator-content-portal input[type="file"], '
                '.semi-portal input[type="file"], '
                '[role="modal"] input[type="file"]'
            )
            if modal_file_inputs.count() > 0:
                modal_file_inputs.first.set_input_files(local_file)
                logger.info("已通过模态框内文件输入框上传图片")
                time.sleep(5)
                return True
        except Exception as e:
            logger.warning("模态框内文件输入框上传失败: %s", e)

        # ── 方法 3: 尝试任意 file input（带 image accept） ──
        try:
            img_inputs = page.locator("input[type='file'][accept*='image']")
            if img_inputs.count() > 0:
                img_inputs.first.set_input_files(local_file)
                logger.info("已通过图片文件输入框上传图片")
                time.sleep(5)
                return True
        except Exception as e:
            logger.warning("图片文件输入框上传失败: %s", e)

        logger.warning("所有上传方式均失败")
        return False

    def _close_any_modal(self):
        """关闭任何打开的模态框（Semi 和抖音自有模态系统）"""
        page = self._page

        # 先尝试点击「取消」或关闭按钮
        for btn_text in ["取消"]:
            try:
                btn = page.get_by_text(btn_text, exact=True)
                if btn.count() > 0 and btn.first.is_visible():
                    btn.first.click()
                    logger.info("已点击'%s'关闭模态", btn_text)
                    time.sleep(2)
                    if not self._is_cover_modal_open():
                        return
            except Exception:
                pass

        # 按 Escape
        try:
            page.keyboard.press("Escape")
            time.sleep(1)
            if not self._is_cover_modal_open():
                return
        except Exception:
            pass

        # 点击关闭按钮（×）
        try:
            close_btns = page.locator(
                '.semi-modal-close, .dy-creator-content-modal-wrap [class*="close"], '
                '[class*="modal"] [class*="close-icon"], [class*="modal"] .close'
            )
            if close_btns.count() > 0:
                close_btns.first.click(force=True)
                time.sleep(1)
        except Exception:
            pass

        # JS 兜底：隐藏所有模态覆盖
        try:
            page.evaluate("""() => {
                // 隐藏 Semi portal 模态
                document.querySelectorAll('.semi-portal').forEach(el => {
                    const modal = el.querySelector('[role="modal"], .semi-modal-wrap');
                    if (modal) el.style.display = 'none';
                });
                // 隐藏抖音自有模态
                document.querySelectorAll('.dy-creator-content-portal').forEach(el => {
                    const modal = el.querySelector('.dy-creator-content-modal-wrap, [class*="modal"]');
                    if (modal) el.style.display = 'none';
                });
                // 隐藏预览覆盖层
                document.querySelectorAll('[class*="preview-"]').forEach(el => {
                    if (el.closest('.dy-creator-content-portal')) {
                        el.style.display = 'none';
                    }
                });
            }""")
            logger.info("JS 兜底关闭模态")
        except Exception:
            pass

    # ────────── 处理封面设置对话框 ──────────

    def _dismiss_cover_crop_modal(self):
        """确保所有封面设置模态框已关闭"""
        page = self._page
        logger.info("确保封面模态框已关闭...")
        time.sleep(1)

        # 多次尝试关闭
        for _ in range(3):
            if not self._is_cover_modal_open():
                logger.info("无封面模态框")
                return
            self._close_any_modal()
            time.sleep(2)

        logger.warning("封面模态框可能仍然存在")

    def _click_cover_save_button(self) -> bool:
        """点击封面设置对话框中的红色确认/保存按钮"""
        page = self._page
        logger.info("点击封面保存按钮...")

        # 方法 1: 通过 JS 查找 Semi 和抖音模态框中的红色/主要按钮
        try:
            result = page.evaluate("""() => {
                // 搜索范围：Semi 模态 + 抖音自有模态
                const modalSelectors = [
                    '.semi-modal-wrap', '[role="modal"]',
                    '.dy-creator-content-modal-wrap', '.dy-creator-content-portal'
                ];
                for (const sel of modalSelectors) {
                    const modals = document.querySelectorAll(sel);
                    for (const modal of modals) {
                        if (modal.style.display === 'none') continue;
                        const rect = modal.getBoundingClientRect();
                        if (rect.width === 0 && rect.height === 0) continue;

                        const buttons = modal.querySelectorAll('button');
                        let primaryBtn = null;

                        for (const btn of buttons) {
                            const text = btn.textContent.trim();
                            const style = window.getComputedStyle(btn);
                            const bgColor = style.backgroundColor;

                            if (text === '取消' || text === '封面检测') continue;
                            if (btn.offsetParent === null) continue;

                            // 红色/主色调按钮
                            if (bgColor.includes('254') || bgColor.includes('255') ||
                                bgColor.includes('fe2') || bgColor.includes('ff0') ||
                                btn.classList.toString().match(/primary|danger|confirm/i)) {
                                primaryBtn = btn;
                                break;
                            }

                            if (['保存', '确认', '完成', '确定'].includes(text)) {
                                primaryBtn = btn;
                            }
                        }

                        if (primaryBtn) {
                            primaryBtn.click();
                            return {clicked: true, text: primaryBtn.textContent.trim(),
                                    bg: window.getComputedStyle(primaryBtn).backgroundColor};
                        }
                    }
                }
                return {clicked: false};
            }""")
            if result.get("clicked"):
                logger.info("已点击封面保存按钮: %s", result.get("text"))
                return True
        except Exception as e:
            logger.warning("JS 点击保存按钮失败: %s", e)

        # 方法 2: 通过文本查找按钮
        for btn_text in ["保存", "确认", "完成", "确定"]:
            try:
                btn = page.get_by_role("button", name=btn_text)
                if btn.count() > 0 and btn.first.is_visible():
                    btn.first.click(force=True)
                    logger.info("已点击'%s'按钮", btn_text)
                    return True
            except Exception:
                continue

        logger.warning("未找到封面保存按钮")
        return False

    # ────────── 验证封面 ──────────

    def _verify_covers(self):
        """验证封面图片是否已成功上传（检查「选择封面」空槽位是否还在）"""
        page = self._page
        time.sleep(2)

        try:
            # 检查是否仍有空的「选择封面」按钮
            empty_slots = page.get_by_text("选择封面")
            empty_count = 0
            for i in range(empty_slots.count()):
                try:
                    if empty_slots.nth(i).is_visible():
                        empty_count += 1
                except Exception:
                    pass

            if empty_count == 0:
                logger.info("封面验证通过：所有封面槽位已填充")
                return

            # 仍有空槽位，检查是否有已上传的图片（通过 img 标签或缩略图）
            cover_info = page.evaluate("""() => {
                const body = document.body.innerText;
                const imgs = document.querySelectorAll('img[src*="cover"], img[src*="image"], img[class*="cover"]');
                const thumbs = document.querySelectorAll('[class*="cover"] img, [class*="thumb"] img');
                return {
                    emptySlotTexts: body.match(/选择封面/g)?.length || 0,
                    coverImgs: imgs.length,
                    thumbImgs: thumbs.length,
                };
            }""")
            logger.info("封面状态: 空槽位=%d, 封面图=%d, 缩略图=%d",
                         cover_info.get("emptySlotTexts", 0),
                         cover_info.get("coverImgs", 0),
                         cover_info.get("thumbImgs", 0))

            if empty_count > 0 and cover_info.get("coverImgs", 0) == 0 and cover_info.get("thumbImgs", 0) == 0:
                logger.warning("封面可能未成功上传！仍有 %d 个空槽位", empty_count)
        except Exception as e:
            logger.warning("封面验证异常: %s", e)

    # ────────── 移除覆盖层阻挡 ──────────

    def _remove_overlay_blockers(self):
        """移除可能阻挡交互的覆盖层（模态框残余、预览面板等）"""
        page = self._page
        try:
            page.evaluate("""() => {
                // 移除/隐藏 抖音 portal 中的覆盖层
                document.querySelectorAll('.dy-creator-content-portal').forEach(el => {
                    const modal = el.querySelector(
                        '.dy-creator-content-modal-wrap, [class*="modal-wrap"], [class*="preview-"]'
                    );
                    if (modal) {
                        el.style.display = 'none';
                        el.style.pointerEvents = 'none';
                    }
                });

                // 移除 Semi portal 中的残留模态
                document.querySelectorAll('.semi-portal').forEach(el => {
                    const modal = el.querySelector('[role="modal"], .semi-modal-wrap');
                    if (modal) {
                        el.style.display = 'none';
                        el.style.pointerEvents = 'none';
                    }
                });

                // 移除 ReactCrop 覆盖
                document.querySelectorAll('.ReactCrop').forEach(el => {
                    const portal = el.closest('.semi-portal, .dy-creator-content-portal');
                    if (portal) {
                        portal.style.display = 'none';
                        portal.style.pointerEvents = 'none';
                    }
                });

                // 通用：移除带 pointer-events 拦截的大覆盖层
                document.querySelectorAll('[class*="preview-"], [class*="modal-mask"], [class*="overlay"]').forEach(el => {
                    const rect = el.getBoundingClientRect();
                    if (rect.width > 500 && rect.height > 300) {
                        const portal = el.closest('.dy-creator-content-portal, .semi-portal');
                        if (portal) {
                            portal.style.display = 'none';
                        }
                    }
                });
            }""")
            logger.info("已清理覆盖层")
        except Exception as e:
            logger.warning("清理覆盖层失败: %s", e)

    # ────────── 填写标题 ──────────

    def _fill_title(self, title: str):
        """填写标题"""
        page = self._page
        logger.info("填写标题: %s", title)

        # 先确保没有覆盖层阻挡
        self._remove_overlay_blockers()

        title_selectors = [
            "input[placeholder*='标题']",
            "input[placeholder*='作品标题']",
            "input[placeholder*='填写']",
            ".el-input__inner",
            "[class*='title'] input",
            "[class*='Title'] input",
            "input[type='text']",
        ]
        loc = self._wait_for_first(title_selectors, timeout=ELEMENT_TIMEOUT)
        if loc:
            try:
                loc.first.click()
                loc.first.fill(title)
                logger.info("标题已填写")
                return
            except Exception as e:
                logger.warning("标题点击被阻挡: %s，尝试强制点击", e)
                self._remove_overlay_blockers()
                try:
                    loc.first.click(force=True)
                    loc.first.fill(title)
                    logger.info("标题已填写 (force)")
                    return
                except Exception:
                    pass

        # contenteditable
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

        # JS 兜底
        try:
            handle = page.evaluate_handle(
                "() => {"
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
        except Exception as e:
            logger.debug("_fill_title JS fallback failed: %s", e)

        self._screenshot("douyin_title_not_found.png")
        raise DouyinPublishError("未找到标题输入框")

    # ────────── 填写正文 ──────────

    def _fill_body(self, text: str):
        """填写正文"""
        page = self._page
        logger.info("填写正文 (%d 字)", len(text))

        body_selectors = [
            ".ProseMirror",
            ".ql-editor",
            "[contenteditable='true'][class*='editor']",
            "[contenteditable='true'][class*='content']",
            "[contenteditable='true'][class*='desc']",
            ".el-textarea__inner",
            "textarea[placeholder*='描述']",
            "textarea[placeholder*='添加']",
            "textarea",
        ]
        loc = self._wait_for_first(body_selectors, timeout=ELEMENT_TIMEOUT)
        if loc:
            loc.first.click()
            loc.first.fill(text)
            logger.info("正文已填写")
            return

        # 兜底：取最后一个 contenteditable
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

        self._screenshot("douyin_body_not_found.png")
        raise DouyinPublishError("未找到正文编辑器")

    # ────────── 点击发布 ──────────

    def _click_publish(self):
        """点击发布按钮（精简版：优先精确匹配，避免误触导航）"""
        page = self._page
        logger.info("点击发布...")

        try:
            page.keyboard.press("Escape")
            time.sleep(0.5)
        except Exception:
            pass

        self._screenshot("douyin_before_click_publish.png")

        # 滚动到底部确保发布按钮可见
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(1)
        except Exception:
            pass

        # 策略 1：JS 精确匹配"发布"按钮（页面最底部的那个）
        try:
            result = page.evaluate("""() => {
                const btns = [...document.querySelectorAll('button')];
                // 精确匹配：文字恰好是"发布"
                const exact = btns.filter(b => {
                    const txt = b.textContent.trim();
                    return txt === '发布' && b.offsetParent !== null && !b.disabled;
                });
                // 取 y 坐标最大的（页面最底部的发布按钮）
                if (exact.length > 0) {
                    const best = exact.reduce((a, b) =>
                        a.getBoundingClientRect().top > b.getBoundingClientRect().top ? a : b
                    );
                    const r = best.getBoundingClientRect();
                    return {found: true, x: r.x + r.width/2, y: r.y + r.height/2, text: '发布'};
                }
                // 宽松匹配：文字包含"发布"但不含"定时"
                const fuzzy = btns.filter(b => {
                    const txt = b.textContent.trim();
                    return txt.includes('发布') && !txt.includes('定时')
                        && b.offsetParent !== null && !b.disabled;
                });
                if (fuzzy.length > 0) {
                    const best = fuzzy.reduce((a, b) =>
                        a.getBoundingClientRect().top > b.getBoundingClientRect().top ? a : b
                    );
                    const r = best.getBoundingClientRect();
                    return {found: true, x: r.x + r.width/2, y: r.y + r.height/2,
                            text: best.textContent.trim().slice(0, 20)};
                }
                return {found: false};
            }""")
            if result.get("found"):
                page.mouse.click(result["x"], result["y"])
                logger.info("已点击 '%s' 按钮", result.get("text", "发布"))
                time.sleep(3)
                self._handle_publish_dialog()
                return
        except Exception as e:
            logger.debug("JS 精确定位发布按钮失败: %s", e)

        # 策略 2：Playwright role 定位
        for btn_text in ["发布", "发布作品"]:
            try:
                loc = page.get_by_role("button", name=btn_text, exact=True)
                if loc.count() > 0:
                    target = loc.last
                    if target.is_visible():
                        target.scroll_into_view_if_needed()
                        time.sleep(0.5)
                        target.click(force=True)
                        logger.info("已点击 '%s' 按钮 (Playwright role)", btn_text)
                        time.sleep(3)
                        self._handle_publish_dialog()
                        return
            except Exception:
                continue

        # 诊断并报错
        self._screenshot("douyin_btn_not_found.png")
        try:
            diag = page.evaluate("""() => {
                return [...document.querySelectorAll('button')].filter(b =>
                    b.offsetParent !== null && b.textContent.trim()
                ).map(b => ({text: b.textContent.trim().slice(0, 30)}));
            }""")
            logger.warning("页面按钮列表: %s", diag)
        except Exception:
            pass

        raise DouyinPublishError("未找到发布按钮")

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

    # ────────── 检查结果 ──────────

    def _check_publish_success(self) -> bool:
        """多策略检测发布是否成功"""
        page = self._page

        for wait_sec in (2, 3, 5):
            time.sleep(wait_sec)
            url = page.url.lower()

            if "upload" not in url and "publish" not in url:
                logger.info("发布成功（页面已跳转: %s）", page.url)
                return True

            try:
                body_text = page.evaluate("document.body.innerText")
                success_keywords = [
                    "发布成功", "已发布", "作品发布成功",
                    "内容管理", "作品管理",
                ]
                for keyword in success_keywords:
                    if keyword in body_text:
                        logger.info("发布成功（检测到: %s）", keyword)
                        return True
            except Exception:
                pass

        self._screenshot("douyin_publish_uncertain.png")
        return False

    # ────────── 视频发布 ──────────

    def publish_video(self, video_path: str, title: str, body: str) -> bool:
        """在抖音创作者中心发布视频"""
        page = self._page
        video_file = Path(video_path)
        if not video_file.exists():
            raise DouyinPublishError(f"视频文件不存在: {video_path}")

        logger.info("开始发布视频: %s", title)
        logger.info("视频文件: %s (%.1fMB)", video_file.name,
                     video_file.stat().st_size / 1024 / 1024)

        try:
            # 1. 打开上传页（默认是视频上传）
            logger.info("打开上传页面...")
            page.goto(PUBLISH_URL, wait_until="commit")
            self._wait_for_video_page()

            # 1.5. 关闭引导弹窗
            self._dismiss_guide_popups()
            time.sleep(2)

            # 2. 上传视频
            self._upload_video(str(video_file))

            # 3. 等待视频处理
            self._wait_for_video_processed()
            self._wait_for_editor()

            # 3.5 关闭"设置竖封面"等弹窗
            self._dismiss_cover_dialogs()

            # 4. 填写标题
            safe_title = title[:30] if len(title) > 30 else title
            self._fill_title(safe_title)

            # 5. 填写正文
            self._fill_body(body)

            # 收起可能的下拉面板（用 Escape 而非 mouse.click 避免触发导航）
            try:
                page.keyboard.press("Escape")
                time.sleep(1)
            except Exception:
                pass

            # 6. 声明原创
            self._declare_original()

            self._screenshot("douyin_video_before_publish.png")
            time.sleep(2)

            # 7. 点击发布
            self._click_publish()
            time.sleep(5)

            # 7. 检查结果
            success = self._check_publish_success()
            if success:
                logger.info("视频发布成功！")
            else:
                logger.warning("视频发布结果不确定，请手动检查")
                self._screenshot("douyin_video_result.png")
            return success

        except DouyinPublishError:
            self._screenshot("douyin_video_error.png")
            raise
        except Exception as e:
            self._screenshot("douyin_video_error.png")
            raise DouyinPublishError(f"视频发布异常: {e}") from e

    def _declare_original(self):
        """勾选原创声明（增强版：展开更多设置 + JS 深度搜索）"""
        page = self._page
        logger.info("尝试勾选原创声明...")

        # Step 0: 展开"更多设置"（抖音视频页的原创通常在折叠区域内）
        self._expand_more_options()

        # Step 1: 充分滚动让所有选项可见
        for scroll_y in [300, 600, 900, 1200, 1600]:
            try:
                page.evaluate(f"window.scrollTo(0, {scroll_y})")
                time.sleep(0.3)
            except Exception:
                pass

        # Step 2: JS 全页面深度搜索"原创"并点击对应控件
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
                            'input[type="checkbox"], input[type="radio"], [role="switch"], [role="checkbox"], ' +
                            '[class*="switch"], [class*="Switch"], [class*="toggle"], [class*="Toggle"], ' +
                            '[class*="check"], [class*="Check"], [class*="semi-switch"], [class*="Semi"]'
                        );
                        for (const ctrl of controls) {
                            const rect = ctrl.getBoundingClientRect();
                            if (rect.width === 0 || rect.height === 0) continue;
                            const checked = ctrl.getAttribute('aria-checked') || String(ctrl.checked);
                            if (checked === 'true') return {status: 'already_checked'};
                            ctrl.click();
                            return {status: 'clicked_control', tag: ctrl.tagName,
                                    cls: (ctrl.className||'').toString().slice(0,60)};
                        }
                        cur = cur.parentElement;
                    }
                    el.click();
                    return {status: 'clicked_text', text: text.slice(0, 30)};
                }
                return {status: 'not_found'};
            }""")
            status = result.get('status', 'not_found')
            if status == 'already_checked':
                logger.info("原创已勾选")
                return
            elif status in ('clicked_control', 'clicked_text'):
                logger.info("已勾选原创声明 (JS: %s)", status)
                time.sleep(1)
                self._screenshot("dy_original_clicked.png")
                return
        except Exception as e:
            logger.warning("JS 原创搜索失败: %s", e)

        # Step 3: CSS 兜底
        for sel in ['[class*="original"]', '[class*="yuanchuang"]',
                    'label:has-text("原创")', '[class*="Original"]']:
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click()
                    logger.info("已勾选原创 (sel=%s)", sel)
                    time.sleep(1)
                    return
            except Exception:
                continue

        self._screenshot("dy_original_not_found.png")
        logger.warning("未找到原创声明选项")

    def _expand_more_options(self):
        """展开'更多设置'/'更多选项'等折叠区域（抖音视频页关键步骤）"""
        page = self._page
        # 抖音的"更多设置"按钮
        for txt in ["更多设置", "更多选项", "高级设置", "展开更多", "更多配置"]:
            try:
                loc = page.get_by_text(txt, exact=False)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click()
                    logger.info("已展开: %s", txt)
                    time.sleep(2)
                    return
            except Exception:
                continue
        # JS 兜底：查找可折叠区域
        try:
            page.evaluate("""() => {
                const all = document.querySelectorAll('*');
                for (const el of all) {
                    const txt = el.textContent.trim();
                    if (el.children.length > 3) continue;
                    if (['更多设置','更多选项','高级设置','展开更多'].includes(txt)) {
                        if (el.offsetParent !== null) { el.click(); return true; }
                    }
                }
                // 查找带有展开/折叠图标的区域
                const arrows = document.querySelectorAll('[class*="arrow"], [class*="Arrow"], [class*="expand"], [class*="Expand"], [class*="collapse"]');
                for (const a of arrows) {
                    const parent = a.parentElement;
                    if (parent && parent.textContent.includes('设置') && a.offsetParent !== null) {
                        parent.click();
                        return true;
                    }
                }
                return false;
            }""")
            time.sleep(2)
        except Exception:
            pass

    def _wait_for_video_page(self):
        """等待视频上传页加载"""
        page = self._page
        logger.info("等待视频上传页加载...")

        try:
            page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT)
        except Exception:
            pass

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
        try:
            page.wait_for_selector("input[type='file']", state="attached", timeout=30000)
            logger.info("上传区域已就绪")
        except Exception:
            logger.warning("上传控件未找到")

    def _upload_video(self, video_path: str):
        """上传视频文件"""
        page = self._page
        logger.info("上传视频...")

        # 策略 1: 直接找 input[type='file']
        try:
            page.wait_for_selector("input[type='file']", state="attached", timeout=15000)
            all_inputs = page.locator("input[type='file']")
            file_input = None
            for i in range(all_inputs.count()):
                inp = all_inputs.nth(i)
                accept = inp.get_attribute("accept") or ""
                if "mp4" in accept or "mov" in accept or "video" in accept:
                    file_input = inp
                    break
            if not file_input:
                file_input = all_inputs.first

            file_input.set_input_files(video_path)
            logger.info("视频文件已提交上传 (策略1: 文件输入框)")
            return
        except Exception as e:
            logger.warning("策略1 文件输入框未找到: %s", e)

        # 策略 2: 通过 file_chooser 事件触发上传按钮
        try:
            logger.info("尝试策略2: 点击上传按钮触发文件选择器...")
            upload_btn_selectors = [
                'text=上传视频',
                'text=点击上传',
                'text=上传',
                '[class*="upload"] button',
                '[class*="upload-btn"]',
                '[class*="uploader"] button',
                'button:has-text("上传")',
            ]
            for sel in upload_btn_selectors:
                try:
                    loc = page.locator(sel)
                    if loc.count() > 0 and loc.first.is_visible():
                        with page.expect_file_chooser(timeout=10000) as fc_info:
                            loc.first.click()
                        file_chooser = fc_info.value
                        file_chooser.set_files(video_path)
                        logger.info("视频文件已提交上传 (策略2: 文件选择器 via '%s')", sel)
                        return
                except Exception:
                    continue
        except Exception as e:
            logger.warning("策略2 文件选择器失败: %s", e)

        # 策略 3: 找到拖拽区域通过 JS 注入 file input
        try:
            logger.info("尝试策略3: 注入文件输入框...")
            page.evaluate("""(videoPath) => {
                const input = document.createElement('input');
                input.type = 'file';
                input.style.display = 'none';
                document.body.appendChild(input);
            }""", video_path)
            # 找到新注入的 input
            all_inputs = page.locator("input[type='file']")
            if all_inputs.count() > 0:
                all_inputs.last.set_input_files(video_path)
                logger.info("视频文件已提交上传 (策略3: JS注入)")
                return
        except Exception as e:
            logger.warning("策略3 失败: %s", e)

        # 截图诊断
        self._screenshot("douyin_upload_failed.png")
        # 记录页面状态
        try:
            logger.debug("页面文本长度: %d 字符", len(page.inner_text("body")))
            all_btns = page.locator("button")
            visible_count = sum(1 for i in range(min(all_btns.count(), 10)) 
                               if all_btns.nth(i).is_visible())
            logger.debug("页面上可见按钮数量: %d", visible_count)
        except Exception:
            pass

        raise DouyinPublishError("视频上传失败: 所有上传策略均失败")

    def _wait_for_video_processed(self):
        """等待视频上传和处理完成"""
        page = self._page
        logger.info("等待视频处理（最长 3 分钟）...")

        start = time.time()
        max_wait = 180

        while time.time() - start < max_wait:
            elapsed = int(time.time() - start)

            for sel in ["input[placeholder*='标题']", ".el-input__inner",
                        "input[type='text']", "[contenteditable='true']"]:
                try:
                    loc = page.locator(sel)
                    if loc.count() > 0 and loc.first.is_visible():
                        logger.info("视频处理完成 (%ds)", elapsed)
                        return
                except Exception:
                    pass

            if elapsed % 15 == 0 and elapsed > 0:
                logger.info("视频处理中... (%ds/%ds)", elapsed, max_wait)

            time.sleep(3)

        logger.warning("视频处理等待超时 (%ds)", max_wait)

    # ────────── 诊断 ──────────

    def diagnose(self):
        """诊断发布页面元素"""
        page = self._page
        page.goto(IMAGE_PUBLISH_URL, wait_until="commit")
        self._wait_for_publish_page()

        print("\n" + "=" * 60)
        print("  抖音创作者发布页诊断报告")
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

        self._screenshot("douyin_diagnose.png")
        print(f"\n  截图已保存: logs/douyin_diagnose.png")
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

def publish_douyin_note(content: DouyinContent, headless: bool = False) -> bool:
    """一键发布抖音图文笔记"""
    with DouyinPublisher(headless=headless) as pub:
        pub.login()
        time.sleep(settings.douyin_publish_delay)
        return pub.publish(content)


def publish_douyin_video(video_path: str, title: str, body: str,
                         headless: bool = False) -> bool:
    """一键发布抖音视频"""
    with DouyinPublisher(headless=headless) as pub:
        pub.login()
        time.sleep(settings.douyin_publish_delay)
        return pub.publish_video(video_path, title, body)


def diagnose_douyin_page():
    """诊断发布页面（debug 命令入口）"""
    with DouyinPublisher(headless=False) as pub:
        pub.login()
        pub.diagnose()
        print("\n浏览器保持 15 秒，可手动检查...")
        time.sleep(15)
