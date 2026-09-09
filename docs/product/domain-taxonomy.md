# Domain Taxonomy

This is the MVP MECE taxonomy for personal knowledge. It classifies knowledge by the primary problem domain it helps solve, not by whether the user has mastered it.

## Classification Rules

- Each knowledge object has exactly one primary domain.
- Tags, entities and relations express cross-domain connections.
- Mastery level, freshness, source quality and confidence are attributes, not domains.
- When a note spans multiple domains, choose the domain that would own the main future question.
- Taxonomy changes are versioned and cannot automatically rewrite formal knowledge at scale.

## Primary Domains

| ID | Domain | Owns | Does not own |
|---|---|---|---|
| `academic_career` | Academic and Career | research planning, papers as career/research assets, applications, professional positioning | implementation details better owned by technical engineering |
| `technology_engineering` | Technology and Engineering | programming, systems, AI engineering, software architecture, tools, reproducible experiments | career strategy or academic administration |
| `finance_assets` | Finance and Assets | financial literacy, investing concepts, budgeting, risk frameworks, personal asset decisions | automatic trading or external transactions |
| `society_public_issues` | Society and Public Issues | news, policy, institutions, public debates, social phenomena | private relationship advice unless the object is mainly about interpersonal behavior |
| `learning_personal_development` | Learning and Personal Development | learning plans, metacognition, habits, productivity, language and skill acquisition | domain-specific factual knowledge when another domain is primary |
| `health_wellbeing` | Health and Wellbeing | physical health, mental wellbeing, sleep, exercise, medical literacy | diagnosis or treatment execution |
| `relationships_communication` | Relationships and Communication | daily conversation, emotional communication, conflict handling, social etiquette | public society analysis or professional writing |
| `lifestyle_aesthetics` | Lifestyle and Aesthetics | clothing, grooming, photography taste, home/life choices, aesthetic references | art-making process when creation is the main goal |
| `arts_creation` | Arts and Creation | writing, photography projects, visual creation, music/film/art study as creative practice | general aesthetic preference without a creation task |
| `personal_archive_experience` | Personal Archive and Experience | personal decisions, reflections, autobiographical events, project history, outcomes | external facts that merely appeared during an experience |

## Suggested Secondary Facets

Secondary facets are optional tags or structured fields:

- `topic`: concrete subject, such as retrieval, wardrobe, portfolio, macroeconomics.
- `format`: paper, web article, book note, conversation, image, decision record.
- `status`: to_read, processing, active, archived, deprecated.
- `mastery`: unfamiliar, learning, usable, fluent, teaching.
- `source_quality`: primary, secondary, opinion, uncertain.
- `time_sensitivity`: stable, slowly_changing, current, expired.
- `sensitivity`: public, private, sensitive, highly_sensitive.

## Ambiguity Examples

| Object | Primary domain | Reason |
|---|---|---|
| Paper about retrieval evaluation for an Agent project | `technology_engineering` | Main future use is implementation and evaluation |
| Notes about choosing a PhD graduation job direction | `academic_career` | Main future use is career decision support |
| Article about market index investing basics | `finance_assets` | Main future use is financial literacy |
| Reflection on a difficult conversation | `relationships_communication` | Main future use is communication behavior |
| Personal record of a decision and its later outcome | `personal_archive_experience` | Main value is autobiographical evidence and feedback |

## Versioning

Taxonomy versions are stored in SQLite and referenced by knowledge rows. A taxonomy proposal can suggest mappings, but only approved migrations can change formal primary domains. Old domain IDs remain resolvable for audit.
