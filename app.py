"""ConvoMemory — Build and explore memory from conversation history.

Run: streamlit run app.py
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
load_dotenv()

import streamlit as st

from core.api_client import APIClient
from core.graph_builder import build_graph_from_sessions, parse_locomo, parse_claude_export
from core.graph_memory import GraphMemory, NodeType
from components.visualizer import build_pyvis_html

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="ConvoMemory",
    page_icon="🧠",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
  .main { background-color: #0e0e1a; }
  .stTabs [data-baseweb="tab"] { font-size: 15px; }
  .stat-card {
    background: #1a1a2e; border-radius: 8px; padding: 16px 20px;
    border-left: 4px solid #4A90D9; margin-bottom: 10px;
  }
  .step-box {
    background: #1a1a2e; border-radius: 6px; padding: 10px 14px;
    border-left: 3px solid #F5A623; margin: 6px 0; font-size: 13px;
  }
  .answer-box {
    background: #0d2137; border-radius: 8px; padding: 14px 18px;
    border-left: 4px solid #27AE60; font-size: 16px; margin-top: 10px;
  }
</style>
""", unsafe_allow_html=True)

# ── Session state defaults ─────────────────────────────────────────────────────

if "graph" not in st.session_state:
    st.session_state.graph: GraphMemory | None = None
if "speakers" not in st.session_state:
    st.session_state.speakers: list[str] = []
if "build_log" not in st.session_state:
    st.session_state.build_log: list[str] = []
if "chat_history" not in st.session_state:
    st.session_state.chat_history: list[dict] = []

# ── Sidebar ────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("🧠 ConvoMemory")
    st.caption("Build memory from conversations")
    st.divider()

    st.subheader("API Settings")
    api_key = st.text_input(
        "OpenAI API Key",
        value=os.environ.get("OPENAI_API_KEY", ""),
        type="password",
        placeholder="sk-...",
    )
    model = st.selectbox("Model", ["gpt-4.1-mini", "gpt-4o-mini", "gpt-4o"], index=0)

    st.divider()
    if st.session_state.graph:
        g = st.session_state.graph
        st.subheader("Current Memory")
        st.metric("Nodes", g.node_count)
        st.metric("Triples", g.triple_count)
        if st.session_state.speakers:
            st.caption(f"People: {', '.join(st.session_state.speakers)}")
        if st.button("🗑 Clear Memory", use_container_width=True):
            st.session_state.graph = None
            st.session_state.speakers = []
            st.session_state.build_log = []
            st.session_state.chat_history = []
            st.rerun()

# ── Tabs ──────────────────────────────────────────────────────────────────────

tab_build, tab_graph, tab_query = st.tabs([
    "📥 Build Memory",
    "🕸 Knowledge Graph",
    "💬 Query",
])

