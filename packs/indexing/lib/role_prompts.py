"""
Cluster-specific prompts for role classification.

Each cluster gets targeted instructions and consolidation rules
to improve classification consistency.
"""

from typing import List, Dict, Union, Any

# =============================================================================
# BASE PROMPT TEMPLATE (Legacy - used with CLUSTER_INSTRUCTIONS)
# =============================================================================

BASE_CLASSIFICATION_PROMPT = """You are classifying job titles into structured components for a professional search system.

MAIN OBJECTIVE: Map each title to the correct role_id(s) in snake_case format.

CRITICAL ROLE_ID RULES - WHAT NOT TO DO:
- NEVER include seniority in role_id: "senior_software_engineer" -> WRONG
  CORRECT: role_id="software_engineer", seniority_band="senior"
- NEVER include company/team names: "google_engineer", "platform_engineer"
  CORRECT: role_id="software_engineer", specialization="platform"
- NEVER include department in role_id: "engineering_director" -> WRONG
  CORRECT: role_id="director", function="engineering"
- NEVER create overly specific role_ids: "react_engineer", "python_developer"
  CORRECT: role_id="frontend_engineer" or "software_engineer"
- NEVER use spaces or capitals: "Software Engineer", "software engineer"
  CORRECT: role_id="software_engineer" (always snake_case)
- NEVER CREATE FLUFFY/VAGUE TITLES:
  NO titles with: "guru", "ninja", "rockstar", "wizard", "evangelist"
  ONLY professional, legitimate business titles
  Extract the actual professional function, ignore fluffy language

HANDLING BOGUS TITLES:
If a title is clearly NOT a professional job title (e.g., "ramen aficionado"):
- Set confidence=0.0
- Set role_ids="" (empty string)
- Set role_type="ic" (default)
- Set seniority_band="mid" (default)
- Add reasoning="Title is not a professional job title, skipping classification"

{cluster_instructions}

ROLE TYPES:
- executive: CEO, CTO, VP, Director roles (leadership/decision-making)
- management: People managers like Engineering Manager, Team Lead
- ic: Individual contributors (engineers, designers, PMs who don't manage people)
- investor: VCs, Angels, PE professionals
- board: Board members, observers, chairman
- advisor: Advisors, mentors, consultants
- academic: Professors, researchers, postdocs
- founder: Founders, co-founders, entrepreneurs

SENIORITY BANDS (use these exact lowercase values):
- owner
- partner
- c-suite
- vice-president
- director
- principal
- staff
- manager
- senior
- mid
- junior
- entry
- trainee

ROLE_TRACK (functional area - use these exact values when applicable):
- Customer Service
- Design
- Education
- Engineering
- Finance
- Health
- Human Resources
- Legal
- Marketing
- Media
- Operations
- Public Relations
- Real Estate
- Sales
- Trades

{exemplars}

For each title, return:
{{
    "raw_title": "exact input",
    "canonical_title": "normalized title without company-specific details",
    "role_ids": ["array", "of", "role_ids"],
    "role_type": "category",
    "role_track": "functional area from ROLE_TRACK list or null",
    "seniority_band": "level from SENIORITY BANDS list",
    "specialization": "specific_area or null",
    "doc2query": ["search", "variations", "abbreviations"],
    "inferred_skills": ["relevant", "skills"],
    "confidence": 0.95,
    "reasoning": "brief explanation"
}}

Classify these {num_titles} titles from the {cluster} cluster:
{titles}

Return JSON array of TitleClassification objects."""


# =============================================================================
# CLUSTER-SPECIFIC INSTRUCTIONS (standalone variables for readability)
# =============================================================================

C_SUITE_INSTRUCTIONS = """
CLUSTER: C-SUITE EXECUTIVES
You're processing C-level executive titles.

CANONICAL TITLE NORMALIZATION:
- Expand abbreviations: "CTO" -> "Chief Technology Officer"
- Remove company-specific terms: "Chief Technology Officer, Acme Corp" -> "Chief Technology Officer"
- Keep standard format: "Chief [Function] Officer"

ROLE_ID PATTERNS:
- Chief [X] Officer -> chief_[x]_officer (e.g., "Chief Technology Officer" -> chief_technology_officer)
- C[X]O abbreviations -> chief_[x]_officer (e.g., "CTO" -> chief_technology_officer)
- President -> president

CONSOLIDATION RULES:
- {CEO, Chief Executive, Chief Exec} -> chief_executive_officer
- {CTO, Chief Technology Officer, Chief Tech Officer} -> chief_technology_officer
- {CFO, Chief Financial Officer} -> chief_financial_officer
- {COO, Chief Operating Officer, Chief Ops Officer} -> chief_operating_officer
- {CMO, Chief Marketing Officer} -> chief_marketing_officer
- {CPO, Chief Product Officer} -> chief_product_officer
- {CRO, Chief Revenue Officer} -> chief_revenue_officer
- {CHRO, Chief Human Resources Officer, Chief People Officer} -> chief_people_officer
- {President, Pres.} -> president

SENIORITY: All c-level
ROLE_TYPE: executive (or founder if also founder)
"""



INVESTOR_INSTRUCTIONS = """
CLUSTER: INVESTMENT PROFESSIONALS
You're processing investor and venture capital titles.

ROLE_ID & SENIORITY MAPPING:
- Managing Partner -> managing_partner (c-level, investor)
- General Partner/GP -> general_partner (vp, investor)
- Partner -> partner (director, investor)
- Venture Partner -> venture_partner (senior, investor)
- Principal -> principal (principal, investor)
- Senior Associate -> senior_associate (senior, investor)
- Associate -> associate (mid, investor)
- Analyst -> analyst (junior, investor)
- Scout -> scout (entry, investor)
- Angel Investor -> angel_investor (senior, investor)
- Limited Partner/LP -> limited_partner (passive role)

CONSOLIDATION RULES:
- {Managing Partner, MP} -> managing_partner
- {General Partner, GP} -> general_partner
- {Venture Partner, VP (at VC firms)} -> venture_partner
- {Angel Investor, Angel} -> angel_investor
- {Scout, VC Scout, Venture Scout} -> scout

ROLE_TYPE: investor
"""

BOARD_GOVERNANCE_INSTRUCTIONS = """
CLUSTER: BOARD & ADVISORY ROLES
You're processing board and advisory positions.

ROLE_ID PATTERNS:
- Board Member -> board_member
- Board Director -> board_director
- Board Chairman/Chair -> board_chairman
- Board Observer -> board_observer
- Advisor -> advisor
- Advisory Board -> advisory_board_member

CONSOLIDATION RULES:
- {Board Member, Board of Directors Member} -> board_member
- {Chairman, Chair, Chairperson, Board Chair} -> board_chairman
- {Advisor, Adviser, Strategic Advisor} -> advisor
- {Board Observer, Observer} -> board_observer

SENIORITY MAPPING:
- Board Chairman -> c-level
- Board Member/Director -> director
- Board Observer -> senior
- Advisor -> senior

ROLE_TYPE: board (for board positions), advisor (for advisory positions)
"""

# =============================================================================
# DEDICATED PROMPT: BOARD & GOVERNANCE
# =============================================================================

BOARD_GOVERNANCE_CLASSIFICATION_SYSTEM = """You are a job title classification system for board and advisory roles.

These are typically part-time, oversight, or advisory positions - NOT full-time operating roles.

## OUTPUT SCHEMA
{{
  "raw_title": "exact input",
  "canonical_title": "normalized title",
  "role_ids": ["all", "applicable", "role_ids"],
  "role_track": "board",
  "seniority_band": "from SENIORITY enum",
  "confidence": 0.0-1.0,
  "reasoning": "required if confidence < 0.7"
}}

## ROLE_IDS ENUM
```
# Board of Directors
board_chairman            - Chairman, Chair, Chairperson, Board Chair
board_vice_chairman       - Vice Chairman, Vice Chair
board_member              - Board Member, Director (on board), Board Director
independent_director      - Independent Director, Independent Board Member, Non-Executive Director
board_observer            - Board Observer, Observer

# Advisory
advisor                   - Advisor, Adviser
strategic_advisor         - Strategic Advisor
technical_advisor         - Technical Advisor, Technology Advisor
senior_advisor            - Senior Advisor, Special Advisor
advisory_board_member     - Advisory Board Member

# Other governance
trustee                   - Trustee (nonprofit/foundation)
governor                  - Governor (institutional boards)
committee_member          - Committee Member (Audit, Compensation, etc.)
mentor                    - Mentor

# Founder (combine if applicable)
founder                   - Founder/Co-founder
```

## SENIORITY ENUM
```
senior      = 4   - Mentor, Advisor, Advisory Board Member
director    = 8   - Board Member, Board Observer, Trustee, Independent Director
chairman    = 9   - Board Chairman, Vice Chairman
```

## KEY RULES
1. **Board roles are part-time oversight** - NOT operating roles
2. **"Director" is ambiguous** - board director vs operating director. Check context
3. **"Executive Chairman" is operating** - route to c_suite cluster
4. **"Chairman & CEO" is compound** - include both board_chairman and c-level role
5. **Advisory board < Board of Directors** - less formal, less fiduciary duty
6. **Board Observer** - can attend but usually can't vote

## BOARD vs OPERATING ROLE SIGNALS

### Board role signals (this cluster)
- "Board of Directors", "Board Member"
- "Non-Executive", "Independent"
- "Quarterly meetings", "governance", "fiduciary"
- "Oversight", "advisory capacity"

### Operating role signals (wrong cluster)
- "Day-to-day", "full-time", "manage team"
- "Reports to CEO", "direct reports"
- "Executive Chairman" (route to c_suite)

## EXAMPLES

### Board of Directors
| raw_title | canonical_title | role_ids | seniority_band | confidence |
|-----------|-----------------|----------|-----------|------------|
| Board Member | Board Member | ["board_member"] | director | 0.90 |
| Board Director | Board Director | ["board_member"] | director | 0.90 |
| Director | Board Director | ["board_member"] | director | 0.50 |
| Chairman | Chairman | ["board_chairman"] | chairman | 0.85 |
| Chairman of the Board | Chairman of the Board | ["board_chairman"] | chairman | 0.95 |
| Board Chair | Board Chair | ["board_chairman"] | chairman | 0.95 |
| Vice Chairman | Vice Chairman | ["board_vice_chairman"] | chairman | 0.90 |
| Independent Director | Independent Director | ["independent_director"] | director | 0.95 |
| Non-Executive Director | Non-Executive Director | ["independent_director"] | director | 0.95 |
| Board Observer | Board Observer | ["board_observer"] | director | 0.90 |
| Observer | Board Observer | ["board_observer"] | director | 0.75 |

### Advisory
| raw_title | canonical_title | role_ids | seniority_band | confidence |
|-----------|-----------------|----------|-----------|------------|
| Advisor | Advisor | ["advisor"] | senior | 0.85 |
| Adviser | Advisor | ["advisor"] | senior | 0.85 |
| Strategic Advisor | Strategic Advisor | ["strategic_advisor"] | senior | 0.90 |
| Technical Advisor | Technical Advisor | ["technical_advisor"] | senior | 0.90 |
| Senior Advisor | Senior Advisor | ["senior_advisor"] | senior | 0.90 |
| Special Advisor | Special Advisor | ["senior_advisor"] | senior | 0.85 |
| Advisory Board Member | Advisory Board Member | ["advisory_board_member"] | senior | 0.95 |
| Advisory Board | Advisory Board Member | ["advisory_board_member"] | senior | 0.85 |
| Mentor | Mentor | ["mentor"] | senior | 0.85 |

### Other governance
| raw_title | canonical_title | role_ids | seniority_band | confidence |
|-----------|-----------------|----------|-----------|------------|
| Trustee | Trustee | ["trustee"] | director | 0.90 |
| Board of Trustees | Trustee | ["trustee"] | director | 0.85 |
| Governor | Governor | ["governor"] | director | 0.85 |
| Audit Committee Member | Audit Committee Member | ["committee_member"] | director | 0.90 |
| Compensation Committee | Compensation Committee Member | ["committee_member"] | director | 0.85 |

### Founder compounds
| raw_title | canonical_title | role_ids | seniority_band | confidence |
|-----------|-----------------|----------|-----------|------------|
| Founder & Board Member | Board Member | ["founder", "board_member"] | director | 0.90 |
| Co-founder & Chairman | Chairman | ["founder", "board_chairman"] | chairman | 0.95 |
| Founder & Advisor | Advisor | ["founder", "advisor"] | senior | 0.90 |

### Multi-role compounds
| raw_title | canonical_title | role_ids | seniority_band | confidence |
|-----------|-----------------|----------|-----------|------------|
| Chairman & Board Member | Chairman | ["board_chairman", "board_member"] | chairman | 0.90 |
| Board Member & Advisor | Board Member | ["board_member", "advisor"] | director | 0.85 |

### Edge cases - route elsewhere
| raw_title | handling | confidence | reasoning |
|-----------|----------|------------|-----------|
| Executive Chairman | Route to c_suite | 0.30 | Operating role, not pure board |
| Chairman & CEO | Compound - include both | 0.85 | ["board_chairman", "chief_executive_officer"] |
| Director of Engineering | Route to engineering | 0.20 | Operating role, not board |
| Director of Sales | Route to sales_bd | 0.20 | Operating role, not board |
| Managing Director | Route to finance or c_suite | 0.25 | Usually operating role |

### Ambiguous - flag for review
| raw_title | canonical_title | role_ids | confidence | reasoning |
|-----------|-----------------|----------|------------|-----------|
| Director | Board Director | ["board_member"] | 0.50 | Could be board or operating director |
| Observer | Board Observer | ["board_observer"] | 0.60 | Usually board observer, could be other |
| Advisor | Advisor | ["advisor"] | 0.70 | Clear but context helps specify type |
| Board | Board Member | ["board_member"] | 0.55 | Incomplete title |
| Venture Advisor | Advisor | ["advisor"] | 0.50 | Could be board_governance or investor cluster |"""

BOARD_GOVERNANCE_CLASSIFICATION_USER = """Classify these {num_titles} board/advisory titles:

{titles}

Return JSON array."""



DESIGN_INSTRUCTIONS = """
CLUSTER: DESIGN ROLES
You're processing design and creative titles.

ROLE_ID PATTERNS:
- UX Designer -> ux_designer
- UI Designer -> ui_designer
- Product Designer -> product_designer
- Graphic Designer -> graphic_designer

CONSOLIDATION RULES:
- {UX Designer, User Experience Designer, UXD} -> ux_designer
- {UI Designer, User Interface Designer, Visual Designer} -> ui_designer
- {Product Designer, UX/UI Designer} -> product_designer
- {Graphic Designer, Brand Designer} -> graphic_designer
- {Design Lead, Lead Designer} -> design_lead
- {Creative Director, Design Director} -> creative_director
- {UX Researcher, User Researcher} -> ux_researcher

SENIORITY (default mid unless specified):
- Junior -> junior
- No prefix -> mid
- Senior/Lead -> senior
- Staff/Principal -> staff/principal
- Director -> director

ROLE_TYPE: ic (except Director which is executive)
"""

SALES_BD_INSTRUCTIONS = """
CLUSTER: SALES & BUSINESS DEVELOPMENT
You're processing sales and BD titles.

ROLE_ID PATTERNS:
- Account Executive -> account_executive
- Sales Rep -> sales_representative
- Business Development -> business_development_representative
- Sales Engineer -> sales_engineer

CONSOLIDATION RULES:
- {Account Executive, AE, Sales Executive} -> account_executive
- {Sales Rep, Sales Representative, SDR} -> sales_representative
- {BDR, Business Development Rep, BD Rep} -> business_development_representative
- {Sales Engineer, SE, Solutions Engineer, Pre-Sales Engineer} -> sales_engineer
- {Account Manager, AM, Customer Success Manager, CSM} -> account_manager
- {Sales Manager} -> sales_manager
- {Sales Director, Director of Sales} -> sales_director
- {VP Sales, VP of Sales} -> vp_sales

SENIORITY MAPPING:
- SDR/BDR -> junior
- AE/Sales Rep -> mid
- Senior AE -> senior
- Sales Manager -> senior
- Sales Director -> director
- VP Sales -> vp
- CRO -> c-level

ROLE_TYPE: ic (reps), management (managers), executive (directors+)
"""

ACADEMIC_INSTRUCTIONS = """
CLUSTER: ACADEMIC & RESEARCH
You're processing academic and research titles.

ROLE_ID PATTERNS:
- Professor -> professor
- Research Scientist -> research_scientist
- Postdoc -> postdoctoral_researcher
- PhD Student -> phd_student

CONSOLIDATION RULES:
- {Professor, Prof} -> professor
- {Assistant Professor, Asst Prof} -> assistant_professor
- {Associate Professor, Assoc Prof} -> associate_professor
- {Full Professor, Professor} -> professor
- {Research Scientist, Researcher} -> research_scientist
- {Postdoc, Postdoctoral Researcher, Postdoctoral Fellow} -> postdoctoral_researcher
- {PhD Student, Doctoral Student, PhD Candidate} -> phd_student
- {Research Assistant, RA} -> research_assistant
- {Lecturer} -> lecturer

SENIORITY MAPPING:
- PhD Student/RA -> entry
- Postdoc -> junior
- Assistant Professor/Research Scientist -> mid
- Associate Professor -> senior
- Full Professor -> principal

ROLE_TYPE: academic
"""

FOUNDER_INSTRUCTIONS = """
CLUSTER: FOUNDERS & ENTREPRENEURS
You're processing founder and entrepreneurial titles.

IMPORTANT - COMPOUND TITLE HANDLING:
For compound titles (Founder + another role), include ALL roles in the role_ids array:
- "Founder & CEO" -> role_ids=["founder", "chief_executive_officer"], seniority_band="c-level"
- "Co-founder & CTO" -> role_ids=["founder", "chief_technology_officer"], seniority_band="c-level"
- "Founder" (standalone) -> role_ids=["founder"], seniority_band="founder"
- "Entrepreneur" -> role_ids=["entrepreneur"], seniority_band="mid"

NOTE: "Founder" and "Co-founder" are both mapped to just "founder" (no separate "cofounder" tag).

CONSOLIDATION RULES:
- Entrepreneur, Serial Entrepreneur -> entrepreneur
- Owner, Business Owner -> owner

ROLE_TYPE:
- Standalone "Founder" -> founder
- "Founder & [Role]" -> use the other role's type
"""

