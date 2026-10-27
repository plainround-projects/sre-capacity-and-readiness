"""The command-line programs and the file adapters, against real files and sockets."""

import asyncio
import json
import shutil
from datetime import date

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from capacity.infrastructure import compose
from capacity.infrastructure.metrics import InstanceMetrics
from capacity.infrastructure.ops import build_ops_app
from capacity.infrastructure.project_files import FileSystemProject, JsonEvidence, SloFilesCatalog, YamlChecklist
from capacity.infrastructure.settings import InstanceSettings
from capacity.interfaces import capacity_cli, instance_app, loadtest_cli, slo_cli

pytestmark = pytest.mark.integration


class TestAdapters:
    def test_evidence_round_trips_and_a_missing_experiment_is_none(self, tmp_path):
        evidence = JsonEvidence(tmp_path / "evidence")
        assert evidence.load("knee") is None
        evidence.save("knee", {"safe_rps": 375})
        assert evidence.load("knee") == {"safe_rps": 375}

    def test_the_project_files_read_and_glob_relative_paths(self, tmp_path):
        (tmp_path / "dashboards").mkdir()
        (tmp_path / "dashboards" / "a.json").write_text("{}")
        files = FileSystemProject(tmp_path)
        assert files.exists("dashboards/a.json")
        assert files.glob("dashboards/*.json") == ["dashboards/a.json"]
        assert files.read("dashboards/a.json") == "{}"
        assert files.root == tmp_path

    def test_the_checklist_reads_owners_dates_and_accepted_risks(self, root):
        checklist = YamlChecklist(root / "readiness" / "checklist.yaml")
        assert checklist.owner("restore") == "database on-call"
        assert checklist.due("restore") == date(2026, 11, 3)
        assert checklist.blocking("runbooks") is True
        assert checklist.blocking("restore") is False
        assert {risk["area"] for risk in checklist.accepted_risks()} >= {"Autoscaling"}

    def test_the_slo_catalog_reports_whether_the_committed_rules_are_current(self, root, tmp_path):
        assert SloFilesCatalog(root).rules_in_sync()
        shutil.copytree(root / "slo", tmp_path / "slo")
        (tmp_path / "slo" / "generated" / "rules.yml").write_text("stale")
        assert not SloFilesCatalog(tmp_path).rules_in_sync()


class TestCompose:
    async def test_healthy_members_reads_envoys_stats_line(self, monkeypatch):
        async def stats(_):
            return web.Response(text="cluster.app.membership_healthy: 5\n")

        app = web.Application()
        app.router.add_get("/stats", stats)
        server = TestServer(app)
        await server.start_server()
        try:
            monkeypatch.setattr(compose, "HEALTHY_MEMBERS_URL", f"http://127.0.0.1:{server.port}/stats")
            assert await asyncio.to_thread(compose.healthy_members) == 5
            await asyncio.to_thread(compose.wait_for_members, 5, 2.0)
            with pytest.raises(TimeoutError):
                await asyncio.to_thread(compose.wait_for_members, 8, 1.0)
        finally:
            await server.close()

    def test_an_unreachable_balancer_counts_as_zero_healthy(self, monkeypatch):
        monkeypatch.setattr(compose, "HEALTHY_MEMBERS_URL", "http://127.0.0.1:9/stats")
        assert compose.healthy_members() == 0

    async def test_versions_seen_reads_the_version_header(self):
        async def work(_):
            return web.json_response({}, headers={"X-Version": "v2"})

        app = web.Application()
        app.router.add_get("/work", work)
        server = TestServer(app)
        await server.start_server()
        try:
            assert await asyncio.to_thread(compose.versions_seen, f"http://127.0.0.1:{server.port}", 3) == {"v2"}
        finally:
            await server.close()


