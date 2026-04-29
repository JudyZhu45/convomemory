# ConvoMemory 知识图谱可视化调研报告

**调研日期**：2026-04-29  
**目标产品**：ConvoMemory（对话→知识图谱，节点类型 PERSON/EVENT/STATE/FACT/CONCEPT）  
**核心问题**：PERSON 节点可能有 50-200 个邻居，force-directed 布局产生 hairball（毛球）问题

---

## Part 1：五维度分析

---

### 维度 1：Hairball 问题的成因与学术对策

#### 数学层面的成因

以 Fruchterman & Reingold 1991 为代表的 force-directed 布局将图抽象为物理系统：

- **吸引力**（边）：$f_a = d^2/k$
- **排斥力**（所有节点对）：$f_r = k^2/d$

**为什么 hub 节点（高度节点）会产生局部密度问题？**

对于度数为 $\delta(v)$ 的节点 $v$，其净吸引力合力大致正比于 $\delta(v)$（$\delta(v)$ 条边同时向邻居方向拉拽），而排斥力来自整图所有节点，与局部度数无关。三个根本原因：

**① 合力不平衡**：hub 节点受到的总吸引力 $\propto \delta(v) \cdot f_a$，远大于低度节点，导致能量收敛时 hub 的邻居密集堆积于其周围（每个邻居都被 hub 以相同强度吸引，而它们之间的排斥力不足以将 50-200 个节点分散到视口中）。