OTHER_INSTRUCTIONS = """
CLUSTER: GENERAL/OTHER
You're processing titles that don't fit specific clusters.

Apply general classification rules:
1. Identify the core role function
2. Map to appropriate role_id in snake_case
3. Determine seniority from title prefixes
4. Infer role_type from the function

COMMON PATTERNS:
- Operations roles -> operations_manager, operations_analyst
- HR roles -> hr_manager, recruiter, talent_acquisition
- Finance roles -> financial_analyst, accountant, controller
- Legal roles -> legal_counsel, lawyer, paralegal
- Marketing roles -> marketing_manager, content_marketer
- Support roles -> customer_support, technical_support

SENIORITY (default mid unless specified):
- Junior/Associate -> junior
- No prefix -> mid
- Senior -> senior
- Director/Head -> director
- VP -> vp
- Chief -> c-level
"""

BUSINESS_FUNCTIONS_INSTRUCTIONS = """
CLUSTER: BUSINESS FUNCTIONS (Marketing, Finance, Operations, Legal, HR)
You're processing titles from core business functions.

CANONICAL TITLE NORMALIZATION:
- Remove seniority: "Senior Marketing Manager" -> "Marketing Manager"
- Remove company-specific terms
- Expand abbreviations: "CFP" -> "Certified Financial Planner"

ROLE_ID PATTERNS BY FUNCTION:

MARKETING & GROWTH:
- Marketing Manager -> marketing_manager
- Growth Manager -> growth_manager
- Brand Manager -> brand_manager
- Content Marketer -> content_marketer
- Communications Manager -> communications_manager
- PR Manager -> public_relations_manager

FINANCE & ACCOUNTING:
- Financial Analyst -> financial_analyst
- Accountant -> accountant
- Controller -> controller
- FP&A -> financial_planning_analyst
- Treasury -> treasury_analyst
- Auditor -> auditor
- CPA -> accountant

OPERATIONS & LOGISTICS:
- Operations Manager -> operations_manager
- Operations Analyst -> operations_analyst
- Supply Chain Manager -> supply_chain_manager
- Logistics Manager -> logistics_manager
- Process Manager -> process_manager

LEGAL & COMPLIANCE:
- General Counsel -> general_counsel
- Legal Counsel -> legal_counsel
- Attorney/Lawyer -> attorney
- Paralegal -> paralegal
- Compliance Manager -> compliance_manager
- Contract Manager -> contract_manager

HR & TALENT:
- Recruiter -> recruiter
- Technical Recruiter -> recruiter (specialization: technical)
- Talent Acquisition -> talent_acquisition_specialist
- HR Manager -> hr_manager
- HR Business Partner -> hr_business_partner
- People Operations -> people_operations_manager
- Compensation Analyst -> compensation_analyst

CONSOLIDATION RULES:
- {Marketing, Growth Marketing, Performance Marketing} -> marketing_manager
- {Recruiter, Recruiting, Talent Scout} -> recruiter
- {Accountant, CPA, Bookkeeper} -> accountant
- {Attorney, Lawyer, Legal Counsel} -> attorney
- {HR, Human Resources, People Ops} -> hr_manager (unless specialist role)

SENIORITY (default mid unless specified):
- Coordinator/Assistant -> junior
- Specialist/Associate -> junior to mid
- Manager -> mid to senior
- Senior Manager -> senior
- Director/Head -> director
- VP -> vp
- Chief -> c-level

ROLE_TYPE: ic (analysts, specialists), management (managers)
"""

GENERAL_PROFESSIONAL_INSTRUCTIONS = """
CLUSTER: GENERAL PROFESSIONAL ROLES
You're processing general professional titles (analysts, consultants, specialists, etc.)

CANONICAL TITLE NORMALIZATION:
- Remove seniority: "Senior Consultant" -> "Consultant"
- Remove company-specific terms
- Keep the functional descriptor: "Strategy Consultant" -> "Strategy Consultant"

ROLE_ID PATTERNS:

ANALYSTS:
- Business Analyst -> business_analyst
- Strategy Analyst -> strategy_analyst
- Policy Analyst -> policy_analyst
- Research Analyst -> research_analyst
- Market Analyst -> market_analyst

CONSULTANTS:
- Consultant -> consultant
- Management Consultant -> management_consultant
- Strategy Consultant -> strategy_consultant
- Technical Consultant -> technical_consultant
- Implementation Consultant -> implementation_consultant

SPECIALISTS & COORDINATORS:
- Specialist -> specialist (with function in specialization field)
- Coordinator -> coordinator
- Strategist -> strategist

CONTENT & WRITING:
- Writer -> writer
- Technical Writer -> technical_writer
- Editor -> editor
- Content Creator -> content_creator
- Copywriter -> copywriter

SUPPORT & SERVICE:
- Customer Support -> customer_support
- Customer Success -> customer_success
- Client Services -> client_services
- Account Representative -> account_representative

FREELANCE & INDEPENDENT:
- Freelancer -> freelancer
- Contractor -> contractor
- Independent Consultant -> consultant (note in specialization)

ADMINISTRATIVE:
- Executive Assistant -> executive_assistant
- Administrative Assistant -> administrative_assistant
- Office Manager -> office_manager

OWNERS & EXECUTIVES (small business):
- Owner -> owner
- Business Owner -> owner
- Executive -> executive

CONSOLIDATION RULES:
- {Consultant, Consulting} -> consultant (or more specific variant)
- {Analyst, Analysis} -> analyst (or more specific variant)
- {Specialist, Specialize} -> specialist
- {Freelance, Freelancer, Independent} -> freelancer
- {Contractor, Contract} -> contractor

SENIORITY (default mid unless specified):
- Assistant/Coordinator -> junior
- Associate -> junior to mid
- No prefix -> mid
- Senior -> senior
- Lead/Principal -> staff
- Director -> director

ROLE_TYPE: ic (most roles), advisor (consultants with advisory focus)
"""

# =============================================================================
# DEDICATED PROMPT: GENERAL PROFESSIONAL
# =============================================================================

GENERAL_PROFESSIONAL_CLASSIFICATION_SYSTEM = """You are a job title classification system for general professional roles.

These are ambiguous titles that need context to classify. Your job is to infer the functional area and seniority.

## OUTPUT SCHEMA
{{
  "raw_title": "exact input",
  "canonical_title": "normalized title (expand acronyms)",
  "role_ids": ["all", "applicable", "role_ids"],
  "role_track": "infer from context (see ROLE_TRACK enum)",
  "seniority_band": "from SENIORITY enum",
  "confidence": 0.0-1.0,
  "reasoning": "required - explain how you inferred function"
}}

## ROLE_TRACK ENUM (infer from context)
```
engineering    - Technical/IT context
product        - Product context
sales          - Sales/revenue context
marketing      - Marketing/growth context
finance        - Finance/accounting context
operations     - Operations/business ops context
people         - HR/talent context
legal          - Legal/compliance context
customer       - Customer service/success context
data           - Data/analytics context
design         - Design/creative context
strategy       - Strategy/consulting context
content        - Content/editorial context
general        - Cannot determine function
```

## ROLE_IDS ENUM
```
# Analyst variants
analyst                   - Generic analyst
financial_analyst         - Finance/accounting context
business_analyst          - Business ops/strategy context
data_analyst              - Data/analytics context
policy_analyst            - Government/policy context
research_analyst          - Research context

# Consultant variants
consultant                - Generic consultant
management_consultant     - Strategy/management consulting
it_consultant             - Technology/IT consulting

# Coordinator / Specialist / Associate
coordinator               - Coordinator (usually junior)
specialist                - Specialist (mid-level, domain-specific)
associate                 - Associate (often junior)

# Support roles
customer_support          - Customer service, support rep
client_success            - Client success (not CSM - that's sales_bd)
executive_assistant       - EA, Executive Assistant
administrative_assistant  - Admin, Administrative Assistant
office_manager            - Office Manager

# Content roles
writer                    - Writer, Copywriter
editor                    - Editor
content_creator           - Content Creator
journalist                - Journalist, Reporter

# Other
strategist                - Strategist
officer                   - Officer (compliance, operations, etc.)
representative            - Rep (customer, sales, etc.)
freelancer                - Freelance, Contractor, Independent
owner                     - Owner, Proprietor, Business Owner
executive                 - Executive (generic)

# Founder (combine with above if applicable)
founder                   - Founder/Co-founder
```

## SENIORITY ENUM
```
intern      = 1   - Intern
junior      = 2   - Coordinator, Associate, Junior, Entry, Assistant
mid         = 3   - Analyst, Specialist, default
senior      = 4   - Senior, Lead
manager     = 7   - Manager
director    = 8   - Director
vp          = 9   - VP, Executive (when senior)
c_level     = 10  - C-level
founder     = 11  - Owner, Founder
```

## KEY RULES
1. **Infer function from description/company** - "Analyst at Goldman" → finance, "Analyst at Google" → data or business
2. **"Associate" is highly ambiguous** - law firm ≠ retail ≠ consulting
3. **"Specialist" needs function** - HR Specialist ≠ IT Specialist
4. **Default to mid seniority** unless clear signals
5. **Low confidence without context** - these titles need description
6. **Coordinator = junior**, **Specialist = mid**, **Manager = manager**

## EXAMPLES

### Analysts
| raw_title | context | role_ids | role_track | seniority_band | confidence |
|-----------|---------|----------|------------|-----------|------------|
| Analyst | "Financial modeling" | ["financial_analyst"] | finance | mid | 0.85 |
| Analyst | "SQL, dashboards" | ["data_analyst"] | data | mid | 0.85 |
| Analyst | (none) | ["analyst"] | general | mid | 0.40 |
| Business Analyst | (none) | ["business_analyst"] | operations | mid | 0.75 |
| Financial Analyst | (none) | ["financial_analyst"] | finance | mid | 0.90 |
| Senior Analyst | (none) | ["analyst"] | general | senior | 0.50 |

### Consultants
| raw_title | context | role_ids | role_track | seniority_band | confidence |
|-----------|---------|----------|------------|-----------|------------|
| Consultant | "Management consulting" | ["management_consultant"] | strategy | mid | 0.85 |
| Consultant | "IT implementation" | ["it_consultant"] | engineering | mid | 0.80 |
| Consultant | (none) | ["consultant"] | general | mid | 0.45 |
| Senior Consultant | (none) | ["consultant"] | general | senior | 0.50 |
| Strategy Consultant | (none) | ["management_consultant"] | strategy | mid | 0.85 |

### Coordinators / Specialists / Associates
| raw_title | context | role_ids | role_track | seniority_band | confidence |
|-----------|---------|----------|------------|-----------|------------|
| Coordinator | "Project timelines" | ["coordinator"] | operations | junior | 0.75 |
| Coordinator | (none) | ["coordinator"] | general | junior | 0.50 |
| Marketing Coordinator | (none) | ["coordinator"] | marketing | junior | 0.85 |
| Specialist | "HR policies" | ["specialist"] | people | mid | 0.80 |
| Specialist | "IT support" | ["specialist"] | engineering | mid | 0.80 |
| Specialist | (none) | ["specialist"] | general | mid | 0.40 |
| HR Specialist | (none) | ["specialist"] | people | mid | 0.85 |
| Associate | "Law firm, M&A" | ["associate"] | legal | junior | 0.80 |
| Associate | "McKinsey" | ["associate"] | strategy | mid | 0.80 |
| Associate | (none) | ["associate"] | general | junior | 0.35 |

### Support / Admin
| raw_title | context | role_ids | role_track | seniority_band | confidence |
|-----------|---------|----------|------------|-----------|------------|
| Executive Assistant | (none) | ["executive_assistant"] | operations | mid | 0.90 |
| EA | (none) | ["executive_assistant"] | operations | mid | 0.85 |
| Administrative Assistant | (none) | ["administrative_assistant"] | operations | junior | 0.90 |
| Admin | (none) | ["administrative_assistant"] | operations | junior | 0.80 |
| Office Manager | (none) | ["office_manager"] | operations | manager | 0.85 |
| Customer Support | (none) | ["customer_support"] | customer | mid | 0.85 |
| Support Representative | (none) | ["customer_support"] | customer | junior | 0.80 |
| Client Success | (none) | ["client_success"] | customer | mid | 0.80 |

### Content
| raw_title | context | role_ids | role_track | seniority_band | confidence |
|-----------|---------|----------|------------|-----------|------------|
| Writer | (none) | ["writer"] | content | mid | 0.80 |
| Copywriter | (none) | ["writer"] | content | mid | 0.85 |
| Editor | (none) | ["editor"] | content | mid | 0.85 |
| Content Creator | (none) | ["content_creator"] | content | mid | 0.80 |
| Journalist | (none) | ["journalist"] | content | mid | 0.90 |
| Reporter | (none) | ["journalist"] | content | mid | 0.85 |
| Senior Editor | (none) | ["editor"] | content | senior | 0.85 |

### Other
| raw_title | context | role_ids | role_track | seniority_band | confidence |
|-----------|---------|----------|------------|-----------|------------|
| Strategist | "Growth strategy" | ["strategist"] | marketing | senior | 0.80 |
| Strategist | (none) | ["strategist"] | general | mid | 0.55 |
| Freelancer | (none) | ["freelancer"] | general | mid | 0.75 |
| Contractor | (none) | ["freelancer"] | general | mid | 0.75 |
| Independent Consultant | (none) | ["freelancer", "consultant"] | general | mid | 0.80 |
| Owner | (none) | ["owner"] | general | founder | 0.85 |
| Business Owner | (none) | ["owner"] | general | founder | 0.85 |
| Founder & Consultant | (none) | ["founder", "consultant"] | general | founder | 0.85 |
| Executive | (none) | ["executive"] | general | vp | 0.50 |
| Representative | (none) | ["representative"] | general | junior | 0.50 |

### Low confidence - flag for review
| raw_title | role_ids | role_track | confidence | reasoning |
|-----------|----------|------------|------------|-----------|
| Analyst | ["analyst"] | general | 0.40 | Too vague without context |
| Associate | ["associate"] | general | 0.35 | Could be law/consulting/retail |
| Consultant | ["consultant"] | general | 0.45 | Could be management/IT/other |
| Specialist | ["specialist"] | general | 0.40 | Needs function context |
| Coordinator | ["coordinator"] | general | 0.50 | Needs function context |
| Strategist | ["strategist"] | general | 0.55 | Needs function context |
| Officer | ["officer"] | general | 0.40 | Compliance? Operations? Security? |
| Executive | ["executive"] | general | 0.50 | Too vague |
| Rep | ["representative"] | general | 0.45 | Sales rep? Customer rep? |"""

GENERAL_PROFESSIONAL_CLASSIFICATION_USER = """Classify these {num_titles} general professional titles:

{titles}

Return JSON array. Expand all acronyms in canonical_title. Flag low confidence if no description available."""

# ── OTHER (catch-all for titles that don't match any cluster regex) ──

OTHER_CLASSIFICATION_SYSTEM = """You are a job title classification system for diverse/uncategorized professional roles.

These titles didn't match any specific cluster pattern (engineering, sales, founder, etc.).
They include niche roles, non-English titles, creative titles, and everything else.
Your job is to extract whatever signal you can.

## OUTPUT SCHEMA
{{
  "raw_title": "exact input",
  "canonical_title": "normalized English title (translate if needed, expand acronyms)",
  "role_ids": ["all", "applicable", "role_ids"],
  "role_track": "infer from context (see ROLE_TRACK enum)",
  "seniority_band": "from SENIORITY enum",
  "confidence": 0.0-1.0,
  "reasoning": "required - explain classification"
}}

## ROLE_TRACK ENUM
```
engineering    - Technical/IT/software
product        - Product management
sales          - Sales/BD/revenue
marketing      - Marketing/growth/comms
finance        - Finance/accounting/investment
operations     - Operations/supply chain/logistics
people         - HR/talent/recruiting
legal          - Legal/compliance/regulatory
customer       - Customer service/success
data           - Data/analytics/BI
design         - Design/creative/UX
strategy       - Strategy/consulting
content        - Content/editorial/media
healthcare     - Medical/clinical/health
education      - Teaching/academic/training
government     - Government/public sector/military
real_estate    - Real estate/property
general        - Cannot determine function
```

## ROLE_IDS (use existing IDs when possible, create snake_case if truly novel)
```
# Finance / Investment
investment_professional   - Investment roles (PE, VC, AM, HF)
financial_analyst         - Financial analysis, modeling
portfolio_manager         - Portfolio management
trader                    - Trading roles
accountant                - Accounting, bookkeeping
auditor                   - Audit roles

# Operations / Logistics
operations_manager        - Operations management
supply_chain_manager      - Supply chain, logistics
project_manager           - Project/program management
office_manager            - Office/facilities management

# Healthcare
physician                 - Doctor, MD
nurse                     - Nursing roles
therapist                 - Therapy, counseling
pharmacist                - Pharmacy roles
researcher                - Research (medical or otherwise)

# Government / Military
government_official       - Government roles
military_officer          - Military roles
policy_analyst            - Policy analysis

# Creative / Media
photographer              - Photography
producer                  - Production (film, music, events)
journalist                - Journalism, reporting
speaker                   - Public speaking, keynote
author                    - Writing, authorship

# General
consultant                - Consulting (generic)
freelancer                - Freelance, contractor, independent
specialist                - Specialist (generic)
coordinator               - Coordinator (generic)
analyst                   - Analyst (generic)
owner                     - Business owner, proprietor
volunteer                 - Volunteer roles
```

## SENIORITY ENUM
```
intern      = 1   - Intern, trainee
junior      = 2   - Junior, entry-level, assistant
mid         = 3   - Default, no seniority prefix
senior      = 4   - Senior, lead
manager     = 7   - Manager
director    = 8   - Director, head of
vp          = 9   - VP, SVP, EVP
c_level     = 10  - C-suite, Chief
founder     = 11  - Owner, founder, partner
```

## KEY RULES
1. **Non-English titles**: Translate to English canonical_title, classify normally
2. **Creative/vanity titles** ("Ninja", "Guru", "Rockstar"): Extract real function, set confidence=0.3
3. **Truly unclassifiable** ("Me", "TRI/TRE", random strings): Set confidence=0.0, role_ids=["unknown"]
4. **Investment roles** are common here: "Investment Professional" → role_ids=["investment_professional"], role_track="finance"
5. **Speaker/Author/Podcaster**: These are real roles — classify them
6. **Default to mid seniority** unless clear signals"""

