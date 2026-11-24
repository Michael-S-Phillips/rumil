# Contributing to Rumil

## Reporting a problem

Open an issue at https://github.com/michael-s-phillips/rumil/issues. Say what you did and what happened, give the DOI or citekey if a verification result looks wrong, and paste the output of `rumil doctor`. That output never includes your ADS token; it does include your contact email, which you can delete before posting.

## Working on the code

```bash
git clone https://github.com/michael-s-phillips/rumil.git
cd rumil
python3 -m venv .venv && .venv/bin/pip install -e ".[test]"
.venv/bin/pytest
```

The tests run offline. Registry responses are replayed from `tests/fixtures/`, and the test transport rejects requests the real services would reject (for example, a Crossref search that asks for a field Crossref doesn't allow). When you fix a bug, add a test that fails without the fix. GitHub runs the tests on macOS and Linux with Python 3.10 and 3.13 for every push and pull request.

To try the library window with your changes, run `.venv/bin/rumil serve --open`. Set `RUMIL_HOME` to a scratch folder first if you don't want to touch your own library.

## Making a release

1. Set the new version in both `pyproject.toml` and `rumil/__init__.py`.
2. Add a section for it at the top of `CHANGELOG.md`.
3. Commit, then tag and push:

   ```bash
   git tag -a v0.3.1 -m "Rumil 0.3.1"
   git push origin main v0.3.1
   ```

Pushing the tag runs `.github/workflows/release.yml`. It runs the tests, checks that the tag matches both version numbers, builds `rumil-v0.3.1.zip` and a wheel, and publishes a GitHub release with that version's changelog section as its notes. Everyone's `rumil update` then installs it.
