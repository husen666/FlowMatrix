/**
 * 全平台视频发布 (Node.js Playwright)
 * 绕过 Python 3.14 + greenlet 兼容性问题
 *
 * 用法:
 *   node publish_video_node.js                     # 发布到全部平台
 *   node publish_video_node.js --platform xhs      # 只发布小红书
 *   node publish_video_node.js --platform douyin    # 只发布抖音
 */

const { chromium } = require('playwright');
const path = require('path');
const fs = require('fs');

// ════════ 配置 ════════

const PROJECT_ROOT = __dirname;
const OUTPUT_DIR = path.join(PROJECT_ROOT, 'output', '二零二六春风暖');

// 查找视频文件
function findVideo() {
  const files = fs.readdirSync(OUTPUT_DIR);
  const mp4 = files.find(f => f.endsWith('.mp4'));
  if (!mp4) throw new Error(`未找到 MP4 文件: ${OUTPUT_DIR}`);
  return path.join(OUTPUT_DIR, mp4);
}

// 读取内容 JSON
function readContent(filename) {
  const fp = path.join(OUTPUT_DIR, filename);
  if (!fs.existsSync(fp)) return null;
  return JSON.parse(fs.readFileSync(fp, 'utf-8'));
}

// 延时
const sleep = ms => new Promise(r => setTimeout(r, ms));

// ════════ 小红书 ════════

async function publishXHS(videoPath) {
  const data = readContent('xhs_content.json');
  if (!data) { console.log('[XHS] 无内容文件，跳过'); return false; }

  const profileDir = path.join(PROJECT_ROOT, 'xiaohongshu', 'data', 'browser_profile');
  console.log('[XHS] 启动浏览器...');
  const context = await chromium.launchPersistentContext(profileDir, {
    channel: 'msedge',
    headless: false,
    args: ['--disable-blink-features=AutomationControlled'],
    viewport: { width: 1280, height: 800 },
  });

  try {
    const page = context.pages()[0] || await context.newPage();

    // 导航到发布页
    console.log('[XHS] 打开创作者中心...');
    await page.goto('https://creator.xiaohongshu.com/publish/publish?source=official&type=normal', { waitUntil: 'domcontentloaded', timeout: 30000 });
    await sleep(3000);

    // 检查登录
    const url = page.url();
    if (url.includes('login')) {
      console.log('[XHS] 需要登录，请在浏览器中手动登录...');
      await page.waitForURL('**/publish/**', { timeout: 120000 });
      console.log('[XHS] 登录成功');
    }

    // 上传视频
    console.log('[XHS] 上传视频...');
    const fileInputs = await page.$$('input[type="file"]');
    let uploaded = false;
    for (const fi of fileInputs) {
      const accept = await fi.getAttribute('accept') || '';
      if (accept.includes('video') || accept.includes('mp4') || accept.includes('mov')) {
        await fi.setInputFiles(videoPath);
        uploaded = true;
        break;
      }
    }
    if (!uploaded && fileInputs.length > 0) {
      await fileInputs[0].setInputFiles(videoPath);
      uploaded = true;
    }
    if (!uploaded) throw new Error('未找到文件上传控件');

    // 等待视频处理
    console.log('[XHS] 等待视频处理...');
    await sleep(10000);
    for (let i = 0; i < 36; i++) {
      const hasTitle = await page.$('input[placeholder*="标题"]');
      const hasEditor = await page.$('.ProseMirror, .ql-editor, [contenteditable="true"]');
      if (hasTitle || hasEditor) break;
      await sleep(5000);
    }

    // 填写标题
    console.log('[XHS] 填写标题...');
    await sleep(2000);
    const titleInput = await page.$('input[placeholder*="标题"]') || await page.$('.el-input__inner');
    if (titleInput) {
      await titleInput.click();
      await titleInput.fill(data.title);
    }

    // 填写正文
    console.log('[XHS] 填写正文...');
    const editor = await page.$('.ProseMirror') || await page.$('.ql-editor') || await page.$('[contenteditable="true"]');
    if (editor) {
      await editor.click();
      await editor.fill(data.full_text || data.body);
    }

    // 等待发布按钮可用
    console.log('[XHS] 等待发布按钮...');
    await sleep(5000);

    // 点击发布
    console.log('[XHS] 点击发布...');
    const publishBtn = await page.getByRole('button', { name: '发布' }).last();
    if (publishBtn) {
      await publishBtn.click();
      await sleep(10000);
      console.log('[XHS] 发布完成!');
      return true;
    }
    return false;
  } catch (e) {
    console.error(`[XHS] 失败: ${e.message}`);
    return false;
  } finally {
    await sleep(3000);
    await context.close();
  }
}

