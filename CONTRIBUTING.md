# Contributing

Thanks for your interest in Powerpacks.

## Development setup

1. Install Python dependencies:

   ```bash
   bin/setup-python
   ```

2. For the local console, install app dependencies:

   ```bash
   cd app
   npm install
   ```

3. Run targeted tests for the area you changed before opening a PR.

## Pull requests

- Keep changes focused and explain the intent in the PR description.
- Include exact test commands and results.
- Do not commit local credentials, `.env` files, generated `.powerpacks/` data,
  or private customer/operator artifacts.
- Update docs or skill files when behavior changes.

## Reporting issues

Use the GitHub issue templates when possible and include reproduction steps,
expected behavior, actual behavior, and relevant environment details.
