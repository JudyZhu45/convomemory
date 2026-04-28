"""Knowledge graph visualization using pyvis.

Renders a GraphMemory as an interactive HTML network graph.
"""
from __future__ import annotations

from core.graph_memory import GraphMemory, NodeType

# Node type → (color, shape)
_NODE_STYLE: dict[str, tuple[str, str]] = {
    NodeType.PERSON:  ("#4A90D9", "dot"),
    NodeType.EVENT:   ("#F5A623", "diamond"),
    NodeType.STATE:   ("#27AE60", "box"),
    NodeType.FACT:    ("#9B59B6", "ellipse"),
    NodeType.ORG:     ("#1ABC9C", "triangle"),
    NodeType.CONCEPT: ("#95A5A6", "dot"),
}

_DEFAULT_STYLE = ("#BDC3C7", "dot")

# Predicate → edge color
_EDGE_COLOR: dict[str, str] = {
    "HAS_STATE":        "#27AE60",
    "HAS_FACT":         "#9B59B6",
    "PARTICIPATED_IN":  "#F5A623",
    "ORGANIZED":        "#E74C3C",
    "RELATED_TO":       "#95A5A6",
    "KNOWS":            "#4A90D9",
    "SUPERSEDED_BY":    "#E74C3C",
}

_LEGEND_HTML = """
<div style="
    position: absolute; top: 10px; right: 10px;
    background: rgba(26,26,46,0.92); border-radius: 8px;
    padding: 12px 16px; font-family: sans-serif; font-size: 12px; color: #eee;
    border: 1px solid #444; z-index: 999;">
  <b style="font-size:13px;">Node Types</b><br><br>
  <span style="color:#4A90D9">●</span> Person &nbsp;
  <span style="color:#F5A623">◆</span> Event &nbsp;
  <span style="color:#27AE60">▪</span> State<br><br>
  <span style="color:#9B59B6">○</span> Fact &nbsp;&nbsp;
  <span style="color:#1ABC9C">▲</span> Org &nbsp;&nbsp;
  <span style="color:#95A5A6">●</span> Concept
</div>
"""


def build_pyvis_html(
    graph: GraphMemory,
    filter_types: set[str] | None = None,
    height: int = 620,
) -> str:
    """Generate interactive pyvis HTML for the given GraphMemory.

    filter_types: if provided, only include nodes of these NodeTypes.
    """
    from pyvis.network import Network

    net = Network(
        height=f"{height}px",
        width="100%",
        bgcolor="#1a1a2e",
        font_color="#eeeeee",
        directed=True,
    )
    net.set_options("""
    {
      "physics": {
        "enabled": true,
        "solver": "forceAtlas2Based",
        "forceAtlas2Based": {
          "gravitationalConstant": -60,
          "centralGravity": 0.005,
          "springLength": 120,
          "springConstant": 0.08
        },
        "stabilization": {"iterations": 150}
      },
      "interaction": {
        "hover": true,
        "tooltipDelay": 100,
        "navigationButtons": true
      },
      "edges": {
        "arrows": {"to": {"enabled": true, "scaleFactor": 0.6}},
        "smooth": {"type": "dynamic"},
        "font": {"size": 10, "color": "#aaaaaa", "align": "middle"}
      },
      "nodes": {
        "borderWidth": 1.5,
        "shadow": true
      }
    }
    """)

    included_node_ids: set[str] = set()

    for node in graph._nodes.values():
        if filter_types and node.node_type not in filter_types:
            continue
        color, shape = _NODE_STYLE.get(node.node_type, _DEFAULT_STYLE)
        # Size by degree (connection count)
        degree = len(graph._adj.get(node.node_id, [])) + len(graph._radj.get(node.node_id, []))
        size = max(12, min(40, 12 + degree * 3))
        tooltip = (
            f"<b>{node.name}</b><br>"
            f"Type: {node.node_type}<br>"
            f"Sessions: {node.sessions}<br>"
            f"Connections: {degree}"
        )
        net.add_node(
            node.node_id,
            label=node.name,
            color=color,
            shape=shape,
            size=size,
            title=tooltip,
            font={"size": 11, "color": "#ffffff"},
        )
        included_node_ids.add(node.node_id)

    for triple in graph._triples:
        if triple.subject_id not in included_node_ids:
            continue
        if triple.object_id not in included_node_ids:
            # Object might be a literal or filtered-out node — add a small label node
            if triple.object_id not in graph._nodes:
                obj_label = triple.object_id[:40]
                net.add_node(
                    triple.object_id,
                    label=obj_label,
                    color="#555555",
                    shape="text",
                    size=8,
                    font={"size": 9, "color": "#aaaaaa"},
                )
                included_node_ids.add(triple.object_id)
            else:
                continue

        edge_color = _EDGE_COLOR.get(triple.predicate, "#666666")
        label = triple.predicate.replace("_", " ").lower()
        tooltip = f"{triple.predicate}"
        if triple.date:
            tooltip += f"\n{triple.date}"
        if triple.dialog_id:
            tooltip += f"\n({triple.dialog_id})"

        net.add_edge(
            triple.subject_id,
            triple.object_id,
            label=label,
            color={"color": edge_color, "opacity": 0.8},
            title=tooltip,
            width=1.5,
        )

    html = net.generate_html(notebook=False)
    # Inject legend into the body
    html = html.replace("</body>", f"{_LEGEND_HTML}</body>")
    return html
