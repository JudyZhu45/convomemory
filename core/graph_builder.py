"""Memory Construction Agent — builds GraphMemory from conversation sessions.

Adapted from statemem/src/locomo/graph_builder.py.
Accepts generic session dicts instead of LoCoMo-specific format.

Session format:
    {
        "session_num": int,
        "date_time": str,
        "dialogs": [{"speaker": str, "text": str, "dia_id": str}]
    }
"""
from __future__ import annotations

import json
import logging
import re
from typing import Callable

from .api_client import APIClient
from .graph_memory import GraphMemory, NodeType, RelType, EntityNode

logger = logging.getLogger(__name__)

# ── Prompts ───────────────────────────────────────────────────────────────────

_SYSTEM = """\
You are a memory curator building a knowledge graph about two people's ongoing conversation.

For each piece of information in the new session, choose one action:

  CREATE       — a brand-new entity (person, org, concept) not yet in the graph
  ADD_EVENT    — a one-time event that happened (attended, ran, visited, gave, painted…)
  ADD_STATE    — a mutable current condition: identity, relationship status, goals, ongoing plans
  ADD_FACT     — an immutable fact: preference, possession, memory, skill, characteristic
  UPDATE_STATE — an existing STATE has changed (old value replaced by new one)
  IGNORE       — small talk, emotional support, meta-conversation, no new factual content

━━ PERSON creation rules ━━
- CREATE PERSON only for a single individual human referred to by a proper name
- Do NOT create PERSON for: group nouns ("the family", "the community"), event names, place names
- For groups, use CREATE with entity_type=CONCEPT

━━ STATE vs FACT ━━
STATE  (mutable — can change):
  is married, plans to become a counselor, wants to adopt a child
FACT   (immutable — preferences, possessions, memories, skills):
  favorite book is X, has a sentimental necklace, plays violin

━━ Deduplication ━━
- Before ADD_STATE/ADD_FACT/ADD_EVENT: check EXISTING KNOWLEDGE carefully
- If a very similar node already exists → IGNORE
- UPDATE_STATE only when a STATE has genuinely changed

━━ Other rules ━━
- Always use the speaker's full canonical name, never pronouns
- Keep state/fact names concise (under 60 characters)
- For ADD_EVENT relation, use: PARTICIPATED_IN, ORGANIZED, or RELATED_TO

Output a JSON array of action objects. Each must have "action" as the first key.
ADD_EVENT fields:  entity, event, relation, dialog_id
ADD_STATE fields:  entity, state, dialog_id
ADD_FACT fields:   entity, fact, dialog_id
UPDATE_STATE:      entity, old_state, new_state, dialog_id
CREATE fields:     entity, entity_type (PERSON/ORG/CONCEPT)
IGNORE fields:     reason

If nothing meaningful, return [].
"""

