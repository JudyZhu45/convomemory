# ConvoMemory

Turn multi-session conversation histories into a queryable knowledge graph.

**Extract → Visualize → Query**

![graph](docs/graph-viz-research.md)

---

## What it does

Given a conversation dataset, ConvoMemory:
1. Calls an LLM to extract typed nodes (PERSON / STATE / EVENT / FACT / CONCEPT) and labeled edges from each session
2. Renders the graph in a concentric ring layout — each person is a hub, facts/events/states fan out in typed rings around them
3. Answers natural-language questions by running a ReAct agent over the graph

Demo dataset: LoCoMo conv-26 (Caroline & Melanie, 19 sessions).

---

## Quick start

### 1. Clone & install

```bash
git clone https://github.com/JudyZhu45/convomemory.git
cd convomemory
pip install -r requirements.txt
```

### 2. Set your API key

```bash
cp .env.example .env
# then edit .env:
# OPENAI_API_KEY=sk-...
```

Or export directly:

```bash
export OPENAI_API_KEY=sk-...
```

### 3. Run the server

```bash
uvicorn server:app --reload --port 8000
```

Open **http://localhost:8000** in your browser.

### 4. Build a memory graph

- Click **+ Add Memory** in the top bar
- Choose number of sessions (3–5 for a quick demo, up to 19 for the full dataset)
- Click **Build** — progress streams in real time

### 5. Explore & query

- **Graph tab**: drag nodes, scroll to zoom, drag background to pan, double-click to reset layout
- **Chat**: ask anything — *"What are Caroline's hobbies?"*, *"What events did Melanie attend?"*

---

## Streamlit app (alternative UI)

```bash
streamlit run app.py
```

Set your API key in the sidebar. Same build/graph/query flow with Streamlit components.

---

## Project structure

```
convomemory/
├── server.py              # FastAPI server (SSE build, /graph, /query)
├── prototype.html         # Single-page React+SVG UI
├── app.py                 # Streamlit UI (alternative)
├── requirements.txt
├── demo/
│   └── locomo_sample.json # LoCoMo conv-26 sample data
├── core/
│   ├── api_client.py      # OpenAI API wrapper
│   ├── graph_builder.py   # LLM extraction → GraphMemory
│   ├── graph_memory.py    # Graph data structures
│   └── query_agent.py     # ReAct query agent
├── components/
│   └── visualizer.py      # Pyvis renderer (for Streamlit)
└── docs/
    ├── graph-viz-research.md       # Visualization strategy research
    └── graph-viz-implementation.md # Concentric ring layout design notes
```

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `OPENAI_API_KEY` | — | Required |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Override for proxy/other providers |
| `OPENAI_MODEL` | `gpt-4.1-mini` | Model used for extraction and queries |

---

## Tech stack

- **Backend**: FastAPI, Server-Sent Events for build streaming
- **Frontend**: React 18 (CDN), SVG, no build step
- **LLM**: OpenAI-compatible API (gpt-4.1-mini default)
- **Visualization**: custom concentric ring layout with fly-in animation
