"""
Data-layer module for the snowballing literature review app.

Responsibilities:
- Seed search via OpenAlex (covers arXiv, journals, conferences — 250M+ works).
- Paper metadata, references (backward) and citations (forward) from OpenAlex.
- Breadth-first snowballing traversal that builds a (papers, edges) graph.

The Streamlit UI in `app.py` is the only consumer.
"""

from __future__ import annotations

import math
import re
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import requests

OPENALEX_BASE = "https://api.openalex.org"

# Only fetch fields we actually use — cuts response size significantly.
_OA_SELECT = (
    "id,title,publication_year,authorships,abstract_inverted_index,"
    "cited_by_count,concepts,topics,open_access,best_oa_location,"
    "locations,doi,primary_location"
)


@dataclass
class Paper:
    """Normalized paper record used throughout the app."""

    paper_id: str  # OpenAlex work ID (e.g. "W2963403868")
    title: str
    year: int | None
    authors: list[str]
    abstract: str
    citation_count: int
    fields_of_study: list[str]
    venue: str
    pdf_url: str | None
    landing_url: str | None
    arxiv_id: str | None
    doi: str | None

    @property
    def primary_field(self) -> str:
        return self.fields_of_study[0] if self.fields_of_study else "Unknown"

    @property
    def author_set(self) -> set[str]:
        return {a.lower() for a in self.authors}


@dataclass
class SnowballGraph:
    papers: dict[str, Paper] = field(default_factory=dict)
    # list of (source_id, target_id, kind) where kind is "references" or "cites"
    edges: list[tuple[str, str, str]] = field(default_factory=list)
    seed_id: str | None = None


# ---------------------------------------------------------------------------
# OpenAlex client (free, no API key needed)
# ---------------------------------------------------------------------------

class OpenAlexClient:
    """Thin wrapper around the OpenAlex REST API with retry/backoff."""

    def __init__(self, email: str | None = None, timeout: int = 15):
        self.timeout = timeout
        self.session = requests.Session()
        # OpenAlex asks for a polite email for higher rate limits (optional)
        self.params: dict[str, str] = {"select": _OA_SELECT}
        if email:
            self.params["mailto"] = email

    def _get(self, url: str, params: dict | None = None, max_retries: int = 3) -> dict:
        merged = {**self.params, **(params or {})}
        backoff = 1.0
        for attempt in range(max_retries):
            resp = self.session.get(url, params=merged, timeout=self.timeout)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code in (429, 502, 503, 504):
                time.sleep(backoff * (2 ** attempt))
                continue
            if resp.status_code == 404:
                return {}
            resp.raise_for_status()
        raise RuntimeError(f"OpenAlex request failed after {max_retries} retries: {url}")

    def search(self, query: str, limit: int = 5) -> list[dict]:
        data = self._get(
            f"{OPENALEX_BASE}/works",
            params={"search": query, "per_page": limit},
        )
        return data.get("results", [])

    def get_work(self, work_id: str) -> dict:
        """Fetch a single work by OpenAlex ID (e.g. 'W123') or external ID (e.g. 'doi:10.xxx')."""
        short_id = work_id.replace("https://openalex.org/", "")
        return self._get(f"{OPENALEX_BASE}/works/{short_id}")

    def get_references(self, work_id: str, limit: int = 10) -> list[dict]:
        """Get works referenced by this paper (backward snowballing)."""
        short_id = work_id.replace("https://openalex.org/", "")
        data = self._get(
            f"{OPENALEX_BASE}/works",
            params={
                "filter": f"cited_by:{short_id}",
                "per_page": limit,
                "sort": "cited_by_count:desc",
            },
        )
        return data.get("results", [])

    def get_citations(self, work_id: str, limit: int = 10) -> list[dict]:
        """Get works that cite this paper (forward snowballing)."""
        short_id = work_id.replace("https://openalex.org/", "")
        data = self._get(
            f"{OPENALEX_BASE}/works",
            params={
                "filter": f"cites:{short_id}",
                "per_page": limit,
                "sort": "publication_date:desc",
            },
        )
        return data.get("results", [])

    def get_refs_and_cites(
        self, work_id: str, max_refs: int = 10, max_cites: int = 10,
    ) -> tuple[list[dict], list[dict]]:
        """Fetch references and citations in parallel threads."""
        refs: list[dict] = []
        cites: list[dict] = []
        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_refs = pool.submit(self.get_references, work_id, max_refs)
            fut_cites = pool.submit(self.get_citations, work_id, max_cites)
            try:
                refs = fut_refs.result()
            except Exception:
                pass
            try:
                cites = fut_cites.result()
            except Exception:
                pass
        return refs, cites


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

