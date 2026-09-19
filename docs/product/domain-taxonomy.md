# Domain Taxonomy

This is the target taxonomy for personal knowledge, aiming for MECE primary-domain boundaries. It classifies knowledge by the primary problem domain it helps solve, independently of record type or mastery. The domain/type separation below is a design decision; runtime changes and migration of existing records remain pending.

## Classification Rules

- The topic system must cover both academic knowledge and personal life practice. The fourteen domains below are the user-approved starting point; their MECE boundaries and coverage must be checked against actual entries.
- Preserve complete source materials. Extract independently understandable conclusions as separate knowledge entries, each with its own premises, primary domain and original-source reference.
- Each extracted knowledge entry has exactly one primary domain. A source spanning multiple domains may yield entries with different primary domains.
- Tags, entities and relations express cross-domain connections.
- Personal Archive and Experience belongs to a separate record-type dimension, not the primary-domain dimension. Personal records still receive a primary domain based on their subject.
- Mastery level, freshness, source quality and confidence are attributes, not domains.
- When a note spans multiple domains, choose the domain that would own the main future question.
- Taxonomy changes are versioned and cannot automatically rewrite formal knowledge at scale.
- For new entries, propose the primary domain and cross-domain associations alongside the conclusion for user review and approval. Changes to an existing entry's classification require user confirmation.
- Adding, merging or splitting domains requires a proposal explaining the reason and affected entries, user approval, and preservation of historical assignments.

## Primary Domains — Starting Point

| 主领域 | 主要内容 |
|---|---|
| 数学与形式科学 | 数学、逻辑、概率、统计基础 |
| 自然科学 | 物理、化学、生物、地球与宇宙科学 |
| 计算机与工程技术 | 计算机、AI、软件、电子及其他工程 |
| 医学与健康 | 医学、营养、运动、睡眠、疾病与心理健康 |
| 心理与认知 | 感知、情绪、动机、认知及行为机制 |
| 社会、政治与法律 | 社会结构、公共政策、政治、法律与制度 |
| 经济、金融与商业 | 经济学、投资、个人财务、经营与商业管理 |
| 历史、哲学与宗教 | 历史解释、哲学思想、伦理与宗教 |
| 语言、文学与艺术 | 语言研究、文学、音乐、视觉艺术与创作 |
| 教育与学习 | 教育方法、学习策略、知识管理与技能习得方法 |
| 职业与工作实践 | 职业选择、求职、工作协作与个人工作方法 |
| 人际关系与沟通 | 亲密关系、家庭、社交、沟通与冲突处理 |
| 生活方式与日常事务 | 居家、穿搭、饮食制作、出行与日常生活安排 |
| 体育、游戏与休闲 | 运动项目、竞技、游戏规则与休闲活动 |

These are topic labels, not new database IDs. Runtime identifiers and old-to-new mappings will be specified during implementation planning.

## Record Type

Personal Archive and Experience describes the nature of a record independently of its primary domain. Events, decisions and reflections are examples of personal records; the complete type vocabulary and its cardinality remain to be agreed.

For example, a personal investment reflection belongs to 经济、金融与商业 and is also a personal-experience record. A career decision belongs to 职业与工作实践 and is also a personal record. Cross-domain associations remain available in both cases.

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
| Paper about retrieval evaluation for an Agent project | 计算机与工程技术 | Main question concerns engineering evaluation |
| Notes about choosing a job after graduation | 职业与工作实践 | Main question concerns career choice |
| Article about market index investing basics | 经济、金融与商业 | Main question concerns investing |
| Reflection on a difficult conversation | 人际关系与沟通 | Main question concerns interpersonal communication |
| Personal record of an investment decision and its outcome | 经济、金融与商业 | Finance is the subject; personal experience is the record type |
| Depression treatment | 医学与健康 | Main question concerns treatment |
| Mechanisms of emotion formation | 心理与认知 | Main question concerns psychological mechanisms |
| Basketball tactics | 体育、游戏与休闲 | Main question concerns sporting strategy |
| Rehabilitation of a sports injury | 医学与健康 | Main question concerns recovery from injury |

## Versioning

Taxonomy versions are stored in SQLite and referenced by knowledge rows. A taxonomy proposal can suggest mappings, but only approved migrations can change formal primary domains. Old domain IDs remain resolvable for audit.

The legacy primary-domain ID `personal_archive_experience` remains resolvable for existing records until an approved migration assigns their subject domains and records their types. This documentation change does not migrate stored data. See [the domain/type separation decision](../adr/0003-domain-and-record-type.md).
