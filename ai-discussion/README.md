# Snowballing Literature Review

An interactive web app that automates the "snowballing" literature review process:
pick a seed paper, then explore its references (**backward snowballing**) and
the papers that cite it (**forward snowballing**) on an interactive citation
graph — with field-of-study coloring, citation-count sizing, year/field filters,
and abstract pop-ups.

Built with **Streamlit** + **streamlit-agraph**, backed by the
**Semantic Scholar Graph API** (primary — provides real citation data) and the
**arXiv API** (optional alternative for seed search).

---

## Features

| Area | What it does |
|---|---|
| **Seed search** | Text box + top-5 results from Semantic Scholar or arXiv. |
| **One-click expansion** | Snowballs N refs backward + M citations forward per paper. |
| **Depth control** | 1–3 generations (configurable). |
| **Draggable graph** | Click and drag nodes, zoom/pan, physics layout. |
| **Color by field** | Auto color-coding by Semantic Scholar `fieldsOfStudy`. |
| **Size by impact** | Node radius scales with citation count (log). |
| **Hover tooltip** | Title, authors, year, venue, abstract, field. |
| **Details panel** | Clicking a node opens a panel with full abstract + PDF / landing page links. |
| **Year slider** | Hide older papers. |
| **Field filter** | Multi-select to show only chosen fields. |
| **Same-author toggle** | Keep only papers sharing ≥1 author with the seed. |
| **Safety cap** | Max-total-papers knob prevents runaway BFS at depth 3. |

---

## Project layout

```
.
├── app.py               # Streamlit UI
├── snowball.py          # API clients + snowball BFS engine
├── requirements.txt
├── .env.example         # Copy to .env for local config
├── .streamlit/
│   └── config.toml      # Streamlit server/theme defaults
└── README.md
```

---

## 1. Run locally

### Prerequisites

- Python 3.11+
- `pip`

### Setup

```bash
# from this directory
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt

# optional: copy the env template and fill in an API key
cp .env.example .env    # (Windows: copy .env.example .env)
```

### Launch

```bash
streamlit run app.py
```

The app opens at <http://localhost:8501>.

### Semantic Scholar API key (optional)

The app works **without a key**, but unauthenticated requests are throttled
(~1 request/sec, shared quota). For faster snowballing, request a free key at
<https://www.semanticscholar.org/product/api> and add it to `.env`:

```
SEMANTIC_SCHOLAR_API_KEY=xxxxxxxxxxxxxxxxxxxx
```

---

## 2. How to use the app

1. **Search** — In the sidebar, type a topic or paper title and click **Search**. Pick one of the top 5 results as your seed.
2. **Tune parameters** — adjust:
   - **Depth** (1–3 hops)
   - **Refs per paper** (backward neighbors)
   - **Citations per paper** (forward neighbors)
   - **Max total papers** safety cap
3. **Click "Start Map from This Paper"** — a progress bar shows BFS progress.
4. **Explore** — drag nodes, hover for a tooltip, click a node to load its details (abstract + PDF / landing page) in the right-hand panel.
5. **Filter** — use the year slider, field multiselect, or the **Same author as seed** toggle above the graph to reduce noise.

Tip: start with **depth = 1**, **refs = 10**, **citations = 10**. Increase depth only after you're comfortable with the shape of the graph — depth 3 can pull in thousands of papers quickly.

---

## 3. Flexible deployment

Configuration is layered: environment variables → `.env` → `st.secrets`. Whichever
is set first wins. That lets the same code run locally, on Streamlit Community
Cloud, or inside a container.

### Streamlit Community Cloud

1. Push the folder to a GitHub repo.
2. On <https://share.streamlit.io>, create a new app pointing at `app.py`.
3. In the app's **Secrets** panel, add:
   ```toml
   SEMANTIC_SCHOLAR_API_KEY = "xxxx"
   SEED_SEARCH_BACKEND = "semanticscholar"
   ```
4. Deploy.

### Docker / any container host

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8501
CMD ["streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.port=8501"]
```

```bash
docker build -t snowball-lit .
docker run -p 8501:8501 -e SEMANTIC_SCHOLAR_API_KEY=xxxx snowball-lit
```

### Behind a reverse proxy / custom port

Any standard Streamlit flag works:

```bash
streamlit run app.py --server.port 9000 --server.address 0.0.0.0 --server.baseUrlPath /snowball
```

---

## 4. Configuration reference

| Variable | Default | Purpose |
|---|---|---|
| `SEMANTIC_SCHOLAR_API_KEY` | _(unset)_ | Lifts rate limits on Semantic Scholar. |
| `SEED_SEARCH_BACKEND` | `semanticscholar` | Default seed-search source (`semanticscholar` \| `arxiv`). Also switchable in the sidebar. |
| `STREAMLIT_SERVER_PORT` | `8501` | Streamlit-standard. |
| `STREAMLIT_SERVER_ADDRESS` | `localhost` | Streamlit-standard. |

---

## 5. Known limitations

- **Semantic Scholar coverage varies by field.** Highly-cited CS/bio papers are well-indexed; some niche venues may have sparse `fieldsOfStudy` or missing abstracts.
- **Rate limits without an API key** make depth-3 runs slow. Get a free key.
- **"Funding source" connection toggle** was in the spec but Semantic Scholar doesn't expose funding metadata consistently, so the app ships with **same-author** as the relationship toggle instead. Easy to extend in `app.py` if you add a metadata source.
- Snowballing is directional but de-duplicated — if two papers cite each other through different paths, you'll see the logical edge once.

---

## 6. Extending the app

- **Swap the citation backend** — replace `SemanticScholarClient` in `snowball.py` with an OpenAlex or CrossRef wrapper; keep the `Paper` dataclass shape.
- **Add LLM summaries** — the `Paper.abstract` field is the natural input. Drop a call to an LLM in the details panel in `app.py`.
- **Export the graph** — `graph.papers` and `graph.edges` are plain Python; a CSV / GraphML exporter is ~10 lines.