_INLINE_TAG_RE = re.compile(r"</?(?:scp|sub|sup|i|b|em|strong)>", re.IGNORECASE)


def _strip_inline_tags(text: str) -> str:
    """Remove inline XML/HTML tags like <scp>, <sub>, <sup>, <i> from titles."""
    return _INLINE_TAG_RE.sub("", text)


def _normalize(raw: dict) -> Paper | None:
    """Convert a raw OpenAlex work dict into our Paper record."""
    if not raw or not raw.get("id"):
        return None

    oa_id = raw["id"].replace("https://openalex.org/", "")

    authors = []
    for authorship in (raw.get("authorships") or []):
        name = (authorship.get("author") or {}).get("display_name")
        if name:
            authors.append(name)

    # Fields of study from concepts/topics
    fields = []
    for concept in (raw.get("concepts") or []):
        name = concept.get("display_name")
        if name and concept.get("level", 99) <= 1:
            fields.append(name)
    if not fields:
        for topic in (raw.get("topics") or [])[:2]:
            name = topic.get("display_name")
            if name:
                fields.append(name)

    # Best available PDF URL
    pdf_url = None
    oa_info = raw.get("open_access") or {}
    if oa_info.get("oa_url"):
        pdf_url = oa_info["oa_url"]
    best_oa = raw.get("best_oa_location") or {}
    if best_oa.get("pdf_url"):
        pdf_url = best_oa["pdf_url"]

    # Extract arXiv ID from locations or IDs
    arxiv_id = None
    doi = raw.get("doi")
    if doi and isinstance(doi, str):
        doi = doi.replace("https://doi.org/", "")
    for loc in (raw.get("locations") or []):
        source = loc.get("source") or {}
        if "arxiv" in (source.get("display_name") or "").lower():
            landing = loc.get("landing_page_url") or ""
            if "arxiv.org/abs/" in landing:
                arxiv_id = landing.rsplit("/abs/", 1)[-1].split("v")[0]
            break

    venue = ""
    primary_loc = raw.get("primary_location") or {}
    source = primary_loc.get("source") or {}
    venue = source.get("display_name") or ""

    return Paper(
        paper_id=oa_id,
        title=_strip_inline_tags(raw.get("title") or "(untitled)"),
        year=raw.get("publication_year"),
        authors=authors,
        abstract=_reconstruct_abstract(raw.get("abstract_inverted_index")),
        citation_count=raw.get("cited_by_count") or 0,
        fields_of_study=fields,
        venue=venue,
        pdf_url=pdf_url,
        landing_url=(f"https://doi.org/{doi}" if doi else f"https://openalex.org/{oa_id}"),
        arxiv_id=arxiv_id,
        doi=doi,
    )


def _reconstruct_abstract(inverted_index: dict | None) -> str:
    """OpenAlex stores abstracts as inverted indexes — reconstruct the text."""
    if not inverted_index:
        return ""
    word_positions: list[tuple[int, str]] = []
    for word, positions in inverted_index.items():
        for pos in positions:
            word_positions.append((pos, word))
    word_positions.sort()
    return " ".join(w for _, w in word_positions)


# ---------------------------------------------------------------------------
# Seed search (OpenAlex)
# ---------------------------------------------------------------------------

def seed_search(query: str, client: OpenAlexClient, limit: int = 5) -> list[Paper]:
    """Search OpenAlex for papers matching a query. Returns normalized Papers."""
    results = client.search(query, limit=limit)
    papers = []
    for raw in results:
        p = _normalize(raw)
        if p is not None:
            papers.append(p)
    return papers


# ---------------------------------------------------------------------------
# Snowball traversal
# ---------------------------------------------------------------------------

