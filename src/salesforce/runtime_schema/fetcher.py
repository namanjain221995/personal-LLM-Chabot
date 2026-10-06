"""Where schema comes from.

Two sources behind one interface. The mirror works offline and needs no org
credentials, at the cost of 15 columns Salesforce only computes at runtime. A
live describe fills those. Both produce the same SchemaBundle, so the rest of
the subsystem never learns which one ran -- only each row's `source` records it.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Iterable

from .models import RecordType, SchemaBundle
from .normalizer import (NS, flag, generate_aliases, invert_to_child_relationships,
                         name_field_info, platform_fields,
                         normalize_field, normalize_object, normalize_picklist,
                         normalize_relationship, text)

log = logging.getLogger(__name__)


class FetchError(RuntimeError):
    """Raised when a source cannot be read at all."""


class SchemaFetcher(ABC):
    """Produces a SchemaBundle from somewhere."""

    source_name: str = "unknown"

    @abstractmethod
    def fetch(self, only: Iterable[str] | None = None) -> SchemaBundle:
        """Every object, or just the named ones."""


class MirrorFetcher(SchemaFetcher):
    """Reads the Salesforce DX metadata mirror on disk.

    Complete for everything the metadata format carries: names, labels, types,
    relationships, picklists, descriptions, record-type identity. Silent on the
    runtime properties -- key prefixes, CRUD and queryable/filterable/sortable
    flags, record-type ids -- which stay None rather than defaulting to False,
    because "not knowable from here" is a different claim from "no".
    """

    source_name = "mirror"

    def __init__(self, mirror_path: str | Path,
                 business_knowledge: str | Path | None = None) -> None:
        self.root = Path(mirror_path)
        self.business_knowledge = business_knowledge
        self.business_report = None
        if not (self.root / "objects").is_dir():
            raise FetchError(
                f"{self.root}: no objects/ directory. Expected a DX default/ folder.")

    def _parse(self, path: Path) -> ET.Element | None:
        try:
            return ET.parse(path).getroot()
        except (ET.ParseError, OSError, UnicodeError) as exc:
            log.warning("could not parse %s: %s", path, exc)
            return None

    def _global_value_sets(self) -> dict[str, list[dict[str, Any]]]:
        """Read globalValueSets once; fields reference them by name."""
        out: dict[str, list[dict[str, Any]]] = {}
        directory = self.root / "globalValueSets"
        if not directory.is_dir():
            return out
        for path in sorted(directory.glob("*.globalValueSet-meta.xml")):
            root = self._parse(path)
            if root is None:
                continue
            name = path.name[: -len(".globalValueSet-meta.xml")]
            values = []
            for order, value in enumerate(root.findall(f"{NS}customValue")):
                full_name = text(value.find(f"{NS}fullName"))
                if full_name:
                    values.append({"value": full_name,
                                   "label": text(value.find(f"{NS}label")),
                                   "is_default": flag(value.find(f"{NS}default")),
                                   "is_active": text(value.find(f"{NS}isActive")) != "false",
                                   "sort_order": order})
            out[name] = values
        return out

    def fetch(self, only: Iterable[str] | None = None) -> SchemaBundle:
        bundle = SchemaBundle()
        wanted = set(only) if only else None
        global_sets = self._global_value_sets()
        objects_dir = self.root / "objects"

        for directory in sorted(objects_dir.iterdir()):
            if not directory.is_dir():
                continue
            api_name = directory.name
            if wanted is not None and api_name not in wanted:
                continue
            try:
                self._fetch_object(directory, api_name, global_sets, bundle)
            except Exception as exc:                      # one bad object
                # must not lose the other 418.
                bundle.errors.append(f"{api_name}: {exc}")
                log.warning("describe failed for %s: %s", api_name, exc)

        bundle.child_relationships = invert_to_child_relationships(bundle.relationships)
        # Only on a full fetch: a partial refresh of three objects cannot tell
        # a stale term from one that points at an object it did not read.
        if wanted is None:
            from .business_knowledge import load_business_aliases
            self.business_report = load_business_aliases(self.business_knowledge,
                                                         bundle)
        return bundle

    def _fetch_object(self, directory: Path, api_name: str,
                      global_sets: dict[str, list[dict[str, Any]]],
                      bundle: SchemaBundle) -> None:
        meta = directory / f"{api_name}.object-meta.xml"
        root = self._parse(meta) if meta.is_file() else None
        record_types_dir = directory / "recordTypes"
        has_record_types = record_types_dir.is_dir()

        obj, name_field = normalize_object(root, api_name,
                                           has_record_types=has_record_types)

        fields = []
        fields_dir = directory / "fields"
        if fields_dir.is_dir():
            for path in sorted(fields_dir.glob("*.field-meta.xml")):
                field_root = self._parse(path)
                if field_root is None:
                    continue
                field_name = path.name[: -len(".field-meta.xml")]
                field = normalize_field(field_root, api_name, field_name, name_field)
                fields.append(field)
                bundle.picklist_values.extend(
                    normalize_picklist(field_root, api_name, field_name, global_sets))
                bundle.relationships.extend(normalize_relationship(field))

        name_label, name_type = name_field_info(root)
        fields.extend(platform_fields(obj, {f.api_name for f in fields},
                                      name_field, name_label, name_type))

        obj.field_count = len(fields)
        obj.relationship_count = sum(1 for f in fields if f.reference_to
                                     and f.source != "platform_standard")
        bundle.objects.append(obj)
        bundle.fields.extend(fields)

        if has_record_types:
            for path in sorted(record_types_dir.glob("*.recordType-meta.xml")):
                rt_root = self._parse(path)
                if rt_root is None:
                    continue
                developer_name = path.name[: -len(".recordType-meta.xml")]
                bundle.record_types.append(RecordType(
                    object_api_name=api_name,
                    developer_name=developer_name,
                    name=text(rt_root.find(f"{NS}label")),
                    description=text(rt_root.find(f"{NS}description")),
                    # record_type_id stays None: it is a runtime id, and the
                    # metadata format has no place for it.
                    is_active=text(rt_root.find(f"{NS}active")) != "false"))

        object_aliases, field_aliases = generate_aliases(obj, fields)
        bundle.object_aliases.extend(object_aliases)
        bundle.field_aliases.extend(field_aliases)


class DescribeFetcher(SchemaFetcher):
    """Reads the live org through the existing SalesforceClient.

    Not wired up yet. It exists so the 15 runtime-only columns have a defined
    way to be filled, and so nothing downstream has to change when they are.
    Reuses the repository's own client rather than opening a second auth stack.
    """

    source_name = "describe"

    def __init__(self, client: Any) -> None:
        self.client = client

    def fetch(self, only: Iterable[str] | None = None) -> SchemaBundle:
        raise NotImplementedError(
            "DescribeFetcher needs org credentials and is not part of the "
            "mirror-sourced build. Its purpose is to fill the columns the "
            "mirror leaves NULL: key_prefix, the CRUD and "
            "queryable/filterable/sortable flags, and record_type_id.")


def build_fetcher(kind: str, **kwargs: Any) -> SchemaFetcher:
    if kind == "mirror":
        return MirrorFetcher(kwargs["mirror_path"],
                             kwargs.get("business_knowledge"))
    if kind == "describe":
        return DescribeFetcher(kwargs["client"])
    raise FetchError(f"unknown fetcher {kind!r}; expected 'mirror' or 'describe'")
