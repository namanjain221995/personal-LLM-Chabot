from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from .models import Evidence
from .scanner import SourceFile


@dataclass(frozen=True, slots=True)
class Reference:
    name: str
    kind: str
    evidence: Evidence


_APEX_OBJECT = re.compile(
    r"\b(Account|Contact|Lead|Case|Opportunity|User|[A-Z][A-Za-z0-9]*__c)\b"
)
_APEX_FIELD = re.compile(r"\b([A-Z][A-Za-z0-9_]*(?:__c|Id|Name|Email|Phone|Status|Type|Date))\b")


def _line(text: str, offset: int) -> tuple[int, str]:
    line_no = text.count("\n", 0, offset) + 1
    snippet = text.splitlines()[line_no - 1].strip()[:240]
    return line_no, snippet


def parse_apex(source: SourceFile) -> tuple[Reference, ...]:
    if source.text is None:
        return ()
    refs: list[Reference] = []
    for pattern, kind in ((_APEX_OBJECT, "object"), (_APEX_FIELD, "field")):
        for match in pattern.finditer(source.text):
            line, snippet = _line(source.text, match.start(1))
            refs.append(Reference(match.group(1), kind, Evidence(source.relative_path, line, snippet)))
    return tuple(refs)


def parse_xml_references(source: SourceFile) -> tuple[Reference, ...]:
    if source.text is None or source.path.suffix.lower() != ".xml":
        return ()
    try:
        root = ET.fromstring(source.text)
    except ET.ParseError as exc:
        raise ValueError(f"invalid XML in {source.relative_path}: {exc}") from exc
    refs: list[Reference] = []
    for element in root.iter():
        if element.text and element.text.strip():
            value = element.text.strip()
            if len(value) <= 200 and (element.tag.lower().endswith(("name", "reference", "member", "type", "object")) or "." in value):
                offset = source.text.find(value)
                line, snippet = _line(source.text, max(offset, 0))
                refs.append(Reference(value, "xml_reference", Evidence(source.relative_path, line, snippet)))
    return tuple(refs)