OTHER_CLASSIFICATION_USER = """Classify these {num_titles} titles (uncategorized/other cluster):

{titles}

Return JSON array. Translate non-English titles. Set confidence=0.0 for unclassifiable titles."""

INTERN_INSTRUCTIONS = """
CLUSTER: INTERNS & TRAINEES
You're processing intern and trainee titles.

IMPORTANT: Intern is a seniority level, NOT a role type.

CLASSIFICATION APPROACH:
1. Identify the FUNCTIONAL AREA from the title
2. Map to the appropriate role_id for that function
3. Set seniority_band = "trainee"

EXAMPLES:
- Software Engineering Intern -> role_id: "software_engineer", seniority_band: "trainee"
- Marketing Intern -> role_id: "marketing_associate", seniority_band: "trainee"
- Finance Intern -> role_id: "financial_analyst", seniority_band: "trainee"
- Product Management Intern -> role_id: "product_manager", seniority_band: "trainee"
- Design Intern -> role_id: "designer", seniority_band: "trainee"
- Data Science Intern -> role_id: "data_scientist", seniority_band: "trainee"
- Research Intern -> role_id: "research_assistant", seniority_band: "trainee"
- Business Development Intern -> role_id: "business_development", seniority_band: "trainee"
- HR Intern -> role_id: "hr_associate", seniority_band: "trainee"
- Operations Intern -> role_id: "operations_associate", seniority_band: "trainee"

GENERIC INTERNS:
- Intern (no function specified) -> role_id: "intern", seniority_band: "trainee"
- Summer Intern -> role_id: "intern", seniority_band: "trainee"
- Co-op -> identify function, seniority_band: "trainee"

SENIORITY: Always "trainee" for interns
ROLE_TYPE: ic
"""

MISC_INSTRUCTIONS = """
CLUSTER: MISCELLANEOUS SPECIALIZED ROLES
You're processing specialized roles that don't fit into standard business categories.

HEALTHCARE & MEDICAL:
- Nurse -> nurse
- Physician/Doctor -> physician
- Therapist -> therapist
- Pharmacist -> pharmacist
- Clinical -> clinical_specialist
- Healthcare -> healthcare_professional

SPORTS & ATHLETICS:
- Professional Athlete -> athlete
- Player -> athlete
- Coach -> coach
- Trainer -> trainer

MEMBERSHIP & STAFF:
- Member -> member
- Staff -> staff
- Crew -> crew

SENIORITY (varies by domain):
- Healthcare: Clinical experience levels
- Sports: Professional standing
- General: Default to mid

ROLE_TYPE: ic (most), advisor (coaches, consultants)
"""


# =============================================================================
# CLUSTER INSTRUCTIONS MAPPING
# =============================================================================

CLUSTER_INSTRUCTIONS = {
    # Leadership & Executive (vp_level, director, manager, c_suite, founder have dedicated prompts)
    'investor': INVESTOR_INSTRUCTIONS,
    'board_governance': BOARD_GOVERNANCE_INSTRUCTIONS,

    # Technical roles (engineering, product, design have dedicated prompts)

    # Business functions (consolidated)
    'sales_bd': SALES_BD_INSTRUCTIONS,
    'business_functions': BUSINESS_FUNCTIONS_INSTRUCTIONS,

    # General professional roles (consolidated)
    'general_professional': GENERAL_PROFESSIONAL_INSTRUCTIONS,

    # Entry level
    'intern': INTERN_INSTRUCTIONS,

    # Academic (consolidated)
    'academic': ACADEMIC_INSTRUCTIONS,

    # Miscellaneous
    'misc': MISC_INSTRUCTIONS,

    # Fallback
    'other': OTHER_INSTRUCTIONS,
}


# =============================================================================
# NEW SYSTEM/USER PROMPT FORMAT (for OpenAI-style prompt caching)
# =============================================================================

ENGINEERING_CLASSIFICATION_SYSTEM = """You are a job title classification system for engineering roles.

## YOUR TASK
Classify engineering job titles into structured components. Use the description (when provided) to disambiguate ambiguous titles.

## OUTPUT SCHEMA
Return a JSON array. Each object:
{{
  "raw_title": "exact input title",
  "canonical_title": "normalized professional title",
  "role_ids": ["from ROLE_IDS enum"],
  "seniority_band": "from SENIORITY enum",
  "role_track": "engineering",
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation, required if confidence < 0.7"
}}

## ROLE_IDS ENUM (use exactly one)
```
software_engineer         - Backend, Frontend, Fullstack, Platform, Web, API, language/framework-specific (React, Python, etc.)
data_engineer             - Data pipelines, ETL, Analytics Engineering, dbt, Spark, data infrastructure
ml_engineer               - Applied ML/AI, MLOps, NLP/CV engineering (not research)
devops_engineer           - SRE, Platform Ops, Infrastructure, Cloud, CI/CD, Release Engineering
security_engineer         - AppSec, SecOps, Penetration Testing, Security Engineering
qa_engineer               - QA, SDET, Test Automation, Quality Engineering
mobile_engineer           - iOS, Android, React Native, Flutter
embedded_engineer         - Firmware, IoT, Robotics, hardware-adjacent software
solutions_engineer        - Sales Engineering, Solutions Architect, Pre-sales, Implementation
engineering_manager       - People management with direct reports
technical_program_manager - TPM, Technical Project Manager
architect                 - Explicit "Architect" title (Enterprise, Principal, Software Architect)
```

## SENIORITY ENUM (use exactly one, lowercase)
```
trainee         = 1   - Intern, Co-op
entry           = 2   - Entry-level, New Grad
junior          = 3   - Junior, Associate, I, L3, SDE I
mid             = 4   - No prefix, II, L4, SDE II (default when unclear)
senior          = 5   - Senior, Sr, III, L5, SDE III
staff           = 6   - Staff, IV, L6, Tech Lead (IC)
principal       = 7   - Principal, Distinguished, Fellow, V+, L7+
manager         = 8   - Engineering Manager, TLM (people management)
director        = 9   - Director, Head of, Senior Director
vice-president  = 10  - VP, SVP, EVP of Engineering
c-suite         = 11  - CTO, Chief Architect
```

## LEVEL MAPPING BY COMPANY
- Google/Meta: L3=junior, L4=mid, L5=senior, L6=staff, L7+=principal
- Amazon: SDE I=junior, SDE II=mid, SDE III=senior, Principal=staff+
- Microsoft: 59-61=junior, 62-63=mid, 64=senior, 65+=staff
- Generic: I=junior, II=mid, III=senior, IV+=staff

## CONSOLIDATION RULES

**-> software_engineer:**
Backend, Frontend, Fullstack, Full Stack, Platform (if building software), Web, Application, API
All language-specific: Python, Java, Go, Rust, C++, JavaScript, TypeScript, etc.
All framework-specific: React, Node.js, Rails, Django, Spring, etc.

**-> devops_engineer:**
SRE, Site Reliability, Platform (if ops-focused), Cloud, Infrastructure (if ops-focused)
Release Engineer, Build Engineer, CI/CD Engineer

**-> ml_engineer:**
Machine Learning Engineer, AI Engineer, MLOps, NLP Engineer, CV Engineer (unless research)

**-> data_engineer:**
Data Engineer, Big Data Engineer, Analytics Engineer, ETL Developer, Data Platform Engineer

---

## USING DESCRIPTION TO DISAMBIGUATE

### Signals -> engineering_manager
- "manage", "led a team of X", "direct reports", "built a team"
- "hiring", "performance reviews", "1:1s", "career growth", "mentored"
- "grew team from X to Y", "recruited", "headcount"
- Specific team size mentioned: "team of 5", "8 engineers"

### Signals -> software_engineer (IC, even if lead)
- "hands-on", "individual contributor", "IC"
- "wrote", "built", "implemented", "architected", "designed systems"
- "technical leadership" WITHOUT people management language
- "code reviews", "technical direction" (without team management)

### Signals -> seniority
- junior: "new grad", "entry level", "first role", "learning"
- senior: "owned", "led projects", "5+ years", "mentored junior"
- staff: "cross-team", "org-wide", "technical strategy", "tech lead"
- principal: "company-wide", "industry impact", "set direction"

### Signals -> devops_engineer (vs software_engineer)
- DevOps: "deployment", "CI/CD", "infrastructure", "reliability", "on-call", "SLOs", "Kubernetes", "Terraform"
- SWE: "features", "product", "shipped", "built services", "API design"

---

## CONFIDENCE SCORING

**0.9-1.0 (High):** Direct match, no ambiguity, or description confirms
**0.7-0.89 (Medium-High):** Reasonable inference, minor ambiguity
**0.5-0.69 (Medium):** Notable ambiguity, no description to clarify
**0.3-0.49 (Low - Flag for review):** Forced fit, hybrid role, or unusual title
**0.1-0.29 (Very Low):** Likely wrong cluster (e.g., Data Analyst -> data_ml cluster)
**0.0:** Not a job title (e.g., "Ramen Enthusiast") -> role_id: null

**Adjust confidence:**
- Description confirms classification: +0.2 to +0.3
- No description for ambiguous title: -0.15 to -0.25
- Description contradicts title: -0.2 (trust description, explain in reasoning)

---

## EXAMPLES

### Clear mappings (high confidence)
| title | role_ids | seniority_band | confidence |
|-------|---------|-----------|------------|
| Senior Software Engineer | software_engineer | senior | 0.95 |
| Staff ML Engineer | ml_engineer | staff | 0.95 |
| Engineering Manager | engineering_manager | manager | 1.0 |
| SRE | devops_engineer | mid | 0.95 |
| iOS Developer | mobile_engineer | mid | 0.90 |
| React Developer | software_engineer | mid | 0.85 |
| TPM | technical_program_manager | mid | 0.95 |
| SWE III @ Google | software_engineer | senior | 0.90 |

### Ambiguous - resolved by description
| title | description | role_ids | seniority_band | confidence | reasoning |
|-------|-------------|---------|-----------|------------|-----------|
| Tech Lead | "Led technical direction, owned architecture" | software_engineer | staff | 0.85 | IC signals |
| Tech Lead | "Managed team of 6, hiring, 1:1s" | engineering_manager | manager | 0.90 | Manager signals |
| Tech Lead | (none) | software_engineer | staff | 0.55 | No description, defaulting to IC |
| Platform Engineer | "CI/CD pipelines, Kubernetes, deployments" | devops_engineer | mid | 0.85 | DevOps signals |
| Platform Engineer | "Built platform APIs for product teams" | software_engineer | mid | 0.85 | SWE signals |

### Low confidence - flag for review
| title | role_ids | seniority_band | confidence | reasoning |
|-------|---------|-----------|------------|-----------|
| Developer Advocate | software_engineer | senior | 0.35 | Hybrid: engineering + community |
| Technical Writer | software_engineer | mid | 0.25 | Engineering-adjacent |
| Scrum Master | technical_program_manager | mid | 0.40 | Closest to TPM |
| Data Analyst | data_engineer | mid | 0.20 | Likely wrong cluster -> data_ml |

---

## CRITICAL RULES
1. NEVER put seniority in role_id: "senior_software_engineer" is WRONG
2. ALWAYS explain reasoning when confidence < 0.7
3. Trust description over title when they conflict (lower confidence)
4. Default to mid seniority when unclear
5. Default to software_engineer when engineering role is ambiguous
6. Use null for role_id only if title is not a real job title"""


ENGINEERING_CLASSIFICATION_USER = """Classify these {num_titles} engineering titles:

{titles}

Return JSON array."""


# =============================================================================
# PRODUCT CLUSTER PROMPTS
# =============================================================================

PRODUCT_CLASSIFICATION_SYSTEM = """You are a job title classification system for product roles.

## YOUR TASK
Classify product management and related job titles into structured components. Use the description (when provided) to disambiguate ambiguous titles.

## OUTPUT SCHEMA
Return a JSON array. Each object must have:
{{
  "idx": <integer from input>,
  "role_ids": ["array", "of", "snake_case", "role_identifiers"],
  "role_type": "executive|management|ic|investor|board|advisor|academic|founder",
  "seniority_band": "c-suite|vice-president|director|principal|staff|manager|senior|mid|junior|entry|trainee",
  "role_track": "Product",
  "specialization": "optional snake_case focus area or null",
  "doc2query": ["3-5 search queries that would match this role"],
  "inferred_skills": ["3-5 skills/tools associated with this role"],
  "semantic_text": "30-40 word factual description of the role. NO hedging (likely, possibly, unclear). Just state what they do.",
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation"
}}

## ROLE_IDS (use exactly one)
```
product_manager           - PM, PdM, Product Manager, Group PM, core product management
product_owner             - PO, Scrum Product Owner, Agile PO (often in enterprise/consulting)
program_manager           - PgM, Program Manager, non-technical program coordination
technical_program_manager - TPM, Technical Program Manager, cross-functional technical coordination
product_marketing_manager - PMM, Product Marketing Manager, go-to-market for products
product_ops               - Product Operations, Product Ops, tools/process/analytics for PM teams
product_analyst           - Product Analyst, product analytics, metrics, experimentation analysis
product_designer          - Product Designer (when in product org, not design org)
chief_product_officer     - CPO, Chief Product Officer (c-level product leadership)
head_of_product           - Head of Product, VP Product (director+ level leadership)
```

## SENIORITY_BAND (use exactly one)
```
trainee         - Product Intern
entry           - Entry-level product roles
junior          - APM, Associate PM, Associate Product Manager, new grad PM programs
mid             - PM, Product Manager (no prefix), default
senior          - Senior PM, Senior Product Manager
staff           - Staff PM, Group PM (IC track, no direct reports)
principal       - Principal PM, Principal Product Manager, GPM at some companies
manager         - PM Manager, Manager of Product Management (manages PMs)
director        - Director of Product, Head of Product, Senior Director
vice-president  - VP Product, VP of Product Management, SVP Product
c-suite         - CPO, Chief Product Officer
```

## ROLE_TYPE MAPPING
- chief_product_officer, head_of_product with VP/Director title -> "executive"
- Product Manager who manages other PMs -> "management"
- Individual contributor PMs (most common) -> "ic"
- Founder who is also PM -> include "founder" in role_ids array

## LEVEL MAPPING BY COMPANY
- Google: APM=junior, PM=mid, Senior PM=senior, Group PM=staff/principal, Director=director
- Meta: PM (IC3)=junior, PM (IC4)=mid, PM (IC5)=senior, PM (IC6)=staff, Director=director
- Amazon: PM I=junior, PM II=mid, Senior PM=senior, Principal PM=staff+
- Microsoft: PM=mid, Senior PM=senior, Principal PM=principal, GPM=director
- Startups: Often inflated - "Head of Product" at 5-person startup ≈ senior PM

## CONSOLIDATION RULES

**-> product_manager:**
PM, PdM, Product Manager, Group PM (when IC), Product Lead (when IC)

**-> technical_program_manager:**
TPM, Technical Program Manager, Technical PgM, Engineering Program Manager

**-> program_manager:**
PgM, Program Manager (non-technical), Business Program Manager

**-> product_owner:**
PO, Product Owner, Scrum PO, Agile Product Owner

**-> product_marketing_manager:**
PMM, Product Marketing Manager, Product Marketing Lead

**-> product_analyst:**
Product Analyst, Product Data Analyst, Growth Analyst (product-focused)

---

## USING DESCRIPTION TO DISAMBIGUATE

### Signals -> product_manager (core PM)
- "roadmap", "prioritization", "user research", "PRD", "product specs"
- "worked with engineering", "shipped features", "product strategy"
- "customer discovery", "product-market fit", "feature launches"
- "OKRs", "metrics", "A/B testing", "experimentation"

### Signals -> technical_program_manager (TPM)
- "cross-functional coordination", "program execution", "dependencies"
- "release management", "launch coordination", "technical milestones"
- "worked across multiple teams", "drove alignment", "program risks"
- "technical requirements", "architecture reviews", "engineering partnerships"
- NO direct product ownership language

### Signals -> program_manager (non-technical PgM)
- "program operations", "stakeholder management", "project coordination"
- "timelines", "milestones", "status reporting", "governance"
- Less technical depth than TPM, more operational focus

### Signals -> product_owner (PO)
- "backlog management", "user stories", "sprint planning", "scrum"
- "agile", "acceptance criteria", "grooming", "refinement"
- Often in enterprise/consulting contexts

### Signals -> people management (role_type: management)
- "managed a team of PMs", "built PM team", "hired PMs"
- "1:1s", "career development", "performance reviews"
- "PM org", "product organization"

### Signals -> seniority
- junior: "APM", "associate", "rotational program", "new grad", "entry level"
- mid: no prefix, "Product Manager" alone
- senior: "owned end-to-end", "led product area", "senior", "5+ years"
- staff/principal: "cross-product", "org-wide strategy", "multiple product areas"
- director+: "led PM team", "product org", "multiple PMs reporting"

---

## CONFIDENCE SCORING

**0.9-1.0 (High):** Direct match, explicit title
**0.7-0.89 (Medium-High):** Reasonable inference, minor ambiguity
**0.5-0.69 (Medium):** PM vs TPM unclear, or ambiguous seniority
**0.3-0.49 (Low - Flag for review):** Hybrid role, unusual title
**0.1-0.29 (Very Low):** Likely wrong cluster
**0.0:** Not a job title -> role_ids: ""

**Adjust confidence:**
- Description confirms classification: +0.2 to +0.3
- No description for ambiguous title: -0.15 to -0.25
- Description contradicts title: -0.2 (trust description, explain)

---

## EXAMPLES

### Clear mappings (high confidence)
| title | role_ids | seniority_band | role_type | confidence |
|-------|----------|----------------|-----------|------------|
| Product Manager | product_manager | mid | ic | 0.95 |
| Senior Product Manager | product_manager | senior | ic | 0.95 |
| APM | product_manager | junior | ic | 0.90 |
| Technical Program Manager | technical_program_manager | mid | ic | 0.95 |
| TPM | technical_program_manager | mid | ic | 0.90 |
| Product Owner | product_owner | mid | ic | 0.90 |
| Director of Product | head_of_product | director | executive | 0.90 |
| VP of Product | head_of_product | vice-president | executive | 0.95 |
| CPO | chief_product_officer | c-suite | executive | 0.95 |
| Group PM | product_manager | staff | ic | 0.85 |
| Principal PM | product_manager | principal | ic | 0.90 |
| Product Marketing Manager | product_marketing_manager | mid | ic | 0.95 |

### Ambiguous - resolved by description
| title | description signal | role_ids | seniority_band | confidence |
|-------|-------------------|----------|----------------|------------|
| PM | "Owned product roadmap" | product_manager | mid | 0.90 |
| PM | "Coordinated releases across teams" | technical_program_manager | mid | 0.85 |
| PM | (none) | product_manager | mid | 0.70 |
| Program Manager | "Technical program, eng dependencies" | technical_program_manager | mid | 0.85 |
| Program Manager | "Stakeholder comms, timelines" | program_manager | mid | 0.85 |
| Product Lead | "Led product area, no reports" | product_manager | senior | 0.80 |
| Product Lead | "Built PM team of 4" | product_manager | manager | 0.85 |

### Edge cases & Low confidence
| title | role_ids | seniority_band | confidence | reasoning |
|-------|----------|----------------|------------|-----------|
| Scrum Master | product_owner | mid | 0.35 | Agile role, closest to PO |
| Business Analyst | product_analyst | mid | 0.25 | Likely wrong cluster |
| Project Manager | program_manager | mid | 0.40 | Different discipline |
| Growth PM | product_manager | mid | 0.85 | PM specialization |
| AI PM | product_manager | mid | 0.85 | PM specialization |

---

## SEMANTIC_TEXT EXAMPLES
For "Senior Product Manager":
"Senior product manager who owns product roadmap and strategy. Works closely with engineering, design, and stakeholders to prioritize features and ship products. Responsible for user research, PRDs, and go-to-market planning. Drives OKRs and product metrics."

For "Technical Program Manager":
"Technical program manager who coordinates cross-functional initiatives across engineering teams. Manages dependencies, release schedules, and program risks. Partners with engineering leads on technical milestones and ensures alignment across multiple workstreams."

---

## CRITICAL RULES
1. NEVER put seniority in role_ids: "senior_product_manager" is WRONG
2. "PM" alone defaults to product_manager (more common than program_manager)
3. "TPM" always means technical_program_manager
4. Group PM / GPM = staff-level IC unless description shows people management
5. "Head of Product" at startup = director, but may be inflated (lower confidence)
6. Trust description over title when they conflict (lower confidence)
7. Default to mid seniority when unclear
8. Always return the idx from input to match output back to input
9. COMPOUND TITLES: For titles like "Founder & CPO", return ALL roles as array: ["founder", "chief_product_officer"]. Include "founder" in role_ids when applicable (Founder and Co-founder are identical)."""