# ════════════════════════════════════════════════════════════════════════
# TAB 1: BUILD MEMORY
# ════════════════════════════════════════════════════════════════════════
with tab_build:
    st.header("Build Memory from Conversations")

    col1, col2 = st.columns([1, 1], gap="large")

    with col1:
        st.subheader("Load Conversation")
        source = st.radio(
            "Data source",
            ["LoCoMo Demo (conv-26)", "Upload JSON file"],
            horizontal=True,
        )

        sessions = None
        speakers_detected = []

        if source == "LoCoMo Demo (conv-26)":
            demo_path = Path(__file__).parent / "demo" / "locomo_sample.json"
            max_sess = st.slider("Max sessions to process", 1, 19, 5,
                                 help="More sessions = richer memory but slower build (~30s/session)")
            if demo_path.exists():
                demo_data = json.loads(demo_path.read_text())
                sessions = parse_locomo(demo_data[0], max_sessions=max_sess)
                speakers_detected = list(dict.fromkeys(
                    d["speaker"] for d in sessions[0]["dialogs"]
                )) if sessions else []
                st.success(f"Loaded conv-26: {len(sessions)} sessions, "
                           f"speakers: {', '.join(speakers_detected)}")
                with st.expander("Preview session 1"):
                    for d in sessions[0]["dialogs"][:6]:
                        st.markdown(f"**{d['speaker']}**: {d['text']}")
            else:
                st.error("Demo file not found at demo/locomo_sample.json")

        else:
            uploaded = st.file_uploader(
                "Upload conversation JSON",
                type=["json"],
                help="Supported: LoCoMo format or Claude.ai export",
            )
            fmt = st.radio("Format", ["LoCoMo", "Claude Export"], horizontal=True)
            if uploaded:
                try:
                    data = json.load(uploaded)
                    if fmt == "LoCoMo":
                        sample = data[0] if isinstance(data, list) else data
                        sessions = parse_locomo(sample)
                    else:
                        sessions = parse_claude_export(data)
                    speakers_detected = list(dict.fromkeys(
                        d["speaker"] for d in sessions[0]["dialogs"]
                    )) if sessions else []
                    st.success(f"Loaded {len(sessions)} sessions")
                except Exception as e:
                    st.error(f"Parse error: {e}")

    with col2:
        st.subheader("Build")

        if sessions:
            st.info(f"Ready to process **{len(sessions)} sessions** → extract entities, "
                    f"facts, events, and states into a knowledge graph.")

            if st.button("🚀 Build Memory Graph", type="primary", use_container_width=True,
                         disabled=not api_key):
                if not api_key:
                    st.error("Add your OpenAI API key in the sidebar.")
                else:
                    client = APIClient(api_key=api_key, model=model)
                    log_placeholder = st.empty()
                    progress_bar = st.progress(0)
                    log_lines: list[str] = []

                    def on_progress(current: int, total: int, msg: str):
                        pct = int(current / max(total, 1) * 100)
                        progress_bar.progress(pct)
                        log_lines.append(f"[{current}/{total}] {msg}")
                        log_placeholder.code("\n".join(log_lines[-8:]), language=None)

                    with st.spinner("Building memory graph…"):
                        try:
                            graph = build_graph_from_sessions(
                                sessions, client, progress_callback=on_progress
                            )
                            st.session_state.graph = graph
                            st.session_state.speakers = speakers_detected
                            st.session_state.build_log = log_lines
                            st.session_state.chat_history = []
                            progress_bar.progress(100)
                        except Exception as e:
                            st.error(f"Build failed: {e}")
                            st.stop()

                    st.success("Memory graph built!")
        else:
            st.info("Select a data source on the left to get started.")

    # Build results
    if st.session_state.graph:
        st.divider()
        st.subheader("Memory Stats")
        g = st.session_state.graph

        type_counts: dict[str, int] = {}
        for n in g._nodes.values():
            type_counts[n.node_type] = type_counts.get(n.node_type, 0) + 1

        cols = st.columns(len(type_counts) + 1)
        cols[0].metric("Total Nodes", g.node_count)
        for i, (nt, cnt) in enumerate(sorted(type_counts.items())):
            cols[i + 1].metric(nt.capitalize(), cnt)

        rel_counts: dict[str, int] = {}
        for t in g._triples:
            rel_counts[t.predicate] = rel_counts.get(t.predicate, 0) + 1

        st.markdown("**Relationships extracted:**")
        rel_cols = st.columns(min(len(rel_counts), 4))
        for i, (pred, cnt) in enumerate(sorted(rel_counts.items(), key=lambda x: -x[1])):
            rel_cols[i % len(rel_cols)].metric(pred.replace("_", " "), cnt)

        # Save/load graph
        st.divider()
        col_save, col_load = st.columns(2)
        with col_save:
            graph_json = json.dumps(g.to_dict(), ensure_ascii=False, indent=2)
            st.download_button(
                "💾 Export Graph JSON",
                data=graph_json,
                file_name="memory_graph.json",
                mime="application/json",
                use_container_width=True,
            )
        with col_load:
            loaded_file = st.file_uploader("Load saved graph", type=["json"], key="load_graph")
            if loaded_file:
                try:
                    data = json.load(loaded_file)
                    st.session_state.graph = GraphMemory.from_dict(data)
                    st.success("Graph loaded!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Load failed: {e}")

