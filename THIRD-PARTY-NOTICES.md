# Third-party notices

Rumil's own code is under the MIT License (see LICENSE). The following parts come from elsewhere or carry their own terms.

## Citation styles

The `.csl` files in `rumil/styles/` (except `lpsc.csl`) come from the Citation Style Language project's style repository (https://github.com/citation-style-language/styles). They are licensed under the Creative Commons Attribution-ShareAlike 3.0 License (https://creativecommons.org/licenses/by-sa/3.0/). Each file names its authors and contributors in its `<info>` block. `lpsc.csl` was written for Rumil and is released under the same license so it can be shared the same way.

Styles that Rumil downloads later (`rumil styles --install ...`) come from the same repository under the same license.

## Python packages

The installer downloads these packages from PyPI. They are not included in this folder, and each keeps its own license:

| Package | License |
|---|---|
| mcp (Model Context Protocol SDK) | MIT |
| citeproc-py | BSD-2-Clause-Views |
| python-docx | MIT |
| bibtexparser | LGPLv3 or BSD (dual) |
| pypdf | BSD-3-Clause |
| certifi | MPL-2.0 |
| starlette | BSD-3-Clause |
| uvicorn | BSD-3-Clause |

## Registry data

Rumil looks up records at Crossref, DataCite, doi.org, arXiv, NASA ADS, Open Library, Google Books, PubMed, and the Lunar and Planetary Institute. Their metadata stays under each service's terms of use. The test fixtures in `tests/fixtures/` are a few records captured from Crossref and arXiv, whose metadata is openly available.
