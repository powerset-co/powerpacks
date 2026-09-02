# Trait extraction redesign: N person-traits, no buckets

Created: 2026-09-02

Change log:
- 2026-09-02 (judgeable): owner pushback on run 2 — "super hand-wavy — what is
  'someone who wants to be a founder', how do we evaluate that?" The shared
  core of all six traits prompts gained a **PROFILE TEST** (every trait is
  written as the career-profile evidence that proves it; "If you cannot name
  the profile line that would prove it, it is not a trait"). Run 3 below grades
  every trait on judgeability by hand; the run-2 table is kept as notes.
- 2026-09-02 (shipped + eval): the §3 contract landed on `feat/pond-trait-layering`
  with one amendment from the product owner — traits come from a **second,
  per-family model call** (`prompts/traits.txt`, `prompts/families/<family>/traits.txt`,
  resolved by `pond_prompts.load_pond_prompt(brief, "traits")` from the plan
  call's `pond_prompt_family`), each file = the §4 core rules + a universal
  fluff list + a short family block. The plan call's prompt is
  `build_eval_inputs.PLAN_SYSTEM` (standalone; no trait instructions; seven
  hint kinds). §4 below is the draft the shipped prompts were built from; the
  files are authoritative. Eval results are in "Eval run" at the end.
- 2026-09-02 (later): the exhaustive engine (judge, triage, core gate) and the
  Reflect bench were deleted, so every "exhaustive-only reader" named below is
  gone; the contract change now touches only plan extraction, validation, the
  Pond-1 prompt context, and the company-fit panel.
- 2026-09-02: first draft. Read-only analysis; nothing run, nothing edited in the repo.

Paths: `BEI` = `packs/search/primitives/deep_search/build_eval_inputs.py`; `TG` = `packs/search/primitives/expand_search_request/prompts/trait_generation.txt`; `DS` = `packs/search/primitives/deep_search/`.

## 0. The one-line diagnosis

The JD is parsed by a prompt written for **search queries** (`TG:4` "Given a search query"), where every noun is user intent. In a JD most nouns are the company describing itself. The adapter then orders the model to "always preserve" the industry (`BEI:151-152`) and to fill four Core slots (`BEI:92`), and the shipped default path flattens all of it into one string anyway (`DS/search_harness.py:515-519`).

## 1. Per-JD table

"CD" = company-derived (product / customers / mission / stack), the contamination the recruiter rejected. Full predicted traits with quotes are in §5; this column is the short form.

| JD | Current `must_have` (trimmed) | Current nice / hints that are CD or noise | Jake's thesis + judgment | New person-traits (short) |
|---|---|---|---|---|
| **agentmail** Founding Eng | core: "building APIs for both human and AI agents"; "designing low-latency distributed systems". table_stakes: backend/infra; system-design-to-rollout ownership; **"clear communication skills"**; production software at tech co/startup | CD: "APIs for … AI agents" (the product is email-for-agents); nice "messaging or email systems", "building for AI/LLM use cases" (product). Soft: communication | No Jake compression. Nearest analog is Pylon Underwriting: "a software engineer … who can do serious backend work"; rank on backend/distributed depth, startup fit, move plausibility | 1 backend/infra systems engineer · 2 owns projects design→prod · 3 shipped production software at a tech co/startup |
| **fortuna** Sr Ops Associate | 4 core: **"research and interpret Medicaid eligibility rules … across states"**; "building scalable operational processes, repeatable playbooks, and state-expansion workflows"; "analytical experimentation … hypotheses, tests"; "translate operational workflows and policy logic into product requirements" | CD: core #1 (Medicaid = the customer domain); ranking-boost "Medicaid policy and enrollment domain experience"; capability-adjacent pond "Medicaid policy analyst". Demoted to nice: the actual background "1-2+ years in consulting, IB, or a startup". Soft ×5: attention to detail, product mindset, ownership, velocity, "respectful disagreement / low-ego" | "An early-career, high-upside generalist in New York who can build operating playbooks." Feeder careers are the pond. Judge: early consulting/IB tenure, real startup operating role, playbook building + ownership, employer/school as priors, still motivated for an ambiguous role (seniors are a down-move) | 1 bg: 1-2 yrs consulting / IB / startup ops · 2 builds operating processes & playbooks from ambiguity · 3 analytical: hypothesis→test→data |
| **icarus** Sr Thermal Eng | 4 core: thermal design of flight hardware analysis→test→flight; radiation-dominated / low-convection environments; battery thermal management; hands-on TVAC/altitude-chamber test | CD: ranking-boost **"High-altitude solar-powered aircraft engineer"** quoted from the company overview ("We build solar-powered aircraft that fly at 60,000 ft"). Stack: "Thermal Desktop, ANSYS, Simcenter", "Python or MATLAB". Soft: "ownership, rapid troubleshooting, field execution". Demoted to nice: the degree | "A thermal engineer, most plausibly trained in mechanical or aerospace engineering." Judge: direct thermal title or repeated thermal responsibility; mech/aero education; heat-transfer/thermal-systems evidence; distinguish from chem-eng/controls/fluids; level + employer size → move plausibility | 1 thermal design of flight/space hardware taken through test · 2 bg: mech/aero engineering degree · 3 radiation-dominated environments (space/high-alt/UAV) · 4 battery thermal management |
| **latchbio** SWE | 4 core: systems fundamentals (distributed, DB, OS, compilers, PL); **"infrastructure for data ingestion, evaluation, grading"**; systems/process-engineering approach to tooling; shipped products people love | CD: core #2 nouns are the product pipeline; nice "agent infrastructure, model benchmarking", **"computational biology, genomics, biotech"**, "interest in using AI … to accelerate biological research"; ranking-boost **"Computational biology and frontier AI benchmarking specialist"** quoted from "We build the benchmarks…" | No Jake compression. The JD's own "Signs of strong fit" is the person list. Analog: tldraw + Pylon Underwriting: generalist systems engineer, product ownership | 1 systems-level software engineer · 2 designed and shipped products people love / owns a product area · 3 systems-engineering approach to tooling and data systems |
| **leadoptik** Mfg Technician | **6 core** (5 JD + 1 user): optical/fiber-optic assembly; precision manufacturing; micro-scale alignment; fiber handling & bonding; fine-motor under microscope; functional/performance test. table_stakes: drawings; cleanroom | Six "cores" are one craft restated. Nice: "ISO 13485 or FDA-regulated" (industry), "catheters, endoscopes … medical devices" (product), "power meters" (tool) | No Jake compression. Stable literal occupation ("optical assembly technician"); rank on hands-on years, micro-alignment, test | 1 precision fiber-optic assembly (alignment, handling, bonding, microscope) · 2 functional/performance test of optical assemblies + data · 3 bg: optics/photonics/EM-tech certificate or equivalent hands-on · 4 works to and drafts MPIs / drawings |
| **lovable** Staff/Principal Design Eng | 4 core: ship polished landing pages; React frontend fundamentals; "design engineering craft: translating visual explorations…"; advanced interactions/micro-animations "while balancing speed with polish" | Four cores are two halves (design, code) paraphrased four ways; "landing pages" twice. Soft: "balancing speed with polish" (Jake: "generic judgment/tradeoff language … discarded"). Stack: WebGL. Level leak: "10+ years", "Staff- or Principal-level" in nice | "A designer who can code, ideally in New York." Judge: product designer/design lead **and** frontend engineer history; design systems with implementation ownership; founder/studio spanning both. Negatives: engineer with vague design adjacency; chip/mechanical "design engineer"; unlikely relocation; funded active founder | 1 frontend engineer shipping production web UI (React/TS as medium) · 2 design craft — designs interactions, eye for detail · 3 bg: portfolio of shipped landing pages / advanced interactions |
| **spectral** Research Eng RL | 4 core: RL pipelines; **"reward functions for spatial or downstream engineering tasks"**; ML theory; "generative CAD, 3D, embodied AI, image/video/world modeling" | CD: core #2 (spatial/CAD = the product); ranking-boost **"CAD foundation-model domain experience"** quoted from "power our next generation of CAD foundation models". Noise: "collaborate effectively", "objectively impressive achievements", "high GPA" | "An engineer who is likely to have meaningful reinforcement-learning experience." Capability beats title. Two bands: direct RL evidence / exceptional at high-talent org where RL is plausible. Academics supportive, not a gate | 1 built RL pipelines / RL training · 2 ML depth demonstrated through background · 3 runs experiments scientifically · 4 (last) bg: 3D / embodied-AI / world-model experience — named as candidate experience, a technique not a market |
| **tldraw** Product Eng | 4 core: **"React, Next.js, Node.js, and TypeScript"** (stack list as core); product-focused frontend; full-stack; **"SDK or platform engineering for developer-facing products"** | CD: core #4 elevates a "strong plus" that is the company's product (an SDK); ranking-boost **"Creative tooling and collaborative web product"** quoted from "building the go-to infinite canvas primitive"; nice "maintaining developer documentation … SDK usage patterns" (product). Soft ×4 | "A generalist software engineer in London with product sense and taste." Judge: former founder/founding eng; ownership of core features; consumer/product-led co; generalist not silo. Negatives: narrow infra/auth/security/research; PhD-heavy ML; too senior | 1 frontend-leaning full-stack web engineer · 2 shipped meaningful features and shaped the product · 3 craft: obsessive attention to detail in products |
| **listen** MTS Platform | 3 core: end-to-end across LLM pipeline/infra/backend/product; **"complex AI-native systems … at significant scale"**; "frontier LLM experimentation, evaluation, or systems integration" | CD: core #2 from the company TL;DR ("build a complex AI-native product") + "massive scale"; ranking-boost **"AI-native human-preference product builder"** from "We're the bridge between AI systems and what humans actually want"; tool-culture **"Compiler engineer"** from a culture sentence. Soft ×5. Location null although Jake says SF | Jake: "engineers in SF, rank by the JD." | 1 end-to-end engineer owning a product slice across infra/backend/UX · 2 bg: future or past founder · 3 has built with LLMs · 4 (last) highly technical / systems depth |
| **pylon** MTS API | 3 core: GraphQL/API product design (semantic modeling, docs, versioning, DX); event-driven + workflow backends; **"model complex, nonlinear, path-dependent business workflows"** | CD: core #3 is the mortgage-domain paragraph ("It's path-dependent … nonlinear") rewritten as a capability; tool-culture pond **"AI infrastructure engineers"** from a tech-stack bullet. Stack: TypeScript/NestJS, PostgreSQL, Temporal.io as three nice traits | Jake: "backend or full-stack engineers in SF Bay Area; API/GraphQL/event-driven/workflow as ranking." | 1 backend engineer · 2 designs and owns an API as a product (versioning, breaking changes, DX) · 3 tool: GraphQL — the JD says the GraphQL API *is* the product · 4 event-driven / workflow architecture |
| **listen** Founding Research Scientist | 4 core: research record in LLMs/post-training/RLHF/behavioral modeling/simulation; **"human preference and multi-agent behavior simulation expertise"**; trains models, evals, production; "frontier-level research impact … publications" | CD: core #2 is the company's "Research Challenges" section; capability-adjacent pond **"Behavioral simulation researcher"** quoted from "We simulate human behavior at scale". Core #1 and #4 are the same trait. Nice: "genuine interest in human decision-making" (attitude), "frontier AI lab" (that is the JD's stated equivalence for #1, not a separate trait) | Jake: pond 1 "research scientist with behavioral modeling" fine; pond 2 broad technical roles at frontier labs; pond 3 academia; rerank on research relevance, seniority, ownership, hireability | 1 bg: published in LLMs/post-training/RLHF/behavioral modeling/simulation, or equivalent frontier-lab impact · 2 hands-on: trains models, writes evals, ships to production · 3 sets own research agenda |

Pattern across the 11: 8 plans have exactly 4 core (the cap), one has 6; every plan carries 3-8 nice-to-haves of which most are soft skills or stack; 6 of the 9 new-format plans carry a `ranking-boost` or `tool-culture` hint quoted from the company overview or tech-stack section.

## 2. Why the current prompt does this

### (a) Company / customer / product domain becomes a trait

- `TG:4` "Given a search query, generate traits" — the policy is a query parser. `BEI:83-85` then says "Apply it to the job description." In a query, "fintech" is what the user wants; in a JD, "fintech" is the About section.
- `TG:13-16` "When the query names a specialized industry:sector or business type:sector, generate the industry context + relevant role type within that industry."
- `TG:46` "`<IC role> at <sector> companies` → `<IC role> experience` + `Has worked at a <sector> company`" and `TG:55` "when the query says 'experience in <domain>', the domain is a requirement." Example `TG:237-241` → "Healthcare experience". This is the literal template for "Backend Engineer with fintech experience".
- `BEI:90` Core = "domain-defining, evidence-checkable capabilities".
- `BEI:151-152` `ranking-boost`: "the industry or domain served by the role; **always preserve it when stated**"; `BEI:168-170` "verify independently that the output preserves every … ranking-only domain … Do not trade one category away to include another." The model is told it has failed if the industry is absent. It then reaches into the About section for a quote (icarus, latchbio, tldraw, listen ×2, spectral).
- The only readers of `ranking-boost` are exclusion sets: `DS/network_floors.py:27`, `DS/search_harness.py:1574-1576`. Nothing ranks on it. But `DS/decompose_jd.py:88-89` tells the pond generator "Ranking-boost hints may shape ordering or **an experience clause**" — the sanctioned path for the industry to reach the retrieval query.

### (b) Stack / tool lists become traits

- `TG:10-12` "When the query names a specific framework:technology or language:technology, generate both the specific skill AND the broader domain." A JD's "React / Next / Node / TypeScript required" fires this (tldraw core #1).
- `TG:146-147` is the counter-rule but is gated on "the query makes that exact technology independently central" — a "Tech Stack" heading reads as exactly that. Result: pylon nice ×3 (TypeScript/NestJS, PostgreSQL, Temporal.io), icarus nice ×2 (Thermal Desktop/ANSYS, Python/MATLAB), lovable "WebGL".
- `BEI:142-143` `tool-culture`: "an optional or supporting professional medium or tool signals a neighboring source occupation" — a stack bullet became "AI infrastructure engineers" (pylon) and a culture sentence became "Compiler engineer with language-design expertise" (listen MTS).

### (c) Generic soft skills become traits

- `BEI:94-95` "`nice_to_have` contains every non-Core evidence preference, **including generic leadership, communication, mentoring, strategic thinking, and management requirements**." The prompt asks for them by name.
- `TG:168` "Capture every independent criterion"; `TG:161` "usually return 4-8 non-redundant traits." Fortuna's five culture headings each became a trait; agentmail has "clear communication skills" as a must_have.

### (d) The must / nice / core_groups split adds nothing for ranking

- Quota, not judgment: `BEI:91-93` "at most 4 … Most roles have 1-3." Eight of eleven plans are exactly 4; leadoptik's six one-craft cores show the model splitting to fill. `BEI:439-441` silently demotes the overflow to nice, so tier is an artifact of emission order.
- `core_groups` are mechanical: `DS/plan_filters.py:109-113` takes ceil(2n/3)-combinations of the core list. No JD content enters. Fortuna's four "core paths" are four ways to say "3 of 4".
- Shipped path ignores the split: default mode is `simple` (`DS/deep_search_loop.py:465-466`). The simple harness concatenates all core traits into one string `brief.defining_capability` (`DS/search_harness.py:515-519`). `nice_to_have` has no reader outside exhaustive-mode `triage_candidates.py:82-101` and the exhaustive seed context `decompose_jd.py:100-119`; `core_groups` is read only by `judge_consensus.py:58-72` (exhaustive core-gate, `deep_search_loop.py:878`) and that triage prompt. `micro_sort_shortlist.py:215-216` and `expand_from_anchor.py:74-80` also flatten to core text.
- In exhaustive mode it is worse than useless: `decompose_jd.py:117` "Every core group and must-have needs explicit probe coverage" — traits drive retrieval, which the product owner forbids; and the core-gate is a gate.

## 3. Proposed contract

```json
"traits": [
  {"trait": "…", "kind": "capability|background|tool", "evidence_quote": "exact contiguous JD text"}
]
```

- **Flat, ordered, 3-6 entries.** Order = importance; the first trait is the role. No `weight` field: an ordered list carries the same information with zero new fields; add an integer only if a deterministic scorer ever needs one.
- **`kind` is a fact about the trait, not a bucket to fill.**
  - `capability` — the work itself, the role's recurring output.
  - `background` — an occupation, track, or qualification the JD names for the candidate: feeder career, degree, published work, "former founder", a portfolio.
  - `tool` — a language/tool only when the role's recurring output *is* that artifact.
  - There is no `industry` kind, and no field where one could go.
- **`evidence_quote` is mandatory and verified** the same way `_candidate_populations` verifies hints (`BEI:322-323`: `quote not in jd_text` → drop). No quote, no trait. A quote from an About/Background/Why-us/Tech-stack section is rejected by the prompt rule; the eval checks it.
- **Level, years, location stay where they are:** `target_level` + `usable_cutoff` (`BEI:445-447, 498`), `search_scope` (`BEI:213-245`), `filters` → `retrieval_filters` (`plan_filters.py:120-154`). The prompt forbids traits for any of them.
- **`candidate_populations`: keep, minus three kinds.** It is retrieval-side and has real readers (`decompose_jd.py:77-99`, `network_floors.py:51-56`). Delete `ranking-boost` (readers are exclusion sets; its instruction is the industry-contamination source), `tool-culture` (both observed outputs were wrong), and `comp-band-anchor` (duplicates `comp_band`, `BEI:173-175` — one home per concept). 10 kinds → 7.
- **`comp_band`: keep unchanged.** Read by `company_context.py:439-471` into every fit expert including `MOVE_FEASIBILITY` (`:483`).
- **Delete:** `must_have`, `nice_to_have`, `tier`, `core_groups`, `MAX_CORE_TRAITS`, `compile_core_groups`, the core-gate in `judge_consensus.py:48-72`, the `core_groups` validation in `deep_search_loop.py:257-309` and `plan_critic.py:94-121`. Readers to point at the flat list: `search_harness.py:515-519` (join all `capability` traits), `triage_candidates.py:82-101`, `micro_sort_shortlist.py:215-216`, `expand_from_anchor.py:74-80`. `decompose_jd.py:100-119` must stop sending traits to seed generation at all.
- **Stop composing on `trait_generation.txt`.** `compose_plan_system_prompt` (`BEI:194-202`) appends the adapter to the query prompt; the new prompt stands alone for the JD path. `TG` stays as-is for fast search.

## 4. The new system prompt

```
You read a job description and return the traits a recruiter scores a candidate's profile against
AFTER retrieval. Traits never narrow a search; they only rank people already found.

A trait is a fact about the PERSON: work they have done, a track they came from, or a qualification
they hold. It must be scorable against a career profile. Return 3-6 traits, most defining first;
the first trait is the role. Fewer is better. Add a trait only if a recruiter would rank two
otherwise-equal candidates differently on it.

KINDS
- capability: the work itself — the role's recurring output. "thermal design of flight hardware
  taken through test"; "builds operating playbooks from ambiguous requirements"; "designs and
  owns an API as a product".
- background: an occupation, track, or qualification the JD names FOR THE CANDIDATE. "1-2 years in
  management consulting or investment banking"; "degree in mechanical or aerospace engineering";
  "published work in RLHF or behavioral modeling"; "former founder"; "portfolio of shipped landing
  pages".
- tool: a language or tool, ONLY when the role's recurring output IS that artifact — the person
  writes the compiler; the JD says the GraphQL API is the product. A tool the work merely uses
  (React for a web app, Python for tooling, Postgres, a simulation package) is never a trait,
  even when the JD says "required". Fold it into the capability's wording at most.

NEVER A TRAIT
- The hiring company's industry, market, customers, product, or mission. Sections such as About,
  Background, Why us, Technical/Research Challenges, and Tech Stack describe the company; read them
  to understand the role, quote nothing from them. Fintech, healthcare, B2B SaaS, mortgages,
  computational biology, and "what we are building" are the company, not the person.
- Soft skills, culture, attitude: communication, ownership, curiosity, humility, velocity, high bar,
  low ego, "excited about", "passion for".
- Level, years of experience, seniority words, location, work authorization, compensation. Those go
  in target_level, usable_cutoff, location, filters, and comp_band, never in traits.
- Restatements. One trait per distinct kind of evidence; if proving A makes B unsurprising, keep A.

BOUNDARIES
- A technical specialty the JD names as the candidate's own prior experience (reinforcement
  learning, 3D/embodied AI, distributed systems, search systems) is a trait — it is a body of
  technique. An industry the candidate worked in is not — it is a market. Ask: technique or market?
- A hybrid role (design AND code; research AND shipping) yields one capability per half, both listed.
- A role with no stable title (ops associate, product engineer) yields the feeder track as a
  background trait plus one or two capabilities.
- Every trait carries one exact contiguous quote from the JD's role or requirements text.
  No quote, no trait.

Also extract, from the JD only: job_title, hiring_company_name, normalized_archetype (2-4 words),
pond_prompt_family, hire_stage, target_level, usable_cutoff, location + location_filters, filters
(location/authorization/license gates only), candidate_populations (retrieval hints, each with a
verbatim quote, kinds: stated-background, dual-craft-sentence, portfolio-signal,
department-title-tension, feeder-career-language, situational-population, capability-adjacent;
never an industry), comp_band or null, recruiter_preferences only if the JD states them.

Return strict JSON:
{"job_title":"","hiring_company_name":"","normalized_archetype":"",
 "pond_prompt_family":"engineering|marketing-sales|customer-support|operations-finance-people|design|general",
 "hire_stage":"founding_early|scaling_late",
 "target_level":"senior_ic|staff_ic|lead|manager|director|vp|exec","usable_cutoff":"",
 "location":"","location_filters":{"cities":[],"states":[],"countries":[],"metro_areas":[],"macro_regions":[]},
 "filters":[""],
 "traits":[{"trait":"","kind":"capability|background|tool","evidence_quote":""}],
 "candidate_populations":[{"population":"","hint_kind":"","evidence_quote":""}],
 "comp_band":{"currency":"","minimum":0,"maximum":0,"period":"year|month|hour|unknown","evidence_quote":""}|null,
 "recruiter_preferences":{}}
```

The location/level rules currently at `BEI:107-126` can be kept verbatim under the "Also extract" paragraph if the model needs them; they are unchanged by this redesign.

## 5. Predicted outputs (hand-derived, NOT run)

Quotes are exact substrings of each `jd.txt` (curly apostrophes avoided). Order = importance. These are the eval set; a maintainer runs the prompt and diffs.

**agentmail-role**
1. capability — Backend/infrastructure systems engineer: APIs, distributed infrastructure, reliability — "This is a role for someone who likes systems work: APIs, distributed infrastructure, reliability, developer tooling."
2. capability — Owns projects end-to-end from system design to production — "Own projects end-to-end, from system design to production rollout"
3. background — Shipped production software as an engineer at a tech company or startup — "prior experience shipping production software by real users as a software engineer at a tech company or startup"
Dropped: agents-as-users, email/messaging/auth, AI/LLM use cases (product), communication (soft), cloud (stack).

**fortuna-health-senior-operations-associate**
1. background — 1-2 years in management consulting, investment banking, or a startup operating role — "1-2+ years of experience in management consulting, investment banking, or working at a startup"
2. capability — Builds operational processes and repeatable playbooks from ambiguous requirements — "you are comfortable building operational processes in ambiguous situations"
3. capability — Analytical: forms hypotheses, runs tests, decides from data — "design and run tests to validate them, document what we learn as repeatable best practices"
Dropped: Medicaid (customer domain), state expansion (go-to-market), all five culture headings. NYC 5-days → filters; early-career → usable_cutoff.

**icarus-senior-thermal-engineer**
1. capability — Thermal analysis/design of flight, space, or high-reliability hardware, taken through test into flying hardware — "A thermal design you took from analysis through test and into flying hardware."
2. background — Degree in mechanical, aerospace, or another engineering discipline — "with a degree in mechanical, aerospace, or another engineering discipline"
3. capability — Thermal work in radiation-dominated, low-convection environments (spacecraft, high-altitude, UAV) — "Experience in low-convection environments where radiation dominates"
4. capability — Battery thermal management: pack modeling, heaters, cold-soak — "Battery thermal management experience: pack-level modeling, heater strategies, and cold-soak survival."
Dropped: solar aircraft at 60,000 ft (company), Thermal Desktop/ANSYS/Python/MATLAB (stack), "3+ years" (usable_cutoff), ownership/fast-paced (soft). Chamber test is inside #1's "through test".

**latchbio-14900ad1**
1. capability — Systems-level software engineer (distributed systems, databases, OS, compilers, PL) — "Interest in computer systems and their inner workings"
2. capability — Designed and shipped products people love; owns a product area — "Designed and shipped products that people love"
3. capability — Applies a systems/process-engineering approach to tooling and data systems — "Ability to apply process or systems engineering approach to tooling and data systems"
Dropped: computational biology, benchmarks, agent harness, genomics (product/industry), full-stack (stack), collaboration with scientists (soft).

**leadoptik-manufacturing-technician**
1. capability — Precision fiber-optic/optical assembly: micro-scale alignment, fiber handling and bonding, microscope work — "Three or more years in optical/fiber-optic assembly or precision manufacturing."
2. capability — Functional/performance testing of optical assemblies with recorded, analyzed test data — "Conduct functional and performance testing of optical subassemblies and finished assemblies."
3. background — Associate degree / technical certificate in optics, photonics, or electro-mechanical technology, or equivalent hands-on experience — "Associate degree or technical certificate in Optics, Photonics, Electro-Mechanical Technology, or equivalent hands-on experience."
4. capability — Works to and helps draft MPIs / work instructions — "Assist engineering in drafting, reviewing, and revising Manufacturing Process Instructions (MPIs) and work instructions."
Dropped: ISO 13485/FDA (industry), catheters/endoscopes (product), power meters (tool), cleanroom (environment; becomes wording inside #1 if at all). Six current cores collapse into #1.

**lovable-staff-principal-design-engineer**
1. capability — Frontend engineer shipping production web UI with performance, accessibility, cross-browser rigor (React/TypeScript as the medium, not a trait) — "Implement web interactions in React/TypeScript with obsessive attention to performance, responsiveness, and cross-browser consistency"
2. capability — Design craft: designs interactions, eye for detail and usability — "Strong design sense with an eye for detail, accessibility, and usability"
3. background — Portfolio of shipped landing pages and advanced web interactions — "Portfolio demonstrating landing pages and advanced web interactions"
Dropped: WebGL (stack), speed-vs-polish tradeoffs (soft), "10+ years" / Staff-Principal (level). Hybrid: judge requires #1 and #2 both.

**spectral-labs-research-engineer-rl**
1. capability — Has built reinforcement-learning pipelines / RL training — "Design and implement RL pipelines to improve our models"
2. capability — ML depth demonstrated through background — "Strong intuitive grasp of ML concepts and theory, demonstrated through background"
3. capability — Runs experiments in an iterative, scientific way — "Design and run experiments in an iterative, scientific way"
4. background — Prior work in 3D / generative CAD / embodied AI / world modeling (named as candidate experience; a technique, not a market) — "Experience with generative CAD modeling or other 3D domains, embodied AI, or image/video/world modeling."
Dropped: CAD foundation models (product), reward functions for spatial tasks (product), GPA, "impressive achievements", collaboration. Degree "a plus" → not a trait; RL evidence outranks credentials per Jake.

**tldraw-product-engineer**
1. capability — Frontend-leaning full-stack web engineer — "leans front-end but is comfortable diving across the stack"
2. capability — Shipped meaningful features and shaped the product on a high-agency team — "shipped meaningful features and helped shape the product"
3. capability — Craft: obsessive attention to detail in products — "We celebrate craft and nerd out about bringing obsessive attention to detail in our products."
Dropped: React/Next/Node/TS (stack — negative case for the tool rule despite "required"), SDK/dev-tools (product, and a "we'd love"), infinite canvas (company), open-source (a fourth if wanted; Jake never used it), four soft skills.

**listenlabs MTS Platform** (search-debug-20260902)
1. capability — End-to-end engineer owning a product slice across infrastructure, backend, and UX — "every engineer owns a part of the product and makes decisions across the LLM pipeline, infrastructure, backend, and UX"
2. background — Future or past founder; scopes own work — "You're a future or past founder."
3. capability — Has built with LLMs in production — "You're excited about pushing LLMs to their limits."
4. capability — Highly technical / systems depth — "You're highly technical."
Dropped: human-preference model, AI-native at scale, Database of Humanity, agent evals (all company), compilers (culture sentence), five soft skills. Location: Jake says SF; the plan has null. That is a `search_scope` fix, not a trait.

**pylon-d1ef993a MTS API**
1. capability — Backend engineer — "We're looking for Back End Engineers to join our API team."
2. capability — Designs and owns an API as a product: versioning, breaking changes, onboarding, DX — "You'll think about breaking changes, versioning, onboarding, and the full lifecycle of an API that real companies depend on."
3. tool — GraphQL API design (the one positive tool case in this set: the JD says the GraphQL API is the product) — "We treat our GraphQL API as a product"
4. capability — Event-driven / workflow-oriented backend architecture — "Build toward an event-driven API."
Dropped: mortgage / path-dependent domain modeling (company domain rewritten as a skill), TypeScript/NestJS/PostgreSQL/Temporal.io (stack), former founders (culture), AI tooling (stack).

**listenlabs-4b1725cc Founding Research Scientist**
1. background — Published in LLMs, post-training, RLHF, behavioral modeling, or simulation, or equivalent frontier-lab impact — "Published work in LLMs, post-training, RLHF, behavioral modeling, simulation, or adjacent fields."
2. capability — Hands-on: trains models, writes evals, ships to production — "You can train models, write evals, and collaborate with our research engineers to put the model into production."
3. capability — Sets own research agenda: picks problems, scopes programs — "scope research programs, and decide what success looks like"
Dropped: human-preference / multi-agent simulation (company's Research Challenges), "genuinely curious about humans" (attitude), writing (soft). "Founding"/"lead" → target_level.

Eval assertions worth encoding: no trait quote from an About/Background/Challenges/Tech-Stack section (all 11); no trait naming an industry (fortuna, latchbio, pylon, spectral, listen ×2); tool kind fires only on pylon; React "required" yields no tool trait (lovable, tldraw); leadoptik collapses to ≤4; count within 3-6 everywhere.

## 6. Risks and open questions

1. **Hybrid roles need an AND, and core_groups was the (bad) answer.** Lovable requires design and code; Listen RS requires research record and shipping. With buckets gone, the judge rubric must say "every `capability` trait is expected; `background` traits are priors" — kind carries it, no new field. Verify Jake agrees that this is the only AND semantics needed; if a JD ever states two *alternative* routes (Firecrawl: search systems OR large-scale infra), that is two ponds, not two traits, and belongs in `candidate_populations`.
2. **Feeder-career roles make the background trait the main ranker,** and the real gate is a down-move check (senior consultants are "structurally wrong"). That lives in `usable_cutoff` / move feasibility, not traits. Fortuna will look thin (3 traits) and that is correct.
3. **Research roles: publication record is a real background trait** but profiles rarely list papers. The JD itself gives the equivalence ("or equivalent industrial impact at a frontier lab"); the judge must accept employer-as-evidence here without letting "worked at a frontier lab" become a general pedigree rule.
4. **Tool rule edge.** Pylon is the only positive in this set, on the strength of "GraphQL (this is the product)". Someone will argue Lovable's React the same way. The rule as written ("output IS the artifact" vs "the work uses it") decides both; the eval must pin them so the rule does not drift.
5. **Technique vs market has soft edges.** "Search systems", "3D/embodied AI", "RL" are techniques; "underwriting/AML", "computational biology", "creative tooling" are markets for these roles — but "computational biology" is a technique for a computational biologist. The section rule (quote from requirements, never from About) resolves most cases; the residue is a judgment the eval should sample.
6. **Three traits can be too few** when the occupation is broad and the JD is specific in the candidate's own terms (icarus has 4-5 genuinely distinct capabilities). The 3-6 band covers it; the "fewer is better" instruction should not be tightened to a fixed N.
7. **Retrieval leak is a separate fix.** Cleaning traits does not stop `decompose_jd.py:88-89` from putting an "experience clause" into pond queries, nor `candidate_populations` from carrying industry ponds (fortuna "Medicaid policy analyst", listen RS "Behavioral simulation researcher", both quoted from About). The same never-industry rule should be applied to hints in the same change.
8. **The judge and triage prompts are out of scope here but are the other half.** `triage_candidates.py:95-101` renders "Alternative core paths / Core / table-stakes / Nice-to-have" headings; they must be rewritten to the flat list or the new contract changes nothing the candidate sees.
9. **`trait_generation.txt` is shared with fast search.** Decoupling `compose_plan_system_prompt` from it (`BEI:194-202`) changes tests that assert composition; fast search is untouched.
10. **Jake's taste layer stays outside traits.** Company bar, tenure, jumpiness, slope, recruitability, destination pull — the things he spends most words on — are `company_context` annotators, not JD traits. This redesign narrows traits to the JD half on purpose; it does not make the taste half rank, which the validation draft identifies as the other missing piece.

## Eval run (2026-09-02)

Real `build_eval_inputs.extract_plan` path (plan call + traits call), gpt-5.6-luna,
reasoning medium, `.env` key, no pond/candidate artifact opened. Run 1: 22 calls,
$0.031. The traits prompt was revised once (below) and only the traits call was
rerun for all 11 JDs: 11 calls, $0.011. Total 33 calls, $0.041. Every v2 plan
passes `validate_approved_plan` (schema + cross-field). Family routing is the
plan call's `pond_prompt_family`; "§5" = the hand-derived predictions above.

### Run 1 (draft prompt) — what missed

- Four JDs quoted the posting title line as trait #1 ("Founding engineer",
  "Optical manufacturing technician", "Platform engineering", "Founding research
  scientist in human simulation") — a hollow, unscorable trait.
- leadoptik split one assembly craft into five traits (assertion ≤ 4 failed);
  lovable emitted five with two restatements.
- listen RS quoted two traits from the "Research Challenges" section (the one
  true company-section quote; my other section flags were heuristic false
  positives — agentmail/icarus/lovable/fortuna quotes all sit under "What
  you'll do" / "What we're looking for" / "Requirements").
- pylon kept "path-dependence" (the mortgage paragraph rewritten) and emitted no
  `tool` trait; tldraw kept "developer-facing documentation … SDK usage
  patterns" (product); latchbio kept "agent infrastructure … model benchmarking".
- Verbatim gate worked: fortuna and latchbio each lost one trait whose quote
  was not an exact substring (curly-apostrophe / punctuation drift).
- Counts 3–6: 11/11. React "required" → no tool trait: pass on lovable and
  tldraw. No industry/market named anywhere.

### Prompt revision (shared core, all six files)

1. "The first trait is the role written as the work it produces, quoted from
   the sentence describing that work; the posting title line is a label, never
   a quote." + "most roles need 3 or 4".
2. Restatements: "merge any two traits one profile line would prove together:
   the steps of one assembly craft are one trait, owning landing pages and
   building landing pages are one trait."
3. New boundary: a responsibilities sentence naming the company's product,
   pipeline, SDK, documentation, benchmark, or domain → keep the technique,
   drop the product noun; nothing left means no trait.
4. Quote rule names the allowed sections (what you'll do, looking for, who you
   are, requirements) and forbids company/challenges paragraphs, questions the
   company asks about its own research, and the title line.
5. Tool: when the JD itself says an artifact of a named language/tool is the
   product, emit it as its own `tool` trait next to the capability; a
   parenthetical stack list after a product name is still a stack list.

### Run 2 (contamination fixed) — history

Traits call only, 11 calls, $0.011. Every contamination assertion passed:
no company-section quotes (11/11), counts 3–5, verbatim 11/11, `tool` only on
pylon ("GraphQL (this is the product)"), React never a tool, leadoptik 4, §5
predictions present 31/36, 8 extras none a stack / soft skill / level /
industry. Four rows kept a responsibilities line whose noun was the company's
product with a technique in the trait text (latchbio, spectral, tldraw,
pylon). The owner's read: this measured contamination, not judgeability —
"future or past founder", "scopes own work", "semantic modeling at the API
layer", "playbooks for state expansion" are not things a career profile can
prove or disprove.

### Run 3 (judgeable)

Prompt change (shared core, all six files): a **PROFILE TEST** block. Every
trait must be provable or disprovable from a career profile — a position
title, a company, a dated role and its description, or an education/credential
line — and is written as that evidence ("has founded a company", "has built
event-driven backend systems in production", "has designed and shipped a
public API", "took a thermal design through test into flying hardware", "1–2
years in management consulting or investment banking"). Dropped: intentions or
aspirations ("future founder", "wants to"), attitudes, ownership / scoping /
autonomy language ("scopes own work", "owns decisions", "high agency"),
abstract nouns lifted from JD prose ("semantic modeling at the API layer",
"playbooks for state expansion"), and anything only the JD's own wording would
prove. The `evidence_quote` stays the JD source; the trait text is the
profile-checkable claim. Rule text: **"If you cannot name the profile line
that would prove it, it is not a trait."**

Run 3a (11 traits calls): four traits were still "has" + a JD manner sentence
("has applied a process or systems engineering approach", "has designed and
run iterative, scientifically structured experiments", "has modeled complex,
branching, path-dependent workflows", "has conducted primary-source research
into eligibility rules"), listen MTS fell to 2 traits, leadoptik to 5. The
block gained two sentences: prefixing "has" to a JD sentence does not pass —
rewrite to the artifact, system, or role a profile would show ("has built a
workflow engine", "has built internal data tooling") or drop it; and a line
mixing a provable track with an aspiration or attitude ("future or past
founder", "excited about pushing LLMs to their limits", "a passion for
building developer tools") yields the provable half. Restatements: "an
umbrella trait and its parts are one trait." Run 3b reran only fortuna,
latchbio, pylon, listen MTS (4 calls; the round's 15-call cap). Round: 15
calls, $0.017. All-time: 48 calls, $0.058.

Rows below are 3b for those four JDs and 3a for the other seven. Grading is
by hand, no model calls. **J** = judgeable: the ≤8-word profile evidence that
proves it follows the arrow; *weak* = provable only from an unusually
descriptive role line. Q/I/T/R/L/N/V as in run 2.

| JD (family) | traits: kind — trait → proving profile evidence | J | Q | I | T | R | L | N | V |
|---|---|---|---|---|---|---|---|---|---|
| agentmail (eng) | 1 cap — has designed APIs and abstractions for end users → role line "designed public API" · 2 cap — has built high-performance, low-latency systems → role line: low-latency/serving system · 3 cap — has built or operated cloud infrastructure, distributed systems, or developer tooling → infra/platform title or description · 4 cap — has built messaging, authentication, or protocol-heavy systems → role line: email/auth/messaging system · 5 bg — has shipped production software used by real users as a software engineer → any SWE title at a product company | 5/5 | ✓ | ✓ | ✓ | – | – | ✓ 5 | ✓ |
| fortuna (ops-fin-people, 3b) | 1 cap — has built operational processes and playbooks that teams execute → ops role "built X process/playbook" · 2 cap — has designed and run tests to validate operational processes → ops role: process pilots/experiments (*weak*) · 3 cap — has translated operational rules into product workflows and checks → ops/product role: wrote product requirements (*weak*) · 4 bg — has worked in management consulting, investment banking, or at a startup → consulting/IB firm or startup title. The 3a "primary-source research into eligibility rules" (Medicaid) trait is gone. | 4/4 | ✓ | ✓ | ✓ | – | – | ✓ 4 | ✓ |
| icarus (eng) | 1 cap — has taken aircraft thermal design from first-order sizing through validation against altitude-chamber and flight data → thermal engineer role: flight qualification · 2 cap — has designed battery thermal management (insulation, heater sizing, cold-soak) → role line: battery/pack thermal design · 3 cap — has worked on thermal problems in low-convection, radiation-dominated environments (spacecraft, high-altitude, UAV) → employer/role in spacecraft or UAV thermal · 4 cap — has defined and run thermal tests with instrumentation and correlated models to data → role line: TVAC/altitude-chamber test · 5 bg — degree in mechanical, aerospace, or another engineering discipline → education line | 5/5 | ✓ | ✓ | ✓ | – | – | ✓ 5 | ✓ |
| latchbio (eng, 3b) | 1 cap — has built software for agent infrastructure, model benchmarking at scale, and full-stack web development → role line: agent infra / eval infra / full-stack (*weak*: three claims) · 2 cap — has designed and shipped products → product-shipping role · 3 cap — has built tooling and data systems → role line: internal tooling / data pipelines. The 3a "applied process or systems engineering approach" trait became #3. | 3/3 | ✓ | ✓ | ✓ | – | – | ✓ 3 | ✓ |
| leadoptik (eng) | 1 cap — has performed precision assembly and testing of miniature fiber-optic imaging components → technician role: fiber-optic assembly · 2 cap — has performed micro-scale optical alignment using precision fixturing → role line: optical alignment · 3 cap — has handled and bonded optical fibers (stripping, cleaving, polishing, epoxy, UV-cure) → role line: fiber termination/bonding · 4 cap — has conducted functional/performance tests on optical assemblies, analyzed data, root-cause → role line: optical test technician · 5 cap — has drafted, reviewed, or revised manufacturing process instructions → role line: wrote MPIs/work instructions | 5/5 | ✓ | ✓ | ✓ | – | ✗ 5 | ✓ 5 | ✓ |
| lovable (design) | 1 cap — has built and elevated landing pages and web experiences → role line: marketing site / landing pages · 2 cap — has implemented performant, responsive, cross-browser web interactions → frontend engineer title · 3 cap — has translated visual explorations into scalable, web-native implementations → design engineer title / "implemented designs" (*weak*) · 4 cap — has built and maintained reusable frontend components → role line: design system / component library | 4/4 | ✓ | ✓ | ✓ | ✓ | – | ✓ 4 | ✓ |
| spectral (eng) | 1 cap — has designed and implemented reinforcement-learning pipelines → role line: RL training pipeline · 2 cap — has designed reward functions for spatial and downstream engineering tasks → RL role: reward design/shaping (*weak*; suffix is the product domain) · 3 cap — has designed and run iterative, scientifically structured experiments → **NOT judgeable**: a manner of working; any research title "proves" it, so it ranks nobody · 4 bg — experience with generative CAD modeling, 3D domains, embodied AI, or image/video/world modeling → role/publication in 3D, embodied AI, world models | 3/4 | ✓ | ✓ | ✓ | – | – | ✓ 4 | ✓ |
| tldraw (eng) | 1 cap — has built, maintained, and scaled core features of modern web applications → web/frontend engineer role · 2 cap — has ideated, prototyped, shipped, and iterated product experiences → product/frontend role: shipped features (*weak*) · 3 cap — has maintained backend services and APIs alongside frontend product work → full-stack role line · 4 cap — has contributed to developer-facing documentation, code samples, and SDK usage patterns → devtools role: wrote SDK docs · 5 bg — has built developer tools, SDKs, or platforms → devtools company/role (the "passion for" line reduced to its provable half) | 5/5 | ✓ | ✓ | ✓ | ✓ | – | ✓ 5 | ✓ |
| listen MTS (eng, 3b) | 1 cap — has built end-to-end product features spanning LLM pipelines, infrastructure, backend, and UX → full-stack role line with LLM work · 2 bg — has founded a company → Founder/CEO title. **2 traits in both 3a and 3b**: the model dropped "scopes your own work" and "highly technical" (correct) but did not reduce "excited about pushing LLMs to their limits" to "has built LLM systems" as the 3b rule asks. `plan_from_obj` raises "trait extraction produced 2 traits; 3-6 required" — the run stops before review. | 2/2 | ✓ | ✓ | ✓ | – | – | ✗ 2 | ✓ |
| pylon (eng, 3b) | 1 cap — has designed and shipped event-driven APIs → role line: event-driven API / webhooks · 2 cap — has modeled complex, branching workflows in software → role line: workflow engine / state machines (*weak*; quote is the mortgage paragraph, trait text is the technique) · 3 cap — has designed API abstractions and documentation for developer-facing systems → role line: public API + docs · 4 cap — has managed API versioning, breaking changes, onboarding, lifecycle → role line: owned public API versioning. 3a also emitted `tool` — "has built GraphQL APIs as customer-facing products" from "We treat our GraphQL API as a product"; 3b did not (same rule, same JD: model variance). | 4/4 | ✓ | ✓ | ✓ (3a) / vacuous (3b) | – | – | ✓ 4 | ✓ |
| listen RS (eng) | 1 cap — has trained models, written evaluations, and put research models into production → research engineer/scientist role line · 2 bg — has published research in LLMs, post-training, RLHF, behavioral modeling, or simulation → publications / research scientist at a lab · 3 bg — has delivered equivalent industrial research impact at a frontier lab → employer is a frontier lab (the JD's own "or"; one trait with #2 would be tighter) | 3/3 | ✓ | ✓ | ✓ | – | – | ✓ 3 | ✓ |

Totals (run 3): 44 traits emitted, **43 judgeable (98%)**, 7 of them weak; 1
NOT (spectral #3, not rerun at the cap — the 3b rule names that wording). Q
11/11 · I 11/11 (no industry or market; fortuna's Medicaid trait gone) · T
pylon `tool` in 3a, absent in 3b · R 2/2 · L ✗ leadoptik 5 (umbrella + parts,
the "first trait is the role" instruction won over the merge rule) · N 10/11
(listen MTS 2) · V 11/11. Versus run 2 the gain is the trait *text*: every
trait now starts "has …" / names a degree, title, or track, and the four
run-2 abstractions the owner called out are gone.

Open: (1) listen MTS — this "Who You Are" section yields two provable claims;
either MIN_TRAITS becomes 2, or thin JDs keep failing loudly at extraction
(current behavior). (2) The pylon `tool` trait is a coin flip on one JD; if
the panel needs it reliably the tool rule needs a positive example in the
engineering block. (3) leadoptik: drop the role-umbrella when its parts are
listed, or accept 5.
