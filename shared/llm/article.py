"""
文章内容生成器
—— DeepSeek LLM 优先 + 规则引擎兜底
从 wordpress/publisher/content_engine.py 提取，不再依赖 wordpress 子项目
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from shared.llm.client import LLMClient
from shared.utils.helpers import parse_prompt_context, slugify_chinese
from shared.utils.logger import get_logger

logger = get_logger("article-gen")

# ══════════════════════════════════════════════════════════════
#  常量池
# ══════════════════════════════════════════════════════════════

# ── 意图识别关键词 ──

INTENT_KEYWORDS = {
    "tutorial": ["如何", "怎么", "教程", "步骤", "实操", "实战", "指南", "搭建", "落地"],
    "comparison": ["对比", "区别", "vs", "versus", "优劣", "哪个好", "选型"],
    "listicle": ["清单", "推荐", "合集", "盘点", "工具", "模板", "方案集合", "必备"],
    "risk": ["风险", "避坑", "问题", "错误", "失败", "误区", "注意事项"],
}

# ── 标题模板池 ──

TITLE_POOLS = {
    "tutorial": [
        "{core}：从零到一完整实施手册",
        "{core}分步指南：策略与落地路径",
        "如何高效推进{core}：关键步骤与实操经验",
    ],
    "comparison": [
        "{core}：深度对比与选型建议",
        "如何选择{core}方案：决策框架与关键考量",
    ],
    "risk": [
        "{core}：常见陷阱与应对策略全解析",
        "{core}避坑指南：高频问题与解决方案",
    ],
    "listicle": [
        "{core}：精选方案与最佳实践盘点",
        "{core}工具清单：高效选型与使用指南",
    ],
    "default": [
        "{core}：策略、路径与实施要点",
        "{core}深度解析：方法论与执行框架",
        "{core}实战手册：从规划到落地全流程",
    ],
}

# ── 小节框架池 ──

FRAME_POOLS = {
    "default": [
        (
            "大多数人搞错了第一步",
            "你可能以为这件事的难点在技术选型或预算审批。真正卡住80%团队的，是目标定义——「到底要解决什么问题」这个问题本身就没想清楚。一家做B2B SaaS的50人公司告诉我，他们花了三个月评估方案，最后发现真正需要解决的根本不是最初提出的那个问题。",
            "你的第一步行动：用一句话写下「如果这件事成功了，6个月后哪个业务指标会发生变化」。如果写不出来，说明还没准备好开始。",
        ),
        (
            "怎样用最低成本验证可行性",
            "别一上来就搞大规划。找一个痛点最明显、数据最干净的业务场景做试点，把周期控制在2-3周。一个做电商的20人运营团队，用最简单的方案处理退货工单，两周内就把平均处理时间从45分钟压到12分钟——这比任何PPT都有说服力。",
            "你的行动建议：列出3个候选试点场景，选数据量最大、流程最标准化的那个先下手。",
        ),
        (
            "如何衡量到底有没有效果",
            "不要看感觉，看数字。你需要在启动前就确定3-5个核心指标：效率（处理时间、吞吐量）、质量（错误率、满意度）、成本（人力投入、工具费用）。每周看趋势，不要盯单次波动。",
            "具体操作：建一个简单的周报表，包含这些指标的基准值和当前值，坚持更新4周以上再做判断。",
        ),
        (
            "为什么试点成功后反而容易翻车",
            "试点和规模化是两件完全不同的事。试点时你可能有最好的团队成员盯着、领导天天关注、数据也是精挑细选的。一旦推广到更多业务线，数据质量下降、团队能力参差不齐、关注度也分散了，效果打折甚至回退都很常见。",
            "规模化的关键不是复制方案，而是复制能力——把关键步骤做成标准SOP和培训材料，让普通员工也能执行到80分。",
        ),
        (
            "持续优化比初始方案更重要",
            "很多团队花了大量精力做上线，然后就没人管了。现实是：第一版方案的效果通常只有理想值的60-70%，剩下的30-40%全靠后续的迭代优化。设定一个固定的复盘节奏——比如每两周一次，回顾哪些有效、哪些没达预期、下一步改什么。",
            "给自己定一个规则：至少持续优化3个迭代周期再决定这件事值不值得继续投入。",
        ),
    ],
    "tutorial": [
        (
            "开始之前你需要确认这3件事",
            "不是所有场景都适合用同一套方法。在动手之前，花15分钟确认三个问题：你的数据是否足够干净和可用？团队里有没有至少一个人能持续投入？最终效果有没有一个明确的量化标准？如果三个问题有两个答案是否定的，建议先补短板再启动。",
            "行动建议：把这三个问题发给团队相关人，让每个人独立回答，再对齐分歧。",
        ),
        (
            "第一周该做什么：搭建最小可运行版本",
            "目标很简单——不要完美，要能跑。用最少的配置、最小的数据集、最简单的流程，搭出一个能看到基本效果的版本。一家做SaaS的团队第一周只跑了100条测试数据，但足以验证核心逻辑是不是走得通。",
            "关键提醒：如果第一周结束还没有一个可演示的东西，说明你的规划太复杂了，砍掉一半功能再试。",
        ),
        (
            "配置细节决定成败",
            "80%的上线问题都出在配置上，而不是代码。常见的坑包括：环境变量遗漏、权限设置不对、数据格式不匹配。最好的办法是维护一份配置checklist，每次部署前逐项核对。",
            "实用技巧：把所有配置项整理成一个表格，标注「必改」「可选」「默认即可」三个级别。",
        ),
        (
            "上线之后怎么判断效果好不好",
            "上线后的第一周不要急着优化，先观察数据趋势。重点看三个信号：核心指标是否在预期范围内、有没有明显的异常波动、用户（或团队）的直觉反馈和数据是否一致。如果这三个信号都正面，可以开始调优。",
            "A/B测试是最可靠的调优方法——每次只改一个变量，跑够样本量再看结论，不要凭直觉下判断。",
        ),
        (
            "从「能用」到「好用」的进阶路径",
            "初始版本只是起点。接下来的优化可以分三个方向推进：覆盖更多场景、提升现有场景的效果、减少人工干预。建议每个方向各设一个指标，每两周回顾一次进展。",
            "不要贪多。每个迭代周期只做一个方向的优化，做透了再切换到下一个。",
        ),
    ],
    "comparison": [
        (
            "选型之前先定评估标准",
            "大多数选型失败不是因为选错了方案，而是因为评估标准不统一。技术团队看性能指标，业务团队看操作便捷性，老板看成本——三方各执一词，最终选出来的方案谁都不满意。",
            "先花半天时间让各方一起定义评估维度和权重，比你花三周试用方案更有价值。",
        ),
        (
            "核心能力逐项对比：别被宣传页忽悠",
            "对比时重点关注四个方面：功能是否覆盖你的核心场景（不是全部功能）、实际操作体验（不是Demo演示）、真实场景下的性能表现、以及长期持有成本（包含维护和升级）。",
            "建议每个方案至少拿真实业务数据测试一周——Demo和实际使用往往是两回事。",
        ),
        (
            "什么场景选什么方案：适配比功能更重要",
            "功能最全的方案未必是最好的选择。一个50人的营销团队和一个500人的制造企业，最佳方案可能完全不同。关键看三点：你的团队技术能力如何、预算天花板在哪、需要多快出效果。",
            "如果团队技术能力有限，优先选学习曲线平缓的方案，而不是功能最强的方案。",
        ),
        (
            "最终决策建议：用数据说话而不是拍脑袋",
            "不要被某个方案的销售demo打动就直接签约。最佳实践是：选2-3个候选方案，各做1-2周的真实场景小规模测试，用统一的指标框架量化对比，然后让数据替你做决定。",
            "如果条件允许，保留第二名作为备选方案。主方案一旦出问题，切换成本越低越好。",
        ),
    ],
    "listicle": [
        (
            "筛选标准：为什么推荐这些而不是那些",
            "市面上同类方案少说几十个，本文的筛选基于四个硬指标：至少有1000+活跃用户、核心功能免费或提供充分的试用期、有中文支持或社区、过去一年持续更新。符合全部四条的方案才会出现在推荐列表中。",
            "选工具跟选人一样——不要看他说什么，看他过去一年做了什么。",
        ),
        (
            "核心推荐清单与亮点速览",
            "以下推荐按适用场景而非排名排列。每个方案都有其最擅长的一件事——有的胜在易用性、有的强在定制能力、有的性价比最高。对号入座比盲目选「第一名」更明智。",
            "如果你只有5分钟，直接跳到最符合你业务规模的那个推荐项。",
        ),
        (
            "快速上手：选好之后怎么用起来",
            "选定方案后最重要的事是在第一天就跑通核心流程。不要花一周看文档——直接动手，遇到问题再查。大部分工具的核心功能在30分钟内就能跑通。",
            "给自己设一个目标：选定方案的当天就完成注册、配置和第一次成功的操作。",
        ),
        (
            "组合使用策略：1+1怎么大于2",
            "没有哪个工具能覆盖所有需求。关键是找到2-3个能互补的方案，并确保数据能在它们之间流通。比如用A做数据采集、B做分析、C做自动化——前提是它们之间有API或导出功能互通。",
            "组合时的红线：如果两个工具的数据不能互通、只能手动导入导出，那这个组合不值得。",
        ),
    ],
    "risk": [
        (
            "排名前三的高频翻车场景",
            "根据实际项目经验，最常见的失败原因不是技术不行，而是这三个：目标太宏大导致无法落地、没有量化指标导致无法判断效果、关键人员中途离场导致项目断档。这三个问题至少占到失败案例的70%以上。",
            "启动前做一次「如果失败会因为什么」的逆向推演，把答案写下来贴在工位上。",
        ),
        (
            "真实翻车案例：他们到底错在哪里",
            "一个300人的零售企业投入了6个月和大量预算做自动化改造，最终搁置。事后复盘发现核心问题是：项目范围不断膨胀、没有阶段性交付物证明价值、团队疲劳后失去信心。这不是技术问题，是项目管理问题。",
            "最好的防御方法是把大项目拆成4周一个的小阶段，每个阶段必须有一个可展示的成果。",
        ),
        (
            "怎么提前设好安全网",
            "针对每个已识别的风险，预设「触发条件」和「应对动作」。比如：如果试点两周后核心指标没有改善超过10%，就暂停并复盘原因。提前定好这些规则，比出了问题再临时开会讨论高效10倍。",
            "具体做法：建一个三列表格——风险描述、触发条件、应对方案——在项目启动会上让全员确认。",
        ),
        (
            "长期运营中如何避免温水煮青蛙",
            "很多项目不是突然失败的，而是慢慢失去效果——指标一点点下滑，团队一点点松懈，直到有一天发现回到了原点。预防这种情况的最好方法是设定「健康度指标」，定期自动检查，低于阈值就自动告警。",
            "每月花半小时看一次趋势图。如果核心指标连续两周走平或下滑，立即安排一次深度复盘。",
        ),
    ],
}

# ── 图片风格池（英文描述以获得更好的生成效果）──

IMAGE_STYLES = [
    # 信息图风格 —— 扁平矢量流程
    "flat vector infographic composition, clean modular flow with geometric connectors, "
    "modern minimalist, soft shadows, 3D depth layering effect",
    # 数据可视化风格 —— 仪表盘
    "holographic dashboard visualization with floating metric cards and abstract chart forms, "
    "glassmorphism UI elements, professional tech aesthetic",
    # 等距商务风 —— 等距 3D
    "isometric 3D business scene illustration, digital workspace with abstract device outlines, "
    "subtle grain texture, clean geometric objects",
    # 抽象节点风 —— 连接网络
    "abstract network diagram, modular structure with connected glowing nodes and data paths, "
    "ultra-clean flat design, neon gradient connection lines, futuristic",
    # 时间轴/路线图风 —— 进度路径
    "timeline roadmap illustration, left-to-right progressive pathway with milestone markers, "
    "paper-cut layered depth, gradient connecting lines",
    # 对比布局风 —— 双面对比
    "split-screen comparison layout with side-by-side abstract panels, "
    "clear visual contrast through geometric shape variations",
    # 3D 抽象科技风 —— 粒子几何
    "3D abstract technology illustration, floating geometric shapes and luminous data particles, "
    "depth of field blur, volumetric cinematic lighting",
    # 极简图标风 —— 中心辐射
    "minimal symbolic illustration, centered abstract icon with radiating geometric elements, "
    "clean composition with single accent color focus, thin line art details",
    # 渐变层叠风 —— 景观层次
    "layered gradient abstract landscape, rolling geometric hills with flowing data stream patterns, "
    "paper texture overlay, calm and professional atmosphere",
    # 线框蓝图风 —— 技术蓝图
    "wireframe blueprint technical diagram, precision geometric shapes with dotted connection lines, "
    "engineering aesthetic with subtle glow highlights",
]

# ── DeepSeek 系统提示词 ──

_DEEPSEEK_SYSTEM_PROMPT = """\
你是一位有10年实战经验的中文商业科技内容作者，同时精通SEO。
你写的文章以鲜明观点、具体细节和读者能记住的金句著称。
你信奉「一句有用的话胜过十句正确的废话」，永远站在读者的利益角度写作。

