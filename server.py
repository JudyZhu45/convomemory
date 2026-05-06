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

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from core.api_client import APIClient
from core.graph_builder import build_graph_from_sessions, parse_locomo, parse_claude_export
from core.graph_memory import GraphMemory

app = FastAPI(title="ConvoMemory API")

_ROOT = Path(__file__).parent
app.mount("/assets", StaticFiles(directory=str(_ROOT / "assets")), name="assets") if (_ROOT / "assets").exists() else None
app.mount("/ui_kits", StaticFiles(directory=str(_ROOT / "ui_kits")), name="ui_kits") if (_ROOT / "ui_kits").exists() else None

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
_sessions_embedded: list[dict] = []  # [{sess_num, text, embedding}, ...]


# ── Request models ────────────────────────────────────────────────────────────

class BuildRequest(BaseModel):
    source: str = "demo"            # "demo" | "upload"
    sessions: Optional[list[dict]] = None
    max_sessions: int = 5
    sample_idx: int = 0             # index into locomo10.json (0-9)


class QueryRequest(BaseModel):
    question: str
    history: list[dict] = []   # [{role: "user"|"assistant", content: str}]


class EvalRequest(BaseModel):
    questions: list[dict]   # [{idx, question, answer, category}, ...]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _word_f1(pred: str, gold: str) -> float:
    """Token-level F1 score between predicted and gold answer strings."""
    pred_toks = set(pred.lower().split())
    gold_toks = set(gold.lower().split())
    if not pred_toks or not gold_toks:
        return 0.0
    common = pred_toks & gold_toks
    p = len(common) / len(pred_toks)
    r = len(common) / len(gold_toks)
    return round(2 * p * r / (p + r), 3) if p + r else 0.0


def _parse_plain_text(text: str) -> list[dict]:
    """Parse plain text into a single session with heuristic speaker detection."""
    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
    dialogs = []
    for para in paragraphs:
        # Support "Name: text" format
        if ':' in para.split('\n')[0]:
            first_line = para.split('\n')[0]
            colon_pos = first_line.index(':')
            potential_speaker = first_line[:colon_pos].strip()
            if len(potential_speaker) <= 30 and potential_speaker.replace(' ', '').isalpha():
                speaker = potential_speaker
                body = (first_line[colon_pos+1:].strip() + ' ' + ' '.join(para.split('\n')[1:])).strip()
            else:
                speaker = 'User' if len(dialogs) % 2 == 0 else 'Assistant'
                body = para.replace('\n', ' ').strip()
        else:
            speaker = 'User' if len(dialogs) % 2 == 0 else 'Assistant'
            body = para.replace('\n', ' ').strip()
        if body:
            dialogs.append({"speaker": speaker, "text": body, "dia_id": f"T1:{len(dialogs)+1}"})
    if not dialogs:
        return []
    return [{"session_num": 1, "date_time": "", "dialogs": dialogs}]


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

@app.get("/colors_and_type.css")
def css():
    return FileResponse(Path(__file__).parent / "colors_and_type.css", media_type="text/css")


@app.get("/landing")
def landing():
    return FileResponse(Path(__file__).parent / "landing.html")


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "prototype.html")


@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """Parse an uploaded conversation file and return sessions for /build."""
    content = await file.read()
    filename = file.filename or ""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if ext == "txt":
        sessions = _parse_plain_text(content.decode("utf-8", errors="replace"))
    elif ext in ("json", "") :
        try:
            data = json.loads(content)
        except Exception:
            raise HTTPException(400, "Invalid JSON file")
        if isinstance(data, list) and data and "conversation" in data[0]:
            sessions = parse_locomo(data[0])
        elif isinstance(data, list) or (isinstance(data, dict) and "conversations" in data):
            sessions = parse_claude_export(data)
        else:
            raise HTTPException(400, "Unrecognized JSON format. Supported: Claude export, LoCoMo.")
    else:
        raise HTTPException(400, "Unsupported file type. Use .json or .txt")

    sessions = sessions[:100]   # guard against huge files
    if not sessions:
        raise HTTPException(400, "No conversation sessions found in file")
    return {"sessions": sessions, "session_count": len(sessions)}


