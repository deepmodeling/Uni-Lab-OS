"""可选的外部化合物数据源（PubChem）：本地未登记 CAS 时查询候选化学信息，不写库。

配置与 V1 相同：``UNILABOS_PUBCHEM_ADDR`` / ``PUBCHEM_ADDR``（``off`` 关闭），
``UNILABOS_PUBCHEM_TIMEOUT`` / ``PUBCHEM_TIMEOUT``（如 ``30s``）。
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
    """通过 PubChem PUG REST 按 CAS 查询化合物基础信息。"""

    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        base_url = str(base_url or "").strip().rstrip("/")
        if not base_url.startswith(("http://", "https://")):
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
        endpoint = f"{self.base_url}/rest/pug/compound/name/{quote(cas, safe='')}/property/{properties}/JSON"
        request = Request(endpoint, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - 地址来自部署配置
                payload = json.loads(response.read(1 << 20).decode("utf-8"))
        except HTTPError as error:
            if error.code == 404:
                raise CompoundNotFoundError(cas) from error
            raise RuntimeError(f"PubChem returned HTTP {error.code}") from error
        except (URLError, TimeoutError, OSError, ValueError) as error:
            raise RuntimeError("PubChem request failed") from error
        rows = payload.get("PropertyTable", {}).get("Properties", [])
        if not rows:
            raise CompoundNotFoundError(cas)
        row = rows[0]
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
    """按环境变量构造数据源；未配置为默认公网地址，``off`` 或非法配置返回 ``None``。"""

    base_url = os.environ.get("UNILABOS_PUBCHEM_ADDR", os.environ.get("PUBCHEM_ADDR", "https://pubchem.ncbi.nlm.nih.gov"))
    if not str(base_url).strip() or str(base_url).strip().lower() == "off":
        return None
    raw_timeout = os.environ.get("UNILABOS_PUBCHEM_TIMEOUT", os.environ.get("PUBCHEM_TIMEOUT", "30s"))
    try:
        text = str(raw_timeout).strip().lower()
        timeout = float(text[:-1]) if text.endswith("s") else float(text)
        return PubChemCompoundSource(str(base_url), timeout)
    except (TypeError, ValueError):
        return None


__all__ = ["CompoundNotFoundError", "PubChemCompoundSource", "configured_compound_source"]
