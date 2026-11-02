import pytest
import yaml

from capacity.application import rules
from capacity.application.experiments import fleet_instances, spare_instances
from capacity.domain.cpu import cpu_work
from capacity.domain.latency import Sample
from capacity.infrastructure.settings import InstanceSettings, LedgerSettings, SettingsError
from capacity.infrastructure.slo_files import render_fleet_files, render_rule_files


class TestSettings:
    def test_an_instance_has_defaults_and_no_dependency_unless_told(self):
        settings = InstanceSettings.from_env({})
        assert (settings.zone, settings.version, settings.work_rounds, settings.ledger_url) == ("a", "v1", 3000, None)

    def test_the_ledger_url_loses_its_trailing_slash(self):
        assert InstanceSettings.from_env({"LEDGER_URL": "http://ledger:8082/"}).ledger_url == "http://ledger:8082"

    def test_the_ledger_needs_a_database_url(self):
        with pytest.raises(SettingsError, match="DATABASE_URL"):
            LedgerSettings.from_env({})

    @pytest.mark.parametrize(
        ("name", "value"), [("POOL_SIZE", "0"), ("POOL_SIZE", "200"), ("HOLD_MS", "-1"), ("POOL_TIMEOUT_S", "x")]
    )
    def test_bad_ledger_values_are_refused_by_name(self, name, value):
        with pytest.raises(SettingsError, match=name):
            LedgerSettings.from_env({"DATABASE_URL": "postgresql://u:p@db/x", name: value})

    def test_bad_instance_values_are_refused(self):
        with pytest.raises(SettingsError, match="WORK_ROUNDS"):
            InstanceSettings.from_env({"WORK_ROUNDS": "0"})


class TestCpuWork:
    def test_the_same_rounds_give_the_same_digest(self):
        assert cpu_work(50) == cpu_work(50)

    def test_more_rounds_change_the_digest(self):
        assert cpu_work(50) != cpu_work(51)

    def test_it_is_a_sha256_hex_digest(self):
        assert len(cpu_work(10)) == 64


class TestRules:
    def test_expressions_read_the_envoy_edge_not_the_instances(self, spec):
        availability = rules.error_ratio_expr(spec.slos[0], "5m")
        assert "envoy_http_downstream_rq_xx" in availability
        assert 'envoy_response_code_class="5"' in availability
        assert "app_requests_total" not in availability

    def test_the_latency_threshold_is_the_edge_bucket_at_fifty_milliseconds(self, spec):
        expression = rules.error_ratio_expr(spec.slos[1], "5m")
        assert 'le="50"' in expression
        assert "envoy_http_downstream_rq_time_count" in expression

    def test_a_threshold_that_is_not_an_edge_bucket_is_refused(self):
        with pytest.raises(ValueError, match="no histogram bucket"):
            rules.bucket_label(40)

    def test_the_committed_rule_files_are_what_the_generator_produces(self, spec, root):
        for path, text in render_rule_files(spec).items():
            assert (root / path).read_text() == text

    def test_the_blind_spot_alert_watches_the_edge_metric(self, spec):
        document = rules.build_rules(spec.slos, spec.alerts)
        absent = next(r for g in document["groups"] for r in g["rules"] if r.get("alert") == "EdgeMetricsAbsent")
        assert absent["expr"] == "absent(envoy_http_downstream_rq_xx)"


def test_sample_is_a_plain_value_object():
    assert Sample(1.0, 0.01, True) == Sample(1.0, 0.01, True)


class TestFleetFiles:
    def test_the_alert_thresholds_and_the_replay_follow_the_plan(self):
        files = {path.name: text for path, text in render_fleet_files(3, 5, 3).items()}
        rules = yaml.safe_load(files["fleet.rules.yml"])["groups"][0]["rules"]
        assert [rule["expr"].rsplit("< ", 1)[1] for rule in rules] == ["3", "5"]
        replay = yaml.safe_load(files["fleet.test.yml"])
        assert replay["tests"][0]["input_series"][0]["values"] == "5x9 3x39 5x30"
        assert replay["tests"][1]["input_series"][0]["values"] == "5x9 2x39 5x30"

    def test_the_spares_are_the_instances_compose_defines_and_the_plan_does_not_run(self):
        assert fleet_instances((3, 3, 2))[-1] == "app-c2"
        assert spare_instances((3, 3, 2)) == ["app-c3"]
        assert len(spare_instances((2, 2, 1))) == 4
