"""可选的外部化合物数据源。

OS 本地模式仍以 ``inventory.db`` 为权威；本模块只负责在本地未登记时，
向 PubChem 查询候选化学信息，不写入本地数据库。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class CompoundNotFoundError(LookupError):
    """外部数据源没有收录该 CAS。"""


class PubChemCompoundSource:
    """通过 PubChem PUG REST 查询化合物基础信息。"""

    def __init__(self, base_url: str, timeout: float = 30.0):
        base_url = str(base_url or "").strip().rstrip("/")
        if not (base_url.startswith("http://") or base_url.startswith("https://")):
            raise ValueError(f"invalid PubChem base URL: {base_url!r}")
        if timeout <= 0:
            raise ValueError("PubChem timeout must be positive")
        self.base_url = base_url
        self.timeout = timeout

    def lookup_by_cas(self, cas: str) -> Dict[str, Any]:
        properties = (
            "Title,MolecularFormula,IUPACName,IsomericSMILES,"
            "CanonicalSMILES,ConnectivitySMILES,SMILES,InChIKey,MolecularWeight"
        )
        endpoint = (
            f"{self.base_url}/rest/pug/compound/name/{quote(cas, safe='')}"
            f"/property/{properties}/JSON"
        )
        request = Request(endpoint, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                payload = json.loads(response.read(1 << 20).decode("utf-8"))
        except HTTPError as error:
            if error.code == 404:
                raise CompoundNotFoundError(cas) from error
            raise RuntimeError(f"PubChem returned HTTP {error.code}") from error
        except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("PubChem request failed") from error

        properties_rows = payload.get("PropertyTable", {}).get("Properties", [])
        if not properties_rows:
            raise CompoundNotFoundError(cas)
        row = properties_rows[0]
        smiles = next(
            (row.get(key) for key in ("IsomericSMILES", "CanonicalSMILES", "ConnectivitySMILES", "SMILES") if row.get(key)),
            None,
        )
        weight = row.get("MolecularWeight")
        try:
            weight = float(weight) if weight not in (None, "") else None
        except (TypeError, ValueError):
            weight = None
        return {
            "name": row.get("Title") or row.get("IUPACName") or cas,
            "molecular_formula": row.get("MolecularFormula"),
            "smiles": smiles,
            "inchi_key": row.get("InChIKey"),
            "molecular_weight": weight,
        }


def configured_compound_source() -> Optional[PubChemCompoundSource]:
    """读取环境变量，返回已配置的数据源；未配置或非法时返回 ``None``。"""

    base_url = os.environ.get("PUBCHEM_ADDR")
    if base_url is None:
        base_url = os.environ.get(
            "UNILABOS_PUBCHEM_ADDR", "https://pubchem.ncbi.nlm.nih.gov"
        )
    if not str(base_url).strip() or str(base_url).strip().lower() == "off":
        return None
    raw_timeout = os.environ.get(
        "PUBCHEM_TIMEOUT", os.environ.get("UNILABOS_PUBCHEM_TIMEOUT", "30s")
    )
    try:
        timeout_text = str(raw_timeout).strip().lower()
        timeout = float(timeout_text[:-1]) if timeout_text.endswith("s") else float(timeout_text)
        return PubChemCompoundSource(str(base_url), timeout)
    except (TypeError, ValueError):
        return None


__all__ = ["CompoundNotFoundError", "PubChemCompoundSource", "configured_compound_source"]