## 写作铁律

### 1. 禁用词表（出现即失败）
显著提升、赋能、抓手、助力、深度赋能、全面赋能、一站式、
生态、闭环、数智化、全链路、范式转变、降维打击、
不言而喻、毋庸置疑、综上所述、众所周知、不可或缺、
日新月异、蓬勃发展、方兴未艾、如火如荼、与日俱增、
围绕、聚焦、本节、本文将、接下来我们、让我们

### 2. 数据规则
- 引用数据必须标明出处（如「据 Gartner 2024 报告」「McKinsey 调研显示」）
- 没有真实数据时，用具体的场景数字代替（如「一个5人客服团队每天处理800条工单」）
- 绝对禁止凭空编造百分比和统计数字

### 3. 案例规则
- 每篇文章至少包含2个具体场景或案例
- 案例要有细节：行业+团队规模+做了什么+结果（如「一家200人的跨境电商公司用AI客服替代了3个夜班坐席，月省4.2万」）
- 禁止写「某企业」「某公司」，要写具体行业和规模

### 4. 语言风格
- 用第二人称「你」直接与读者对话
- 每2-3段至少一个口语化表达、比喻或类比
- 段落首句必须有信息量，禁止用「随着…的发展」「在…背景下」「近年来」等空洞句式开头
- 允许有态度和判断（如「这个方案性价比很低」），不要永远两面讨好
- 适当使用短句和反问句增加节奏感

