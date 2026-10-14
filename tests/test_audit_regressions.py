"""Regression tests for blockers and security/statistical fixes from the latest-commit audit."""
import importlib
import random

import pytest

import benchmark
import network_manager
import security_utils
from benchmark import _randomized_trial_order, compute_statistics, paired_t_test, welch_t_test
from network_manager import BoundInterfaceHTTPAdapter


def test_core_modules_import_in_clean_process_context():
    # Module-level imports above deliberately catch import blockers during collection.
    assert importlib.import_module("security_utils") is security_utils
    assert importlib.import_module("network_manager") is network_manager
    assert importlib.import_module("benchmark") is benchmark


def test_randomized_trial_order_is_reproducible_and_does_not_mutate_input():
    configs = [{"id": name} for name in ("A", "B", "AB")]
    original = [item["id"] for item in configs]
    order_one = _randomized_trial_order(configs, random.Random(20261014))
    order_two = _randomized_trial_order(configs, random.Random(20261014))
    assert [item["id"] for item in order_one] == [item["id"] for item in order_two]
    assert sorted(item["id"] for item in order_one) == sorted(original)
    assert [item["id"] for item in configs] == original


def test_welch_zero_variance_does_not_falsely_claim_significance():
    t_stat, p_value, df, significant = welch_t_test([20.0, 20.0, 20.0], [10.0, 10.0, 10.0])
    assert t_stat == 0.0
    assert p_value == 1.0
    assert df == 0.0
    assert significant is False


def test_welch_preserves_full_precision_for_multiple_comparison_correction():
    import scipy.stats
    a = [12.1, 14.5, 13.2, 15.0, 14.1, 16.2]
    b = [8.5, 9.2, 8.8, 9.5]
    t_stat, p_value, df, significant = welch_t_test(a, b)
    expected = scipy.stats.ttest_ind(a, b, equal_var=False, alternative="greater")
    assert abs(t_stat - float(expected.statistic)) < 1e-12
    assert abs(p_value - float(expected.pvalue)) < 1e-12
    assert abs(df - float(expected.df)) < 1e-12
    assert significant is True


def test_paired_t_test_uses_block_aligned_differences():
    combined = [12.0, 14.0, 13.0, 17.0, 15.0]
    baseline = [9.0, 10.0, 10.0, 12.0, 11.0]
    t_stat, p_value, df, significant = paired_t_test(combined, baseline)
    assert t_stat > 0
    assert 0.0 <= p_value <= 1.0
    assert df == 4.0
    assert significant is True


def test_paired_t_test_rejects_unpaired_or_degenerate_data():
    assert paired_t_test([3.0], [1.0])[3] is False
    assert paired_t_test([3.0, 4.0], [1.0])[3] is False
    assert paired_t_test([4.0, 5.0, 6.0], [1.0, 2.0, 3.0])[3] is False


def test_single_sample_statistics_are_not_certification_evidence():
    stats = compute_statistics([15.0])
    assert stats["n"] == 1
    assert stats["std_dev"] == 0.0


def test_outbound_binding_cannot_be_verified_without_a_live_probe():
    adapter = BoundInterfaceHTTPAdapter(ifname="lo", source_ip="127.0.0.1")
    ok, message = adapter.verify_outbound_binding()
    assert ok is False
    assert "requires an explicit live test URL" in message


def test_ipv6_literal_is_validated_without_malformed_url_construction():
    assert security_utils.resolve_and_validate_host("2001:4860:4860::8888", 443) == "2001:4860:4860::8888"
    with pytest.raises(Exception, match="prohibited IP address"):
        security_utils.resolve_and_validate_host("fd00::1", 443)


def test_malformed_url_port_is_rejected_as_validation_result():
    valid, error, ips = security_utils.validate_url_ssrf("http://example.com:99999/")
    assert valid is False
    assert "Invalid URL port" in error
    assert ips == []



def test_raw_proxy_socket_rejects_private_destination_before_connect():
    manager = network_manager.NetworkManager()
    with pytest.raises(network_manager.InterfaceBindingError, match="SSRF/DNS validation"):
        manager.create_bound_socket("lo", "127.0.0.1", 80)


def test_literal_ip_with_invalid_port_is_rejected():
    valid, error, _ = security_utils.validate_url_ssrf("http://8.8.8.8:99999/")
    assert valid is False
    assert "Invalid URL port" in error



def test_zero_url_port_is_rejected_instead_of_defaulting_to_port_80():
    valid, error, _ = security_utils.validate_url_ssrf("http://8.8.8.8:0/")
    assert valid is False
    assert "Invalid URL port: 0" in error



def test_proxy_round_robin_starts_with_first_active_interface():
    manager = network_manager.NetworkManager()
    manager.interfaces["first"] = network_manager.InterfaceStats(name="first", ip="192.0.2.10")
    manager.interfaces["second"] = network_manager.InterfaceStats(name="second", ip="192.0.2.11")
    from proxy import MultiWANProxyServer
    proxy = MultiWANProxyServer(manager)
    assert proxy._next_interface() == "first"
    assert proxy._next_interface() == "second"
    assert proxy._next_interface() == "first"
