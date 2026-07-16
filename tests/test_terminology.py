"""Tests for clinical terminology validation (ADR 0001)."""

import json

from metaxu import (
    AssuranceArtifact,
    AssuranceSession,
    Coding,
    FormatResolver,
    TerminologyValidator,
    extract_codings,
    merge_artifacts,
    normalize_system,
)
from metaxu.cli import main as cli_main
from metaxu.terminology import (
    ICD10,
    LOINC,
    RXNORM,
    SNOMED,
    UCUM,
    luhn_mod10_ok,
    verhoeff_ok,
)

R = FormatResolver()


# -- check-digit algorithms verified against real codes ----------------------


def test_loinc_luhn_against_real_codes():
    # 2160-0 creatinine, 777-3 platelets, 2823-3 potassium — all real LOINC.
    for code in ("21600", "7773", "28233"):
        assert luhn_mod10_ok(code)
    assert not luhn_mod10_ok("21601")


def test_snomed_verhoeff_against_real_concept_ids():
    # Real SNOMED CT concepts: MI, diabetes, hypertension, fever, appendectomy.
    for sctid in ("22298006", "73211009", "38341003", "386661006", "80146002"):
        assert verhoeff_ok(sctid), sctid
    assert not verhoeff_ok("22298007")  # corrupted check digit


def test_loinc_resolver():
    assert R.resolve("http://loinc.org", "2160-0").valid
    bad = R.resolve("http://loinc.org", "2160-1")
    assert not bad.valid
    assert bad.status == "malformed"
    assert R.resolve("http://loinc.org", "abc").status == "malformed"


def test_snomed_resolver():
    assert R.resolve("http://snomed.info/sct", "22298006").valid
    assert not R.resolve("http://snomed.info/sct", "12345").valid  # too short
    assert not R.resolve("http://snomed.info/sct", "22298007").valid  # bad checksum


def test_rxnorm_resolver_is_format_only():
    ok = R.resolve("http://www.nlm.nih.gov/research/umls/rxnorm", "7980")
    assert ok.valid
    assert "existence unverified" in ok.message
    assert not R.resolve("rxnorm", "not-a-number").valid


def test_ucum_resolver():
    assert R.resolve("http://unitsofmeasure.org", "mg/dL").valid
    assert R.resolve("ucum", "10*3/uL").valid
    unknown = R.resolve("ucum", "furlongs")
    assert unknown.valid  # structurally fine
    assert unknown.status == "unknown"
    assert not R.resolve("ucum", "mm[Hg").valid  # unbalanced bracket


def test_icd10_resolver():
    assert R.resolve("http://hl7.org/fhir/sid/icd-10-cm", "I48.0").valid
    assert R.resolve("icd10", "E11.9").valid
    assert not R.resolve("icd10", "148.0").valid  # leading digit, not letter


def test_unknown_system_is_unvalidated_not_flagged():
    result = R.resolve("http://example.org/local-codes", "XYZ")
    assert result.valid  # cannot disprove an unknown system
    assert result.status == "unvalidated"


def test_every_result_carries_terminology_version():
    for system, code in [("loinc", "2160-0"), ("snomed", "22298006"), ("ucum", "mg/dL")]:
        assert R.resolve(system, code).terminology_version == "format-check"


def test_normalize_system():
    assert normalize_system("http://loinc.org") == LOINC
    assert normalize_system("http://snomed.info/sct") == SNOMED
    assert normalize_system("RxNorm") == RXNORM
    assert normalize_system("http://unitsofmeasure.org") == UCUM
    assert normalize_system(None) == "unknown"


# -- coding extraction from FHIR --------------------------------------------


def test_extract_codings_from_fhir_observation():
    obs = {
        "resourceType": "Observation",
        "code": {
            "coding": [
                {"system": "http://loinc.org", "code": "2160-0", "display": "Creatinine"}
            ]
        },
        "valueQuantity": {"code": "mg/dL", "system": "http://unitsofmeasure.org"},
    }
    codings = extract_codings(obs)
    pairs = {(normalize_system(c.system), c.code) for c in codings}
    assert (LOINC, "2160-0") in pairs
    assert (UCUM, "mg/dL") in pairs


def test_extract_codings_deduplicates():
    obj = {"a": {"system": "x", "code": "1"}, "b": {"system": "x", "code": "1"}}
    assert len(extract_codings(obj)) == 1


# -- session integration -----------------------------------------------------


def test_session_records_and_validates_codings():
    with AssuranceSession(question="Q?") as session:
        session.record_coding("http://loinc.org", "2160-0", "Creatinine")
        session.record_coding("http://snomed.info/sct", "22298006", "MI")
        session.set_answer("A")
    results = session.artifact.terminology
    assert len(results) == 2
    assert all(r["valid"] for r in results)
    assert all(r["terminology_version"] == "format-check" for r in results)


