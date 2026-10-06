"""Lexical schema discovery over SQLite FTS5.

Ranking is a ladder, not a single score: an exact API-name match is a fact, an
FTS hit is a guess, and mixing them on one scale lets a fuzzy match outrank a
certainty. Each rung is tried and its results are placed before the next.

    exact API name -> exact label -> alias -> FTS

Vector retrieval is deliberately absent. `search_objects` and `search_fields`
are the seam it would slot behind.
"""
from __future__ import annotations

import json
import re
from typing import Any, Sequence

from .database import Database
from .repository import SchemaRepository

# Rung scores. Gapped widely so no number of FTS hits can outrank an exact name.
SCORE_EXACT_API = 1.0
SCORE_EXACT_LABEL = 0.95
SCORE_ALIAS = 0.85
SCORE_FTS_BASE = 0.6

_FTS_UNSAFE = re.compile(r'["\'()*:^-]')


def _fts_query(text: str) -> str:
    """A safe FTS5 MATCH expression.

    User text reaches this directly, and FTS5 treats quotes, parentheses and
    `*` as syntax -- an apostrophe alone raises rather than returning nothing.
    Every token is quoted, so the query is matched as words, never as operators.
    """
    tokens = [t for t in _FTS_UNSAFE.sub(" ", text).split() if t]
    return " OR ".join(f'"{t}"' for t in tokens)


