# Changelog

## 0.2.1

First public distribution: Apache-2.0 source, independently installable package, runnable quick start, supported-platform CI and package inventory/import verification. Existing API behavior is preserved from the locally consumed candidate.

On Linux, a replaced remembered directory pathname now fails with
`PATH_FORBIDDEN` instead of returning its relocated `/proc` path, matching the
documented binding contract and macOS behavior.
