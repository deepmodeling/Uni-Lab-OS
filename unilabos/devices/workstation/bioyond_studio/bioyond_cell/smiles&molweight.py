import pubchempy as pcp

cas = "21324-40-3"  # 示例
comps = pcp.get_compounds(cas, namespace="name")
if not comps:
    raise ValueError("No hit")

c = comps[0]

print("Canonical SMILES:", c.canonical_smiles)
print("Isomeric  SMILES:", c.isomeric_smiles)
print("MW:", c.molecular_weight)
