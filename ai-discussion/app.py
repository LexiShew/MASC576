"""
Snowballing Literature Review — Streamlit app.

Run locally:
    streamlit run app.py
"""

from __future__ import annotations

import os
from html import escape

import streamlit as st
from dotenv import load_dotenv
from streamlit_agraph import Config, Edge, Node, agraph

from snowball import (
    OpenAlexClient,
    Paper,
    SnowballGraph,
    field_color_map,
    make_node_sizer,
    seed_search,
    snowball,
)

load_dotenv()

st.set_page_config(
    page_title="Snowballing Literature Review",
    page_icon="❄️",
    layout="wide",
)


# ---------- client ----------

@st.cache_resource
def get_client() -> OpenAlexClient:
    return OpenAlexClient(email=os.getenv("OPENALEX_EMAIL"))


@st.cache_data(show_spinner=False, ttl=60 * 60)
def cached_search(query: str, limit: int) -> list[Paper]:
    return seed_search(query, get_client(), limit=limit)


# ---------- session state ----------

ss = st.session_state
ss.setdefault("search_results", [])   # list[Paper]
ss.setdefault("seed", None)           # Paper
ss.setdefault("graph", None)          # SnowballGraph
ss.setdefault("selected_node", None)


# ---------- sidebar: search + config ----------

st.sidebar.title("❄️ Snowballing")
st.sidebar.caption("Map a research area starting from one seed paper.")

with st.sidebar.form("seed_search_form", clear_on_submit=False):
    query = st.text_input("Topic or paper title", placeholder="e.g. diffusion models for protein design")
    search_submitted = st.form_submit_button("🔍 Search", use_container_width=True)

if search_submitted and query.strip():
    with st.spinner("Searching…"):
        try:
            ss.search_results = cached_search(query.strip(), 5)
        except Exception as exc:
            st.sidebar.error(f"Search failed: {exc}")
            ss.search_results = []
    ss.seed = None
    ss.graph = None
    ss.selected_node = None

if ss.search_results:
    st.sidebar.markdown("**Top matches** — pick a seed:")
    for i, p in enumerate(ss.search_results):
        label_parts = [p.title or "(untitled)"]
        if p.year:
            label_parts.append(str(p.year))
        if p.authors:
            label_parts.append(", ".join(p.authors[:2]) + (" et al." if len(p.authors) > 2 else ""))
        if p.citation_count:
            label_parts.append(f"{p.citation_count} cites")
        label = " · ".join(label_parts)
        if st.sidebar.button(label, key=f"pick_{i}", use_container_width=True):
            ss.seed = p
            ss.graph = None
            ss.selected_node = None

st.sidebar.divider()
st.sidebar.subheader("Snowball parameters")
depth = st.sidebar.slider("Depth (generations)", 1, 3, 1, help="How many hops out from the seed.")
max_refs = st.sidebar.slider("Refs per paper (backward)", 0, 25, 10)
max_cites = st.sidebar.slider("Citations per paper (forward)", 0, 25, 10)
max_papers = st.sidebar.number_input(
    "Max total papers (safety cap)", min_value=10, max_value=5000, value=300, step=50
)

seed: Paper | None = ss.seed
expand_disabled = seed is None
if st.sidebar.button(
    "Start Map from This Paper",
    type="primary",
    use_container_width=True,
    disabled=expand_disabled,
):
    progress = st.sidebar.progress(0.0, text="Expanding…")
    status = st.sidebar.empty()

    def _cb(n_papers, queue_size, title):
        pct = min(1.0, n_papers / float(max_papers))
        progress.progress(pct, text=f"{n_papers} papers · queue {queue_size}")
        status.caption(title[:80])

    try:
        with st.spinner("Snowballing…"):
            ss.graph = snowball(
                seed.paper_id,
                get_client(),
                depth=depth,
                max_refs=max_refs,
                max_cites=max_cites,
                max_papers=int(max_papers),
                progress_cb=_cb,
            )
        progress.empty()
        status.empty()
    except Exception as exc:
        progress.empty()
        status.empty()
        st.sidebar.error(f"Snowball failed: {exc}")


# ---------- main area ----------

st.title("Snowballing Literature Review")

if seed is None:
    st.info(
        "👈 Start by searching for a topic or paper title in the sidebar, "
        "then pick one of the top matches as your seed."
    )
    st.stop()

