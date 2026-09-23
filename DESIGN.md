# Design

## Source of truth
- Status: Active
- Last refreshed: 2026-09-09
- Primary product surfaces: research, library/import/reading, comparison, personal context, settings/system improvements, login/bootstrap.
- Evidence reviewed: `docs/product/PRD.md`, `src/zhiheng/api/static/*`, knowledge/auth/model/memory/evolution API routes.
- User direction: find previously read research, verify conclusions against sources, answer from personal evidence, request consent before external search. This overrides the older broad "whole life" homepage emphasis.
- Observed: four HTML pages; the research page currently combines six unrelated work areas. Text imports, PDF preview, processing, knowledge detail/delete/restore/reindex, memory review and model status APIs exist. No consented web-search API is present.

## Brand
- Personality: quiet, precise, scholarly, approachable.
- Trust signals: source title and excerpt, explicit answer limitations, reversible operations, distinguish user context from evidence.
- Avoid: decorative glass, gradients, oversized headings, fake charts, fabricated demo data, raw IDs as primary labels.

## Product goals
- Goals: get from a research question to a verifiable source; browse/import material without losing a question; make gaps legible.
- Non-goals: implementing web search, research projects, PDF annotation, or persistent conversation history as part of this visual redesign.
- Success signals: complete text-import → processing → source question → citation reading; recover from errors; all existing page actions accessible on mobile and keyboard.

## Personas and jobs
- Primary persona: individual researcher maintaining a personal evidence library.
- User jobs: recall a material, examine a claim, ask a question, add sources, review personal context.
- Context: long desktop reading sessions; occasional narrow-screen review.

## Information architecture
- Primary navigation: 研究问答 / 资料库.
- Secondary navigation: 方案比较 / 个人上下文.
- Utility navigation: 设置 / 系统改进.
- `/knowledge-agent#research` (default) is the focused question/answer workspace.
- `/knowledge-agent#library` is the searchable current-material list and detail reader.
- `/knowledge-agent#decisions` is the existing comparison tool, removed from the research home.
- `/knowledge-agent#settings` groups model status, account/logout, and limitations.
- `/memory-center` preserves candidate/formal/history/trash actions and profile preview, presented as personal context.
- `/evolution-center` is the secondary system-improvement overview, preserving read-only release/proposal/trajectory details.
- `/login` handles existing sign-in and first-account setup.
- Import is a modal available from research/library; evidence opens a right-side modal reader with close/Escape and focus restoration. Hash navigation preserves in-page drafts, answer and list filters; cross-page persistence is not promised.
- Legacy hashes `#knowledge-library` and `#model-config` map to library/settings.

## Design principles
- Focus on the current task: one primary action per screen.
- Progressive disclosure: import/settings and technical details never dominate the research home.
- Evidence next to conclusions: readable citation cards, highlighted quote, source context in a separate reader.
- Uncertainty must name the gap, not bury it in a generic disclaimer.
- No fabricated capability: web search is described as not connected, never offered as a working button.
- Preserve input on errors, retain answers when viewing sources, prevent duplicate submissions.
- Tradeoff: hash screens reuse the existing authenticated static routes and preserve backend boundaries.

## Visual language
- Color: warm white canvas `#f7f8fa`, white surface, ink `#20242c`, muted `#606775`, lines `#e5e8ed`, blue accent `#315fc4`, pale blue selection; amber for evidence gaps, red for destructive states.
- Typography: system SF/PingFang sans stack, 15px base; readable answer 16px/1.9; page title 28–32px, supporting section title 18–20px. Use a restrained serif only for the research welcome headline if needed.
- Spacing/layout rhythm: 4px base; 8/12/16/24/32/48px. Sidebar 224px, main maximum 1160px, answer text maximum about 72 characters per line.
- Shape/radius/elevation: 8px inputs, 12px cards, 16px dialogs; thin dividers; subtle shadows only for elevated overlays.
- Motion: 120–180ms feedback; respect prefers-reduced-motion.
- Imagery/iconography: simple line/typographic symbols, no generated images necessary for an evidence workspace.

## Components
- Extend existing vanilla HTML/CSS/JS; preserve API clients and DOM IDs where practical.
- Shared authenticated design tokens and shell live in `knowledge-agent.css`; secondary styles import this through `/knowledge-agent.css` then scope page-specific additions.
- Shared shell classes: `workspace`, `sidebar`, `brand`, `brand-mark`, `nav-group`, `nav-label`, `nav-link`, `nav-icon`, `sidebar-footer`, `workspace-body`, `topbar`, `topbar-path`, `page-content`, `page-heading`, `eyebrow`, `panel`, `button`, `secondary`, `badge`, `muted`, `empty-state`, `toolbar`, `toast`.
- Each page embeds the same navigation. Use `aria-current=page` on active link. Sidebar horizontal compact navigation below 760px.
- Variants: primary/secondary/quiet/danger buttons; neutral/success/warning badges; loading/empty/error lists; source reader; upload form.
- Source-reader text uses textContent/text nodes and mark, never untrusted HTML.

## Accessibility
- Target: WCAG 2.2 AA intent; verify contrast, labels, focus and keyboard manually, do not claim certification.
- Visible focus ring, skip link, semantic headings, real buttons/links, labeled fields.
- Modal native dialog with Escape, close button and focus return; tab panels preserve existing semantics.
- Announce loading/errors with role=status or role=alert, avoid announcing entire long answer repeatedly.
- Text/icon accompanies color. No hover-only action, minimum 40px controls / 44px mobile targets.

## Responsive behavior
- Desktop >=1100px: persistent sidebar, generous reading space, source reader on right.
- Tablet 760–1099px: narrower content, comparison scrolls within its own container.
- Mobile <760px: sidebar becomes compact header/navigation, panels stack, dialogs fit viewport, no body horizontal scrolling.
- Touch: all actions explicit, no tooltips as the sole instruction.

## Interaction states
- Loading: button text and disabled state; list message; preserve last successful content where useful.
- Empty: specific next action (add material, submit question), never prepopulate fake results.
- Error: inline readable text and retry; do not delete entered content.
- Success: source title and real processing state; offer question or library action.
- Disabled: explain waiting for processing or unsupported capability.
- Slow/offline: finite polling; processing state remains visible after polling ends, manual refresh available.

## Content voice
- Chinese, direct, short. Use "资料", "原文", "待确认", "已生效", "处理进度".
- Do not expose route enums, Worker, hashes, or API errors as the main experience.
- Do not imply full multi-turn context: existing recent questions are current-page shortcuts, not persisted conversations.
- Do not equate missing evidence with falsehood; partial evidence still has value.

## Implementation constraints
- Vanilla static frontend served by FastAPI; no new frontend framework or dependencies.
- Preserve existing dirty workspace changes and all confirmation/privacy boundaries.
- Existing PDF preview converts to a text draft; do not claim original PDF/page annotation is retained by this flow.
- Do not transmit personal data to a new external service. Web-search permission workflow is a proposed feature only.
- Meaningful tests: existing API/UI checks plus browser navigation, import, reader, errors, narrow screens and keyboard behavior with isolated synthetic data.
- Save review screenshots outside user data, record limitations and results in `docs/product/page-redesign.md`.

## Open questions
- [ ] Web-search provider, explicit search request/consent contract, and external-result provenance: future backend work.
- [ ] Original PDF retention with page-accurate reading and annotations: verify backend capability before promising.
- [ ] Persisted research conversations and source-scoped retrieval: future data contracts; title-based query is not a strict source filter.
- [ ] Research project collections, reading progress and duplicate-material handling: prioritize after core workflow validation.
