import os
import sys

# Make the syncworker package importable when pytest runs from this directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
