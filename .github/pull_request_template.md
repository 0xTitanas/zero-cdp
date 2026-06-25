<!-- Thanks for contributing to ZeroCDP. Keep changes small and auditable. -->

## Summary

<!-- What does this change do, and why? -->

## Related issue

<!-- e.g. Fixes #123, or "none". -->

## Type of change

- [ ] Bug fix
- [ ] New capability within the project boundary
- [ ] Documentation only
- [ ] Tests / CI only

## Checklist

- [ ] `bare_cdp.py` still imports only the Python standard library (no new runtime dependency).
- [ ] The project remains single-file (`bare_cdp.py`).
- [ ] Tests are added or updated for behavior changes, and remain stdlib-only (no pytest/third-party fixtures).
- [ ] `CHANGELOG.md` updated under `## [Unreleased]` (for user-visible changes).
- [ ] Docs/README updated if the public surface or claims changed.
- [ ] No secrets, credentials, private paths, or authenticated page content are included in the diff.
- [ ] Chrome remote-debugging safety guidance still favors `127.0.0.1`, dedicated/disposable profiles, and no credential dumps.
- [ ] Selectors, JavaScript snippets, and shell examples avoid injection-prone string construction.
- [ ] I ran the live-Chrome smoke if this touches launch, navigation, selectors, input, click, keypress, screenshots, targets, or events.

## Verification

<!-- Paste the commands you ran. The local smoke set is: -->

```sh
python -m py_compile bare_cdp.py tests/test_bare_cdp.py examples/*.py
python -W error::ResourceWarning -m unittest discover -s tests -v
python -m bare_cdp --help
python -S -c "import sys; sys.path.insert(0, '.'); import bare_cdp; print(bare_cdp.__version__)"
```
