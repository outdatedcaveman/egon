"""High-order graph representation for Egon's unified mind.

Builds a typed graph from shared mind activity, sessions, memory, files, tags,
and category-theory definitions. The graph can be exported to GEXF for Gephi
and queried for structural insights that are injected into mind context.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "state" / "mind.db"
OUT_DIR = ROOT / "state" / "mind_graph"

PATH_RE = re.compile(
    r"(?:[A-Za-z]:[\\/][^\"'<>|\r\n]+|(?:[A-Za-z0-9_.-]+[\\/])+[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,8})"
)
ENDPOINT_RE = re.compile(r"`?(/api/v1/[A-Za-z0-9_./{}-]+)`?")
URL_RE = re.compile(r"https?://[^\s\"'<>]+")
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")


def _now() -> int:
    return int(time.time())


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=8)
    conn.row_factory = sqlite3.Row
    return conn


def _norm_id(prefix: str, value: Any) -> str:
    s = str(value or "").strip().replace("\\", "/")
    s = re.sub(r"[^A-Za-z0-9_.:/@-]+", "_", s)[:180]
    return f"{prefix}:{s or 'unknown'}"


def _safe_json(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        body = json.loads(raw)
        return body if isinstance(body, dict) else {"value": body}
    except Exception:
        return {}


def _clean_artifact(raw: str) -> str:
    return raw.strip().rstrip("`.,);]").replace("\\\\", "\\")


def _add_text_artifacts(graph: "MindGraph",
                        owner_id: str,
                        text: str,
                        artifact_hits: Counter[str],
                        relation_prefix: str) -> None:
    for raw in URL_RE.findall(text)[:8]:
        clean = _clean_artifact(raw)
        uid = graph.node(_norm_id("url", clean), clean[:120], "url")
        graph.edge(owner_id, uid, f"{relation_prefix}_url")
        artifact_hits[uid] += 1
    for raw in ENDPOINT_RE.findall(text)[:12]:
        clean = _clean_artifact(raw)
        eid = graph.node(_norm_id("endpoint", clean), clean, "endpoint")
        graph.edge(owner_id, eid, f"{relation_prefix}_endpoint")
        artifact_hits[eid] += 1
    for raw in PATH_RE.findall(text)[:12]:
        clean = _clean_artifact(raw)
        if clean.startswith("/api/"):
            continue
        fid = graph.node(_norm_id("file", clean), Path(clean).name or clean,
                         "file", path=clean)
        graph.edge(owner_id, fid, f"{relation_prefix}_file")
        artifact_hits[fid] += 1


class MindGraph:
    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: dict[tuple[str, str, str], dict[str, Any]] = {}

    def node(self, node_id: str, label: str, kind: str, **attrs: Any) -> str:
        if node_id not in self.nodes:
            self.nodes[node_id] = {
                "id": node_id,
                "label": str(label)[:240],
                "kind": kind,
                "weight": 0,
                **{k: v for k, v in attrs.items() if v is not None},
            }
        self.nodes[node_id]["weight"] = self.nodes[node_id].get("weight", 0) + 1
        return node_id

    def edge(self, source: str, target: str, kind: str, **attrs: Any) -> None:
        if source == target:
            return
        key = (source, target, kind)
        if key not in self.edges:
            self.edges[key] = {
                "source": source,
                "target": target,
                "kind": kind,
                "weight": 0,
                **{k: v for k, v in attrs.items() if v is not None},
            }
        self.edges[key]["weight"] = self.edges[key].get("weight", 0) + 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": list(self.nodes.values()),
            "edges": list(self.edges.values()),
        }


def _query_tokens(query: str | None) -> set[str]:
    if not query:
        return set()
    return {t.lower() for t in TOKEN_RE.findall(query) if len(t) >= 3}


def _text_matches(text: str, tokens: set[str]) -> int:
    if not tokens:
        return 0
    low = text.lower()
    return sum(1 for t in tokens if t in low)


def build_mind_graph(project: str | None = None,
                     query: str | None = None,
                     limit_activity: int = 1500) -> dict[str, Any]:
    graph = MindGraph()
    tokens = _query_tokens(query)
    if not DB_PATH.exists():
        return {"status": "error", "error": "mind.db missing"}

    artifact_hits: Counter[str] = Counter()
    bridge_pairs: Counter[tuple[str, str]] = Counter()

    with _connect() as conn:
        projects = conn.execute("SELECT * FROM projects").fetchall()
        for p in projects:
            pid = graph.node(_norm_id("project", p["slug"]), p["slug"], "project",
                             root_path=p["root_path"], status=p["status"])
            if project and p["slug"] == project:
                graph.nodes[pid]["query_focus"] = True

        agents = conn.execute("SELECT * FROM agents").fetchall()
        for a in agents:
            graph.node(_norm_id("agent", a["name"]), a["name"], "agent")

        sessions_sql = """SELECT s.*, ag.name AS agent_name, p.slug AS project_slug
                          FROM sessions s
                          JOIN agents ag ON ag.id = s.agent_id
                          LEFT JOIN projects p ON p.id = s.project_id"""
        params: list[Any] = []
        if project:
            sessions_sql += " WHERE p.slug = ?"
            params.append(project)
        for s in conn.execute(sessions_sql, params).fetchall():
            sid = graph.node(_norm_id("session", s["id"]),
                             s["external_id"] or f"session {s['id']}",
                             "session", started_at=s["started_at"],
                             ended_at=s["ended_at"])
            aid = graph.node(_norm_id("agent", s["agent_name"]),
                             s["agent_name"], "agent")
            graph.edge(aid, sid, "ran_session")
            if s["project_slug"]:
                pid = graph.node(_norm_id("project", s["project_slug"]),
                                 s["project_slug"], "project")
                graph.edge(sid, pid, "worked_on")

        act_sql = """SELECT a.*, ag.name AS agent_name, p.slug AS project_slug,
                            s.external_id AS session_external_id
                     FROM activity a
                     JOIN sessions s ON s.id = a.session_id
                     JOIN agents ag ON ag.id = s.agent_id
                     LEFT JOIN projects p ON p.id = s.project_id"""
        params = []
        if project:
            act_sql += " WHERE p.slug = ?"
            params.append(project)
        act_sql += " ORDER BY a.ts DESC LIMIT ?"
        params.append(int(limit_activity))
        for a in conn.execute(act_sql, params).fetchall():
            activity_id = graph.node(_norm_id("activity", a["id"]),
                                     a["kind"], "activity",
                                     ts=a["ts"], agent=a["agent_name"],
                                     project=a["project_slug"])
            session_id = graph.node(_norm_id("session", a["session_id"]),
                                    a["session_external_id"] or a["session_id"],
                                    "session")
            graph.edge(session_id, activity_id, "emitted")
            tool = _safe_json(a["payload_json"]).get("tool")
            if tool:
                tid = graph.node(_norm_id("tool", tool), tool, "tool")
                graph.edge(activity_id, tid, "used_tool")
            payload_text = json.dumps(_safe_json(a["payload_json"]),
                                      ensure_ascii=False)
            _add_text_artifacts(graph, activity_id, payload_text,
                                artifact_hits, "mentions")

        mem_sql = "SELECT * FROM memory"
        params = []
        clauses = []
        if project:
            clauses.append("tags LIKE ?")
            params.append(f"%{project}%")
        if tokens:
            like = " OR ".join(["content LIKE ? OR tags LIKE ?" for _ in tokens])
            clauses.append(f"({like})")
            for token in tokens:
                params.extend([f"%{token}%", f"%{token}%"])
        if clauses:
            mem_sql += " WHERE " + " OR ".join(clauses)
        mem_sql += " ORDER BY updated_at DESC LIMIT 400"
        for m in conn.execute(mem_sql, params).fetchall():
            text = m["content"] or ""
            mid = graph.node(_norm_id("memory", m["id"]),
                             f"{m['kind']}:{text[:80]}", "memory",
                             memory_kind=m["kind"],
                             updated_at=m["updated_at"],
                             query_score=_text_matches(text + " " + (m["tags"] or ""), tokens))
            if m["attribution_session_id"]:
                sid = graph.node(_norm_id("session", m["attribution_session_id"]),
                                 f"session {m['attribution_session_id']}", "session")
                graph.edge(sid, mid, "documented")
            for tag in [t.strip() for t in (m["tags"] or "").split(",") if t.strip()]:
                tag_id = graph.node(_norm_id("tag", tag.lower()), tag.lower(), "tag")
                graph.edge(mid, tag_id, "tagged")
            _add_text_artifacts(graph, mid, text, artifact_hits, "mentions")

        for f in conn.execute("SELECT * FROM files").fetchall():
            fid = graph.node(_norm_id("file", f["path"]),
                             Path(f["path"]).name or f["path"], "file",
                             path=f["path"], lease_expires_at=f["lease_expires_at"])
            if f["project_id"]:
                row = conn.execute("SELECT slug FROM projects WHERE id = ?",
                                   (f["project_id"],)).fetchone()
                if row:
                    pid = graph.node(_norm_id("project", row["slug"]),
                                     row["slug"], "project")
                    graph.edge(pid, fid, "owns_file")

    _add_category_layer(graph, tokens)

    degrees = Counter()
    for e in graph.edges.values():
        degrees[e["source"]] += e["weight"]
        degrees[e["target"]] += e["weight"]
        bridge_pairs[(graph.nodes[e["source"]]["kind"],
                      graph.nodes[e["target"]]["kind"])] += e["weight"]

    insights = _make_insights(graph, degrees, artifact_hits, bridge_pairs, tokens)
    path = export_gexf(graph, project=project, query=query)
    return {
        "status": "ok",
        "generated_at": _now(),
        "project": project,
        "query": query,
        "node_count": len(graph.nodes),
        "edge_count": len(graph.edges),
        "gephi_gexf_path": str(path),
        "insights": insights,
        "graph": graph.to_dict(),
    }


def _add_category_layer(graph: MindGraph, tokens: set[str]) -> None:
    try:
        from lib.categorical_mind import parse_categories_from_markdown
    except Exception:
        return

    paths = list(ROOT.glob("*.md"))
    for folder in (ROOT / "docs", ROOT / "state"):
        if folder.exists():
            paths.extend(folder.rglob("*.md"))
    brain = Path.home() / ".gemini" / "antigravity" / "brain"
    if brain.exists():
        paths.extend(brain.rglob("*.md"))

    seen_categories = set()
    for path in paths[:2000]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for cat in parse_categories_from_markdown(text):
            cid = graph.node(_norm_id("category", cat.name), cat.name, "category",
                             source=str(path),
                             query_score=_text_matches(cat.name, tokens))
            if cat.name not in seen_categories:
                seen_categories.add(cat.name)
            for obj in sorted(cat.objects):
                oid = graph.node(_norm_id("catobj", f"{cat.name}:{obj}"),
                                 obj, "category_object",
                                 category=cat.name,
                                 query_score=_text_matches(obj, tokens))
                graph.edge(cid, oid, "has_object")
            for (dom, codom), labels in cat.morphisms.items():
                did = graph.node(_norm_id("catobj", f"{cat.name}:{dom}"),
                                 dom, "category_object", category=cat.name)
                cid_obj = graph.node(_norm_id("catobj", f"{cat.name}:{codom}"),
                                     codom, "category_object", category=cat.name)
                for label in labels:
                    graph.edge(did, cid_obj, "morphism", label=label,
                               category=cat.name)


def _make_insights(graph: MindGraph,
                   degrees: Counter[str],
                   artifact_hits: Counter[str],
                   bridge_pairs: Counter[tuple[str, str]],
                   tokens: set[str]) -> list[dict[str, Any]]:
    insights: list[dict[str, Any]] = []

    for node_id, score in degrees.most_common(8):
        n = graph.nodes.get(node_id)
        if n and n["kind"] in {"file", "url", "tag", "memory", "category_object"}:
            insights.append({
                "kind": "central_node",
                "title": f"{n['kind']} hub: {n['label']}",
                "score": score,
                "node": n,
            })

    for node_id, count in artifact_hits.most_common(8):
        n = graph.nodes.get(node_id)
        if n and count >= 2:
            insights.append({
                "kind": "reused_artifact",
                "title": f"Repeated artifact: {n['label']}",
                "score": count,
                "node": n,
            })

    if tokens:
        scored = []
        for node_id, n in graph.nodes.items():
            score = _text_matches(json.dumps(n, ensure_ascii=False), tokens)
            if score:
                scored.append((score + degrees[node_id], n))
        for score, n in sorted(scored, key=lambda x: x[0], reverse=True)[:8]:
            insights.append({
                "kind": "query_resonance",
                "title": f"Query resonance: {n['label']}",
                "score": score,
                "node": n,
            })

    for (left, right), count in bridge_pairs.most_common(8):
        insights.append({
            "kind": "type_bridge",
            "title": f"{left} -> {right}",
            "score": count,
        })

    deduped = []
    seen = set()
    for item in insights:
        key = (item.get("kind"), item.get("title"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped[:16]


def export_gexf(graph: MindGraph,
                project: str | None = None,
                query: str | None = None) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    suffix = project or "all"
    if query:
        suffix += "_query"
    path = OUT_DIR / f"mind_graph_{suffix}_{stamp}.gexf"

    ET.register_namespace("", "http://www.gexf.net/1.2draft")
    root = ET.Element("gexf", {
        "xmlns": "http://www.gexf.net/1.2draft",
        "version": "1.2",
    })
    meta = ET.SubElement(root, "meta", {"lastmodifieddate": time.strftime("%Y-%m-%d")})
    ET.SubElement(meta, "creator").text = "Egon Mind"
    ET.SubElement(meta, "description").text = "Unified agent/action/artifact graph"
    g = ET.SubElement(root, "graph", {"mode": "static", "defaultedgetype": "directed"})
    nodes_el = ET.SubElement(g, "nodes")
    for n in graph.nodes.values():
        node = ET.SubElement(nodes_el, "node", {
            "id": n["id"],
            "label": n["label"],
        })
        node.set("type", n["kind"])
        node.set("weight", str(n.get("weight", 1)))
    edges_el = ET.SubElement(g, "edges")
    for i, e in enumerate(graph.edges.values()):
        edge = ET.SubElement(edges_el, "edge", {
            "id": str(i),
            "source": e["source"],
            "target": e["target"],
            "label": e["kind"],
            "weight": str(e.get("weight", 1)),
        })
        if e.get("label"):
            edge.set("relation", str(e["label"]))

    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return path


def context_insights(project: str | None = None,
                     query: str | None = None,
                     limit: int = 6) -> list[dict[str, Any]]:
    res = build_mind_graph(project=project, query=query, limit_activity=600)
    if res.get("status") != "ok":
        return []
    return (res.get("insights") or [])[:limit]


if __name__ == "__main__":
    print(json.dumps(build_mind_graph(limit_activity=400), indent=2, ensure_ascii=False))
