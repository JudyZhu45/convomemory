"""ReAct query agent for GraphMemory — generic version (no LoCoMo-specific categories).

Adapted from statemem/src/locomo/query_agent/react.py.
"""
from __future__ import annotations

import logging
import re

from .api_client import APIClient
from .graph_memory import GraphMemory, _cosine

logger = logging.getLogger(__name__)

MAX_ITER = 5
_MAX_TRIPLES = 20

# ── Tool implementations ──────────────────────────────────────────────────────

def _search_entity(name: str, graph: GraphMemory) -> tuple[str, list[str]]:
    nodes, triples = graph.get_subgraph(name, depth=2, bidirectional=True)
    if not triples:
        return f"No information found for: {name!r}. Try search_semantic or a different spelling.", []
    node_ids = [n.node_id for n in nodes]
    return graph.linearize_subgraph(nodes, triples[:_MAX_TRIPLES], seed_name=name), node_ids


def _search_semantic(phrase: str, graph: GraphMemory, phrase_emb: list[float]) -> tuple[str, list[str]]:
    similar = graph.find_entity_by_embedding(phrase_emb, top_k=5, threshold=0.60)
    if not similar:
        return f"No semantically similar nodes found for: {phrase!r}.", []

    seen_node_ids: set[str] = set()
    seen_triple_ids: set[str] = set()
    all_nodes, all_triples = [], []

    for node in similar:
        ns, ts = graph.get_subgraph(node.name, depth=1, bidirectional=True)
        for n in ns:
            if n.node_id not in seen_node_ids:
                all_nodes.append(n)
                seen_node_ids.add(n.node_id)
        for t in ts:
            if t.triple_id not in seen_triple_ids:
                all_triples.append(t)
                seen_triple_ids.add(t.triple_id)

    if not all_triples:
        return "Found similar nodes but no connected facts.", []

    if len(all_triples) > _MAX_TRIPLES:
        scored = [(n, _cosine(phrase_emb, n.embedding)) for n in all_nodes if n.embedding]
        scored.sort(key=lambda x: x[1], reverse=True)
        top_ids = {n.node_id for n, _ in scored[:_MAX_TRIPLES]}
        all_triples = [
            t for t in all_triples
            if t.subject_id in top_ids or t.object_id in top_ids
        ][:_MAX_TRIPLES]
        used = {t.subject_id for t in all_triples} | {t.object_id for t in all_triples}
        all_nodes = [n for n in all_nodes if n.node_id in used]

    node_ids = [n.node_id for n in all_nodes]
    return graph.linearize_subgraph(all_nodes, all_triples, seed_name=similar[0].name), node_ids


def _lookup_node(node_name: str, graph: GraphMemory) -> tuple[str, list[str]]:
    nodes, triples = graph.get_subgraph(node_name, depth=1, bidirectional=True)
    if not triples:
        return f"No connections found for: {node_name!r}.", []
    node_ids = [n.node_id for n in nodes]
    return graph.linearize_subgraph(nodes, triples[:_MAX_TRIPLES], seed_name=node_name), node_ids


_TOOL_DESCRIPTIONS = """\
  search_entity[name]     — BFS depth=2 subgraph for a person or entity (best first step)
  search_semantic[phrase] — semantic search across all graph nodes by meaning
  lookup_node[node_name]  — all direct connections of a specific node"""

# ── Agent ─────────────────────────────────────────────────────────────────────

_SYSTEM = """\
You are a memory query agent. Your goal is to answer a question about people \
by querying their knowledge graph.

Available tools:
{tools}

Output format for each step:
  Thought: <your reasoning>
  Action: tool_name[argument]

When you have enough information, output:
  Thought: I now have enough information.
  Final Answer: <concise answer>

Rules:
- If confident after any observation, output Final Answer IMMEDIATELY
- Never repeat the same tool call with the same argument
- Use exact node names from Observations when calling lookup_node
- Do not fabricate — only use facts from Observations
""".format(tools=_TOOL_DESCRIPTIONS)

_QA_SYSTEM = (
    "You are answering questions about people based on retrieved facts. "
    "Answer concisely. If the information is not present, say 'I don't have that information.'"
)

