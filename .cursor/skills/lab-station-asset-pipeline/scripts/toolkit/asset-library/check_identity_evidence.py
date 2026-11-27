"""Check scoped identity mappings and field-level evidence; never infer product identity."""
from p0_common import checked, cli, digest, finite, read, require, resolve, result, scoped, unique


def pointer(document, path):
    require(path == "" or path.startswith("/"), "Invalid JSON pointer")
    for key in path.split("/")[1:]:
        key = key.replace("~1", "/").replace("~0", "~")
        document = document[int(key)] if isinstance(document, list) else document[key]
    return document


def check(document, base):
    require(document["schema"] == "asset-identity-evidence/1", "Unsupported identity contract")
    entities = unique(document["entities"])
    sources = unique(document["sources"])
    claims = document["claims"]
    scoped(document, claims)
    for row in entities.values():
        require(row.get("system") and row.get("key"), "Entity needs source system and original key")
    checks = []
    for ident, source in sources.items():
        def validate_source(source=source):
            require(source["kind"] in {"manufacturer", "procurement", "database", "cad", "photo",
                    "measurement", "user_confirmation", "derived", "catalog"}, "Unknown source kind")
            require(source.get("path"), "Offline source snapshot required; URL alone is not verified evidence")
            path = resolve(base, source["path"])
            require(digest(path) == source["sha256"], "Source snapshot digest differs")
            return {"sha256": source["sha256"]}
        checks.append(checked("source:" + ident, validate_source))
    valid_sources = {r["id"][7:] for r in checks if r["status"] == "passed"}
    bindings = set()
    for claim in claims:
        def validate_claim(c=claim):
            require(c["subject"] in entities, "Unknown subject")
            require(c["status"] in {"confirmed", "candidate", "unknown", "not_applicable"}, "Invalid claim status")
            relation = c.get("relation")
            if relation:
                require(relation in {"same_product", "selected_for", "geometry_reference", "compatible_with"}, "Unknown relation")
                require(c["target"] in entities and c["target"] != c["subject"], "Invalid target")
                key = (c["subject"], relation, c["target"])
            else:
                require(c.get("field"), "Field required")
                key = (c["subject"], c["field"])
                require("value" in c, "Value must be explicit, including null for unknown")
                if c.get("numeric") and c["value"] is not None:
                    require(finite(c["value"]) and c.get("unit"), "Numeric field requires finite value and unit")
            require(key not in bindings, "Duplicate or conflicting claim; resolve explicitly")
            bindings.add(key)
            evidence = c.get("evidence", [])
            for ev in evidence:
                require(ev["source"] in valid_sources and ev.get("locator"), "Missing/invalid source or locator")
                require(ev["scope"] in {"identity", "specification", "geometry", "selection", "compatibility", "record"}, "Evidence scope required")
                if "pointer" in ev:
                    value = pointer(read(resolve(base, sources[ev["source"]]["path"])), ev["pointer"])
                    require(value == ev["equals"], "Evidence pointer does not contain stated value")
            if c["status"] != "confirmed":
                require(c.get("reason"), "Unconfirmed/not-applicable claim needs reason")
                return {"status": "passed" if c["status"] == "not_applicable" else "incomplete", "claim_status": c["status"], "reason": c["reason"]}
            require(evidence, "Confirmed claim has no evidence")
            if relation == "same_product":
                require(any(e["scope"] == "identity" and sources[e["source"]]["kind"] in
                            {"manufacturer", "procurement", "database", "user_confirmation"} for e in evidence),
                        "Geometry/color/selection cannot establish same-product identity")
            elif relation == "selected_for":
                require(any(e["scope"] == "selection" for e in evidence), "Selection evidence required")
            elif relation:
                require(any(e["scope"] == ("geometry" if relation == "geometry_reference" else "compatibility") for e in evidence), "Relation evidence scope differs")
            else:
                require(c["value"] is not None, "Confirmed value is null")
                if c.get("numeric") and any(sources[e["source"]]["kind"] == "photo" for e in evidence):
                    m = c.get("measurement", {})
                    require(all(m.get(k) for k in ("method", "reference", "plane")), "Photo measurement lacks calibration/plane")
                    require(finite(m.get("uncertainty")) and m["uncertainty"] > 0, "Photo estimate needs nonzero uncertainty in field units")
            return {"claim_status": "confirmed", "evidence_count": len(evidence)}
        checks.append(checked(claim["id"], validate_claim))
    return result(checks, scope={"claims": len(claims), "entities": len(entities)},
                  limitation="Validates evidence integrity and declaration consistency, not whether a document/image proves a claim; semantic review remains required")


if __name__ == "__main__":
    raise SystemExit(cli(__doc__, check))
