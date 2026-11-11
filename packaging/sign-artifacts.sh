#!/usr/bin/env bash
set -euo pipefail

action="${1:-}"
shift || true
fingerprint="${FLOWCORE_SIGNING_FINGERPRINT:-}"
if [[ ! "$fingerprint" =~ ^([A-Fa-f0-9]{40}|[A-Fa-f0-9]{64})$ ]]; then
    printf '%s\n' 'Set FLOWCORE_SIGNING_FINGERPRINT to an explicit full signing-key fingerprint.' >&2
    exit 2
fi
fingerprint="${fingerprint^^}"
if [[ -z "${GNUPGHOME:-}" || ! -d "$GNUPGHOME" ]]; then
    printf '%s\n' 'GNUPGHOME must identify the operator-controlled keyring.' >&2
    exit 2
fi
if ! gpg --batch --with-colons --list-secret-keys "$fingerprint" |
        awk -F: -v wanted="$fingerprint" '$1 == "fpr" && toupper($10) == wanted { found=1 } END { exit !found }'; then
    printf '%s\n' 'The selected full fingerprint is not present in this keyring.' >&2
    exit 2
fi
if (($# == 0)); then
    printf '%s\n' 'Provide at least one artifact path.' >&2
    exit 2
fi

case "$action" in
    --rpm-only)
        command -v rpmsign >/dev/null || { printf '%s\n' 'rpmsign is required.' >&2; exit 2; }
        for artifact in "$@"; do
            [[ -f "$artifact" && "$artifact" == *.rpm ]] || {
                printf '%s\n' 'The RPM-signing phase accepts only existing .rpm files.' >&2
                exit 2
            }
            rpmsign --define "_gpg_name $fingerprint" --addsign "$artifact"
        done
        ;;
    --detached-only)
        for artifact in "$@"; do
            [[ -f "$artifact" ]] || { printf '%s\n' "Missing artifact: $artifact" >&2; exit 2; }
            [[ ! -e "$artifact.asc" ]] || {
                printf '%s\n' "Refusing to overwrite an existing detached signature." >&2
                exit 2
            }
            gpg --batch --yes --local-user "$fingerprint" --armor --detach-sign \
                --output "$artifact.asc" "$artifact"
        done
        ;;
    *)
        printf '%s\n' 'Use --rpm-only first, regenerate provenance, then use --detached-only.' >&2
        exit 2
        ;;
esac
