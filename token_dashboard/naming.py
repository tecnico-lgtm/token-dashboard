"""Project display names from Claude Code's slug + cwd."""
from __future__ import annotations

import re
from typing import Optional


def _encode_slug(path: str) -> str:
    """Claude Code's project-slug encoding: each of `:`, `\\`, `/`, space → one `-`."""
    return re.sub(r"[:\\/ ]", "-", path)


def _root_path(cwd: str, slug: str) -> Optional[str]:
    """If any ancestor of cwd encodes to slug, return that ancestor's full path."""
    if not cwd or not slug:
        return None
    trimmed = cwd.rstrip("/\\")
    sep = "\\" if "\\" in trimmed else "/"
    parts = trimmed.split(sep)
    for i in range(len(parts), 0, -1):
        if _encode_slug(sep.join(parts[:i])) == slug and parts[i - 1]:
            return sep.join(parts[:i])
    return None


def _walk_to_root(cwd: str, slug: str) -> Optional[str]:
    """If any ancestor of cwd encodes to slug, return that ancestor's basename."""
    root = _root_path(cwd, slug)
    return re.split(r"[\\/]", root)[-1] if root else None


def project_name_for(cwd: Optional[str], fallback_slug: str) -> str:
    """Pretty project name from a single cwd + slug (best-effort).

    For the multi-cwd case, prefer `best_project_name`.
    """
    name = _walk_to_root(cwd or "", fallback_slug or "")
    if name:
        return name
    if cwd:
        trimmed = cwd.rstrip("/\\")
        sep = "\\" if "\\" in trimmed else "/"
        tail = trimmed.split(sep)[-1]
        if tail:
            return tail
    if fallback_slug:
        parts = [p for p in re.split(r"-+", fallback_slug) if p]
        if parts:
            return parts[-1]
    return fallback_slug or ""


def best_project_name(cwds, slug: str) -> str:
    """Pick a pretty name from a list of cwds.

    Prefer a cwd whose walk-up matches `slug` (a true descendant of the project
    root). If none match, fall back to `project_name_for` on the first cwd,
    then to the slug's last segment.
    """
    cwds = [c for c in (cwds or []) if c]
    for cwd in cwds:
        name = _walk_to_root(cwd, slug)
        if name:
            return name
    return project_name_for(cwds[0] if cwds else None, slug)


def project_names(conn) -> dict:
    """{slug: display name} for every project, with clashing names disambiguated.

    Two folders can share a basename (``Desktop\\app`` and ``work\\app``); those
    get their parent folder prepended, and the full path if that still clashes.
    """
    cwds_by_slug = {}
    for row in conn.execute("SELECT project_slug, cwd FROM messages GROUP BY project_slug, cwd"):
        cwds = cwds_by_slug.setdefault(row["project_slug"], [])
        if row["cwd"]:
            cwds.append(row["cwd"])
    names, roots = {}, {}
    for slug, cwds in cwds_by_slug.items():
        names[slug] = best_project_name(cwds, slug)
        roots[slug] = next((r for r in (_root_path(c, slug) for c in cwds) if r), None) \
            or (cwds[0].rstrip("/\\") if cwds else None)
    for depth in (2, None):
        seen = {}
        for slug, name in names.items():
            seen.setdefault(name.lower(), []).append(slug)
        clashes = [s for group in seen.values() if len(group) > 1 for s in group]
        if not clashes:
            break
        for slug in clashes:
            root = roots[slug]
            if not root:
                names[slug] = slug
                continue
            sep = "\\" if "\\" in root else "/"
            parts = [p for p in root.split(sep) if p]
            names[slug] = sep.join(parts[-depth:]) if depth else root
    return names