### 5. 结构规则
- 开头第一段直接抛出一个反直觉观点、真实痛点或引发好奇的问题，5秒内抓住读者
- 每个小节标题必须让读者知道「读完能得到什么」，用疑问句或「动词+具体结果」
- 每个小节结尾有一句可立即执行的行动建议
- 全文2000-3000字

### 6. SEO要求
- focus_keyword 自然出现在：标题、第一段、至少2个小标题、结论中
- FAQ答案要具体到可以被搜索引擎直接引用为精选摘要
- seo_description 第一句话就包含关键词，像在回答一个搜索问题

## 输出格式

严格返回JSON对象（不要包裹在markdown代码块中），字段：
- title(string): 20-50字，含核心关键词，用冒号或破折号分隔主副标题
- slug(string): 英文短横线分隔，3-6个单词，如 ai-sales-automation-guide
- focus_keyword(string): 核心关键词，2-8字
- seo_description(string): 120-150字，首句含关键词，像在回答搜索问题
- excerpt(string): 80-120字，告诉读者「读完你能获得什么」
- quick_answer(string): 2-3句话直接回答核心问题，不铺垫，适合搜索引擎精选摘要
- key_takeaways(string[]): 5-6条，每条是一个具体可执行的建议而非空泛总结
- sections([{title:string, paragraphs:string[]}]): 4-6个小节，每节2-4段，每段100-200字
- faq([{question:string, answer:string}]): 5个真实长尾搜索问题，答案具体有用可被直接引用
- tags(string[]): 5-8个短关键词，每个2-6字
- conclusion(string): 150-250字，回顾核心论点，给出明确下一步行动，用「如果你…那么…」句式结尾
- cta({heading:string, text:string}): heading为3-6字动词短语，text为1-2句具体可执行的下一步建议"""


# ══════════════════════════════════════════════════════════════
#  ArticleGenerator
# ══════════════════════════════════════════════════════════════

class ArticleGenerator:
    """
    文章内容生成器
    - generate(): DeepSeek 优先，规则兜底
    - image_prompts(): 基于文章结构生成图片提示词
    """

    def __init__(
        self,
        llm: Optional[LLMClient] = None,
        max_content_images: int = 4,
        deepseek_enabled: bool = True,
    ) -> None:
        self.llm = llm
        self.max_content_images = max(1, max_content_images)
        self.deepseek_enabled = deepseek_enabled

    # ── 意图识别 ──

    @staticmethod
    def detect_intent(text: str) -> str:
        lowered = text.lower()
        for intent, words in INTENT_KEYWORDS.items():
            for word in words:
                if word.lower() in lowered:
                    return intent
        return "default"

    @staticmethod
    def choose_section_frames(core_text: str, section_count: int) -> List[Tuple[str, str, str]]:
        intent = ArticleGenerator.detect_intent(core_text)
        selected_pool = FRAME_POOLS.get(intent) or FRAME_POOLS["default"]
        seed = sum(ord(ch) for ch in core_text)
        start = seed % len(selected_pool)
        frames: List[Tuple[str, str, str]] = []
        for i in range(section_count):
            frames.append(selected_pool[(start + i) % len(selected_pool)])
        return frames

    # ── 统一入口 ──

    def generate(
        self,
        prompt: str,
        focus_keyword: Optional[str] = None,
        use_deepseek: Optional[bool] = None,
    ) -> Dict:
        """生成文章：DeepSeek 优先，规则兜底"""
        base_article = self._generate_rule_based(prompt=prompt, focus_keyword=focus_keyword)

        if use_deepseek is None:
            use_deepseek = self.deepseek_enabled
        if not use_deepseek or not self.llm or not self.llm.available:
            return base_article

        user_prompt = self._build_user_prompt(prompt, base_article.get("intent", "default"))
        llm_payload = self.llm.chat_json(
            system_prompt=_DEEPSEEK_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            temperature=0.82,
        )
        if not llm_payload:
            return base_article

        article = self._merge_llm_result(base_article, llm_payload, prompt)
        article = self._post_process(article)
        return article

    # ── 构建用户提示词 ──

    @staticmethod
    def _build_user_prompt(prompt: str, intent: str) -> str:
        """根据用户输入和意图构建详细的 user prompt"""
        intent_hints = {
            "tutorial": "请侧重实操步骤和具体配置方法，每步给出预期结果和常见报错处理。",
            "comparison": "请用表格思维逐项对比（功能、价格、适用场景、上手难度），给出明确推荐而非模棱两可。",
            "listicle": "每个推荐项需有一句话优势总结+适用人群+一个真实使用场景。",
            "risk": "请聚焦真实踩坑经历和具体损失数字，每个风险点给出可操作的规避方案。",
        }
        hint = intent_hints.get(intent, "请确保每个小节都有至少一个具体案例或数据支撑。")

        return (
            f"请根据以下要求生成一篇高质量中文文章：\n\n"
            f"{prompt}\n\n"
            f"内容方向提示：{hint}\n\n"
            f"写作提醒（非常重要）：\n"
            f"- 开头第一句话就要有冲击力，禁止用「随着」「在当今」「近年来」开头\n"
            f"- 每个小节至少包含一个具体场景、案例或数据\n"
            f"- 结尾给出具体可执行的下一步行动，而不是「未来可期」式的展望\n"
            f"- 全文保持对话感，像在跟一个聪明但时间有限的企业管理者面对面聊天\n"
            f"- 语气可以有态度，有判断，不要面面俱到不敢得罪人"
        )

    # ── 质量后处理 ──

    @staticmethod
    def _post_process(article: Dict) -> Dict:
        """对 LLM 生成的文章做质量清洗"""
        # 禁用词过滤（在段落中替换为更自然的表达）
        banned = [
            "显著提升", "赋能", "抓手", "助力", "深度赋能", "全面赋能",
            "一站式", "闭环", "数智化", "全链路", "范式转变", "降维打击",
            "不言而喻", "毋庸置疑", "众所周知", "不可或缺",
            "日新月异", "蓬勃发展", "方兴未艾", "如火如荼", "与日俱增",
        ]

        def _clean(text: str) -> str:
            for word in banned:
                text = text.replace(word, "")
            # 清理连续的标点（因为删词可能留下多余逗号）
            text = re.sub(r"[，,]{2,}", "，", text)
            text = re.sub(r"[、]{2,}", "、", text)
            text = re.sub(r"^[，,、]+", "", text)
            return text.strip()

        # 清洗 sections
        for sec in article.get("sections", []):
            sec["paragraphs"] = [_clean(p) for p in sec.get("paragraphs", []) if _clean(p)]

        # 清洗其他文本字段
        for key in ["conclusion", "quick_answer", "excerpt", "seo_description"]:
            if key in article and isinstance(article[key], str):
                article[key] = _clean(article[key])

        # 清洗 key_takeaways
        if "key_takeaways" in article and isinstance(article["key_takeaways"], list):
            article["key_takeaways"] = [_clean(t) for t in article["key_takeaways"] if _clean(t)]

        # 清洗 FAQ
        for item in article.get("faq", []):
            if "answer" in item:
                item["answer"] = _clean(item["answer"])

        return article

    # ── 图片提示词 ──

    def image_prompts(self, article: Dict) -> List[Dict[str, str]]:
        """生成图片提示词列表：1张特色图 + N张内容图

        改进：
        - 所有 prompt 纯英文，中文内容先翻译再传入（防止 AI 生成乱码文字）
        - 统一色彩体系和视觉风格，保证同一篇文章的多张图风格一致
        - 明确禁止任何文字渲染
        """
        focus = article["focus_keyword"]
        topic = article.get("topic", focus)
        title = article.get("title", topic)
        sections = article.get("sections", [])

        # ── 全部翻译为英文 ──
        topic_en = self._topic_to_english_concept(topic)

        # 统一色彩体系（同一篇文章的所有图共享色盘，提升视觉一致性）
        # 根据主题 hash 选择主色调
        seed = sum(ord(c) for c in topic)
        PALETTES = [
            "deep navy to electric blue gradient with vibrant purple and cyan accents, subtle warm orange highlights",
            "dark teal to emerald green gradient with gold and white accents, subtle warm amber highlights",
            "deep indigo to violet gradient with magenta and soft pink accents, subtle silver highlights",
            "charcoal to steel blue gradient with coral and mint green accents, subtle cream highlights",
        ]
        palette = PALETTES[seed % len(PALETTES)]
        # 统一风格描述（所有图共享，确保一致性）
        unified_style = (
            f"Color palette: {palette}. "
            f"Style: modern 3D render with glassmorphism elements, soft ambient lighting, "
            f"depth of field blur on edges, clean geometric shapes. "
            f"CRITICAL: absolutely NO text, NO letters, NO numbers, NO words, NO Chinese characters, "
            f"NO symbols, NO watermark anywhere in the image. "
            f"NO human faces, NO people, NO characters. "
            f"Pure abstract visual illustration only."
        )

        prompts: List[Dict[str, str]] = []

        # ── 特色图（封面横幅） ──
        prompts.append({
            "role": "featured",
            "prompt": (
                f"Hero banner illustration for a business technology article about {topic_en}. "
                f"Abstract geometric composition: floating 3D shapes, interconnected glowing nodes, "
                f"flowing data streams with particle effects, dynamic motion suggesting innovation. "
                f"{unified_style} "
                f"Wide 16:9 aspect ratio, 4K quality digital illustration."
            ),
            "alt_text": title,
            "caption": title,
        })

        # ── 内容图（每个小节一张） ──
        for idx, sec in enumerate(sections[: self.max_content_images - 1]):
            sec_title = sec.get("title", topic)
            # 翻译小节标题和概念为英文（避免中文进入 prompt 导致乱码）
            sec_title_en = self._topic_to_english_concept(sec_title)
            sec_concept = self._extract_section_concept(sec)
            # 风格轮选但保持统一色盘
            style = IMAGE_STYLES[idx % len(IMAGE_STYLES)]
            prompts.append({
                "role": "content",
                "prompt": (
                    f"Professional illustration visualizing the concept of {sec_title_en}. "
                    f"Visual metaphor: {sec_concept}. "
                    f"Artistic approach: {style}. "
                    f"{unified_style} "
                    f"Composition: balanced layout with clear focal point, "
                    f"subtle depth through layering and soft shadows. "
                    f"High quality, 16:9 aspect ratio, 4K resolution."
                ),
                "alt_text": f"{topic} - {sec_title}",
                "caption": sec_title,
            })

        return prompts

    @staticmethod
    def _topic_to_english_concept(topic: str) -> str:
        """将中文主题转换为英文概念描述（扩展映射 + 纯英文输出保证）

        改进：
        - 扩展关键词映射表覆盖更多商业/技术领域
        - 确保输出为纯英文，彻底杜绝中文进入图片 prompt
        - 对未匹配到的中文内容使用通用商业技术描述
        """
        # 扩展关键词映射表
        keyword_map = {
            # 技术
            "AI": "AI artificial intelligence",
            "人工智能": "artificial intelligence",
            "机器学习": "machine learning neural network",
            "深度学习": "deep learning neural network",
            "自然语言": "natural language processing NLP",
            "大模型": "large language model LLM",
            "GPT": "GPT generative AI",
            "算法": "algorithm computing",
            "自动化": "automation workflow",
            "Agent": "AI agent autonomous system",
            "机器人": "robot automation",
            "聊天机器人": "chatbot conversational AI",
            "工具": "tools software interface",
            "API": "API integration interface",
            "云计算": "cloud computing infrastructure",
            "SaaS": "SaaS software platform",
            "低代码": "low-code no-code platform",
            "物联网": "IoT internet of things sensor",
            "区块链": "blockchain distributed ledger",
            # 商业
            "销售": "sales pipeline revenue",
            "营销": "marketing strategy campaign",
            "客服": "customer service support center",
            "数据": "data analytics dashboard",
            "运营": "business operations workflow",
            "管理": "management strategy leadership",
            "电商": "e-commerce online retail shopping",
            "企业": "enterprise business organization",
            "效率": "productivity efficiency optimization",
            "成本": "cost reduction financial optimization",
            "团队": "team collaboration workspace",
            "协作": "collaboration teamwork workflow",
            "决策": "decision making strategy analysis",
            "增长": "growth expansion scaling",
            "创新": "innovation breakthrough technology",
            "转型": "digital transformation modernization",
            "品牌": "brand identity marketing",
            "用户": "user experience engagement",
            "客户": "customer relationship CRM",
            "流量": "traffic acquisition funnel",
            "转化": "conversion optimization funnel",
            # 内容
            "内容": "content creation strategy",
            "SEO": "SEO search optimization ranking",
            "写作": "writing content authoring",
            "视频": "video production media",
            "直播": "live streaming broadcast",
            # 行业
            "金融": "finance fintech banking",
            "医疗": "healthcare medical technology",
            "教育": "education edtech learning",
            "制造": "manufacturing production industry",
            "零售": "retail commerce shopping",
            "物流": "logistics supply chain delivery",
            # 概念
            "步骤": "step by step process methodology",
            "对比": "comparison analysis versus",
            "风险": "risk assessment security shield",
            "指标": "metrics KPI performance dashboard",
            "案例": "case study real-world example",
            "试点": "pilot program test experiment",
            "规模": "scaling growth expansion",
            "优化": "optimization improvement tuning",
            "趋势": "trend forecast future prediction",
            "策略": "strategy planning roadmap",
        }
        parts = []
        for cn, en in keyword_map.items():
            if cn in topic:
                parts.append(en)

        if parts:
            return " ".join(parts[:4])

        # 如果没有匹配到任何关键词，检查是否纯英文
        import re
        if re.match(r'^[a-zA-Z0-9\s\-_.,!?:;()]+$', topic):
            return topic

        # 中文内容未匹配到关键词时，返回通用描述
        return "business technology digital transformation innovation"

    @staticmethod
    def _extract_section_concept(section: Dict) -> str:
        """从小节内容中提取核心概念，输出纯英文视觉隐喻描述

        改进：
        - 扩展关键词映射，描述侧重"视觉隐喻"而非抽象词汇
        - 多关键词可叠加，生成更具体的视觉描述
        - 确保输出 100% 纯英文
        """
        title = section.get("title", "")
        paragraphs = section.get("paragraphs", [])
        first_para = paragraphs[0][:80] if paragraphs else title
        text = f"{title} {first_para}"

        # 关键词 → 视觉隐喻描述（侧重可视化的场景描述）
        concepts = {
            "步骤": "step-by-step ascending staircase with glowing checkpoints",
            "流程": "flowing pipeline with connected modular nodes",
            "对比": "split-screen comparison with contrasting visual elements",
            "风险": "protective shield barrier with warning signal lights",
            "指标": "floating holographic dashboard with rising chart lines",
            "案例": "magnifying glass focusing on a detailed blueprint",
            "工具": "interconnected floating tool icons in orbital arrangement",
            "试点": "rocket launching from a launchpad with data trails",
            "规模": "expanding concentric circles radiating outward",
            "优化": "tuning knobs and sliders on a control panel interface",
            "团队": "interconnected network nodes in collaborative grid",
            "成本": "balanced scale with geometric weight blocks",
            "效率": "speedometer gauge with accelerating particle trails",
            "数据": "flowing data streams through crystalline pipeline",
            "安全": "multi-layered shield with encryption lock patterns",
            "增长": "upward growing bar chart with sprouting elements",
            "创新": "lightbulb radiating geometric innovation rays",
            "用户": "user journey pathway with interaction touchpoints",
            "客户": "customer engagement funnel with flowing elements",
            "平台": "multi-tier platform architecture with floating layers",
            "集成": "puzzle pieces connecting into unified structure",
            "分析": "analytical lens examining data crystal structures",
            "策略": "chess board with strategic pathway arrows",
            "未来": "futuristic horizon with emerging technology shapes",
            "智能": "neural network brain with glowing synaptic connections",
            "转化": "transformation funnel with morphing geometric shapes",
        }

        matched = []
        for cn, en in concepts.items():
            if cn in text:
                matched.append(en)

        if matched:
            return " and ".join(matched[:2])
        return "abstract business strategy concept with interconnected geometric elements"

    # ── 规则引擎 ──

    def _generate_rule_based(self, prompt: str, focus_keyword: Optional[str] = None) -> Dict:
        context = parse_prompt_context(prompt)
        core = context.get("theme", "").strip() or prompt.strip()
        if not core:
            raise ValueError("prompt 不能为空")
        focus = (focus_keyword or core)[:30]

        content_intent = self.detect_intent(core)
        title_pool = TITLE_POOLS.get(content_intent, TITLE_POOLS["default"])
        seed = sum(ord(ch) for ch in core)
        title_tpl = title_pool[seed % len(title_pool)]
        title = title_tpl.format(core=core if len(core) <= 30 else core[:28])

        # 拆分主题词
        raw_parts = re.split(r"[，,、；;。/\|]+|(?:和|及|与|以及|并且|并)", core)
        stopwords = {"如何", "怎么", "指南", "方案", "策略", "系统", "方法", "实践"}
        topic_parts: List[str] = []
        for part in raw_parts:
            value = part.strip()
            if not value or value in stopwords:
                continue
            if value not in topic_parts:
                topic_parts.append(value)
        tokens = re.findall(r"[A-Za-z0-9\u4e00-\u9fff]{2,}", core)
        for token in tokens:
            if token in stopwords or token in topic_parts:
                continue
            topic_parts.append(token)

        section_count = min(len(FRAME_POOLS.get(content_intent, FRAME_POOLS["default"])), 5)
        while len(topic_parts) < section_count:
            topic_parts.append(focus)

        # 生成小节
        section_frames = self.choose_section_frames(core, section_count)
        sections = []
        for idx in range(section_count):
            frame_title, para_1, para_2 = section_frames[idx]
            sec_title = f"{core}：{frame_title}" if idx == 0 else frame_title
            sections.append({"title": sec_title, "paragraphs": [para_1, para_2]})

        # SEO 描述 & 摘要
        sec_titles = [s["title"] for s in sections[:3]]
        topic_hint = "、".join(topic_parts[:3])
        seo_desc = (
            f'{title}。本文从{sec_titles[0]}、{sec_titles[1] if len(sec_titles) > 1 else "实施策略"}'
            f'等角度深入分析，为企业管理者提供可执行的实施路径与优化建议。'
        )[:155]
        excerpt = (
            f'深入解析{core}的核心策略与实施路径。'
            f'覆盖{topic_hint}等关键维度，'
            f'提供可落地的方法论、效果指标与优化建议。'
        )[:140]

        quick_answer = (
            f'{core}的核心是先明确业务目标，再建立可量化的执行路径与效果指标。'
            f'建议从单一高价值场景切入，完成试点验证后再规模化推广，'
            f'通常可在1-2个迭代周期内看到明显改善。'
        )

        key_takeaways = [
            f'先用一句话定义{focus}要解决的具体业务问题——定义不清就不要动手。',
            f'选一个数据最干净、痛点最明显的场景做2-3周试点，不要上来就搞全面推广。',
            f'启动前就确定3-5个量化指标和基准值，没有基准就无法判断效果。',
            f'每两周做一次复盘，如果连续两个周期没有进展就暂停诊断原因。',
            f'规模化的关键不是复制方案，而是把关键步骤做成SOP让普通人也能执行到80分。',
            f'最大的风险不是做错了，而是一直在「准备做」——先迈出第一步。',
        ]

        faq = self._build_faq(content_intent, focus, core)
        tags = [t for t in [focus] + topic_parts if 2 <= len(t) <= 10][:8]
        slug = slugify_chinese(core)
        conclusion = self._build_conclusion(content_intent, core, focus, sections)
        cta = self._build_cta(content_intent, core, focus)

        return {
            "topic": core,
            "intent": content_intent,
            "title": title,
            "slug": slug,
            "focus_keyword": focus,
            "seo_description": seo_desc,
            "excerpt": excerpt,
            "quick_answer": quick_answer,
            "key_takeaways": key_takeaways,
            "faq": faq,
            "sections": sections,
            "tags": tags,
            "conclusion": conclusion,
            "cta": cta,
            "content_source": "rules",
        }

    # ── LLM 结果合并 ──

    def _merge_llm_result(self, base: Dict, llm: Dict, prompt: str) -> Dict:
        article = dict(base)

        # 文本字段：LLM 非空则覆盖
        for key in ["title", "slug", "focus_keyword", "seo_description", "excerpt", "quick_answer", "conclusion"]:
            value = llm.get(key)
            if isinstance(value, str) and value.strip():
                article[key] = value.strip()

        # slug 额外检查：确保是纯英文+短横线
        slug = article.get("slug", "")
        if slug and not re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", slug):
            # LLM 返回了非法 slug，回退到规则生成
            article["slug"] = base.get("slug", slug)

        # key_takeaways：至少3条才替换
        key_takeaways = llm.get("key_takeaways")
        if isinstance(key_takeaways, list):
            cleaned = [str(x).strip() for x in key_takeaways if str(x).strip()]
            if len(cleaned) >= 3:
                article["key_takeaways"] = cleaned[:6]

        # sections：至少3节、每节至少2段才替换
        sections = llm.get("sections")
        if isinstance(sections, list):
            normalized = []
            for sec in sections[:6]:
                if not isinstance(sec, dict):
                    continue
                title = str(sec.get("title", "")).strip()
                paras_raw = sec.get("paragraphs", [])
                if not isinstance(paras_raw, list):
                    continue
                paras = [str(p).strip() for p in paras_raw if str(p).strip()]
                if title and len(paras) >= 2:
                    normalized.append({"title": title, "paragraphs": paras[:5]})
            if len(normalized) >= 3:
                article["sections"] = normalized
            else:
                logger.warning("LLM sections 不足 3 节（%d），使用规则兜底", len(normalized))

        # faq：至少3条才替换
        faq = llm.get("faq")
        if isinstance(faq, list):
            normalized_faq = []
            for item in faq[:8]:
                if not isinstance(item, dict):
                    continue
                q = str(item.get("question", "")).strip()
                a = str(item.get("answer", "")).strip()
                if q and a and len(a) >= 15:  # 答案至少15字才算有效
                    normalized_faq.append({"question": q, "answer": a})
            if len(normalized_faq) >= 3:
                article["faq"] = normalized_faq

        # tags：验证长度
        tags = llm.get("tags")
        if isinstance(tags, list):
            cleaned_tags = [str(t).strip() for t in tags if 1 < len(str(t).strip()) <= 10]
            if cleaned_tags:
                article["tags"] = cleaned_tags[:8]

        # cta
        cta = llm.get("cta")
        if isinstance(cta, dict):
            heading = str(cta.get("heading", "")).strip()
            text = str(cta.get("text", "")).strip()
            if heading and text:
                article["cta"] = {"heading": heading, "text": text}

        article["intent"] = self.detect_intent(prompt)
        article["content_source"] = "deepseek"
        return article

    # ── FAQ 构建 ──

    @staticmethod
    def _build_faq(intent: str, focus: str, core: str) -> List[Dict[str, str]]:
        base_faq = [
            {
                "question": f"{focus}适合多大规模的团队？小公司也能做吗？",
                "answer": (
                    f"完全可以。{focus}的核心不在于企业大小，而在于你是否有明确的业务问题要解决。"
                    f"小团队（5-20人）建议从现成的SaaS工具入手，月成本控制在几百到几千元；"
                    f"中型团队（50-200人）可以考虑轻度定制；超过200人再评估自建方案。"
                ),
            },
            {
                "question": f"实施{focus}需要多久才能看到效果？",
                "answer": (
                    f"如果选对了切入场景，通常2-4周就能在试点范围内看到明确改善。"
                    f"但从试点到全面推广一般需要2-3个月。很多团队犯的错误是期望一步到位——"
                    f"建议把大目标拆成4个两周的小阶段，每个阶段都有可验证的产出。"
                ),
            },
            {
                "question": f"怎么判断{focus}的投入值不值？有没有简单的ROI算法？",
                "answer": (
                    f"最简单的算法：（节省的人力成本 + 减少的错误损失 - 工具和实施费用）÷ 实施费用。"
                    f"如果你算不清这笔账，大概率是因为没有量化的基准数据。"
                    f"建议在启动前先用一周时间记录现状数字（处理时间、错误率、人力投入），有了基准才能评估变化。"
                ),
            },
            {
                "question": f"{focus}做到一半失败了怎么办？最常见的坑是什么？",
                "answer": (
                    "根据实际项目经验，失败原因排名前三的是：①目标模糊导致方向反复调整；"
                    "②没有设置阶段性交付物导致团队丧失信心；③关键执行人员离场没有backup。"
                    "预防方法很简单——每两周做一次复盘，如果连续两次没有进展就暂停诊断原因。"
                ),
            },
            {
                "question": f"团队没有技术背景，能做好{focus}吗？",
                "answer": (
                    f"可以，但需要选对工具。现在大部分SaaS产品都不需要写代码，"
                    f"操作难度和Excel差不多。关键是团队要有一个人负责持续跟进和优化——"
                    f"这个人不需要懂技术，但需要懂业务流程、会看数据趋势。"
                ),
            },
        ]

        # 根据意图替换第2条FAQ
        intent_faq = {
            "tutorial": {
                "question": f"{focus}具体怎么做？最关键的第一步是什么？",
                "answer": (
                    f"第一步不是选工具，而是定义清楚你要解决什么问题。"
                    f"然后选一个最痛、数据最充分的场景做试点。具体步骤是："
                    f"明确目标→选定工具→小范围测试→验证效果→标准化流程→逐步推广。"
                    f"整个过程控制在4-8周完成第一轮。"
                ),
            },
            "comparison": {
                "question": f"选{focus}方案时，最容易踩的坑是什么？",
                "answer": (
                    "最大的坑是被功能列表和Demo演示迷惑。很多方案Demo很炫但实际用起来体验一般。"
                    "建议你关注三个硬指标：用你自己的真实数据测效果、让实际使用者（不是决策者）试用打分、"
                    "算清楚三年总成本（含维护升级培训）。"
                ),
            },
            "risk": {
                "question": f"怎么在{focus}过程中做好风险预警？",
                "answer": (
                    "设定三个自动预警指标：核心指标连续两周下滑、团队满意度低于6分（10分制）、"
                    "预算消耗速度超出计划20%。任何一个指标触发就暂停并复盘。"
                    "不要等到项目半年后才发现问题——那时候调整成本已经非常高了。"
                ),
            },
        }
        if intent in intent_faq:
            base_faq[1] = intent_faq[intent]

        return base_faq

    # ── 总结构建 ──

    @staticmethod
    def _build_conclusion(intent: str, core: str, focus: str, sections: List[Dict]) -> str:
        sec_refs = "、".join(s["title"] for s in sections[:3])
        conclusions = {
            "tutorial": (
                f'做好{core}没有捷径，但有正确的节奏。'
                f'回顾一下关键路径：{sec_refs}——每一步都可以独立验证，不需要等全部准备好才开始。'
                f'最大的浪费不是做错了，而是一直在「准备做」。'
                f'如果你手上已经有一个明确的业务场景和基本的数据，那今天就可以启动试点。'
                f'两周后你会拥有比任何报告都更有说服力的东西：真实的效果数据。'
            ),
            "comparison": (
                f'选{focus}方案不是选「最好的」，而是选「最适合你的」。'
                f'通过{sec_refs}的对比分析，你应该对各方案的长短板有了清晰认知。'
                f'不要被Demo打动就急着签约——花一到两周用真实业务数据做小规模测试，'
                f'让数据替你做决定。如果你的团队技术能力有限，宁可选学习成本低的方案，放弃一些高级功能。'
                f'记住：能用起来的方案才是好方案。'
            ),
            "risk": (
                f'{core}最怕的不是遇到问题，而是遇到问题时没有预案。'
                f'从{sec_refs}到长期运维，每一步都有可预见的风险——'
                f'好消息是，绝大多数风险都有成熟的应对方法。'
                f'如果你今天只做一件事，就建一份风险清单：列出排名前五的风险、对应的触发条件和应对动作。'
                f'这张表的价值会在你最需要的时候体现出来。'
            ),
            "listicle": (
                f'工具只是手段，你真正需要的是解决一个具体的业务问题。'
                f'从{sec_refs}到组合策略，推荐的每个方案都有其最佳适用场景。'
                f'如果你看到这里还在纠结选哪个——那就选一个今天就能注册免费试用的，'
                f'花30分钟跑通核心流程。亲手试过一次，比看十篇测评都管用。'
            ),
        }
        default = (
            f'回顾整篇文章，{core}的核心逻辑其实很简单：'
            f'从{sec_refs}一路走下来，你需要的不是一个完美方案，而是一个「够用且能验证」的起点。'
            f'最大的风险不是方案选错了——试错成本远比你想象得低——'
            f'而是一直停在调研和比较阶段，迟迟不动手。'
            f'如果你已经有一个明确的痛点和基本的数据，那就从今天开始。'
            f'选一个最小化的场景，跑一个两周的试点，用结果决定下一步。'
        )
        return conclusions.get(intent, default)

    # ── CTA 构建 ──

    @staticmethod
    def _build_cta(intent: str, core: str, focus: str) -> Dict[str, str]:
        ctas = {
            "tutorial": {
                "heading": "现在就动手",
                "text": (
                    f"打开你的工作台，选一个最熟悉的业务场景，按上面的步骤跑一个最小试点。"
                    f"不需要完美，不需要审批——先花两小时把第一步做了。"
                    f"两周后你手上会有真实数据，那时候再决定要不要继续投入。"
                ),
            },
            "comparison": {
                "heading": "做个两周测试",
                "text": (
                    f"从文中挑2个最匹配的方案，今天就注册试用。"
                    f"各跑一周真实业务数据，用同一套指标对比。"
                    f"数据会帮你做出比直觉更靠谱的决定。"
                ),
            },
            "risk": {
                "heading": "今天建一份预案",
                "text": (
                    f"花30分钟列出{focus}排名前5的风险和对应的应对方案，发给团队所有人确认。"
                    f"这张表可能是你这个月做过的性价比最高的一件事。"
                ),
            },
            "listicle": {
                "heading": "选一个先试",
                "text": (
                    f"不要纠结了。从推荐列表里选一个今天就能免费试用的方案，"
                    f"花30分钟跑通核心流程。亲手试过比看十篇测评都管用。"
                ),
            },
        }
        default = {
            "heading": "从今天开始",
            "text": (
                f"选一个最小的场景，用本文的方法启动{focus}试点。"
                f"不需要完美计划——先做两周，用结果说话。"
                f"你会发现，最难的其实是迈出第一步，而不是后面的事情。"
            ),
        }
        return ctas.get(intent, default)
