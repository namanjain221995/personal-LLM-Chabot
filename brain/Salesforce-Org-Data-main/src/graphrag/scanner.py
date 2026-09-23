from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class ScanError(ValueError):
    """Raised when a source directory cannot be scanned."""


@dataclass(frozen=True, slots=True)
class SourceFile:
    path: Path
    relative_path: str
    component: str
    kind: str
    text: str | None


_KINDS = {
    "classes": "apex_class", "triggers": "apex_trigger", "objects": "custom_object",
    "aura": "aura_bundle", "lwc": "lwc_bundle", "flows": "flow",
    "permissionsets": "permission_set", "profiles": "profile", "layouts": "layout",
    "tabs": "custom_tab", "labels": "custom_labels", "customMetadata": "custom_metadata",
    "experiences": "experience", "email": "email_template", "pages": "visualforce_page",
    "components": "visualforce_component", "staticresources": "static_resource",
    "roles": "role", "queues": "queue", "flexipages": "lightning_page",
    "reports": "report", "dashboards": "dashboard", "applications": "app",
    "permissionSetGroups": "permission_set_group",
}
_TEXT_EXTENSIONS = {".xml", ".cls", ".trigger", ".cmp", ".app", ".design", ".evt",
                    ".page", ".component", ".js", ".html", ".css", ".json", ".flow",
                    ".site", ".labels", ".object", ".permissionset"}


def classify_path(relative_path: str) -> tuple[str, str]:
    parts = Path(relative_path).parts
    component = parts[0] if parts else "unknown"
    return component, _KINDS.get(component, "salesforce_metadata")


def scan(source: str | Path) -> tuple[SourceFile, ...]:
    root = Path(source)
    if not root.exists() or not root.is_dir():
        raise ScanError(f"source directory does not exist: {root}")
    files: list[SourceFile] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        component, kind = classify_path(relative)
        text = None
        if path.suffix in _TEXT_EXTENSIONS:
            try:
                text = path.read_text(encoding="utf-8", errors="strict")
            except UnicodeDecodeError:
                # Some exported assets have a text-like suffix but are binary or
                # encoded outside UTF-8; they remain represented as file nodes.
                text = None
        files.append(SourceFile(path, relative, component, kind, text))
    return tuple(files)
