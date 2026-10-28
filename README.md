# stackhpc-readthedocs-mcp

A small [MCP](https://modelcontextprotocol.io) server that lets your AI assistant read StackHPC
docs on Read the Docs for the release you actually run. It can also look at the config repo
(kayobe-config, azimuth-config) at the exact commit those docs were built from.

## Why bother

Ask an assistant "how do I enable X in kayobe-config" and the answer usually mixes three
releases, or comes straight from `master`. Generic docs MCP servers read one version or the
default branch.

With kayobe-config the answer depends on the release, and quite often it's in
`etc/kayobe/*.yml`, not in the docs. So here every question is tied to a release, and that
release points to both the docs and the git commit behind them.

## Example questions

- "We're on 2025.1. How do I turn on cephadm and which variables do I need?"
- "What does `kolla_base_distro` default to in 2024.1, and where is it set?"
- "How do I upgrade from Antelope (2023.1) to Epoxy (2025.1)?"
- "How do I migrate to Rocky Linux 10 on Epoxy?"

The assistant searches the docs for that release, reads the page, then greps the repo at the
same commit and opens the file. You get GitHub links back, so you can check it yourself.

Issues and PRs welcome, small ones especially ;)
