"""Memory Construction Agent — builds GraphMemory from conversation sessions.

Aligned with statemem/src/locomo/graph_builder.py (v6).

Generic session format (vs. LoCoMo-specific original):
    {
        "session_num": int,
        "date_time": str,
        "dialogs": [{"speaker": str, "text": str, "dia_id": str}]
    }

Post-processing pipeline (deferred, after batch embedding):
  Phase 1: FACT→EVENT promotion (co-participant or ORG reference)
  Phase 2: STATE-STATE CONTRADICTS → supersede_state
  Phase 3: Cascade invalidation
"""
from __future__ import annotations

import json
import logging
import re
from typing import Callable

from .api_client import APIClient
from .graph_memory import GraphMemory, NodeType, RelType, EntityNode, _cosine

logger = logging.getLogger(__name__)

# ── Builder system prompt ──────────────────────────────────────────────────────

_SYSTEM = """\
You are a memory curator building a knowledge graph about two people's ongoing conversation.

For each piece of information in the new session, choose one action:

  CREATE       — a brand-new entity (person, animal, org, concept) not yet in the graph
  ADD_FACT     — an immutable fact OR a past action/event: preference, possession, memory,
                 skill, characteristic, or anything that happened (attended, ran, visited,
                 gave, painted, went to…)
  ADD_STATE    — a mutable current condition: identity, relationship status, goals, ongoing plans
  UPDATE_STATE — an existing STATE has changed (old value replaced by new one)
  ADD_EVENT    — ONLY when ≥2 named speakers are explicitly doing the same thing TOGETHER in
                 this very turn (e.g. "we went hiking yesterday", "she and I attended X")
  IGNORE       — small talk, emotional support, meta-conversation, no new factual content

━━ PERSON creation rules ━━
- CREATE PERSON only for a single individual human referred to by a proper name
- Do NOT create PERSON for: group nouns ("Melanie and kids", "the family", "the community"),
  event names, place names, or any non-human entity
- For groups, use CREATE with entity_type=CONCEPT or extract as a separate event

━━ ANIMAL creation rules ━━
- CREATE ANIMAL for named pets/animals (Luna, Oliver, Buddy)
- Animals can have ADD_STATE / ADD_FACT, just like a PERSON
- Unnamed animals ("a dog at the park") → do NOT create node; fold into FACT text
- Animal kind (dog, cat, etc.) goes into the FACT, not into the node name

━━ FACT vs EVENT — builder ALWAYS emits FACT for solo past actions ━━
When recording an action that already happened (someone did/attended/went/ran/painted…),
ALWAYS emit ADD_FACT, never ADD_EVENT — unless the joint-speaker exception applies.

Whether the FACT should be promoted to a shared EVENT node (because multiple people
participated, or it relates to a known organization) is decided automatically by the
post-processing pipeline. You do NOT need to make that decision.

Exception: if the dialog explicitly describes BOTH speakers participating in the same
activity in this very turn ("we went hiking together yesterday"), you MAY emit two
ADD_FACT actions (one per speaker) — the pipeline will merge them.

━━ STATE vs FACT ━━
STATE  (mutable — can change, will use UPDATE_STATE later):
  is transgender, is married, plans to become a counselor, wants to adopt a child
FACT   (immutable — preferences, possessions, memories, skills, past actions):
  favorite book is X, has a sentimental necklace, attended LGBTQ pride fest in 2022

━━ Emotional STATE — strict rules ━━
Emit ADD_STATE for an emotion ONLY IF the dialog satisfies ≥1 of:
  (a) The emotion is directed at a long-term object/community
  (b) The same emotion is mentioned ≥2 times in this session
  (c) The speaker uses persistence language: "always", "ever since", "all these years"

If 0 conditions met → IGNORE
If only (a) is uncertain and (b)/(c) not met → ADD_FACT (one-time feeling)

━━ Deduplication ━━
- Before ADD_STATE/ADD_FACT/ADD_EVENT: check EXISTING KNOWLEDGE carefully
- If a very similar node already exists with only minor wording differences → IGNORE
- If the same event happened at a different date/location → it IS a new ADD_FACT
- UPDATE_STATE only when a STATE has genuinely changed (new information contradicts old)
- Do NOT extract the same relationship/event from both speakers' perspectives separately

━━ Specific entity grounding (HARD RULE) ━━
When a speaker refers to a SPECIFIC entity using a generic placeholder
("home country", "my school", "the company", "that book"), you MUST resolve
it to the concrete proper name if that name appears ANYWHERE in:
  (a) the current dialog,
  (b) earlier turns of this session,
  (c) the [EXISTING KNOWLEDGE] block.

Examples:
  Dialog says "moved from home country" + earlier mention of "Sweden"
    → fact: "moved from Sweden 4 years ago"   (NOT "moved from home country")
  Dialog says "my school's reunion" + earlier mention of "Stanford"
    → event: "Stanford reunion"               (NOT "my school's reunion")

If the concrete name is NOT recoverable from any of (a)(b)(c), keep the generic
phrase but also emit a CREATE action for a CONCEPT node with that placeholder
name, so future sessions can resolve it.

━━ Time absolutization (HARD RULE) ━━
The session header provides SESSION_DATE — the absolute date of THIS session.
You MUST convert every relative time expression to an absolute year/date BEFORE
writing it into a node name or fact.

  "today" / "tonight"         → SESSION_DATE
  "yesterday"                 → SESSION_DATE − 1 day
  "last week"                 → "the week before SESSION_DATE" (use month/year)
  "last month"                → month before SESSION_DATE (use absolute month + year)
  "last year" / "a year ago"  → year(SESSION_DATE) − 1
  "X years ago"               → year(SESSION_DATE) − X
  "next year"                 → year(SESSION_DATE) + 1
  Specific dates already absolute → keep as-is

NEVER store the literal phrase "last year", "yesterday", "X years ago" in a
node name. Always emit the resolved absolute year/date.

━━ Other rules ━━
- Always use the speaker's full canonical name, never pronouns
- Include date/time in fact name when mentioned (e.g. "charity race on 20 May 2023")
- Keep state/fact names concise (under 60 characters); split compound facts into separate nodes
- For ADD_EVENT relation, use one of: PARTICIPATED_IN, ORGANIZED, RELATED_TO

Output a JSON array of action objects. Each must have "action" as the first key.
ADD_FACT fields:   entity, fact, dialog_id
ADD_STATE fields:  entity, state, dialog_id
UPDATE_STATE:      entity, old_state, new_state, dialog_id
ADD_EVENT fields:  entity, event (include date if known), relation (PARTICIPATED_IN or ORGANIZED), dialog_id
CREATE fields:     entity, entity_type (PERSON/ANIMAL/ORG/CONCEPT)
IGNORE fields:     reason

If nothing meaningful, return [].
"""

