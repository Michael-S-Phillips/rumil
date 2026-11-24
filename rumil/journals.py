"""Journal-name macros from AAS / NASA ADS BibTeX exports (\\apj, \\icarus, \\maps, ...)."""

from __future__ import annotations

import re

AAS_MACROS = {
    "aj": "The Astronomical Journal", "actaa": "Acta Astronomica", "araa": "Annual Review of Astronomy and Astrophysics",
    "apj": "The Astrophysical Journal", "apjl": "The Astrophysical Journal Letters",
    "apjs": "The Astrophysical Journal Supplement Series", "ao": "Applied Optics", "apss": "Astrophysics and Space Science",
    "aap": "Astronomy & Astrophysics", "aapr": "The Astronomy and Astrophysics Review",
    "aaps": "Astronomy and Astrophysics Supplement Series", "azh": "Astronomicheskii Zhurnal",
    "baas": "Bulletin of the American Astronomical Society", "caa": "Chinese Astronomy and Astrophysics",
    "cjaa": "Chinese Journal of Astronomy and Astrophysics", "icarus": "Icarus",
    "jcap": "Journal of Cosmology and Astroparticle Physics", "jrasc": "Journal of the Royal Astronomical Society of Canada",
    "memras": "Memoirs of the Royal Astronomical Society", "mnras": "Monthly Notices of the Royal Astronomical Society",
    "na": "New Astronomy", "nar": "New Astronomy Reviews", "pra": "Physical Review A", "prb": "Physical Review B",
    "prc": "Physical Review C", "prd": "Physical Review D", "pre": "Physical Review E", "prl": "Physical Review Letters",
    "pasa": "Publications of the Astronomical Society of Australia",
    "pasp": "Publications of the Astronomical Society of the Pacific", "pasj": "Publications of the Astronomical Society of Japan",
    "qjras": "Quarterly Journal of the Royal Astronomical Society", "rmxaa": "Revista Mexicana de Astronomía y Astrofísica",
    "skytel": "Sky and Telescope", "solphys": "Solar Physics", "sovast": "Soviet Astronomy", "ssr": "Space Science Reviews",
    "zap": "Zeitschrift für Astrophysik", "nat": "Nature", "iaucirc": "IAU Circulars", "aplett": "Astrophysical Letters",
    "bain": "Bulletin of the Astronomical Institutes of the Netherlands", "gca": "Geochimica et Cosmochimica Acta",
    "grl": "Geophysical Research Letters", "jcp": "The Journal of Chemical Physics", "jgr": "Journal of Geophysical Research",
    "jgre": "Journal of Geophysical Research: Planets", "jgra": "Journal of Geophysical Research: Space Physics",
    "jqsrt": "Journal of Quantitative Spectroscopy and Radiative Transfer", "memsai": "Memorie della Società Astronomica Italiana",
    "physrep": "Physics Reports", "physscr": "Physica Scripta", "planss": "Planetary and Space Science",
    "procspie": "Proceedings of the SPIE", "psj": "The Planetary Science Journal", "maps": "Meteoritics & Planetary Science",
    "epsl": "Earth and Planetary Science Letters", "sci": "Science", "science": "Science", "nature": "Nature",
}

_MACRO_RE = re.compile(r"^\s*\\([A-Za-z]+)\s*$")


def journal_from_macro(name: str | None) -> str | None:
    """'\\maps' -> 'Meteoritics & Planetary Science'; None if `name` isn't a known journal macro."""
    m = _MACRO_RE.match(name or "")
    if not m:
        return None
    return AAS_MACROS.get(m.group(1).lower())