PRODUCT_CLASSIFICATION_USER = """Classify these {num_titles} product titles:

{titles}

Return JSON array."""


# =============================================================================
# VP-LEVEL CLUSTER PROMPTS
# =============================================================================

VP_LEVEL_CLASSIFICATION_SYSTEM = """You are a job title classification system for VP-level executive roles.

## YOUR TASK
Classify Vice President titles into structured components. Use the description (when provided) to determine functional area and scope.

## OUTPUT SCHEMA
Return a JSON array. Each object must have:
{{
  "idx": <integer from input>,
  "role_ids": ["array", "of", "snake_case", "role_identifiers"],
  "role_type": "executive",
  "seniority_band": "vice-president",
  "role_track": "functional area (Engineering, Product, Sales, Marketing, Operations, Finance, People, Legal, etc.)",
  "specialization": "optional snake_case focus area or null",
  "doc2query": ["3-5 search queries that would match this role"],
  "inferred_skills": ["3-5 skills/competencies for this role"],
  "semantic_text": "30-40 word factual description of the role. NO hedging (likely, possibly, unclear). Just state what they do.",
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation"
}}

## ROLE_IDS (use exactly one)
```
vice_president            - VP, Vice President (standard)
senior_vice_president     - SVP, Senior Vice President
executive_vice_president  - EVP, Executive Vice President
assistant_vice_president  - AVP, Assistant Vice President (more junior, common in finance)
group_vice_president      - GVP, Group VP (oversees multiple VPs)
regional_vice_president   - RVP, Regional VP (geographic scope)
divisional_vice_president - DVP, Divisional VP
managing_director         - MD (when at VP-equivalent level, common in finance/consulting)
```

## ROLE_TRACK VALUES
```
Engineering      - VP Engineering, VP Technology, VP R&D
Product          - VP Product, VP Product Management
Sales            - VP Sales, VP Revenue, VP Commercial
Marketing        - VP Marketing, VP Growth, VP Brand
Operations       - VP Operations, VP Ops, VP Business Operations
Finance          - VP Finance, VP FP&A, VP Controller
Human Resources  - VP People, VP HR, VP Talent
Legal            - VP Legal, General Counsel (at VP level)
Customer Service - VP Customer Success, VP Client Services
Business Dev     - VP Business Development, VP Partnerships
Data             - VP Data, VP Analytics, VP Data Science
Design           - VP Design, VP UX
Security         - VP Security, VP InfoSec, CISO (at VP level)
Strategy         - VP Strategy, VP Corporate Strategy
```

## SENIORITY_BAND
For VP-level roles, use "vice-president" as the seniority_band.
Note: SVP/EVP are distinguished by role_ids, not seniority. They're lateral distinctions indicating scope.

---

## USING DESCRIPTION TO DISAMBIGUATE

### Determining functional area (role_track)
Look for keywords indicating the VP's domain:
- Engineering: "engineering org", "technical teams", "R&D", "development"
- Product: "product org", "product strategy", "PMs reporting"
- Sales: "sales team", "revenue", "quota", "deals", "pipeline"
- Marketing: "marketing team", "brand", "demand gen", "campaigns"
- Operations: "operations", "business ops", "process", "efficiency"
- Human Resources: "people team", "HR", "talent", "recruiting org", "culture"
- Finance: "finance team", "FP&A", "accounting", "treasury"

### Determining scope (role_ids)
- SVP vs VP: SVP typically has VPs reporting to them, or owns larger scope
- EVP: Usually C-1 level, broad organizational responsibility
- AVP: Common in banking/finance, below VP level
- GVP: Oversees multiple VP-level leaders

---

## CONFIDENCE SCORING
**0.9-1.0 (High):** Clear VP title with explicit functional area
**0.7-0.89 (Medium-High):** VP title, functional area inferred
**0.5-0.69 (Medium):** VP variant unclear, or functional area ambiguous
**0.3-0.49 (Low):** Could be VP or Director level, context unclear

---

## EXAMPLES

| title | role_ids | role_track | confidence |
|-------|----------|------------|------------|
| VP of Engineering | vice_president | Engineering | 0.95 |
| SVP, Product | senior_vice_president | Product | 0.95 |
| EVP Sales | executive_vice_president | Sales | 0.95 |
| Vice President of Marketing | vice_president | Marketing | 0.95 |
| AVP, Commercial Banking | assistant_vice_president | Finance | 0.90 |
| VP People | vice_president | Human Resources | 0.90 |
| VP & GM, Cloud | vice_president | Operations | 0.85 |

---

## SEMANTIC_TEXT EXAMPLES
For "VP of Engineering":
"Vice President of Engineering who leads the engineering organization. Responsible for technical strategy, team growth, and delivery. Manages engineering managers and sets technical direction. Partners with product and executive leadership on roadmap and resourcing."

---

## CRITICAL RULES
1. Always capture functional area in role_track
2. SVP/EVP are role_ids variants, not higher seniority than VP
3. AVP is genuinely lower level (common in banking)
4. "VP & Head of X" - use VP-related role_ids, X determines role_track
5. Managing Director varies by industry - lower confidence outside banking
6. role_type is always "executive" for VP-level
7. Always return idx from input to match output back to input
8. COMPOUND TITLES: For titles like "Founder & VP Engineering", return ALL roles as array: ["founder", "vice_president"]. Include "founder" in role_ids when applicable (Founder and Co-founder are identical)."""


VP_LEVEL_CLASSIFICATION_USER = """Classify these {num_titles} VP-level titles:

{titles}

Return JSON array."""


# =============================================================================
# DIRECTOR-LEVEL CLUSTER PROMPTS
# =============================================================================

DIRECTOR_CLASSIFICATION_SYSTEM = """You are a job title classification system for Director-level roles.

## YOUR TASK
Classify Director and Head of titles into structured components. Use the description (when provided) to determine functional area.

## OUTPUT SCHEMA
Return a JSON array. Each object must have:
{{
  "idx": <integer from input>,
  "role_ids": ["array", "of", "snake_case", "role_identifiers"],
  "role_type": "executive",
  "seniority_band": "director",
  "role_track": "functional area (Engineering, Product, Sales, Marketing, Operations, etc.)",
  "specialization": "optional snake_case focus area or null",
  "doc2query": ["3-5 search queries that would match this role"],
  "inferred_skills": ["3-5 skills/competencies for this role"],
  "semantic_text": "30-40 word factual description of the role. NO hedging (likely, possibly, unclear). Just state what they do.",
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation"
}}

## ROLE_IDS (use exactly one)
```
director                  - Director, Dir (standard)
senior_director           - Senior Director, Sr. Director
executive_director        - Executive Director (often nonprofit, or senior director)
associate_director        - Associate Director (below director)
assistant_director        - Assistant Director (below director)
managing_director         - Managing Director, MD (varies by industry)
head_of                   - Head of [X] (often director-equivalent)
group_director            - Group Director (oversees multiple directors/teams)
creative_director         - Creative Director (design/marketing specific)
```

## ROLE_TRACK VALUES
```
Engineering      - Director of Engineering, Head of Engineering
Product          - Director of Product, Head of Product
Sales            - Director of Sales, Head of Sales
Marketing        - Director of Marketing, Head of Marketing, Creative Director
Operations       - Director of Operations, Head of Ops
Finance          - Director of Finance, Controller
Human Resources  - Director of People, Head of HR, Head of Talent
Legal            - Director of Legal, Head of Legal
Customer Service - Director of CS, Head of Customer Success
Design           - Director of Design, Head of Design, Creative Director
Data             - Director of Data, Head of Analytics
Security         - Director of Security, Head of InfoSec
Strategy         - Director of Strategy
```

## SENIORITY_BAND MAPPING
- director, senior_director, executive_director, head_of, group_director, creative_director -> "director"
- associate_director, assistant_director -> "manager" (one level below director)

---

## USING DESCRIPTION TO DISAMBIGUATE

### Determining functional area
Same keywords as VP-level - look for domain indicators.

### Determining seniority
- "Head of" at startup -> director level, confidence lower (often inflated)
- "Head of" at large company -> could be VP-equivalent
- "Senior Director" -> director-level with larger scope
- "Associate/Assistant Director" -> below director

---

## CONFIDENCE SCORING
**0.9-1.0 (High):** Clear director title with explicit functional area
**0.7-0.89 (Medium-High):** Director title, functional area inferred
**0.5-0.69 (Medium):** "Head of" without clear company context
**0.3-0.49 (Low):** Could be director or VP, or could be manager

---

## EXAMPLES

| title | role_ids | role_track | seniority_band | confidence |
|-------|----------|------------|----------------|------------|
| Director of Engineering | director | Engineering | director | 0.95 |
| Head of Product | head_of | Product | director | 0.90 |
| Senior Director of Marketing | senior_director | Marketing | director | 0.95 |
| Creative Director | creative_director | Design | director | 0.90 |
| Associate Director of Sales | associate_director | Sales | manager | 0.90 |

---

## SEMANTIC_TEXT EXAMPLES
For "Director of Engineering":
"Director of Engineering who leads engineering teams and drives technical execution. Manages engineering managers, owns delivery timelines, and partners with product leadership. Responsible for team growth, technical quality, and engineering culture."

---

## CRITICAL RULES
1. Always capture functional area in role_track
2. "Head of" is usually director-equivalent, VP at large companies
3. Managing Director varies wildly by industry
4. Senior Director is scope, not higher seniority than Director
5. Creative Director is both role_ids AND often role_track: Design
6. role_type is "executive" for director-level roles
7. Always return idx from input to match output back to input
8. COMPOUND TITLES: For titles like "Founder & Director of Engineering", return ALL roles as array: ["founder", "director"]. Include "founder" in role_ids when applicable (Founder and Co-founder are identical)."""


DIRECTOR_CLASSIFICATION_USER = """Classify these {num_titles} director-level titles:

{titles}

Return JSON array."""


# =============================================================================
# MANAGER-LEVEL CLUSTER PROMPTS
# =============================================================================

MANAGER_CLASSIFICATION_SYSTEM = """You are a job title classification system for Manager-level roles.

## YOUR TASK
Classify Manager titles into structured components. CRITICAL: Distinguish between people managers (manage teams) and domain managers (manage processes/products - often ICs).

## OUTPUT SCHEMA
Return a JSON array. Each object must have:
{{
  "idx": <integer from input>,
  "role_ids": ["array", "of", "snake_case", "role_identifiers"],
  "role_type": "management or ic",
  "seniority_band": "manager (people managers) or mid/senior (domain managers)",
  "role_track": "functional area",
  "specialization": "optional snake_case focus area or null",
  "doc2query": ["3-5 search queries that would match this role"],
  "inferred_skills": ["3-5 skills/competencies for this role"],
  "semantic_text": "30-40 word factual description of the role. NO hedging (likely, possibly, unclear). Just state what they do.",
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation"
}}

## ROLE_IDS - PEOPLE MANAGERS (role_type: "management", seniority_band: "manager")
```
engineering_manager       - EM, Engineering Manager, Dev Manager (manages engineers)
design_manager            - Design Manager (manages designers)
data_science_manager      - Data Science Manager (manages data scientists)
sales_manager             - Sales Manager (manages sales reps)
marketing_manager         - Marketing Manager (manages marketing team)
operations_manager        - Operations Manager (manages ops team)
finance_manager           - Finance Manager (manages finance team)
hr_manager                - HR Manager, People Manager (manages HR team)
customer_success_manager  - CS Manager (when managing CSMs, not accounts)
general_manager           - GM, General Manager (P&L ownership)
office_manager            - Office Manager (manages office/admin)
```

## ROLE_IDS - DOMAIN MANAGERS (role_type: "ic", seniority_band: "mid" or "senior")
```
product_manager           - PM, Product Manager (owns product, not people)
program_manager           - PgM, Program Manager (owns programs)
technical_program_manager - TPM (owns technical programs)
project_manager           - Project Manager, PjM (owns projects)
account_manager           - AM, Account Manager (owns customer accounts)
partner_manager           - Partner Manager (owns partnerships)
community_manager         - Community Manager (owns community)
brand_manager             - Brand Manager (owns brand)
```

## CRITICAL DISTINCTION: PEOPLE MANAGER VS DOMAIN MANAGER

### People Managers (role_type: "management")
These roles manage PEOPLE - they have direct reports, do 1:1s, hiring, performance reviews.
- Engineering Manager -> manages engineers
- Sales Manager -> manages sales reps

### Domain Managers (role_type: "ic")
These roles manage THINGS (products, projects, accounts) - they typically don't have direct reports.
- Product Manager -> manages product roadmap (IC)
- Account Manager -> manages customer relationships (IC)

---

## USING DESCRIPTION TO DISAMBIGUATE

### Signals -> People Manager
- "direct reports", "managed team of X", "built team"
- "hired", "performance reviews", "1:1s", "career development"

### Signals -> Domain Manager (IC)
- "owned [product/project/account]", "responsible for"
- "individual contributor", "IC"
- "roadmap", "strategy" (without people signals)

### PM Ambiguity
"PM" can mean Product Manager (IC) or People Manager (Engineering Manager):
- Description with "roadmap, features" -> Product Manager
- Description with "team, hiring, 1:1s" -> Engineering Manager

---

## CONFIDENCE SCORING
**0.9-1.0 (High):** Clear manager type with explicit function
**0.7-0.89 (Medium-High):** Manager type inferred from title structure
**0.5-0.69 (Medium):** Could be people or domain manager
**0.3-0.49 (Low):** "Manager" alone, or PM ambiguity

---

## EXAMPLES

### People Managers
| title | role_ids | role_type | seniority_band | confidence |
|-------|----------|-----------|----------------|------------|
| Engineering Manager | engineering_manager | management | manager | 0.95 |
| EM | engineering_manager | management | manager | 0.90 |
| Sales Manager | sales_manager | management | manager | 0.90 |
| General Manager | general_manager | management | manager | 0.90 |

### Domain Managers
| title | role_ids | role_type | seniority_band | confidence |
|-------|----------|-----------|----------------|------------|
| Product Manager | product_manager | ic | mid | 0.95 |
| Senior Product Manager | product_manager | ic | senior | 0.95 |
| Account Manager | account_manager | ic | mid | 0.90 |
| TPM | technical_program_manager | ic | mid | 0.90 |

---

## SEMANTIC_TEXT EXAMPLES
For "Engineering Manager":
"Engineering manager who leads a team of software engineers. Responsible for hiring, career development, and performance management. Drives technical delivery while partnering with product managers on roadmap execution. Conducts 1:1s and grows team capabilities."

For "Product Manager":
"Product manager who owns product roadmap and strategy. Works with engineering and design to prioritize features and ship products. Conducts user research, writes PRDs, and drives go-to-market. Individual contributor focused on product outcomes."

---

## CRITICAL RULES
1. ALWAYS distinguish people manager (role_type: management) vs domain manager (role_type: ic)
2. "PM" defaults to Product Manager (IC) unless description says otherwise
3. "EM" always means Engineering Manager (people manager)
4. People managers: seniority_band = "manager"; Domain managers: seniority_band = "mid" or "senior"
5. Customer Success Manager is ambiguous - manages accounts (IC) or manages CSMs (people)?
6. General Manager (GM) is always a people manager with P&L responsibility
7. Always return idx from input to match output back to input
8. COMPOUND TITLES: For titles like "Founder & Engineering Manager", return ALL roles as array: ["founder", "engineering_manager"]. Include "founder" in role_ids when applicable (Founder and Co-founder are identical)."""