_USER_TMPL = """\
[EXISTING KNOWLEDGE]
{prior_knowledge}

[NEW SESSION — session {session_num}, {date_time}]
SESSION_DATE for time absolutization: {date_time}
Speakers: {speakers}

Pronoun resolution:
{pronoun_rules}

{dialog_text}

Output JSON array of actions only (no markdown, no explanation):"""


# ── LLM judge prompts ─────────────────────────────────────────────────────────

_RELATION_JUDGE_SYSTEM = """\
You judge the relation between two pieces of information about the SAME PERSON.

Output exactly ONE of these labels (no explanation, no punctuation):
  SUPPORTS     — the new info is evidence for the candidate (only when candidate is STATE)
  CONTRADICTS  — the two cannot both be currently true
  REFINES      — the new info is a more specific version of the candidate (STATE-STATE only)
  UNRELATED    — none of the above

Rules:
- A FACT/EVENT can SUPPORTS or CONTRADICT a STATE, never REFINE it.
- Two STATEs can CONTRADICT or REFINE; FACT-FACT is always UNRELATED.
- "single parent" does NOT contradict "is single". Output SUPPORTS or UNRELATED, never CONTRADICTS.
- "lives in Beijing" CONTRADICTS "lives in Shanghai" (mutually exclusive locations).
- "likes coffee" + "likes pour-over single-origin coffee" → REFINES.
- When in doubt, output UNRELATED.
"""

