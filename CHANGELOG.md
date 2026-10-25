# Changelog

## Unreleased

### Bug Fixes

- **collect:** bucket usage rows by hour so tokens land when they were spent
- **collect:** store only numeric values from toolStats
- **collect:** pin the store's host key so it can't drift with the network
- **skill-usage:** parse YAML block-scalar descriptions in frontmatter
- **skill-usage:** canonicalize namespaced skill ids in the render join
- **skill-usage:** follow symlinked skill dirs in the inventory scan

### Documentation

- **changelog:** regenerate to include the usage hour-bucket fix
- **collect:** note the limits of the hour-bucket timestamp max and cite the row count
- **working-memory:** document the hour field on usage rows
- **changelog:** regenerate for module 2
- **working-memory:** add the usage report to the output contracts
- **working-memory:** document the usage, agents, and stats streams as built
- rewrite the README around all four streams
- license the repo and write down what a contributor can't infer
- **working-memory:** settle the tool-vs-slash disjointness question
- **working-memory:** hydrate the six shared files
- design for module 2 (agents, models, tokens)
- title-case the README section headings
- add README

### Features

- **audit:** add a read-only audit subcommand cross-checking the streams
- **render:** add tokens by skill, cache economics, and skills by agent
- **render:** add the usage report with Models × Thread and Agent Types [BREAKING]
- **render:** add a Tokens column and stop counting agent runs as sessions
- **collect:** add the session-stats stream and stamp cc_version everywhere
- **collect:** record one row per subagent run
- **collect:** record token usage per model and skill
- **collect:** tag events with the thread that produced them
- ship the macOS launchd scheduling template and installer
- **skill-usage:** title-case the MOC section headings
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


