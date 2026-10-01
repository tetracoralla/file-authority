# Releasing

The source is Apache-2.0. Preserve LICENSE and NOTICE in all artifacts. Public source, GitHub release assets and the PyPI package are separate distribution surfaces. A source tag alone does not establish registry publication.

1. Review a clean candidate, increment the package version and update CHANGELOG. Run the checks in CONTRIBUTING.md. The package check installs the emitted artifact outside this checkout and exercises its public API.
2. Push the reviewed source; require the supported-platform CI and CodeQL checks to pass. Create an annotated `v<version>` tag on that exact commit. GitHub release assets contain the built wheel and sdist and SHA-256 checksums.
3. Publish through the manual `release.yml` workflow with the exact version and main commit. Configure the PyPI pending trusted publisher for openadam-file-authority, owner tetracoralla, repository file-authority, workflow release.yml, environment pypi. Do not store long-lived registry tokens in the repository.
4. Reacquire the registry artifact into a fresh consumer. Verify metadata, public imports and wheel licenses and exports, then migrate consumers with exact versions and updated lockfiles. Offline distributions may vendor the verified release artifact.

Only invoke publication with owner authority. Do not replace a published version with different bytes. Correct a bad release with a new version; preserve its diagnostics and notify affected consumers through their normal maintenance channel.
