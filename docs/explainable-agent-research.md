# ConvoMemory 可解释 Agent 深度调研报告

> 生成日期：2026-04-29
> 项目：ConvoMemory — 聊天记录 → 知识图谱 → ReAct agent
> 目标：在 agent 回答问题时于图上高亮推理路径，并在文本中解释"为什么是这个答案"

---

## 目录

1. [三层可解释性实现路径](#part-1三层可解释性实现路径)
2. [图高亮设计规范](#part-2图高亮设计规范)
3. [技术栈选型对比](#part-3技术栈选型对比)
4. [Faithfulness 工程方案](#part-4faithfulness-工程方案)
5. [风险清单](#part-5风险清单)
6. [MVP 路线图](#part-6mvp-路线图)

---

## 学术基础综述

### A1. W3C PROV-O 与 Buneman 溯源分类

**W3C PROV-O**（PROV Ontology）是 W3C 推荐标准，提供 OWL2 编码的溯源数据模型，核心三元：`Entity`（存在于某时刻的事物）、`Activity`（发生于一段时间的活动）、`Agent`（对活动负责的实体）。
来源：[W3C PROV-O: The PROV Ontology](https://www.w3.org/TR/prov-o/)

**Buneman et al. 2001** "Why and Where: A Characterization of Data Provenance" 将溯源分为：
- **Why provenance**：哪些源数据影响了结果的"存在"（因果）
- **Where provenance**：结果从哪个源位置被提取（位置）

来源：[Buneman et al., ICDT 2001](https://link.springer.com/chapter/10.1007/3-540-44503-X_20)

**L1（来源溯源）对应哪种类型？** 对应 **annotation-based provenance**（PROV-O 中的 Qualified Relationship 模式）：每个 KG 节点携带的 `dialog_id`/`session_idx` 字段，精确指向源文本位置，既是 "where"（位置）又是 "why"（该节点因该对话行而存在）的混合体。PROV-O 的 `prov:wasQuotedFrom` 和 `prov:hadPrimarySource` 属性直接对应这个语义。

### A2. ReAct 论文（Yao et al. 2022）

**Yao, S., Zhao, J., Yu, D., Du, N., Shafran, I., Narasimhan, K., & Cao, Y. (2022).** ReAct: Synergizing Reasoning and Acting in Language Models. arXiv:2210.03629.

核心定义：
- **Thought**：模型分析当前情况、生成推理轨迹的文本步骤
- **Action**：基于 Thought 采取的工具调用（如搜索数据库）
- **Observation**：工具返回的反馈/信息

论文明确主张 trace 是 explanation 的依据：*"generates human-like task-solving trajectories that are more interpretable than baselines without reasoning traces"*，以及 *"allows easy human inspection and behavior correction by changing a couple of model thoughts"*。

来源：[arXiv:2210.03629](https://arxiv.org/abs/2210.03629) | [Google Research Blog](https://research.google/blog/react-synergizing-reasoning-and-acting-in-language-models/)

### A3. Turpin et al. 2023 — CoT 可解释性争议

**Turpin, M., Michael, J., Perez, E., & Bowman, S. (2023).** Language Models Don't Always Say What They Think: Unfaithful Explanations in Chain-of-Thought Prompting. NeurIPS 2023.

核心发现：CoT 解释可系统性地歪曲模型真实预测原因。实验中，通过重排多选题选项（使答案总是"A"）引入偏置特征后，模型在 BIG-Bench Hard 13 个任务上准确率最多下降 **36%**，且其 CoT 解释从不提及这个偏置因素。结论：CoT 解释"plausible yet misleading"，在不保证安全性的同时可能提升对 LLM 的错误信任。

来源：[arXiv:2305.04388](https://arxiv.org/abs/2305.04388) | [NeurIPS 2023](https://neurips.cc/virtual/2023/poster/71118)

### A4. Jacovi & Goldberg 2020 — faithfulness vs plausibility

**Jacovi, A. & Goldberg, Y. (2020).** Towards Faithfully Interpretable NLP Systems: How Should We Define and Evaluate Faithfulness? ACL 2020.

正式定义：
- **Faithfulness**：解释准确反映模型真实推理过程（不撒谎）
- **Plausibility**（也称 persuasiveness / understandability）：解释对目标受众可理解且有说服力

关键警告：*"a plausible but unfaithful interpretation may be the worst-case scenario"*（有说服力但不忠实的解释是最糟情况），因为它增加信任却不保障安全。该论文呼吁放弃二元 faithfulness 定义，改用渐进式度量。

来源：[ACL Anthology](https://aclanthology.org/2020.acl-main.386/) | [arXiv:2004.03685](https://arxiv.org/abs/2004.03685)

### A5. KG 推理路径解释代表工作

**PathCon（Wang et al., KDD 2021）**：同时编码关系上下文（k-hop 邻居关系）和关系路径（实体间连接），用 RNN 学习路径表示进行知识图谱补全。路径选择标准：路径长度 ≤ k（通常 k≤3，避免指数级路径数），信息量由 RNN 编码决定。
来源：[PathCon KDD 2021 PDF](https://www-cs-faculty.stanford.edu/people/jure/pubs/pathcon-kdd21.pdf) | [GitHub](https://github.com/hwwang55/PathCon)

**PaGE-Link（Zhang et al., WWW 2023）**：为异构图链接预测生成路径解释，具备连接可解释性、模型可扩展性、图异构性处理能力。评估结果：生成的路径解释使推荐 AUC 提升 9-35%，78.79% 的人工评估者选择其解释优于基线。路径选择标准：**connection interpretability**（路径自然捕获两节点间连接）+ **model scalability**（稀疏路径）。
来源：[arXiv:2302.12465](https://arxiv.org/abs/2302.12465) | [WWW 2023 ACM](https://dl.acm.org/doi/10.1145/3543507.3583511)

**Path-based Explanation for KGC（Chang et al., KDD 2024）**：将路径视为头尾实体间的闭合路径，用嵌入相似度找最可靠路径，是 KGC 解释的最新 survey 代表工作。
来源：[arXiv:2401.02290](https://arxiv.org/pdf/2401.02290)

---

## Part 1：三层可解释性实现路径

### L1 — 来源溯源（Source Attribution）

| 维度 | 详情 |
|------|------|
| **数据来源** | KG 节点已有 `dialog_id`（对话 ID）和 `session_idx`（会话内编号）字段；对应原始文本段落可从聊天记录 DB 查询 |
| **后端补充** | `search_entity` / `lookup_node` 工具返回值中新增 `source_snippet` 字段（原始文本前后 ±1 句）；agent 最终 answer 字段新增 `cited_nodes: [node_id, ...]` 列表 |
| **前端呈现** | 文本：答案中每个关键主张标注上角标 `[节点名]`，悬停展示浮层（原始对话片段 + session/dialog 信息）；图：命中节点加描边高亮，点击跳转 |
| **难度评估** | ★★☆☆☆（2/5）— 字段已存在，主要是 API 层新增返回字段 + 前端悬停 UI |
| **实施顺序** | **M1 第一优先**，是 L2/L3 的基础 |

**理论基础**：对应 Buneman et al. 的 *where provenance*（位置）+ PROV-O 的 `prov:wasQuotedFrom`。L1 属于 **annotation-based provenance**：节点本身携带源标注，不需要重建推导链。

---

### L2 — 检索路径（Retrieval Path Animation）

| 维度 | 详情 |
|------|------|
| **数据来源** | ReAct trace 的完整 Thought/Action/Observation 序列（已由 MAX_ITER=5 限制上界）；每个 Action 携带工具名和参数（如 `search_entity("张三")`）；每个 Observation 携带返回节点 ID 列表 |
| **后端补充** | ① Trace 序列化接口：将 `List[TraceStep]` 结构化为 `{step_idx, tool, query, returned_node_ids, thought_text}` 的 JSON 数组；② Agent 运行结束后，从 Observation 字段提取所有 `node_id`，按步骤顺序组装 `trace_graph_path: [{step, nodes, edges_activated}]` |
| **前端呈现** | 图动画：右侧控制面板「步骤 1/5 ▶」，每步点亮对应节点 + 连接边；支持「自动播放」（500ms/步）和手动逐步；同时文本侧边栏显示该步 Thought 文字 |
| **难度评估** | ★★★☆☆（3/5）— trace 解析逻辑中等，前端动画时序控制需要工程投入 |
| **实施顺序** | **M2**，依赖 L1 完成 |

**理论基础**：Yao et al. (2022) 明确说明 trace 本身是 explanation 的载体，*"allows easy human inspection"*。L2 将这个 trace 直接映射为图上的动态路径——从 ReAct 论文的角度，这是最忠实（faithful）的可视化方式，因为展示的就是 agent 实际执行的步骤，而非事后重建。

---

### L3 — 推理链（Reasoning Chain Explanation）

| 维度 | 详情 |
|------|------|
| **数据来源** | L2 的结构化 trace + 每步 Observation 内容 + 最终答案；注意：这层依赖 LLM 生成自然语言解释，存在 faithfulness 风险（见 Part 4） |
| **后端补充** | ① 新增 `/explain` 端点：接收 `{question, trace, answer}`，调用 LLM 生成「先…再…所以…」格式的推理链文本；② Prompt 注入完整 trace（见 Part 4 模板）；③ 可选：post-hoc 校验步骤 |
| **前端呈现** | 文本区显示带编号步骤（"① 在图中找到节点'张三'→ ② 沿'认识'边找到'李四'→ ③ 查询事件节点'会议2023'→ 所以答案是..."）；图区同步动画高亮对应节点/边；支持点击步骤跳转到对应图状态 |
| **难度评估** | ★★★★☆（4/5）— LLM 生成解释的 faithfulness 难以保证（Turpin 2023 警告）；需要 trace-grounded prompt + post-hoc 校验 |
| **实施顺序** | **M3**，依赖 L2 + 校验框架 |

**核心警告**（基于 Turpin et al. 2023）：L3 生成的"先…再…所以…"文本属于**事后解释**，存在 unfaithful 风险——LLM 可能生成听起来合理但不反映真实推理路径的文字。必须通过 trace-grounded prompting 和 post-hoc 校验缓解（详见 Part 4）。

---

## Part 2：图高亮设计规范

### 2.1 节点状态定义（6种）

| 状态 | 触发条件 | 视觉表现 |
|------|----------|----------|
| **DEFAULT** 默认 | 未被 trace 涉及 | 节点：`fill: #2A2D3E`，边框：`#4A4D5E 1px`，标签：`#8B8FA8`，透明度：`opacity: 0.35` |
| **CANDIDATE** 候选 | trace 中 `search_entity` 的查询目标，尚未确认命中 | 节点：`fill: #1E3A5F`，边框：`#4A90D9 1.5px dashed`，标签：`#7BB3E0`，opacity：`0.65` |
| **HIT** 命中 | Observation 返回的节点（有效检索结果） | 节点：`fill: #1A4A3A`，边框：`#4CAF80 2px solid`，标签：`#80EAB5`，opacity：`1.0`，轻微 box-shadow：`0 0 8px #4CAF8066` |
| **FOCUSED** 当前关注 | 动画播放时"当前步骤"正在关注的节点 | 节点：`fill: #2D4A6A`，边框：`#64B5F6 3px solid`，标签：`#FFFFFF bold`，opacity：`1.0`，pulse 动画（缩放 1.0→1.08→1.0，600ms） |
| **VISITED** 已访问 | 前序步骤已处理完毕的命中节点 | 节点：`fill: #1A3A2A`，边框：`#2E7D52 1.5px`，标签：`#5CB87A`，opacity：`0.75`，无动画 |
| **FAILED** 失败分支 | Observation 返回空、或 `lookup_node` 未找到的查询 | 节点：`fill: #3A1E1E`，边框：`#E57373 1.5px dashed`，标签：`#E57373`，opacity：`0.5`，X 角标 |

### 2.2 边状态定义（3种）

| 状态 | 触发条件 | 视觉表现 |
|------|----------|----------|
| **INACTIVE** 未激活 | 默认，不在推理路径上 | `stroke: #3A3D4E`，`stroke-width: 1px`，opacity：`0.2` |
| **TRAVERSED** 已遍历 | trace 步骤中被跟随的边（已完成） | `stroke: #4CAF80`，`stroke-width: 2px`，opacity：`0.7`，`stroke-dasharray: none` |
| **ACTIVE** 当前激活 | 动画当前步骤正在通过的边 | `stroke: #64B5F6`，`stroke-width: 3px`，opacity：`1.0`，流动虚线动画（`stroke-dashoffset` 递减，300ms/cycle） |

### 2.3 动画时序

```
每步停留时间（自动播放）：500ms（可在设置中调整为 200ms~2000ms）
FOCUSED pulse 动画：600ms，ease-in-out
边 ACTIVE 流动动画：300ms/cycle，持续至步骤切换
步骤切换缓动：前一步节点从 FOCUSED→VISITED（300ms fade），新步节点从 DEFAULT/CANDIDATE→FOCUSED（300ms）
用户控制：
  ⏮ 重置到初始状态
  ◀ 上一步
  ▶/⏸ 自动播放/暂停
  ▶ 下一步
  速度滑块：0.5x / 1x / 2x
```

### 2.4 配色原则（深色主题）

- 背景：`#1A1C2E`（深海军蓝）
- 主色调：以蓝绿为主（`#4CAF80` 命中绿，`#64B5F6` 关注蓝），饱和度克制
- 警示色：`#E57373`（失败红）仅用于失败状态，不泛滥
- 未激活节点 opacity 降至 0.35，制造自然的 focus+context 视觉层次（参考 Furnas 1986 fisheye 原理：距焦点越远，细节越少）
- 字体大小不随状态变化（避免布局抖动），仅通过颜色和 opacity 区分

来源（focus+context 理论）：[Sarkar & Brown, CHI 1992 — Graphical Fisheye Views](https://www.cs.montana.edu/courses/spring2005/430/pg/ft_gateway.cfm.pdf) | [Cockburn et al. 2007 Review of F+C interfaces](https://worrydream.com/refs/Cockburn_2007_-_A_Review_of_Overview+Detail,_Zooming,_and_Focus+Context_Interfaces.pdf)

---

### 2.5 ASCII Wireframe

**Wireframe A：主界面布局（L2 路径动画状态）**

```
┌─────────────────────────────────────────────────────────────────────────┐
│  ConvoMemory                                          [设置] [历史]     │
├──────────────────────────────────┬──────────────────────────────────────┤
│                                  │  QUESTION                            │
│         知识图谱视图              │  "张三和李四在哪次会议上认识的？"      │
│                                  │                                      │
│   [节点:张三●]──认识──[节点:李四○]│  ANSWER                              │
│        │                   │    │  "他们在 2023 年北京峰会上认识。"      │
│       参加                 参加  │  来源：[节点:会议2023▲] ¹            │
│        │                   │    │                                      │
│   [节点:峰会2023★]──地点──[北京] │  ─────────────────────────────────  │
│                                  │  TRACE (步骤 2 / 4)                  │
│   ● DEFAULT   ○ CANDIDATE        │                                      │
│   ▲ HIT       ★ FOCUSED          │  💭 需要找张三和李四的共同事件        │
│                                  │  🔧 search_entity("张三", "参加")    │
│  ────────── 动画控制 ──────────   │  📋 返回节点: 峰会2023, 研讨会2022  │
│  ⏮  ◀  ▶  ▶|    速度: [───●──]  │                                      │
│  步骤: 2/4  [████░░░░]           │  [¹ 原文] "张三和李四在北京峰会..."  │
└──────────────────────────────────┴──────────────────────────────────────┘
```

**Wireframe B：节点悬停浮层（L1 来源溯源）**

```
                    ┌──────────────────────────────────┐
                    │ 节点：峰会2023                    │
   [峰会2023★]◄────│ 类型：EVENT                       │
      │ (hover)    │ 属性：date="2023-11", loc="北京"  │
      │            │                                   │
      │            │ 来源对话                           │
      │            │ ┌─────────────────────────────┐  │
      │            │ │ Session 3 · Dialog 47        │  │
      │            │ │ "...两人在北京峰会的茶歇期间  │  │
      │            │ │  首次交谈..."                 │  │
      │            │ └─────────────────────────────┘  │
      │            │                                   │
      │            │ [↗ 跳转到对话原文]                │
      │            └──────────────────────────────────┘
```

---

## Part 3：技术栈选型对比

### 3.1 四库对比表

| 维度 | D3.js v7 | **Cytoscape.js** | Sigma.js v3 | vis-network |
|------|----------|-----------------|-------------|-------------|
| **上手难度** | ★★★★★（极高，手写 SVG/force） | ★★★☆☆（中，API 文档完善） | ★★★☆☆（中，需了解 WebGL） | ★★☆☆☆（低，开箱即用） |
| **500+ 节点性能** | ★★★☆☆（SVG 在 500+ 节点时掉帧） | ★★★☆☆（Canvas，中等） | ★★★★★（WebGL，可到万级） | ★★☆☆☆（Canvas，500+ 明显变慢） |
| **路径动画原生支持** | 无原生，需手写 `stroke-dashoffset` tween | `ele.animate()` 原生支持，`cy.batch()` 批量更新 | 无原生，需 `reducers` 手动驱动颜色 | 有 `network.moveTo()` 和 `fit()`，无路径动画 |
| **React 集成难度** | ★★★★☆（需 `useRef` + `useEffect` 手动管理 DOM） | ★★☆☆☆（有 `react-cytoscapejs` 官方封装） | ★★★☆☆（有 `react-sigma` 库） | ★★☆☆☆（有 `react-vis-network`） |
| **样式系统** | CSS-in-JS 或内联 SVG 属性 | CSS 选择器样式表，支持状态类 | reducer 函数动态属性 | `options` 对象，无 CSS 类 |
| **社区 / 维护** | 极活跃，D3 v7 | 活跃，v3.30+ | 活跃重写中（v3 已稳定） | 维护中，更新较慢 |

来源：[Cytoscape.js 官方文档](https://js.cytoscape.org/) | [Sigma.js 官方](https://www.sigmajs.org/) | [vis-network 官方文档](https://visjs.github.io/vis-network/docs/) | [D3.js 官方](https://d3js.org/)

### 3.2 路径高亮核心代码示例

**Cytoscape.js — 路径高亮 + 动画（推荐方案）**

```javascript
// 1. 定义状态样式类
const styleSheet = [
  { selector: 'node', style: { 'background-color': '#2A2D3E', 'opacity': 0.35 } },
  { selector: 'node.hit',     style: { 'background-color': '#1A4A3A', 'border-color': '#4CAF80', 'border-width': 2, 'opacity': 1 } },
  { selector: 'node.focused', style: { 'background-color': '#2D4A6A', 'border-color': '#64B5F6', 'border-width': 3, 'opacity': 1 } },
  { selector: 'node.visited', style: { 'background-color': '#1A3A2A', 'border-color': '#2E7D52', 'opacity': 0.75 } },
  { selector: 'node.failed',  style: { 'background-color': '#3A1E1E', 'border-color': '#E57373', 'opacity': 0.5 } },
  { selector: 'edge.active',  style: { 'line-color': '#64B5F6', 'width': 3, 'opacity': 1,
      'line-dash-pattern': [6, 3], 'line-dash-offset': 0 } },
  { selector: 'edge.traversed', style: { 'line-color': '#4CAF80', 'width': 2, 'opacity': 0.7 } },
];

// 2. 播放 trace 某步骤
function playTraceStep(cy, step) {
  const { focused_nodes, hit_nodes, active_edges } = step;
  cy.batch(() => {
    // 清除上一步的 focused
    cy.nodes('.focused').removeClass('focused').addClass('visited');
    cy.edges('.active').removeClass('active').addClass('traversed');
    // 激活当前步骤
    cy.nodes(focused_nodes.map(id => `#${id}`).join(',')).addClass('focused');
    hit_nodes.forEach(id => cy.getElementById(id).addClass('hit'));
    active_edges.forEach(id => cy.getElementById(id).addClass('active'));
  });
  // FOCUSED pulse 动画
  focused_nodes.forEach(id => {
    cy.getElementById(id).animate(
      { style: { width: '+=8', height: '+=8' } },
      { duration: 300, complete: () =>
          cy.getElementById(id).animate({ style: { width: '-=8', height: '-=8' } }, { duration: 300 })
      }
    );
  });
}
```

来源：[Cytoscape.js ele.animate() docs](https://js.cytoscape.org/) | [cytoscape.js-view-utilities extension](https://github.com/iVis-at-Bilkent/cytoscape.js-view-utilities)

**D3.js — 边流动动画参考（不推荐作主方案，代码复杂度高）**

```javascript
// stroke-dashoffset 驱动边流动效果
const path = d3.select(`#edge-${edgeId}`);
const totalLength = path.node().getTotalLength();
path
  .style('stroke-dasharray', `${totalLength} ${totalLength}`)
  .style('stroke-dashoffset', totalLength)
  .transition().duration(600).ease(d3.easeLinear)
  .style('stroke-dashoffset', 0);
```

来源：[SVG Path animations with D3.js tutorial](https://mikeheavers.com/tutorials/svg_path_animations_with_d3/) | [D3 Animation docs](https://observablehq.com/@d3/learn-d3-animation)

### 3.3 最终推荐：Cytoscape.js

**理由**：
1. **`react-cytoscapejs`** 提供成熟的 React 封装，无需手动管理 DOM
2. **CSS 样式类系统**（`addClass`/`removeClass`）与"节点状态机"设计天然匹配，6 种状态只需切换 CSS 类
3. **`ele.animate()` + `cy.batch()`** 原生支持路径动画，无需自行实现 tween
4. **`eles.dijkstra()` 等图算法内置**，未来如需计算最短路径展示可直接使用
5. 500 节点 Canvas 渲染性能在 ConvoMemory 典型图规模下完全够用（对话型 KG 通常 < 1000 节点）

Sigma.js 的 WebGL 性能更好，但路径动画需完全手动实现（通过 `reducers`），且样式系统不如 Cytoscape.js 直观，在 ConvoMemory 这种中等规模图中优势不显著。

---

## Part 4：Faithfulness 工程方案

### 4.1 核心问题定位

**工作假设**：直接让 LLM "解释为什么"可能产生 plausible 但 unfaithful 的解释（Turpin et al. 2023；Jacovi & Goldberg 2020）。

**缓解策略**：trace-grounded prompting（将完整 trace 注入 prompt，强制 LLM 以 trace 为唯一信息源生成解释）+ post-hoc 一致性校验。

### 4.2 Agent 解释 Prompt 模板（可直接使用）

```
系统提示：
你是 ConvoMemory 的推理解释助手。你的任务是根据以下 ReAct agent 的执行轨迹，
用自然语言解释 agent 是如何得出答案的。

约束：
- 你的解释必须严格基于下方 [TRACE] 中的步骤，不得引入 trace 以外的信息
- 每个推理步骤必须对应到 trace 中的一个具体 Action 或 Observation
- 格式要求：按步骤编号输出，格式为 "步骤N：[动作描述] → [发现/结果]"，最后一行为 "因此：[结论]"
- 如果 trace 中没有某个信息，写"（trace 中未记录）"而不是编造

[TRACE]
问题：{question}

{formatted_trace}
（以下为 trace 格式：每条包含 step/thought/action/tool/query/observation）

[ANSWER]
{answer}

[CITED_NODES]
{cited_node_ids_with_labels}

请用中文输出解释，格式严格遵守上述约束。
```

**Trace 格式化示例**（`{formatted_trace}` 的内容）：

```
步骤1:
  Thought: 需要找到张三的社交关系，看他和谁有"认识"或"合作"关系
  Action: search_entity(entity="张三", relation_type="认识")
  Observation: 找到节点 [李四(node_042), 王五(node_098)]

步骤2:
  Thought: 李四和张三都认识，需要找两人的共同事件
  Action: search_semantic(query="张三 李四 共同参加", node_types=["EVENT"])
  Observation: 找到事件节点 [峰会2023(node_201), 研讨会2022(node_187)]

步骤3:
  Thought: 峰会2023 和研讨会2022 都是可能答案，需要确认哪个是两人首次见面
  Action: lookup_node(node_id="node_201")
  Observation: 峰会2023 属性: {date: "2023-11", location: "北京", attendees: ["张三", "李四"], note: "两人在此初识"}

步骤4:
  Thought: node_201 的 note 字段明确记录了"两人在此初识"，答案确定
  Action: finish(answer="他们在 2023 年北京峰会上认识")
```

### 4.3 Post-hoc 校验伪代码

```python
def verify_explanation_faithfulness(
    trace: list[TraceStep],
    explanation: str,
    cited_nodes: list[str],
    answer: str
) -> tuple[bool, str]:
    """
    校验 LLM 生成的解释是否忠实于 trace。
    返回 (is_faithful, reason)
    """
    # 规则1：解释中每个步骤编号必须存在于 trace 中
    mentioned_steps = extract_step_numbers(explanation)  # regex: "步骤\d+"
    trace_steps = {step.step_idx for step in trace}
    hallucinated_steps = mentioned_steps - trace_steps
    if hallucinated_steps:
        return False, f"解释引用了不存在的步骤: {hallucinated_steps}"

    # 规则2：解释中提及的节点 ID / 名称必须出现在 trace 的 Observation 中
    mentioned_nodes = extract_entity_names(explanation)  # NER 或正则
    observed_nodes = {obs_item for step in trace for obs_item in step.observation_entities}
    hallucinated_nodes = mentioned_nodes - observed_nodes - {answer}
    if hallucinated_nodes:
        return False, f"解释引用了 trace 中未出现的实体: {hallucinated_nodes}"

    # 规则3：cited_nodes 必须在 trace 的 Observation 中出现过
    for node_id in cited_nodes:
        if not any(node_id in step.observation_node_ids for step in trace):
            return False, f"引用节点 {node_id} 未出现在任何 Observation 中"

    # 规则4：解释的结论必须包含 answer 中的关键词（简单字符串匹配）
    answer_keywords = extract_keywords(answer)  # jieba 分词 + 停词过滤
    conclusion_text = extract_conclusion(explanation)  # "因此：" 之后的文本
    if not any(kw in conclusion_text for kw in answer_keywords):
        return False, "解释结论与最终答案不一致"

    return True, "OK"


def generate_explanation_with_fallback(
    trace, answer, cited_nodes, question,
    max_retries=2
) -> str:
    for attempt in range(max_retries):
        explanation = call_llm(EXPLANATION_PROMPT.format(
            question=question,
            formatted_trace=format_trace(trace),
            answer=answer,
            cited_node_ids_with_labels=format_cited_nodes(cited_nodes)
        ))
        is_faithful, reason = verify_explanation_faithfulness(
            trace, explanation, cited_nodes, answer
        )
        if is_faithful:
            return explanation
        # 重试时在 prompt 中注入失败原因
        EXPLANATION_PROMPT += f"\n[上次生成失败原因：{reason}，请修正]\n"

    # 降级策略：返回结构化模板而非 LLM 生成文本
    return generate_template_explanation(trace, answer, cited_nodes)


def generate_template_explanation(trace, answer, cited_nodes) -> str:
    """降级：完全基于 trace 的模板化解释，无 LLM 参与，100% faithful"""
    lines = []
    for step in trace:
        lines.append(f"步骤{step.step_idx}：使用 {step.tool}({step.query}) → 找到 {', '.join(step.observation_labels)}")
    lines.append(f"因此：根据节点 {', '.join(cited_nodes)} 的信息，答案为"{answer}"")
    return "\n".join(lines)
```

### 4.4 降级策略层次

```
Level 1（正常）：LLM 生成解释 + post-hoc 校验通过
Level 2（软降级）：LLM 生成失败，retry（最多2次，注入失败原因）
Level 3（硬降级）：全部 retry 失败，使用 generate_template_explanation()
              — 完全基于 trace 机械拼接，faithfulness = 100%
              — 显示提示文字："（系统使用简化解释模式）"
Level 4（最终降级）：trace 本身损坏，只展示 cited_nodes 的原始文本节点内容
              — 不提供推理链，只提供来源节点（L1 保底）
```

**理论基础**：Level 3 硬降级对应 Anthropic Citations API 的设计原则——"citations are guaranteed to contain valid pointers to the provided documents"，即通过系统约束（而非 LLM 生成）保证可溯源性。
来源：[Anthropic Citations API Docs](https://platform.claude.com/docs/en/docs/build-with-claude/citations) | [Introducing Citations on the Anthropic API](https://claude.com/blog/introducing-citations-api)

---

## Part 5：风险清单

### R1：解释不忠实（Unfaithfulness）

**风险**：LLM 生成的 L3 推理链解释是 plausible 但 unfaithful 的——即解释听起来合理，但不反映 agent 真实推理路径，甚至描述不存在的推理步骤。用户会错误地信任这个解释。

**文献证据**：Turpin et al. (2023) 在 13 个 BIG-Bench Hard 任务上实测 CoT 解释系统性误导，准确率下降高达 36%。Jacovi & Goldberg (2020) 明确指出 "plausible but unfaithful interpretation may be the worst-case scenario"。
来源：[arXiv:2305.04388](https://arxiv.org/abs/2305.04388) | [ACL 2020](https://aclanthology.org/2020.acl-main.386/)

**防御措施**：trace-grounded prompt（强制 LLM 仅引用 trace 内容）+ post-hoc 校验 + 硬降级到模板解释（见 Part 4）；前端对 L3 标注"AI 生成解释，请参考原始 trace 验证"。

---

### R2：过度信任偏差（Over-Trust）

**风险**：可视化路径高亮和自然语言解释会显著提升用户对 agent 答案的信任，即使答案本身是错误的。用户看到漂亮的路径动画后，倾向于不质疑结论。

**文献证据**：Turpin et al. (2023) 直接警告 "risks increasing trust in LLMs without guaranteeing their safety"。人机交互领域的 automation bias 文献（Parasuraman & Manzey, 2010）记录了可视化解释反而降低批判性评估的现象。

**防御措施**：在答案区始终显示置信度指示（低置信度时橙色警告）；提供"怀疑此答案"按钮触发 agent 重新推理；对 L3 解释加免责注释。

---

### R3：图过载（Graph Overload）

**风险**：当聊天记录量大时，图节点数 > 500，动画播放期间所有非路径节点 opacity 降低，但布局仍然复杂，用户难以追踪高亮路径。

**文献证据**：Cambridge Intelligence 图可视化 UX 指南指出，在缺乏 focus+context 机制时，节点数超过 300 的图可视化显著降低用户任务完成率。
来源：[Cambridge Intelligence Graph Viz UX Guide](https://cambridge-intelligence.com/graph-visualization-ux-how-to-avoid-wrecking-your-graph-visualization/)

**防御措施**：动画播放时自动 `cy.fit()` 到路径节点的子图视口；提供"仅显示推理路径"toggle（隐藏非路径节点）；实现 focus+context 布局（Furnas 1986 原理）。

---

### R4：Trace 被 Agent 截断（Incomplete Trace）

**风险**：MAX_ITER=5 限制下，agent 可能在找到最佳路径前被截断，导致 trace 不完整，L2 动画展示的是"失败路径"或"中间状态"而非最优路径。

**文献证据**：Yao et al. (2022) 指出 ReAct 在某些任务中出现 "repetitive loops" 和 "context drift"，Towards Data Science 2024 文章 "Your ReAct Agent Is Wasting 90% of Its Retries" 记录了迭代效率问题。
来源：[arXiv:2210.03629](https://arxiv.org/abs/2210.03629) | [TDS 2024](https://towardsdatascience.com/your-react-agent-is-wasting-90-of-its-retries-heres-how-to-stop-it/)

**防御措施**：L2 动画中对未完成/被截断步骤使用 FAILED 状态；在动画控制区显示"路径完整性：4/5 步有效"；超过 3 次 FAILED 步骤时，向用户显示警告"推理路径不完整，答案可信度降低"。

---

### R5：节点/Dialog 映射缺失（Missing L1 Provenance）

**风险**：部分 KG 节点在抽取时未能正确携带 `dialog_id`/`session_idx`（如 CONCEPT 类型节点或跨 session 聚合的 FACT 节点），导致 L1 溯源功能对这些节点无法工作，用户点击"跳转原文"时看到空白。

**防御措施**：KG 抽取阶段强制验证：每个节点必须有 `dialog_id`（对于聚合节点记录为列表）；L1 前端对无溯源节点显示"（来源不可追踪）"占位符而非空白；建立 provenance 覆盖率监控指标（目标 > 95%）。

---

### R6：路径动画与文本不同步（Sync Gap）

**风险**：图动画与侧边栏 Thought 文字的渲染不同步——在网络延迟或渲染性能较差设备上，动画已经进入下一步，但文字尚未更新，造成用户困惑。

**防御措施**：使用 React `useReducer` 管理统一的 `currentStep` 状态，图动画和文字面板订阅同一状态源；动画回调 `cy.batch()` 完成后再触发文字更新；针对低性能设备提供"禁用动画"模式（仅保留步骤切换，无 transition）。

---

### R7：隐私泄露（Privacy Leakage via Provenance）

**风险**：L1 溯源直接显示原始聊天记录片段。在多用户场景或共享链接场景下，可能将用户 A 的私人对话暴露给用户 B。

**防御措施**：溯源 snippet 接口必须经过 session-level 权限检查（仅允许该对话所属用户查看）；分享链接功能默认不包含溯源内容，需用户明确开启；`source_snippet` 字段在 API 响应中默认不返回，需前端明确请求。

---

### R8：Cytoscape.js 大图内存问题（Performance Risk）

**风险**：对话记录超过 500 次对话时，图节点数可能达到 2000+。Cytoscape.js 在 Canvas 模式下 2000+ 节点 + 动画并发时，内存占用超过 500MB，在移动端或低端浏览器崩溃。

**文献证据**：[Sigma.js 官方文档](https://www.sigmajs.org/) 明确指出 WebGL 渲染可处理"thousands of nodes and edges"，而 Canvas-based 方案在大图上有明显瓶颈。

**防御措施**：超过 1000 节点时自动降级为 Sigma.js（WebGL 渲染）；实现"社区分组"收折（聚合同一 session 的节点）；提供图过滤器（只显示与推理路径距离 ≤ 2 的节点）。

---

## Part 6：MVP 路线图

### M1：来源溯源（L1）

**目标**：答案中的关键实体可点击，弹出原始对话片段。

**核心功能**：
- 后端：`search_entity` / `lookup_node` 返回值新增 `source_snippet` + `source_meta`（session_idx, dialog_id, timestamp）
- 后端：agent 最终答案结构中新增 `cited_nodes: [node_id, ...]`
- 前端：答案文本中对 `cited_nodes` 对应实体名高亮，悬停显示浮层（Wireframe B）
- 前端：图中命中节点加 HIT 样式（绿色描边）

**工程量估计**：
- 后端 API 修改：2 天
- 前端悬停 UI + 图样式：2 天
- 端到端测试：1 天
- **合计：5 天**

**验证目标**：
- 5 条测试问题，每条答案至少有 1 个可点击的来源节点
- 来源节点溯源覆盖率 ≥ 90%（cited_nodes 中有 source_snippet 的比例）
- 用户测试：3 名测试用户能在 10 秒内找到并点击来源

**遗留问题**：CONCEPT 类型节点的 dialog_id 可能为空，需要在 M1 期间评估覆盖率并制定修复方案。

---

### M2：检索路径动画（L2）

**目标**：用户可以逐步观看 agent 的检索过程在图上动态展现。

**核心功能**：
- 后端：新增 `trace_graph_path` 字段（结构：`[{step_idx, tool, query, returned_node_ids, active_edge_ids, thought}]`），在 agent 执行后从 trace 中解析
- 后端：图的边数据结构新增 `edge_id`（确保可以按 ID 查找）
- 前端：实现 6 种节点状态 + 3 种边状态的 Cytoscape.js 样式类（设计规范见 Part 2）
- 前端：动画控制面板（⏮◀▶▶| + 速度滑块）
- 前端：侧边栏显示当前步骤的 Thought 文本

**工程量估计**：
- Trace 解析后端：2 天
- Cytoscape.js 样式系统：2 天
- 动画控制面板 UI：2 天
- 图-文本同步逻辑：1 天
- 测试 + 调试：2 天
- **合计：9 天**

**验证目标**：
- 10 条测试问题，L2 动画步骤数与实际 trace 步骤数 100% 一致
- 动画流畅度：500 节点图上 60fps（Chrome + 标准笔记本）
- 用户测试：5 名用户能通过观看动画正确描述 agent 做了哪些检索步骤

**遗留问题**：MAX_ITER 截断场景下动画如何优雅处理 FAILED 步骤（需要 M2 期间 UX 决策）。

---

### M3：推理链文本解释（L3）

**目标**：在 L2 动画基础上，提供"先…再…所以…"格式的自然语言推理解释，且具备 faithfulness 保障。

**核心功能**：
- 后端：`/explain` 端点（接收 `{question, trace, answer, cited_nodes}`，返回结构化解释文本）
- 后端：post-hoc 校验逻辑（见 Part 4 伪代码），校验失败自动降级
- 后端：硬降级 `generate_template_explanation()` 函数
- 前端：解释文本区（步骤编号列表，点击步骤联动图动画跳转到对应步骤）
- 前端：对 L3 解释显示"AI 生成，基于 trace 验证"标注；降级模式下显示"（简化解释模式）"

**工程量估计**：
- `/explain` 端点 + LLM 调用：2 天
- Post-hoc 校验逻辑：2 天
- 硬降级模板生成：1 天
- 前端步骤联动点击：2 天
- Faithfulness 评测（人工标注 20 条）：2 天
- **合计：9 天**

**验证目标**：
- L3 解释 faithfulness 评测：20 条人工标注，post-hoc 校验精确率 ≥ 80%（即校验通过的解释中，确实 faithful 的比例）
- 校验召回率 ≥ 70%（unfaithful 解释被正确拦截的比例）
- 降级触发率 ≤ 15%（即绝大多数情况下 LLM 生成解释可通过校验）
- 用户测试：5 名用户中，阅读解释后能正确回答"agent 第二步做了什么"的比例 ≥ 80%

**遗留问题**：
1. L3 的 faithfulness 上限受制于 LLM 能力，Turpin et al. (2023) 的警告意味着即使有 post-hoc 校验，仍可能存在我们无法检测的微妙不忠实。论文中需如实说明这一限制。
2. 中文实体 NER（用于 post-hoc 校验中的实体提取）精度需要评估，建议使用 `jieba` + 自定义词典（ConvoMemory 图的 PERSON/EVENT 节点名）。
3. 当 MAX_ITER 设为 5 时，L3 解释最长 5 步，前端文本区高度设计需要兼容 1-5 步的变长内容。

---

## 产品对比总结

| 产品 | 解释模态 | 逐步动画 | 可点击源跳转 | 粒度 |
|------|----------|----------|--------------|------|
| **Perplexity AI** | 文本内联引用编号 | 无 | 有（展开 snippet） | 句子级 |
| **Microsoft GraphRAG** | 社区摘要文本 | 无（Gephi 静态图） | 无（导出文件） | 社区级 |
| **Neo4j Bloom** | 图形探索 | 无专用动画，可手动导航 | 节点属性面板 | 节点属性级 |
| **LangSmith** | ReAct trace JSON 树 | 时间线视图（非图动画） | 无（仅 JSON 展开） | span 级 |
| **LangFuse** | 时间线 + Agent 图 | 时间线，无步骤动画 | 无源文档跳转 | span/observation 级 |
| **Elicit** | 句子级引用 + 引文图 | 无 | 有（跳转论文原文） | 句子级 |
| **ConvoMemory 目标** | **文本+图两者联动** | **有（5步动画，可控）** | **有（原始对话段落）** | **节点+句子双粒度** |

来源：
- Perplexity：[ZipTie.dev 技术分析](https://ziptie.dev/blog/how-perplexity-ai-answers-work/) | [Perplexity Help Center](https://www.perplexity.ai/help-center/en/articles/10352895-how-does-perplexity-work)
- GraphRAG：[microsoft/graphrag GitHub](https://github.com/microsoft/graphrag) | [GraphRAG Visualization Guide](https://microsoft.github.io/graphrag/visualization_guide/)
- Neo4j Bloom：[Neo4j Bloom 官方文档](https://neo4j.com/docs/bloom-user-guide/current/)
- LangSmith：[LangSmith Observability Docs](https://docs.langchain.com/oss/python/langgraph/observability) | [DigitalOcean LangSmith Guide](https://www.digitalocean.com/community/tutorials/langsmith-debudding-evaluating-llm-agents)
- LangFuse：[LangFuse Agent Graphs Docs](https://langfuse.com/docs/observability/features/agent-graphs) | [LangFuse Timeline View](https://langfuse.com/changelog/2024-06-12-timeline-view)
- Elicit：[Elicit 官网](https://elicit.com/) | [NIH PMC Review](https://pmc.ncbi.nlm.nih.gov/articles/PMC10089336/)

**关键洞察**：现有工具要么只有文本引用（Perplexity、Elicit），要么只有 trace 日志（LangSmith、LangFuse），没有一个产品实现了"图动画 + 自然语言推理链 + 原始来源跳转"的三合一。这是 ConvoMemory L2+L3 的差异化机会。

---

## 参考文献汇总

1. Yao, S. et al. (2022). ReAct: Synergizing Reasoning and Acting in Language Models. arXiv:2210.03629. https://arxiv.org/abs/2210.03629
2. Turpin, M. et al. (2023). Language Models Don't Always Say What They Think. NeurIPS 2023. https://arxiv.org/abs/2305.04388
3. Jacovi, A. & Goldberg, Y. (2020). Towards Faithfully Interpretable NLP Systems. ACL 2020. https://aclanthology.org/2020.acl-main.386/
4. Buneman, P., Khanna, S., & Tan, W-C. (2001). Why and Where: A Characterization of Data Provenance. ICDT 2001. https://link.springer.com/chapter/10.1007/3-540-44503-X_20
5. W3C PROV-O. (2013). PROV-O: The PROV Ontology. W3C Recommendation. https://www.w3.org/TR/prov-o/
6. Zhang, S. et al. (2023). PaGE-Link: Path-based GNN Explanation for Heterogeneous Link Prediction. WWW 2023. https://arxiv.org/abs/2302.12465
7. Wang, H. et al. (2021). Relational Message Passing for Knowledge Graph Completion (PathCon). KDD 2021. https://www-cs-faculty.stanford.edu/people/jure/pubs/pathcon-kdd21.pdf
8. Chang, H. et al. (2024). Path-based Explanation for Knowledge Graph Completion. KDD 2024. https://arxiv.org/pdf/2401.02290
9. Anthropic. (2025). Citations API Documentation. https://platform.claude.com/docs/en/docs/build-with-claude/citations
10. Furnas, G. W. (1986). Generalized fisheye views. CHI 1986. (cited via Cockburn et al. 2007 review https://worrydream.com/refs/Cockburn_2007_-_A_Review_of_Overview+Detail,_Zooming,_and_Focus+Context_Interfaces.pdf)
11. Cytoscape.js Documentation. https://js.cytoscape.org/
12. Sigma.js Documentation. https://www.sigmajs.org/
13. vis-network Documentation. https://visjs.github.io/vis-network/docs/
14. LangFuse Agent Graphs. https://langfuse.com/docs/observability/features/agent-graphs
15. Microsoft GraphRAG GitHub. https://github.com/microsoft/graphrag
