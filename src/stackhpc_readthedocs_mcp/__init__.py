"""MCP server for StackHPC documentation on Read the Docs and the repositories behind it."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("stackhpc-readthedocs-mcp")
except PackageNotFoundError:  # running from a source tree without installing
    __version__ = "0.0.0"
