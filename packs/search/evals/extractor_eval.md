# Extractor Eval

> **Dated evaluation snapshot.** These results describe the recorded extractor
> dataset run, not current production accuracy. Rerun the eval before using the
> numbers for calibration decisions.

Dataset: `/path/to/network-search-api/tests/evals/query_expansion/datasets`

| Extractor | Cases | Accuracy | Key Fields | Time |
|---|---:|---:|---|---:|
| education | 37 | 97% | schools=97%, degree_levels=100%, fields_of_study=100% | 41352ms |
| company | 35 | 100% | company_names=100%, company_semantic_queries=91%, investors=100%, entity_types=94%, sector_types=92% | 87788ms |
| location | 70 | 97% | cities=97%, states=100%, metro_areas=100%, countries=100%, macro_regions=96%, company_cities=97%, company_states=100%, company_metro_areas=100%, company_countries=100%, company_macro_regions=99% | 137589ms |
| seniority | 40 | 100% | seniority_bands=100% | 28637ms |
| role | 45 | 4% | expected_bm25_queries=20% | 108080ms |

### education failures (1/37)

- **who attended stanford**
  - schools: expected=[], got=['stanford university']

### location failures (2/70)

- **Intros to senior engineers at Andreessen Horowitz backed startups in NYC**
  - cities: expected=['new york city'], got=[]
  - company_cities: expected=[], got=['new york city']
- **product managers at B2C unicorns in NYC**
  - cities: expected=['new york city'], got=[]
  - company_cities: expected=[], got=['new york city']

### role failures (43/45)

- **machine learning engineers**
  - expected_bm25_queries: expected=['machine learning engineer', 'ml engineer', 'senior machine learning engineer', 'staff ml engineer'], got=['machine learning developer', 'machine learning engineer', 'machine learning specialist', 'ml engineer']
- **software engineers**
  - expected_bm25_queries: expected=['senior software engineer', 'software developer', 'software engineer', 'staff software engineer'], got=['application developer', 'software dev', 'software developer', 'software engineer', 'software engineering', 'swe']
- **product managers**
  - expected_bm25_queries: expected=['director of product', 'group product manager', 'product manager', 'senior product manager'], got=['pm', 'product management', 'product manager']
- **data scientists**
  - expected_bm25_queries: expected=['data scientist', 'lead data scientist', 'senior data scientist', 'staff data scientist'], got=['data analyst scientist', 'data science specialist', 'data scientist', 'machine learning scientist']
- **backend engineers at startups**
  - expected_bm25_queries: expected=['backend developer', 'backend engineer', 'senior backend engineer', 'server engineer'], got=['api engineer', 'back-end developer', 'back-end engineer', 'backend developer', 'backend engineer', 'server-side engineer']
- **CTOs at fintech companies**
  - expected_bm25_queries: expected=['chief technology officer', 'cto', 'vp engineering'], got=['chief technology officer', 'cto', 'head of technology', 'technology lead']
- **sales leaders**
  - expected_bm25_queries: expected=['director of sales', 'head of sales', 'sales director', 'vp sales'], got=['chief sales officer', 'head of sales', 'sales director', 'sales executive', 'sales leader', 'sales manager', 'vp of sales']
- **founders**
  - expected_bm25_queries: expected=['ceo', 'co-founder', 'co-founder & ceo', 'founder'], got=['co-founder', 'cofounder', 'founder', 'founding ceo']
- **UX designers**
  - expected_bm25_queries: expected=['product designer', 'senior ux designer', 'ui/ux designer', 'ux designer'], got=['interaction designer', 'user experience designer', 'ux designer', 'ux/ui designer']
- **DevOps engineers**
  - expected_bm25_queries: expected=['devops engineer', 'senior devops engineer', 'site reliability engineer', 'sre'], got=['devops', 'devops engineer', 'devops specialist', 'infrastructure engineer', 'platform engineer', 'site reliability engineer', 'sre']
