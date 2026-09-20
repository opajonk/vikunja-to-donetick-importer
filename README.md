# vikunja-to-donetick-importer

Import chores from a Vikunja JSON export into DoneTick with a single Python script.

## What it does

The `vikunja_to_donetick.py` script reads a Vikunja export file and creates matching chores in DoneTick through the DoneTick API.

It supports:

- task title import
- task descriptions and comments
- due dates and recurring chores
- priority mapping
- labels
- project-to-project or project-to-label mapping
- dry runs before writing anything
- optional cleanup of migrated data

## Requirements

- Python 3.9+
- a Vikunja JSON export file
- a reachable DoneTick instance for `--apply`, `--inspect`, or `--wipe`

The script uses only the Python standard library.

## Files

- `vikunja_to_donetick.py` — importer script
- `migration_state.json` — created during imports to track already imported tasks

## Authentication

DoneTick access can be provided with:

- `--token`
- `--api-key`
- `--username` and `--password`

The following environment variables are also supported:

- `DT_USERNAME`
- `DT_PASSWORD`
- `DT_TOKEN`
- `DT_API_KEY`

## Basic usage

Inspect your DoneTick API responses first:

```bash
python3 vikunja_to_donetick.py \
  --url http://localhost:2021 \
  --username your-user \
  --password your-password \
  --inspect
```

Preview an import without creating chores:

```bash
python3 vikunja_to_donetick.py \
  --export export.json \
  --user-id 1
```

Import a single task as a test:

```bash
python3 vikunja_to_donetick.py \
  --export export.json \
  --user-id 1 \
  --url http://localhost:2021 \
  --username your-user \
  --password your-password \
  --limit 1 \
  --apply
```

Run the full import:

```bash
python3 vikunja_to_donetick.py \
  --export export.json \
  --user-id 1 \
  --url http://localhost:2021 \
  --username your-user \
  --password your-password \
  --apply
```

## Important options

- `--project-mode projects|labels|both|none` controls how Vikunja projects are represented
- `--project-parent-field` enables nested DoneTick projects when your DoneTick API exposes a parent field
- `--description-format html|markdown|text` changes how descriptions are written to DoneTick
- `--include-done` imports completed Vikunja tasks as inactive DoneTick chores
- `--include-archived` includes archived Vikunja projects
- `--no-labels` skips label creation and assignment
- `--private` sets `isPrivate=true` on every created chore
- `--set KEY=JSON` adds extra chore fields to every created payload
- `--limit N` imports only the first `N` tasks
- `--state PATH` changes the state file location

## Cleanup

The script can delete imported data from DoneTick:

```bash
python3 vikunja_to_donetick.py \
  --export export.json \
  --url http://localhost:2021 \
  --token your-jwt \
  --wipe migrated
```

Use `--apply` to perform the deletion for real. Without it, wipe mode is a dry run.

## What is not migrated

The script does not migrate:

- buckets
- views
- attachments
- reminders
- related tasks
- assignees

## License

MIT. See [LICENSE](LICENSE).