@app.get("/demo_samples")
def demo_samples():
    """Return metadata for all available LoCoMo demo samples."""
    demo_path = Path(__file__).parent / "demo" / "locomo10.json"
    if not demo_path.exists():
        return {"samples": []}
    raw = json.loads(demo_path.read_text())
    result = []
    for i, sample in enumerate(raw):
        conv = sample.get("conversation", {})
        sess_keys = sorted(
            [k for k in conv if "session" in k and "date_time" not in k],
            key=lambda k: int(k.split("_")[1]),
        )
        first_sess = conv[sess_keys[0]] if sess_keys else []
        speakers = list(dict.fromkeys(d["speaker"] for d in first_sess))[:2]
        result.append({"idx": i, "speakers": speakers, "session_count": len(sess_keys)})
    return {"samples": result}


@app.post("/build")
def build(req: BuildRequest):
    """SSE stream: progress events → final done event with graph JSON."""
    global _graph, _speakers, _dialog_map

    # Resolve sessions
    if req.source == "demo":
        demo_path = Path(__file__).parent / "demo" / "locomo10.json"
        if not demo_path.exists():
            # fallback to original single-sample file
            demo_path = Path(__file__).parent / "demo" / "locomo_sample.json"
        if not demo_path.exists():
            def _err():
                yield f'data: {json.dumps({"type":"error","msg":"Demo file not found"})}\n\n'
            return StreamingResponse(_err(), media_type="text/event-stream")
        raw = json.loads(demo_path.read_text())
        idx = max(0, min(req.sample_idx, len(raw) - 1))
        sessions = parse_locomo(raw[idx], max_sessions=req.max_sessions)
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
            graph, session_index = build_graph_from_sessions(sessions, client,
                                                              progress_callback=on_progress)
            global _graph, _sessions_embedded
            _graph = graph
            _sessions_embedded = session_index
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
    from core.smart_query import SmartMemoryQuery
    client = APIClient()
    agent = SmartMemoryQuery(_graph, client, _sessions_embedded)
    answer, steps, cited_node_ids = agent.answer(req.question, speakers=_speakers or None, history=req.history or None)
    cited_nodes = _enrich_cited_nodes(cited_node_ids)
    return {"answer": answer, "steps": steps, "cited_nodes": cited_nodes}


@app.get("/qa/{sample_idx}")
def get_qa(sample_idx: int):
    """Return all QA pairs for a LoCoMo sample."""
    demo_path = Path(__file__).parent / "demo" / "locomo10.json"
    if not demo_path.exists():
        raise HTTPException(404, "locomo10.json not found")
    raw = json.loads(demo_path.read_text())
    idx = max(0, min(sample_idx, len(raw) - 1))
    qa = raw[idx].get("qa", [])
    return {"qa": [{"idx": i, **q} for i, q in enumerate(qa)], "count": len(qa)}


@app.post("/run_eval")
def run_eval(req: EvalRequest):
    """SSE stream: run a list of questions against the current graph and return scored results."""
    if _graph is None:
        def _err():
            yield f'data: {json.dumps({"type":"error","msg":"No graph built. Build a memory graph first."})}\n\n'
        return StreamingResponse(_err(), media_type="text/event-stream")

    from core.smart_query import SmartMemoryQuery
    client = APIClient()
    agent = SmartMemoryQuery(_graph, client, _sessions_embedded)
    result_q: queue.Queue = queue.Queue()

    def run():
        for item in req.questions:
            try:
                answer, _, _ = agent.answer(item["question"], speakers=_speakers or None,
                                            category=item.get("category", 0))
                score = _word_f1(answer, str(item["answer"]))
                result_q.put({
                    "type": "result",
                    "idx": item["idx"],
                    "question": item["question"],
                    "expected": str(item["answer"]),
                    "got": answer,
                    "score": score,
                    "category": item.get("category", 0),
                })
            except Exception as e:
                result_q.put({
                    "type": "result",
                    "idx": item["idx"],
                    "question": item["question"],
                    "expected": str(item["answer"]),
                    "got": f"ERROR: {e}",
                    "score": 0.0,
                    "category": item.get("category", 0),
                })
        result_q.put(None)

    threading.Thread(target=run, daemon=True).start()

    def stream():
        total = len(req.questions)
        done = 0
        yield f'data: {json.dumps({"type":"start","total":total})}\n\n'
        while True:
            item = result_q.get()
            if item is None:
                yield f'data: {json.dumps({"type":"done","total":total})}\n\n'
                break
            done += 1
            item["progress"] = {"current": done, "total": total}
            yield f'data: {json.dumps(item, ensure_ascii=False)}\n\n'

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
