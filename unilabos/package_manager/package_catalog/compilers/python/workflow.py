"""把显式工作流源码（Workflow Source）投影为目录定义。"""

from __future__ import annotations

from collections.abc import Mapping

from unilabos.workflow.authoring_ast import (
    ActionDeclaration,
    AuthoringSyntaxError,
    CompositeDeclaration,
    parse_authoring_source,
)
from unilabos.workflow.authoring_material import MaterialSourceDeclaration
from unilabos.workflow.source_manifest import EditablePackageManifest

from ...model import (
    PackageCompileError,
    PackageDefinition,
    PackageDiagnostic,
)
from ...sources import WorkspaceSource


def compile_workflow_definitions(
    *,
    source: WorkspaceSource,
    import_package: str,
    namespace: str,
    manifest: EditablePackageManifest | None,
    content_hashes: Mapping[str, str],
) -> tuple[PackageDefinition, ...]:
    """编译清单显式授权的全部工作流源码。

    参数：``source`` 是受限工作区来源；``import_package`` 是导入包身份；
    ``namespace`` 是社区命名空间；``manifest`` 是可选封闭清单；
    ``content_hashes`` 是源码摘要索引。
    返回：保持稳定全限定身份排序的工作流定义集合。
    异常：源码不是可信创作子集、UUID 不一致或定义身份重复时抛出
    ``PackageCompileError``，不返回部分集合。
    """

    if manifest is None:
        return ()
    # ``definitions`` 收集本清单同代的完整工作流目录候选，函数结束前不会发布。
    definitions: list[PackageDefinition] = []
    # ``identities`` 按规范 FQID 关闭式拒绝包内重复工作流定义。
    identities: set[str] = set()
    for entry in manifest.workflows:
        # ``workflow_uuid`` 是清单与源码装饰器必须共同确认的稳定工作流身份。
        workflow_uuid = entry.workflow_uuid
        # ``logical_path`` 是源码证据和诊断共用的包内路径身份。
        logical_path = f"{import_package}/{entry.relative_path}"
        try:
            # ``python_source`` 只进入可信静态解析器，不执行作者模块。
            python_source = source.read_bytes(logical_path).decode("utf-8")
            # ``program`` 是不执行源码得到的可信工作流静态程序。
            program = parse_authoring_source(
                python_source=python_source,
                expected_workflow_uuid=workflow_uuid,
            )
        except (UnicodeError, ValueError, AuthoringSyntaxError) as error:
            diagnostic_code = (
                error.code
                if isinstance(error, AuthoringSyntaxError)
                else "workflow_source_invalid"
            )
            raise PackageCompileError(
                (
                    PackageDiagnostic(
                        code=diagnostic_code,
                        message="工作流源码无法静态编译",
                        path=logical_path,
                    ),
                )
            ) from error
        # ``fqid`` 是跨包查询和冲突检测使用的规范工作流定义身份。
        fqid = f"{namespace}.{program.function_name}"
        if fqid in identities:
            raise PackageCompileError(
                (
                    PackageDiagnostic(
                        code="duplicate_definition",
                        message="工作流定义全限定身份重复",
                        path=logical_path,
                    ),
                )
            )
        identities.add(fqid)
        # ``module`` 与函数符号共同保存源码映射，不承担工作流 UUID 身份。
        module = logical_path.removesuffix(".py").replace("/", ".")
        definitions.append(
            PackageDefinition(
                kind="workflow",
                id=program.function_name,
                fqid=fqid,
                module=module,
                symbol=program.function_name,
                declaring_file=logical_path,
                content_hash=content_hashes[logical_path],
                title=program.display_name,
                description=program.description or "",
                details={
                    "action_references": _action_references(program.actions),
                    "input_contract": program.input_contract,
                    "output_contract": [
                        {"name": name, "schema": schema}
                        for name, schema in program.declared_output_schemas
                    ],
                    "source_uri": (f"package://{import_package}/{entry.relative_path}"),
                    "workflow_uuid": workflow_uuid,
                },
            )
        )
    return tuple(sorted(definitions, key=_definition_fqid))


def _definition_fqid(definition: PackageDefinition) -> str:
    """读取工作流目录定义的规范全限定身份排序键。

    参数：``definition`` 是已经完成静态校验的工作流定义。
    返回：定义的稳定 ``fqid`` 字符串。
    异常：无；输入由本模块构造且身份已验证。
    """

    return definition.fqid


def _action_references(actions: tuple[object, ...]) -> list[dict[str, str]]:
    """投影工作流内动作、子工作流和物料来源的静态引用。

    参数：``actions`` 是可信作者解析器产生的节点声明顺序。
    返回：不含运行状态或数据库身份的封闭引用列表。
    异常：遇到未知节点声明类型时抛出 ``TypeError``，防止静默漏编译。
    """

    references: list[dict[str, str]] = []
    for declaration in actions:
        if isinstance(declaration, ActionDeclaration):
            references.append(
                {
                    "action_name": declaration.action_name,
                    "device_symbol": declaration.device_symbol,
                    "kind": "action",
                }
            )
        elif isinstance(declaration, CompositeDeclaration):
            references.append(
                {
                    "kind": "workflow",
                    "module": declaration.module,
                    "symbol": declaration.symbol,
                }
            )
        elif isinstance(declaration, MaterialSourceDeclaration):
            references.append({"kind": "material_source"})
        else:
            raise TypeError(f"未知工作流静态节点声明: {type(declaration).__name__}")
    return references


__all__ = ["compile_workflow_definitions"]
