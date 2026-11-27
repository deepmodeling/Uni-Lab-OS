"""生成领域包自有的静态 @device 声明，把动作转发给已安装或已固化的实现类（来源：postproc-runtime 0.1.1 project.forwarder）。

Uni-Lab-OS 只静态扫描被装饰类自己的类体（ast_registry_scanner._extract_class_body），不看继承来的
方法；model.entry 相对声明文件所在目录解析，不允许 ``..``，且必须落在导入包内
（package_catalog/material_models._logical_model_path）。所以转发类必须在自己的类体里重新声明
OS 会登记的每一个成员，并按新位置给出相对模型入口：

- ``@action`` / ``@legacy_action``，同步与 async 都转发（async 用 ``return await``）；
- ``@property`` / ``@topic_config`` 与无参 ``get_*`` 状态，property 的 setter/deleter 一并转发；
- ``__init__`` 签名（OS 据此登记 init 参数），以及未装饰的公开方法（OS 登记为 auto 方法）；
- 保留方法 docstring（OS 用作动作说明），``@not_action`` / ``@subscribe`` / 私有方法不重声明；
- registry ID 默认不变；只有显式传 ``registry_id_prefix`` 才加前缀。实现类自己已用同一 ID 注册时拒绝生成，
  因为运行时 OS 会把两次登记判为重复（资产行为实现本身不得注册，正好满足）。

新消费者使用默认 ``ForwarderOptions()``。把 ``forward_*``、``keep_docstrings``、``import_module_symbols``、
``copy_literal_constants`` 全部关掉、``always_import_typed_dict_and_resource_slot`` 打开，即得到来源实现
（postproc-runtime 0.1.1）的原有输出；领域专用的导入改写由调用方通过 ``import_rewrites`` 等参数提供。
"""
from __future__ import annotations

import ast
import copy
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Mapping

_STATUS = ('property', 'topic_config')
_SKIP = ('not_action', 'subscribe')


@dataclass(frozen=True)
class ForwarderOptions:
    """转发生成选项。默认值即中立合同。"""

    registry_id_prefix: str | None = None
    import_rewrites: Mapping[str, str] = field(default_factory=dict)
    drop_imported_names: Mapping[str, frozenset] = field(default_factory=dict)
    typed_dict_sources: tuple = ()
    model_entry: str | None = None
    forward_async: bool = True
    forward_bare_action: bool = True
    forward_legacy_action: bool = True
    forward_status: bool = True
    forward_init: bool = True
    forward_auto_methods: bool = True
    keep_docstrings: bool = True
    import_module_symbols: bool = True
    copy_literal_constants: bool = True
    always_import_typed_dict_and_resource_slot: bool = False
    allow_registered_implementation: bool = False


def relative_model_entry(declaration_file, model_file, package_dir):
    """按 OS 合同计算模型相对入口：必须在声明文件目录之下、且在导入包内。"""
    declaration_dir = Path(declaration_file).resolve().parent
    model = Path(model_file).resolve()
    package = Path(package_dir).resolve()
    if not model.is_relative_to(package):
        raise ValueError('Model entry must live inside the import package')
    if not model.is_relative_to(declaration_dir):
        raise ValueError('Model entry must be below the declaration directory (OS rejects "..")')
    return PurePosixPath(model.relative_to(declaration_dir).as_posix()).as_posix()


def _name(decorator):
    f = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _decorator_names(item, bare):
    names = set()
    for d in item.decorator_list:
        if isinstance(d, ast.Call) and isinstance(d.func, ast.Name):
            names.add(d.func.id)
        elif bare and isinstance(d, ast.Name):
            names.add(d.id)
    return names


def _property_accessor(item):
    """返回 (属性名, 'setter'|'deleter')，否则 None。"""
    for d in item.decorator_list:
        if isinstance(d, ast.Attribute) and d.attr in ('setter', 'deleter') and isinstance(d.value, ast.Name):
            return d.value.id, d.attr
    return None


