# Releasing NomadPL

The repository root is this directory. The source, tests and examples run independently of the surrounding workspace.

1. Update the version in `pyproject.toml` and `src/nomadpl/__init__.py`, the gateway version string, and `CHANGELOG.md`.
2. Install development dependencies, run Ruff, and run the full suite with `NOMADPL_NETWORK_TEST=1`.
3. Build and inspect the artifacts:

   ```sh
   python -m build
   python -m twine check --strict dist/*
   ```

4. Install the new wheel into a fresh environment with its declared dependencies. Run the tests against that installation, outside `src`, and check `nomadpl --help`.
5. Review the files to be committed. Keep identities, databases, `.env` files and build output out of Git. Commit the reviewed source using your own Git author identity.

## GitHub releases

The repository is [Thorian/nomadpl](https://github.com/Thorian/nomadpl). Commit the
reviewed source and push it to `main` before preparing a release:

```sh
git status --short
git add .
git commit -m "Update NomadPL"
git push origin main
```

Check the staged diff before committing. Source and issue-tracker URLs are in
`pyproject.toml`; update them if the repository moves.

After the entire GitHub CI matrix passes, create an annotated version tag and push it. Create a GitHub release from that tag and attach the wheel and source archive. Mark the initial 0.2.0 release as a GitHub pre-release because its APIs are experimental. Use `CHANGELOG.md` for the release notes. The workflow checks source and installed-wheel tests on Windows/Linux with Python 3.11–3.13, but does not publish a GitHub release or upload to PyPI. Local Windows validation alone does not establish the other matrix results.

PyPI publication is a separate step. It requires an available package name and a configured maintainer account; no upload credentials or automatic registry publishing are included here.