def test_malformed_code_is_critical_safety_finding():
    with AssuranceSession(question="Q?") as session:
        session.record_coding("http://loinc.org", "9999-9", "hallucinated")
        session.set_answer("A")
    checks = {f["check"]: f for f in session.artifact.safety_checks}
    assert "malformed_terminology" in checks
    assert checks["malformed_terminology"]["severity"] == "critical"


def test_terminology_trust_dimension_conditional():
    # No codings: dimension absent.
    with AssuranceSession(question="Q?") as s1:
        s1.set_answer("A")
    assert "terminology_correctness" not in s1.artifact.trust_scores

    # With one malformed of two: present, score 0.5.
    with AssuranceSession(question="Q?") as s2:
        s2.record_coding("loinc", "2160-0")
        s2.record_coding("loinc", "2160-1")  # malformed
        s2.set_answer("A")
    dim = s2.artifact.trust_scores["terminology_correctness"]
    assert dim["score"] == 0.5
    assert dim["inputs"] == {"codings": 2, "malformed": 1}


def test_record_codings_from_fhir():
    obs = {"code": {"coding": [{"system": "http://loinc.org", "code": "777-3"}]}}
    with AssuranceSession(question="Q?") as session:
        session.record_codings_from(obs)
        session.set_answer("A")
    assert session.artifact.terminology[0]["code"] == "777-3"


def test_data_backed_resolver_version_flows_through():
    class FakeLoinc278:
        version = "LOINC-2.78"

        def resolve(self, system, code):
            from metaxu import CodeValidation

            return CodeValidation(
                system="LOINC", code=code, valid=True, status="active",
                terminology_version=self.version, display="Creatinine",
            )

    with AssuranceSession(question="Q?", terminology_resolver=FakeLoinc278()) as session:
        session.record_coding("http://loinc.org", "2160-0")
        session.set_answer("A")
    result = session.artifact.terminology[0]
    assert result["terminology_version"] == "LOINC-2.78"
    assert result["status"] == "active"


# -- merge re-validates ------------------------------------------------------


def test_merge_revalidates_terminology():
    with AssuranceSession(question="Q?", interaction_id="ixn-t") as a:
        a.record_coding("loinc", "2160-0")
        a.set_answer("A")
    with AssuranceSession(question="Q?", interaction_id="ixn-t") as b:
        b.record_coding("loinc", "bad")  # malformed
        b.set_answer("A")
    merged = merge_artifacts([a.artifact, b.artifact])
    assert len(merged.terminology) == 2
    assert any(not r["valid"] for r in merged.terminology)
    assert merged.trust_scores["terminology_correctness"]["score"] == 0.5


# -- artifact roundtrip + schema --------------------------------------------


def test_terminology_survives_roundtrip_and_integrity(tmp_path):
    with AssuranceSession(question="Q?") as session:
        session.record_coding("loinc", "2160-0")
        session.set_answer("A")
    path = str(tmp_path / "a.json")
    session.artifact.save(path)
    loaded = AssuranceArtifact.load(path)
    assert loaded.terminology == session.artifact.terminology
    assert loaded.verify_integrity()
    assert cli_main(["validate", path]) == 0


# -- CLI ---------------------------------------------------------------------


def test_cli_inspect_shows_terminology(tmp_path, capsys):
    with AssuranceSession(question="Q?") as session:
        session.record_coding("http://loinc.org", "2160-0")
        session.record_coding("http://loinc.org", "9999-9")  # malformed
        session.set_answer("A")
    path = str(tmp_path / "a.json")
    session.artifact.save(path)
    assert cli_main(["inspect", path]) == 0
    out = capsys.readouterr().out
    assert "Terminology" in out
    assert "format-check" in out
    assert "1 malformed" in out


def test_cli_report_aggregates_terminology(tmp_path, capsys):
    store = tmp_path / "store"
    store.mkdir()
    with AssuranceSession(question="Q?") as good:
        good.record_coding("loinc", "2160-0")
        good.set_answer("A")
    good.artifact.save(str(store / "good.json"))
    with AssuranceSession(question="Q?") as bad:
        bad.record_coding("loinc", "bad")
        bad.set_answer("A")
    bad.artifact.save(str(store / "bad.json"))

    assert cli_main(["report", str(store), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["terminology"]["codes_checked"] == 2
    assert report["terminology"]["malformed"] == 1
    assert report["terminology"]["malformed_rate"] == 0.5
