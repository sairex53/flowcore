# FLOWCORE supply-chain and artifact signing

## Inventory

The Fedora release pipeline emits an SPDX 2.3 JSON runtime SBOM from a clean, dependency-resolved DNF installroot. It records the FLOWCORE binary RPM, its source archive, the resolved RPM package closure, package versions/architectures, package URLs, and SHA-256 checksums for the project RPM and source. Third-party RPM license strings are preserved as source metadata and are not silently translated into SPDX license expressions.

The release provenance records the Git commit, source archive digest, build environment, tool versions, build requirements, RPM/SRPM/SBOM checksums, reproducibility comparison result, and signing status. packaging/validate-sbom.py validates the SPDX structure and checks requested artifact digests without network or privileged access.

## Signing

No production signing key is stored in this repository or GitHub Actions. Official release signing is therefore BLOCKED until the release operator supplies and manages an approved key.

The operator-controlled workflow is:

1. Build unsigned source/RPM/SRPM artifacts and verify their provenance.
2. Set GNUPGHOME to the operator-owned keyring and FLOWCORE_SIGNING_FINGERPRINT to a full fingerprint.
3. Run packaging/sign-artifacts.sh --rpm-only with the RPM and SRPM paths to apply RPM signatures.
4. Regenerate provenance so it contains the signed RPM/SRPM hashes.
5. Run packaging/sign-artifacts.sh --detached-only with the source, RPM, SRPM, SBOM, and provenance paths.
6. Verify rpm --checksig and each detached signature using the trusted public key distributed out of band.

The script refuses short key IDs, missing keyrings, missing secret keys, unexpected file types in the RPM phase, and overwriting existing detached signatures. Key rotation and revocation must be published through the project's trusted release channel.

CI creates a temporary, clearly identified test key, signs copies of candidate artifacts, imports only that test key into a disposable RPM database, and verifies RPM and detached signatures. The private test key is deleted at job exit. This validates the signing procedure; it is not an official project signature.

## Trust boundaries

Checksums establish integrity only if the manifest itself is trusted. Signatures authenticate artifacts only when verified against an independently trusted key. A successful build, SBOM, RPM dependency resolution, or test-key signature does not constitute a stable release approval.

The current dependency declarations are in packaging/flowcore.spec. RPM-generated runtime requirements are inspected by the build pipeline. Optional BPF dependencies are excluded from the base Fedora RPM; eBPF capability remains runtime-detected.
