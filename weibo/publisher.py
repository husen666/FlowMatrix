"""
微博自动发布模块
使用 Playwright 自动化浏览器，在微博发布文章和视频
支持两种发布形式：
  1. 微博头条文章（长文章）—— 通过 card.weibo.com/article 编辑器
  2. 微博视频 —— 通过 weibo.com 视频上传
核心策略：networkidle + skeleton消失检测 + 轮询选择器 + JS兜底
"""

import tempfile
import time
from pathlib import Path
from typing import List, Optional

import requests as req

from shared.config import get_settings
from shared.utils.exceptions import WeiboPublishError, WeiboLoginTimeoutError
from shared.utils.logger import get_logger
from shared.llm.weibo import WeiboContent
from shared.publisher_base import BasePublisher, NAV_TIMEOUT, ELEMENT_TIMEOUT

settings = get_settings()

logger = get_logger("weibo_publisher")

# ── 微博 URL ──
WEIBO_URL = "https://weibo.com"
LOGIN_URL = "https://passport.weibo.com/sso/signin"
# 头条文章编辑器
ARTICLE_EDITOR_URL = "https://card.weibo.com/article/v3/editor"
# 视频上传（微博创作者中心）
VIDEO_UPLOAD_URL = "https://weibo.com"

COOKIES_FILE = Path(__file__).resolve().parent / "data" / "weibo_cookies.json"