def _call_arguments(method):
    a = method.args
    positional = [ast.Name(id='self', ctx=ast.Load())]
    keywords = []
    positional += [ast.Name(id=x.arg, ctx=ast.Load()) for x in a.posonlyargs if x.arg != 'self']
    regular = [x.arg for x in a.args if x.arg != 'self']
    if a.vararg:
        positional += [ast.Name(id=x, ctx=ast.Load()) for x in regular]
        positional.append(ast.Starred(value=ast.Name(id=a.vararg.arg, ctx=ast.Load()), ctx=ast.Load()))
    else:
        keywords += [ast.keyword(arg=x, value=ast.Name(id=x, ctx=ast.Load())) for x in regular]
    keywords += [ast.keyword(arg=x.arg, value=ast.Name(id=x.arg, ctx=ast.Load())) for x in a.kwonlyargs]
    if a.kwarg:
        keywords.append(ast.keyword(arg=None, value=ast.Name(id=a.kwarg.arg, ctx=ast.Load())))
    return positional, keywords


def _impl_attr(name):
    return ast.Attribute(value=ast.Name(id='_Implementation', ctx=ast.Load()), attr=name, ctx=ast.Load())


def _docstring(item, keep):
    if keep and item.body and isinstance(item.body[0], ast.Expr) and isinstance(getattr(item.body[0], 'value', None), ast.Constant) \
            and isinstance(item.body[0].value.value, str):
        return [copy.deepcopy(item.body[0])]
    return []


def _legacy_body(method):
    """旧实现：所有参数按关键字传，不处理 *args/**kwargs。"""
    args = [a.arg for a in method.args.args if a.arg != 'self'] + [a.arg for a in method.args.kwonlyargs]
    return ast.Call(func=_impl_attr(method.name), args=[ast.Name(id='self', ctx=ast.Load())],
                    keywords=[ast.keyword(arg=a, value=ast.Name(id=a, ctx=ast.Load())) for a in args])


def _forward(item, options, kind):
    method = copy.deepcopy(item)
    doc = _docstring(item, options.keep_docstrings)
    if kind == 'legacy':
        method.body = [ast.Return(_legacy_body(method))]
        return method
    if any(_name(d) in ('staticmethod', 'classmethod') for d in item.decorator_list):
        raise ValueError(f'Cannot forward static/class method {item.name}; declare it on an instance method')
    if kind == 'property':
        call = ast.Call(func=ast.Attribute(value=_impl_attr(item.name), attr='fget', ctx=ast.Load()),
                        args=[ast.Name(id='self', ctx=ast.Load())], keywords=[])
    elif kind in ('setter', 'deleter'):
        prop, _ = _property_accessor(item)
        attr = 'fset' if kind == 'setter' else 'fdel'
        names = [a.arg for a in [*method.args.posonlyargs, *method.args.args]]
        call = ast.Call(func=ast.Attribute(value=_impl_attr(prop), attr=attr, ctx=ast.Load()),
                        args=[ast.Name(id=n, ctx=ast.Load()) for n in names], keywords=[])
    else:
        positional, keywords = _call_arguments(method)
        call = ast.Call(func=_impl_attr(item.name), args=positional, keywords=keywords)
    if isinstance(item, ast.AsyncFunctionDef):
        call = ast.Await(value=call)
    statement = ast.Expr(value=call) if kind in ('init', 'setter', 'deleter') else ast.Return(value=call)
    method.body = doc + [statement]
    return method


def _select_members(original, options):
    """按 OS 静态扫描规则选出需要在类体内重新声明的成员。"""
    legacy = not (options.forward_bare_action or options.forward_async or options.forward_status
                  or options.forward_init or options.forward_auto_methods)
    out = []
    for item in original.body:
        if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        is_async = isinstance(item, ast.AsyncFunctionDef)
        if legacy:
            if not is_async and any(isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id == 'action'
                                    for d in item.decorator_list):
                out.append(_forward(item, options, 'legacy'))
            continue
        if is_async and not options.forward_async:
            continue
        if item.name == '__init__':
            if options.forward_init:
                out.append(_forward(item, options, 'init'))
            continue
        if item.name.startswith('_'):
            continue
        accessor = _property_accessor(item)
        if accessor:
            if options.forward_status:
                out.append(_forward(item, options, accessor[1]))
            continue
        names = _decorator_names(item, options.forward_bare_action)
        if 'action' in names or (options.forward_legacy_action and 'legacy_action' in names):
            out.append(_forward(item, options, 'call'))
        elif any(n in names for n in _SKIP):
            continue
        elif any(_name(d) == 'property' for d in item.decorator_list):
            if options.forward_status:
                out.append(_forward(item, options, 'property'))
        elif 'topic_config' in names or any(_name(d) == 'topic_config' for d in item.decorator_list):
            if options.forward_status:
                out.append(_forward(item, options, 'call'))
        elif item.name.startswith('get_') and not [a for a in item.args.args if a.arg != 'self']:
            if options.forward_status:
                out.append(_forward(item, options, 'call'))
        elif item.name in ('post_init', '__str__', '__repr__'):
            continue
        elif options.forward_auto_methods:
            out.append(_forward(item, options, 'call'))
    return out


