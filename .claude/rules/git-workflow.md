# Git Workflow Rules

## Commits
- Use conventional commits: `type(scope): description`
- Types: feat, fix, refactor, docs, test, chore, style, perf
- Keep commit messages concise (under 72 chars for title)
- Focus on "why" not "what" in commit descriptions

## Safety
- Never force push to main/master
- Never use --no-verify unless explicitly requested
- Always create NEW commits rather than amending unless explicitly asked
- Stage specific files, not `git add -A` or `git add .`

## Branching
- Use descriptive branch names: `feat/feature-name`, `fix/bug-description`
- Keep branches focused on a single task
