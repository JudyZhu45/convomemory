"""Smart memory query — aligned with idea_27_adaptive_smart_guard_halflife.

Three retrieval layers:
  1. Graph FACT nodes — cosine search (substitute for FactLog)
  2. GraphMemory — BFS subgraph + halflife/guard scoring on STATE nodes
  3. Session text — cosine search, always include last session

Router: one LLM call selects system prompt (strict/lenient) and QA format.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass

from .graph_memory import GraphMemory, NodeType

# ── Temporal question detection ───────────────────────────────────────────────

_TEMPORAL_Q_RE = re.compile(
    r"\b(how long|how many (months?|years?|weeks?|days?|times?)|since when|"
    r"when did|what (year|date|month)|how old|how (much )?time|duration|"
    r"still|anymore|how often|how frequent)\b",
    re.IGNORECASE,
)

# ── Scoring parameters ────────────────────────────────────────────────────────

_GUARD_PENALTY  = 0.15
_HALFLIFE_BETA  = 0.10
_TOP_FACT_K     = 20
_TOP_GRAPH_K    = 15
_TOP_GRAPH_TRIP = 20
_SESSION_TOP_K  = 2

# ── System prompts (from _shared.py) ─────────────────────────────────────────

_COMBINED_PLUS_SYSTEM_LENIENT = (
    "You are answering questions about a conversation between two people. "
    "You are given three memory sources: extracted facts, a knowledge graph, and raw session excerpts. "
    "Use all sources to answer concisely using exact words when possible. "
    "Rules: "
    "(1) Session excerpts contain the actual conversation text — prefer direct quotes from sessions. "
    "(2) If NO source contains a clear direct answer, write: no information available. "
    "(3) Prefer exact wording from the sources over paraphrasing. "
    "(4) Short phrases are preferred; be longer only when the question requires it."
)

_COMBINED_PLUS_SYSTEM_STRICT = (
    "You are answering questions about a conversation between two people. "
    "You are given three memory sources: extracted facts, a knowledge graph, and raw session excerpts. "
    "Answer concisely in a short phrase. "
    "Rules (STRICT mode — this question may have no answer): "
    "(1) If the session excerpts do NOT contain a direct, explicit answer, "
    "write exactly: no information available. "
    "(2) Do NOT infer from tangential or indirect information. "
    "(3) Only answer if you find an explicit, direct statement in the sources. "
    "(4) When in doubt, prefer 'no information available'."
)

# ── QA prompts (from _shared.py) ─────────────────────────────────────────────

_QA_PROMPT = (
    "Based on the above context, write an answer in the form of a short phrase "
    "for the following question. Answer with exact words from the context whenever possible.\n\n"
    "Question: {q} Short answer:"
)
_QA_PROMPT_CAT2 = (
    "Based on the above context, write an answer in the form of a short phrase "
    "for the following question. Answer with exact words from the context whenever possible.\n\n"
    "Question: {q} Use DATE of CONVERSATION to answer with an approximate date. Short answer:"
)
_QA_PROMPT_CAT5 = (
    "Based on the above context, answer the following question. "
    "If the information is not present in the context, write exactly: no information available\n\n"
    "Question: {q} Short answer:"
)
_QA_PROMPT_LIST = (
    "Based on the above context, answer with a comma-separated list of items. "
    "Use exact words from the sources whenever possible.\n\n"
    "Question: {q} Short answer:"
)


def _qa_prompt(question: str, category: int, answer_format: str = "short_phrase") -> str:
    if answer_format == "comma_list":
        return _QA_PROMPT_LIST.format(q=question)
    if category == 2:
        return _QA_PROMPT_CAT2.format(q=question)
    if category == 5:
        return _QA_PROMPT_CAT5.format(q=question)
    return _QA_PROMPT.format(q=question)


# ── Router ────────────────────────────────────────────────────────────────────

_ROUTER_SYSTEM = """\
You are a question type router for a memory query system.
Given a user's question, output a structured guidance for how the answering agent should respond.

You must output EXACTLY a JSON object with three fields. No explanation.

Fields:

  answer_format: how the final answer should be shaped
    - "short_phrase"        : 2-6 words, e.g. dates, names, single facts
    - "yes_no_with_reason"  : starts with Yes/No/Likely yes/Likely no, brief optional reason
    - "comma_list"          : multiple short noun-phrase items separated by commas
    - "complete_sentence"   : ONLY for "why/how/what does X mean to Y/what motivates X" questions

  strictness: how cautious the agent should be
    - "strict"  : the question may not have a clear answer in the conversation; prefer "no information available" over guessing
    - "lenient" : the question is clearly about the conversation's content; follow up on clues if direct evidence is missing

  expected_answer_kind: shape hint
    - "date" | "name" | "list" | "yes_no" | "phrase" | "other"