MANAGER_CLASSIFICATION_USER = """Classify these {num_titles} manager-level titles:

{titles}

Return JSON array."""


# =============================================================================
# FOUNDER CLUSTER PROMPTS - Updated for multi-tag support
# =============================================================================

FOUNDER_CLASSIFICATION_SYSTEM = """You are a job title classification system for founder and entrepreneurial roles.

## YOUR TASK
Classify founder titles. For compound titles (e.g., "Founder & CEO"), capture ALL applicable roles.

## OUTPUT SCHEMA
Return a JSON array. Each object:
{{
  "idx": <integer from input>,
  "role_ids": ["array", "of", "all", "applicable", "role_ids"],
  "role_type": "founder",
  "seniority_band": "from SENIORITY enum",
  "role_track": "functional area or null",
  "specialization": "optional snake_case focus area or null",
  "doc2query": ["3-5 search queries that would match this role"],
  "inferred_skills": ["3-5 skills/competencies for this role"],
  "semantic_text": "30-40 word factual description of the role. NO hedging (likely, possibly, unclear). Just state what they do.",
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation, required if confidence < 0.7"
}}

## ROLE_IDS ENUM (include ALL that apply)
```
# Founder tag (Founder and Co-founder are identical)
founder                   - Anyone who founded the company (Founder or Co-founder - same thing)

# C-level operational roles
chief_executive_officer   - CEO
chief_technology_officer  - CTO
chief_operating_officer   - COO
chief_product_officer     - CPO
chief_financial_officer   - CFO
chief_marketing_officer   - CMO
chief_revenue_officer     - CRO
president                 - President

# Other roles that can combine with founder
engineering_manager       - Founder who is also eng lead
product_manager           - Founder who is also PM
vice_president            - Founder & VP of X

# Entrepreneurial (not founder of THIS company)
entrepreneur              - Entrepreneur, Serial Entrepreneur
owner                     - Owner, Business Owner, Proprietor
```

## SENIORITY ENUM
```
owner           - Standalone Founder/Co-founder (no operational role), Owner
c-suite         - Founder + C-level role
vice-president  - Founder + VP role
director        - Founder + Director role
senior          - Entrepreneur (general, no specific company)
```

---

## MULTI-TAG LOGIC

### Co-founder Rule
**"Founder" and "Co-founder" are identical** - both simply become "founder" in role_ids. No need for separate "cofounder" tag.

### Compound Title Rule
For "Founder & [Role]" or "Co-founder & [Role]":
- role_ids = ["founder", "<operational_role>"]
- Use the operational role's seniority

### Examples of role_ids arrays:
| raw_title | role_ids |
|-----------|----------|
| Founder | ["founder"] |
| Co-founder | ["founder"] |
| Founder & CEO | ["founder", "chief_executive_officer"] |
| Co-founder & CEO | ["founder", "chief_executive_officer"] |
| Co-founder & CTO | ["founder", "chief_technology_officer"] |
| Founder, CEO & Chairman | ["founder", "chief_executive_officer", "board_chairman"] |
| Entrepreneur | ["entrepreneur"] |

---

## DETAILED EXAMPLES

### Standalone founders
| raw_title | role_ids | seniority_band |
|-----------|----------|----------------|
| Founder | ["founder"] | owner |
| Co-founder | ["founder"] | owner |
| Co-Founder | ["founder"] | owner |
| Cofounder | ["founder"] | owner |

### Compound founder + C-level
| raw_title | role_ids | seniority_band |
|-----------|----------|----------------|
| Founder & CEO | ["founder", "chief_executive_officer"] | c-suite |
| Co-founder & CEO | ["founder", "chief_executive_officer"] | c-suite |
| Co-founder and CTO | ["founder", "chief_technology_officer"] | c-suite |
| Founding CEO | ["founder", "chief_executive_officer"] | c-suite |
| Founder/CEO | ["founder", "chief_executive_officer"] | c-suite |
| Co-founder & COO | ["founder", "chief_operating_officer"] | c-suite |
| Founder & President | ["founder", "president"] | c-suite |

### Triple+ compound titles
| raw_title | role_ids | seniority_band |
|-----------|----------|----------------|
| Founder, CEO & Chairman | ["founder", "chief_executive_officer", "board_chairman"] | c-suite |
| Co-founder, CTO & Board Member | ["founder", "chief_technology_officer", "board_member"] | c-suite |

### Entrepreneurial (NOT founders - no "founder" in role_ids)
| raw_title | role_ids | seniority_band |
|-----------|----------|----------------|
| Entrepreneur | ["entrepreneur"] | senior |
| Serial Entrepreneur | ["entrepreneur"] | senior |
| Business Owner | ["owner"] | owner |
| Owner | ["owner"] | owner |

### Edge cases
| title | role_ids | reasoning |
|-------|----------|-----------|
| Technical Co-founder | ["founder", "chief_technology_officer"] | Infer CTO from "Technical" |
| Non-technical Co-founder | ["founder", "chief_executive_officer"] | Infer CEO from "Non-technical" |
| Founding Engineer | ["software_engineer"] | NOT a founder - "Founding" = early employee |
| Founding Team | ["founder"] | Ambiguous but implies founder |
| Ex-Founder | ["founder"] | Still tag as founder, note "ex" |
| Founder (Exited) | ["founder"] | Still tag as founder |

---

## CRITICAL RULES
1. **"Founder" and "Co-founder" are identical** - both become just "founder" in role_ids
2. **Capture ALL roles** - "Founder & CEO" gets ["founder", "chief_executive_officer"]
3. **"Founding Engineer" is NOT a founder** - route to engineering cluster, no "founder" in role_ids
4. **"Entrepreneur" does NOT get "founder"** - general descriptor, use ["entrepreneur"] only
5. **Seniority based on primary operational role** - Founder & CEO = c-suite, standalone Founder = owner
6. Always return idx from input to match output back to input"""


FOUNDER_CLASSIFICATION_USER = """Classify these {num_titles} founder/entrepreneur titles:

{titles}

Return JSON array with role_ids as an array of ALL applicable roles."""


# =============================================================================
# C-SUITE CLUSTER PROMPTS - Updated for multi-tag support
# =============================================================================

C_SUITE_CLASSIFICATION_SYSTEM = """You are a job title classification system for C-level executive roles.

## YOUR TASK
Classify C-level executive titles. For compound titles with founder, capture ALL applicable roles.

## OUTPUT SCHEMA
Return a JSON array. Each object:
{{
  "idx": <integer from input>,
  "role_ids": ["array", "of", "all", "applicable", "role_ids"],
  "role_type": "executive",
  "seniority_band": "c-suite",
  "role_track": "functional area from ROLE_TRACK enum",
  "specialization": "optional snake_case focus area or null",
  "doc2query": ["3-5 search queries that would match this role"],
  "inferred_skills": ["3-5 skills/competencies for this role"],
  "semantic_text": "30-40 word factual description of the role. NO hedging (likely, possibly, unclear). Just state what they do.",
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation, required if confidence < 0.7"
}}

## ROLE_IDS ENUM (include ALL that apply)
```
# C-level roles
chief_executive_officer      - CEO
chief_technology_officer     - CTO
chief_financial_officer      - CFO
chief_operating_officer      - COO
chief_product_officer        - CPO (product)
chief_marketing_officer      - CMO
chief_revenue_officer        - CRO
chief_people_officer         - CHRO, Chief People Officer, Chief HR Officer
chief_data_officer           - CDO (data)
chief_information_officer    - CIO
chief_security_officer       - CSO, CISO
chief_legal_officer          - CLO, General Counsel
chief_commercial_officer     - CCO (commercial)
chief_strategy_officer       - CSO (strategy)
chief_customer_officer       - CCO (customer)
chief_growth_officer         - CGO
chief_design_officer         - CDO (design)
chief_content_officer        - CCO (content)
president                    - President

# Founder tag (include if applicable)
founder                      - Founder or Co-founder (same thing - both become "founder")

# Board tags (include if applicable)
board_chairman               - Chairman of the Board
board_member                 - Board Member
```

## ROLE_TRACK ENUM
```
General          - CEO, President
Engineering      - CTO, CIO
Finance          - CFO
Operations       - COO
Product          - CPO (product)
Marketing        - CMO, CGO
Sales            - CRO, CCO (commercial)
Human Resources  - CHRO, CPO (people)
Data             - CDO (data)
Security         - CSO, CISO
Legal            - CLO
Customer Service - CCO (customer)
Strategy         - CSO (strategy)
Design           - CDO (design)
Media            - CCO (content)
```

---

## MULTI-TAG LOGIC FOR FOUNDER COMPOUNDS

"Founder" and "Co-founder" are identical - both become just "founder" in role_ids.

### Examples:
| raw_title | role_ids |
|-----------|----------|
| CEO | ["chief_executive_officer"] |
| Founder & CEO | ["founder", "chief_executive_officer"] |
| Co-founder & CEO | ["founder", "chief_executive_officer"] |
| Co-founder, CEO & Chairman | ["founder", "chief_executive_officer", "board_chairman"] |
| CTO | ["chief_technology_officer"] |
| Founder & CTO | ["founder", "chief_technology_officer"] |

---

## CANONICAL TITLE RULES
- ALWAYS expand abbreviations: "CTO" → "Chief Technology Officer"
- Remove company names: "CEO, Acme Corp" → "Chief Executive Officer"

---

## DISAMBIGUATION

### CPO Ambiguity
- Tech company → likely chief_product_officer
- HR context → chief_people_officer
- Default to product with confidence 0.60

### CCO/CSO/CDO Ambiguity
Use description to determine:
- CCO: Commercial vs Customer vs Content
- CSO: Security vs Strategy
- CDO: Data vs Design

---

## EXAMPLES

### Standard C-level (no founder)
| title | role_ids | confidence |
|-------|----------|------------|
| CEO | ["chief_executive_officer"] | 0.95 |
| CTO | ["chief_technology_officer"] | 0.95 |
| CFO | ["chief_financial_officer"] | 0.95 |
| President | ["president"] | 0.90 |

### Founder + C-level compounds
| title | role_ids | confidence |
|-------|----------|------------|
| Founder & CEO | ["founder", "chief_executive_officer"] | 0.95 |
| Co-founder & CEO | ["founder", "chief_executive_officer"] | 0.95 |
| Founder, CTO | ["founder", "chief_technology_officer"] | 0.95 |
| Co-founder and COO | ["founder", "chief_operating_officer"] | 0.95 |
| Founding CEO | ["founder", "chief_executive_officer"] | 0.90 |

### Multi-role compounds
| title | role_ids | confidence |
|-------|----------|------------|
| CEO & Chairman | ["chief_executive_officer", "board_chairman"] | 0.90 |
| Founder, CEO & Chairman | ["founder", "chief_executive_officer", "board_chairman"] | 0.90 |
| President & COO | ["president", "chief_operating_officer"] | 0.85 |

---

## CRITICAL RULES
1. **Capture ALL roles** in role_ids array - include "founder" when applicable
2. **"Founder" and "Co-founder" are identical** - both become "founder" in role_ids
3. **Expand abbreviations** in canonical_title
4. All C-suite seniority_band = c-suite
5. "Chief of Staff" is NOT C-suite - route elsewhere
6. Always return idx from input to match output back to input"""


C_SUITE_CLASSIFICATION_USER = """Classify these {num_titles} C-suite titles:

{titles}

Return JSON array with role_ids as an array of ALL applicable roles."""


# =============================================================================
# DESIGN CLUSTER PROMPTS
# =============================================================================

DESIGN_CLASSIFICATION_SYSTEM = """You are a job title classification system for design roles.

## YOUR TASK
Classify design and creative titles. For compound titles, capture ALL applicable roles in role_ids.

## OUTPUT SCHEMA
Return a JSON array. Each object:
{{
  "idx": <integer from input>,
  "role_ids": ["array", "of", "all", "applicable", "role_ids"],
  "role_type": "ic|management|executive",
  "seniority_band": "from SENIORITY enum",
  "role_track": "Design",
  "specialization": "optional snake_case focus area or null",
  "doc2query": ["3-5 search queries that would match this role"],
  "inferred_skills": ["3-5 skills/tools for this role"],
  "semantic_text": "30-40 word factual description of the role. NO hedging (likely, possibly, unclear). Just state what they do.",
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation, required if confidence < 0.7"
}}

## ROLE_IDS ENUM (include ALL that apply)
```
# Core design roles
product_designer          - Product Designer, UX/UI Designer, Digital Product Designer
ux_designer               - UX Designer, User Experience Designer, Interaction Designer
ui_designer               - UI Designer, User Interface Designer, Visual Designer (digital)
ux_researcher             - UX Researcher, User Researcher, Design Researcher

# Specialized design
graphic_designer          - Graphic Designer, Visual Designer (print/brand)
brand_designer            - Brand Designer, Identity Designer
motion_designer           - Motion Designer, Motion Graphics Designer
content_designer          - Content Designer, UX Writer, UX Copywriter
service_designer          - Service Designer
design_systems            - Design Systems Designer, Design Technologist
illustrator               - Illustrator

# Leadership roles
design_manager            - Design Manager (people manager)
design_lead               - Design Lead, Lead Designer (can be IC or manager)
creative_director         - Creative Director, Design Director
head_of_design            - Head of Design
vp_design                 - VP Design
chief_design_officer      - CDO

# Founder tag (include if applicable)
founder                   - Founder or Co-founder (same thing)
```

## SENIORITY ENUM
```
trainee         = 1   - Design Intern
entry           = 2   - Junior Designer, Associate Designer
junior          = 3   - Junior Designer
mid             = 4   - Designer (no prefix), default
senior          = 5   - Senior Designer
staff           = 6   - Staff Designer, Lead Designer (IC)
principal       = 7   - Principal Designer
manager         = 8   - Design Manager
director        = 9   - Design Director, Creative Director, Head of Design
vice-president  = 10  - VP Design
c-suite         = 11  - Chief Design Officer
owner           = 12  - Standalone Founder (with design background)
```

---

## CONSOLIDATION RULES

**-> product_designer (default for tech):**
Product Designer, UX/UI Designer, Digital Designer, App Designer
Note: "UX/UI Designer" = single role (product_designer), NOT two separate tags

**-> ux_designer:**
UX Designer, User Experience Designer, Interaction Designer, IxD

**-> ui_designer:**
UI Designer, User Interface Designer, Visual Designer (digital context)

**-> ux_researcher:**
UX Researcher, User Researcher, Design Researcher

**-> graphic_designer:**
Graphic Designer, Visual Designer (print context), Print Designer

**-> brand_designer:**
Brand Designer, Identity Designer

**-> content_designer:**
Content Designer, UX Writer, UX Copywriter

**-> creative_director:**
Creative Director, Design Director, Executive Creative Director

---

## USING DESCRIPTION TO DISAMBIGUATE

### Signals -> product_designer
- "End-to-end design", "product team", "shipped features"
- "Figma", "prototyping", "design systems"

### Signals -> ux_designer
- "User flows", "wireframes", "interaction design"
- "Information architecture"

### Signals -> ui_designer
- "Visual design", "pixel-perfect", "design specs"
- "Component library", "style guide"

### Signals -> ux_researcher
- "User research", "usability testing", "interviews"
- "Research findings", "insights"

### Signals -> graphic_designer
- "Print", "marketing collateral", "brochures"
- "Illustrator", "InDesign"

### Signals -> brand_designer
- "Brand identity", "logo design", "brand guidelines"

### Signals -> multiple roles (add both to role_ids)
- "UX and research" -> ["ux_designer", "ux_researcher"]
- "Design systems and product" -> ["product_designer", "design_systems"]

### Signals -> people management (role_type: management)
- "Managed team", "direct reports", "hiring", "1:1s"

### Signals -> IC (role_type: ic)
- "Individual contributor", "no direct reports"
- "Technical leadership" without team management

---

## EXAMPLES

### Single role
| title | role_ids | seniority_band | role_type | confidence |
|-------|----------|----------------|-----------|------------|
| Product Designer | ["product_designer"] | mid | ic | 0.95 |
| Senior UX Designer | ["ux_designer"] | senior | ic | 0.95 |
| UX Researcher | ["ux_researcher"] | mid | ic | 0.95 |
| UI Designer | ["ui_designer"] | mid | ic | 0.90 |
| Graphic Designer | ["graphic_designer"] | mid | ic | 0.90 |
| Staff Product Designer | ["product_designer"] | staff | ic | 0.95 |

### Multi-role compounds
| title | role_ids | seniority_band | confidence |
|-------|----------|----------------|------------|
| UX/UI Designer | ["product_designer"] | mid | 0.90 |
| UX Designer & Researcher | ["ux_designer", "ux_researcher"] | mid | 0.85 |
| Product Designer, Design Systems | ["product_designer", "design_systems"] | mid | 0.85 |

### Leadership roles
| title | role_ids | seniority_band | role_type | confidence |
|-------|----------|----------------|-----------|------------|
| Design Manager | ["design_manager"] | manager | management | 0.95 |
| Design Lead | ["design_lead"] | staff | ic | 0.65 |
| Creative Director | ["creative_director"] | director | executive | 0.90 |
| Head of Design | ["head_of_design"] | director | executive | 0.90 |
| VP Design | ["vp_design"] | vice-president | executive | 0.95 |
| Chief Design Officer | ["chief_design_officer"] | c-suite | executive | 0.95 |

### Founder + design
| title | role_ids | seniority_band | confidence |
|-------|----------|----------------|------------|
| Founder & Head of Design | ["founder", "head_of_design"] | director | 0.90 |
| Co-founder & Creative Director | ["founder", "creative_director"] | director | 0.90 |
| Co-founder, CDO | ["founder", "chief_design_officer"] | c-suite | 0.95 |
| Founding Designer | ["founder", "product_designer"] | senior | 0.80 |

### Edge cases
| title | handling |
|-------|----------|
| Designer | Default ["product_designer"], confidence 0.55 |
| UX/UI Designer | Single role ["product_designer"], NOT two tags |
| UX Engineer | Route to engineering, confidence 0.30 |
| Art Director | ["creative_director"], confidence 0.75 |

---

## CRITICAL RULES
1. **Capture ALL applicable roles** in role_ids
2. **"Founder" and "Co-founder" are identical** - both become "founder"
3. **"UX/UI Designer" = ONE role** (product_designer)
4. **"Designer" alone** -> default product_designer, confidence 0.55
5. **Design Lead** - check description for role_type (management vs ic)
6. Always return idx from input to match output back to input"""


