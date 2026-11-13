# Contributing

Use Python 3.11 or newer. Create a virtual environment, activate it, then install the project and development tools:

```sh
python -m pip install -e ".[dev]"
python -m ruff check .
python -m ruff format .
python -m unittest discover -s tests -v
```

Set `NOMADPL_NETWORK_TEST=1` to include the two-process Reticulum test. In PowerShell use `$env:NOMADPL_NETWORK_TEST = "1"`; in a POSIX shell use `export NOMADPL_NETWORK_TEST=1`. The fixture uses temporary identities, databases and loopback interfaces. It does not use your personal Reticulum configuration.

Keep functions focused and name variables for the role they play. Use straightforward branches when a nested expression is harder to read. Give every function and method a concise docstring explaining its purpose or contract, including private helpers and test fixtures. Put longer usage examples in the README. Additional comments should explain a protocol rule, lifecycle constraint or reason for a decision; they should not narrate the next line.

Add a regression test when fixing a bug. Changes to destination aspects or the wire envelope need a compatibility note in `docs/protocol.md` and `CHANGELOG.md`. Do not change `nomadpl.node` or reuse NomadNet's namespace without an explicit protocol migration.

Keep sample websites small and offline-friendly. Do not commit identity files, caches, local settings or generated distributions. Reticulum is a separate dependency; preserve its license and do not copy upstream implementation into this repository.

Before a pull request, run the same checks as CI and describe the behavior that changed, why it changed, and how you tested it. See [RELEASING.md](RELEASING.md) for distribution checks.
