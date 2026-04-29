# ConvoMemory 知识图谱可视化实现方案

> 实现版本：Scheme A — 同心环布局（Concentric Ring Layout）  
> 文件：`prototype.html`（React + SVG，CDN 加载，无构建步骤）  
> 状态：已上线，服务于 FastAPI `/` 路由

---

## 背景

LoCoMo 数据集（conv-26，Caroline & Melanie，19 sessions）构建出的知识图谱含 50+ 节点、5 种类型（PERSON / STATE / EVENT / FACT / CONCEPT）。原 force-directed 布局存在两个核心问题：

1. **Hub 节点聚合效应**：PERSON 节点引力 O(degree)，所有邻居节点向其聚集，形成"发状"拥挤
2. **角分辨率退化**：节点数量多时，相邻节点夹角过小，标签重叠不可读

**解决方案**：放弃连续物理模拟，改用极坐标确定性布局（同心环），节点位置由类型和所属 PERSON 决定，不受物理力影响。

---

## 布局算法

### 1. PERSON 节点定位

```
totalW = PERSON_SEP × (N - 1)   // PERSON_SEP = 1100
每个 PERSON 水平等间距排列，整体居中于原点
```

### 2. Multi-source BFS 归属分配

对每个非 PERSON 节点，通过图的邻接关系 BFS 找到距其最近的 PERSON，确定所属 cluster。未连通的孤立节点默认归属 persons[0]。

### 3. 扇形方向

每个 PERSON 的 cluster 在以 PERSON 为圆心的 300° 扇形内展开，扇形方向**背对重心**（两人时相互背对，单人时朝上）：

```javascript
faceAngle = atan2(cy - centY, cx - centX)  // 指向重心反方向
sectorStart = faceAngle - SECTOR/2         // SECTOR = 300° = 5.236 rad
```

### 4. 动态环半径

四种类型按固定顺序排布（STATE → EVENT → FACT → CONCEPT）：

```
// 环半径取两者较大值，防止节点重叠
minR      = currentR + RING_GAP          // RING_GAP = 70
rFromCirc = count × nodeSize × 1.2 / (2π × angleFrac)
r = max(minR, rFromCirc)
```

| 类型    | 形状   | NODE_SIZE | 渲染形状 |
|---------|--------|-----------|----------|
| STATE   | 矩形   | 108       | `<rect>` 100×30 |
| EVENT   | 菱形   | 58        | `<polygon>` 钻石 |
| FACT    | 椭圆   | 96        | `<ellipse>` 44×17 |
| CONCEPT | 圆形   | 38        | `<circle>` r=15 |
| PERSON  | 圆形   | —         | `<circle>` r=22 |

### 5. 弧内节点均匀分布

```javascript
padding = (count > 1) ? SECTOR × 0.04 : 0   // 防止节点卡在扇形边界
arcRange = SECTOR - 2 × padding
angle[i] = sectorStart + padding + (i / (count-1)) × arcRange
```

---

## 飞入动画

节点不是静态出现的，而是从各自所属 PERSON 的中心位置**飞出**到环位置：

```javascript
// 起始位置：每个节点 = 其 PERSON 的坐标
startPositions = nodes.map(n => ({ ...n, x: personCenters[pid].x, y: personCenters[pid].y }))

// ease-out quart（快出慢落）
ea = 1 - Math.pow(1 - t, 4)   // t ∈ [0, 1]

// RAF 循环，1100ms
x(t) = startX + (finalX - startX) × ea
```

双击背景可触发**复位动画**（800ms），将所有节点从当前拖拽位置平滑归回原始环位置。

---

## 交互设计

| 操作 | 效果 |
|------|------|
| 单击节点 | 选中 + 高亮直接相连节点，其余节点 opacity→0.12；锚定栏显示来源对话 |
| 拖拽节点 | 自由移动（立即取消飞入动画）|
| 拖拽背景 | SVG viewBox 平移 |
| 滚轮 | 以光标为中心缩放（非被动事件监听，`passive: false`） |
| 双击背景 | 动画复位到同心环原始位置 |
| hover 边 | 显示关系类型标签 |

---

## 视觉辅助

**环形导引弧（RingGuide）**：每个类型环绘制一条虚线弧，颜色与节点类型一致，opacity=0.18，弧中点标注类型名（opacity=0.3）。

```javascript
// SVG arc path
d = `M ${x1} ${y1} A ${r} ${r} 0 ${largeArc} 1 ${x2} ${y2}`
```

**渲染层级**（SVG z-order）：ringGuides → edges → nodes

---

## 实现关键点

### 非被动滚轮监听

React 合成事件 `onWheel` 默认为 passive listener，无法调用 `e.preventDefault()`（会报错且不生效）。解决方案：

```javascript
useEffect(() => {
  const svg = svgRef.current;
  svg.addEventListener('wheel', onWheel, { passive: false });
  return () => svg.removeEventListener('wheel', onWheel);
}, [graphReady]);
```

### 取消陈旧 RAF

拖拽节点时立即取消正在进行的飞入动画，防止拖拽坐标被动画帧覆盖：

```javascript
function handleNodeMouseDown(e, nodeId) {
  if (rafRef.current) { cancelAnimationFrame(rafRef.current); rafRef.current = null; }
  // ...
}
```

### ViewBox 状态双写

zoom/pan 在 `onMouseMove` 中高频更新，同时写 `vbRef.current`（供同帧 handler 读取）和 `setVb`（触发 React re-render）：

```javascript
vbRef.current = nv;
setVb(nv);
```

---

## 与调研报告的对应关系

详见 `docs/graph-viz-research.md` Scheme A 部分（综合得分 3.65 / 5，高于 Scheme B 3.35 和 Scheme C 3.40）。

主要优势：
- **零 overlap**（确定性极坐标 → 无碰撞检测需求）
- **类型可读性强**（每圈一种类型，颜色 + 形状双编码）
- **50 节点以内性能好**（无物理模拟，纯 RAF 动画）

主要局限：
- 节点数急增（>100）时扇形角分辨率仍会下降
- 无时间维度支持（待 V1.1 加 timeline toggle）
- 无 Level 0 全局概览（待 V2.0）

---

## 后续方向（未实现）

- **V1.1**：时间轴切换，按 session 序号控制哪些节点可见
- **V2.0**：双层视图 — 全局 cluster 鱼眼 + 单 PERSON 展开模式（参考 van Ham & Perer 2009）