st.markdown(f"**Seed:** {escape(seed.title)}", unsafe_allow_html=True)
meta_bits = []
if seed.year:
    meta_bits.append(str(seed.year))
if seed.authors:
    meta_bits.append(", ".join(seed.authors[:3]))
if seed.citation_count:
    meta_bits.append(f"{seed.citation_count} citations")
if meta_bits:
    st.caption(" · ".join(meta_bits))

graph: SnowballGraph | None = ss.graph
if graph is None:
    st.info("Adjust parameters in the sidebar, then click **Start Map from This Paper**.")
    st.stop()


# ---------- filters ----------

papers = list(graph.papers.values())
years = [p.year for p in papers if p.year]
year_min = min(years) if years else 1990
year_max = max(years) if years else 2026
all_fields = sorted({p.primary_field for p in papers})

cite_counts = sorted({p.citation_count for p in papers})
cite_max = cite_counts[-1] if cite_counts else 0

col_f1, col_f2, col_f3 = st.columns([2, 2, 2])
with col_f1:
    year_range = st.slider(
        "Year range",
        min_value=int(year_min),
        max_value=int(year_max),
        value=(int(year_min), int(year_max)),
        disabled=year_min == year_max,
    )
with col_f2:
    field_filter = st.multiselect(
        "Fields of study",
        options=all_fields,
        default=all_fields,
    )
with col_f3:
    min_citations = st.slider(
        "Min citations",
        min_value=0,
        max_value=max(cite_max, 1),
        value=0,
        help="Hide papers with fewer citations than this.",
    )

col_f4, col_f5, col_f6 = st.columns([2, 2, 2])
with col_f4:
    edge_kind_filter = st.radio(
        "Edge type",
        ["All", "References (backward)", "Citations (forward)"],
        index=0,
        horizontal=True,
        help="Filter which citation links are shown.",
    )
with col_f5:
    same_author_only = st.toggle(
        "Same author as seed only",
        value=False,
        help="Keep papers sharing at least one author with the seed.",
    )
with col_f6:
    freeze_layout = st.toggle(
        "Freeze layout",
        value=False,
        help="Turn on after the graph settles so dragged nodes stay in place.",
    )

seed_paper = graph.papers.get(graph.seed_id)
seed_authors = seed_paper.author_set if seed_paper else set()


def keep(p: Paper) -> bool:
    if p.year is not None and not (year_range[0] <= p.year <= year_range[1]):
        return False
    if field_filter and p.primary_field not in field_filter:
        return False
    if min_citations > 0 and p.citation_count < min_citations:
        return False
    if same_author_only and p.paper_id != graph.seed_id and not (
        p.author_set & seed_authors
    ):
        return False
    return True


visible_ids = {p.paper_id for p in papers if keep(p)}
# Always keep seed visible.
if graph.seed_id:
    visible_ids.add(graph.seed_id)

color_map = field_color_map([p.primary_field for p in papers])
node_size = make_node_sizer(papers)


# ---------- build agraph nodes/edges ----------

def tooltip(p: Paper) -> str:
    abstract = (p.abstract[:280] + "…") if len(p.abstract) > 280 else p.abstract
    auth = ", ".join(p.authors[:4]) + (" et al." if len(p.authors) > 4 else "")
    bits = [
        escape(p.title),
        escape(auth or "—"),
        f"{p.year or '?'} · {escape(p.venue or '')}",
        f"Citations: {p.citation_count}",
        f"Field: {escape(p.primary_field)}",
        "",
        escape(abstract or "(no abstract available)"),
    ]
    return "\n".join(bits)


def _short_label(title: str, max_len: int = 25) -> str:
    """Truncate to max_len, breaking at a word boundary."""
    if len(title) <= max_len:
        return title
    cut = title[:max_len].rsplit(" ", 1)
    # If no space found, hard-cut at max_len
    text = cut[0] if len(cut) > 1 else title[:max_len]
    return text + "…"