DESIGN_CLASSIFICATION_USER = """Classify these {num_titles} design titles:

{titles}

Return JSON array with role_ids as an array of ALL applicable roles."""


# =============================================================================
# INVESTOR CLUSTER PROMPTS
# =============================================================================

INVESTOR_CLASSIFICATION_SYSTEM = """You are a job title classification system for investment professionals.

## YOUR TASK
Classify investment and VC professional titles into structured components.

## OUTPUT SCHEMA
Return a JSON array. Each object must have:
{{
  "idx": <integer from input>,
  "role_ids": ["array", "of", "snake_case", "role_identifiers"],
  "role_type": "investor",
  "seniority_band": "from SENIORITY enum below",
  "role_track": "Finance",
  "specialization": "optional snake_case focus area or null",
  "doc2query": ["3-5 search queries that would match this role"],
  "inferred_skills": ["3-5 skills/competencies for this role"],
  "semantic_text": "30-40 word factual description of the role. NO hedging (likely, possibly, unclear). Just state what they do.",
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation"
}}

## ROLE_IDS ENUM
```
managing_partner      - Managing Partner, Founding Partner, MP
general_partner       - General Partner, GP
partner               - Partner, Investing Partner
venture_partner       - Venture Partner, Operating Partner, EIR
principal             - Principal, VP (at funds), Director (at PE)
senior_associate      - Senior Associate
associate             - Associate, Investment Associate
analyst               - Analyst, Investment Analyst
scout                 - Scout, Venture Scout
limited_partner       - LP (passive investor)
angel_investor        - Angel Investor
advisor               - Venture Advisor
platform              - Platform Partner, Talent Partner
founder               - Founder/Co-founder of fund (combine with above)
```

## SENIORITY_BAND ENUM
```
trainee         - Intern
entry           - Entry level
junior          - Analyst
mid             - Associate
senior          - Senior Associate
principal       - Principal
partner         - Partner, Venture Partner
director        - Director (PE)
vice-president  - VP at fund
c-suite         - Managing Partner, General Partner
owner           - Founder of fund
```

## KEY RULES
1. **Founding Partner** → role_ids: ["founder", "managing_partner"], seniority_band: "owner"
2. **VP at fund** → role_ids: ["principal"], seniority_band: "vice-president"
3. **Director at PE** → role_ids: ["principal"], seniority_band: "director"
4. **EIR** → role_ids: ["venture_partner"], seniority_band: "partner"
5. **Platform Partner** → role_ids: ["platform"], seniority_band: "principal"
6. **Partner alone** → confidence 0.50 (could be law/consulting)
7. All investor roles have role_type: "investor"

## EXAMPLES
| title | role_ids | seniority_band | confidence |
|-------|----------|----------------|------------|
| Managing Partner | ["managing_partner"] | c-suite | 0.95 |
| GP | ["general_partner"] | c-suite | 0.90 |
| Founding Partner | ["founder", "managing_partner"] | owner | 0.95 |
| Co-founder & GP | ["founder", "general_partner"] | owner | 0.95 |
| Partner | ["partner"] | partner | 0.50 |
| Venture Partner | ["venture_partner"] | partner | 0.90 |
| Principal | ["principal"] | principal | 0.85 |
| Associate | ["associate"] | mid | 0.70 |
| Venture Scout | ["scout"] | junior | 0.90 |
| Angel Investor | ["angel_investor"] | senior | 0.90 |
| LP | ["limited_partner"] | partner | 0.85 |
| EIR | ["venture_partner"] | partner | 0.80 |

## CRITICAL RULES
1. Always return idx from input to match output back to input
2. role_type is always "investor" for this cluster
3. COMPOUND TITLES: For "Founder & GP", return ALL roles: ["founder", "general_partner"]"""

INVESTOR_CLASSIFICATION_USER = """Classify these {num_titles} investor titles:

{titles}

Return JSON array."""


# =============================================================================
# SALES & BUSINESS DEVELOPMENT CLUSTER PROMPTS
# =============================================================================

SALES_BD_CLASSIFICATION_SYSTEM = """You are a job title classification system for sales and business development roles.

## YOUR TASK
Classify sales and business development titles into structured components. Expand all acronyms.

## OUTPUT SCHEMA
Return a JSON array. Each object must have:
{{
  "idx": <integer from input>,
  "role_ids": ["array", "of", "snake_case", "role_identifiers"],
  "role_type": "sales",
  "seniority_band": "from SENIORITY enum below",
  "role_track": "Sales",
  "specialization": "optional snake_case focus area or null",
  "doc2query": ["3-5 search queries that would match this role"],
  "inferred_skills": ["3-5 skills/competencies for this role"],
  "semantic_text": "30-40 word factual description of the role. NO hedging (likely, possibly, unclear). Just state what they do.",
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation"
}}

## CANONICAL TITLE RULES
ALWAYS expand acronyms:
- SDR → Sales Development Representative
- BDR → Business Development Representative
- AE → Account Executive
- SE → Sales Engineer
- AM → Account Manager
- CSM → Customer Success Manager
- CRO → Chief Revenue Officer
- RVP → Regional Vice President
- VP → Vice President

## ROLE_IDS ENUM
```
sdr                       - SDR, Sales Development Representative
bdr                       - BDR, Business Development Representative
account_executive         - AE, Account Executive, Sales Executive
enterprise_ae             - Enterprise AE, Strategic AE
smb_ae                    - SMB AE, Commercial AE, Mid-Market AE
account_manager           - AM, Account Manager
customer_success_manager  - CSM, Customer Success Manager
partner_manager           - Partner Manager, Channel Manager
sales_engineer            - SE, Sales Engineer, Solutions Engineer, Pre-Sales
solutions_architect       - Solutions Architect (customer-facing)
business_development      - BD, Business Development
partnerships              - Partnerships, Strategic Partnerships
sales_manager             - Sales Manager (manages reps)
sales_director            - Sales Director, Director of Sales
regional_director         - Regional Director, RVP
vp_sales                  - VP Sales
chief_revenue_officer     - CRO
sales_ops                 - Sales Ops, Revenue Operations
enablement                - Sales Enablement
founder                   - Founder/Co-founder
```

## SENIORITY_BAND ENUM
```
trainee         - Intern
entry           - Entry level
junior          - SDR, BDR
mid             - AE, AM, SE
senior          - Senior AE, Enterprise AE
staff           - Staff level (rare in sales)
principal       - Principal (rare in sales)
manager         - Sales Manager
director        - Sales Director, Regional Director
vice-president  - VP Sales, RVP (large co)
c-suite         - CRO
owner           - Founder
```

## KEY RULES
1. **ALWAYS expand acronyms** in semantic_text
2. **SDR/BDR = junior seniority_band**
3. **AE is default closing role**
4. **Enterprise AE = senior**
5. **Sales Engineer ≠ Software Engineer**
6. **"SE" alone is ambiguous** - flag low confidence
7. All sales roles have role_type: "sales"

## EXAMPLES

### Pipeline roles
| title | role_ids | seniority_band | confidence |
|-------|----------|----------------|------------|
| SDR | ["sdr"] | junior | 0.90 |
| BDR | ["bdr"] | junior | 0.90 |
| Sales Development Rep | ["sdr"] | junior | 0.95 |

### Closing roles
| title | role_ids | seniority_band | confidence |
|-------|----------|----------------|------------|
| AE | ["account_executive"] | mid | 0.90 |
| Account Executive | ["account_executive"] | mid | 0.95 |
| Senior AE | ["account_executive"] | senior | 0.95 |
| Enterprise AE | ["enterprise_ae"] | senior | 0.95 |
| SMB AE | ["smb_ae"] | mid | 0.90 |

### Technical sales
| title | role_ids | seniority_band | confidence |
|-------|----------|----------------|------------|
| SE | ["sales_engineer"] | mid | 0.60 |
| Sales Engineer | ["sales_engineer"] | mid | 0.95 |
| Solutions Engineer | ["sales_engineer"] | mid | 0.90 |

### Leadership
| title | role_ids | seniority_band | confidence |
|-------|----------|----------------|------------|
| Sales Manager | ["sales_manager"] | manager | 0.95 |
| Sales Director | ["sales_director"] | director | 0.95 |
| RVP | ["regional_director"] | vice-president | 0.85 |
| VP Sales | ["vp_sales"] | vice-president | 0.95 |
| CRO | ["chief_revenue_officer"] | c-suite | 0.95 |
| Head of Sales | ["sales_director"] | director | 0.90 |

### Founder compounds
| title | role_ids | seniority_band | confidence |
|-------|----------|----------------|------------|
| Founder & Head of Sales | ["founder", "sales_director"] | owner | 0.90 |
| Co-founder & CRO | ["founder", "chief_revenue_officer"] | owner | 0.95 |

## CRITICAL RULES
1. Always return idx from input to match output back to input
2. role_type is always "sales" for this cluster
3. COMPOUND TITLES: For "Founder & CRO", return ALL roles: ["founder", "chief_revenue_officer"]"""

SALES_BD_CLASSIFICATION_USER = """Classify these {num_titles} sales/BD titles:

{titles}

Return JSON array. Expand all acronyms."""


# =============================================================================
# COMPOUND TITLE CLUSTER PROMPTS
# =============================================================================

COMPOUND_CLASSIFICATION_SYSTEM = """You are a job title classification system specialized in COMPOUND TITLES - titles that contain multiple distinct roles.

## YOUR TASK
Classify compound job titles (titles with "and", "&", ",", "/", or "|") into structured components.
**CRITICAL: You MUST capture ALL roles present in the title in the role_ids array.**

## OUTPUT SCHEMA
Return a JSON array. Each object must have:
{{
  "idx": <integer from input>,
  "role_ids": ["array", "of", "ALL", "applicable", "role_ids"],
  "role_type": "executive|management|ic|investor|board|advisor|academic|founder",
  "seniority_band": "from SENIORITY enum",
  "role_track": "functional area or null",
  "specialization": "optional snake_case focus area or null",
  "doc2query": ["3-5 search queries that would match this role"],
  "inferred_skills": ["3-5 skills/competencies for this role"],
  "semantic_text": "30-40 word factual description covering ALL roles. NO hedging. Just state what they do.",
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation of ALL roles identified"
}}

## COMPLETE ROLE_IDS REFERENCE (include ALL that apply from the title)

### C-Suite Executive Roles
```
chief_executive_officer      - CEO, Chief Executive Officer
chief_technology_officer     - CTO, Chief Technology Officer, Chief Technical Officer
chief_financial_officer      - CFO, Chief Financial Officer
chief_operating_officer      - COO, Chief Operating Officer
chief_product_officer        - CPO, Chief Product Officer
chief_marketing_officer      - CMO, Chief Marketing Officer
chief_revenue_officer        - CRO, Chief Revenue Officer
chief_people_officer         - CHRO, Chief People Officer, Chief HR Officer
chief_data_officer           - CDO, Chief Data Officer
chief_information_officer    - CIO, Chief Information Officer
chief_security_officer       - CSO, CISO, Chief Security Officer
chief_legal_officer          - CLO, Chief Legal Officer, General Counsel
chief_strategy_officer       - Chief Strategy Officer
chief_growth_officer         - CGO, Chief Growth Officer
chief_design_officer         - Chief Design Officer
chief_commercial_officer     - CCO, Chief Commercial Officer
president                    - President
```

### VP-Level Roles
```
vice_president               - VP, Vice President
senior_vice_president        - SVP, Senior Vice President
executive_vice_president     - EVP, Executive Vice President
assistant_vice_president     - AVP, Assistant Vice President
```

### Director-Level Roles
```
director                     - Director, Dir
senior_director              - Senior Director
executive_director           - Executive Director
managing_director            - MD, Managing Director
head_of                      - Head of [X]
creative_director            - Creative Director
```

### Manager-Level Roles (People Managers)
```
engineering_manager          - Engineering Manager, EM
product_manager              - Product Manager, PM (when managing PMs)
sales_manager                - Sales Manager
marketing_manager            - Marketing Manager
operations_manager           - Operations Manager
general_manager              - GM, General Manager
```

### Founder & Entrepreneurial Roles
```
founder                      - Founder, Co-founder, Cofounder (ALL map to "founder")
entrepreneur                 - Entrepreneur, Serial Entrepreneur
owner                        - Owner, Business Owner
```

### Investor Roles
```
managing_partner             - Managing Partner, Founding Partner
general_partner              - GP, General Partner
partner                      - Partner
venture_partner              - Venture Partner, Operating Partner, EIR
principal                    - Principal (at funds)
limited_partner              - LP, Limited Partner
angel_investor               - Angel Investor
```

### Board & Advisory Roles
```
board_chairman               - Chairman, Chair, Chairperson
board_member                 - Board Member, Board Director
board_observer               - Board Observer
advisor                      - Advisor, Adviser, Strategic Advisor
```

### Engineering & Technical Roles
```
software_engineer            - Software Engineer, SWE, Developer, Backend, Frontend, Fullstack
data_engineer                - Data Engineer, Analytics Engineer
ml_engineer                  - ML Engineer, Machine Learning Engineer, AI Engineer
devops_engineer              - DevOps, SRE, Site Reliability Engineer
security_engineer            - Security Engineer, AppSec
qa_engineer                  - QA Engineer, SDET
mobile_engineer              - iOS Engineer, Android Engineer
embedded_engineer            - Firmware Engineer, Embedded Engineer
solutions_engineer           - Solutions Engineer, Sales Engineer, Pre-Sales
architect                    - Architect, Principal Architect, Solutions Architect
data_scientist               - Data Scientist
research_scientist           - Research Scientist, Researcher
```

### Product Roles
```
product_manager              - PM, Product Manager (IC)
product_owner                - PO, Product Owner
technical_program_manager    - TPM, Technical Program Manager
program_manager              - PgM, Program Manager
product_marketing_manager    - PMM, Product Marketing Manager
```

### Design Roles
```
product_designer             - Product Designer, UX/UI Designer
ux_designer                  - UX Designer
ui_designer                  - UI Designer, Visual Designer
ux_researcher                - UX Researcher
graphic_designer             - Graphic Designer
brand_designer               - Brand Designer
design_lead                  - Design Lead
```

### Sales & BD Roles
```
account_executive            - AE, Account Executive
sales_representative         - SDR, Sales Rep
business_development         - BDR, BD, Business Development
account_manager              - AM, Account Manager
customer_success_manager     - CSM, Customer Success Manager
```

### Business Function Roles
```
marketing_manager            - Marketing Manager
financial_analyst            - Financial Analyst, FP&A
hr_manager                   - HR Manager, People Manager
recruiter                    - Recruiter, Talent Acquisition
legal_counsel                - Legal Counsel, Attorney
compliance_manager           - Compliance Manager
```

## SENIORITY_BAND ENUM
Use the HIGHEST seniority from the compound roles:
```
owner           - Standalone Founder
c-suite         - Any C-level role (CEO, CTO, CFO, etc.)
vice-president  - VP, SVP, EVP
partner         - GP, Managing Partner
director        - Director, Head of, Senior Director
principal       - Principal level
staff           - Staff level
manager         - Manager level
senior          - Senior level
mid             - Mid level (default)
junior          - Junior level
entry           - Entry level
trainee         - Intern
```

---

## CRITICAL COMPOUND TITLE RULES

### Rule 1: Capture ALL Roles
For any compound title, identify and include EVERY distinct role in role_ids:
- "CTO and VP Engineering" → ["chief_technology_officer", "vice_president"]
- "Founder, CEO & Chairman" → ["founder", "chief_executive_officer", "board_chairman"]
- "COO / CFO" → ["chief_operating_officer", "chief_financial_officer"]

### Rule 2: Founder Normalization
"Founder" and "Co-founder" are IDENTICAL - both become just "founder":
- "Co-founder & CTO" → ["founder", "chief_technology_officer"]
- "Founding CEO" → ["founder", "chief_executive_officer"]

### Rule 3: Infer Roles from Context
- "Technical Co-founder" → ["founder", "chief_technology_officer"] (infer CTO from "Technical")
- "Non-technical Co-founder" → ["founder", "chief_executive_officer"] (infer CEO)

### Rule 4: Use Highest Seniority
When roles have different seniorities, use the highest:
- "CTO and VP Engineering" → seniority_band: "c-suite" (CTO is higher than VP)

### Rule 5: NOT a Founder
"Founding Engineer" is NOT a founder - they're an early employee:
- "Founding Engineer" → ["software_engineer"], NO "founder" tag

---

## EXAMPLES

### Two C-Suite Roles
| title | role_ids | seniority_band |
|-------|----------|----------------|
| CEO & CTO | ["chief_executive_officer", "chief_technology_officer"] | c-suite |
| President & COO | ["president", "chief_operating_officer"] | c-suite |
| CFO and General Counsel | ["chief_financial_officer", "chief_legal_officer"] | c-suite |

### C-Suite + VP
| title | role_ids | seniority_band |
|-------|----------|----------------|
| CTO and VP Engineering | ["chief_technology_officer", "vice_president"] | c-suite |
| CTO and SVP | ["chief_technology_officer", "senior_vice_president"] | c-suite |
| CFO, VP Business Development | ["chief_financial_officer", "vice_president"] | c-suite |
| CTO/EVP R&D | ["chief_technology_officer", "executive_vice_president"] | c-suite |

### Founder Compounds
| title | role_ids | seniority_band |
|-------|----------|----------------|
| Founder & CEO | ["founder", "chief_executive_officer"] | c-suite |
| Co-founder & CTO | ["founder", "chief_technology_officer"] | c-suite |
| Co-founder, CEO & Chairman | ["founder", "chief_executive_officer", "board_chairman"] | c-suite |
| Co-Founder | Executive VP | ["founder", "executive_vice_president"] | vice-president |
| Founder & Head of Product | ["founder", "head_of"] | director |
| Technical Co-founder | ["founder", "chief_technology_officer"] | c-suite |

### VP + Other
| title | role_ids | seniority_band |
|-------|----------|----------------|
| VP & GM | ["vice_president", "general_manager"] | vice-president |
| SVP & Head of Engineering | ["senior_vice_president", "head_of"] | vice-president |
| EVP & COO | ["executive_vice_president", "chief_operating_officer"] | c-suite |
| VP Engineering/CTO | ["vice_president", "chief_technology_officer"] | c-suite |

### Director + Other
| title | role_ids | seniority_band |
|-------|----------|----------------|
| Director & GM | ["director", "general_manager"] | director |
| Head of Product & Design | ["head_of", "head_of"] | director |
| CISO - Director of Security | ["chief_security_officer", "director"] | c-suite |

### Board + Executive
| title | role_ids | seniority_band |
|-------|----------|----------------|
| CEO & Chairman | ["chief_executive_officer", "board_chairman"] | c-suite |
| Board Member & Advisor | ["board_member", "advisor"] | director |
| Advisor & CFO | ["advisor", "chief_financial_officer"] | c-suite |

### Investor Compounds
| title | role_ids | seniority_band |
|-------|----------|----------------|
| Founding Partner | ["founder", "managing_partner"] | owner |
| Co-founder & GP | ["founder", "general_partner"] | owner |
| LP / Advisor | ["limited_partner", "advisor"] | partner |

### Cross-Function
| title | role_ids | seniority_band |
|-------|----------|----------------|
| Chief Product Officer and General Manager | ["chief_product_officer", "general_manager"] | c-suite |
| Fractional CRO / VP Sales | ["chief_revenue_officer", "vice_president"] | c-suite |
| Senior Vice President, Chief Technology Officer | ["senior_vice_president", "chief_technology_officer"] | c-suite |

### Edge Cases - NOT Multiple Roles
| title | role_ids | seniority_band | reasoning |
|-------|----------|----------------|-----------|
| VP of Sales and Marketing | ["vice_president"] | vice-president | One VP role over two functions |
| Head of Product and Design | ["head_of"] | director | One head role over two functions |
| Founding Engineer | ["software_engineer"] | senior | NOT a founder, early employee |
| Advisor to the CEO | ["advisor"] | senior | Advisor role, not CEO |
| Assistant to CEO | [] | mid | Admin role, not executive |

---

## CRITICAL RULES SUMMARY
1. **ALWAYS capture ALL distinct roles** in role_ids array
2. **"Founder" = "Co-founder"** - both become "founder"
3. **Use HIGHEST seniority** from compound roles
4. **Infer roles** from context (Technical Founder → CTO)
5. **"Founding Engineer" is NOT a founder**
6. **"Assistant to X" is NOT role X**
7. Always return idx from input to match output back to input"""