// ════════ 抖音 ════════

async function publishDouyin(videoPath) {
  const data = readContent('dy_content.json');
  if (!data) { console.log('[Douyin] 无内容文件，跳过'); return false; }

  const profileDir = path.join(PROJECT_ROOT, 'douyin', 'data', 'browser_profile');
  console.log('[Douyin] 启动浏览器...');
  const context = await chromium.launchPersistentContext(profileDir, {
    channel: 'msedge',
    headless: false,
    args: ['--disable-blink-features=AutomationControlled'],
    viewport: { width: 1280, height: 800 },
  });

  try {
    const page = context.pages()[0] || await context.newPage();

    console.log('[Douyin] 打开上传页...');
    await page.goto('https://creator.douyin.com/creator-micro/content/upload', { waitUntil: 'domcontentloaded', timeout: 30000 });
    await sleep(3000);

    if (page.url().includes('login')) {
      console.log('[Douyin] 需要登录，请手动登录...');
      await page.waitForURL('**/upload**', { timeout: 120000 });
    }

    // 上传视频
    console.log('[Douyin] 上传视频...');
    const fileInputs = await page.$$('input[type="file"]');
    let uploaded = false;
    for (const fi of fileInputs) {
      const accept = await fi.getAttribute('accept') || '';
      if (accept.includes('video') || accept.includes('mp4')) {
        await fi.setInputFiles(videoPath);
        uploaded = true;
        break;
      }
    }
    if (!uploaded && fileInputs.length > 0) {
      await fileInputs[0].setInputFiles(videoPath);
      uploaded = true;
    }
    if (!uploaded) throw new Error('未找到文件上传控件');

    // 等待处理
    console.log('[Douyin] 等待视频处理...');
    await sleep(10000);
    for (let i = 0; i < 36; i++) {
      const hasTitle = await page.$('input[placeholder*="标题"]') || await page.$('input[placeholder*="作品标题"]');
      const hasEditor = await page.$('.ProseMirror, .ql-editor, [contenteditable="true"]');
      if (hasTitle || hasEditor) break;
      await sleep(5000);
    }

    // 填写标题
    console.log('[Douyin] 填写标题...');
    await sleep(2000);
    const titleInput = await page.$('input[placeholder*="标题"]') || await page.$('input[placeholder*="作品标题"]');
    if (titleInput) {
      await titleInput.click();
      await titleInput.fill(data.title);
    }

    // 填写正文
    console.log('[Douyin] 填写正文...');
    const editor = await page.$('.ProseMirror') || await page.$('.ql-editor') || await page.$('[contenteditable="true"]');
    if (editor) {
      await editor.click();
      await editor.fill(data.full_text || data.body);
    }

    // 关闭封面弹窗
    await sleep(3000);
    try {
      const coverClose = await page.$('button:has-text("取消"), .close-btn, [aria-label="Close"]');
      if (coverClose) await coverClose.click();
    } catch (e) {}

    // 点击发布
    console.log('[Douyin] 点击发布...');
    await sleep(3000);
    const publishBtn = await page.getByRole('button', { name: '发布' }).last();
    if (publishBtn) {
      await publishBtn.click();
      await sleep(10000);
      console.log('[Douyin] 发布完成!');
      return true;
    }
    return false;
  } catch (e) {
    console.error(`[Douyin] 失败: ${e.message}`);
    return false;
  } finally {
    await sleep(3000);
    await context.close();
  }
}

// ════════ 微信视频号 ════════

