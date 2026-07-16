"""Clinical terminology validation.

Two layers, per ADR 0001 (``docs/adr/0001-terminology-validation.md`` — read
it before changing this module):

* **Format/checksum validation** (this module's built-in ``FormatResolver``):
  is a code *well-formed* — right shape, right check digit? Catches
  hallucinated and malformed codes using only public algorithms, no data,
  no licensing exposure.
* **Pluggable resolution** (the ``TerminologyResolver`` interface): is a code
  the *right, active* code for the claim? That needs the real code tables,
  which institutions supply via their own terminology server. Metaxu ships
  the interface and the check logic, never the data.

Every validation carries the ``terminology_version`` it was checked against,
so results are auditable and reproducible — ``FormatResolver`` reports
``"format-check"`` (algorithmic, not tied to a release); a data-backed
resolver reports e.g. ``"LOINC-2.78"``. See the ADR's versioning section.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Canonical short names used throughout Metaxu.
LOINC = "LOINC"
SNOMED = "SNOMED-CT"
RXNORM = "RxNorm"
UCUM = "UCUM"
ICD10 = "ICD-10-CM"
UNKNOWN_SYSTEM = "unknown"

# FHIR/URI system identifiers -> canonical short name.
_SYSTEM_ALIASES = {
    "http://loinc.org": LOINC,
    "loinc": LOINC,
    "http://snomed.info/sct": SNOMED,
    "snomed": SNOMED,
    "snomedct": SNOMED,
    "http://www.nlm.nih.gov/research/umls/rxnorm": RXNORM,
    "rxnorm": RXNORM,
    "http://unitsofmeasure.org": UCUM,
    "ucum": UCUM,
    "http://hl7.org/fhir/sid/icd-10-cm": ICD10,
    "http://hl7.org/fhir/sid/icd-10": ICD10,
    "icd-10-cm": ICD10,
    "icd10": ICD10,
}


def normalize_system(system: str | None) -> str:
    """Map a FHIR system URI (or loose name) to a canonical short name."""
    if not system:
        return UNKNOWN_SYSTEM
    return _SYSTEM_ALIASES.get(system.strip().lower(), system)


# -- status values -----------------------------------------------------------

STATUS_ACTIVE = "active"        # known-valid (data-backed resolvers only)
STATUS_WELL_FORMED = "well-formed"  # passes format/checksum; existence unverified
STATUS_MALFORMED = "malformed"  # fails format/checksum — likely hallucinated
STATUS_INACTIVE = "inactive"    # retired/deprecated (data-backed resolvers only)
STATUS_UNKNOWN = "unknown"      # code not found by a data-backed resolver
STATUS_UNVALIDATED = "unvalidated"  # no validator for this system


@dataclass
class CodeValidation:
    """Outcome of validating one coded value."""

    system: str
    code: str
    valid: bool
    status: str
    terminology_version: str
    display: str | None = None
    message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "system": self.system,
            "code": self.code,
            "valid": self.valid,
            "status": self.status,
            "terminology_version": self.terminology_version,
            "display": self.display,
            "message": self.message,
        }


# -- check-digit / format algorithms (public, no data) -----------------------


def luhn_mod10_ok(number: str) -> bool:
    """Luhn mod-10 check over a digit string whose last digit is the check
    digit. LOINC uses this (verified against 2160-0, 777-3)."""
    if not number.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2 == 1:  # every second digit from the right (skipping the check digit)
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# Verhoeff dihedral-group tables (standard). SNOMED CT SCTIDs end in a
# Verhoeff check digit computed over the preceding digits.
_VERHOEFF_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_VERHOEFF_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def verhoeff_ok(number: str) -> bool:
    """Validate a digit string whose last digit is a Verhoeff check digit."""
    if not number.isdigit():
        return False
    c = 0
    for i, ch in enumerate(reversed(number)):
        c = _VERHOEFF_D[c][_VERHOEFF_P[i % 8][int(ch)]]
    return c == 0


# A pragmatic UCUM subset: common atoms and prefixes plus structural rules.
# Full UCUM is a formal grammar; format-checking validates a curated set and
# structural well-formedness, marking unknown-but-structural units UNKNOWN.
_UCUM_ATOMS = {
    "g", "kg", "mg", "ug", "ng", "pg", "L", "dL", "mL", "uL", "m", "cm", "mm",
    "um", "nm", "km", "s", "min", "h", "d", "wk", "mo", "a", "mol", "mmol",
    "umol", "nmol", "pmol", "eq", "meq", "U", "IU", "mU", "kU", "Cel", "K",
    "mm[Hg]", "%", "1", "10*3/uL", "10*6/uL", "10*9/L", "10*12/L", "/uL",
    "/L", "/mL", "mg/dL", "g/dL", "mmol/L", "umol/L", "mEq/L", "ng/mL",
    "pg/mL", "U/L", "IU/L", "mg/L", "beats/min", "/min",
}
_UCUM_STRUCTURAL = re.compile(r"^[A-Za-z0-9\[\]{}%/.*+\-]+$")


@dataclass
class FormatResolver:
    """The built-in, data-free resolver: format + check-digit validation.

    Never asserts a code *exists* — only that it is well-formed. A passing
    result has status ``well-formed``, never ``active``. Systems without a
    format check (or a code from an unknown system) return ``unvalidated``.
    """

    version: str = "format-check"

    def resolve(self, system: str, code: str) -> CodeValidation:
        canonical = normalize_system(system)
        code = (code or "").strip()
        checker = {
            LOINC: self._loinc,
            SNOMED: self._snomed,
            RXNORM: self._rxnorm,
            UCUM: self._ucum,
            ICD10: self._icd10,
        }.get(canonical)
        if checker is None:
            return CodeValidation(
                system=canonical,
                code=code,
                valid=True,  # can't disprove; don't flag unknown systems
                status=STATUS_UNVALIDATED,
                terminology_version=self.version,
                message=f"no format validator for system '{canonical}'",
            )
        return checker(canonical, code)

    def _ok(self, system, code, message=None):
        return CodeValidation(system, code, True, STATUS_WELL_FORMED, self.version, message=message)

    def _bad(self, system, code, message):
        return CodeValidation(system, code, False, STATUS_MALFORMED, self.version, message=message)

    def _loinc(self, system, code):
        if not re.fullmatch(r"\d{1,7}-\d", code):
            return self._bad(system, code, "LOINC must be <number>-<check digit>")
        if not luhn_mod10_ok(code.replace("-", "")):
            return self._bad(system, code, "LOINC check digit failed")
        return self._ok(system, code)

    def _snomed(self, system, code):
        if not re.fullmatch(r"\d{6,18}", code):
            return self._bad(system, code, "SNOMED SCTID must be 6-18 digits")
        if not verhoeff_ok(code):
            return self._bad(system, code, "SNOMED Verhoeff check digit failed")
        return self._ok(system, code)

    def _rxnorm(self, system, code):
        # RxCUI is a plain integer; format-check can only catch non-numeric.
        if not re.fullmatch(r"\d{1,8}", code):
            return self._bad(system, code, "RxNorm RxCUI must be numeric")
        return self._ok(system, code, message="RxCUI is well-formed; existence unverified")

    def _ucum(self, system, code):
        if code in _UCUM_ATOMS:
            return self._ok(system, code)
        if not _UCUM_STRUCTURAL.fullmatch(code):
            return self._bad(system, code, "UCUM contains characters outside the grammar")
        if code.count("[") != code.count("]") or code.count("{") != code.count("}"):
            return self._bad(system, code, "UCUM has unbalanced brackets")
        return CodeValidation(
            system, code, True, STATUS_UNKNOWN, self.version,
            message="structurally valid UCUM but not in the known-atom subset",
        )

    def _icd10(self, system, code):
        if not re.fullmatch(r"[A-TV-Z]\d[0-9A-Z](\.[0-9A-Z]{1,4})?", code):
            return self._bad(system, code, "ICD-10-CM pattern failed")
        return self._ok(system, code)


# -- coding extraction -------------------------------------------------------


@dataclass
class Coding:
    """A system+code reference, optionally with the display the AI used."""

    system: str
    code: str
    display: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"system": self.system, "code": self.code, "display": self.display}


def extract_codings(obj: Any) -> list[Coding]:
    """Pull codings out of a FHIR-shaped object (or any nested dict/list).

    Recognizes FHIR ``coding`` arrays (``{system, code, display}``) and bare
    sibling ``system``/``code`` pairs. Deduplicates on (system, code).
    """
    found: list[Coding] = []
    seen: set[tuple[str, str]] = set()

    def add(system: Any, code: Any, display: Any) -> None:
        if not isinstance(code, (str, int)) or system is None:
            return
        key = (str(system), str(code))
        if key in seen:
            return
        seen.add(key)
        found.append(
            Coding(system=str(system), code=str(code), display=display if isinstance(display, str) else None)
        )

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "system" in node and "code" in node:
                add(node.get("system"), node.get("code"), node.get("display"))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(obj)
    return found


class TerminologyValidator:
    """Validates a set of codings with a resolver (FormatResolver by default)."""

    def __init__(self, resolver: Any | None = None):
        self.resolver = resolver or FormatResolver()

    @property
    def version(self) -> str:
        return getattr(self.resolver, "version", "unknown")

    def validate(self, codings: list[Coding]) -> list[CodeValidation]:
        return [self.resolver.resolve(c.system, c.code) for c in codings]
