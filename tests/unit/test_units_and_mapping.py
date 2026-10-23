"""Identity crosswalks, unit conversion and the canonical employee model."""

from decimal import Decimal

from app.mapping.canonicalize import canonical_employee
from app.mapping.model import MappingProblem, QuantityInBase, ScopeKind
from tests import factories as f


def test_box_converts_to_twelve_each():
    q = f.mapping().resolve_quantity("ERP", "ERP-BOX-12", Decimal(10), "BOX")
    assert isinstance(q, QuantityInBase)
    assert (q.canonical_product_id, q.factor, q.base_qty, q.base_uom) == ("SKU-002", 12, 120, "EA")
    assert q.alias.source_record_key == "ERP:ERP-BOX-12"


def test_quantity_already_in_base_unit_converts_with_factor_one():
    q = f.mapping().resolve_quantity("ERP", "ERP-BOX-12", Decimal(5), "EA")
    assert isinstance(q, QuantityInBase)
    assert (q.factor, q.base_qty) == (1, 5)


def test_erp_and_wms_ids_map_to_one_canonical_product():
    m = f.mapping()
    erp = m.resolve_quantity("ERP", "ERP-SKU-001", Decimal(1), "EA")
    wms = m.resolve_quantity("WMS", "WMS-BRC-001", Decimal(1), "EA")
    assert erp.canonical_product_id == wms.canonical_product_id == "SKU-001"


def test_identity_is_per_source_system_never_guessed_across_systems():
    problem = f.mapping().resolve_quantity("ERP", "WMS-BRC-001", Decimal(1), "EA")
    assert isinstance(problem, MappingProblem) and problem.code == "PRODUCT_UNMAPPED"


def test_unknown_unit_is_not_convertible():
    problem = f.mapping().resolve_quantity("ERP", "ERP-BOX-12", Decimal(1), "PALLET")
    assert isinstance(problem, MappingProblem) and problem.code == "UOM_NOT_CONVERTIBLE"


def test_alias_in_conflict_is_treated_as_ambiguous():
    aliases = [
        f.clone(a, review_status="CONFLICT") if a.source_record_key == "ERP:ERP-BOX-12" else a
        for a in f.facts("product_aliases")
    ]
    problem = f.mapping(product_facts=aliases).resolve_quantity("ERP", "ERP-BOX-12", Decimal(1), "BOX")
    assert problem.code == "PRODUCT_MAPPING_IN_CONFLICT"


def test_inconsistent_base_units_refuse_every_mapping_to_that_product():
    aliases = [
        f.clone(a, base_uom="KG", source_uom="KG") if a.source_record_key == "WMS:WMS-MAT-2002" else a
        for a in f.facts("product_aliases")
    ]
    m = f.mapping(product_facts=aliases)
    assert m.resolve_quantity("ERP", "ERP-BOX-12", Decimal(1), "BOX").code == "PRODUCT_BASE_UOM_INCONSISTENT"
    assert [(cid, p.code) for cid, p in m.crosswalk_problems()] == [("SKU-002", "PRODUCT_BASE_UOM_INCONSISTENT")]


def test_erp_wms_and_hrms_location_codes_map_to_one_store():
    m = f.mapping()
    resolved = {
        m.resolve_location(s, ref).canonical_location_id
        for s, ref in [("ERP", "MUM-014"), ("WMS", "WH-MH-014"), ("HRMS", "ST-014")]
    }
    assert resolved == {"STORE-014"}
    assert m.resolve_location("HRMS", "MUM-014").code == "LOCATION_UNMAPPED"


def test_mapping_version_is_deterministic_and_tracks_content():
    assert f.mapping().version == f.mapping().version
    changed = [
        f.clone(a, content_hash="different") if a.source_record_key == "ERP:ERP-BOX-12" else a
        for a in f.facts("product_aliases")
    ]
    assert f.mapping(product_facts=changed).version != f.mapping().version


def test_store_employee_location_is_mapped_before_use():
    employees = f.employees()
    assert employees["HR-102"].scope.kind == ScopeKind.STORE
    assert employees["HR-102"].scope.location_id == "STORE-014"
    assert employees["HR-401"].scope.location_id == "STORE-015"


def test_regional_employee_is_region_scoped_without_a_store():
    hr501 = f.employees()["HR-501"]
    assert hr501.scope.kind == ScopeKind.REGION
    assert (hr501.scope.region_id, hr501.scope.location_id) == ("REGION-MH", None)


def test_employee_with_unmapped_location_has_no_scope_and_a_problem():
    fact = f.clone(f.by_key("employees")["HR-102"], location_ref="ST-099")
    employee = canonical_employee(fact, f.mapping())
    assert employee.scope is None
    assert [p.code for p in employee.problems] == ["EMPLOYEE_LOCATION_UNMAPPED"]


def test_employee_region_disagreeing_with_crosswalk_is_flagged():
    fact = f.clone(f.by_key("employees")["HR-102"], region_id="REGION-KA")
    employee = canonical_employee(fact, f.mapping())
    assert employee.scope.region_id == "REGION-MH"
    assert [p.code for p in employee.problems] == ["EMPLOYEE_REGION_MISMATCH"]


def test_unknown_employment_status_is_not_active():
    fact = f.clone(f.by_key("employees")["HR-102"], employment_status="ON_LEAVE")
    employee = canonical_employee(fact, f.mapping())
    assert employee.is_active is None
    assert [p.code for p in employee.problems] == ["UNRECOGNIZED_EMPLOYMENT_STATUS"]