async function publishChannels(videoPath) {
  const data = readContent('channels_content.json');
  if (!data) { console.log('[Channels] 无内容文件，跳过'); return false; }

  const cookiesFile = path.join(PROJECT_ROOT, 'channels', 'data', 'channels_cookies.json');
  const screenshotDir = path.join(OUTPUT_DIR, 'screenshots');
  if (!fs.existsSync(screenshotDir)) fs.mkdirSync(screenshotDir, { recursive: true });

  console.log('[Channels] 启动浏览器...');
  const browser = await chromium.launch({
    channel: 'msedge',
    headless: false,
    args: ['--disable-blink-features=AutomationControlled'],
  });

  const ctxOpts = {
    viewport: { width: 1280, height: 800 },
    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0',
  };
  if (fs.existsSync(cookiesFile)) {
    ctxOpts.storageState = cookiesFile;
    console.log('[Channels] 加载已保存的 cookies');
  }

  const context = await browser.newContext(ctxOpts);
  const page = await context.newPage();

  try {
    console.log('[Channels] 打开创建页...');
    await page.goto('https://channels.weixin.qq.com/platform/post/create', { waitUntil: 'domcontentloaded', timeout: 30000 });
    await sleep(5000);

    // 检查登录状态
    const currentUrl = page.url();
    if (currentUrl.includes('login') || currentUrl.includes('passport')) {
      console.log('[Channels] 需要扫码登录，请在浏览器中操作...');
      await page.waitForURL(url => !url.toString().includes('login') && !url.toString().includes('passport'), { timeout: 180000 });
      console.log('[Channels] 登录成功!');
      await sleep(3000);
      // 登录后重新导航到创建页
      await page.goto('https://channels.weixin.qq.com/platform/post/create', { waitUntil: 'domcontentloaded', timeout: 30000 });
      await sleep(5000);
    }

    await page.screenshot({ path: path.join(screenshotDir, 'channels_01_page.png') });

    // 上传视频 — 重新查询 DOM 元素
    console.log('[Channels] 上传视频...');
    await sleep(3000);
    const fileInputs = await page.$$('input[type="file"]');
    console.log(`[Channels] 找到 ${fileInputs.length} 个 file input`);
    let uploaded = false;
    for (const fi of fileInputs) {
      const accept = await fi.getAttribute('accept') || '';
      if (accept.includes('video') || accept.includes('mp4') || accept.includes('mov')) {
        await fi.setInputFiles(videoPath);
        uploaded = true;
        console.log('[Channels] 视频文件已选择');
        break;
      }
    }
    if (!uploaded) {
      // 尝试所有 file input
      for (const fi of fileInputs) {
        try {
          await fi.setInputFiles(videoPath);
          uploaded = true;
          console.log('[Channels] 通过备选 input 上传');
          break;
        } catch (e) {}
      }
    }
    if (!uploaded) throw new Error('未找到视频上传控件');

    // 等待视频处理
    console.log('[Channels] 等待视频处理（最多5分钟）...');
    await sleep(10000);
    for (let i = 0; i < 60; i++) {
      const bodyText = await page.evaluate(() => document.body.innerText).catch(() => '');
      const processing = bodyText.includes('上传中') || bodyText.includes('转码中') || bodyText.includes('处理中');
      if (!processing && i > 2) break;
      if (i % 6 === 0) console.log(`[Channels] 处理中... (${i * 5}s)`);
      await sleep(5000);
    }

    await page.screenshot({ path: path.join(screenshotDir, 'channels_02_uploaded.png') });

    // 填写描述 — 多策略
    console.log('[Channels] 填写描述...');
    await sleep(3000);

    const descText = data.full_text || data.body;
    const filled = await page.evaluate((text) => {
      // 策略1: textarea
      const textareas = document.querySelectorAll('textarea');
      for (const ta of textareas) {
        if (ta.offsetParent !== null) {
          ta.focus();
          ta.value = text;
          ta.dispatchEvent(new Event('input', { bubbles: true }));
          ta.dispatchEvent(new Event('change', { bubbles: true }));
          return 'textarea';
        }
      }
      // 策略2: contenteditable
      const editables = document.querySelectorAll('[contenteditable="true"]');
      for (const ed of editables) {
        if (ed.offsetParent !== null && ed.offsetHeight > 20) {
          ed.focus();
          ed.innerText = text;
          ed.dispatchEvent(new Event('input', { bubbles: true }));
          return 'contenteditable';
        }
      }
      // 策略3: ProseMirror / ql-editor
      const pm = document.querySelector('.ProseMirror, .ql-editor');
      if (pm) {
        pm.focus();
        pm.innerText = text;
        pm.dispatchEvent(new Event('input', { bubbles: true }));
        return 'prosemirror';
      }
      return null;
    }, descText);

    if (filled) {
      console.log(`[Channels] 描述已填写 (${filled})`);
    } else {
      console.log('[Channels] 尝试键盘输入...');
      // 点击页面中央偏下可能是描述区域
      await page.click('body', { position: { x: 640, y: 500 } });
      await sleep(500);
      await page.keyboard.type(descText.substring(0, 200), { delay: 15 });
    }

    await page.screenshot({ path: path.join(screenshotDir, 'channels_03_filled.png') });

    // 点击发表 — 多策略
    console.log('[Channels] 点击发表...');
    await sleep(5000);

    const published = await page.evaluate(() => {
      const buttons = document.querySelectorAll('button, [role="button"], .btn, [class*="publish"], [class*="submit"]');
      for (const btn of buttons) {
        const text = btn.innerText.trim();
        if ((text === '发表' || text === '发布') && btn.offsetParent !== null && !btn.disabled) {
          btn.click();
          return text;
        }
      }
      return null;
    });

    if (published) {
      console.log(`[Channels] 已点击 "${published}" 按钮`);
      await sleep(10000);
      // 保存 cookies
      try {
        const state = await context.storageState();
        fs.writeFileSync(cookiesFile, JSON.stringify(state, null, 2));
      } catch (e) {}
      await page.screenshot({ path: path.join(screenshotDir, 'channels_04_result.png') });
      console.log('[Channels] 发布完成!');
      return true;
    } else {
      // 备选: Playwright locator
      try {
        const btn = page.getByRole('button', { name: /发表|发布/ });
        await btn.last().click({ timeout: 10000 });
        await sleep(10000);
        console.log('[Channels] 发布完成! (locator)');
        return true;
      } catch (e) {
        await page.screenshot({ path: path.join(screenshotDir, 'channels_05_fail.png') });
        console.error('[Channels] 未找到发布按钮');
        return false;
      }
    }
  } catch (e) {
    console.error(`[Channels] 失败: ${e.message}`);
    try { await page.screenshot({ path: path.join(screenshotDir, 'channels_error.png') }); } catch (se) {}
    return false;
  } finally {
    await sleep(3000);
    await context.close();
    await browser.close();
  }
}

