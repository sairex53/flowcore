#!/usr/bin/env python3
"""Emit an SPDX 2.3 runtime SBOM from a clean, dependency-resolved RPM installroot."""
import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
from pathlib import Path
from urllib.parse import quote


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rpm_query(*args):
    return subprocess.check_output(["rpm", *map(str, args)], text=True, stderr=subprocess.STDOUT)


def purl(name, version, release, arch):
    return (f"pkg:rpm/fedora/{quote(name, safe='-._~')}@"
            f"{quote(version + '-' + release, safe='-._~')}?arch={quote(arch, safe='-._~')}")


def package_id(name, version, release, arch):
    digest = hashlib.sha256(f"{name}\0{version}\0{release}\0{arch}".encode()).hexdigest()[:20]
    return f"SPDXRef-RPM-{digest}"


def package_row(name, version, release, arch, license_text, *, spdx_id, checksum=None,
                filename=None, root=False):
    row = {
        "name": name,
        "SPDXID": spdx_id,
        "versionInfo": f"{version}-{release}",
        "downloadLocation": "NOASSERTION",
        "filesAnalyzed": False,
        "licenseConcluded": "NOASSERTION",
        "licenseDeclared": "Apache-2.0" if root else "NOASSERTION",
        "copyrightText": "NOASSERTION",
        "externalRefs": [{
            "referenceCategory": "PACKAGE-MANAGER",
            "referenceType": "purl",
            "referenceLocator": purl(name, version, release, arch),
        }],
        "licenseComments": f"RPM metadata License field: {license_text or 'not supplied'}",
    }
    if checksum:
        row["checksums"] = [{"algorithm": "SHA256", "checksumValue": checksum}]
    if filename:
        row["packageFileName"] = filename
    return row


def generate(rpm, installroot, source):
    root_metadata = rpm_query("-qp", "--qf",
                              "%{NAME}\t%{VERSION}\t%{RELEASE}\t%{ARCH}\t%{LICENSE}", rpm).strip().split("\t")
    if len(root_metadata) != 5 or root_metadata[0] != "flowcore":
        raise ValueError("input binary RPM is not the FLOWCORE package")
    root_name, root_version, root_release, root_arch, root_license = root_metadata
    installed = rpm_query("--root", installroot, "-qa", "--qf",
                          "%{NAME}\t%{VERSION}\t%{RELEASE}\t%{ARCH}\t%{LICENSE}\n")
    rows = []
    for line in installed.splitlines():
        fields = line.split("\t", 4)
        if len(fields) != 5:
            raise ValueError("unparseable installed RPM metadata")
        name, version, release, arch, license_text = fields
        if name == "flowcore":
            if (version, release, arch) != (root_version, root_release, root_arch):
                raise ValueError("installroot FLOWCORE NEVRA differs from requested RPM")
            continue
        rows.append((name, version, release, arch, license_text))
    rows.sort(key=lambda row: row[:4])

    root_hash = sha256(rpm)
    source_hash = sha256(source)
    closure = [f"{name}-{version}-{release}.{arch}" for name, version, release, arch, _ in rows]
    namespace_seed = "\n".join([root_hash, source_hash, *closure]).encode()
    namespace_id = hashlib.sha256(namespace_seed).hexdigest()
    epoch_value = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch_value is None:
        epoch_value = rpm_query("-qp", "--qf", "%{BUILDTIME}", rpm)
    epoch = int(epoch_value)
    created = dt.datetime.fromtimestamp(epoch, dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

    root_id = "SPDXRef-Package-FLOWCORE"
    source_id = "SPDXRef-Source-FLOWCORE"
    packages = [package_row(root_name, root_version, root_release, root_arch, root_license,
                            spdx_id=root_id, checksum=root_hash, filename=rpm.name, root=True)]
    packages.extend(package_row(name, version, release, arch, license_text,
                                spdx_id=package_id(name, version, release, arch))
                    for name, version, release, arch, license_text in rows)
    packages.append({
        "name": "flowcore-source",
        "SPDXID": source_id,
        "versionInfo": root_version,
        "downloadLocation": "NOASSERTION",
        "filesAnalyzed": False,
        "licenseConcluded": "NOASSERTION",
        "licenseDeclared": "Apache-2.0",
        "copyrightText": "NOASSERTION",
        "primaryPackagePurpose": "SOURCE",
        "packageFileName": source.name,
        "checksums": [{"algorithm": "SHA256", "checksumValue": source_hash}],
    })
    relationships = [
        {"spdxElementId": "SPDXRef-DOCUMENT", "relationshipType": "DESCRIBES",
         "relatedSpdxElement": root_id},
        {"spdxElementId": root_id, "relationshipType": "GENERATED_FROM",
         "relatedSpdxElement": source_id},
    ]
    relationships.extend(
        {"spdxElementId": root_id, "relationshipType": "DEPENDS_ON",
         "relatedSpdxElement": package_id(name, version, release, arch)}
        for name, version, release, arch, _ in rows
    )
    return {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": f"FLOWCORE {root_version}-{root_release} resolved runtime",
        "documentNamespace": f"https://flowcore.dev/spdx/{namespace_id}",
        "documentDescribes": [root_id],
        "creationInfo": {
            "created": created,
            "creators": ["Tool: flowcore-generate-sbom/1.0"],
            "comment": ("Runtime package closure captured from an empty DNF installroot with weak dependencies "
                        "disabled. DEPENDS_ON edges include the resolved transitive closure."),
        },
        "packages": packages,
        "relationships": relationships,
    }


def validate(document):
    if document.get("spdxVersion") != "SPDX-2.3" or document.get("dataLicense") != "CC0-1.0":
        raise ValueError("expected SPDX 2.3 document")
    packages = document.get("packages")
    if not isinstance(packages, list) or not packages:
        raise ValueError("SPDX package inventory is empty")
    ids = [row.get("SPDXID") for row in packages]
    if len(ids) != len(set(ids)) or "SPDXRef-DOCUMENT" in ids:
        raise ValueError("duplicate or invalid SPDX identifiers")
    known = set(ids) | {"SPDXRef-DOCUMENT"}
    for row in packages:
        if not row.get("name") or not row.get("versionInfo") or not row.get("downloadLocation"):
            raise ValueError("incomplete SPDX package")
        for checksum in row.get("checksums", []):
            if checksum.get("algorithm") == "SHA256" and len(checksum.get("checksumValue", "")) != 64:
                raise ValueError("invalid SHA256 in SPDX package")
    if not any(row.get("name") == "flowcore" for row in packages):
        raise ValueError("SPDX document omits FLOWCORE")
    if not any(row.get("name") == "flowcore-source" for row in packages):
        raise ValueError("SPDX document omits its source archive")
    for relationship in document.get("relationships", []):
        if relationship.get("spdxElementId") not in known or relationship.get("relatedSpdxElement") not in known:
            raise ValueError("SPDX relationship references an unknown identifier")
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rpm", type=Path, required=True)
    parser.add_argument("--installroot", type=Path, required=True)
    parser.add_argument("--source-archive", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    document = generate(args.rpm.resolve(), args.installroot.resolve(), args.source_archive.resolve())
    validate(document)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"status": "PASS", "format": "SPDX-2.3",
                      "package_count": len(document["packages"]),
                      "dependency_closure_count": len(document["relationships"]) - 2,
                      "output": args.output.name}, sort_keys=True))


if __name__ == "__main__":
    main()