async def test_the_load_test_cli_runs_a_fleet_report_against_a_real_instance(capsys):
    settings = InstanceSettings("a", "a1", "v1", 100, None, 0.3, 0, 0, "INFO")
    metrics = InstanceMetrics("a", "a1")
    server = TestServer(instance_app.build_app(settings, metrics))
    ops = TestServer(build_ops_app(metrics.render))
    await server.start_server()
    await ops.start_server()
    try:
        argv = [
            "fleet",
            "--target",
            f"http://127.0.0.1:{server.port}",
            "--rps",
            "50",
            "--seconds",
            "2",
            "--label",
            "t",
            "--window",
            "1",
        ]
        await asyncio.to_thread(loadtest_cli.main, argv)
        report = json.loads(capsys.readouterr().out)
        assert (report["experiment"], report["label"], report["requests"], report["errors"]) == ("fleet", "t", 100, 0)
        knee_argv = [
            "knee", "--target", f"http://127.0.0.1:{server.port}", "--ops", f"http://127.0.0.1:{ops.port}",
            "--steps", "10,20,30,40", "--step-seconds", "0.5",
        ]  # fmt: skip
        await asyncio.to_thread(loadtest_cli.main, knee_argv)
        knee = json.loads(capsys.readouterr().out)
        assert knee["experiment"] == "knee"
        assert len(knee["steps"]) == 4
    finally:
        await server.close()
        await ops.close()


class TestCapacityCli:
    def project(self, tmp_path, root, evidence):
        shutil.copytree(root / "slo", tmp_path / "slo")
        shutil.copytree(root / "readiness", tmp_path / "readiness")
        (tmp_path / "docs").mkdir()
        for name, document in evidence.items():
            JsonEvidence(tmp_path / "docs" / "evidence").save(name, document)
        return tmp_path

    def run(self, argv):
        with pytest.raises(SystemExit) as exit_info:
            capacity_cli.main(argv)
        return exit_info.value.code

    def test_the_plan_command_sizes_the_fleet_from_the_measured_safe_load_and_saves_it(self, tmp_path, root, capsys):
        project = self.project(tmp_path, root, {"knee": {"safe_rps": 400}, "fleet-zone-loss": {"within_slo": True}})
        assert self.run(["plan", "--evidence", str(project / "docs" / "evidence")]) == 0
        plan = JsonEvidence(project / "docs" / "evidence").load("plan")
        assert plan is not None
        assert (plan["instances_for_peak"], plan["total_instances"], plan["verified_by_lost_zone_run"]) == (5, 8, True)
        assert "ceil(2000 / 400) = 5" in capsys.readouterr().out

    def test_the_plan_command_refuses_to_guess_without_a_measurement(self, tmp_path):
        with pytest.raises(SystemExit, match="no safe load"):
            capacity_cli.main(["plan", "--evidence", str(tmp_path)])

    def test_the_review_command_prints_the_checklist_and_writes_the_decision(self, tmp_path, root, capsys):
        project = self.project(tmp_path, root, {})
        shutil.copytree(root / "dashboards", project / "dashboards")
        shutil.copytree(root / "docs" / "runbooks", project / "docs" / "runbooks")
        shutil.copy(root / "docs" / "ownership.md", project / "docs" / "ownership.md")
        (project / "slo" / "tests").mkdir(exist_ok=True)
        assert self.run(["review", "--root", str(project), "--write"]) == 0
        output = capsys.readouterr().out
        assert "Readiness review:" in output
        assert "Backup and restore" in output
        assert "Decision: " in output
        text = (project / "docs" / "launch-decision.md").read_text()
        assert text.startswith("# Launch decision")
        assert "restore not yet tested" in text

    def test_the_report_command_needs_all_the_evidence(self, tmp_path):
        with pytest.raises(SystemExit, match="missing evidence"):
            capacity_cli.main(["report", "--root", str(tmp_path)])

    def test_the_rules_command_detects_drift_and_writes(self, monkeypatch, tmp_path, root):
        (tmp_path / "slo").mkdir()
        shutil.copy(root / "slo" / "slos.yaml", tmp_path / "slo" / "slos.yaml")
        monkeypatch.chdir(tmp_path)
        for argv, expected in ((["rules", "--check"], 1), (["rules"], 0), (["rules", "--check"], 0)):
            with pytest.raises(SystemExit) as exit_info:
                slo_cli.main(argv)
            assert exit_info.value.code == expected
