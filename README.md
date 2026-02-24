# Aineoo 内容自动发布工具

AI 视频/文章自动生成 + 全平台一键分发（小红书、抖音、视频号、知乎、头条、微博）。

## 项目结构

```
code/
├── xiyouji_havoc.py            # 西游记恶搞视频生成 + 全平台发布
├── main.py                     # WordPress/小红书 CLI 入口
├── requirements.txt            # Python 依赖
├── .env                        # 配置文件（不入库）
│
├── shared/                     # 共享核心模块
│   ├── config.py               # 统一配置加载器
│   ├── publisher_base.py       # 发布器基类（浏览器管理、原创声明等）
│   ├── utils/
│   │   ├── logger.py           # 彩色日志 + 文件轮转
│   │   ├── retry.py            # 通用重试装饰器
│   │   ├── exceptions.py       # 统一异常体系
│   │   └── helpers.py          # 工具函数
│   ├── llm/
│   │   ├── client.py           # DeepSeek / LLM 统一客户端
│   │   ├── article.py          # 文章内容生成
│   │   ├── xhs.py              # 小红书文案
│   │   ├── douyin.py           # 抖音文案
│   │   ├── channels.py         # 视频号文案
│   │   ├── zhihu.py            # 知乎文案
│   │   ├── toutiao.py          # 头条文案
│   │   └── weibo.py            # 微博文案
│   ├── media/
│   │   ├── image.py            # 火山引擎图片生成
│   │   ├── video.py            # 火山引擎视频生成（即梦AI）
│   │   └── tts.py              # edge-tts 配音 + ASS 字幕
│   └── wp/
│       └── client.py           # WordPress REST API 客户端
│
├── xiaohongshu/                # 小红书发布器（继承 BasePublisher）
│   ├── publisher.py
│   └── data/                   # 浏览器数据 + Cookies
│
├── douyin/                     # 抖音发布器
│   ├── publisher.py
│   └── data/
│
├── channels/                   # 视频号发布器
│   ├── publisher.py
│   └── data/
│
├── zhihu/                      # 知乎发布器
│   ├── publisher.py
│   └── data/
│
├── toutiao/                    # 头条发布器
│   ├── publisher.py
│   └── data/
│
├── weibo/                      # 微博发布器
│   ├── publisher.py
│   └── data/
│
├── wordpress/                  # WordPress 发布流水线
│   ├── pipeline.py
│   └── html_builder.py
│
├── output/                     # 生成的素材和视频
│   └── xiyouji/havoc/
│       ├── master.json         # 主数据
│       ├── *_content.json      # 各平台素材
│       ├── shot*.mp4           # 分镜视频
│       └── havoc_final.mp4     # 最终视频
│
└── logs/                       # 日志 + 截图
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. 配置

```bash
cp .env.example .env
# 编辑 .env 填入火山引擎、DeepSeek 等 API Key
```

### 3. 使用

#### 视频生成 + 全平台发布

```bash
# 预览脚本（不生成视频）
python xiyouji_havoc.py --dry-run

# 生成视频
python xiyouji_havoc.py

# 生成视频后自动发布全平台
python xiyouji_havoc.py --publish

# 仅发布已有视频
python xiyouji_havoc.py --publish-only

# 重试上次失败的平台
python xiyouji_havoc.py --retry-failed
```

#### WordPress 发布

```bash
python main.py wp --topic "AI销售自动化"
python main.py wp --topic "AI销售自动化" --video
```

## 架构说明

### 发布器继承体系

所有平台发布器继承自 `shared/publisher_base.py` 的 `BasePublisher`，共享以下功能：

- 浏览器持久化上下文管理（自动保存登录态）
- 反爬检测绕过（WebDriver 属性隐藏）
- 截图保存
- 原创声明多策略勾选（Playwright 原生 + JS 深度搜索 + CSS 兜底）
- 发布按钮多策略点击
- 折叠区域自动展开

各平台发布器只需实现平台特有的 UI 交互逻辑。

### 支持的平台

| 平台 | 视频 | 文章 | 原创声明 |
|------|------|------|----------|
| 小红书 | Yes | Yes | Yes |
| 抖音 | Yes | Yes | Yes |
| 视频号 | Yes | Yes | Yes |
| 知乎 | Yes | Yes | Yes |
| 头条 | Yes | Yes | Yes |
| 微博 | Yes | Yes | Yes |

## 配置项

| 变量 | 说明 | 必填 |
|------|------|------|
| `VOLC_AK` | 火山引擎 Access Key | 是（视频生成）|
| `VOLC_SK` | 火山引擎 Secret Key | 是（视频生成）|
| `DEEPSEEK_API_KEY` | DeepSeek API Key | 是（文案生成）|
| `WP_BASE` | WordPress 站点地址 | 否 |
| `WP_USER` | WordPress 用户名 | 否 |
| `WP_APP_PASSWORD` | WordPress 应用密码 | 否 |
| `OUTPUT_DIR` | 素材输出目录（默认 output/） | 否 |
