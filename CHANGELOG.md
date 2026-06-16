# Changelog

## [0.13.0](https://github.com/aigengame/cli-agentic-workflow/compare/v0.12.0...v0.13.0) (2026-06-16)


### Features

* **report:** name the blocker for a blocked-skipped node ([#94](https://github.com/aigengame/cli-agentic-workflow/issues/94)) ([#105](https://github.com/aigengame/cli-agentic-workflow/issues/105)) ([c67fa1d](https://github.com/aigengame/cli-agentic-workflow/commit/c67fa1d4a0f1d4bf86fe8b0422e8debc45224843))

## [0.12.0](https://github.com/aigengame/cli-agentic-workflow/compare/v0.11.0...v0.12.0) (2026-06-16)


### Features

* codex.exec adapter, capability-symmetric with claude.print ([#11](https://github.com/aigengame/cli-agentic-workflow/issues/11)) ([#101](https://github.com/aigengame/cli-agentic-workflow/issues/101)) ([477f15e](https://github.com/aigengame/cli-agentic-workflow/commit/477f15e5b3a3aaa15179d1b7311c0f925eb4586c))
* pattern controller infrastructure + run groups + loop-until-done ([#15](https://github.com/aigengame/cli-agentic-workflow/issues/15)) ([#104](https://github.com/aigengame/cli-agentic-workflow/issues/104)) ([3227b3e](https://github.com/aigengame/cli-agentic-workflow/commit/3227b3eae3c2a6cdccf5cd7778d8b9074f8a5fc0))

## [0.11.0](https://github.com/aigengame/cli-agentic-workflow/compare/v0.10.0...v0.11.0) (2026-06-16)


### Features

* harden when-predicate validation + unify the skip walk ([#75](https://github.com/aigengame/cli-agentic-workflow/issues/75), [#77](https://github.com/aigengame/cli-agentic-workflow/issues/77)) ([#99](https://github.com/aigengame/cli-agentic-workflow/issues/99)) ([7061f41](https://github.com/aigengame/cli-agentic-workflow/commit/7061f41e8cd42b19bbb1a6c6b84b8b370c1121bc))
* shared SubprocessAdapter base + first-class adapter-determined failure ([#83](https://github.com/aigengame/cli-agentic-workflow/issues/83), [#84](https://github.com/aigengame/cli-agentic-workflow/issues/84)) ([#98](https://github.com/aigengame/cli-agentic-workflow/issues/98)) ([35926d8](https://github.com/aigengame/cli-agentic-workflow/commit/35926d8706aa79be7ff7df121a890a57ad38fbdc))

## [0.10.0](https://github.com/aigengame/cli-agentic-workflow/compare/v0.9.0...v0.10.0) (2026-06-16)


### Features

* pattern expander infrastructure, pipeline/parallel, and caw init/patterns ([#8](https://github.com/aigengame/cli-agentic-workflow/issues/8)) ([#96](https://github.com/aigengame/cli-agentic-workflow/issues/96)) ([a6f0043](https://github.com/aigengame/cli-agentic-workflow/commit/a6f0043d05b805735d72a3d17af27b520fb7a5c7))

## [0.9.0](https://github.com/aigengame/cli-agentic-workflow/compare/v0.8.0...v0.9.0) (2026-06-15)


### Features

* **env:** shell-node env allow-list + adapter/output-contract cleanup ([#66](https://github.com/aigengame/cli-agentic-workflow/issues/66), [#67](https://github.com/aigengame/cli-agentic-workflow/issues/67)) ([#92](https://github.com/aigengame/cli-agentic-workflow/issues/92)) ([7bd07a0](https://github.com/aigengame/cli-agentic-workflow/commit/7bd07a0cb18572c0c5e126f5b7c277804ee50c68))

## [0.8.0](https://github.com/aigengame/cli-agentic-workflow/compare/v0.7.0...v0.8.0) (2026-06-15)


### Features

* reporters and `caw report` in four formats ([#12](https://github.com/aigengame/cli-agentic-workflow/issues/12)) ([#90](https://github.com/aigengame/cli-agentic-workflow/issues/90)) ([604bcba](https://github.com/aigengame/cli-agentic-workflow/commit/604bcba1f5fd5983fab65bf979d19c52c39a989c))


### Bug Fixes

* refuse resume of pre-cause State schema with actionable error ([#91](https://github.com/aigengame/cli-agentic-workflow/issues/91)) ([720071b](https://github.com/aigengame/cli-agentic-workflow/commit/720071bf38e45c28e6455c8bbda7f85741fd9ed6)), closes [#76](https://github.com/aigengame/cli-agentic-workflow/issues/76)

## [0.7.0](https://github.com/aigengame/cli-agentic-workflow/compare/v0.6.0...v0.7.0) (2026-06-15)


### Features

* claude.print adapter for claude -p ([#80](https://github.com/aigengame/cli-agentic-workflow/issues/80)) ([9b5198f](https://github.com/aigengame/cli-agentic-workflow/commit/9b5198ff6f6360d968453f6ecb66481837069003))

## [0.6.0](https://github.com/aigengame/cli-agentic-workflow/compare/v0.5.1...v0.6.0) (2026-06-13)


### Features

* node `when` predicates and skip semantics ([#7](https://github.com/aigengame/cli-agentic-workflow/issues/7)) ([#74](https://github.com/aigengame/cli-agentic-workflow/issues/74)) ([7e84822](https://github.com/aigengame/cli-agentic-workflow/commit/7e84822de8a9401386d3324af375c2a5e0ebfcf6))

## [0.5.1](https://github.com/aigengame/cli-agentic-workflow/compare/v0.5.0...v0.5.1) (2026-06-13)


### Bug Fixes

* **executor:** verify snapshot checksum and translate adapter errors on resume ([#70](https://github.com/aigengame/cli-agentic-workflow/issues/70)) ([#72](https://github.com/aigengame/cli-agentic-workflow/issues/72)) ([6bfc0cb](https://github.com/aigengame/cli-agentic-workflow/commit/6bfc0cbcbf7139ab106a9623c743858d0c62fc45))

## [0.5.0](https://github.com/aigengame/cli-agentic-workflow/compare/v0.4.0...v0.5.0) (2026-06-13)


### Features

* failure semantics — retries, timeouts, cancellation, and resume ([#69](https://github.com/aigengame/cli-agentic-workflow/issues/69)) ([fdf7344](https://github.com/aigengame/cli-agentic-workflow/commit/fdf7344b66d1b9e92d4a5477e6ff69b523fdecd9))

## [0.4.0](https://github.com/aigengame/cli-agentic-workflow/compare/v0.3.0...v0.4.0) (2026-06-13)


### Features

* Adapter interface, agent nodes, mock adapter, output contracts, env policy ([#60](https://github.com/aigengame/cli-agentic-workflow/issues/60)) ([f9bbdfc](https://github.com/aigengame/cli-agentic-workflow/commit/f9bbdfcc1b02ca73de6e6c794b23fb9e3d900ae4))

## [0.3.0](https://github.com/aigengame/cli-agentic-workflow/compare/v0.2.1...v0.3.0) (2026-06-13)


### Features

* parallel scheduling and joins in the local executor ([#53](https://github.com/aigengame/cli-agentic-workflow/issues/53)) ([320da11](https://github.com/aigengame/cli-agentic-workflow/commit/320da118bacbd65eb889a7c4455085c078fca3d7))

## [0.2.1](https://github.com/aigengame/cli-agentic-workflow/compare/v0.2.0...v0.2.1) (2026-06-13)


### Bug Fixes

* **build:** keep uv.lock in sync on release so CI passes ([#48](https://github.com/aigengame/cli-agentic-workflow/issues/48)) ([fac224d](https://github.com/aigengame/cli-agentic-workflow/commit/fac224d050fab2b7c6a75866ba30b22f32fd9689))

## [0.2.0](https://github.com/aigengame/cli-agentic-workflow/compare/v0.1.0...v0.2.0) (2026-06-13)


### Features

* caw validate and caw graph for multi-node workflows ([#44](https://github.com/aigengame/cli-agentic-workflow/issues/44)) ([fddc872](https://github.com/aigengame/cli-agentic-workflow/commit/fddc872cb99958c4829b4c5b7bc99e7c204d94e9))
