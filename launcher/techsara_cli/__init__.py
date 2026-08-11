"""Portable, dependency-free host bootstrap for the TechSara platform."""

from .hardware import HardwareInfo, detect_hardware
from .profiles import SelectedProfile, select_profile

__all__ = ["HardwareInfo", "SelectedProfile", "detect_hardware", "select_profile"]

__version__ = "1.0.0"