_RELATION_JUDGE_USER = """\
New {new_kind}: {new_text}
Existing {candidate_kind}: {candidate_text}

Label:"""

_EVENT_MATCH_SYSTEM = """\
You judge whether two FACTs from different people describe the SAME real-world event.

Output exactly ONE label:
  SAME      — same event, same time, same place (or one is silent on details that match)
  DIFFERENT — different events, even if similar in nature
  UNCLEAR   — not enough information

Rules:
- Same activity at same time = SAME (even if one mentions location and the other doesn't)
- Same activity at different times = DIFFERENT
- Generic activities without specifics ("went hiking") = UNCLEAR unless time/place align
- When in doubt, output UNCLEAR (we'd rather miss a merge than create wrong ones).
"""

_EVENT_MATCH_USER = """\
Person A's fact: {fact_a_text}
Person B's fact: {fact_b_text}

Label:"""

_EVENT_NAME_SYSTEM = """\
Given multiple FACT descriptions of the same event from different people,
output a single concise event name that captures it.

Rules:
- Include date if any source mentions it
- Include location if any source mentions it
- Use neutral framing (not from any one person's perspective)
- Keep under 80 characters
- No quotes, no punctuation at end
"""

_CASCADE_SYSTEM = """\
You identify stored memory states that are now LITERALLY FALSE given a life change.

Only mark a state as invalidated if ONE of these patterns applies:

PATTERN 1 — Search satisfied:
  Old state describes searching/looking for X.
  New state says person acquired/found X.
  → The search state is now FALSE (they found it).

PATTERN 2 — Specific future plan superseded:
  Old state is a concrete future plan (with date or "next month/week").
  New state is a different concrete plan for the same or overlapping time.
  → The old plan is FALSE (replaced by new plan).

PATTERN 3 — Constraint-based preference, constraint removed:
  Old state explicitly cites a constraint that no longer holds.

DO NOT invalidate:
- Emotional states, feelings, hobbies, personality traits, interests
- States that describe WHY the new state exists (causes)
- Ongoing habits or preferences unrelated to the specific change
- States that remain true even after the change

When in doubt, output []. Prefer empty over wrong.

Output ONLY a JSON array of 0-based integer indices, e.g. [0, 2] or [].
"""

_CASCADE_USER = """\
Person: {person}

State changed:
  OLD (no longer true): {old_state}
  NEW (now true): {new_state}

Earlier stored states for {person}:
{candidates}

Apply the 3 patterns strictly. Which are now LITERALLY FALSE? Output JSON array only:"""


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


# ── GraphBuilder class ────────────────────────────────────────────────────────