**② 角分辨率下降**：当 hub 有 $n$ 个邻居时，每个邻居理论上占据角度 $360°/n$。当 $n=100$ 时每邻居仅 3.6°，远低于人眼可分辨的 ~5-10°，节点标签完全重叠。（来源：[Wikipedia "Force-directed graph drawing"](https://en.wikipedia.org/wiki/Force-directed_graph_drawing) —— "angular resolution can be bounded below by a function of the degree"。）

**③ 全局冷却无法局部精细化**：FR 的退火温控对全图施加全局温度，当某区域（hub 周围）需要更精细调整而其他区域已收敛时，算法无法局部继续优化，因此 hub 周围停留在视觉上拥挤的局部最小值。Wikipedia 明确指出：这类算法 "produce a graph with minimal energy...only a local minimum"。

#### 学术奠基文献

**ForceAtlas2（Jacomy et al. 2014, PLOS ONE, [DOI:10.1371/journal.pone.0098679](https://doi.org/10.1371/journal.pone.0098679)）**

ForceAtlas2 是 Gephi 内置的算法，专门针对 scale-free 网络（幂律度分布，即有明显 hub 的网络）优化：

| 特性 | 描述 | 与 FR 的区别 |
|------|------|-------------|
| **Degree-Dependent Repulsion** | 排斥力 $\propto (deg(v)+1)(deg(u)+1)$，高度节点获得更大排斥空间 | FR 排斥力与度数无关 |
| **Dissuade Hubs** | 吸引力沿 outbound 边均匀分配（除以出度），hub 倾向向外推 | FR 每条边贡献相同吸引力 |
| **LinLog Mode** | 切换为 Noack LinLog 力模型，使簇更紧密、簇间分离更清晰 | FR 线性吸引力 |
| **Barnes-Hut 加速** | 四叉树近似将排斥力从 O(n²) 降至 O(n log n) | FR 标准 O(n²) |

基准测试：68 个网络（5–23,133 节点）上，ForceAtlas2 准最优收敛平均 **638ms**，而 Fruchterman-Reingold 需 **20,201ms**。

**Holten 2006 "Hierarchical Edge Bundles"（[DOI:10.1109/TVCG.2006.147](https://doi.org/10.1109/TVCG.2006.147)）**

利用层次结构构建控制多边形，相邻路径使用 B-spline 聚合。参数 $\beta \in [0,1]$：$\beta=0$ 为直线，$\beta=0.85$ 通常视觉效果最佳。**局限**：① 必须有层次结构；② 只减少视觉杂乱，不减少边数；③ 对无层次的异构图需先构造人工层次。

**Heer & Boyd 2005 "Vizster"（[DOI:10.1109/INFVIS.2005.1532151](https://doi.org/10.1109/INFVIS.2005.1532151)）**

针对 Friendster 社交网络（数万节点）的可视化系统。核心思路：**Ego Network Focus**（默认显示以选定用户为中心的 1-hop 子图）+ **Search + Context**（搜索某用户时高亮该用户及其连接，其余节点灰显但保留）。

**van Ham & Perer 2009 "Search, Show Context, Expand on Demand"（[DOI:10.1109/TVCG.2009.108](https://doi.org/10.1109/TVCG.2009.108)）**

核心范式三步：**Search → Show Context → Expand on Demand**。初始只渲染搜索结果，按需增量加载邻居。当图的节点数在数千以上时，这是唯一能在浏览器内保持流畅的范式。

#### 公认的反 hairball 策略

| 策略 | 机制 | 适用场景 | 代价 |
|------|------|---------|------|
| **Edge Bundling** | 视觉上将平行路径合并为一条带 | 有层次结构或可定义路由的图 | 需要层次结构；β 过高时路径可读性下降 |
| **Node Aggregation** | 将高度相似或同类型节点折叠为超级节点 | 有明确类别且类内连接密集 | 损失细节；用户需交互下钻 |
| **Focus + Context（Fisheye）** | 焦点区域放大，上下文区域缩小 | 节点数中等（<500），需同时看局部细节和全局位置 | 失真可能误导；实现复杂 |
| **Constraint-Based Layout** | 强制约束位置，hub 居中，邻居分环 | 有明确中心节点、异构类型图 | 灵活性低；节点过多时同一环仍拥挤 |
| **Edge Filtering** | 只显示高权重/高重要性边 | 边有权重/类型属性 | 可能遗漏低权重但关键的连接 |

---

### 维度 2：类型化布局策略（异构图）

#### 同心环布局（Concentric Layout）

以 hub（PERSON）为圆心，按节点类型分配同心圆层级。Cytoscape.js 内置 `concentric` 布局（[文档](https://js.cytoscape.org/#layouts)）。对 ConvoMemory 的异构图扩展：

```
Ring 0（内）：PERSON（hub）
Ring 1：STATE（is_current=true）
Ring 2：EVENT（最近 N 条，按时间顺时针排列）
Ring 3：FACT + CONCEPT（各占 180° 扇区）
Ring 4：其他 PERSON（关系型）
```

**局限**：Ring 内超过 ~60 节点时仍会重叠。解决方案：每层内再按类型细分扇区。

#### 按边类型分扇区（Sector Layout）

基于 Multipartite 布局思想，将 hub 的邻居按边类型划分扇区（上半圆 STATE/FACT，右扇区 EVENT，左扇区 CONCEPT，下方 PERSON），每扇区内使用局部 force-directed，避免跨扇区干扰。用户无需颜色编码就能通过位置知道节点类型。

**最适合 ConvoMemory 的方案**：**同心环 + 扇区分类 + 按需展开**。理由：① ConvoMemory 核心场景是 ego-centric；② 节点有语义类型，同心环直接编码类型层次；③ 当邻居 50-200 时，扇区分类可将每个扇区限制在 20-40 节点。

---

### 维度 3：时间维度可视化

ConvoMemory 的状态链：

```
STATE_v1 --SUPERSEDED_BY--> STATE_v2 --SUPERSEDED_BY--> STATE_v3 (is_current=true)
```

| 方法 | 描述 | 评价 |
|------|------|------|
| **透明度/灰度编码** | `is_current=false` → 30-40% 透明度；SUPERSEDED_BY 边用虚线 | 实现简单，但无法表达时间顺序；过时节点仍占空间 |
| **同心环时间层** | 最新 STATE 最靠近 PERSON（内圈），历史 STATE 向外推，透明度随时间递减 | 视觉上传达"越新越近"隐喻；STATE 通常 <20 条，不造成密度问题 |
| **时间轴布局** | 时间维度映射到水平轴，STATE 按时间戳横向排列 | 最直观，但横向空间需求大，难以同时展示非时间节点 |
| **历史折叠（Expand on Demand）** | 过期 STATE 折叠为"历史摘要"节点，点击展开 | 最适合 ConvoMemory；默认减少视觉噪声，按需展开对应 van Ham & Perer 2009 |

**综合推荐**：默认只显示 `is_current=true` 节点，过期节点折叠为"历史胶囊"；用户点击胶囊后展开，切换到时间轴布局（水平 x=时间戳，`SUPERSEDED_BY` 边变为横向箭头）。

---

### 维度 4：真实产品调研

#### Obsidian Graph View
来源：[官方文档](https://obsidian.md/)

| 维度 | 详情 |
|------|------|
| 默认布局算法 | Force-directed（自定义物理引擎） |
| Hub 节点特殊处理 | 无自动处理；提供 Center Force + Repel Force 手动调节 |
| 用户可调参数 | Filters（标签/路径）、Display（节点大小按入链数缩放）、Forces（Center/Repel/Link distance/Link force）、Groups（按标签着色） |
| N 节点降级策略 | 无官方降级；节点超 ~5000 时卡顿；**Local Graph**（只显示当前笔记 1-hop）是隐式解法 |

#### Neo4j Bloom
来源：[官方文档](https://neo4j.com/docs/bloom-user-guide/current/bloom-visual-tour/settings-drawer/)

| 维度 | 详情 |
|------|------|
| 默认布局算法 | Force-directed（未在文档公开算法名） |
| Hub 节点特殊处理 | 未在公开文档说明 |
| 用户可调参数 | Node query limit（100-10000 可调）；WebGL 兼容模式；Pattern Search（自然语言 Cypher） |
| N 节点降级策略 | Query limit 限制返回节点数；**Pattern Search** 天然实现 expand-on-demand |

#### Cytoscape.js
来源：[官方文档](https://js.cytoscape.org/) | [fCoSE 扩展](https://github.com/iVis-at-Bilkent/cytoscape.js-fcose)

| 维度 | 详情 |
|------|------|
| 默认布局算法 | 内置：grid/circle/**concentric**/breadthfirst/preset；扩展：**fCoSE**（推荐）/Cola/Dagre/ELK |
| Hub 节点特殊处理 | **fCoSE 支持 Fixed Node Constraints**（可固定 hub 位置）；Cola 支持空间约束 |
| 用户可调参数 | 各布局独立参数；性能优化：`hideEdgesOnViewport`、`textureOnViewport` |
| N 节点降级策略 | 建议万节点以上关闭动画；推荐结合 `hideEdgesOnViewport` |

#### Sigma.js
来源：[官方网站](https://www.sigmajs.org/) | [GitHub](https://github.com/jacomyal/sigma.js)

| 维度 | 详情 |
|------|------|
| 默认布局算法 | **自身不提供布局**；与 graphology 配合：[graphology-layout-forceatlas2](https://graphology.github.io/standard-library/layout-forceatlas2.html) |
| Hub 节点特殊处理 | ForceAtlas2 `outboundAttractionDistribution`（对应 Dissuade Hubs）；`barnesHutTheta` 加速 |
| N 节点降级策略 | **WebGL 渲染**，官方声称可流畅处理"数千节点"；超 ~10,000 需配合 LOD 策略 |

#### vis-network
来源：[官方文档](https://visjs.github.io/vis-network/docs/network/)

| 维度 | 详情 |
|------|------|
| 默认布局算法 | **BarnesHut**（默认 physics solver）；备选：ForceAtlas2Based/Repulsion/HierarchicalRepulsion |
| Hub 节点特殊处理 | **`clusterByHubsize(threshold)`** — 自动将度数超阈值的节点及其邻居聚合为超节点；阈值不填时自动计算（均值+2σ） |
| N 节点降级策略 | 官方文档："works smooth...for up to a few thousand nodes and edges"；**clustering API 是官方推荐的大图降级路径** |

> vis-network 是唯一在标准文档中提供 `clusterByHubsize` 方法的库，对 ConvoMemory 的自动折叠 50-200 邻居有直接支持。

#### Gephi + ForceAtlas2
来源：[Gephi 官方博客](https://gephi.wordpress.com/2011/06/06/forceatlas2-the-new-version-of-our-home-brew-layout/)

| 维度 | 详情 |
|------|------|
| 默认布局算法 | ForceAtlas2（连续运行，用户实时观察收敛） |
| Hub 节点特殊处理 | **Dissuade Hubs**：吸引力按出度分摊，hub 被推向外围；**LinLog mode**：对数吸引力，使簇更紧密 |
| 用户可调参数 | Scaling、LinLog mode、Gravity、Edge Weight Influence、Dissuade Hubs（bool）、**Prevent Overlapping**（考虑节点尺寸） |
| N 节点降级策略 | 桌面端可处理数万节点（基准：23,133 节点）；浏览器端用 graphology+sigma 承接 |

---

### 维度 5：交互式探索 vs 静态渲染

| 策略 | 适用条件 | ConvoMemory 评估 |
|------|---------|-----------------|
| **全图一次性显示** | 节点总数 < 200，度数均匀 | 若单次对话 50-80 节点，勉强可行；但有 hub PERSON 时视觉拥挤 |
| **默认 1-hop，按需展开** | 节点总数 > 200，或有明显 hub | **ConvoMemory 应采用此策略** |
| **搜索先行，再显示上下文** | 图极大（>1000 节点） | 如未来累积多年对话数据，该范式适用 |

**van Ham & Perer 2009 对 ConvoMemory 的映射**：

```
Search        → 用户输入人名"Alice"，系统定位 PERSON 节点
Show Context  → 显示 Alice 的 1-hop 子图 + 类型色彩编码
Expand        → 点击 EVENT 节点展开其 FACT；点击"历史胶囊"展开 SUPERSEDED_BY 链
```

**结论**：Expand on Demand 范式与 ConvoMemory 高度吻合，且解决了 PERSON 节点 50-200 邻居的密度问题。**这是应当采纳的核心交互框架**。

---

## Part 2：针对 ConvoMemory 的差异化方案

---

### 方案 A：PERSON 同心环 + 类型扇区 + 按需展开

**设计哲学**：结构优先，用空间位置编码语义类型，用户只需看位置就知道节点是什么类型。

#### 默认布局策略

**算法**：自定义极坐标（Polar Coordinate）布局，而非物理仿真

```
PERSON(hub)      → 坐标 (0,0)，radius=0
Ring 1 (r=120px) → STATE（is_current=true）
Ring 2 (r=240px) → EVENT（按时间顺时针排列）
Ring 3 (r=360px) → FACT + CONCEPT（各占 180° 扇区）
Ring 4 (r=480px) → 其他 PERSON（关系型）
```

每个 ring 内节点均匀分布：$\theta_i = 2\pi \cdot i / N_{ring}$。当单 ring 节点超过 40 个时，触发**子分层**：同 ring 内按子类型分扇区，每扇区间加小间隙（分隔弧）。

#### Hub 节点周围处理

- PERSON 固定居中（Cytoscape.js fCoSE `fixed: true` constraint，或 D3 `fx=0, fy=0`）
- Ring 1 只显示 `is_current=true` 节点（通常 3-8 个）；过时节点折叠为"历史胶囊"
- Ring 2 显示最近 20 条 EVENT；更早的折叠为"更早"箭头节点
- 每个 ring 内边数等于 ring 内节点数（无 ring 内部边），**视觉上无 hairball**

#### 边密度高时的降级路径

| 条件 | 降级动作 |
|------|---------|
| Ring 内节点 > 40 | Ring 自动分扇区，加入分隔弧；缩略图模式下 `hideEdgesOnViewport` |
| Ring 内节点 > 80 | 自动聚合为超节点（"8 个工作相关 FACT"），hover 显示列表 |
| 总节点 > 150 | 只显示 Ring 1-2，Ring 3-4 折叠为计数徽章，点击展开 |

#### 时间维度表达

- `is_current=true`：节点边框亮色 + 实心填充
- `is_current=false`（过期）：25% 透明度 + 灰色，折叠到"历史胶囊"
- `SUPERSEDED_BY` 边：虚线弧（`stroke-dasharray: 5,3`），灰色，径向方向穿越 ring 间
- 展开历史时：Ring 1 内按时间顺时针排列，最新 STATE 在 12 点位置

#### 用户核心交互流程

```
1. 搜索/点击某 PERSON 节点
   ↓
2. 视图切换为同心环，PERSON 居中，Ring 1-2 默认展开
   ↓
3. 点击 Ring 标签（STATE/EVENT/FACT）→ 其余 Ring 淡出，聚焦所选 Ring
   ↓
4. 点击某 Ring 内节点 → 弹出 Detail Panel（右侧）
   ↓
5. 点击"展开关系" → 该节点的邻居追加到对应 Ring 或 Ring+1
   ↓
6. 点击"查看历史" → Ring 1 展开 SUPERSEDED_BY 链，透明度编码时序
```

#### 技术实现栈

| 组件 | 推荐 | 原因 |
|------|------|------|
| 图渲染 | **Cytoscape.js** | 内置 concentric 布局，fCoSE 支持固定约束，API 完整 |
| 极坐标计算 | 自定义 JS + Cytoscape `preset` 布局 | 同心环是预计算位置后用 preset 放置 |
| 动画过渡 | Cytoscape.js `animate()` API | 平滑展开/折叠 |
| 时间轴辅助 | D3 time scale（用于 EVENT ring 排序） | 与 Cytoscape 位置计算解耦 |

#### 适用上限

- **300 节点以内**流畅
- Ring 内节点超 80 时退化为超节点聚合，上限可扩展至 500 原始节点
- Cytoscape.js Canvas 渲染：~2000 节点前流畅

---

### 方案 B：时间轴分层布局 + 边捆绑

**设计哲学**：时间是第一维度，用户关心"事情发生的顺序"，x 轴 = 时间。

#### 默认布局策略

**算法**：自定义 Timeline Layout

```
纵轴（节点类型分层）：
  Row 0（顶）：PERSON
  Row 1       ：STATE（按时间戳，x=time）
  Row 2       ：EVENT（按时间戳，x=time）
  Row 3       ：FACT（按关联 EVENT 的 x 位置投影）
  Row 4（底） ：CONCEPT（按 FACT 聚合位置分布）

横轴：时间轴（D3 scaleTime），从最早对话到最新
```

PERSON 节点固定在左侧，作为行标签（类似 Gantt 图泳道标签）。多个 PERSON 时每人占一行（泳道），x 轴共用时间刻度。

#### Hub 节点周围处理

- PERSON 不参与时间轴布局，固定在 Row 0 最左侧
- PERSON→STATE 连边用 **Hierarchical Edge Bundling**（Holten 2006，B-spline 捆绑）聚合为主带，降低视觉密度
- 多 PERSON 时，不同泳道的 PERSON 可通过共享 EVENT（跨行连线）可视化关系

#### 边密度高时的降级路径

| 条件 | 降级动作 |
|------|---------|
| Row 内节点横向重叠（时间戳接近） | 自动 jitter（x 轴微扰 ±5px）+ 节点半径缩小 |
| 同一时间区间节点 > 10 | 折叠为"+N 个事件"气泡，hover 展开 |
| 跨行边数 > 50 | 启用 Edge Bundling（D3 `curveBundle`+`cluster`，基于 Holten 2006） |
| 总节点 > 200 | 仅显示最近 90 天时间窗口，左侧"加载更早"按钮 |

#### 时间维度表达（本方案最强项）

- `SUPERSEDED_BY` 边：在 Row 1（STATE 行）中变为**横向箭头**（→），与时间轴方向一致，极其直观
- `is_current=true`：STATE 节点右端加"▶ 当前"标签
- 过时 STATE：透明度 40%，连续的 SUPERSEDED_BY 箭头构成时间线
- Range Slider 过滤时间窗口，只显示指定区间内 STATE/EVENT

#### 用户核心交互流程

```
1. 选择 PERSON（下拉列表或点击）
   ↓
2. 时间轴展开，Row 1-2 默认显示
   ↓
3. 时间 Range Slider 过滤时间窗口
   ↓
4. 点击某 STATE 节点 → 右侧 Panel 显示状态详情 + 关联 FACT
   ↓
5. 点击某 EVENT 节点 → 突出显示参与该 EVENT 的所有 PERSON（跨泳道高亮）
   ↓
6. 切换"关系模式"按钮 → 切换到方案 A 的同心环视图
```

#### 技术实现栈

| 组件 | 推荐 | 原因 |
|------|------|------|
| 图渲染 | **D3.js**（SVG 模式） | D3 time scale 天然适合时间轴；自定义布局灵活 |
| 边捆绑 | D3 hierarchical edge bundling（`d3.curveBundle`+`d3.cluster`，基于 Holten 2006） | D3 内置实现 |
| 时间轴交互 | D3 `brush`（范围选择） | 标准做法 |
| 节点折叠气泡 | 自定义碰撞检测 | 防止时间轴上节点重叠 |

#### 适用上限

- 时间跨度决定布局质量：若 1000 条对话集中在 3 天内，x 轴密度极高
- 节点数：~500（SVG）；用 Canvas 可至 ~2000
- 时间跨度超 2 年且节点 > 200 时，需要时间分辨率切换（年/月/周）

---

### 方案 C：聚合视图 + 双层下钻（Overview + Detail）

**设计哲学**：先看全貌（所有 PERSON 的摘要），再下钻到单人详情。对应 Shneiderman 1996 的 "Overview first, zoom and filter, details on demand"。

#### 默认布局策略（Level 0：全局视图）

**算法**：**ForceAtlas2**（via graphology + sigma.js）

全局视图只显示 PERSON 节点（5-20 个），边为 PERSON-PERSON 关系。每个 PERSON 节点大小 $\propto$ 其邻居总数（STATE+EVENT+FACT+CONCEPT），直观呈现"谁的信息最丰富"。

ForceAtlas2 参数配置：

```javascript
{
  dissuadeHubs: true,      // 防止高信息量 PERSON 向中心堆积
  linLogMode: true,        // 使 PERSON 间社区结构更清晰
  barnesHutOptimize: true  // 加速（via graphology）
}
```

#### Level 1：单 PERSON 下钻

点击某 PERSON 后，动画过渡到**方案 A 的同心环视图**（以该 PERSON 为中心）。

#### Hub 节点周围处理

- Level 0 中，PERSON 的所有 STATE/EVENT/FACT 节点在数据层聚合，不渲染。PERSON 节点用数字徽章显示"12 个状态 / 47 个事件"
- Level 1（下钻）才展开内部节点，采用方案 A 的同心环

#### 边密度高时的降级路径

| 层级 | 条件 | 降级动作 |
|------|------|---------|
| Level 0 | PERSON 数 > 30 | ForceAtlas2 + Dissuade Hubs 自动分散；超过 50 个时按对话分组 |
| Level 1 | PERSON 邻居 > 100 | 自动启用方案 A 的 ring 折叠策略 |
| Level 2（可选） | 点击 CONCEPT → 显示所有含此 CONCEPT 的 PERSON | `clusterByConnection` 聚合相关 PERSON |

#### 时间维度表达

- Level 0：PERSON 节点徽章内用色条表示"最近活跃时间"（热力颜色，越近越亮）
- Level 1：采用方案 A 的历史胶囊策略
- 专门的"时间线模式"按钮：Level 1 切换为方案 B 的时间轴布局

#### 用户核心交互流程

```
1. 进入 ConvoMemory → 全局视图（Level 0）
   → 看到所有 PERSON 节点分布，大小反映信息量
   ↓
2. 悬停某 PERSON → 弹出摘要卡（最近 STATE/EVENT 前 3 条）
   ↓
3. 点击 PERSON → 动画放大过渡到 Level 1（同心环视图）
   ↓
4. Level 1 内按方案 A 的流程交互
   ↓
5. 按 ESC 或点击"返回全局" → 动画缩回 Level 0
   ↓
6. 在 Level 1 点击 CONCEPT 节点 → 高亮 Level 0 中所有含该 CONCEPT 的 PERSON
```

#### 技术实现栈

| 层级 | 推荐技术 | 原因 |
|------|---------|------|
| Level 0 渲染 | **Sigma.js + graphology** | WebGL，可承载数千节点；ForceAtlas2 成熟 |
| Level 1 渲染 | **Cytoscape.js** | 同心环布局、fCoSE 约束、丰富动画 |
| 层级过渡动画 | CSS `transform: scale()` + Cytoscape `fit()` | 平滑缩放感 |
| 数据层 | graphology `Graph` 对象（共享） | Level 0/1 共用数据模型 |

#### 适用上限

- Level 0：PERSON 数量到 200 个前流畅（Sigma.js WebGL）
- Level 1：单 PERSON 邻居到 500 个（采用 ring 折叠后）
- 整体数据集：~10,000 节点（graphology + Sigma.js WebGL 官方声称）

---

## Part 3：决策检查清单（量化标准）

### 数据规模检查

| 检查项 | 数值目标 | 评估方式 |
|--------|---------|---------|
| 单个 PERSON 节点的平均邻居数 | 实测 P50/P95 | 统计每个 PERSON 的邻居数量 |
| 最大 PERSON 邻居数 | P95 > 50 → 需要折叠策略 | max 值 |
| 总节点数（单次对话） | < 200 → 方案 A 可全图；> 200 → 必须按需展开 | 统计单对话实体数 |
| STATE 的历史链长度 | < 5 → 透明度编码足够；> 5 → 需要折叠+时间轴 | 统计 SUPERSEDED_BY 链长 |
| EVENT 节点的时间跨度 | < 3 个月 → 时间轴方案可行；> 1 年 → 必须时间分辨率切换 | 查询最早/最新 EVENT 时间戳 |

### 用户任务 × 方案选择矩阵

| 主要用户任务 | 最优方案 | 次优方案 |
|------------|---------|---------|
| 查看某人的当前状态 | 方案 A（Ring 1 一目了然） | 方案 C（Level 1） |
| 追溯状态演变历史 | 方案 B（时间轴横向箭头直观） | 方案 A + 展开历史胶囊 |
| 浏览所有涉及的人物关系 | 方案 C（Level 0 全局图） | — |
| 找某个 FACT 是否过时 | 方案 A（颜色+透明度标记） | 方案 B（时间轴有时序参考） |
| 查看特定时间段发生的事 | 方案 B（Range Slider） | 方案 C（PERSON 徽章热力色） |

### 方案选择评分矩阵

| 评估维度（权重） | 方案 A（同心环） | 方案 B（时间轴） | 方案 C（聚合+下钻） |
|----------------|:---:|:---:|:---:|
| 单人信息概览（25%） | ★★★★★ | ★★★ | ★★★★ |
| 状态历史可读性（20%） | ★★★ | ★★★★★ | ★★★ |
| 多人关系视图（15%） | ★★ | ★★ | ★★★★★ |
| Hub 节点密度控制（20%） | ★★★★ | ★★★★★ | ★★★★ |
| 实现复杂度（10%） | ★★★★ | ★★★ | ★★ |
| 移动端适配（10%） | ★★★★ | ★★ | ★★★ |
| **加权总分** | **3.65** | **3.35** | **3.40** |

### 三个量化通过标准

| 标准 | 数值目标 |
|------|---------|
| 200 个邻居的 PERSON 节点，默认视图能看清多少个 | 方案 A：Ring 1（STATE）3-8 个 + Ring 2（EVENT）≤20 个，其余折叠 → 清晰可见 23-28 个 |
| 用户从"打开图"到"找到一条特定关系"的点击数 | ≤ 3 次（点击 PERSON → Ring 展开 → 点击目标节点） |
| 首屏加载时间 | < 2 秒（初始渲染 < 150 节点；方案 C Level 0 只渲染 PERSON 层） |

### 推荐实施路径

1. **MVP**：实施方案 A（同心环 + 按需展开），技术门槛最低，覆盖最高频用户任务
2. **V1.1**：在方案 A 的 STATE Ring 内增加时间轴切换按钮，融合方案 B 的状态历史视图
3. **V2.0**：增加方案 C 的全局 PERSON 概览视图（Level 0），构成完整两层浏览体验

---

## 关键引用

| 来源 | URL / DOI |
|------|---------|
| ForceAtlas2（Jacomy et al. 2014） | [DOI:10.1371/journal.pone.0098679](https://doi.org/10.1371/journal.pone.0098679) |
| ForceAtlas2 Gephi 官方博客 | https://gephi.wordpress.com/2011/06/06/forceatlas2-the-new-version-of-our-home-brew-layout/ |
| Holten 2006 Hierarchical Edge Bundles | [DOI:10.1109/TVCG.2006.147](https://doi.org/10.1109/TVCG.2006.147) |
| Heer & Boyd 2005 Vizster | [DOI:10.1109/INFVIS.2005.1532151](https://doi.org/10.1109/INFVIS.2005.1532151) |
| van Ham & Perer 2009 Expand on Demand | [DOI:10.1109/TVCG.2009.108](https://doi.org/10.1109/TVCG.2009.108) |
| Force-directed graph drawing（数学背景） | https://en.wikipedia.org/wiki/Force-directed_graph_drawing |
| Cytoscape.js 文档（concentric, fCoSE） | https://js.cytoscape.org/ |
| fCoSE GitHub（约束参数） | https://github.com/iVis-at-Bilkent/cytoscape.js-fcose |
| Sigma.js 官网（WebGL 性能） | https://www.sigmajs.org/ |
| graphology ForceAtlas2 参数 | https://graphology.github.io/standard-library/layout-forceatlas2.html |
| vis-network 文档（clusterByHubsize） | https://visjs.github.io/vis-network/docs/network/ |
| Neo4j Bloom settings 文档 | https://neo4j.com/docs/bloom-user-guide/current/bloom-visual-tour/settings-drawer/ |

> **注意**：Logseq 图视图的技术细节（算法名、参数）在官方文档中未公开，本报告相关描述为推断，证据强度：中。Neo4j Bloom 的具体布局算法名称同样未在公开文档中披露。
