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
