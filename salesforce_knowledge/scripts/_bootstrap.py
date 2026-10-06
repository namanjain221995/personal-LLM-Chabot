"""Put the repository's src/ on the path so the scripts can be run directly."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
KNOWLEDGE_ROOT = ROOT / "salesforce_knowledge"
