from observability_migration.core import variable_classifier as vc


def test_accepted_binding_is_frozen():
    binding = vc.AcceptedBinding(
        field="service.instance.id", multi=False, options_query="FROM x"
    )
    try:
        binding.field = "other"  # type: ignore[misc]
    except Exception as exc:
        assert isinstance(exc, (AttributeError, TypeError))
    else:
        raise AssertionError("AcceptedBinding must be frozen")


def test_rejected_binding_requires_known_reason():
    binding = vc.RejectedBinding(reason="unsupported_variable_type")
    assert binding.reason in vc.REJECT_REASONS


def test_rejected_binding_unknown_reason_is_caught_at_construction():
    import pytest
    with pytest.raises(ValueError):
        vc.RejectedBinding(reason="not_a_real_reason")


def test_reject_reasons_tuple_and_literal_agree():
    from typing import get_args
    assert set(get_args(vc.RejectReason)) == set(vc.REJECT_REASONS)


def test_every_reject_reason_constructs_a_rejected_binding():
    for reason in vc.REJECT_REASONS:
        binding = vc.RejectedBinding(reason=reason)
        assert binding.reason == reason


def test_variable_binding_map_accepts_heterogeneous_values():
    bm: vc.VariableBindingMap = {
        "accepted_var": vc.AcceptedBinding(
            field="service.instance.id", multi=False, options_query="FROM x"
        ),
        "rejected_var": vc.RejectedBinding(reason="include_all_unsupported"),
    }
    assert isinstance(bm["accepted_var"], vc.AcceptedBinding)
    assert isinstance(bm["rejected_var"], vc.RejectedBinding)


def test_compute_min_kibana_version_empty_map():
    assert vc.compute_min_kibana_version({}) == "9.1.0"


def test_compute_min_kibana_version_single_value_only():
    bm = {"x": vc.AcceptedBinding(field="f", multi=False, options_query="FROM y")}
    assert vc.compute_min_kibana_version(bm) == "9.1.0"


def test_compute_min_kibana_version_one_multi_value():
    bm = {
        "x": vc.AcceptedBinding(field="f", multi=True, options_query="FROM y"),
        "y": vc.AcceptedBinding(field="g", multi=False, options_query="FROM y"),
    }
    assert vc.compute_min_kibana_version(bm) == "9.3.0"


def test_compute_min_kibana_version_rejected_multi_does_not_lift_floor():
    bm = {"x": vc.RejectedBinding(reason="include_all_unsupported")}
    assert vc.compute_min_kibana_version(bm) == "9.1.0"