nodes = []
for p in papers:
    if p.paper_id not in visible_ids:
        continue
    is_seed = p.paper_id == graph.seed_id
    sz = node_size(p.citation_count)
    base_color = color_map.get(p.primary_field, "#adb5bd")

    nodes.append(
        Node(
            id=p.paper_id,
            label=_short_label(p.title),
            size=sz * (1.4 if is_seed else 1.0),
            color={
                "background": base_color,
                "border": "#222" if is_seed else base_color,
                "highlight": {"background": "#F7A35C", "border": "#e67700"},
            },
            title=tooltip(p),
            borderWidth=4 if is_seed else 1.5,
            borderWidthSelected=3,
            shape="dot",
            shadow={"enabled": is_seed, "size": 10, "color": "rgba(0,0,0,0.2)"},
            font={
                "size": 11,
                "color": "#1a1a1a",
                "strokeWidth": 3,
                "strokeColor": "#ffffff",
                "face": "Inter, Segoe UI, sans-serif",
            },
        )
    )

_edge_kind_map = {
    "All": None,
    "References (backward)": "references",
    "Citations (forward)": "cites",
}
_active_kind = _edge_kind_map.get(edge_kind_filter)

edges = []
for s, t, k in graph.edges:
    if s not in visible_ids or t not in visible_ids:
        continue
    if _active_kind is not None and k != _active_kind:
        continue
    is_ref = k == "references"
    edges.append(
        Edge(
            source=s,
            target=t,
            type="CURVE_SMOOTH",
            color="#94d2bd" if is_ref else "#74c0fc",
            width=1.2,
        )
    )

config = Config(
    width=1100,
    height=700,
    directed=True,
    physics=not freeze_layout,
    hierarchical=False,
    nodeHighlightBehavior=True,
    highlightColor="#F7A35C",
    collapsible=False,
    staticGraph=False,
    initialZoom=0.9,
    maxZoom=4,
    minZoom=0.2,
)

# ---------- layout ----------

left, right = st.columns([3, 1])

with left:
    st.markdown("#### Citation map")
    selected = agraph(nodes=nodes, edges=edges, config=config)
    if selected:
        ss.selected_node = selected

    # Legend
    with st.expander("Legend", expanded=False):
        st.markdown("**Node color — field of study**")
        for fname, color in color_map.items():
            st.markdown(
                f"<span style='display:inline-block;width:12px;height:12px;"
                f"background:{color};border-radius:50%;margin-right:6px;'></span>"
                f"{escape(fname)}",
                unsafe_allow_html=True,
            )
        st.markdown("**Edge color — link type**")
        st.markdown(
            "<span style='display:inline-block;width:20px;height:3px;"
            "background:#94d2bd;margin-right:6px;vertical-align:middle;'></span>"
            "References (backward)",
            unsafe_allow_html=True,
        )
        st.markdown(
            "<span style='display:inline-block;width:20px;height:3px;"
            "background:#74c0fc;margin-right:6px;vertical-align:middle;'></span>"
            "Citations (forward)",
            unsafe_allow_html=True,
        )
        st.markdown("**Node size** — proportional to citation count")

with right:
    st.markdown("#### Paper details")
    target_id = ss.selected_node or graph.seed_id
    p = graph.papers.get(target_id) if target_id else None
    if p is None:
        st.caption("Click a node to see details.")
    else:
        st.markdown(f"**{escape(p.title)}**", unsafe_allow_html=True)
        if p.authors:
            st.caption(", ".join(p.authors[:6]) + (" et al." if len(p.authors) > 6 else ""))
        meta = []
        if p.year:
            meta.append(str(p.year))
        if p.venue:
            meta.append(p.venue)
        meta.append(f"{p.citation_count} citations")
        meta.append(p.primary_field)
        st.caption(" · ".join(meta))

        if p.abstract:
            st.markdown("**Abstract**")
            st.write(p.abstract)
        else:
            st.caption("No abstract available.")

        link_cols = st.columns(2)
        with link_cols[0]:
            if p.pdf_url:
                st.link_button("Open PDF", p.pdf_url, use_container_width=True)
            elif p.arxiv_id:
                st.link_button(
                    "Open arXiv",
                    f"https://arxiv.org/abs/{p.arxiv_id}",
                    use_container_width=True,
                )
        with link_cols[1]:
            if p.landing_url:
                st.link_button("Paper page", p.landing_url, use_container_width=True)

# ---------- summary stats ----------

st.divider()
c1, c2, c3, c4 = st.columns(4)
c1.metric("Papers collected", len(graph.papers))
c2.metric("Papers shown", len(visible_ids))
c3.metric("Edges shown", len(edges))
c4.metric("Fields", len({graph.papers[i].primary_field for i in visible_ids}))
