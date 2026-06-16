# Changelog

## [0.6.0](https://github.com/powerset-co/powerpacks/compare/powerpacks-console-v0.5.0...powerpacks-console-v0.6.0) (2026-06-16)


### Features

* button copy 'Process Contacts' with count on its own line (LinkedIn + Gmail) ([ba33487](https://github.com/powerset-co/powerpacks/commit/ba334870bc0b6f3e4d14b6425a0fa9a976e7643c))
* drop email step from /onboarding, move full wizard to /onboarding-v2 ([6ef5434](https://github.com/powerset-co/powerpacks/commit/6ef5434b14c7cf15a476ba3e0bfa889c181d82bc))
* drop email step from /onboarding, move full wizard to /onboarding-v2 ([f2d9f16](https://github.com/powerset-co/powerpacks/commit/f2d9f16b749cf5c141533b2882dd098fc63e54b9))
* incremental Parallel.ai cost estimate under Gmail Process button ([3102b97](https://github.com/powerset-co/powerpacks/commit/3102b97e574cddf91f8b524854910f05fd8493ed))
* show incremental Parallel.ai cost estimate under Gmail Process button ([11b87dc](https://github.com/powerset-co/powerpacks/commit/11b87dcd7efdd16e1a9358e1fdf21245c39dd48f))
* single "Process" button for Gmail (local enrich -&gt; Modal index) ([f482423](https://github.com/powerset-co/powerpacks/commit/f48242394ec8b1e099d0fcaea2d4538462286733))
* single Process button for Gmail (local enrich → Modal index) ([ebff403](https://github.com/powerset-co/powerpacks/commit/ebff403ac59984d8a60a6bee39b4dcf9f8f35c85))


### Bug Fixes

* don't treat stale (killed) Modal runs as in-progress ([318ea30](https://github.com/powerset-co/powerpacks/commit/318ea30ccf75f05804f3d49f98bbdb59f2ebaa11))
* don't treat stale (killed) Modal runs as in-progress ([ba40f44](https://github.com/powerset-co/powerpacks/commit/ba40f44a8ad2bc19af684f2ebd5ac5eafe0704a1))

## [0.5.0](https://github.com/powerset-co/powerpacks/compare/powerpacks-console-v0.4.0...powerpacks-console-v0.5.0) (2026-06-16)


### Features

* collapse BYO keys behind a chevron; open Codex via codex:// deeplink ([22d1ef5](https://github.com/powerset-co/powerpacks/commit/22d1ef5bb8fe4d858e86a53d3057813516bbaacf))
* double-line stepper (label stacked under each circle) ([4dab26c](https://github.com/powerset-co/powerpacks/commit/4dab26c8ed682d8c3722349eef046ec28ccdaa1b))
* GCP-free Modal setup — pull runtime keys from Powerset API ([2710c51](https://github.com/powerset-co/powerpacks/commit/2710c517b73ac1c9d3c2bf7cc1b0f5b975b19538))
* Gmail sync date-window + per-vertical source pages ([#67](https://github.com/powerset-co/powerpacks/issues/67)) ([a3f4176](https://github.com/powerset-co/powerpacks/commit/a3f4176641c1e793c2d3d151420e1dd53f585ce1))
* Gmail vault setup UI + onboarding consolidation + bin/launch ([#71](https://github.com/powerset-co/powerpacks/issues/71)) ([f6f4041](https://github.com/powerset-co/powerpacks/commit/f6f4041fb8094018e74ed166386c45facf380ddc))
* onboarding-v3 pulls runtime keys after Powerset login with progress ([8d523ff](https://github.com/powerset-co/powerpacks/commit/8d523ff540a8e7c231043644ddc113274a51b36a))
* onboarding-v3 wizard — Powerset login / BYO keys / first search ([b84e100](https://github.com/powerset-co/powerpacks/commit/b84e100b4605b6068b4b0fe8aeeb72991ab50f16))
* onboarding-v3 wizard (Powerset login / BYO keys / first search) ([59b5bfb](https://github.com/powerset-co/powerpacks/commit/59b5bfb07daef0fc481ed8d1b259045d5183afdb))
* per-vertical source pages with link-only + load-time auto-discover ([#68](https://github.com/powerset-co/powerpacks/issues/68)) ([b494869](https://github.com/powerset-co/powerpacks/commit/b494869207d6543c01474042f07141aa533c62fd))
* prefill Codex deeplink + mark import step complete in stepper ([837a726](https://github.com/powerset-co/powerpacks/commit/837a7267d79789cd425bc634eae7e455334bdbce))
* reuse company classification by LinkedIn slug + skip unresolved companies ([093d69c](https://github.com/powerset-co/powerpacks/commit/093d69c9a283a541aa45ae2b2c5ee5e99e44b5be))
* single "Process" button on LinkedIn source page runs Modal enrich+index ([b8eedea](https://github.com/powerset-co/powerpacks/commit/b8eedea8f9b0adb5542f39c2ea76c9f1d6ccaad0))
* single Codex launch button on first-search step ([24e1c60](https://github.com/powerset-co/powerpacks/commit/24e1c6027fe6306e5edd40127bd76634f22ef63b))


### Bug Fixes

* make local Gmail vault setup and per-account authorize/sync work end-to-end ([58bb39c](https://github.com/powerset-co/powerpacks/commit/58bb39c75fdd5a6d06aa723b9480ccffdf624d24))

## [0.4.0](https://github.com/powerset-co/powerpacks/compare/powerpacks-console-v0.3.0...powerpacks-console-v0.4.0) (2026-06-13)


### Features

* clearer file-attached state and standalone count/estimate on onboarding-v3 ([e0ddf2a](https://github.com/powerset-co/powerpacks/commit/e0ddf2acea12c66e0481716edacc693813af2f97))
* clearer file-attached state and standalone count/estimate on onboarding-v3 ([ab1d722](https://github.com/powerset-co/powerpacks/commit/ab1d722986b286422f7b05485504816014a4179b))
* LinkedIn connections.csv to searchable index on Modal (onboarding v3) ([bf1a679](https://github.com/powerset-co/powerpacks/commit/bf1a6793e688c27f6bf8437d24b80f3b778c8be1))
* onboarding-v3 console page for LinkedIn csv to cloud index ([2d3159b](https://github.com/powerset-co/powerpacks/commit/2d3159bcfdb0bf7f8e73faac61b926684a6414b8))
* persist setup job driver logs under .powerpacks ([2b26781](https://github.com/powerset-co/powerpacks/commit/2b26781079f700605b2618bd44c85db505f5f472))
* persist setup job driver logs under .powerpacks/runs/job-logs ([a32af20](https://github.com/powerset-co/powerpacks/commit/a32af201d83872529bf5cedc2a9951add654426c))
* timestamp each line in setup job driver logs ([4acc82d](https://github.com/powerset-co/powerpacks/commit/4acc82d5a68bd51ccdec1c4cf5e16722aa7212ab))
* timestamp each line in setup job driver logs ([f847f20](https://github.com/powerset-co/powerpacks/commit/f847f20615ab92988ca66c5f2c4c5b1f1ffa650c))


### Performance Improvements

* parallel cache classification and skip estimate pass for internal runs ([bf204de](https://github.com/powerset-co/powerpacks/commit/bf204debd0cbcf53ab6cec39e46c7aecb6d77fce))

## [0.3.0](https://github.com/powerset-co/powerpacks/compare/powerpacks-console-v0.2.0...powerpacks-console-v0.3.0) (2026-06-12)


### Features

* add contacts, profiles, company directory, and local search to console ([091722d](https://github.com/powerset-co/powerpacks/commit/091722dc762f53dbbab55742e73f28057e3a507b))
* add launchd daemon mode to the console run script ([d299848](https://github.com/powerset-co/powerpacks/commit/d299848b0498fa3f49106998cc963a3234bfb1d9))

## [0.2.0](https://github.com/powerset-co/powerpacks/compare/powerpacks-console-v0.1.0...powerpacks-console-v0.2.0) (2026-06-10)


### Features

* add repo-local pipeline reuse, incremental DuckDB indexing, and LinkedIn onboarding v2 ([#30](https://github.com/powerset-co/powerpacks/issues/30)) ([eae042d](https://github.com/powerset-co/powerpacks/commit/eae042d8c81efe68828e93585b1fdec95157b16f))
* Messages onboarding v2, accounts.json writeback, spend estimate fix ([a2d892e](https://github.com/powerset-co/powerpacks/commit/a2d892e00755218128dfaa8e8346b75fb2359a07))
* split JD search and improve search reranking ([#36](https://github.com/powerset-co/powerpacks/issues/36)) ([0ae2fd4](https://github.com/powerset-co/powerpacks/commit/0ae2fd49defbfc9eca5791ed6a346e771d0dfcc8))
