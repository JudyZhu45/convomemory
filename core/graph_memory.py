"""Graph-structured memory: EntityNode + Triple + GraphMemory.

Phase 1 implementation — data structures only.
Phase 2 will add the Memory Construction Agent that populates this graph.

Architecture:
  EntityNode  — typed graph node with embedding + alias table
  Triple      — typed (subject, predicate, object) edge with provenance
  GraphMemory — adjacency-list graph with hybrid search (string + embedding)

NodeType / RelType are open enums (plain strings) to avoid rigid schema
constraints during early development.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from typing import Optional


# ── Helpers ──────────────────────────────────────────────────────────────────

def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def _new_id() -> str:
    return uuid.uuid4().hex[:8]


# ── Node & Triple types (open strings, not Enum) ─────────────────────────────

class NodeType:
    PERSON   = "PERSON"
    ANIMAL   = "ANIMAL"   # named pets/animals — same edge rights as PERSON
    EVENT    = "EVENT"
    STATE    = "STATE"    # mutable current condition (identity, goals, plans, relationships)
    FACT     = "FACT"     # immutable facts, preferences, possessions, memories
    ORG      = "ORG"
    LOCATION = "LOCATION" # placeholder — builder produces these via entity grounding
    CONCEPT  = "CONCEPT"


class RelType:
    PARTICIPATED_IN = "PARTICIPATED_IN"   # person/animal → event (attended/ran/visited)
    ORGANIZED       = "ORGANIZED"         # person → event (led/hosted/arranged)
    HAS_STATE       = "HAS_STATE"         # person/animal → state (mutable)
    HAS_FACT        = "HAS_FACT"          # person/animal → fact (immutable)
    RELATED_TO      = "RELATED_TO"        # person/event → concept/org (general)
    KNOWS           = "KNOWS"             # person → person
    SUPERSEDED_BY   = "SUPERSEDED_BY"     # old state → new state (temporal)
    INVALIDATED_BY  = "INVALIDATED_BY"    # old state → cause (cascade: indirect dependency)
    SUPPORTS        = "SUPPORTS"          # FACT/EVENT → STATE: evidence for the state
    REFINES         = "REFINES"           # STATE → STATE: more specific version
    REALIZED_BY     = "REALIZED_BY"       # STATE → EVENT: plan/goal actualized


# ── EntityNode ────────────────────────────────────────────────────────────────

@dataclass
class EntityNode:
    """A single node in GraphMemory.

    Attributes:
        node_id:    short uuid, globally unique within a GraphMemory instance
        name:       canonical display name ("Caroline", "charity race")
        node_type:  NodeType constant
        aliases:    lowercase strings that resolve to this node
                    (nicknames, short forms — NOT pronouns)
        sessions:   which session indices this node appeared in
        embedding:  embedding of the canonical name, for semantic search
    """
    node_id:   str
    name:      str
    node_type: str
    aliases:    set[str]      = field(default_factory=set, repr=False)
    sessions:   list[int]     = field(default_factory=list)
    embedding:  list[float]   = field(default_factory=list, repr=False)
    is_current: bool          = field(default=True)

    def add_alias(self, alias: str) -> None:
        self.aliases.add(alias.lower())

    def to_dict(self) -> dict:
        d = {
            "node_id":   self.node_id,
            "name":      self.name,
            "node_type": self.node_type,
            "aliases":   sorted(self.aliases),
            "sessions":  self.sessions,
        }
        if not self.is_current:
            d["is_current"] = False
        return d


# ── Triple ────────────────────────────────────────────────────────────────────

@dataclass
class Triple:
    """A directed typed edge: (subject) --[predicate]--> (object).

    object_id may reference another EntityNode.node_id, or be a literal
    string (e.g. a date value for OCCURRED_AT).

    Attributes:
        triple_id:   short uuid
        subject_id:  EntityNode.node_id
        predicate:   RelType constant
        object_id:   EntityNode.node_id or literal string
        dialog_id:   source dialog turn, e.g. "D2:14"
        session_idx: session number
        date:        session date string (for temporal queries)
    """
    triple_id:   str
    subject_id:  str
    predicate:   str
    object_id:   str
    dialog_id:   str   = ""
    session_idx: int   = 0
    date:        str   = ""
    is_valid:    bool  = True

    def to_dict(self) -> dict:
        d = {
            "triple_id":   self.triple_id,
            "subject_id":  self.subject_id,
            "predicate":   self.predicate,
            "object_id":   self.object_id,
            "dialog_id":   self.dialog_id,
            "session_idx": self.session_idx,
            "date":        self.date,
        }
        if not self.is_valid:
            d["is_valid"] = False  # only persist when non-default to keep JSON compact
        return d


# ── GraphMemory ───────────────────────────────────────────────────────────────

class GraphMemory:
    """Adjacency-list knowledge graph with hybrid entity lookup.

    Hybrid lookup order:
      1. Exact match on canonical name (case-insensitive)
      2. Alias match (prefix aliases registered on EntityNode)
      3. Embedding cosine similarity (requires node embeddings)

    The graph is directed: triples are indexed by subject_id.
    Use get_subgraph() for BFS traversal from a seed node.
    """

    def __init__(self) -> None:
        self._nodes:    dict[str, EntityNode]    = {}   # node_id → node
        self._triples:  list[Triple]             = []
        self._adj:      dict[str, list[Triple]]  = {}   # subject_id → triples (forward)
        self._radj:     dict[str, list[Triple]]  = {}   # object_id  → triples (reverse)
        # lookup indexes
        self._name_idx: dict[str, str]           = {}   # lower(name) → node_id
        self._alias_idx: dict[str, str]          = {}   # lower(alias) → node_id

    # ── Write ─────────────────────────────────────────────────────────────────

    def add_entity(
        self,
        name: str,
        node_type: str,
        aliases: list[str] | None = None,
        session_idx: int | None = None,
        embedding: list[float] | None = None,
    ) -> EntityNode:
        """Create or update an entity node. Returns the node.

        If a node with the same canonical name already exists, updates it
        (adds aliases, appends session, updates embedding if provided).
        """
        existing = self.find_entity(name)
        if existing:
            if session_idx is not None and session_idx not in existing.sessions:
                existing.sessions.append(session_idx)
            if embedding:
                existing.embedding = embedding
            for a in (aliases or []):
                existing.add_alias(a)
                self._alias_idx[a.lower()] = existing.node_id
            return existing

        node_id = _new_id()
        node = EntityNode(
            node_id=node_id,
            name=name,
            node_type=node_type,
            aliases=set(a.lower() for a in (aliases or [])),
            sessions=[session_idx] if session_idx is not None else [],
            embedding=embedding or [],
        )
        self._nodes[node_id] = node
        self._name_idx[name.lower()] = node_id
        for a in (aliases or []):
            self._alias_idx[a.lower()] = node_id
        return node

    def add_triple(
        self,
        subject: EntityNode | str,
        predicate: str,
        obj: EntityNode | str,
        dialog_id: str = "",
        session_idx: int = 0,
        date: str = "",
    ) -> Triple:
        """Add a directed edge. subject/obj can be EntityNode or node_id string."""
        sid = subject.node_id if isinstance(subject, EntityNode) else subject
        oid = obj.node_id    if isinstance(obj, EntityNode)    else obj

        triple = Triple(
            triple_id=_new_id(),
            subject_id=sid,
            predicate=predicate,
            object_id=oid,
            dialog_id=dialog_id,
            session_idx=session_idx,
            date=date,
        )
        self._triples.append(triple)
        self._adj.setdefault(sid, []).append(triple)
        self._radj.setdefault(oid, []).append(triple)
        return triple

    def supersede_state(
        self,
        old_node_id: str,
        new_node_id: str,
        dialog_id: str = "",
        session_idx: int = 0,
        date: str = "",
    ) -> Triple:
        """Mark old state as superseded by new state.

        1. Set old node is_current=False.
        2. Invalidate all is_valid=True triples pointing AT the old node
           (except SUPERSEDED_BY edges themselves).
        3. Add SUPERSEDED_BY edge old → new.
        """
        old = self._nodes.get(old_node_id)
        if old:
            old.is_current = False
        for t in self._radj.get(old_node_id, []):
            if t.is_valid and t.predicate != RelType.SUPERSEDED_BY:
                t.is_valid = False
        return self.add_triple(
            old_node_id,
            RelType.SUPERSEDED_BY,
            new_node_id,
            dialog_id=dialog_id,
            session_idx=session_idx,
            date=date,
        )

    def delete_node(self, node_id: str) -> None:
        """Physically remove a node and all its edges. Used by FACT→EVENT promotion."""
        node = self._nodes.pop(node_id, None)
        if not node:
            return
        self._name_idx.pop(node.name.lower(), None)
        for a in node.aliases:
            self._alias_idx.pop(a, None)
        self._triples = [t for t in self._triples
                         if t.subject_id != node_id and t.object_id != node_id]
        self._adj.pop(node_id, None)
        self._radj.pop(node_id, None)
        for sid in list(self._adj):
            self._adj[sid] = [t for t in self._adj[sid] if t.object_id != node_id]
        for oid in list(self._radj):
            self._radj[oid] = [t for t in self._radj[oid] if t.subject_id != node_id]

    def iter_nodes_by_type(self, types: list[str]):
        """Yield all nodes whose node_type is in `types`."""
        for node in self._nodes.values():
            if node.node_type in types:
                yield node

    def merge_entities(self, keep_id: str, merge_id: str) -> None:
        """Merge merge_id node into keep_id. Rewrites triples, removes merge_id."""
        if keep_id not in self._nodes or merge_id not in self._nodes:
            return
        keep = self._nodes[keep_id]
        gone = self._nodes.pop(merge_id)

        # Transfer aliases
        for a in gone.aliases:
            keep.add_alias(a)
            self._alias_idx[a] = keep_id
        self._name_idx[gone.name.lower()] = keep_id

        # Rewrite triples
        for t in self._triples:
            if t.subject_id == merge_id:
                t.subject_id = keep_id
            if t.object_id == merge_id:
                t.object_id = keep_id

        # Rebuild adjacency for keep_id
        self._adj[keep_id] = [t for t in self._triples if t.subject_id == keep_id]
        self._adj.pop(merge_id, None)
        self._radj[keep_id] = [t for t in self._triples if t.object_id == keep_id]
        self._radj.pop(merge_id, None)

    # ── Lookup ────────────────────────────────────────────────────────────────

    def find_entity(self, name: str) -> Optional[EntityNode]:
        """Find entity by name, alias, or prefix. Returns None if not found."""
        if not name:
            return None
        key = name.lower()

        # 1. Exact name match
        if key in self._name_idx:
            return self._nodes[self._name_idx[key]]

        # 2. Exact alias match
        if key in self._alias_idx:
            return self._nodes[self._alias_idx[key]]

        # 3. Prefix match (min 3 chars)
        if len(key) >= 3:
            for stored_key, node_id in {**self._name_idx, **self._alias_idx}.items():
                if stored_key.startswith(key) and len(stored_key) > len(key):
                    return self._nodes[node_id]

        return None

    def find_entity_by_embedding(
        self, query_emb: list[float], top_k: int = 3, threshold: float = 0.80
    ) -> list[EntityNode]:
        """Return top-k nodes by embedding similarity above threshold."""
        if not query_emb:
            return []
        scored = [
            (node, _cosine(query_emb, node.embedding))
            for node in self._nodes.values()
            if node.embedding
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        return [n for n, s in scored[:top_k] if s >= threshold]

    def resolve_entity(
        self, name: str, query_emb: list[float] | None = None
    ) -> Optional[EntityNode]:
        """Hybrid lookup: string match first, embedding fallback."""
        node = self.find_entity(name)
        if node:
            return node
        if query_emb:
            results = self.find_entity_by_embedding(query_emb, top_k=1)
            return results[0] if results else None
        return None

    # ── Traversal ─────────────────────────────────────────────────────────────

    def neighbors(self, node_id: str) -> list[tuple[EntityNode | None, Triple]]:
        """Return (object_node_or_None, triple) for all outgoing edges."""
        result = []
        for t in self._adj.get(node_id, []):
            obj_node = self._nodes.get(t.object_id)
            result.append((obj_node, t))
        return result

    def neighbors_reverse(self, node_id: str) -> list[tuple[EntityNode | None, Triple]]:
        """Return (subject_node_or_None, triple) for all incoming edges."""
        result = []
        for t in self._radj.get(node_id, []):
            subj_node = self._nodes.get(t.subject_id)
            result.append((subj_node, t))
        return result

    def get_subgraph(
        self,
        seed_name: str,
        depth: int = 2,
        predicate_filter: list[str] | None = None,
        bidirectional: bool = False,
    ) -> tuple[list[EntityNode], list[Triple]]:
        """BFS from seed node up to `depth` hops.

        bidirectional=True: traverse both forward and reverse edges,
        so EVENT/STATE seeds can reach the PERSON nodes connected to them.

        Returns (nodes, triples) in the subgraph.
        """
        seed = self.find_entity(seed_name)
        if not seed:
            return [], []

        visited_ids: set[str]   = {seed.node_id}
        seen_triple_ids: set[str] = set()
        frontier: list[str]     = [seed.node_id]
        subgraph_nodes: list[EntityNode] = [seed]
        subgraph_triples: list[Triple]   = []

        for _ in range(depth):
            next_frontier: list[str] = []
            for nid in frontier:
                # Forward edges
                for obj_node, triple in self.neighbors(nid):
                    if predicate_filter and triple.predicate not in predicate_filter:
                        continue
                    if triple.triple_id not in seen_triple_ids:
                        seen_triple_ids.add(triple.triple_id)
                        subgraph_triples.append(triple)
                    if obj_node and obj_node.node_id not in visited_ids:
                        visited_ids.add(obj_node.node_id)
                        subgraph_nodes.append(obj_node)
                        next_frontier.append(obj_node.node_id)
                # Reverse edges
                if bidirectional:
                    for subj_node, triple in self.neighbors_reverse(nid):
                        if predicate_filter and triple.predicate not in predicate_filter:
                            continue
                        if triple.triple_id not in seen_triple_ids:
                            seen_triple_ids.add(triple.triple_id)
                            subgraph_triples.append(triple)
                        if subj_node and subj_node.node_id not in visited_ids:
                            visited_ids.add(subj_node.node_id)
                            subgraph_nodes.append(subj_node)
                            next_frontier.append(subj_node.node_id)
            frontier = next_frontier
            if not frontier:
                break

        return subgraph_nodes, subgraph_triples

    def linearize_subgraph(
        self,
        nodes: list[EntityNode],
        triples: list[Triple],
        seed_name: str = "",
        sort_by_session: bool = False,
    ) -> str:
        """Render subgraph as a natural-language context string for LLM input.

        Groups triples by subject entity. Literal object_ids (dates, values)
        are rendered directly; node object_ids are resolved to names.

        sort_by_session: when True, sort each entity's triples by session_idx
        ascending so the LLM sees the temporal progression explicitly.
        """
        if not triples:
            return ""

        # Group by subject
        by_subject: dict[str, list[Triple]] = {}
        for t in triples:
            by_subject.setdefault(t.subject_id, []).append(t)

        node_map = {n.node_id: n for n in nodes}
        node_map.update(self._nodes)

        lines = []
        seed_node = self.find_entity(seed_name) if seed_name else None

        order = []
        if seed_node and seed_node.node_id in by_subject:
            order.append(seed_node.node_id)
        for nid in by_subject:
            if nid not in order:
                order.append(nid)

        _skip_outdated = {RelType.SUPPORTS, RelType.REFINES, RelType.REALIZED_BY}

        for nid in order:
            subj_node = node_map.get(nid)
            subj_name = subj_node.name if subj_node else nid
            lines.append(f"[{subj_name}]")
            triples_for_subj = by_subject[nid]
            if sort_by_session:
                triples_for_subj = sorted(triples_for_subj, key=lambda t: t.session_idx)
            for t in triples_for_subj:
                obj_node = node_map.get(t.object_id)
                obj_str = obj_node.name if obj_node else t.object_id
                if obj_node and not obj_node.is_current and t.predicate not in _skip_outdated:
                    obj_str += " [outdated]"
                parts = []
                if t.dialog_id:
                    parts.append(str(t.dialog_id))
                if t.date:
                    parts.append(str(t.date))
                src = f" ({', '.join(parts)})" if parts else ""
                lines.append(f"  - {t.predicate}: {obj_str}{src}")

        return "\n".join(lines)

    # ── Serialization ────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Serialize graph to JSON-compatible dict (embeddings excluded)."""
        return {
            "nodes": [n.to_dict() for n in self._nodes.values()],
            "triples": [t.to_dict() for t in self._triples],
        }

    def save(self, path) -> None:
        """Write graph to a JSON file."""
        import json
        from pathlib import Path
        Path(path).write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False))

    @classmethod
    def from_dict(cls, data: dict) -> "GraphMemory":
        """Reconstruct a GraphMemory from a serialized dict."""
        g = cls()
        for nd in data["nodes"]:
            node = EntityNode(
                node_id=nd["node_id"],
                name=nd["name"],
                node_type=nd["node_type"],
                aliases=set(nd.get("aliases", [])),
                sessions=nd.get("sessions", []),
                is_current=nd.get("is_current", True),
            )
            g._nodes[node.node_id] = node
            g._name_idx[node.name.lower()] = node.node_id
            for a in node.aliases:
                g._alias_idx[a] = node.node_id
        for td in data["triples"]:
            triple = Triple(
                triple_id=td["triple_id"],
                subject_id=td["subject_id"],
                predicate=td["predicate"],
                object_id=td["object_id"],
                dialog_id=td.get("dialog_id", ""),
                session_idx=td.get("session_idx", 0),
                date=td.get("date", ""),
                is_valid=td.get("is_valid", True),
            )
            g._triples.append(triple)
            g._adj.setdefault(triple.subject_id, []).append(triple)
            g._radj.setdefault(triple.object_id, []).append(triple)
        return g

    @classmethod
    def load(cls, path) -> "GraphMemory":
        """Load a GraphMemory from a JSON file."""
        import json
        from pathlib import Path
        return cls.from_dict(json.loads(Path(path).read_text()))

    # ── Stats ─────────────────────────────────────────────────────────────────

    def __len__(self) -> int:
        return len(self._nodes)

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    @property
    def triple_count(self) -> int:
        return len(self._triples)

    def summary(self) -> str:
        type_counts: dict[str, int] = {}
        for n in self._nodes.values():
            type_counts[n.node_type] = type_counts.get(n.node_type, 0) + 1
        rel_counts: dict[str, int] = {}
        for t in self._triples:
            rel_counts[t.predicate] = rel_counts.get(t.predicate, 0) + 1
        parts = [f"{self.node_count} nodes ({', '.join(f'{v} {k}' for k,v in type_counts.items())})"]
        parts.append(f"{self.triple_count} triples ({', '.join(f'{v} {k}' for k,v in rel_counts.items())})")
        return " | ".join(parts)