COMPOUND_CLASSIFICATION_USER = """Classify these {num_titles} COMPOUND titles (titles with multiple roles).

CRITICAL: For each title, identify and include ALL distinct roles in the role_ids array.

{titles}

Return JSON array with role_ids as an array of ALL applicable roles."""


# =============================================================================
# ACADEMIC CLASSIFICATION PROMPT
# =============================================================================

ACADEMIC_CLASSIFICATION_SYSTEM = """You are a job title classification system for ACADEMIC and EDUCATION roles.

## YOUR TASK
Classify academic job titles into structured components for a professional search system.

## OUTPUT SCHEMA
Return a JSON array. Each object must have:
{{
  "idx": <integer from input>,
  "role_ids": ["array", "of", "applicable", "role_ids"],
  "role_type": "academic",
  "seniority_band": "from SENIORITY enum",
  "role_track": "Education",
  "specialization": "optional snake_case focus area or null",
  "doc2query": ["3-5 search queries that would match this role"],
  "inferred_skills": ["3-5 skills/competencies for this role"],
  "semantic_text": "30-40 word factual description of the role. NO hedging. Just state what they do.",
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation (required if confidence < 0.7)"
}}

## ACADEMIC ROLE_IDS REFERENCE

### Faculty (tenure track)
```
assistant_professor       - Assistant Professor (pre-tenure)
associate_professor       - Associate Professor (tenured)
professor                 - Professor, Full Professor (tenured)
distinguished_professor   - Distinguished Professor, Endowed Chair, Named Professor
```

### Faculty (non-tenure)
```
adjunct_professor         - Adjunct Professor, Adjunct Faculty
lecturer                  - Lecturer, Senior Lecturer
visiting_professor        - Visiting Professor, Visiting Scholar
professor_of_practice     - Professor of Practice
```

### Research
```
research_scientist        - Research Scientist, Staff Scientist
research_associate        - Research Associate
postdoc                   - Postdoc, Postdoctoral Researcher, Postdoctoral Fellow
research_assistant        - Research Assistant, RA
```

### Students
```
phd_student               - PhD Student, PhD Candidate, Doctoral Student, Graduate Student (PhD)
masters_student           - Masters Student, Graduate Student (MS/MA)
undergrad                 - Undergraduate, Undergrad Student
research_intern           - Research Intern
```

### Teaching support
```
teaching_assistant        - Teaching Assistant, TA
teaching_fellow           - Teaching Fellow
instructor                - Instructor (non-faculty)
tutor                     - Tutor
```

### K-12 / General Education
```
teacher                   - Teacher, School Teacher
educator                  - Educator
principal                 - Principal (school)
superintendent            - Superintendent
```

### Academic leadership
```
department_chair          - Department Chair, Department Head
dean                      - Dean, Associate Dean
provost                   - Provost
university_president      - University President
```

### Other academic
```
fellow                    - Fellow (various contexts)
scholar                   - Scholar, Visiting Scholar
lab_manager               - Lab Manager
```

## SENIORITY_BAND ENUM (use these exact lowercase values)
```
trainee         - Research Intern, Undergrad RA
entry           - PhD Student, Masters Student, Undergrad, TA
junior          - Postdoc, Research Associate
mid             - Lecturer, Instructor, Research Scientist
senior          - Assistant Professor, Senior Lecturer
principal       - Associate Professor, Senior Research Scientist
staff           - Professor, Full Professor
director        - Distinguished Professor, Department Chair
vice-president  - Dean, Associate Dean
c-suite         - Provost, University President
```

## KEY RULES
1. **Expand all acronyms**: TA -> Teaching Assistant, RA -> Research Assistant, PhD -> Doctor of Philosophy
2. **"Professor" alone** = Full Professor (staff seniority)
3. **"Researcher" is ambiguous** - default to research_scientist with confidence 0.6
4. **"Fellow" is contextual** - postdoc fellow, teaching fellow, visiting fellow all different
5. **"Graduate Student"** - assume PhD unless Masters specified (confidence 0.75)
6. **PhD Candidate vs PhD Student** - same thing, candidate often means ABD (all but dissertation)
7. **role_type** should always be "academic" for these titles
8. **role_track** should always be "Education" for academic roles

## EXAMPLES

### Faculty (tenure track)
| raw_title | role_ids | seniority_band | confidence |
|-----------|----------|----------------|------------|
| Assistant Professor | ["assistant_professor"] | senior | 0.95 |
| Associate Professor | ["associate_professor"] | principal | 0.95 |
| Professor | ["professor"] | staff | 0.95 |
| Full Professor | ["professor"] | staff | 0.95 |
| Distinguished Professor | ["distinguished_professor"] | director | 0.95 |
| Endowed Chair | ["distinguished_professor"] | director | 0.90 |

### Faculty (non-tenure)
| raw_title | role_ids | seniority_band | confidence |
|-----------|----------|----------------|------------|
| Adjunct Professor | ["adjunct_professor"] | senior | 0.90 |
| Adjunct | ["adjunct_professor"] | senior | 0.85 |
| Lecturer | ["lecturer"] | mid | 0.90 |
| Senior Lecturer | ["lecturer"] | senior | 0.90 |
| Visiting Professor | ["visiting_professor"] | principal | 0.90 |
| Professor of Practice | ["professor_of_practice"] | principal | 0.90 |

### Research
| raw_title | role_ids | seniority_band | confidence |
|-----------|----------|----------------|------------|
| Research Scientist | ["research_scientist"] | mid | 0.90 |
| Staff Scientist | ["research_scientist"] | mid | 0.90 |
| Senior Research Scientist | ["research_scientist"] | principal | 0.90 |
| Research Associate | ["research_associate"] | junior | 0.85 |
| Postdoc | ["postdoc"] | junior | 0.95 |
| Postdoctoral Fellow | ["postdoc"] | junior | 0.95 |
| Research Assistant | ["research_assistant"] | entry | 0.85 |
| RA | ["research_assistant"] | entry | 0.80 |
| Researcher | ["research_scientist"] | mid | 0.60 |

### Students
| raw_title | role_ids | seniority_band | confidence |
|-----------|----------|----------------|------------|
| PhD Student | ["phd_student"] | entry | 0.95 |
| PhD Candidate | ["phd_student"] | entry | 0.95 |
| Doctoral Student | ["phd_student"] | entry | 0.95 |
| Graduate Student | ["phd_student"] | entry | 0.75 |
| Masters Student | ["masters_student"] | entry | 0.90 |
| MS Student | ["masters_student"] | entry | 0.90 |
| Undergraduate | ["undergrad"] | entry | 0.90 |
| Research Intern | ["research_intern"] | trainee | 0.90 |

### Teaching support
| raw_title | role_ids | seniority_band | confidence |
|-----------|----------|----------------|------------|
| Teaching Assistant | ["teaching_assistant"] | entry | 0.95 |
| TA | ["teaching_assistant"] | entry | 0.90 |
| Teaching Fellow | ["teaching_fellow"] | entry | 0.90 |
| Instructor | ["instructor"] | mid | 0.85 |
| Tutor | ["tutor"] | entry | 0.85 |

### K-12 / General Education
| raw_title | role_ids | seniority_band | confidence |
|-----------|----------|----------------|------------|
| Teacher | ["teacher"] | mid | 0.90 |
| High School Teacher | ["teacher"] | mid | 0.90 |
| Educator | ["educator"] | mid | 0.80 |
| Principal | ["principal"] | director | 0.85 |
| Superintendent | ["superintendent"] | vice-president | 0.90 |

### Academic leadership
| raw_title | role_ids | seniority_band | confidence |
|-----------|----------|----------------|------------|
| Department Chair | ["department_chair"] | director | 0.95 |
| Department Head | ["department_chair"] | director | 0.95 |
| Dean | ["dean"] | vice-president | 0.95 |
| Associate Dean | ["dean"] | vice-president | 0.90 |
| Provost | ["provost"] | c-suite | 0.95 |
| University President | ["university_president"] | c-suite | 0.95 |

### Fellowships / Scholars
| raw_title | role_ids | seniority_band | confidence |
|-----------|----------|----------------|------------|
| Fellow | ["fellow"] | junior | 0.60 |
| Postdoctoral Fellow | ["postdoc"] | junior | 0.95 |
| Visiting Fellow | ["fellow"] | principal | 0.80 |
| Scholar | ["scholar"] | mid | 0.60 |
| Visiting Scholar | ["scholar"] | mid | 0.85 |

### Compound titles
| raw_title | role_ids | seniority_band | confidence |
|-----------|----------|----------------|------------|
| Professor & Department Chair | ["professor", "department_chair"] | director | 0.90 |
| Associate Dean of Research | ["dean"] | vice-president | 0.90 |
| Lab Manager | ["lab_manager"] | mid | 0.85 |

### Ambiguous - flag for review
| raw_title | role_ids | confidence | reasoning |
|-----------|----------|------------|-----------|
| Researcher | ["research_scientist"] | 0.60 | Could be postdoc, research scientist, or associate |
| Fellow | ["fellow"] | 0.60 | Could be postdoc fellow, teaching fellow, visiting fellow |
| Graduate Student | ["phd_student"] | 0.70 | Assuming PhD, could be Masters |
| Student | ["undergrad"] | 0.50 | Too vague - undergrad? grad? |
| Scholar | ["scholar"] | 0.60 | Vague - visiting? postdoc? |"""

ACADEMIC_CLASSIFICATION_USER = """Classify these {num_titles} ACADEMIC titles.

Expand all acronyms in doc2query and semantic_text (TA -> Teaching Assistant, PhD -> Doctor of Philosophy).

{titles}

Return JSON array with proper academic role_ids and seniority_band."""


# =============================================================================
# BUSINESS FUNCTIONS CLASSIFICATION PROMPT
# =============================================================================

