"""ConvoMemory FastAPI server.

Run: uvicorn server:app --reload --port 8000
"""
from __future__ import annotations

import json
import queue
import sys
import threading
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
load_dotenv(override=True)

sys.path.insert(0, str(Path(__file__).parent))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from core.api_client import APIClient
from core.graph_builder import build_graph_from_sessions, parse_locomo, parse_claude_export
from core.graph_memory import GraphMemory

app = FastAPI(title="ConvoMemory API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── In-memory state ───────────────────────────────────────────────────────────

_graph: Optional[GraphMemory] = None
_speakers: list[str] = []
_dialog_map: dict[str, dict] = {}   # dia_id → {speaker, text, session_num, date}


# ── Request models ────────────────────────────────────────────────────────────

class BuildRequest(BaseModel):
    source: str = "demo"            # "demo" | "upload"
    sessions: Optional[list[dict]] = None
    max_sessions: int = 5


class QueryRequest(BaseModel):
    question: str


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_dialog_map(sessions: list[dict]) -> dict[str, dict]:
    dm: dict[str, dict] = {}
    for sess in sessions:
        for d in sess.get("dialogs", []):
            dia_id = d.get("dia_id", "")
            if dia_id:
                dm[dia_id] = {
                    "speaker": d.get("speaker", ""),
                    "text": d.get("text", ""),
                    "session_num": sess.get("session_num", 0),
                    "date": sess.get("date_time", ""),
                }
    return dm


def _enrich_graph(graph: GraphMemory) -> dict:
    """Serialize graph and inject source dialogue into each triple."""
    data = graph.to_dict()
    for t in data["triples"]:
        dia_id = t.get("dialog_id", "")
        if dia_id and dia_id in _dialog_map:
            t["source"] = _dialog_map[dia_id]
    return data


def _enrich_cited_nodes(node_ids: list[str]) -> list[dict]:
    """For each cited node_id, find its source dialogue via associated triples."""
    if _graph is None:
        return []
    result = []
    seen: set[str] = set()
    # Build node_id → first triple with dialog_id
    triple_by_node: dict[str, dict] = {}
    for t in _graph._triples:
        if t.dialog_id:
            for nid in (t.subject_id, t.object_id):
                if nid not in triple_by_node:
                    triple_by_node[nid] = {
                        "dialog_id": t.dialog_id,
                        "session_idx": t.session_idx,
                        "date": t.date,
                    }
    for nid in node_ids:
        if nid in seen:
            continue
        seen.add(nid)
        node = _graph._nodes.get(nid)
        if not node:
            continue
        entry: dict = {
            "node_id": nid,
            "name": node.name,
            "node_type": node.node_type,
            "source": None,
        }
        tri = triple_by_node.get(nid)
        if tri:
            dia_id = tri["dialog_id"]
            dm = _dialog_map.get(dia_id)
            if dm:
                entry["source"] = {
                    "speaker": dm.get("speaker", ""),
                    "text": dm.get("text", ""),
                    "session_num": dm.get("session_num", tri["session_idx"]),
                    "date": dm.get("date", tri["date"]),
                }
        result.append(entry)
    return result


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "prototype.html")


@app.post("/build")
def build(req: BuildRequest):
    """SSE stream: progress events → final done event with graph JSON."""
    global _graph, _speakers, _dialog_map

    # Resolve sessions
    if req.source == "demo":
        demo_path = Path(__file__).parent / "demo" / "locomo_sample.json"
        if not demo_path.exists():
            def _err():
                yield f'data: {json.dumps({"type":"error","msg":"Demo file not found"})}\n\n'
            return StreamingResponse(_err(), media_type="text/event-stream")
        raw = json.loads(demo_path.read_text())
        sessions = parse_locomo(raw[0], max_sessions=req.max_sessions)
    elif req.sessions:
        sessions = req.sessions
    else:
        def _err():
            yield f'data: {json.dumps({"type":"error","msg":"No sessions provided"})}\n\n'
        return StreamingResponse(_err(), media_type="text/event-stream")

    _speakers = (
        list(dict.fromkeys(d["speaker"] for d in sessions[0]["dialogs"]))
        if sessions else []
    )
    _dialog_map = _build_dialog_map(sessions)

    client = APIClient()
    progress_q: queue.Queue = queue.Queue()

    def run_build():
        def on_progress(current: int, total: int, msg: str):
            progress_q.put({"type": "progress", "current": current,
                            "total": total, "msg": msg})
        try:
            graph = build_graph_from_sessions(sessions, client,
                                              progress_callback=on_progress)
            global _graph
            _graph = graph
            progress_q.put({
                "type": "done",
                "graph": _enrich_graph(graph),
                "speakers": _speakers,
                "stats": {"nodes": graph.node_count, "triples": graph.triple_count},
            })
        except Exception as exc:
            progress_q.put({"type": "error", "msg": str(exc)})
        finally:
            progress_q.put(None)   # sentinel

    threading.Thread(target=run_build, daemon=True).start()

    def stream():
        while True:
            item = progress_q.get()
            if item is None:
                break
            yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/graph")
def get_graph():
    if _graph is None:
        return {"ready": False, "nodes": [], "triples": [], "speakers": []}
    return {
        "ready": True,
        "graph": _enrich_graph(_graph),
        "speakers": _speakers,
        "stats": {"nodes": _graph.node_count, "triples": _graph.triple_count},
    }


@app.post("/query")
def query(req: QueryRequest):
    if _graph is None:
        return {"answer": "No memory built yet. Click '+ Add Memory' to get started.",
                "steps": [], "cited_nodes": []}
    from core.query_agent import MemoryQueryAgent
    client = APIClient()
    agent = MemoryQueryAgent(_graph, client)
    answer, steps, cited_node_ids = agent.answer(req.question, speakers=_speakers or None)
    cited_nodes = _enrich_cited_nodes(cited_node_ids)
    return {"answer": answer, "steps": steps, "cited_nodes": cited_nodes}
