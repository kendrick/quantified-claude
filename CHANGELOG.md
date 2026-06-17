# Changelog

## Unreleased

### Bug Fixes

- **skill-usage:** parse YAML block-scalar descriptions in frontmatter
- **skill-usage:** canonicalize namespaced skill ids in the render join
- **skill-usage:** follow symlinked skill dirs in the inventory scan

### Documentation

- add README

### Features

- **skill-usage:** render channel columns and links, wire the collect/render CLI
- **skill-usage:** compose the collect data flow in run_collect
- **skill-usage:** add the events-store persistence and aggregation layer
- **skill-usage:** add session-keyed merge for the durable events store
- **skill-usage:** honor --since cutoff and lock name-extraction edge cases
- **skill-usage:** filter built-in CLI commands from the slash channel
- **skill-usage:** harvest both tool and slash invocation channels

### Refactoring

- rename skill-usage-moc.py to skill_usage.py

## v2026.06 (2026-06-17)

### Features

- **skill-usage:** seed design doc and reference script


