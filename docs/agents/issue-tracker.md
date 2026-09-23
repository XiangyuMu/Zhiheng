# Issue tracker: GitHub

Issues and specs live in XiangyuMu/Zhiheng on GitHub.
Use the gh CLI from this repository, or pass --repo XiangyuMu/Zhiheng.

- Publish a spec or ticket: gh issue create.
- Read a ticket and discussion: gh issue view <number> --comments.
- List work: gh issue list with appropriate state and label filters.
- Comment: gh issue comment <number>.
- Apply or remove labels: gh issue edit <number> --add-label or --remove-label.
- Close completed work: gh issue close <number>.
- For multiline issue bodies and comments, write the text to a temporary
  file and pass --body-file.
- Use docs/agents/triage-labels.md for triage label names.

## Pull requests as a triage surface

PRs as a request surface: no.
