"""Deterministic Draw.io (.drawio) structural evaluator.

The reward signal for the ``drawio`` environment is intentionally
**deterministic** — no LLM judge. We extract the .drawio XML from the
agent response and run a fixed checklist that mirrors the strictness the
``drawio-diagram`` skill itself emphasises:

  1. An XML block is present and well-formed (parses cleanly).
  2. The canonical structure exists: ``mxfile`` → ``diagram`` →
     ``mxGraphModel`` → ``root`` with the ``id=0`` / ``id=1`` cells.
  3. Enough ``vertex="1"`` nodes (``min_nodes``).
  4. Enough ``edge="1"`` connectors (``min_edges``).
  5. Every edge's ``source`` / ``target`` references an existing cell id.
  6. Cell ids are unique.
  7. All required label keywords appear somewhere in the diagram text.
  8. (optional) A dashed edge exists, when the task needs a residual /
     skip connection (``needs_dashed_edge``).

``hard`` is 1.0 only when every applicable check passes; ``soft`` is the
fraction of checks passed — a smooth signal the optimizer can climb.

Public API
----------
- :func:`extract_drawio_xml` — pull the .drawio XML out of a response
- :func:`evaluate`           — score one response against a task spec
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET


# ── XML extraction ────────────────────────────────────────────────────────────

_FENCE_RE = re.compile(
    r"```(?:xml|drawio)?\s*(<mxfile.*?</mxfile>)\s*```",
    re.DOTALL | re.IGNORECASE,
)
_BARE_RE = re.compile(r"<mxfile.*?</mxfile>", re.DOTALL | re.IGNORECASE)


def extract_drawio_xml(text: str) -> str:
    """Extract the ``<mxfile>...</mxfile>`` block from a model response.

    Prefers a fenced ```xml code block; falls back to the first bare
    ``<mxfile>`` block anywhere in the text. Returns ``""`` if none found.
    """
    if not text:
        return ""
    m = _FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    m = _BARE_RE.search(text)
    if m:
        return m.group(0).strip()
    return ""


# ── Structural checks ──────────────────────────────────────────────────────────


def _gather_cells(root: ET.Element) -> list[ET.Element]:
    """Return every ``mxCell`` element anywhere under the model root."""
    return list(root.iter("mxCell"))


def _check_structure(xml: str) -> tuple[bool, ET.Element | None, str]:
    """Parse the XML and verify the canonical Draw.io skeleton.

    Returns ``(ok, parsed_root_element, reason)``. ``parsed_root_element``
    is the ``<root>`` mxGraphModel node when parsing succeeds.
    """
    try:
        tree = ET.fromstring(xml)
    except ET.ParseError as exc:
        return False, None, f"xml-parse-error: {exc}"

    # tree is <mxfile>. Find mxGraphModel/root regardless of nesting depth.
    if tree.tag != "mxfile":
        return False, None, f"root-tag-not-mxfile: <{tree.tag}>"

    diagram = tree.find("diagram")
    if diagram is None:
        return False, None, "missing <diagram>"

    model = diagram.find("mxGraphModel")
    if model is None:
        return False, None, "missing <mxGraphModel>"

    root_el = model.find("root")
    if root_el is None:
        return False, None, "missing <root>"

    ids = {cell.get("id") for cell in root_el.findall("mxCell")}
    if "0" not in ids or "1" not in ids:
        return False, root_el, "missing base cells id=0 / id=1"

    return True, root_el, ""


def evaluate(response_text: str, requirements: dict) -> dict:
    """Score one diagram-generation response against its task spec.

    Parameters
    ----------
    response_text : str
        Raw model output (may include prose + a fenced XML block).
    requirements : dict
        Task spec with optional keys: ``min_nodes``, ``min_edges``,
        ``required_labels`` (list[str]), ``needs_dashed_edge`` (bool).

    Returns
    -------
    dict
        ``hard`` (0/1), ``soft`` (0-1), ``checks`` (per-check booleans),
        ``fail_reasons`` (list[str]), ``extracted`` (bool), ``xml`` (str).
    """
    requirements = requirements or {}
    min_nodes = int(requirements.get("min_nodes", 0) or 0)
    min_edges = int(requirements.get("min_edges", 0) or 0)
    required_labels = [str(x) for x in (requirements.get("required_labels") or [])]
    needs_dashed = bool(requirements.get("needs_dashed_edge", False))

    checks: dict[str, bool] = {}
    reasons: list[str] = []

    xml = extract_drawio_xml(response_text)
    checks["xml_present"] = bool(xml)
    if not xml:
        reasons.append("no <mxfile> XML block found in response")
        return {
            "hard": 0,
            "soft": 0.0,
            "checks": checks,
            "fail_reasons": reasons,
            "extracted": False,
            "xml": "",
        }

    ok, root_el, struct_reason = _check_structure(xml)
    checks["well_formed_structure"] = ok
    if not ok:
        reasons.append(struct_reason)

    nodes: list[ET.Element] = []
    edges: list[ET.Element] = []
    all_ids: list[str] = []
    full_text = xml

    if ok and root_el is not None:
        cells = _gather_cells(root_el)
        all_ids = [c.get("id") for c in cells if c.get("id") is not None]
        nodes = [c for c in cells if c.get("vertex") == "1"]
        edges = [c for c in cells if c.get("edge") == "1"]

    # Check: enough nodes
    if min_nodes:
        passed = len(nodes) >= min_nodes
        checks["enough_nodes"] = passed
        if not passed:
            reasons.append(f"too few vertices: {len(nodes)} < {min_nodes}")

    # Check: enough edges
    if min_edges:
        passed = len(edges) >= min_edges
        checks["enough_edges"] = passed
        if not passed:
            reasons.append(f"too few edges: {len(edges)} < {min_edges}")

    # Check: unique ids
    if all_ids:
        passed = len(all_ids) == len(set(all_ids))
        checks["unique_ids"] = passed
        if not passed:
            reasons.append("duplicate mxCell ids present")

    # Check: edge endpoints reference existing ids
    if edges:
        id_set = set(all_ids)
        dangling = [
            e.get("id")
            for e in edges
            if (e.get("source") and e.get("source") not in id_set)
            or (e.get("target") and e.get("target") not in id_set)
        ]
        passed = not dangling
        checks["edges_connected"] = passed
        if not passed:
            reasons.append(f"edges with dangling source/target: {dangling}")

    # Check: required label keywords present (case-insensitive substring)
    if required_labels:
        haystack = full_text.lower()
        missing = [lab for lab in required_labels if lab.lower() not in haystack]
        passed = not missing
        checks["required_labels"] = passed
        if not passed:
            reasons.append(f"missing required labels: {missing}")

    # Check: dashed edge for residual / skip connections
    if needs_dashed:
        passed = "dashed=1" in full_text
        checks["dashed_edge"] = passed
        if not passed:
            reasons.append("missing dashed edge (dashed=1) for skip/residual connection")

    total = len(checks)
    passed_count = sum(1 for v in checks.values() if v)
    soft = passed_count / total if total else 0.0
    hard = 1 if passed_count == total and total > 0 else 0

    return {
        "hard": hard,
        "soft": round(soft, 4),
        "checks": checks,
        "fail_reasons": reasons,
        "extracted": True,
        "xml": xml,
        "n_nodes": len(nodes),
        "n_edges": len(edges),
    }
