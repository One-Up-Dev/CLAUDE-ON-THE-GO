# Memory Management Rules

## Auto Memory (MEMORY.md)
- Keep MEMORY.md under 200 lines — it's truncated beyond that
- Store detailed notes in separate topic files (e.g., `debugging.md`, `patterns.md`)
- Link topic files from MEMORY.md with relative paths
- Organize by topic, not chronologically

## What to Save
- Stable patterns confirmed across multiple interactions
- Key architectural decisions and important file paths
- User preferences for workflow, tools, and communication
- Solutions to recurring problems and debugging insights
- Explicit user requests ("always do X", "never do Y")

## What NOT to Save
- Session-specific context (current task, in-progress work)
- Incomplete or speculative information
- Anything that duplicates CLAUDE.md instructions
- Generic best practices not specific to the user/project

## Conversation History DB
- Database is at `./database.db` (project-local)
- Query: `SELECT role, content, metadata, source FROM messages ORDER BY id DESC LIMIT 20`