def snowball(
    seed_id: str,
    client: OpenAlexClient,
    depth: int = 1,
    max_refs: int = 10,
    max_cites: int = 10,
    max_papers: int | None = 300,
    progress_cb=None,
) -> SnowballGraph:
    """Breadth-first snowball traversal from a seed paper.

    Expands nodes concurrently (up to 6 at a time) for speed.

    Parameters
    ----------
    seed_id : str
        OpenAlex work ID (e.g. "W2963403868").
    depth : int
        Number of generations to expand (1 = seed + direct neighbors only).
    max_refs, max_cites : int
        Max references / citations to fetch per expanded node.
    max_papers : int | None
        Soft cap on total papers retrieved. Prevents runaway BFS.
    progress_cb : callable | None
        Optional callback `(papers_so_far, queue_size, current_title)`
        used by the UI to update a progress indicator.
    """
    graph = SnowballGraph(seed_id=seed_id)

    seed_raw = client.get_work(seed_id)
    seed = _normalize(seed_raw)
    if seed is None:
        raise ValueError(f"Seed paper not found: {seed_id}")
    graph.papers[seed.paper_id] = seed
    graph.seed_id = seed.paper_id

    expanded: set[str] = set()
    current_level: list[str] = [seed.paper_id]

    for level in range(depth):
        if not current_level:
            break

        # Dedupe and skip already-expanded nodes
        to_expand = [pid for pid in dict.fromkeys(current_level)
                     if pid not in expanded]
        next_level: list[str] = []

        def _expand_one(pid: str) -> tuple[str, list[dict], list[dict]]:
            refs, cites = client.get_refs_and_cites(pid, max_refs, max_cites)
            return pid, refs, cites

        # Fan out up to 6 concurrent expansions (each does 2 parallel requests)
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {}
            for pid in to_expand:
                if max_papers is not None and len(graph.papers) >= max_papers:
                    break
                futures[pool.submit(_expand_one, pid)] = pid

            for future in as_completed(futures):
                pid = futures[future]
                expanded.add(pid)

                if progress_cb:
                    title = graph.papers[pid].title if pid in graph.papers else pid
                    progress_cb(len(graph.papers), len(futures) - len(expanded), title)

                try:
                    _, refs, cites = future.result()
                except Exception:
                    continue

                for raw in refs:
                    neighbor = _normalize(raw)
                    if neighbor is None:
                        continue
                    if neighbor.paper_id not in graph.papers:
                        if max_papers is not None and len(graph.papers) >= max_papers:
                            break
                        graph.papers[neighbor.paper_id] = neighbor
                    graph.edges.append((pid, neighbor.paper_id, "references"))
                    if level + 1 < depth:
                        next_level.append(neighbor.paper_id)

                for raw in cites:
                    neighbor = _normalize(raw)
                    if neighbor is None:
                        continue
                    if neighbor.paper_id not in graph.papers:
                        if max_papers is not None and len(graph.papers) >= max_papers:
                            break
                        graph.papers[neighbor.paper_id] = neighbor
                    graph.edges.append((neighbor.paper_id, pid, "cites"))
                    if level + 1 < depth:
                        next_level.append(neighbor.paper_id)

        current_level = next_level

    # Dedupe edges
    graph.edges = list({(s, t, k) for s, t, k in graph.edges})
    return graph


# ---------------------------------------------------------------------------
# Visualization helpers
# ---------------------------------------------------------------------------

def field_color_map(fields: list[str]) -> dict[str, str]:
    """Stable color mapping across any set of fields of study."""
    palette = [
        "#4C6EF5", "#12B886", "#FA5252", "#FAB005", "#7048E8",
        "#F06595", "#228BE6", "#15AABF", "#82C91E", "#E8590C",
        "#495057",
    ]
    out: dict[str, str] = {}
    for i, name in enumerate(sorted({f or "Unknown" for f in fields})):
        out[name] = palette[i % len(palette)]
    out.setdefault("Unknown", "#adb5bd")
    return out


def make_node_sizer(
    papers: list[Paper], min_size: int = 10, max_size: int = 55,
):
    """Return a function that maps citation count → node size,
    scaled relative to the actual papers in the graph.

    Uses log scaling so a few highly-cited papers don't crush everything
    else to the minimum.
    """
    counts = [p.citation_count for p in papers if p.citation_count and p.citation_count > 0]
    if not counts:
        return lambda _c: min_size

    log_min = math.log1p(min(counts))
    log_max = math.log1p(max(counts))
    span = log_max - log_min

    if span < 1e-9:
        # All papers have the same count
        mid = (min_size + max_size) // 2
        return lambda _c: mid

    def _size(citation_count: int | None) -> int:
        if not citation_count or citation_count <= 0:
            return min_size
        t = (math.log1p(citation_count) - log_min) / span
        t = max(0.0, min(1.0, t))
        return int(min_size + t * (max_size - min_size))

    return _size
