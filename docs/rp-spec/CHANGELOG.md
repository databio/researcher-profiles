# Spec changelog

## 0.1.0 - 2026-09-25

Initial specification release. The version tracked here is independent of the
package versions (`rp-sdk`, `scholarcore`), which follow the SDK's own release
cadence. A profile's `conformsTo` names the context major version (the fixed
IRI ending in `context/v1.jsonld`), and the conformance suite gates on that
IRI; this file tracks prose revisions within that major version. Pre-1.0
versions MAY introduce incompatible changes between minor increments with no
shims and no deprecation period.

Includes `POST /identity/resolve` and the `resolve` app scope.