# ════════════════════════════════════════════════════════════════════════
# TAB 2: KNOWLEDGE GRAPH
# ════════════════════════════════════════════════════════════════════════
with tab_graph:
    if not st.session_state.graph:
        st.info("Build a memory graph first (Tab 1).")
    else:
        g = st.session_state.graph

        st.header("Knowledge Graph")

        # Controls
        ctrl_col, _ = st.columns([2, 3])
        with ctrl_col:
            all_types = sorted(set(n.node_type for n in g._nodes.values()))
            selected_types = st.multiselect(
                "Show node types",
                options=all_types,
                default=all_types,
            )

        if not selected_types:
            st.warning("Select at least one node type.")
        else:
            filter_set = set(selected_types)
            with st.spinner("Rendering graph…"):
                html_content = build_pyvis_html(g, filter_types=filter_set, height=620)
            st.components.v1.html(html_content, height=640, scrolling=False)

            # Node list
            with st.expander("All nodes"):
                for nt in sorted(all_types):
                    if nt not in filter_set:
                        continue
                    nodes_of_type = [n for n in g._nodes.values() if n.node_type == nt]
                    if nodes_of_type:
                        st.markdown(f"**{nt}** ({len(nodes_of_type)})")
                        names = ", ".join(n.name for n in sorted(nodes_of_type, key=lambda n: n.name))
                        st.caption(names)

# ════════════════════════════════════════════════════════════════════════
# TAB 3: QUERY
# ════════════════════════════════════════════════════════════════════════
with tab_query:
    if not st.session_state.graph:
        st.info("Build a memory graph first (Tab 1).")
    elif not api_key:
        st.warning("Add your OpenAI API key in the sidebar to query.")
    else:
        from core.query_agent import MemoryQueryAgent

        st.header("Query Memory")
        st.caption(
            "Ask anything about the people in this conversation. "
            "The agent will search the knowledge graph to answer."
        )

        # Suggested questions
        if st.session_state.speakers:
            sp = st.session_state.speakers
            suggestions = [
                f"What are {sp[0]}'s hobbies or interests?" if sp else "What are this person's interests?",
                f"What events has {sp[0]} participated in?" if sp else "What events happened?",
                f"What is the relationship between {sp[0]} and {sp[1]}?" if len(sp) > 1 else "What facts are known?",
                "What goals or plans are mentioned?",
            ]
            st.markdown("**Suggested questions:**")
            scols = st.columns(len(suggestions))
            for i, sug in enumerate(suggestions):
                if scols[i].button(sug, key=f"sug_{i}", use_container_width=True):
                    st.session_state._pending_question = sug

        # Chat history
        for msg in st.session_state.chat_history:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])
                if msg["role"] == "assistant" and msg.get("steps"):
                    with st.expander("Agent reasoning steps"):
                        for step in msg["steps"]:
                            if step.get("action"):
                                st.markdown(f"""<div class="step-box">
                                    <b>Thought:</b> {step.get('thought', '')}<br>
                                    <b>Action:</b> <code>{step['action']}</code><br>
                                    <b>Observation:</b> {step.get('observation', '')[:300]}
                                </div>""", unsafe_allow_html=True)

        # Question input
        pending = st.session_state.pop("_pending_question", None)
        question = st.chat_input("Ask about the people in this conversation…")
        if pending and not question:
            question = pending

        if question:
            st.session_state.chat_history.append({"role": "user", "content": question})
            with st.chat_message("user"):
                st.markdown(question)

            client = APIClient(api_key=api_key, model=model)
            agent = MemoryQueryAgent(st.session_state.graph, client)

            with st.chat_message("assistant"):
                with st.spinner("Searching memory…"):
                    answer, steps = agent.answer(
                        question,
                        speakers=st.session_state.speakers or None,
                    )

                st.markdown(f"""<div class="answer-box">{answer}</div>""", unsafe_allow_html=True)

                if steps:
                    with st.expander("Agent reasoning steps"):
                        for step in steps:
                            if step.get("action"):
                                st.markdown(f"""<div class="step-box">
                                    <b>Thought:</b> {step.get('thought', '')}<br>
                                    <b>Action:</b> <code>{step['action']}</code><br>
                                    <b>Observation:</b> {step.get('observation', '')[:400]}
                                </div>""", unsafe_allow_html=True)

            st.session_state.chat_history.append({
                "role": "assistant",
                "content": answer,
                "steps": steps,
            })