Decision principles for answer_format:
  1. "when / what date / what year / how long ago" → short_phrase, date
  2. "who / which person / what is the name of" → short_phrase, name
  3. "did / does / has / would / could / is / was / were / will" (Yes/No) → yes_no_with_reason, yes_no
  4. "what [noun] has/have/did/does/do [person] [verb]", "what events/places/activities" → comma_list, list
  5. "why / how does X feel / what does X mean to / what motivates / what inspired" → complete_sentence, phrase
  6. Default → short_phrase, other

  NEVER use complete_sentence for factual "what did X do/have/attend" questions.

Decision principles for strictness:
  - Default: "lenient"
  - Use "strict" when the question is about a specific belief or affiliation the person may never have stated,
    or a future plan/hypothetical that would rarely be mentioned explicitly
  - When uncertain, choose "lenient"

Output JSON only:
"""

_ROUTER_USER = "Question: {question}\n\nGuidance:"


@dataclass
class AnswerGuidance:
    answer_format: str = "short_phrase"
    strictness: str = "lenient"
    expected_answer_kind: str = "other"


def _route_question(question: str, client) -> AnswerGuidance:
    """One LLM call to get structured guidance. Category-agnostic."""
    response = client.gpt(_ROUTER_SYSTEM, _ROUTER_USER.format(question=question), max_tokens=128)
    try:
        text = response.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-z]*\n?", "", text)
            text = re.sub(r"\n?```$", "", text)
        data = json.loads(text)
        return AnswerGuidance(
            answer_format=data.get("answer_format", "short_phrase"),
            strictness=data.get("strictness", "lenient"),
            expected_answer_kind=data.get("expected_answer_kind", "other"),
        )
    except Exception:
        return AnswerGuidance()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def _detect_graph_seeds(
    question: str,
    graph: GraphMemory,
    q_emb: list[float] | None = None,
    speakers: list[str] | None = None,
    top_k_semantic: int = 3,
    semantic_threshold: float = 0.80,
) -> list[str]:
    """Hybrid keyword + semantic seed detection.

    1. Keyword match: check if question tokens match known entity names
    2. Semantic match on ALL node types (bidirectional BFS makes this useful)
    3. Fallback to speakers
    """
    seeds: list[str] = []
    seen: set[str] = set()

    # 1. Keyword match against all node names
    tokens = set(re.findall(r"\b\w+\b", question.lower()))
    for node in graph._nodes.values():
        name_lower = node.name.lower()
        # Exact or near-exact name match
        if name_lower in tokens or any(t in name_lower for t in tokens if len(t) >= 4):
            if node.name not in seen:
                seeds.append(node.name)
                seen.add(node.name)

    # 2. Semantic match (only when keyword found nothing useful)
    if not seeds and q_emb:
        for node in graph.find_entity_by_embedding(q_emb, top_k=top_k_semantic,
                                                    threshold=semantic_threshold):
            if node.name not in seen:
                seeds.append(node.name)
                seen.add(node.name)

    # 3. Fallback to speakers
    if not seeds and speakers:
        seeds = [s for s in speakers if s not in seen]

    return seeds


# ── Main class ────────────────────────────────────────────────────────────────

class SmartMemoryQuery:
    """Direct retrieval with halflife + guard scoring (idea_27 port).

    Args:
        graph:    GraphMemory built by graph_builder
        client:   APIClient for embeddings and LLM calls
        sessions: list of {sess_num, text, embedding} dicts
    """

    def __init__(self, graph: GraphMemory, client, sessions: list[dict]) -> None:
        self._graph    = graph
        self._client   = client
        self._sessions = sessions

    def answer(
        self,
        question: str,
        speakers: list[str] | None = None,
        history: list[dict] | None = None,
        category: int = 0,
    ) -> tuple[str, list, list[str]]:
        """Answer a question using three-layer retrieval.

        Returns:
            (answer_text, steps, cited_node_ids)
        """
        graph   = self._graph
        client  = self._client
        q_emb   = client.embed_single(question)
        is_date = bool(_TEMPORAL_Q_RE.search(question))
        apply_guard = not is_date  # guard penalizes stale states for non-temporal questions

        # Build session_idx → date string map (mirrors idea_27 session_date_map)
        session_date_map: dict[int, str] = {}
        for s in self._sessions:
            text = s.get("text", "")
            date_line = text.split("\n")[0].strip() if text else ""
            if date_line:
                session_date_map[s["sess_num"]] = date_line

        # ── Layer 1: FACT nodes (substitute for FactLog) ──────────────────────
        fact_nodes = [
            n for n in graph._nodes.values()
            if n.node_type == NodeType.FACT and n.embedding
        ]
        scored_facts = sorted(
            fact_nodes,
            key=lambda n: _cosine(q_emb, n.embedding),
            reverse=True,
        )[:_TOP_FACT_K]

        fact_lines = []
        for n in scored_facts:
            sess_idx = n.sessions[-1] if n.sessions else 0
            if is_date:
                date_str = session_date_map.get(sess_idx, "")
                prefix = f"[session {sess_idx}{', ' + date_str if date_str else ''}]"
            else:
                prefix = f"[session {sess_idx}]"
            fact_lines.append(f"- {prefix} {n.name}")
        facts_ctx = "\n".join(fact_lines) if fact_lines else "(no relevant facts found)"

        # ── Layer 2: GraphMemory BFS + halflife/guard scoring ─────────────────
        seeds = _detect_graph_seeds(question, graph, q_emb=q_emb, speakers=speakers)
        if not seeds:
            seeds = speakers or []

        visited_node_ids: set[str] = set()
        visited_triple_ids: set[str] = set()
        all_nodes = []
        all_triples = []
        for sp in seeds:
            ns, ts = graph.get_subgraph(sp, depth=2, bidirectional=True)
            for n in ns:
                if n.node_id not in visited_node_ids:
                    visited_node_ids.add(n.node_id)
                    all_nodes.append(n)
            for t in ts:
                if t.triple_id not in visited_triple_ids:
                    visited_triple_ids.add(t.triple_id)
                    all_triples.append(t)

        graph_ctx = "(no relevant graph nodes found)"
        top_nodes = []
        if all_triples:
            # Only apply halflife/guard scoring when subgraph is large (mirrors idea_27)
            if len(all_triples) > 20 and q_emb:
                max_sess = max(
                    (max(n.sessions) for n in all_nodes if n.sessions), default=1
                ) or 1

                node_scores: list[tuple] = []
                for n in all_nodes:
                    if not n.embedding:
                        continue
                    score = _cosine(q_emb, n.embedding)
                    if n.node_type == NodeType.STATE:
                        if n.is_current and n.sessions:
                            score += _HALFLIFE_BETA * max(n.sessions) / max_sess
                        elif not n.is_current and apply_guard:
                            score -= _GUARD_PENALTY
                    node_scores.append((n, score))

                node_scores.sort(key=lambda x: x[1], reverse=True)
                top_ids = {n.node_id for n, _ in node_scores[:_TOP_GRAPH_K]}
                all_triples = [
                    t for t in all_triples
                    if t.subject_id in top_ids or t.object_id in top_ids
                ][:_TOP_GRAPH_TRIP]
                # Re-filter nodes to only those appearing in filtered triples
                used_ids = {t.subject_id for t in all_triples} | {t.object_id for t in all_triples}
                all_nodes = [n for n in all_nodes if n.node_id in used_ids]

            top_nodes = all_nodes
            graph_ctx = graph.linearize_subgraph(
                all_nodes,
                all_triples,
                seed_name=seeds[0] if seeds else "",
                sort_by_session=True,
            ) or "(no relevant graph nodes found)"

        # ── Layer 3: Session text retrieval ───────────────────────────────────
        scored_sess = sorted(
            [s for s in self._sessions if s.get("embedding")],
            key=lambda s: _cosine(q_emb, s["embedding"]),
            reverse=True,
        )
        top_sessions = scored_sess[:_SESSION_TOP_K]

        if self._sessions:
            last = self._sessions[-1]
            top_sess_nums = {s["sess_num"] for s in top_sessions}
            if last["sess_num"] not in top_sess_nums:
                top_sessions.append(last)

        top_sessions = sorted(top_sessions, key=lambda s: s["sess_num"])
        session_ctx = "\n\n---\n\n".join(
            s.get("text", "") for s in top_sessions
        ) or "(no session excerpts available)"

        # ── LLM generation ────────────────────────────────────────────────────
        user_content = (
            f"Memory facts:\n{facts_ctx}\n\n"
            f"Knowledge graph:\n{graph_ctx}\n\n"
            f"Session excerpts:\n{session_ctx}\n\n"
        )

        if history:
            # Multi-turn chat: use lenient system, append question as latest user msg
            system = _COMBINED_PLUS_SYSTEM_LENIENT
            history_msgs = [{"role": "system", "content": system}] + list(history)
            user_content += f"Question: {question}"
            answer_text = client.gpt_with_history(
                history_msgs, user_content, max_tokens=256
            ).strip()
        else:
            # Single-turn: use router to pick system + QA format
            guidance = _route_question(question, client)
            system = (_COMBINED_PLUS_SYSTEM_STRICT if guidance.strictness == "strict"
                      else _COMBINED_PLUS_SYSTEM_LENIENT)
            qa_prompt = _qa_prompt(question, category, guidance.answer_format)
            user_content += qa_prompt
            answer_text = client.gpt(system, user_content, max_tokens=64, temperature=0.0).strip()

        cited_node_ids = [n.node_id for n in top_nodes]
        return answer_text, [], cited_node_ids
