# src/judging/assignment.py
"""Connectivity-aware auto-assignment planner: judges -> projects.

PURE and DB-FREE on purpose. The caller (judging.services) reads the DB and hands this module
plain dicts/lists; nothing here imports Django, queries a model, or reads the clock, so a plan is a
deterministic function of its inputs and every rule below is unit-testable without a database --
the same split normalize.engine keeps from normalize.services.

WHAT IT PRODUCES
    A list of NEW (judge_ext_id, project_ext_id) assignments to add on top of whatever already
    exists (existing edges are never returned or removed), chosen to:
      1. top every project up to a target of k reviews;
      2. spread the work -- at each step the least-loaded eligible judge picks first and takes the
         neediest project (fewest current reviews, then fewest still-eligible judges);
      3. respect eligibility -- a judge may not review their own team's project, must match the
         project's track, must not already hold it, and must not be recused from it;
      4. prefer, on a tie, an edge that joins two currently-disconnected parts of the graph;
      5. rebalance load across the *planned* edges (a free repair: a planned edge is not yet
         committed, so moving one to a lighter eligible judge costs nothing);
      6. finally add the fewest possible "bridge" reviews so the whole judge<->project graph is a
         single connected component -- the property the normalizer needs to rank every project on
         one scale (see normalize.engine.connected_components).

CONNECTIVITY
    The union-find below is kept LOCAL and matches engine.connected_components' semantics exactly
    (bipartite ("J", id) / ("S", id) nodes, path-compressed find, attach-root union), so a plan this
    module reports as one component is one component under the engine's own counter.

DETERMINISM
    Every choice is broken by a total key ending in the unique ext_id, so the output is a pure
    function of (judges, projects, k, seed) with no reliance on dict/set iteration order. `seed`
    participates as a stable salt in those keys (hashed, never Python's per-process hash()), so a
    given seed reproduces exactly and a different seed only reshuffles genuine ties.
"""
from __future__ import annotations

import hashlib

__all__ = ["plan_assignments", "summarize_plan"]


def _salt(seed, *parts):
    """A deterministic, process-independent tie-break salt. Python's built-in hash() is randomized
    per process (PYTHONHASHSEED), which would make ordering vary run-to-run; sha256 does not."""
    raw = ("%r|" % (seed,)) + "|".join(str(p) for p in parts)
    return int.from_bytes(hashlib.sha256(raw.encode("utf-8")).digest()[:8], "big")


