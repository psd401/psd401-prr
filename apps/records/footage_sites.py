"""Which district site a footage request names, as an Aware device-group code.

Aware has no site field on a camera. Each camera belongs to a device group,
and the district names its groups with the same site codes it uses for each
school's subdomain on https://www.psd401.net/schools (hhe.psd401.net is Harbor
Heights Elementary, and so on). This table maps the names people write in a
request to those codes.

Rules (see AGENTS.md rule 7):
- Every code carries a source. Never derive a code from a school's initials:
  HBH is Henderson Bay High School, not Harbor Heights (HHE). A code with no
  citable source stays out of the table and is held only by a district-wide
  hold.
- Names and short names match case-insensitively as whole words.
  Abbreviations match only when written in capitals, and abbreviations that
  are common acronyms elsewhere (AES, DES, ESC, HRM, HRMS) are left out. A
  false match narrows the hold to the wrong building while the requested
  building's footage ages out; a missed match falls back to a district-wide
  hold, which over-preserves. The table errs toward the missed match.
- A region or city on its own ("Peninsula", "Key Peninsula", "Gig Harbor") is
  not a building and never resolves a site.

Callers match against ``request.description``, which is never edited after
intake (rule 9), so the resolution is reproducible from the legal record.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

SCHOOLS_PAGE = "https://www.psd401.net/schools"


@dataclass(frozen=True)
class Site:
    code: str
    name: str
    # Phrases matched case-insensitively as whole words.
    names: tuple[str, ...]
    # Tokens matched only as written, in capitals, as whole words.
    abbreviations: tuple[str, ...] = ()
    source: str = SCHOOLS_PAGE
    _patterns: tuple[re.Pattern[str], ...] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        patterns = [
            re.compile(_whole_words(phrase), re.IGNORECASE) for phrase in self.names
        ]
        patterns.extend(
            re.compile(_whole_words(token)) for token in self.abbreviations
        )
        object.__setattr__(self, "_patterns", tuple(patterns))

    def first_match(self, text: str) -> int | None:
        positions = [m.start() for p in self._patterns if (m := p.search(text))]
        return min(positions) if positions else None


def _whole_words(phrase: str) -> str:
    words = phrase.split()
    body = r"\s+".join(re.escape(word) for word in words)
    return rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])"


_ESC_NAMES = ("Educational Service Center", "Education Service Center", "District Office")
_DISTRICT_AWARE_GROUPS = "District Aware device groups, confirmed on PR #105 (2026-11-06)"

SITES: tuple[Site, ...] = (
    # Elementary schools. Source for each code: the school's subdomain on the
    # district schools page (SCHOOLS_PAGE), which matches its Aware group name.
    Site("AES", "Artondale Elementary School", ("Artondale Elementary",)),
    Site("DES", "Discovery Elementary School", ("Discovery Elementary",)),
    Site(
        "EES", "Evergreen Elementary School", ("Evergreen Elementary",), ("EES",)
    ),
    # HHES is the district's own short form for Harbor Heights (district staff
    # usage). HHE, not HBH: HBH is Henderson Bay High School.
    Site(
        "HHE",
        "Harbor Heights Elementary School",
        ("Harbor Heights",),
        ("HHES", "HHE"),
    ),
    Site(
        "MES",
        "Minter Creek Elementary School",
        ("Minter Creek Elementary",),
        ("MES",),
    ),
    Site("PIE", "Pioneer Elementary School", ("Pioneer Elementary",), ("PIE",)),
    Site("PES", "Purdy Elementary School", ("Purdy Elementary",), ("PES",)),
    Site(
        "SWE",
        "Swift Water Elementary School",
        ("Swift Water Elementary", "Swiftwater Elementary"),
        ("SWE",),
    ),
    Site("VES", "Vaughn Elementary School", ("Vaughn Elementary",), ("VES",)),
    Site("VGE", "Voyager Elementary School", ("Voyager Elementary",), ("VGE",)),
    # Middle schools (SCHOOLS_PAGE subdomains).
    Site("GMS", "Goodman Middle School", ("Goodman Middle",), ("GMS",)),
    # HRM/HRMS are left out as abbreviations: both are common HR acronyms.
    Site("HRM", "Harbor Ridge Middle School", ("Harbor Ridge",)),
    Site(
        "KPM",
        "Key Peninsula Middle School",
        ("Key Peninsula Middle", "Key Pen Middle"),
        ("KPMS", "KPM"),
    ),
    Site("KMS", "Kopachuck Middle School", ("Kopachuck Middle",), ("KMS",)),
    # High schools (SCHOOLS_PAGE subdomains). Gig Harbor High's subdomain is
    # "ghh"; its Aware group and common short form is GHHS, as on
    # https://en.wikipedia.org/wiki/Gig_Harbor_High_School. Henderson Bay's
    # own site writes its short form as HBHS.
    Site(
        "GHHS",
        "Gig Harbor High School",
        ("Gig Harbor High",),
        ("GHHS", "GHH"),
        source="https://en.wikipedia.org/wiki/Gig_Harbor_High_School",
    ),
    Site(
        "HBH",
        "Henderson Bay High School",
        ("Henderson Bay",),
        ("HBHS", "HBH"),
    ),
    Site("PHS", "Peninsula High School", ("Peninsula High",), ("PHS",)),
    # District programs and offices. The ESC (district office) is named in the
    # footer of SCHOOLS_PAGE; the ESC code is the district's network site code
    # for it. CTP is the district's own name for the Community Transition
    # Program (Special Education), https://www.psd401.net/ctp.
    Site("ESC", "Educational Service Center", _ESC_NAMES),
    Site(
        "CTP",
        "Community Transition Program",
        ("Community Transition",),
        ("CTP",),
        source="https://www.psd401.net/ctp",
    ),
    # Two Aware groups that are not schools, confirmed by the district's Aware
    # device list and its CTO on PR #105. Aware spells the District Resource
    # Center's group DCR; the building and its cameras say DRC. The TSD
    # (Technology Services) group's camera is inside the ESC building, so a
    # request naming the ESC holds both groups.
    Site(
        "DCR",
        "District Resource Center",
        ("District Resource Center",),
        ("DRC",),
        source=_DISTRICT_AWARE_GROUPS,
    ),
    Site(
        "TSD",
        "Technology Services (Educational Service Center)",
        _ESC_NAMES,
        source=_DISTRICT_AWARE_GROUPS,
    ),
)

_BY_CODE = {site.code: site for site in SITES}


def resolve_sites(text: str) -> tuple[Site, ...]:
    """Return every site ``text`` names, in order of first mention."""
    found = [
        (position, index, site)
        for index, site in enumerate(SITES)
        if (position := site.first_match(text or "")) is not None
    ]
    return tuple(site for _, _, site in sorted(found, key=lambda f: f[:2]))


def site_name(code: str) -> str:
    """The display name for an Aware site code, or the code itself."""
    site = _BY_CODE.get(code)
    return site.name if site else code