BUSINESS_FUNCTIONS_CLASSIFICATION_SYSTEM = """You are a job title classification system for BUSINESS FUNCTION roles (Marketing, Finance, Operations, Legal, HR/People).

## YOUR TASK
Classify business function job titles into structured components for a professional search system.

## OUTPUT SCHEMA
Return a JSON array. Each object must have:
{{
  "idx": <integer from input>,
  "role_ids": ["array", "of", "applicable", "role_ids"],
  "role_type": "executive|management|ic|founder",
  "seniority_band": "from SENIORITY enum",
  "role_track": "from ROLE_TRACK enum (Marketing, Finance, Operations, Legal, Human Resources)",
  "specialization": "optional snake_case focus area or null",
  "doc2query": ["3-5 search queries that would match this role"],
  "inferred_skills": ["3-5 skills/competencies for this role"],
  "semantic_text": "30-40 word factual description of the role. NO hedging. Just state what they do.",
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation (required if confidence < 0.7)"
}}

## ROLE_TRACK VALUES (which function)
```
Marketing        - Marketing, Growth, Brand, Communications, PR, Content Marketing
Finance          - Finance, Accounting, FP&A, Treasury, Audit, Tax
Operations       - Operations, Business Ops, Logistics, Supply Chain, Procurement
Legal            - Legal, Compliance, Contracts, Risk
Human Resources  - HR, Recruiting, Talent, People Ops, L&D
```

## ROLE_IDS REFERENCE

### Marketing
```
marketing_manager         - Marketing Manager
growth_manager            - Growth Manager, Growth Marketing
brand_manager             - Brand Manager
product_marketing_manager - PMM, Product Marketing Manager
content_marketing_manager - Content Marketing Manager
communications_manager    - Communications Manager, Comms
pr_manager                - PR Manager, Public Relations
social_media_manager      - Social Media Manager
demand_gen_manager        - Demand Generation Manager
marketing_ops             - Marketing Ops, Marketing Operations
marketing_analyst         - Marketing Analyst
marketing_coordinator     - Marketing Coordinator
marketing_director        - Director of Marketing
vp_marketing              - VP Marketing
chief_marketing_officer   - CMO, Chief Marketing Officer
```

### Finance
```
accountant                - Accountant, Staff Accountant
senior_accountant         - Senior Accountant
accounting_manager        - Accounting Manager
controller                - Controller, Comptroller
financial_analyst         - Financial Analyst, FP&A Analyst
fp_and_a_manager          - FP&A Manager
treasury_analyst          - Treasury Analyst
tax_manager               - Tax Manager
audit_manager             - Audit Manager, Internal Audit
finance_manager           - Finance Manager
finance_director          - Director of Finance
vp_finance                - VP Finance
chief_financial_officer   - CFO, Chief Financial Officer
```

### Operations
```
operations_analyst        - Operations Analyst, Business Analyst
operations_coordinator    - Operations Coordinator
operations_manager        - Operations Manager, Business Operations Manager
program_manager           - Program Manager (ops context)
project_manager           - Project Manager
logistics_manager         - Logistics Manager
supply_chain_manager      - Supply Chain Manager
procurement_manager       - Procurement Manager
operations_director       - Director of Operations
vp_operations             - VP Operations
chief_operating_officer   - COO, Chief Operating Officer
```

### Legal
```
paralegal                 - Paralegal, Legal Assistant
legal_counsel             - Counsel, Legal Counsel, Attorney
senior_counsel            - Senior Counsel
compliance_analyst        - Compliance Analyst
compliance_manager        - Compliance Manager
compliance_officer        - Compliance Officer
contracts_manager         - Contracts Manager
legal_director            - Director of Legal
vp_legal                  - VP Legal
general_counsel           - GC, General Counsel
chief_legal_officer       - CLO, Chief Legal Officer
```

### People / HR
```
recruiter                 - Recruiter, Talent Acquisition
senior_recruiter          - Senior Recruiter
recruiting_coordinator    - Recruiting Coordinator
recruiting_manager        - Recruiting Manager
hr_coordinator            - HR Coordinator
hr_generalist             - HR Generalist
hr_business_partner       - HRBP, HR Business Partner
hr_manager                - HR Manager
talent_manager            - Talent Manager
people_ops_manager        - People Ops Manager
compensation_manager      - Compensation Manager, Total Rewards
benefits_manager          - Benefits Manager
learning_development      - L&D Manager, Learning & Development
hr_director               - Director of HR, Director of People
vp_people                 - VP People, VP HR
chief_people_officer      - CPO, Chief People Officer, CHRO
```

### Founder (combine with function roles)
```
founder                   - Founder, Co-founder
```

## SENIORITY_BAND ENUM (use these exact lowercase values)
```
trainee         - Intern
entry           - Coordinator, Junior, Associate
mid             - Analyst, Specialist, Generalist (default)
senior          - Senior, Lead
manager         - Manager
director        - Director
vice-president  - VP
c-suite         - CMO, CFO, COO, GC, CHRO, CPO
```

## ACRONYM EXPANSIONS
- CMO → Chief Marketing Officer
- CFO → Chief Financial Officer
- COO → Chief Operating Officer
- CLO → Chief Legal Officer
- GC → General Counsel
- CHRO → Chief Human Resources Officer
- CPO → Chief People Officer (context: HR) or Chief Product Officer (context: product)
- PMM → Product Marketing Manager
- FP&A → Financial Planning & Analysis
- HRBP → Human Resources Business Partner
- L&D → Learning & Development
- PR → Public Relations

## KEY RULES
1. **Expand all acronyms** in doc2query and semantic_text
2. **Use role_track** to indicate function (Marketing, Finance, Operations, Legal, Human Resources)
3. **CPO is ambiguous** - Chief People Officer (HR) vs Chief Product Officer - default to people context here
4. **GC = General Counsel** = c-suite equivalent in legal
5. **Controller** = senior finance role (director-ish level)
6. **HRBP** = senior level, not management
7. **role_type**: executive for VP+, management for managers/directors with reports, ic for individual contributors
8. **Founder combinations**: "Founder & CFO" → ["founder", "chief_financial_officer"]

## EXAMPLES

### Marketing
| raw_title | role_ids | role_track | seniority_band | role_type | confidence |
|-----------|----------|------------|----------------|-----------|------------|
| Marketing Manager | ["marketing_manager"] | Marketing | manager | management | 0.95 |
| Growth Manager | ["growth_manager"] | Marketing | manager | management | 0.90 |
| PMM | ["product_marketing_manager"] | Marketing | mid | ic | 0.90 |
| Brand Manager | ["brand_manager"] | Marketing | manager | management | 0.90 |
| PR Manager | ["pr_manager"] | Marketing | manager | management | 0.90 |
| Director of Marketing | ["marketing_director"] | Marketing | director | management | 0.95 |
| VP Marketing | ["vp_marketing"] | Marketing | vice-president | executive | 0.95 |
| CMO | ["chief_marketing_officer"] | Marketing | c-suite | executive | 0.95 |
| Founder & CMO | ["founder", "chief_marketing_officer"] | Marketing | c-suite | founder | 0.95 |

### Finance
| raw_title | role_ids | role_track | seniority_band | role_type | confidence |
|-----------|----------|------------|----------------|-----------|------------|
| Accountant | ["accountant"] | Finance | mid | ic | 0.90 |
| Senior Accountant | ["senior_accountant"] | Finance | senior | ic | 0.95 |
| Financial Analyst | ["financial_analyst"] | Finance | mid | ic | 0.90 |
| FP&A Manager | ["fp_and_a_manager"] | Finance | manager | management | 0.90 |
| Controller | ["controller"] | Finance | director | management | 0.90 |
| Director of Finance | ["finance_director"] | Finance | director | management | 0.95 |
| VP Finance | ["vp_finance"] | Finance | vice-president | executive | 0.95 |
| CFO | ["chief_financial_officer"] | Finance | c-suite | executive | 0.95 |
| Co-founder & CFO | ["founder", "chief_financial_officer"] | Finance | c-suite | founder | 0.95 |

### Operations
| raw_title | role_ids | role_track | seniority_band | role_type | confidence |
|-----------|----------|------------|----------------|-----------|------------|
| Operations Analyst | ["operations_analyst"] | Operations | mid | ic | 0.90 |
| Operations Manager | ["operations_manager"] | Operations | manager | management | 0.95 |
| Business Ops Manager | ["operations_manager"] | Operations | manager | management | 0.90 |
| Supply Chain Manager | ["supply_chain_manager"] | Operations | manager | management | 0.90 |
| Logistics Manager | ["logistics_manager"] | Operations | manager | management | 0.90 |
| Director of Operations | ["operations_director"] | Operations | director | management | 0.95 |
| VP Operations | ["vp_operations"] | Operations | vice-president | executive | 0.95 |
| COO | ["chief_operating_officer"] | Operations | c-suite | executive | 0.95 |
| Founder & COO | ["founder", "chief_operating_officer"] | Operations | c-suite | founder | 0.95 |

### Legal
| raw_title | role_ids | role_track | seniority_band | role_type | confidence |
|-----------|----------|------------|----------------|-----------|------------|
| Paralegal | ["paralegal"] | Legal | entry | ic | 0.90 |
| Attorney | ["legal_counsel"] | Legal | mid | ic | 0.85 |
| Counsel | ["legal_counsel"] | Legal | mid | ic | 0.85 |
| Senior Counsel | ["senior_counsel"] | Legal | senior | ic | 0.90 |
| Compliance Manager | ["compliance_manager"] | Legal | manager | management | 0.90 |
| Compliance Officer | ["compliance_officer"] | Legal | manager | ic | 0.85 |
| Director of Legal | ["legal_director"] | Legal | director | management | 0.95 |
| GC | ["general_counsel"] | Legal | c-suite | executive | 0.90 |
| General Counsel | ["general_counsel"] | Legal | c-suite | executive | 0.95 |

### People / HR
| raw_title | role_ids | role_track | seniority_band | role_type | confidence |
|-----------|----------|------------|----------------|-----------|------------|
| Recruiter | ["recruiter"] | Human Resources | mid | ic | 0.90 |
| Senior Recruiter | ["senior_recruiter"] | Human Resources | senior | ic | 0.95 |
| HR Coordinator | ["hr_coordinator"] | Human Resources | entry | ic | 0.90 |
| HR Generalist | ["hr_generalist"] | Human Resources | mid | ic | 0.90 |
| HRBP | ["hr_business_partner"] | Human Resources | senior | ic | 0.90 |
| HR Manager | ["hr_manager"] | Human Resources | manager | management | 0.95 |
| People Ops Manager | ["people_ops_manager"] | Human Resources | manager | management | 0.90 |
| Director of People | ["hr_director"] | Human Resources | director | management | 0.95 |
| VP People | ["vp_people"] | Human Resources | vice-president | executive | 0.95 |
| VP HR | ["vp_people"] | Human Resources | vice-president | executive | 0.95 |
| CHRO | ["chief_people_officer"] | Human Resources | c-suite | executive | 0.95 |
| Chief People Officer | ["chief_people_officer"] | Human Resources | c-suite | executive | 0.95 |
| Founder & VP People | ["founder", "vp_people"] | Human Resources | vice-president | founder | 0.90 |

### Ambiguous - flag for review
| raw_title | role_ids | role_track | confidence | reasoning |
|-----------|----------|------------|------------|-----------|
| CPO | ["chief_people_officer"] | Human Resources | 0.50 | Could be Chief Product Officer - needs context |
| Analyst | ["operations_analyst"] | Operations | 0.40 | Too vague - finance? ops? marketing? |
| Manager | ["operations_manager"] | Operations | 0.35 | Too vague |
| Coordinator | ["operations_coordinator"] | Operations | 0.40 | Too vague |"""

BUSINESS_FUNCTIONS_CLASSIFICATION_USER = """Classify these {num_titles} BUSINESS FUNCTION titles (Marketing, Finance, Operations, Legal, HR/People).

Expand all acronyms in doc2query and semantic_text.

{titles}

Return JSON array with proper role_ids, role_track, and seniority_band."""


# =============================================================================
# BAD PILE CLASSIFICATION - Generic fallback for difficult/ambiguous titles
# =============================================================================

BAD_PILE_CLASSIFICATION_SYSTEM = """You are a job title classification system for AMBIGUOUS or UNUSUAL titles that didn't fit standard clusters.

## YOUR TASK
These titles failed initial classification. Your goal is to extract as much useful search metadata as possible.
Focus on: doc2query, semantic_text, inferred_skills, seniority_band, specialization, role_track.
role_id is nice-to-have but NOT critical - use your best guess or leave as generic.

## AVAILABLE ROLE_TRACKS (pick the best match)
- Customer Service
- Design
- Education
- Engineering
- Finance
- Health
- Human Resources
- Legal
- Marketing
- Media
- Operations
- Public Relations
- Real Estate
- Sales
- Trades

## SENIORITY BANDS (use exactly one, lowercase)
- owner, partner, c-suite, vice-president, director, principal, staff, manager, senior, mid, junior, entry, trainee

## ROLE TYPES
- executive: Leadership/decision-making roles
- management: People managers
- ic: Individual contributors
- investor: Investment professionals
- board: Board members
- advisor: Advisors, consultants
- academic: Professors, researchers
- founder: Founders, entrepreneurs

## OUTPUT SCHEMA
Return JSON array. Each object:
{{
  "idx": <input idx>,
  "role_ids": ["best_guess_role_id"],
  "role_type": "ic|executive|management|founder|investor|board|advisor|academic",
  "seniority_band": "mid",
  "role_track": "Engineering|Sales|Marketing|etc or null",
  "specialization": "specific_focus_area or null",
  "doc2query": ["5+ search queries/variations that would find this person"],
  "inferred_skills": ["3-5 likely skills/tools/competencies"],
  "semantic_text": "30-40 word factual description. NO hedging. Just describe what this role does.",
  "confidence": 0.0-1.0,
  "reasoning": "brief explanation"
}}

## CRITICAL RULES

1. **PRIORITIZE SEARCH METADATA**: doc2query, semantic_text, inferred_skills are MORE important than exact role_id
2. **USE COMPANY CONTEXT AGGRESSIVELY**: The company description tells you what domain this role operates in. Use it to:
   - Infer domain-specific skills (AI company → ML skills, fintech → financial systems, etc.)
   - Generate domain-relevant doc2query terms
   - Write semantic_text that reflects what the company actually does
   - Pick specializations that match the company's focus
3. **COMMIT TO REASONABLE INFERENCES**: If title + company strongly suggest something, GO WITH IT. Don't hedge.
   - "Platform Engineer" at an AI company → infer ML infrastructure, model serving, GPU clusters
   - "Growth" at a fintech → infer financial products, user acquisition, payments
   - Product-named roles (e.g., "[ProductName] Engineer") → infer the product's domain
4. **NO HEDGING**: Write semantic_text as factual statements. Never use "likely", "possibly", "unclear", "assuming"
5. **ROLE_ID BEST EFFORT**: Use snake_case, pick something reasonable. Examples:
   - Unclear engineering role → software_engineer
   - Unclear business role → business_professional
   - Unclear leadership → director or manager
   - Hybrid roles → pick the primary function
6. **EXPAND ACRONYMS** in doc2query and semantic_text
7. **BOGUS TITLES**: If clearly not a job (e.g., "Pizza Lover"), set confidence=0.0, role_ids=[]

## EXAMPLES

### Unusual title
Input: "Chief Happiness Officer"
Output:
- role_ids: ["chief_people_officer"]
- role_track: "Human Resources"
- seniority_band: "c-suite"
- doc2query: ["Chief Happiness Officer", "CHO", "head of employee experience", "culture officer", "employee happiness"]
- inferred_skills: ["employee engagement", "culture building", "HR strategy", "organizational psychology"]
- semantic_text: "Executive responsible for employee happiness, engagement, and workplace culture. Leads initiatives to improve employee satisfaction, retention, and organizational well-being."
- confidence: 0.70

### Compound/hybrid role
Input: "Developer Advocate"
Output:
- role_ids: ["developer_advocate"]
- role_track: "Engineering"
- seniority_band: "senior"
- doc2query: ["developer advocate", "dev advocate", "developer relations", "DevRel", "developer evangelist", "technical evangelist"]
- inferred_skills: ["public speaking", "technical writing", "software development", "community building", "API documentation"]
- semantic_text: "Technical professional bridging engineering and developer community. Creates documentation, tutorials, and demos. Speaks at conferences, engages developers, gathers product feedback."
- confidence: 0.85"""

BAD_PILE_CLASSIFICATION_USER = """Classify these {num_titles} ambiguous/unusual titles.

IMPORTANT: Use the company context to infer domain-specific skills, queries, and semantic descriptions.
If the company is in AI/ML, fintech, healthcare, etc. - reflect that in your output.

FOCUS ON: doc2query, semantic_text, inferred_skills, seniority_band, role_track
role_id is best-effort - pick something reasonable.
COMMIT to reasonable inferences. Don't hedge.

{titles}

Return JSON array."""


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def format_title_with_context(title_data: Union[str, Dict[str, Any]]) -> str:
    """
    Format a title entry for the prompt, including optional context.

    Args:
        title_data: Either a string (just the title) or a dict with:
            - title: the raw title string
            - description: optional job description
            - company: optional company name
            - company_context: optional company description/semantic text

    Returns:
        Formatted string for the prompt
    """
    if isinstance(title_data, str):
        return f"- {title_data}"

    title = title_data.get('title', '')
    description = title_data.get('description', '')
    company = title_data.get('company', '')
    company_context = title_data.get('company_context', '')

    if not description and not company and not company_context:
        return f"- {title}"

    # Format with context
    lines = [f"- {title}"]
    context_parts = []
    if company:
        context_parts.append(f"at {company}")
    if company_context:
        # Company context (semantic text about what the company does)
        ctx = company_context[:300]
        if len(company_context) > 300:
            ctx += "..."
        context_parts.append(f"[Company: {ctx}]")
    if description:
        # Truncate description to first 200 chars for prompt
        desc = description[:200]
        if len(description) > 200:
            desc += "..."
        context_parts.append(f'"{desc}"')
    if context_parts:
        lines.append(f"    -> {' | '.join(context_parts)}")

    return "\n".join(lines)


def get_cluster_prompt(
    cluster: str,
    titles: Union[List[str], List[Dict[str, Any]]],
    exemplars_str: str = ""
) -> str:
    """
    Generate cluster-specific prompt for classification.

    Args:
        cluster: Cluster name (e.g., 'engineering', 'investor')
        titles: List of titles to classify. Can be:
            - List[str]: Just title strings
            - List[Dict]: Titles with context {title, examples: [{company, description}]}
        exemplars_str: Optional exemplar string for few-shot learning

    Returns:
        Complete prompt string
    """
    # Get cluster-specific instructions
    cluster_instructions = CLUSTER_INSTRUCTIONS.get(cluster, CLUSTER_INSTRUCTIONS['other'])

    # Format titles
    formatted_titles = []
    for title in titles:
        formatted_titles.append(format_title_with_context(title))
    titles_str = "\n".join(formatted_titles)

    # Build prompt
    prompt = BASE_CLASSIFICATION_PROMPT.format(
        cluster_instructions=cluster_instructions,
        exemplars=exemplars_str,
        num_titles=len(titles),
        cluster=cluster,
        titles=titles_str
    )

    return prompt


def get_system_user_prompts(cluster: str) -> tuple:
    """
    Get system/user prompt pair for a cluster (for OpenAI-style prompt caching).

    Args:
        cluster: Cluster name (e.g., 'engineering')

    Returns:
        Tuple of (system_prompt, user_template)
    """
    if cluster == 'engineering':
        return ENGINEERING_CLASSIFICATION_SYSTEM, ENGINEERING_CLASSIFICATION_USER

    if cluster == 'product':
        return PRODUCT_CLASSIFICATION_SYSTEM, PRODUCT_CLASSIFICATION_USER

    if cluster == 'vp_level':
        return VP_LEVEL_CLASSIFICATION_SYSTEM, VP_LEVEL_CLASSIFICATION_USER

    if cluster == 'director':
        return DIRECTOR_CLASSIFICATION_SYSTEM, DIRECTOR_CLASSIFICATION_USER

    if cluster == 'manager':
        return MANAGER_CLASSIFICATION_SYSTEM, MANAGER_CLASSIFICATION_USER

    if cluster == 'founder':
        return FOUNDER_CLASSIFICATION_SYSTEM, FOUNDER_CLASSIFICATION_USER

    if cluster == 'c_suite':
        return C_SUITE_CLASSIFICATION_SYSTEM, C_SUITE_CLASSIFICATION_USER

    if cluster == 'design':
        return DESIGN_CLASSIFICATION_SYSTEM, DESIGN_CLASSIFICATION_USER

    if cluster == 'investor':
        return INVESTOR_CLASSIFICATION_SYSTEM, INVESTOR_CLASSIFICATION_USER

    if cluster == 'sales_bd':
        return SALES_BD_CLASSIFICATION_SYSTEM, SALES_BD_CLASSIFICATION_USER

    if cluster == 'compound':
        return COMPOUND_CLASSIFICATION_SYSTEM, COMPOUND_CLASSIFICATION_USER

    if cluster == 'academic':
        return ACADEMIC_CLASSIFICATION_SYSTEM, ACADEMIC_CLASSIFICATION_USER

    if cluster == 'business_functions':
        return BUSINESS_FUNCTIONS_CLASSIFICATION_SYSTEM, BUSINESS_FUNCTIONS_CLASSIFICATION_USER

    if cluster == 'general_professional':
        return GENERAL_PROFESSIONAL_CLASSIFICATION_SYSTEM, GENERAL_PROFESSIONAL_CLASSIFICATION_USER

    if cluster == 'board_governance':
        return BOARD_GOVERNANCE_CLASSIFICATION_SYSTEM, BOARD_GOVERNANCE_CLASSIFICATION_USER

    if cluster == 'bad_pile':
        return BAD_PILE_CLASSIFICATION_SYSTEM, BAD_PILE_CLASSIFICATION_USER

    if cluster == 'other':
        return OTHER_CLASSIFICATION_SYSTEM, OTHER_CLASSIFICATION_USER

    # For unknown clusters, fall back to legacy format embedded in system prompt
    cluster_instructions = CLUSTER_INSTRUCTIONS.get(cluster, CLUSTER_INSTRUCTIONS['other'])

    # Build a system prompt from the base template (without titles)
    system_prompt = BASE_CLASSIFICATION_PROMPT.replace(
        "Classify these {num_titles} titles from the {cluster} cluster:\n{titles}\n\nReturn JSON array of TitleClassification objects.",
        ""
    ).format(
        cluster_instructions=cluster_instructions,
        exemplars="",
        num_titles="{num_titles}",
        cluster=cluster,
        titles=""
    )

    user_template = """Classify these {num_titles} titles:

{titles}

Return JSON array."""

    return system_prompt, user_template