_USER_TMPL = """\
[EXISTING KNOWLEDGE]
{prior_knowledge}

[NEW SESSION — session {session_num}, {date_time}]
Speakers: {speakers}

Pronoun resolution:
{pronoun_rules}

{dialog_text}

Output JSON array of actions only (no markdown, no explanation):"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _format_prior_knowledge(graph: GraphMemory, speakers: list[str]) -> str:
    if graph.node_count == 0:
        return "(empty — this is the first session)"
    lines = []
    for speaker in speakers:
        node = graph.find_entity(speaker)
        if not node:
            continue
        sub_nodes, sub_triples = graph.get_subgraph(speaker, depth=1)
        if not sub_triples:
            lines.append(f"{speaker} (PERSON): (no facts yet)")
            continue
        lines.append(f"{speaker} (PERSON):")
        for t in sub_triples:
            obj_node = graph._nodes.get(t.object_id)
            obj_str = obj_node.name if obj_node else t.object_id
            src = f" ({t.dialog_id})" if t.dialog_id else ""
            lines.append(f"  - {t.predicate}: {obj_str}{src}")
    return "\n".join(lines) if lines else "(no relevant prior knowledge)"


def _build_pronoun_rules(speakers: list[str]) -> str:
    rules = []
    for i, spk in enumerate(speakers[:2]):
        other = speakers[1 - i] if len(speakers) > 1 else "unknown"
        rules.append(f'- When {spk} says "I/my/me" → {spk}')
        rules.append(f'- When {spk} says "she/her/he/him/they" → likely {other}')
    return "\n".join(rules)


def _token_jaccard(a: str, b: str) -> float:
    ta = set(re.sub(r"[^\w\s]", "", a.lower()).split())
    tb = set(re.sub(r"[^\w\s]", "", b.lower()).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _find_similar_node(
    graph: GraphMemory, name: str, node_type: str, threshold: float
) -> "EntityNode | None":
    for node in graph._nodes.values():
        if node.node_type == node_type and _token_jaccard(node.name, name) >= threshold:
            return node
    return None


def _execute_actions(actions: list[dict], graph: GraphMemory, session_idx: int, date: str) -> None:
    for act in actions:
        action = act.get("action", "").upper()
        dialog_id = str(act.get("dialog_id", ""))
        try:
            if action == "CREATE":
                entity_type = act.get("entity_type", NodeType.CONCEPT)
                if entity_type not in (NodeType.PERSON, NodeType.ORG, NodeType.CONCEPT):
                    entity_type = NodeType.CONCEPT
                graph.add_entity(name=act["entity"], node_type=entity_type, session_idx=session_idx)

            elif action == "ADD_EVENT":
                entity_node = graph.find_entity(act["entity"])
                if not entity_node:
                    entity_node = graph.add_entity(act["entity"], NodeType.PERSON, session_idx=session_idx)
                event_node = _find_similar_node(graph, act["event"], NodeType.EVENT, 0.65)
                if event_node:
                    if session_idx not in event_node.sessions:
                        event_node.sessions.append(session_idx)
                else:
                    event_node = graph.add_entity(act["event"], NodeType.EVENT, session_idx=session_idx)
                _VALID = {RelType.PARTICIPATED_IN, RelType.ORGANIZED, RelType.RELATED_TO}
                relation = act.get("relation", RelType.PARTICIPATED_IN)
                if relation not in _VALID:
                    relation = RelType.RELATED_TO
                graph.add_triple(entity_node, relation, event_node,
                                 dialog_id=dialog_id, session_idx=session_idx, date=date)

            elif action == "ADD_STATE":
                entity_node = graph.find_entity(act["entity"])
                if not entity_node:
                    entity_node = graph.add_entity(act["entity"], NodeType.PERSON, session_idx=session_idx)
                state_node = _find_similar_node(graph, act["state"], NodeType.STATE, 0.70)
                if state_node:
                    if session_idx not in state_node.sessions:
                        state_node.sessions.append(session_idx)
                else:
                    state_node = graph.add_entity(act["state"], NodeType.STATE, session_idx=session_idx)
                graph.add_triple(entity_node, RelType.HAS_STATE, state_node,
                                 dialog_id=dialog_id, session_idx=session_idx, date=date)

            elif action == "ADD_FACT":
                entity_node = graph.find_entity(act["entity"])
                if not entity_node:
                    entity_node = graph.add_entity(act["entity"], NodeType.PERSON, session_idx=session_idx)
                fact_node = _find_similar_node(graph, act["fact"], NodeType.FACT, 0.70)
                if fact_node:
                    if session_idx not in fact_node.sessions:
                        fact_node.sessions.append(session_idx)
                else:
                    fact_node = graph.add_entity(act["fact"], NodeType.FACT, session_idx=session_idx)
                graph.add_triple(entity_node, RelType.HAS_FACT, fact_node,
                                 dialog_id=dialog_id, session_idx=session_idx, date=date)

            elif action == "UPDATE_STATE":
                entity_node = graph.find_entity(act["entity"])
                if not entity_node:
                    entity_node = graph.add_entity(act["entity"], NodeType.PERSON, session_idx=session_idx)
                new_state_node = graph.add_entity(act["new_state"], NodeType.STATE, session_idx=session_idx)
                graph.add_triple(entity_node, RelType.HAS_STATE, new_state_node,
                                 dialog_id=dialog_id, session_idx=session_idx, date=date)
                old_state_node = graph.find_entity(act.get("old_state", ""))
                if old_state_node:
                    graph.add_triple(old_state_node, "SUPERSEDED_BY", new_state_node,
                                     dialog_id=dialog_id, session_idx=session_idx, date=date)

        except Exception as e:
            logger.warning("Action execution failed (%s): %s — %s", action, e, act)


# ── Parsers ───────────────────────────────────────────────────────────────────

def parse_locomo(sample: dict, max_sessions: int | None = None) -> list[dict]:
    """Convert a LoCoMo sample dict to generic session list."""
    conversation = sample["conversation"]
    sess_keys = sorted(
        [k for k in conversation.keys() if "session" in k and "date_time" not in k],
        key=lambda k: int(k.split("_")[1])
    )
    if max_sessions:
        sess_keys = sess_keys[:max_sessions]

    sessions = []
    for key in sess_keys:
        sess_num = int(key.split("_")[1])
        date_time = conversation.get(f"session_{sess_num}_date_time", "")
        dialogs_raw = conversation.get(key, [])
        dialogs = []
        for d in dialogs_raw:
            text = d.get("text", "").strip()
            if text:
                entry = {
                    "speaker": d.get("speaker", "Unknown"),
                    "text": text,
                    "dia_id": d.get("dia_id", ""),
                }
                if d.get("blip_caption"):
                    entry["image_caption"] = d["blip_caption"]
                dialogs.append(entry)
        if dialogs:
            sessions.append({
                "session_num": sess_num,
                "date_time": date_time,
                "dialogs": dialogs,
            })
    return sessions


def parse_claude_export(data: dict) -> list[dict]:
    """Convert Claude.ai export JSON to generic session list.

    Claude exports: {"conversations": [{"name": ..., "messages": [...]}]}
    Each conversation becomes one session.
    """
    sessions = []
    conversations = data if isinstance(data, list) else data.get("conversations", [])
    for i, conv in enumerate(conversations):
        msgs = conv.get("messages", [])
        dialogs = []
        for m in msgs:
            sender = m.get("sender", m.get("role", "unknown"))
            text = m.get("text", m.get("content", "")).strip()
            if isinstance(text, list):
                text = " ".join(str(p) for p in text)
            if text:
                dialogs.append({
                    "speaker": "User" if sender in ("human", "user") else "Assistant",
                    "text": text,
                    "dia_id": f"C{i+1}:{len(dialogs)+1}",
                })
        if dialogs:
            sessions.append({
                "session_num": i + 1,
                "date_time": conv.get("created_at", ""),
                "dialogs": dialogs,
            })
    return sessions


# ── Main builder ──────────────────────────────────────────────────────────────

def build_graph_from_sessions(
    sessions: list[dict],
    client: APIClient,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> GraphMemory:
    """Build a GraphMemory from a list of generic sessions.

    progress_callback(current, total, status_message) — called after each session.
    """
    graph = GraphMemory()

    # Seed speaker nodes from first session
    if sessions:
        all_speakers = list(dict.fromkeys(
            d["speaker"] for d in sessions[0]["dialogs"]
        ))
        for spk in all_speakers:
            graph.add_entity(spk, NodeType.PERSON, session_idx=0)
    else:
        all_speakers = []

    total = len(sessions)
    for idx, sess in enumerate(sessions):
        sess_num = sess["session_num"]
        date_time = sess.get("date_time", "")
        dialogs = sess["dialogs"]

        # Format dialog text
        lines = []
        for d in dialogs:
            line = f'{d["speaker"]}: {d["text"]}'
            if d.get("image_caption"):
                line += f' [shares image: {d["image_caption"]}]'
            lines.append(line)
        dialog_text = "\n".join(lines)

        speakers = list(dict.fromkeys(d["speaker"] for d in dialogs))
        prior_knowledge = _format_prior_knowledge(graph, speakers or all_speakers)
        pronoun_rules = _build_pronoun_rules(speakers or all_speakers)

        user_prompt = _USER_TMPL.format(
            prior_knowledge=prior_knowledge,
            session_num=sess_num,
            date_time=date_time,
            speakers=", ".join(speakers or all_speakers),
            pronoun_rules=pronoun_rules,
            dialog_text=dialog_text,
        )

        if progress_callback:
            progress_callback(idx, total, f"Processing session {sess_num} ({date_time})…")

        raw = ""
        for attempt in range(5):
            result = client.gpt(_SYSTEM, user_prompt, max_tokens=2048)
            if result.startswith("ERROR:"):
                if "429" in result and attempt < 4:
                    import time
                    time.sleep(60 * (attempt + 1))
                    continue
                logger.warning("Session %d agent failed: %s", sess_num, result)
                break
            raw = result
            break

        if not raw:
            continue

        try:
            clean = re.sub(r"^```(?:json)?\s*", "", raw.strip())
            clean = re.sub(r"\s*```$", "", clean).strip()
            actions = json.loads(clean)
            if not isinstance(actions, list):
                actions = []
        except Exception as e:
            logger.warning("Session %d JSON parse failed: %s", sess_num, e)
            actions = []

        _execute_actions(actions, graph, session_idx=sess_num, date=date_time)
        logger.debug("Session %d: %d actions → %s", sess_num, len(actions), graph.summary())

    # Embed all nodes for semantic search
    nodes_to_embed = [n for n in graph._nodes.values() if not n.embedding]
    if nodes_to_embed:
        if progress_callback:
            progress_callback(total, total, f"Embedding {len(nodes_to_embed)} nodes…")
        texts = [n.name for n in nodes_to_embed]
        embeddings = client.embed_batch(texts)
        for node, emb in zip(nodes_to_embed, embeddings):
            node.embedding = emb

    if progress_callback:
        progress_callback(total, total, "Done")

    logger.info("GraphMemory built: %s", graph.summary())
    return graph
