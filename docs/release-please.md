# Release Please

Powerpacks uses release-please to create release PRs, changelog updates, tags,
and GitHub releases from commits on `main`.

## Trigger

The workflow runs on pushes to `main`:

```text
.github/workflows/release-please.yml
```

Release-please scans commits after the current release marker. For the first
configured run, that marker is `bootstrap-sha` in `release-please-config.json`.
Later runs use the last merged release PR / release tag.

It opens or updates a release PR only when it finds releasable Conventional
Commit messages.

## Releasable Commit Shapes

- `fix: ...` - patch release
- `feat: ...` - minor release
- `feat!: ...` - major release
- `fix!: ...` - major release
- `refactor!: ...` - major release
- `<type>: ...` with a `BREAKING CHANGE:` footer - major release
- `deps: ...` - dependency release unit
- `docs: ...` - release-please treats `docs` as releasable for Python-style
  packages

Usually non-releasable commit shapes:

- `chore: ...`
- `ci: ...`
- `build: ...`
- `test: ...`
- `refactor: ...` unless it is marked breaking
- `style: ...`

## Package Mapping

`release-please-config.json` defines a single package: `.` as the Python
package `powerpacks`, tagged like `powerpacks-vX.Y.Z`.

## Forcing A Version

To request a specific version, include a `Release-As` footer in a commit body:

```bash
git commit --allow-empty \
  -m "chore: release 0.2.0" \
  -m "Release-As: 0.2.0"
```

Release-please should then propose that version in the release PR.

## Release Flow

1. Land one or more releasable commits on `main`.
2. Wait for the release-please workflow to open or update a release PR.
3. Review the generated changelog/version changes.
4. Merge the release PR.
5. Release-please creates the tag and GitHub release.