// ════════ 微博 ════════

async function publishWeibo(videoPath) {
  const data = readContent('weibo_content.json');
  if (!data) { console.log('[Weibo] 无内容文件，跳过'); return false; }

  const profileDir = path.join(PROJECT_ROOT, 'weibo', 'data', 'browser_profile');
  console.log('[Weibo] 启动浏览器...');
  const context = await chromium.launchPersistentContext(profileDir, {
    channel: 'msedge',
    headless: false,
    args: ['--disable-blink-features=AutomationControlled'],
    viewport: { width: 1280, height: 800 },
  });

  try {
    const page = context.pages()[0] || await context.newPage();

    console.log('[Weibo] 打开微博...');
    await page.goto('https://weibo.com', { waitUntil: 'domcontentloaded', timeout: 30000 });
    await sleep(5000);

    if (page.url().includes('login') || page.url().includes('passport')) {
      console.log('[Weibo] 需要登录，请手动登录...');
      await page.waitForURL('**/weibo.com/**', { timeout: 120000 });
      await sleep(3000);
    }

    // 找视频按钮并点击触发文件选择
    console.log('[Weibo] 上传视频...');
    const [fileChooser] = await Promise.all([
      page.waitForEvent('filechooser', { timeout: 15000 }).catch(() => null),
      page.evaluate(() => {
        const items = document.querySelectorAll('[class*="tool"] span, [class*="toolbar"] span, button span');
        for (const el of items) {
          if (el.textContent.includes('视频')) { el.click(); return true; }
        }
        const inputs = document.querySelectorAll('input[type="file"]');
        if (inputs.length) { inputs[0].click(); return true; }
        return false;
      })
    ]);

    if (fileChooser) {
      await fileChooser.setFiles(videoPath);
    } else {
      // 直接通过 file input
      const fi = await page.$('input[type="file"]');
      if (fi) await fi.setInputFiles(videoPath);
    }

    // 等待处理
    console.log('[Weibo] 等待视频处理...');
    await sleep(15000);
    for (let i = 0; i < 36; i++) {
      const text = await page.textContent('body').catch(() => '');
      if (text && !text.includes('上传中') && !text.includes('处理中')) break;
      await sleep(5000);
    }

    // 填写正文
    console.log('[Weibo] 填写正文...');
    await sleep(2000);
    const textArea = await page.$('textarea[placeholder*="有什么"]')
      || await page.$('textarea[placeholder*="分享"]')
      || await page.$('textarea[placeholder*="说说"]')
      || await page.$('[contenteditable="true"]');
    if (textArea) {
      await textArea.click();
      await sleep(500);
      const tag = await textArea.evaluate(el => el.tagName.toLowerCase());
      if (tag === 'textarea') {
        await textArea.fill(data.full_text || data.body);
      } else {
        await page.keyboard.type(data.full_text || data.body, { delay: 10 });
      }
    }

    // 点击发送
    console.log('[Weibo] 点击发送...');
    await sleep(5000);
    const sendBtn = await page.getByRole('button', { name: /发送|发布|发微博/ }).first();
    if (sendBtn) {
      await sendBtn.click();
      await sleep(10000);
      console.log('[Weibo] 发布完成!');
      return true;
    }
    return false;
  } catch (e) {
    console.error(`[Weibo] 失败: ${e.message}`);
    return false;
  } finally {
    await sleep(3000);
    await context.close();
  }
}