class WeiboPublisher(BasePublisher):
    """微博自动发布器，支持 with 语句"""

    USER_DATA_DIR = Path(__file__).resolve().parent / "data" / "browser_profile"

    def __init__(self, headless: bool = False):
        super().__init__(headless)

    # ────────── 登录 ──────────

    def login(self):
        """登录微博"""
        page = self._page

        if settings.weibo_cookie and not COOKIES_FILE.exists():
            self._set_cookies_from_string(settings.weibo_cookie)
            logger.info("通过 .env COOKIE 设置登录态")

        logger.info("打开微博首页...")
        page.goto(WEIBO_URL, wait_until="commit")
        time.sleep(5)

        if self._is_logged_in():
            logger.info("已登录")
            self._save_cookies()
            return

        logger.info("未登录，请在浏览器中扫码或手动登录...")
        page.goto(LOGIN_URL, wait_until="commit")
        time.sleep(3)

        # 尝试切换到扫码登录
        try:
            qr_btn = page.get_by_text("扫码登录")
            if qr_btn.count() > 0 and qr_btn.first.is_visible():
                qr_btn.first.click()
                time.sleep(2)
        except Exception as e:
            logger.debug("切换到扫码登录失败: %s", e)

        for i in range(90):
            time.sleep(2)
            if self._is_logged_in():
                logger.info("登录成功！")
                self._save_cookies()
                return
            if i % 10 == 0 and i > 0:
                logger.info("等待登录... (%ds)", i * 2)

        raise WeiboLoginTimeoutError("微博登录超时（180秒）")

    def _is_logged_in(self) -> bool:
        """检测是否已登录（增强：同时检查 URL 和页面内容）"""
        url = self._page.url
        # 明确在登录页
        if "passport.weibo.com" in url and "signin" in url:
            return False
        if "/login" in url:
            return False
        try:
            body_text = self._page.evaluate("document.body.innerText")
            # 登录页特征
            login_indicators = ["扫码登录", "密码登录", "请输入手机号", "请输入密码"]
            if any(kw in body_text for kw in login_indicators):
                return False
            # 已登录特征
            logged_in_indicators = ["首页", "热门", "私信", "消息", "我的",
                                     "创作者中心", "发布", "微博", "关注"]
            return any(kw in body_text for kw in logged_in_indicators)
        except Exception:
            return "passport" not in url and "login" not in url

    def _set_cookies_from_string(self, cookie_str: str):
        """从字符串设置 cookies"""
        cookies = []
        for pair in cookie_str.split(";"):
            pair = pair.strip()
            if "=" in pair:
                name, value = pair.split("=", 1)
                cookies.append({
                    "name": name.strip(),
                    "value": value.strip(),
                    "domain": ".weibo.com",
                    "path": "/",
                })
        if cookies:
            self._context.add_cookies(cookies)

    def _save_cookies(self):
        COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._context.storage_state(path=str(COOKIES_FILE))
        logger.info("Cookie 已保存")

    # ────────── 关闭引导弹窗 ──────────

    def _dismiss_guide_popups(self):
        """关闭微博可能弹出的引导弹窗"""
        page = self._page
        dismiss_texts = ["我知道了", "知道了", "好的", "确定", "跳过", "关闭", "不再提示"]
        for txt in dismiss_texts:
            try:
                loc = page.get_by_text(txt, exact=True)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click()
                    logger.info("已关闭引导弹窗: '%s'", txt)
                    time.sleep(1)
            except Exception:
                continue
        # JS 兜底：隐藏弹窗层
        try:
            page.evaluate("""() => {
                const selectors = [
                    '[class*="tooltip"]', '[class*="Tooltip"]',
                    '[class*="popover"]', '[class*="Popover"]',
                    '[class*="guide"]', '[class*="Guide"]',
                    '[class*="dialog"]', '[class*="Dialog"]',
                    '[class*="layer"]',
                ];
                for (const sel of selectors) {
                    document.querySelectorAll(sel).forEach(el => {
                        if (el.offsetParent !== null &&
                            el.getBoundingClientRect().width > 100) {
                            el.style.display = 'none';
                        }
                    });
                }
            }""")
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════════
    #  文章发布（微博头条文章）
    # ══════════════════════════════════════════════════════════════

    def publish(self, content: WeiboContent) -> bool:
        """发布微博头条文章"""
        page = self._page
        logger.info("开始发布微博文章: %s", content.title)

        try:
            # 1. 打开文章编辑器
            logger.info("打开微博文章编辑器...")
            page.goto(ARTICLE_EDITOR_URL, wait_until="commit")
            self._wait_for_article_page()

            # 1.5. 关闭引导弹窗
            self._dismiss_guide_popups()
            self._screenshot("weibo_article_ready.png")

            # 2. 填写标题
            title = content.title[:40] if len(content.title) > 40 else content.title
            self._fill_article_title(title)

            # 3. 填写正文
            self._fill_article_body(content.body)

            # 4. 上传封面
            if content.cover_urls:
                self._upload_article_cover(content.cover_urls[0])

            # 5. 填写摘要
            if content.summary:
                self._fill_article_summary(content.summary)

            self._screenshot("weibo_article_before_publish.png")
            time.sleep(2)

            # 6. 点击发布
            self._click_article_publish()
            time.sleep(5)

            # 7. 检查结果
            success = self._check_article_publish_success()
            if success:
                logger.info("文章发布成功！")
            else:
                logger.warning("发布结果不确定，请手动检查")
                self._screenshot("weibo_article_result.png")
            return success

        except WeiboPublishError:
            self._screenshot("weibo_article_error.png")
            raise
        except Exception as e:
            self._screenshot("weibo_article_error.png")
            raise WeiboPublishError(f"文章发布异常: {e}") from e

    def _wait_for_article_page(self):
        """等待文章编辑器加载"""
        page = self._page
        logger.info("等待文章编辑器加载...")

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
        logger.info("文章编辑器已就绪")

    def _fill_article_title(self, title: str):
        """填写文章标题"""
        page = self._page
        logger.info("填写文章标题: %s", title)

        title_selectors = [
            "input[placeholder*='标题']",
            "input[placeholder*='文章标题']",
            "input[placeholder*='请输入标题']",
            "textarea[placeholder*='标题']",
            "[class*='title'] input",
            "[class*='title'] textarea",
            "[class*='Title'] input",
            "[class*='artTitle'] input",
            "[class*='art-title'] input",
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
                logger.warning("标题填写被阻挡: %s，尝试强制填写", e)
                try:
                    loc.first.click(force=True)
                    loc.first.fill(title)
                    logger.info("文章标题已填写（强制）")
                    return
                except Exception as e:
                    logger.debug("强制填写标题失败: %s", e)

        # JS 兜底
        try:
            page.evaluate(f"""() => {{
                const inputs = document.querySelectorAll('input, textarea');
                for (const inp of inputs) {{
                    const ph = (inp.placeholder || '').toLowerCase();
                    if (ph.includes('标题') || ph.includes('填写') || ph.includes('请输入')) {{
                        inp.focus();
                        inp.value = '';
                        document.execCommand('insertText', false, {repr(title)});
                        inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                        return true;
                    }}
                }}
                return false;
            }}""")
            logger.info("文章标题已填写（JS）")
            return
        except Exception as e:
            logger.debug("JS兜底填写标题失败: %s", e)

        self._screenshot("weibo_title_not_found.png")
        raise WeiboPublishError("未找到文章标题输入框")

    def _fill_article_body(self, text: str):
        """填写文章正文（富文本编辑器）"""
        page = self._page
        logger.info("填写文章正文 (%d 字)", len(text))

        body_selectors = [
            ".ProseMirror",
            ".ql-editor",
            "[contenteditable='true']",
            ".DraftEditor-root",
            "[class*='editor'] [contenteditable]",
            "[class*='content'] [contenteditable]",
            "[class*='artContent']",
            "[class*='art-content']",
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
                logger.warning("正文填写失败: %s，尝试逐字输入", e)
                try:
                    loc.first.click()
                    page.keyboard.press("Control+a")
                    page.keyboard.type(text, delay=5)
                    logger.info("文章正文已填写（逐字输入）")
                    return
                except Exception as e:
                    logger.debug("逐字输入正文失败: %s", e)

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
            logger.info("文章正文已填写（JS）")
            return
        except Exception as e:
            logger.debug("JS兜底填写正文失败: %s", e)

        self._screenshot("weibo_body_not_found.png")
        raise WeiboPublishError("未找到文章正文编辑器")

    def _upload_article_cover(self, image_source):
        """为文章上传封面图片"""
        page = self._page
        logger.info("上传文章封面...")

        local_files = self._prepare_local_files([image_source])
        if not local_files:
            logger.info("无封面图片可上传")
            return

        local_file = local_files[0]

        # 方法 1: 点击上传封面区域
        upload_triggers = [
            "上传封面", "选择封面", "添加封面",
            "上传图片", "添加图片", "添加头图",
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
                    return
            except Exception as e:
                logger.warning("通过「%s」上传失败: %s", txt, e)

        # 方法 2: 直接找 file input
        try:
            img_inputs = page.locator("input[type='file'][accept*='image']")
            if img_inputs.count() > 0:
                img_inputs.first.set_input_files(local_file)
                logger.info("已通过文件输入框上传封面")
                time.sleep(5)
                return
        except Exception as e:
            logger.warning("文件输入框上传失败: %s", e)

        # 方法 3: 任意 file input
        try:
            all_inputs = page.locator("input[type='file']")
            if all_inputs.count() > 0:
                all_inputs.first.set_input_files(local_file)
                logger.info("已通过通用文件输入框上传封面")
                time.sleep(5)
                return
        except Exception as e:
            logger.warning("通用文件输入框上传失败: %s", e)

        logger.warning("封面上传失败，继续发布...")

    def _fill_article_summary(self, summary: str):
        """填写文章摘要"""
        page = self._page
        logger.info("填写文章摘要...")

        summary_selectors = [
            "textarea[placeholder*='摘要']",
            "textarea[placeholder*='简介']",
            "textarea[placeholder*='描述']",
            "input[placeholder*='摘要']",
            "[class*='summary'] textarea",
            "[class*='summary'] input",
            "[class*='abstract'] textarea",
        ]

        loc = self._wait_for_first(summary_selectors, timeout=10000)
        if loc:
            try:
                loc.first.click()
                loc.first.fill(summary[:200])
                logger.info("文章摘要已填写")
                return
            except Exception as e:
                logger.warning("摘要填写失败: %s", e)

        logger.info("未找到摘要输入框，跳过")

    def _click_article_publish(self):
        """点击文章发布按钮"""
        page = self._page
        logger.info("点击发布...")

        try:
            page.keyboard.press("Escape")
            time.sleep(0.5)
        except Exception as e:
            logger.debug("关闭弹窗操作失败: %s", e)

        self._screenshot("weibo_before_click_publish.png")

        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(0.5)
        except Exception as e:
            logger.debug("滚动到底部失败: %s", e)

        publish_texts = ["发布", "发布文章", "发表", "发送"]

        # 策略 1：JS 定位
        for btn_text in publish_texts:
            try:
                js_code = f"""() => {{
                    const btns = [...document.querySelectorAll('button, a, [role="button"]')];
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
            except Exception as e:
                logger.debug("JS定位发布按钮'%s'失败: %s", btn_text, e)

        # 策略 2：Playwright role
        for btn_text in publish_texts:
            try:
                loc = page.get_by_role("button", name=btn_text, exact=True)
                if loc.count() > 0:
                    target = loc.last if loc.count() > 1 else loc.first
                    if target.is_visible():
                        target.scroll_into_view_if_needed()
                        target.click(force=True)
                        logger.info("已点击'%s'按钮（角色定位）", btn_text)
                        time.sleep(3)
                        self._handle_publish_dialog()
                        return
            except Exception as e:
                logger.debug("角色定位发布按钮'%s'失败: %s", btn_text, e)

        # 策略 3：get_by_text
        for btn_text in publish_texts:
            try:
                loc = page.get_by_text(btn_text, exact=True)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click()
                    logger.info("已点击'%s'（文本定位）", btn_text)
                    time.sleep(3)
                    self._handle_publish_dialog()
                    return
            except Exception as e:
                logger.debug("文本定位发布按钮'%s'失败: %s", btn_text, e)

        # 策略 4：遍历所有按钮
        for btn_text in publish_texts:
            try:
                all_btns = page.locator("button")
                count = all_btns.count()
                for i in range(count - 1, -1, -1):
                    btn = all_btns.nth(i)
                    try:
                        txt = btn.text_content(timeout=1000)
                        if txt and btn_text in txt.strip() and btn.is_visible():
                            btn.scroll_into_view_if_needed()
                            btn.click(force=True)
                            logger.info("已点击包含'%s'的按钮 (遍历)", btn_text)
                            time.sleep(3)
                            self._handle_publish_dialog()
                            return
                    except Exception:
                        continue
            except Exception:
                continue

        self._screenshot("weibo_btn_not_found.png")
        raise WeiboPublishError("未找到发布按钮")

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

    def _check_article_publish_success(self) -> bool:
        """检测文章是否发布成功"""
        page = self._page

        for wait_sec in (2, 3, 5):
            time.sleep(wait_sec)
            url = page.url.lower()

            # 如果跳转离开了编辑器页面
            if "editor" not in url and "edit" not in url:
                logger.info("发布成功（页面已跳转: %s）", page.url)
                return True

            try:
                body_text = page.evaluate("document.body.innerText")
                success_keywords = [
                    "发布成功", "已发布", "文章发布成功",
                    "发表成功", "文章已发表",
                ]
                for keyword in success_keywords:
                    if keyword in body_text:
                        logger.info("发布成功（检测到: %s）", keyword)
                        return True
            except Exception:
                pass

        self._screenshot("weibo_publish_uncertain.png")
        return False

    # ══════════════════════════════════════════════════════════════
    #  视频发布
    # ══════════════════════════════════════════════════════════════

    def publish_video(self, video_path: str, title: str, body: str) -> bool:
        """在微博发布视频"""
        page = self._page
        video_file = Path(video_path)
        if not video_file.exists():
            raise WeiboPublishError(f"视频文件不存在: {video_path}")

        logger.info("开始发布微博视频: %s", title)
        logger.info("视频文件: %s (%.1fMB)", video_file.name,
                     video_file.stat().st_size / 1024 / 1024)

        try:
            # 1. 打开微博首页
            logger.info("打开微博首页...")
            page.goto(WEIBO_URL, wait_until="commit")
            self._wait_for_page_load()

            # 1.5. 关闭引导弹窗
            self._dismiss_guide_popups()
            time.sleep(2)

            # 2. 上传视频
            self._upload_video(str(video_file))

            # 3. 等待视频处理（给视频上传和服务器处理留充足时间）
            self._wait_for_video_processed()
            time.sleep(5)

            # 4. 填写微博文案
            self._fill_weibo_text(body)

            # 5. 声明原创
            self._declare_original()

            self._screenshot("weibo_video_before_publish.png")

            # 6. 等待"发送"按钮可用（确保视频上传+处理完成）
            logger.info("等待发送按钮就绪...")
            time.sleep(5)

            # 7. 点击发布
            self._click_weibo_send()
            time.sleep(5)

            # 6. 检查结果
            success = self._check_weibo_send_success()
            if success:
                logger.info("视频发布成功！")
            else:
                logger.warning("视频发布结果不确定，请手动检查")
                self._screenshot("weibo_video_result.png")
            return success

        except WeiboPublishError:
            self._screenshot("weibo_video_error.png")
            raise
        except Exception as e:
            self._screenshot("weibo_video_error.png")
            raise WeiboPublishError(f"视频发布异常: {e}") from e

    def _wait_for_page_load(self):
        """等待页面加载完成"""
        page = self._page
        logger.info("等待页面加载...")

        try:
            page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT)
        except Exception:
            logger.warning("网络空闲超时，继续...")

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

    def _upload_video(self, video_path: str):
        """上传视频文件（在发布器 composer 区域操作）"""
        page = self._page
        logger.info("上传视频...")

        # ── Step 1: 定位 composer toolbar 中的"视频"图标 ──
        # 微博 composer 工具栏：表情/图片/视频/话题/头条文章/更多
        # "视频"图标在工具栏中（y 坐标在 composer 文本框下方附近）
        # 需要排除 feed 过滤标签中的"视频"和侧边导航中的"视频"
        self._screenshot("weibo_before_video_tab.png")
        video_target = None
        try:
            video_target = page.evaluate("""() => {
                // 策略 A: 找包含"图片"和"视频"的工具栏行，精确定位其中的"视频"
                const walker = document.createTreeWalker(
                    document.body, NodeFilter.SHOW_TEXT, null, false
                );
                const allVideoEls = [];
                while (walker.nextNode()) {
                    const t = walker.currentNode.textContent.trim();
                    if (t !== '视频') continue;
                    const el = walker.currentNode.parentElement;
                    if (!el || el.offsetParent === null) continue;
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) continue;
                    allVideoEls.push({el, r});
                }
                // 对每个"视频"元素，检查其父容器是否也包含"图片"文字（工具栏特征）
                for (const {el, r} of allVideoEls) {
                    let row = el.parentElement;
                    for (let d = 0; d < 5 && row; d++) {
                        const text = row.textContent || '';
                        // 工具栏通常同时包含"图片"和"视频"和"表情"
                        if (text.includes('图片') && text.includes('视频')) {
                            return {x: r.x + r.width/2, y: r.y + r.height/2,
                                    w: r.width, source: 'toolbar_sibling'};
                        }
                        row = row.parentElement;
                    }
                }
                // 策略 B: 找 SVG 图标旁边的"视频"（toolbar 图标通常有 SVG）
                for (const {el, r} of allVideoEls) {
                    let sibling = el.parentElement;
                    if (sibling) {
                        const hasSvg = sibling.querySelector('svg') || sibling.querySelector('i');
                        if (hasSvg && r.width < 60) {
                            return {x: r.x + r.width/2, y: r.y + r.height/2,
                                    w: r.width, source: 'svg_icon'};
                        }
                    }
                }
                // 策略 C: 选 y 坐标最小且 x > 200 的"视频"（页面最上方的那个）
                const filtered = allVideoEls.filter(({r}) => r.x > 200 && r.width < 100);
                filtered.sort((a, b) => a.r.y - b.r.y);
                if (filtered.length > 0) {
                    const {r} = filtered[0];
                    return {x: r.x + r.width/2, y: r.y + r.height/2,
                            w: r.width, source: 'topmost'};
                }
                return null;
            }""")
            if video_target:
                logger.info("定位到视频图标: (%d,%d) source=%s",
                            video_target["x"], video_target["y"], video_target.get("source"))
        except Exception as e:
            logger.debug("JS 定位视频标签失败: %s", e)

        if not video_target:
            try:
                diag = page.evaluate("""() => {
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_TEXT, null, false);
                    const items = [];
                    while (walker.nextNode()) {
                        const t = walker.currentNode.textContent.trim();
                        if (t !== '视频') continue;
                        const el = walker.currentNode.parentElement;
                        if (!el || el.offsetParent === null) continue;
                        const r = el.getBoundingClientRect();
                        items.push({x: Math.round(r.x), y: Math.round(r.y),
                                    w: Math.round(r.width), tag: el.tagName});
                    }
                    return items;
                }""")
                logger.warning("所有'视频'元素: %s", diag)
            except Exception:
                pass
            self._screenshot("weibo_no_video_tab.png")
            raise WeiboPublishError("未找到 composer 内的视频标签")

        # ── Step 2: 用 file_chooser 事件捕获点击"视频"标签触发的文件选择器 ──
        # 微博的"视频"标签点击后直接弹出文件选择器（不是先渲染 file input）
        try:
            with page.expect_file_chooser(timeout=10000) as fc_info:
                page.mouse.click(video_target["x"], video_target["y"])
                logger.info("已点击 composer 内'视频'标签，等待文件选择器...")
            file_chooser = fc_info.value
            file_chooser.set_files(video_path)
            logger.info("视频文件已通过文件选择器上传")
            return
        except Exception as e:
            logger.info("文件选择器方式失败: %s，尝试其他方式...", e)

        # ── Step 3: 可能已经弹出上传区域，找 file input ──
        time.sleep(3)
        self._screenshot("weibo_after_video_tab.png")
        try:
            all_inputs = page.locator("input[type='file']")
            count = all_inputs.count()
            logger.info("找到 %d 个 file input", count)
            if count > 0:
                # 优先找视频专用 file input
                file_input = None
                for i in range(count):
                    inp = all_inputs.nth(i)
                    accept = inp.get_attribute("accept") or ""
                    if "mp4" in accept or "mov" in accept or "video" in accept:
                        file_input = inp
                        break
                if not file_input:
                    file_input = all_inputs.last
                file_input.set_input_files(video_path)
                logger.info("视频文件已通过 file input 上传")
                return
        except Exception as e:
            logger.warning("file input 上传失败: %s", e)

        # ── Step 4: 点击上传区域/按钮触发 file_chooser ──
        upload_selectors = [
            '[class*="upload"]', '[class*="Upload"]',
            'text=上传视频', 'text=点击上传', 'text=上传',
        ]
        for sel in upload_selectors:
            try:
                loc = page.locator(sel)
                if loc.count() > 0 and loc.first.is_visible():
                    with page.expect_file_chooser(timeout=10000) as fc_info:
                        loc.first.click()
                    fc_info.value.set_files(video_path)
                    logger.info("视频已通过上传按钮文件选择器上传 (%s)", sel)
                    return
            except Exception:
                continue

        self._screenshot("weibo_video_upload_failed.png")
        raise WeiboPublishError("视频上传失败: 所有上传策略均失败")

    def _wait_for_video_processed(self):
        """等待视频上传和处理完成（微博首页 composer 模式）"""
        page = self._page
        logger.info("等待视频处理（最长 3 分钟）...")

        start = time.time()
        max_wait = 180

        while time.time() - start < max_wait:
            elapsed = int(time.time() - start)

            try:
                body_text = page.evaluate("document.body.innerText")
                # 还在处理中的指标
                processing = ["上传中", "处理中", "转码中", "压缩中", "视频上传"]
                if any(p in body_text for p in processing):
                    if elapsed % 10 == 0:
                        logger.info("视频处理中... (%ds/%ds)", elapsed, max_wait)
                    time.sleep(3)
                    continue

                # 视频处理完成的指标
                done_indicators = ["上传成功", "处理完成", "上传完成", "可以发布"]
                for indicator in done_indicators:
                    if indicator in body_text:
                        logger.info("视频处理完成 (%ds): %s", elapsed, indicator)
                        time.sleep(2)
                        return
            except Exception:
                pass

            # 检查视频缩略图是否出现（有时长显示说明上传完成）
            try:
                has_video = page.evaluate("""() => {
                    // 找 composer 区域中的视频缩略图
                    const videos = document.querySelectorAll(
                        'video, [class*="video"], [class*="Video"]'
                    );
                    for (const v of videos) {
                        const r = v.getBoundingClientRect();
                        if (r.width > 50 && r.height > 50 && v.offsetParent !== null) {
                            return {found: true, type: 'video_element'};
                        }
                    }
                    // 找带时长标记的缩略图
                    const timeTexts = document.querySelectorAll(
                        '[class*="duration"], [class*="time"], [class*="video-card"]'
                    );
                    for (const t of timeTexts) {
                        if (t.offsetParent !== null && t.textContent.match(/\\d+:\\d+/)) {
                            return {found: true, type: 'duration_text'};
                        }
                    }
                    return {found: false};
                }""")
                if has_video.get("found"):
                    logger.info("视频缩略图已显示 (%ds), type=%s", elapsed, has_video.get("type"))
                    time.sleep(3)
                    return
            except Exception:
                pass

            if elapsed >= 10:
                logger.info("等待视频处理中... (%ds)", elapsed)
            time.sleep(3)

        logger.warning("视频处理等待超时 (%ds)，尝试继续...", max_wait)

    def _fill_weibo_text(self, text: str):
        """填写微博文案（短内容模式）"""
        page = self._page
        logger.info("填写微博文案 (%d 字)", len(text))

        # 微博文案编辑区
        text_selectors = [
            "textarea[placeholder*='有什么']",
            "textarea[placeholder*='分享']",
            "textarea[placeholder*='说说']",
            "textarea[placeholder*='写微博']",
            ".ProseMirror",
            ".ql-editor",
            "[contenteditable='true']",
            "[class*='compose'] textarea",
            "[class*='composer'] textarea",
            "textarea",
        ]

        loc = self._wait_for_first(text_selectors, timeout=30000)
        if loc:
            try:
                loc.first.click()
                loc.first.fill(text)
                logger.info("微博文案已填写")
                return
            except Exception as e:
                logger.warning("文案填写失败: %s，尝试逐字输入", e)
                try:
                    loc.first.click()
                    page.keyboard.press("Control+a")
                    page.keyboard.type(text, delay=5)
                    logger.info("微博文案已填写（逐字输入）")
                    return
                except Exception:
                    pass

        # JS 兜底
        try:
            page.evaluate(f"""() => {{
                const editors = document.querySelectorAll(
                    'textarea, .ProseMirror, .ql-editor, [contenteditable="true"]'
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
            logger.info("微博文案已填写（JS）")
            return
        except Exception:
            pass

        self._screenshot("weibo_text_not_found.png")
        raise WeiboPublishError("未找到微博文案编辑区")

    def _click_weibo_send(self):
        """点击微博发送按钮（composer 工具栏中的橙色「发送」按钮）"""
        page = self._page
        logger.info("点击发送...")

        self._screenshot("weibo_before_send.png")

        # 策略 1：精确定位 composer 工具栏中的「发送」按钮
        # 微博 composer 工具栏中，「发送」按钮是橙色的，在工具栏最右侧
        try:
            result = page.evaluate("""() => {
                const btns = [...document.querySelectorAll('button, a[role="button"], [role="button"]')];
                const sendBtns = btns.filter(b => {
                    const txt = b.textContent.trim();
                    if (txt !== '发送' && txt !== '发布' && txt !== '发微博') return false;
                    if (!b.offsetParent || b.disabled) return false;
                    const r = b.getBoundingClientRect();
                    if (r.width < 30 || r.height < 20) return false;
                    return true;
                });
                if (sendBtns.length === 0) return {found: false};
                // 优先选有橙色/主色背景的按钮
                let best = sendBtns[0];
                for (const b of sendBtns) {
                    const style = window.getComputedStyle(b);
                    const bg = style.backgroundColor;
                    const cls = (b.className || '').toString();
                    // 橙色: rgb(255, 140, 0) 或类似
                    if (bg.includes('255') || bg.includes('ff') ||
                        cls.match(/primary|submit|send|orange|warn/i)) {
                        best = b;
                        break;
                    }
                }
                const r = best.getBoundingClientRect();
                return {found: true, x: r.x + r.width/2, y: r.y + r.height/2,
                        text: best.textContent.trim(), tag: best.tagName,
                        cls: (best.className||'').toString().slice(0,60),
                        disabled: best.disabled || best.getAttribute('aria-disabled') === 'true'};
            }""")

            if result.get("found"):
                logger.info("定位到发送按钮: text=%s, (%d,%d), cls=%s, disabled=%s",
                            result.get("text"), result["x"], result["y"],
                            result.get("cls"), result.get("disabled"))
                if result.get("disabled"):
                    logger.warning("发送按钮疑似禁用状态，等待5秒后重试...")
                    time.sleep(5)

                # 先 scroll into view 确保按钮在可视区
                page.mouse.move(result["x"], result["y"])
                time.sleep(0.3)
                page.mouse.click(result["x"], result["y"])
                logger.info("已点击'发送'按钮 (JS坐标)")
                time.sleep(5)
                self._screenshot("weibo_after_send_click.png")

                # 验证：检查是否有弹窗需要确认
                try:
                    body_text = page.evaluate("document.body.innerText")
                    if "确认" in body_text or "确定" in body_text:
                        for kw in ["确认发送", "确认", "确定", "发送"]:
                            try:
                                btn = page.get_by_role("button", name=kw, exact=True)
                                if btn.count() > 0 and btn.first.is_visible():
                                    btn.first.click()
                                    logger.info("已点击确认弹窗'%s'", kw)
                                    time.sleep(3)
                                    break
                            except Exception:
                                continue
                except Exception:
                    pass
                return
        except Exception as e:
            logger.debug("JS 发送按钮定位失败: %s", e)

        # 策略 2：Playwright 定位器
        for btn_text in ["发送", "发布", "发微博"]:
            try:
                loc = page.get_by_role("button", name=btn_text, exact=True)
                if loc.count() > 0:
                    target = loc.last if loc.count() > 1 else loc.first
                    if target.is_visible():
                        target.scroll_into_view_if_needed()
                        time.sleep(0.5)
                        target.click(force=True)
                        logger.info("已点击'%s'按钮 (Playwright)", btn_text)
                        time.sleep(5)
                        return
            except Exception:
                continue

        self._screenshot("weibo_send_btn_not_found.png")
        raise WeiboPublishError("未找到发送按钮")

    def _check_weibo_send_success(self) -> bool:
        """检测微博是否发送成功"""
        page = self._page

        for wait_sec in (3, 5, 5, 5):
            time.sleep(wait_sec)

            try:
                body_text = page.evaluate("document.body.innerText")
                # 明确的成功关键词
                success_keywords = [
                    "发送成功", "发布成功", "已发布",
                    "微博发送成功", "发布完成",
                ]
                for keyword in success_keywords:
                    if keyword in body_text:
                        logger.info("发送成功（检测到: %s）", keyword)
                        return True
            except Exception:
                pass

            # 检查 composer 是否已清空（发送成功后 composer 会被重置）
            try:
                result = page.evaluate("""() => {
                    // 检查 composer 文本区是否为空
                    const editors = document.querySelectorAll(
                        'textarea, [contenteditable="true"], .ProseMirror, .ql-editor'
                    );
                    for (const ed of editors) {
                        if (ed.offsetParent === null) continue;
                        const text = (ed.value || ed.textContent || '').trim();
                        if (text.length > 50) return {empty: false, len: text.length};
                    }
                    // 检查视频缩略图是否还在
                    const vids = document.querySelectorAll(
                        '[class*="video-card"], [class*="videoCard"], [class*="media-wrap"]'
                    );
                    let hasVid = false;
                    for (const v of vids) {
                        if (v.offsetParent !== null && v.getBoundingClientRect().width > 50) {
                            hasVid = true;
                            break;
                        }
                    }
                    return {empty: true, hasVid};
                }""")
                if result.get("empty") and not result.get("hasVid"):
                    logger.info("发送成功（composer 已清空）")
                    return True
            except Exception:
                pass

            # 检查页面是否跳转
            try:
                url = page.url
                if "weibo.com/u/" in url or "weibo.com/ajax" in url:
                    logger.info("发送成功（页面已跳转: %s）", url[:60])
                    return True
            except Exception:
                pass

        self._screenshot("weibo_send_uncertain.png")
        return False

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

    # ────────── 诊断 ──────────

    def diagnose(self):
        """诊断发布页面元素"""
        page = self._page
        page.goto(ARTICLE_EDITOR_URL, wait_until="commit")
        self._wait_for_article_page()

        print("\n" + "=" * 60)
        print("  微博文章编辑器诊断报告")
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

        self._screenshot("weibo_diagnose.png")
        print(f"\n  截图已保存: logs/weibo_diagnose.png")
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

def publish_weibo_article(content: WeiboContent, headless: bool = False) -> bool:
    """一键发布微博头条文章"""
    with WeiboPublisher(headless=headless) as pub:
        pub.login()
        time.sleep(settings.weibo_publish_delay)
        return pub.publish(content)


def publish_weibo_video(video_path: str, title: str, body: str,
                        headless: bool = False) -> bool:
    """一键发布微博视频"""
    with WeiboPublisher(headless=headless) as pub:
        pub.login()
        time.sleep(settings.weibo_publish_delay)
        return pub.publish_video(video_path, title, body)


def diagnose_weibo_page():
    """诊断发布页面（debug 命令入口）"""
    with WeiboPublisher(headless=False) as pub:
        pub.login()
        pub.diagnose()
        print("\n浏览器保持 15 秒，可手动检查...")
        time.sleep(15)