def _top_level_definitions(tree):
    defs = {}
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            defs[node.name] = node
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    defs[t.id] = node
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defs[node.target.id] = node
    return defs


def _literal_assign(node):
    value = getattr(node, 'value', None)
    if value is None:
        return False
    try:
        ast.literal_eval(value)
        return True
    except Exception:
        return False


def _names_in(nodes):
    return {n.id for root in nodes for n in ast.walk(root) if isinstance(n, ast.Name)}


def _typed_dicts(tree):
    return [n for n in tree.body if isinstance(n, ast.ClassDef)
            and any(isinstance(b, ast.Name) and b.id == 'TypedDict' for b in n.bases)]


def _device_ids(cls):
    ids = set()
    for d in cls.decorator_list:
        if isinstance(d, ast.Call) and _name(d) == 'device':
            for kw in d.keywords:
                if kw.arg == 'id' and isinstance(kw.value, ast.Constant):
                    ids.add(kw.value.value)
                elif kw.arg == 'ids' and isinstance(kw.value, (ast.List, ast.Tuple)):
                    ids.update(e.value for e in kw.value.elts if isinstance(e, ast.Constant))
    return ids


def generate_forwarder(source, module, class_name, *, declaration=None, options=ForwarderOptions()):
    """返回转发模块源码。

    参数：source 为实现类所在 .py；module 为它的可导入模块名；class_name 为实现类名；
    declaration 为可选的 @device 关键字参数（替换原类装饰器，用于资产导出的实现类）。
    """
    tree = ast.parse(Path(source).read_text('utf8'))
    original = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name), None)
    if original is None:
        raise ValueError(f'Class {class_name} not found in {source}')
    imports = [copy.deepcopy(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    if options.always_import_typed_dict_and_resource_slot:
        # 旧行为：总是带上 OS 静态目录解析结果类型所需的两个名字。
        imports.append(ast.ImportFrom(module='typing', names=[ast.alias(name='TypedDict')], level=0))
        imports.append(ast.ImportFrom(module='unilabos.registry.placeholder_type', names=[ast.alias(name='ResourceSlot')], level=0))
    for node in imports:
        if isinstance(node, ast.ImportFrom) and node.level:
            node.module = '.'.join(module.split('.')[:-node.level] + ([node.module] if node.module else []))
            node.level = 0
        if isinstance(node, ast.ImportFrom) and node.module in options.import_rewrites:
            node.module = options.import_rewrites[node.module]
        if isinstance(node, ast.ImportFrom) and node.module in options.drop_imported_names:
            node.names = [a for a in node.names if a.name not in options.drop_imported_names[node.module]]
    imports = [n for n in imports if not isinstance(n, ast.ImportFrom) or n.names]
    if options.import_module_symbols:
        # TYPE_CHECKING 下的导入只用于注解；原样带上，避免注解名在转发模块里悬空。
        for node in tree.body:
            if isinstance(node, ast.If) and 'TYPE_CHECKING' in ast.unparse(node.test):
                block = copy.deepcopy(node)
                for inner in ast.walk(block):
                    if isinstance(inner, ast.ImportFrom) and inner.level:
                        inner.module = '.'.join(module.split('.')[:-inner.level] + ([inner.module] if inner.module else []))
                        inner.level = 0
                imports.append(block)

    extra_trees = [ast.parse(Path(p).read_text('utf8')) for p in options.typed_dict_sources]
    declarations = list({n.name: copy.deepcopy(n) for t in [*extra_trees, tree] for n in _typed_dicts(t)}.values())
    declared = {n.name for n in declarations}

    members = _select_members(original, options)
    cls = copy.deepcopy(original)
    cls.bases = [ast.Name(id='_Implementation', ctx=ast.Load())]
    cls.body = _docstring(original, options.keep_docstrings) + members or [ast.Pass()]
    if not cls.body:
        cls.body = [ast.Pass()]

    constants = []
    if options.import_module_symbols or options.copy_literal_constants:
        defs = _top_level_definitions(tree)
        imported = {(a.asname or a.name).split('.')[0] for root in imports for n in ast.walk(root)
                    if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names}
        used = _names_in([*cls.decorator_list, *members]) | _names_in(declarations)
        decorator_names = _names_in(cls.decorator_list)
        wanted = sorted(n for n in used if n in defs and n not in imported and n not in declared and n != class_name)
        symbols = []
        for name in wanted:
            node = defs[name]
            if options.copy_literal_constants and name in decorator_names and _literal_assign(node):
                # OS 只从声明文件根级字面量展开 available_sites/id_meta 引用，必须原样复制。
                if node not in constants:
                    constants.append(node)
            elif options.import_module_symbols:
                symbols.append(name)
        if symbols:
            imports.append(ast.ImportFrom(module=module, names=[ast.alias(name=s) for s in symbols], level=0))
        constants = [copy.deepcopy(n) for n in constants]
    if options.always_import_typed_dict_and_resource_slot is False and declarations and \
            not any(isinstance(n, ast.ImportFrom) and any((a.asname or a.name) == 'TypedDict' for a in n.names) for n in imports):
        imports.append(ast.ImportFrom(module='typing', names=[ast.alias(name='TypedDict')], level=0))
    imports.append(ast.ImportFrom(module=module, names=[ast.alias(name=class_name, asname='_Implementation')], level=0))

    if declaration:
        imports.append(ast.ImportFrom(module='unilabos.registry.decorators', names=[ast.alias(name='device')], level=0))
        cls.decorator_list = [ast.Call(func=ast.Name(id='device', ctx=ast.Load()), args=[],
                                       keywords=[ast.keyword(arg=k, value=ast.parse(repr(v), mode='eval').body)
                                                 for k, v in declaration.items()])]
    for decorator in cls.decorator_list:
        if not (isinstance(decorator, ast.Call) and _name(decorator) == 'device'):
            continue
        for kw in decorator.keywords:
            if options.registry_id_prefix and kw.arg == 'id' and isinstance(kw.value, ast.Constant):
                kw.value = ast.Constant(options.registry_id_prefix + '_' + kw.value.value)
            elif options.registry_id_prefix and kw.arg == 'ids' and isinstance(kw.value, (ast.List, ast.Tuple)):
                for i, elt in enumerate(kw.value.elts):
                    if isinstance(elt, ast.Constant):
                        kw.value.elts[i] = ast.Constant(options.registry_id_prefix + '_' + elt.value)
            elif options.model_entry and kw.arg == 'model' and isinstance(kw.value, ast.Dict):
                for i, k in enumerate(kw.value.keys):
                    if isinstance(k, ast.Constant) and k.value == 'entry':
                        kw.value.values[i] = ast.Constant(options.model_entry)
    if not options.allow_registered_implementation:
        # 运行时 @device 按 ID 全局登记；实现类已登记的 ID 再由转发类登记一次，OS 会以重复 ID 拒绝。
        overlap = _device_ids(original) & _device_ids(cls)
        if overlap:
            raise ValueError('Implementation already registers ' + ', '.join(sorted(overlap)) +
                             '; give a registry_id_prefix or forward an undecorated implementation')
    body = imports + constants + declarations + [cls]
    return ast.unparse(ast.fix_missing_locations(ast.Module(body=body, type_ignores=[]))) + '\n'


def write_forwarder(target, source, module, class_name, *, package, declaration=None, options=ForwarderOptions(),
                    origin='package'):
    """写出转发模块并返回实现记录：class、source_sha256、project_class、origin。"""
    from .paths import sha
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(generate_forwarder(source, module, class_name, declaration=declaration, options=options), encoding='utf8')
    relative = target.with_suffix('').resolve().relative_to(Path(package).resolve().parent)
    return {'class': module + ':' + class_name, 'source_sha256': sha(source), 'generated_sha256': sha(target),
            'project_class': '.'.join(relative.parts) + ':' + class_name, 'origin': origin}
