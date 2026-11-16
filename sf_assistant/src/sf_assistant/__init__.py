"""sf_assistant: Salesforce assistant (knowledge agent, record agent, merge, checks).

Package layout (imports go one way: engine <- tools <- agent <- pipeline <- cli/web; knowledge does not import
engine). No package ``__init__`` re-exports anything, so every import stays light.
"""

__version__ = "0.1.0"
