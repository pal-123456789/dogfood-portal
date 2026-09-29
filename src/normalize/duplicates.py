# src/normalize/duplicates.py
"""Within-track duplicate-submission detection - pure Python, zero Django (a sibling of
diagnostics.py / engine.py).

WHAT THIS IS
    A DISPLAY-ONLY diagnostic for the organizer review panel: it surfaces clusters of submissions
    that live in the SAME track and share the same normalized title - a likely duplicate or
    near-duplicate re-submission (the released fixture plants exactly one such pair). It groups on
    (track, normalized title) and reports every group of two or more, sorted deterministically.

WHAT THIS IS NOT
    It is NOT a ranking input, a fraud score, or a gate. It reads nothing but the submission rows
    handed to it, changes no state, and is never fed back into the signed result, the leaderboard,
    or any hash. Sharing a title is a prompt for a human to look, not a verdict - two teams can
    independently pick the same name, and one team may legitimately supersede its own entry.

Title normalization is fixed here: " ".join(str(title).split()).casefold() - collapse all runs of
whitespace to single spaces, strip the ends, and casefold - so "Dry  Harbour", " dry harbour " and
"DRY HARBOUR" all match. Grouping is by (track, normalized title); a different track never matches.
"""
from __future__ import annotations


def normalize_title(title):
    """The single, fixed title-normalization rule: collapse internal whitespace, strip, casefold.

    Pulled out so the detector and any caller normalize identically. Pure.
    """
    return " ".join(str(title).split()).casefold()


def find_duplicate_clusters(rows):
    """Group submissions that fall in the SAME track and share a normalized title.

    rows: iterable of dicts {ext_id, team, track, title}. Two submissions cluster when they share
    both a track and a normalized title (normalize_title above). A cluster is any such group with
    >= 2 submissions; singletons are dropped.

    Returns a list of dicts, one per cluster, deterministically ordered by (track, title):
        {
          "track":         track ext_id,
          "title":         the normalized title (also the group + sort key),
          "display_title": a human-readable title (the first raw title seen in the group),
          "submissions":   [ext_id, ...]  sorted by ext_id,
          "teams":         [team,   ...]  parallel to `submissions` (the team of each submission),
          "entries":       [{"ext_id", "team"}, ...] the same pairing, for template rows,
        }
    Pure: no I/O, no mutation of the input, deterministic for a given input.
    """
    groups = {}
    for row in rows:
        track = str(row["track"])
        raw_title = str(row["title"])
        norm = normalize_title(raw_title)
        key = (track, norm)
        group = groups.get(key)
        if group is None:
            # First raw title seen for this (track, normalized-title) becomes the display title.
            group = {"track": track, "title": norm, "display_title": raw_title, "pairs": []}
            groups[key] = group
        group["pairs"].append((str(row["ext_id"]), str(row["team"])))

    clusters = []
    for group in groups.values():
        if len(group["pairs"]) < 2:
            continue                                  # a lone submission is not a duplicate
        pairs = sorted(group["pairs"])                # deterministic: by (ext_id, team)
        clusters.append({
            "track": group["track"],
            "title": group["title"],
            "display_title": group["display_title"],
            "submissions": [ext_id for ext_id, _team in pairs],
            "teams": [team for _ext_id, team in pairs],
            "entries": [{"ext_id": ext_id, "team": team} for ext_id, team in pairs],
        })
    clusters.sort(key=lambda c: (c["track"], c["title"]))
    return clusters
