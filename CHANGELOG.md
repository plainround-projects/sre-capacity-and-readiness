# Changelog

All notable changes to this project are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Drawn architecture and flow figures in `docs/diagrams/`, light and dark, shown in the README.

## [0.1.0] - 2026-10-27

### Added

- A service whose requests burn real CPU, run as eight containers with a fixed 1.0 CPU in three zones behind Envoy, with a shared ledger service and PostgreSQL.
- An open-loop load generator and the stepped knee test, the soak at the safe load, the capacity plan with headroom for a lost zone, and the lost-zone and projected-peak fleet runs.
- The next-bottleneck experiment: the ledger's connection pool swept from the naive size upward.
- Rollout and rollback under load, a frozen-dependency test and a backup and restore test, each writing its evidence to `docs/evidence/`.
- Generated SLO burn-rate rules on the Envoy edge, a fleet-capacity alert tied to the plan, tested with `promtool`.
- A readiness review that gives every item PASS, FAIL or ACCEPTED RISK with an owner and writes the one-page launch decision.