class SchemaSearch:
    def __init__(self, database: Database, repository: SchemaRepository) -> None:
        self.db = database
        self.repo = repository

    @property
    def _c(self):
        return self.db.connection

    # -- index building ---------------------------------------------------
    def rebuild(self, only: Sequence[str] | None = None) -> dict[str, int]:
        """Rebuild search documents. Whole index, or just the named objects."""
        if only:
            placeholders = ",".join("?" * len(only))
            self._c.execute(
                f"DELETE FROM object_search WHERE api_name IN ({placeholders})", only)
            self._c.execute(
                f"DELETE FROM field_search WHERE object_api_name IN ({placeholders})",
                only)
            names = list(only)
        else:
            self._c.execute("DELETE FROM object_search")
            self._c.execute("DELETE FROM field_search")
            names = self.repo.object_names()

        objects = 0
        fields = 0
        for api_name in names:
            obj = self.repo.get_object(api_name)
            if obj is None:
                continue
            aliases = self.repo.get_object_aliases(api_name)
            related = [r["target_object"] for r in self.repo.get_relationships(api_name)]
            search_text = " ".join(filter(None, [
                obj.get("label"), obj.get("plural_label"), api_name,
                obj.get("description"), " ".join(aliases), " ".join(sorted(set(related)))]))
            self._c.execute(
                "INSERT INTO object_search (api_name, label, description,"
                " aliases, related_objects, search_text) VALUES (?,?,?,?,?,?)",
                (api_name, obj.get("label") or "", obj.get("description") or "",
                 " ".join(aliases), " ".join(sorted(set(related))), search_text))
            objects += 1

            for field in self.repo.get_fields(api_name):
                field_aliases = self.repo.get_field_aliases(api_name, field["api_name"])
                values = [v["value"] for v in self.repo.get_picklist_values(
                    api_name, field["api_name"])]
                field_text = " ".join(filter(None, [
                    field.get("label"), field["api_name"], field.get("description"),
                    field.get("inline_help_text"), " ".join(field_aliases),
                    " ".join(values)]))
                self._c.execute(
                    "INSERT INTO field_search (object_api_name, api_name, label,"
                    " description, aliases, data_type, picklist_values,"
                    " search_text) VALUES (?,?,?,?,?,?,?,?)",
                    (api_name, field["api_name"], field.get("label") or "",
                     field.get("description") or "", " ".join(field_aliases),
                     field.get("data_type") or "", " ".join(values), field_text))
                fields += 1
        return {"objects": objects, "fields": fields}

    # -- querying ---------------------------------------------------------
    def search_objects(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        text = (query or "").strip()
        if not text:
            return []
        found: dict[str, dict[str, Any]] = {}

        def add(api_name: str, score: float, how: str) -> None:
            if api_name not in found:
                found[api_name] = {"api_name": api_name, "score": round(score, 4),
                                   "matched_by": how}

        for row in self._c.execute(
                "SELECT api_name FROM objects WHERE api_name = ? COLLATE NOCASE",
                (text,)):
            add(row[0], SCORE_EXACT_API, "exact_api_name")
        for row in self._c.execute(
                "SELECT api_name FROM objects WHERE label = ? COLLATE NOCASE"
                " OR plural_label = ? COLLATE NOCASE", (text, text)):
            add(row[0], SCORE_EXACT_LABEL, "exact_label")
        for row in self._c.execute(
                "SELECT object_api_name, confidence FROM object_aliases"
                " WHERE alias = ? COLLATE NOCASE ORDER BY confidence DESC", (text,)):
            add(row[0], SCORE_ALIAS * float(row[1] or 1.0), "alias")

        expression = _fts_query(text)
        if expression and len(found) < limit:
            for position, row in enumerate(self._c.execute(
                    "SELECT api_name FROM object_search WHERE object_search"
                    " MATCH ? ORDER BY rank LIMIT ?", (expression, limit * 3))):
                add(row[0], SCORE_FTS_BASE - position * 0.005, "fts")

        ranked = sorted(found.values(), key=lambda r: (-r["score"], r["api_name"]))
        catalog = {c["api_name"]: c for c in self.repo.object_catalog()}
        out = []
        for item in ranked[:limit]:
            entry = catalog.get(item["api_name"])
            if entry:
                out.append({**entry, "score": item["score"],
                            "matched_by": item["matched_by"]})
        return out

    def search_fields(self, query: str, object_api_name: str | None = None,
                      limit: int = 20) -> list[dict[str, Any]]:
        text = (query or "").strip()
        if not text:
            return []
        found: dict[tuple[str, str], dict[str, Any]] = {}

        def add(obj: str, field: str, score: float, how: str) -> None:
            if object_api_name and obj != object_api_name:
                return
            key = (obj, field)
            if key not in found:
                found[key] = {"object_api_name": obj, "api_name": field,
                              "score": round(score, 4), "matched_by": how}

        for row in self._c.execute(
                "SELECT object_api_name, api_name FROM fields"
                " WHERE api_name = ? COLLATE NOCASE", (text,)):
            add(row[0], row[1], SCORE_EXACT_API, "exact_api_name")
        for row in self._c.execute(
                "SELECT object_api_name, api_name FROM fields"
                " WHERE label = ? COLLATE NOCASE", (text,)):
            add(row[0], row[1], SCORE_EXACT_LABEL, "exact_label")
        for row in self._c.execute(
                "SELECT object_api_name, field_api_name, confidence"
                " FROM field_aliases WHERE alias = ? COLLATE NOCASE"
                " ORDER BY confidence DESC", (text,)):
            add(row[0], row[1], SCORE_ALIAS * float(row[2] or 1.0), "alias")

        expression = _fts_query(text)
        if expression and len(found) < limit:
            sql = ("SELECT object_api_name, api_name FROM field_search"
                   " WHERE field_search MATCH ?")
            params: list[Any] = [expression]
            if object_api_name:
                sql += " AND object_api_name = ?"
                params.append(object_api_name)
            sql += " ORDER BY rank LIMIT ?"
            params.append(limit * 3)
            for position, row in enumerate(self._c.execute(sql, params)):
                add(row[0], row[1], SCORE_FTS_BASE - position * 0.005, "fts")

        ranked = sorted(found.values(),
                        key=lambda r: (-r["score"], r["object_api_name"], r["api_name"]))
        out = []
        for item in ranked[:limit]:
            field = self.repo.get_field(item["object_api_name"], item["api_name"])
            if field:
                out.append({**field, "score": item["score"],
                            "matched_by": item["matched_by"]})
        return out