_FINAL_ANSWER_RE = re.compile(r"Final Answer:\s*(.+)", re.IGNORECASE)
_ACTION_RE = re.compile(r"Action:\s*(\w+)\[(.+?)\]", re.IGNORECASE | re.DOTALL)


class MemoryQueryAgent:
    """ReAct agent that queries GraphMemory to answer natural language questions."""

    def __init__(self, graph: GraphMemory, client: APIClient):
        self.graph = graph
        self.client = client

    def answer(
        self,
        question: str,
        speakers: list[str] | None = None,
    ) -> tuple[str, list[dict], list[str]]:
        """Answer a question. Returns (answer, steps, cited_node_ids).

        steps: ReAct trace, each step has node_ids, tool, query fields.
        cited_node_ids: deduplicated union of all node_ids across steps.
        """
        if self.graph.node_count == 0:
            return "No memory built yet. Please build the graph first.", [], []

        q_emb = self.client.embed_single(question)
        history: list[dict] = [{"role": "system", "content": _SYSTEM}]
        speaker_str = ", ".join(speakers) if speakers else "the people in this conversation"
        first_user = (
            f"People in the conversation: {speaker_str}\n"
            f"Question: {question}\n\nBegin."
        )

        user_msg = first_user
        observations: list[str] = []
        steps: list[dict] = []
        all_node_ids: list[str] = []  # ordered, for cited_nodes

        for step in range(MAX_ITER):
            response = self.client.gpt_with_history(history, user_msg, max_tokens=256)
            if response.startswith("ERROR:"):
                break

            history.append({"role": "user", "content": user_msg})
            history.append({"role": "assistant", "content": response})

            # Parse thought + action for trace
            thought = ""
            for line in response.split("\n"):
                if line.startswith("Thought:"):
                    thought = line[8:].strip()

            # Check for Final Answer
            m = _FINAL_ANSWER_RE.search(response)
            if m:
                answer = m.group(1).strip()
                steps.append({
                    "thought": thought, "action": None, "observation": None,
                    "answer": answer, "node_ids": [], "tool": None, "query": None,
                })
                cited = list(dict.fromkeys(all_node_ids))  # dedup, preserve order
                return answer, steps, cited

            # Parse and execute action
            action_match = _ACTION_RE.search(response)
            if not action_match:
                break

            tool_name = action_match.group(1).lower()
            tool_arg = action_match.group(2).strip()
            obs, node_ids = self._call_tool(tool_name, tool_arg, q_emb)
            all_node_ids.extend(node_ids)

            steps.append({
                "thought": thought,
                "action": f"{tool_name}[{tool_arg}]",
                "observation": obs,
                "answer": None,
                "node_ids": node_ids,
                "tool": tool_name,
                "query": tool_arg,
            })
            observations.append(obs)
            user_msg = f"Observation: {obs}"

        # Forced answer
        answer = self._forced_answer(question, observations)
        if steps:
            steps[-1]["answer"] = answer
        else:
            steps.append({
                "thought": "", "action": None, "observation": None,
                "answer": answer, "node_ids": [], "tool": None, "query": None,
            })
        cited = list(dict.fromkeys(all_node_ids))
        return answer, steps, cited

    def _call_tool(self, tool_name: str, arg: str, q_emb: list[float]) -> tuple[str, list[str]]:
        if tool_name == "search_entity":
            return _search_entity(arg, self.graph)
        elif tool_name == "search_semantic":
            arg_emb = self.client.embed_single(arg) if arg else q_emb
            return _search_semantic(arg, self.graph, arg_emb or q_emb)
        elif tool_name == "lookup_node":
            return _lookup_node(arg, self.graph)
        else:
            return f"Unknown tool: {tool_name!r}. Available: search_entity, search_semantic, lookup_node", []

    def _forced_answer(self, question: str, observations: list[str]) -> str:
        if not observations:
            return "I don't have that information."
        context = "\n\n".join(observations)
        user = (
            f"Relevant facts from memory:\n{context}\n\n"
            f"Question: {question}\nAnswer concisely:"
        )
        return self.client.gpt(_QA_SYSTEM, user, max_tokens=128).strip()