class _GraphBuilder:
    """Stateful builder: holds APIClient, exposes LLM judge helpers and action dispatcher."""

    def __init__(self, client: APIClient) -> None:
        self.client = client
        self._stats: dict[str, int] = {
            "promotions_total": 0,
            "promotions_cond_a": 0,
            "promotions_cond_b": 0,
            "facts_deleted_by_promotion": 0,
            "cascade_invalidations": 0,
            "cascade_llm_calls": 0,
            "judge_relation_calls": 0,
            "judge_same_event_calls": 0,
            "derive_event_name_calls": 0,
        }

    # ── LLM helpers ───────────────────────────────────────────────────────────

    def _judge_relation(self, new_text: str, candidate_text: str,
                        new_kind: str, candidate_kind: str) -> str:
        prompt = _RELATION_JUDGE_USER.format(
            new_kind=new_kind, new_text=new_text,
            candidate_kind=candidate_kind, candidate_text=candidate_text,
        )
        self._stats["judge_relation_calls"] += 1
        raw = self.client.gpt(_RELATION_JUDGE_SYSTEM, prompt, max_tokens=10)
        if raw.startswith("ERROR:"):
            return "UNRELATED"
        label = raw.strip().upper().split()[0] if raw.strip() else "UNRELATED"
        return label if label in ("SUPPORTS", "CONTRADICTS", "REFINES", "UNRELATED") else "UNRELATED"

    def _judge_same_event(self, fact_a: str, fact_b: str) -> str:
        prompt = _EVENT_MATCH_USER.format(fact_a_text=fact_a, fact_b_text=fact_b)
        self._stats["judge_same_event_calls"] += 1
        raw = self.client.gpt(_EVENT_MATCH_SYSTEM, prompt, max_tokens=10)
        if raw.startswith("ERROR:"):
            return "UNCLEAR"
        label = raw.strip().upper().split()[0] if raw.strip() else "UNCLEAR"
        return label if label in ("SAME", "DIFFERENT", "UNCLEAR") else "UNCLEAR"

    def _derive_event_name(self, fact_text: str, co_fact_texts: list[str]) -> str:
        inputs = "\n".join([fact_text] + co_fact_texts)
        self._stats["derive_event_name_calls"] += 1
        raw = self.client.gpt(_EVENT_NAME_SYSTEM, f"Inputs:\n{inputs}\n\nOutput:", max_tokens=50)
        if raw.startswith("ERROR:") or not raw.strip():
            return fact_text[:60]
        return raw.strip()

    # ── FACT → EVENT promotion ────────────────────────────────────────────────

    def _find_referenced_orgs(self, text: str, graph: GraphMemory) -> list:
        text_lower = text.lower()
        return [n for n in graph.iter_nodes_by_type([NodeType.ORG])
                if n.name.lower() in text_lower]

    def _try_promote_to_event(
        self, fact_node, person_node, has_fact_triple,
        graph: GraphMemory, dialog_id: str, session_idx: int, date: str,
    ) -> None:
        """Promote FACT to EVENT if another person has the same-event FACT, or FACT refs an ORG."""
        _PARTICIPANT_TYPES = [NodeType.PERSON, NodeType.ANIMAL]

        co_participants = []
        for other_node in list(graph.iter_nodes_by_type(_PARTICIPANT_TYPES)):
            if other_node.node_id == person_node.node_id:
                continue
            for obj_node, t in list(graph.neighbors(other_node.node_id)):
                if t.predicate != RelType.HAS_FACT or not t.is_valid:
                    continue
                if not obj_node or obj_node.node_id == fact_node.node_id:
                    continue
                if fact_node.embedding and obj_node.embedding:
                    if _cosine(fact_node.embedding, obj_node.embedding) < 0.7:
                        continue
                verdict = self._judge_same_event(fact_node.name, obj_node.name)
                if verdict == "SAME":
                    co_participants.append((other_node, obj_node, t))

        referenced_orgs = self._find_referenced_orgs(fact_node.name, graph)

        if not co_participants and not referenced_orgs:
            return

        self._stats["promotions_total"] += 1
        if co_participants:
            self._stats["promotions_cond_a"] += 1
        if referenced_orgs:
            self._stats["promotions_cond_b"] += 1

        co_fact_texts = [fn.name for _, fn, _ in co_participants]
        event_name = self._derive_event_name(fact_node.name, co_fact_texts)

        event_node = graph.add_entity(
            name=event_name,
            node_type=NodeType.EVENT,
            session_idx=session_idx,
            embedding=self.client.embed_single(event_name),
        )
        logger.info("Promoted FACT %r → EVENT %r", fact_node.name, event_name)

        graph.add_triple(person_node, RelType.PARTICIPATED_IN, event_node,
                         dialog_id=dialog_id, session_idx=session_idx, date=date)

        for other_node, other_fact, other_t in co_participants:
            graph.add_triple(other_node, RelType.PARTICIPATED_IN, event_node,
                             dialog_id=other_t.dialog_id,
                             session_idx=other_t.session_idx, date=other_t.date)
            graph.delete_node(other_fact.node_id)
            self._stats["facts_deleted_by_promotion"] += 1

        for org_node in referenced_orgs:
            graph.add_triple(event_node, RelType.RELATED_TO, org_node,
                             dialog_id=dialog_id, session_idx=session_idx, date=date)

        graph.delete_node(fact_node.node_id)
        self._stats["facts_deleted_by_promotion"] += 1

    # ── Post-processing ───────────────────────────────────────────────────────

    def _post_process(self, graph: GraphMemory) -> None:
        """Phase 1: FACT→EVENT promotion. Phase 2: STATE-STATE CONTRADICTS → supersede.
        Phase 3: Cascade invalidation."""
        _PARTICIPANT_TYPES = [NodeType.PERSON, NodeType.ANIMAL]

        # Phase 1: FACT→EVENT promotion
        candidates = []
        for person_node in list(graph.iter_nodes_by_type(_PARTICIPANT_TYPES)):
            for obj_node, t in list(graph.neighbors(person_node.node_id)):
                if t.predicate == RelType.HAS_FACT and t.is_valid and obj_node:
                    candidates.append((obj_node, person_node, t))

        for fact_node, person_node, hft in candidates:
            if fact_node.node_id not in graph._nodes:
                continue
            self._try_promote_to_event(
                fact_node, person_node, hft, graph,
                dialog_id=hft.dialog_id, session_idx=hft.session_idx, date=hft.date,
            )

        # Phase 2: STATE-STATE CONTRADICTS → supersede
        for person_node in list(graph.iter_nodes_by_type(_PARTICIPANT_TYPES)):
            states = [
                (obj, t)
                for obj, t in list(graph.neighbors(person_node.node_id))
                if t.predicate == RelType.HAS_STATE and t.is_valid
                and obj and obj.is_current
            ]
            for i, (sa, ta) in enumerate(states):
                for sb, tb in states[i + 1:]:
                    if not sa.is_current or not sb.is_current:
                        continue
                    if sa.embedding and sb.embedding:
                        if _cosine(sa.embedding, sb.embedding) < 0.30:
                            continue
                    verdict = self._judge_relation(sa.name, sb.name, "STATE", "STATE")
                    if verdict == "CONTRADICTS":
                        if ta.session_idx >= tb.session_idx:
                            graph.supersede_state(sb.node_id, sa.node_id,
                                                  dialog_id=ta.dialog_id,
                                                  session_idx=ta.session_idx, date=ta.date)
                        else:
                            graph.supersede_state(sa.node_id, sb.node_id,
                                                  dialog_id=tb.dialog_id,
                                                  session_idx=tb.session_idx, date=tb.date)

        # Phase 3: cascade invalidation
        self._cascade_invalidate(graph)

    def _cascade_invalidate(self, graph: GraphMemory) -> None:
        sup_triples = [t for t in graph._triples
                       if t.predicate == RelType.SUPERSEDED_BY and t.is_valid]
        if not sup_triples:
            return

        processed_pairs: set[tuple[str, str]] = set()

        for sup_t in sup_triples:
            old_node = graph._nodes.get(sup_t.subject_id)
            new_node = graph._nodes.get(sup_t.object_id)
            if not old_node or not new_node:
                continue
            pair_key = (old_node.node_id, new_node.node_id)
            if pair_key in processed_pairs:
                continue
            processed_pairs.add(pair_key)

            person_node = None
            for n, t in graph.neighbors_reverse(new_node.node_id):
                if (t.predicate == RelType.HAS_STATE and n
                        and n.node_type in (NodeType.PERSON, NodeType.ANIMAL)):
                    person_node = n
                    break
            if not person_node:
                continue

            trigger_session = sup_t.session_idx or 0
            candidates = [
                (obj, t)
                for obj, t in graph.neighbors(person_node.node_id)
                if t.predicate == RelType.HAS_STATE and t.is_valid
                and obj and obj.is_current
                and obj.node_id not in (old_node.node_id, new_node.node_id)
                and (t.session_idx or 0) < trigger_session
            ]
            if not candidates:
                continue

            filtered = []
            for obj, t in candidates:
                if obj.embedding and (old_node.embedding or new_node.embedding):
                    sim_old = _cosine(obj.embedding, old_node.embedding) if old_node.embedding else 0.0
                    sim_new = _cosine(obj.embedding, new_node.embedding) if new_node.embedding else 0.0
                    if max(sim_old, sim_new) < 0.25:
                        continue
                filtered.append((obj, t))
            if not filtered:
                continue

            candidates_text = "\n".join(f"[{i}] {obj.name}" for i, (obj, _) in enumerate(filtered))
            prompt = _CASCADE_USER.format(
                person=person_node.name,
                old_state=old_node.name,
                new_state=new_node.name,
                candidates=candidates_text,
            )
            self._stats["cascade_llm_calls"] += 1
            raw = self.client.gpt(_CASCADE_SYSTEM, prompt, max_tokens=64)
            if raw.startswith("ERROR:"):
                continue
            try:
                clean = re.sub(r"^```(?:json)?\s*", "", raw.strip())
                clean = re.sub(r"\s*```$", "", clean).strip()
                indices = json.loads(clean)
                if not isinstance(indices, list):
                    continue
            except Exception:
                continue

            for idx in indices:
                if not isinstance(idx, int) or idx < 0 or idx >= len(filtered):
                    continue
                target_node, target_t = filtered[idx]
                if not target_node.is_current:
                    continue
                graph.add_triple(target_node, RelType.INVALIDATED_BY, new_node,
                                 dialog_id=sup_t.dialog_id, session_idx=sup_t.session_idx,
                                 date=sup_t.date)
                target_node.is_current = False
                for rt in graph._radj.get(target_node.node_id, []):
                    if rt.is_valid and rt.predicate not in (RelType.SUPERSEDED_BY, RelType.INVALIDATED_BY):
                        rt.is_valid = False
                self._stats["cascade_invalidations"] += 1
                logger.info("CASCADE: '%s' invalidated by '%s'", target_node.name, new_node.name)

    # ── Action dispatcher ─────────────────────────────────────────────────────

    def _execute_actions(
        self, actions: list[dict], graph: GraphMemory, session_idx: int, date: str,
    ) -> None:
        for act in actions:
            action = act.get("action", "").upper()
            dialog_id = str(act.get("dialog_id", ""))
            try:
                if action == "CREATE":
                    entity_type = act.get("entity_type", NodeType.CONCEPT)
                    if entity_type not in (NodeType.PERSON, NodeType.ANIMAL,
                                           NodeType.ORG, NodeType.CONCEPT):
                        entity_type = NodeType.CONCEPT
                    graph.add_entity(name=act["entity"], node_type=entity_type,
                                     session_idx=session_idx)

                elif action == "ADD_EVENT":
                    entity_node = graph.find_entity(act["entity"])
                    if not entity_node:
                        entity_node = graph.add_entity(act["entity"], NodeType.PERSON,
                                                        session_idx=session_idx)
                    event_node = _find_similar_node(graph, act["event"], NodeType.EVENT, 0.65)
                    if event_node:
                        if session_idx not in event_node.sessions:
                            event_node.sessions.append(session_idx)
                    else:
                        event_node = graph.add_entity(act["event"], NodeType.EVENT,
                                                       session_idx=session_idx)
                    _VALID = {RelType.PARTICIPATED_IN, RelType.ORGANIZED, RelType.RELATED_TO}
                    relation = act.get("relation", RelType.PARTICIPATED_IN)
                    if relation not in _VALID:
                        relation = RelType.RELATED_TO
                    graph.add_triple(entity_node, relation, event_node,
                                     dialog_id=dialog_id, session_idx=session_idx, date=date)

                elif action == "ADD_STATE":
                    entity_node = graph.find_entity(act["entity"])
                    if not entity_node:
                        entity_node = graph.add_entity(act["entity"], NodeType.PERSON,
                                                        session_idx=session_idx)
                    state_node = _find_similar_node(graph, act["state"], NodeType.STATE, 0.70)
                    if state_node:
                        if session_idx not in state_node.sessions:
                            state_node.sessions.append(session_idx)
                    else:
                        state_node = graph.add_entity(act["state"], NodeType.STATE,
                                                       session_idx=session_idx)
                    graph.add_triple(entity_node, RelType.HAS_STATE, state_node,
                                     dialog_id=dialog_id, session_idx=session_idx, date=date)

                elif action == "ADD_FACT":
                    entity_node = graph.find_entity(act["entity"])
                    if not entity_node:
                        entity_node = graph.add_entity(act["entity"], NodeType.PERSON,
                                                        session_idx=session_idx)
                    fact_node = _find_similar_node(graph, act["fact"], NodeType.FACT, 0.70)
                    if fact_node:
                        if session_idx not in fact_node.sessions:
                            fact_node.sessions.append(session_idx)
                    else:
                        fact_node = graph.add_entity(act["fact"], NodeType.FACT,
                                                      session_idx=session_idx)
                    graph.add_triple(entity_node, RelType.HAS_FACT, fact_node,
                                     dialog_id=dialog_id, session_idx=session_idx, date=date)

                elif action == "UPDATE_STATE":
                    entity_node = graph.find_entity(act["entity"])
                    if not entity_node:
                        entity_node = graph.add_entity(act["entity"], NodeType.PERSON,
                                                        session_idx=session_idx)
                    new_state_node = _find_similar_node(graph, act["new_state"], NodeType.STATE, 0.70)
                    if new_state_node:
                        new_state_node.is_current = True
                        if session_idx not in new_state_node.sessions:
                            new_state_node.sessions.append(session_idx)
                    else:
                        new_state_node = graph.add_entity(act["new_state"], NodeType.STATE,
                                                           session_idx=session_idx)
                    graph.add_triple(entity_node, RelType.HAS_STATE, new_state_node,
                                     dialog_id=dialog_id, session_idx=session_idx, date=date)
                    old_state_node = graph.find_entity(act.get("old_state", ""))
                    if old_state_node and old_state_node.node_id != new_state_node.node_id:
                        graph.supersede_state(
                            old_node_id=old_state_node.node_id,
                            new_node_id=new_state_node.node_id,
                            dialog_id=dialog_id, session_idx=session_idx, date=date,
                        )

                elif action == "IGNORE":
                    pass

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
    """Convert Claude.ai export JSON to generic session list."""
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
) -> tuple[GraphMemory, list[dict]]:
    """Build a GraphMemory from a list of generic sessions.

    Returns (graph, session_index) where session_index is a list of
    {sess_num, text, embedding} dicts for use in smart query retrieval.
    """
    builder = _GraphBuilder(client)
    graph = GraphMemory()
    session_texts: list[dict] = []

    # Seed speaker nodes from first session
    if sessions:
        all_speakers = list(dict.fromkeys(d["speaker"] for d in sessions[0]["dialogs"]))
        for spk in all_speakers:
            graph.add_entity(spk, NodeType.PERSON, session_idx=0)
    else:
        all_speakers = []

    total = len(sessions)
    for idx, sess in enumerate(sessions):
        sess_num = sess["session_num"]
        date_time = sess.get("date_time", "")
        dialogs = sess["dialogs"]

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

        builder._execute_actions(actions, graph, session_idx=sess_num, date=date_time)
        logger.debug("Session %d: %d actions → %s", sess_num, len(actions), graph.summary())
        session_texts.append({
            "sess_num": sess_num,
            "text": f"{date_time}\n{dialog_text}" if date_time else dialog_text,
        })

    # Batch-embed all nodes
    nodes_to_embed = [n for n in graph._nodes.values() if not n.embedding]
    if nodes_to_embed:
        if progress_callback:
            progress_callback(total, total, f"Embedding {len(nodes_to_embed)} nodes…")
        texts = [n.name for n in nodes_to_embed]
        embeddings = client.embed_batch(texts)
        for node, emb in zip(nodes_to_embed, embeddings):
            node.embedding = emb

    # Post-processing (FACT→EVENT promotion, STATE-STATE supersede, cascade)
    if progress_callback:
        progress_callback(total, total, "Post-processing graph…")
    builder._post_process(graph)
    logger.info(
        "Post-process: promotions=%d (A=%d B=%d) deleted=%d cascade=%d (llm=%d)",
        builder._stats["promotions_total"],
        builder._stats["promotions_cond_a"],
        builder._stats["promotions_cond_b"],
        builder._stats["facts_deleted_by_promotion"],
        builder._stats["cascade_invalidations"],
        builder._stats["cascade_llm_calls"],
    )

    # Embed session texts
    if session_texts:
        if progress_callback:
            progress_callback(total, total, f"Embedding {len(session_texts)} sessions…")
        raw_texts = [s["text"][:2000] for s in session_texts]
        sess_embeddings = client.embed_batch(raw_texts)
        for s, emb in zip(session_texts, sess_embeddings):
            s["embedding"] = emb

    if progress_callback:
        progress_callback(total, total, "Done")

    logger.info("GraphMemory built: %s", graph.summary())
    return graph, session_texts