// ════════ 主流程 ════════

const PLATFORMS = {
  xhs: { name: '小红书', fn: publishXHS },
  douyin: { name: '抖音', fn: publishDouyin },
  channels: { name: '视频号', fn: publishChannels },
  weibo: { name: '微博', fn: publishWeibo },
};

async function main() {
  const args = process.argv.slice(2);
  const platformFilter = args.indexOf('--platform') >= 0 ? args[args.indexOf('--platform') + 1] : null;

  const videoPath = findVideo();
  console.log('');
  console.log('═'.repeat(55));
  console.log('  《二零二六春风暖》歌曲视频 — 全平台发布');
  console.log(`  视频: ${path.basename(videoPath)}`);
  console.log(`  大小: ${(fs.statSync(videoPath).size / 1024 / 1024).toFixed(1)} MB`);
  console.log('═'.repeat(55));

  const results = {};

  for (const [key, { name, fn }] of Object.entries(PLATFORMS)) {
    if (platformFilter && key !== platformFilter) continue;

    console.log(`\n${'─'.repeat(40)}`);
    console.log(`正在发布到 ${name}...`);
    console.log('─'.repeat(40));

    try {
      const ok = await fn(videoPath);
      results[name] = ok ? 'OK' : 'UNCERTAIN';
    } catch (e) {
      results[name] = `FAIL: ${e.message}`;
      console.error(`${name} 发布失败: ${e.message}`);
    }

    await sleep(5000);
  }

  // 保存结果
  const resultsPath = path.join(OUTPUT_DIR, 'publish_results.json');
  fs.writeFileSync(resultsPath, JSON.stringify(results, null, 2), 'utf-8');

  // 打印汇总
  console.log('\n' + '═'.repeat(55));
  console.log('  发布结果汇总');
  console.log('═'.repeat(55));
  for (const [name, status] of Object.entries(results)) {
    const icon = status === 'OK' ? '[OK]' : status === 'UNCERTAIN' ? '[??]' : '[!!]';
    console.log(`  ${icon} ${name}: ${status}`);
  }
  const okCount = Object.values(results).filter(s => s === 'OK').length;
  console.log(`  ${okCount}/${Object.keys(results).length} 平台发布成功`);
  console.log('═'.repeat(55));
}

main().catch(e => {
  console.error('致命错误:', e);
  process.exit(1);
});