class _UnionFind:
    """Bipartite judge<->project union-find, semantics-identical to engine.connected_components:
    nodes are ("J", judge_ext) / ("S", project_ext), find path-compresses, union attaches one root
    under the other. A node exists only once an edge introduces it, so n_components counts exactly
    the edge-bearing nodes the engine would count."""

    def __init__(self):
        self.parent = {}

    def _add(self, x):
        if x not in self.parent:
            self.parent[x] = x

    def present(self, x):
        return x in self.parent

    def find(self, x):
        self._add(x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:                 # path compression, as in the engine
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb

    def n_components(self):
        return len({self.find(x) for x in list(self.parent)})


def _as_str_set(value):
    return {str(v) for v in value} if value else set()


def _norm_judge(j):
    """Coerce one judge input into a mutable planning record with concrete string sets."""
    teams = j.get("teams")
    if teams is None and j.get("team") is not None:
        teams = [j["team"]]
    return {
        "ext_id": str(j["ext_id"]),
        "tracks": _as_str_set(j.get("tracks")),
        "teams": _as_str_set(teams),
        "assigned": _as_str_set(j.get("assigned")),
        "recused": _as_str_set(j.get("recused")),
        "load": int(j.get("load", 0) or 0),
    }


def _norm_project(p):
    return {
        "ext_id": str(p["ext_id"]),
        "track": None if p.get("track") is None else str(p["track"]),
        "team": None if p.get("team") is None else str(p["team"]),
        "reviews": int(p.get("reviews", 0) or 0),
        "assigned_judges": _as_str_set(p.get("assigned_judges")),
    }


def _build_state(judges, projects):
    """Normalize inputs and seed the running maps + union-find from the EXISTING edges.

    The existing edge set is the union of each judge's `assigned` and each project's
    `assigned_judges`, so a caller may populate either side and the graph comes out the same."""
    J = [_norm_judge(j) for j in judges]
    P = [_norm_project(p) for p in projects]
    pix = {p["ext_id"]: p for p in P}
    known_j = {j["ext_id"] for j in J}
    held = {j["ext_id"]: set(j["assigned"]) for j in J}            # judge_ext -> {project_ext}
    reviewers = {p["ext_id"]: set(p["assigned_judges"]) for p in P}  # project_ext -> {judge_ext}
    # reconcile the two views so an edge named on only one side still exists on both
    for j in J:
        for pext in j["assigned"]:
            if pext in reviewers:
                reviewers[pext].add(j["ext_id"])
    for p in P:
        for jext in p["assigned_judges"]:
            if jext in held:
                held[jext].add(p["ext_id"])
    load = {j["ext_id"]: j["load"] for j in J}
    reviews = {p["ext_id"]: p["reviews"] for p in P}
    uf = _UnionFind()
    for jext in known_j:
        for pext in held[jext]:
            if pext in pix:                            # only edges over known projects count
                uf.union(("J", jext), ("S", pext))
    return J, P, pix, held, reviewers, load, reviews, uf


def _pair_eligible(judge, project, held):
    """A judge may take a NEW review of a project iff: not on the project's team (conflict of
    interest), track-matched, not already holding it, and not recused from it."""
    pext = project["ext_id"]
    if pext in held[judge["ext_id"]]:
        return False
    if project["track"] not in judge["tracks"]:
        return False
    if project["team"] is not None and project["team"] in judge["teams"]:
        return False
    if pext in judge["recused"]:
        return False
    return True


def _connects(uf, jext, pext):
    """True if adding (jext, pext) would join two different (or not-yet-present) graph parts."""
    jk, pk = ("J", jext), ("S", pext)
    if not uf.present(jk) or not uf.present(pk):
        return True                                    # attaching an isolated node reduces fragmentation
    return uf.find(jk) != uf.find(pk)


def plan_assignments(judges, projects, k, *, seed=0):
    """Return the new (judge_ext_id, project_ext_id) assignments to add. Deterministic; see the
    module docstring for the six goals. Existing assignments are never returned or removed."""
    k = int(k)
    J, P, pix, held, reviewers, load, reviews, uf = _build_state(judges, projects)
    planned = []                                       # decision-ordered list of new edges

    def assign(jext, pext):
        planned.append((jext, pext))
        held[jext].add(pext)
        reviewers[pext].add(jext)
        load[jext] += 1
        reviews[pext] += 1
        uf.union(("J", jext), ("S", pext))

    def remaining_eligible(project):
        return sum(1 for j in J if _pair_eligible(j, project, held))

    # (1)+(2)+(4) greedy k-coverage: the least-loaded eligible judge takes the neediest project.
    if k > 0:
        while True:
            needy = [p for p in P if reviews[p["ext_id"]] < k]
            if not needy:
                break
            judge, jkey = None, None
            for j in J:
                if not any(_pair_eligible(j, p, held) for p in needy):
                    continue
                key = (load[j["ext_id"]], _salt(seed, "J", j["ext_id"]), j["ext_id"])
                if jkey is None or key < jkey:
                    jkey, judge = key, j
            if judge is None:
                break                                  # no eligible judge for any still-needy project
            cands = [p for p in needy if _pair_eligible(judge, p, held)]
            pick = min(cands, key=lambda p: (
                reviews[p["ext_id"]],                                   # neediest first
                remaining_eligible(p),                                 # then scarcest coverage
                0 if _connects(uf, judge["ext_id"], p["ext_id"]) else 1,  # then a connecting edge
                _salt(seed, "P", judge["ext_id"], p["ext_id"]),
                p["ext_id"]))
            assign(judge["ext_id"], pick["ext_id"])

    # (5) load-rebalance repair over the PLANNED edges, then (6) the minimal bridge pass.
    _rebalance(J, pix, planned, held, reviewers, load, seed)
    _bridge(J, P, held, reviewers, load, reviews, planned, seed)
    return [tuple(e) for e in planned]


def _rebalance(J, pix, planned, held, reviewers, load, seed):
    """Move a PLANNED review from a heavier judge to a strictly-lighter eligible one, one swap at a
    time, most-imbalanced first. Coverage is preserved (a project keeps its reviewer count -- one
    reviewer is exchanged for another), and sum(load^2) strictly falls on every swap (gap >= 2), so
    this always terminates. Connectivity is (re)established afterwards by the bridge pass."""
    cap = (len(planned) + 1) * (len(J) + 1)
    for _ in range(cap):
        best = None                                    # (key, idx, donor_ext, project_ext, receiver_ext)
        for idx, (jext, pext) in enumerate(planned):
            project = pix[pext]
            for j2 in J:
                j2ext = j2["ext_id"]
                if j2ext == jext or j2ext in reviewers[pext]:
                    continue                           # self, or already a reviewer of this project
                if not _pair_eligible(j2, project, held):
                    continue
                gap = load[jext] - load[j2ext]
                if gap < 2:
                    continue
                key = (-gap, _salt(seed, "R", jext, pext, j2ext), idx)
                if best is None or key < best[0]:
                    best = (key, idx, jext, pext, j2ext)
        if best is None:
            break
        _, idx, jext, pext, j2ext = best
        planned[idx] = (j2ext, pext)
        held[jext].discard(pext)
        held[j2ext].add(pext)
        reviewers[pext].discard(jext)
        reviewers[pext].add(j2ext)
        load[jext] -= 1
        load[j2ext] += 1


def _bridge(J, P, held, reviewers, load, reviews, planned, seed):
    """Add few cross-component reviews until the judge<->project graph (over edge-bearing nodes) is a
    single component -- the property the normalizer needs to rank every project on one scale.

    Greedy and deterministic: on each pass prefer a MERGE edge whose judge is already in the graph
    and whose project sits in a different component (this drops the component count by one); if none
    exists, EXTEND by attaching an otherwise-isolated judge that is eligible across two or more
    components, which unlocks a merge on the next pass (this is how a multi-track judge stitches two
    single-track islands together). Terminates: each merge removes a component and each judge is
    extended at most once. Stops early if no eligible edge can join the remaining components (e.g.
    genuinely disjoint tracks with no shared-eligibility judge)."""
    known_p = {p["ext_id"] for p in P}

    def add(jext, pext):
        planned.append((jext, pext))
        held[jext].add(pext)
        reviewers[pext].add(jext)
        load[jext] += 1
        reviews[pext] += 1

    for _ in range(2 * (len(J) + len(P)) + 8):
        uf = _UnionFind()                          # rebuilt each pass: rebalance may have moved edges
        for j in J:
            for pext in held[j["ext_id"]]:
                if pext in known_p:
                    uf.union(("J", j["ext_id"]), ("S", pext))
        if uf.n_components() <= 1:
            break
        merge = extend = None
        for j in J:
            jext = j["ext_id"]
            elig = [p for p in P
                    if uf.present(("S", p["ext_id"])) and _pair_eligible(j, p, held)]
            if not elig:
                continue
            if uf.present(("J", jext)):
                rj = uf.find(("J", jext))
                for p in elig:
                    if uf.find(("S", p["ext_id"])) == rj:
                        continue                   # same component already: not a merge
                    pext = p["ext_id"]
                    key = (load[jext], reviews[pext], _salt(seed, "M", jext, pext), jext, pext)
                    if merge is None or key < merge[0]:
                        merge = (key, jext, pext)
            else:
                comps = {uf.find(("S", p["ext_id"])) for p in elig}
                if len(comps) >= 2:                # an isolated judge that can span >=2 components
                    p = min(elig, key=lambda p: (reviews[p["ext_id"]],
                                                 _salt(seed, "X", jext, p["ext_id"]), p["ext_id"]))
                    pext = p["ext_id"]
                    key = (load[jext], -len(comps), _salt(seed, "X", jext, pext), jext, pext)
                    if extend is None or key < extend[0]:
                        extend = (key, jext, pext)
        choice = merge or extend               # a real merge is always preferred over an extend
        if choice is None:
            break
        add(choice[1], choice[2])


def summarize_plan(judges, projects, plan, k, *, seed=0):
    """A DB-free preview of applying `plan`: per-project coverage before/after, per-judge load
    before/after, and the judge<->project component count before/after (a single component means the
    field is fully connected). `seed` is accepted so callers can forward the same kwargs as
    plan_assignments; the summary itself does not depend on it."""
    k = int(k)
    J, P, _pix, _held, _reviewers, load, reviews, uf = _build_state(judges, projects)
    components_before = uf.n_components()
    added = {p["ext_id"]: 0 for p in P}
    load_added = {j["ext_id"]: 0 for j in J}
    for jext, pext in plan:
        if pext in added:
            added[pext] += 1
        if jext in load_added:
            load_added[jext] += 1
        uf.union(("J", jext), ("S", pext))             # extend the graph to measure connectivity
    components_after = uf.n_components()

    project_rows = []
    for p in P:
        pext = p["ext_id"]
        before, add = reviews[pext], added[pext]
        project_rows.append({
            "ext_id": pext, "track": p["track"], "team": p["team"],
            "before": before, "added": add, "after": before + add,
            "meets_k": before + add >= k})
    judge_rows = []
    for j in J:
        jext = j["ext_id"]
        before, add = load[jext], load_added[jext]
        judge_rows.append({"ext_id": jext, "before": before, "added": add, "after": before + add})

    covered_before = sum(1 for r in project_rows if r["before"] >= k)
    covered_after = sum(1 for r in project_rows if r["after"] >= k)
    return {
        "k": k,
        "n_new": len(plan),
        "n_projects": len(P),
        "n_judges": len(J),
        "projects": project_rows,
        "judges": judge_rows,
        "covered_before": covered_before,
        "covered_after": covered_after,
        "uncovered_after": len(P) - covered_after,
        "components_before": components_before,
        "components_after": components_after,
        "connected": components_after == 1,
        "max_load_after": max((r["after"] for r in judge_rows), default=0),
        "min_load_after": min((r["after"] for r in judge_rows), default=0),
    }

