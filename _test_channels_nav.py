"""探测视频号创作平台的视频发布页面入口 - 使用 persistent context"""
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

PLATFORM_URL = "https://channels.weixin.qq.com/platform"
BROWSER_PROFILE = Path(__file__).resolve().parent / "channels" / "data" / "browser_profile"
LOGS_DIR = Path(__file__).resolve().parent / "logs"
LOGS_DIR.mkdir(exist_ok=True)
BROWSER_PROFILE.mkdir(parents=True, exist_ok=True)

sys.stdout.reconfigure(encoding='utf-8')

def main():
    pw = sync_playwright().start()
    context = pw.chromium.launch_persistent_context(
        user_data_dir=str(BROWSER_PROFILE),
        channel="msedge",
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
        viewport={"width": 1280, "height": 800},
    )
    page = context.pages[0] if context.pages else context.new_page()

    # 1. 访问首页
    print("=== 1. 访问首页 ===")
    page.goto(PLATFORM_URL, wait_until="networkidle", timeout=60000)
    time.sleep(3)
    page.screenshot(path=str(LOGS_DIR / "ch_nav_1_home.png"))
    url = page.url
    print(f"  URL: {url}")

    if "/login" in url:
        print("  未登录，请在弹出的浏览器中扫码登录...")
        for i in range(60):
            time.sleep(2)
            cur = page.url
            if "/login" not in cur:
                print(f"  登录成功! URL: {cur}")
                break
            if i % 10 == 0 and i > 0:
                print(f"  等待扫码... ({i*2}s)")
        else:
            print("  登录超时")
            context.close()
            pw.stop()
            return

    time.sleep(2)
    page.screenshot(path=str(LOGS_DIR / "ch_nav_1b_after_login.png"))

    # 2. 获取侧边栏文字
    print("\n=== 2. 侧边栏菜单 ===")
    try:
        all_text = page.evaluate("""() => {
            const items = [];
            document.querySelectorAll('a, [class*="menu"], [class*="nav"] li, [class*="sidebar"] *').forEach(el => {
                const t = el.textContent.trim();
                if (t && t.length < 30 && el.offsetParent !== null) {
                    const href = el.getAttribute('href') || '';
                    const tag = el.tagName;
                    items.push(`[${tag}] "${t}" href="${href}"`);
                }
            });
            return [...new Set(items)].join('\\n');
        }""")
        print(all_text[:2000])
    except Exception as e:
        print(f"  失败: {e}")

    # 3. 访问 /post/create
    print("\n=== 3. 访问 /post/create ===")
    page.goto(f"{PLATFORM_URL}/post/create", wait_until="networkidle", timeout=60000)
    time.sleep(3)
    page.screenshot(path=str(LOGS_DIR / "ch_nav_3_post_create.png"))
    print(f"  URL: {page.url}")
    try:
        body = page.evaluate("document.body.innerText.slice(0, 2000)")
        print(f"  页面文字:\n{body[:1500]}")
    except Exception as e:
        print(f"  获取失败: {e}")

    # 4. 分析页面所有可见元素
    print("\n=== 4. 可交互元素 ===")
    try:
        elems = page.evaluate("""() => {
            const results = [];
            document.querySelectorAll('a, button, [role="tab"], span, div, li, input').forEach(el => {
                const t = el.textContent.trim().slice(0, 40);
                const tag = el.tagName;
                const cls = (el.className || '').toString().slice(0, 60);
                const href = el.getAttribute('href') || '';
                const type = el.getAttribute('type') || '';
                const accept = el.getAttribute('accept') || '';
                if (el.offsetParent !== null && (t || type === 'file')) {
                    results.push({tag, text: t, cls, href, type, accept});
                }
            });
            return results.slice(0, 100);
        }""")
        for el in elems:
            parts = [f"[{el['tag']}]"]
            if el['text']:
                parts.append(f"text=\"{el['text']}\"")
            if el['cls']:
                parts.append(f"class=\"{el['cls']}\"")
            if el['href']:
                parts.append(f"href=\"{el['href']}\"")
            if el['type']:
                parts.append(f"type=\"{el['type']}\"")
            if el['accept']:
                parts.append(f"accept=\"{el['accept']}\"")
            print(f"  {' '.join(parts)}")
    except Exception as e:
        print(f"  失败: {e}")

    print("\n完成！请检查 logs/ 目录下的截图")
    context.close()
    pw.stop()

if __name__ == "__main__":
    main()
