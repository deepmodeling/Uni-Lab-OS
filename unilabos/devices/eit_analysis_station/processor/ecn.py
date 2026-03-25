"""
gcpy: python utilities for chromatographs
Copyright (C) 2021 David Ollodart <LICENSE>

功能:
    根据 SMILES 字符串分类碳原子的官能团, 并计算有效碳数(ECN).
    ECN 用于 GC-FID 定量分析中的响应因子校正.
参数:
    无.
返回:
    无.
"""

import networkx as nx
from pysmiles.read_smiles import read_smiles
from collections import defaultdict

ary_dct = {0: '', 1: 'primary', 2: 'secondary', 3: 'tertiary', 4: 'quaternary'}
alk_dct = {2: 'alkyne', 3: 'alkene', 4: 'alkane'}


def smiles2carbontypes(smiles):
    """
    功能:
        将 SMILES 字符串转换为碳原子分类结果.
        逐个检查碳原子及其最近/次近邻居, 判断所属官能团和芳香性.
    参数:
        smiles: str, 化合物 SMILES 字符串.
    返回:
        Generator[Tuple[str, str]], 每个碳原子的 (芳香性分类, 官能团类型).
    """
    graph = read_smiles(smiles, explicit_hydrogen=True)

    cycles_list = nx.algorithms.cycle_basis(graph)
    cyc_nodes = []
    for cyc in cycles_list:
        # 非芳香性环原子不影响 ECN
        if len(cyc) % 2 == 0 and len(cyc) >= 4:
            for n in cyc:
                if n not in cyc_nodes:
                    cyc_nodes.append(n)

    global dldct
    dldct = graph.nodes(data='element')

    for node0 in graph.nodes:

        if dldct[node0] != 'C':
            continue
            # ECN 仅需统计碳原子

        nbors1 = ''
        nbors2 = ''
        edge01 = []
        edge12 = []
        for node1 in graph.neighbors(node0):
            nbors1 += dldct[node1]
            edge01.append(node1)
            tmp = []
            for node2 in graph.neighbors(node1):
                nbors2 += dldct[node2]
                if node2 != node0:
                    tmp.append(node2)
            edge12.append(tmp)

        nbors1 = ''.join(sorted(nbors1))
        nbors2 = ''.join(sorted(nbors2))

        is_cycle = node0 in cyc_nodes

        yield classify(nbors1, nbors2, edge01, edge12, is_cycle)


def classify(nbors1, nbors2, edge01, edge12, is_cycle):
    """
    功能:
        根据碳原子的邻居信息判断其官能团类型.
    参数:
        nbors1: str, 最近邻原子元素(排序后).
        nbors2: str, 次近邻原子元素(排序后).
        edge01: list, 最近邻节点列表.
        edge12: list, 次近邻节点列表(按最近邻分组).
        is_cycle: bool, 该碳原子是否在环上.
    返回:
        Tuple[str, str], (芳香性分类, 官能团类型).
    """
    ary = nbors1.count('C')
    hyd = nbors1.count('H')

    ln1 = len(nbors1)
    if ln1 == ary + hyd:
        if ln1 == 3 and is_cycle:
            return ary_dct[ary], 'aromatic'
        return ary_dct[ary], alk_dct[ln1]

    if nbors1 == 'CCO':
        return '', 'ketone'
    if nbors1 == 'CHO':
        return '', 'aldehyde'

    if nbors1 == 'CHHO' or nbors1 == 'CCHO':
        for i in range(len(edge01)):
            if dldct[edge01[i]] == 'O':
                n = edge12[i]
                if len(n) == 1 and dldct[n[0]] == 'H':
                    return ary_dct[ary], 'alcohol'
        return '', 'ether'

    if nbors1 == 'COO':
        for i in range(len(edge01)):
            if dldct[edge01[i]] == 'O':
                n = edge12[i]
                if len(n) == 1 and dldct[n[0]] == 'H':
                    return '', 'carboxylic acid'
        return '', 'acid anhydride or ester'

    if 'N' in nbors1:
        if nbors1 == 'CNO':
            return '', 'amide'
        elif nbors1 == 'CN':
            return '', 'nitrile'
        else:
            for i in range(len(edge01)):
                if dldct[edge01[i]] == 'N':
                    n = edge12[i]
                    amine_ary = 1
                    for j in n:
                        if dldct[j] == 'C':
                            return '', 'amine, or carbon adjacent to amide'
                        elif dldct[j] == 'C':
                            amine_ary += 1
                    return ary_dct[amine_ary], 'amine'

    if 'S' in nbors1:
        for i in range(len(edge01)):
            if dldct[edge01[i]] == 'S':
                n = edge12[i]
                if len(n) == 1 and dldct[n[0]] == 'H':
                    return '', 'thiol'
                return '', 'sulfide'

    if 'F' in nbors1 or 'Cl' in nbors1 or 'Br' in nbors1 or 'I' in nbors1:
        return ary_dct[ary], 'halide'

    return '', 'unknown'


# 官能团 → ECN 大类映射
class_dct = {'alkane': 'aliphatic',
             'alkene': 'olefinic',
             'alkyne': 'acetylinic',
             'carboxylic acid': 'carboxyl',
             'aldehyde': 'carbonyl',
             'ketone': 'carbonyl',
             'ester': 'carboxyl',
             'nitrile': 'nitrile',
             'ether': 'ether',
             'amine': 'amine',
             'alcohol': 'alcohol',
             'acid anhydride': 'ester',
             'amide': 'aliphatic',
             'acid anhydride or ester': 'ester',
             'aromatic': 'aromatic'
             }


def factory():
    return 0


class_dct = defaultdict(factory, class_dct)
u = 0

# ECN 大类 → 数值映射
ecn_dct = {"aliphatic": 1.00,
           "aromatic": 1.00,
           "olefinic": 0.95,
           "acetylinic": 1.30,
           "carbonyl": u,
           "carboxyl": u,
           "nitrile": 0.30,
           "ether": -1.00 + 1.5,
           "primary alcohol": -0.50 + 1,
           "secondary alcohol": -0.75 + 1,
           "tertiary alcohol": -0.25 + 1,
           "amine": u + 1
           }

ecn_dct = defaultdict(factory, ecn_dct)
