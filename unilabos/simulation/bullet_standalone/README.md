# 独立 Bullet 后端（B 方案：独立 DLL + RB_* 兼容层）

本目录实现不依赖 Blender 进程内符号的 Bullet 桥：C++ 源码把 Blender 的
`RB_*` C ABI 重新实现在独立编译的 Bullet 上，Python 通过 ctypes 加载
Windows `.dll` / Linux `.so` / macOS `.dylib` 同一套源码的产物。

- 来源工作树：`E:\Design_projects\Lab-OS-Testbed\.worktrees\os-bullet-standalone`
  （分支 `codex/os-bullet-standalone`，基线 `c2d1cda9`）
- 集成位置：`codex/os-bullet-portable`，由
  `unilabos/simulation/bullet_native.py` 的 `UNILABOS_BULLET_BACKEND` 选择调用；
  **未 commit / 未 push**。

## 结论

| 项目 | 状态 |
|---|---|
| Windows x64 独立 DLL 构建 + 加载 | 通过（cmake 4.4.3 + ninja 1.13.2 + clang++ 22.1.8 + conda-forge bullet-cpp 3.25 float64） |
| `bullet_native` 所需 43 个 `RB_*` + 7 个 `lab_*` 符号 | 全部导出，源码解析测试守护 |
| 最小闭环（盒体落地 + 射线 + hinge） | C++ smoke 9/9、Python ctypes 用例全过 |
| `World` / `B` / `E` 模块面映射 | `compat.py` 与 `bullet_native` 同名同语义；13 个基础用例 + 4 个 USD 用例 |
| `bullet_usd` 建体路径映射 | `usd_compat.py`（无 mathutils）；pxr 0.26.8 下 compound/convexHull + SDF/GImpact 两条路径通过 |
| Linux / macOS | 同源代码与 loader 命名已就位，**本机未实测**（无构建主机） |
| ABI 精确一致 | 函数签名/精度一致；底层 Bullet 3.25 与 Blender 内置 3.07 存在行为差异，见下文 |

## 目录

```
unilabos/simulation/bullet_standalone/
├── __init__.py           # 对外导出 loader API
├── loader.py             # ctypes 加载 + 按摘要缓存构建（Windows/Linux/macOS）
├── compat.py             # bullet_native 的模块面：B/E/C/V3/V4/Contact/World/pose/velocity/quat
├── usd_compat.py         # bullet_usd 的无 mathutils 版本：Body/shape/constraint
├── cpp/
│   ├── CMakeLists.txt
│   ├── src/rb_abi.h      # 导出宏 + RB_*/lab_* 声明（float 数组 ABI，对齐 Blender）
│   ├── src/rb_internal.h # 不透明对象布局、四元数/约束帧转换
│   ├── src/rb_compat.cpp # RB_* 实现（世界/刚体/形状/约束）
│   ├── src/lab_extras.cpp# lab_contacts/lab_ray/lab_inertia/lab_ignore/lab_force_at/lab_friction_2d/lab_shape_aabb
│   └── tests/smoke_test.cpp # C++ 最小闭环
└── README.md
tests/blender/
├── conftest.py
├── test_bullet_standalone_abi.py     # 原始 C ABI：落体/射线/hinge/接触溢出/符号面
├── test_bullet_standalone_compat.py  # World/pose/quat/axis_quaternion
└── test_bullet_standalone_usd.py     # pxr 集成（默认跳过）
```

## 工具链现状与安装记录

初始 `PATH` 探测（Windows，2026-10-25）：

```
cl MISSING   cmake MISSING   ninja MISSING   clang++ MISSING   g++ MISSING
conda 26.3.2 (C:\Users\DP\miniforge3)
Visual Studio Build Tools 2022 17.14.41（含 MSVC 14.44.35207 + Windows SDK 10.0.26100）
```

第一次 `conda create -n bullet-standalone -c conda-forge cmake ninja clangxx bullet`
在 defaults + conda-forge 双通道下解析 11 分钟未完成，已终止。改用 mamba +
`--override-channels` 后成功：

```powershell
mamba create -y -n bullet-standalone --override-channels -c conda-forge `
  cmake ninja clangxx bullet-cpp
# 结果：cmake 4.4.3, ninja 1.13.2, clangxx 22.1.8, bullet-cpp 3.25 (hd8fd7ce_5)

# CMake 的 Windows-Clang 平台模块强制 -fuse-ld=lld，而 conda clang 不自带 lld：
mamba install -y -n bullet-standalone --override-channels -c conda-forge lld
# 结果：lld 23.1.1
```

补充说明：

- `bullet-cpp 3.25` 同时安装 32/64 位标量版本；float64 为静态库
  `BulletDynamics-float64.lib`、`BulletCollision-float64.lib`、
  `LinearMath-float64.lib`、`Bullet3Common-float64.lib`，安装的
  `BulletConfig-float64.cmake` 给出 `BULLET_DEFINITIONS = -DBT_USE_DOUBLE_PRECISION`。
  独立桥只链接 `*-float64`（loader/CMake 都会拒绝非 float64 变体）。
- 该 float64 变体按 `-DBULLET2_MULTITHREADING=ON` 构建（`BT_THREADSAFE=1`），
  但安装的 CMake 配置不导出这个宏；loader 默认 `UNILABOS_BULLET_THREADSAFE=1`，
  自定义 Bullet 构建可用 `=0` 关闭。
- CMake 的 Windows-Clang 平台模块会 `enable_language(RC)`；本机没有 llvm-rc，
  loader 自动使用 SDK 的
  `C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\rc.exe`。
- 传给 CMake 的 Windows 路径统一转成 `/`：反斜杠会被原样写进
  `CMakeRCCompiler.cmake` 并触发 `Invalid character escape '\P'`。

## 构建与加载

自动路径（推荐）：

```powershell
$env:UNILABOS_BULLET_ROOT = "C:\Users\DP\miniforge3\envs\bullet-standalone"
$env:UNILABOS_BULLET_THREADSAFE = "1"
python -c "from unilabos.simulation.bullet_standalone import loader; print(loader.load().path)"
```

loader 会：定位 Bullet float64 头文件/库 → 计算源码+工具链摘要 → 在
`%LOCALAPPDATA%\unilabos\bullet-standalone\<digest>\` 配置/编译并缓存 →
`ctypes.CDLL` 加载 → 校验 `lab_scalar_bytes()==8` 与 `lab_abi_version()==1`。
可设缓存目录 `UNILABOS_BULLET_STANDALONE_CACHE`，强制重建
`UNILABOS_BULLET_STANDALONE_REBUILD=1`。

本次实际执行的配置/编译命令（缓存 `0f066efb…`，摘自 `build.log`）：

```
cmake.exe -S <repo>/unilabos/simulation/bullet_standalone/cpp -B <cache>/build -G Ninja
  -DCMAKE_BUILD_TYPE=Release
  -DCMAKE_CXX_COMPILER=.../bullet-standalone/Library/bin/clang++.exe
  -DCMAKE_MAKE_PROGRAM=.../bullet-standalone/Library/bin/ninja.exe
  -DUNILAB_BULLET_INCLUDE_DIR=.../bullet-standalone/Library/include/bullet
  -DUNILAB_BULLET_LIBRARY_DIR=.../bullet-standalone/Library/lib
  -DUNILAB_BULLET_THREADSAFE=ON
  -DCMAKE_RC_COMPILER=C:/Program Files (x86)/Windows Kits/10/bin/10.0.26100.0/x64/rc.exe

cmake.exe --build <cache>/build --config Release --parallel
[1/3] Building CXX object .../lab_extras.cpp.obj
[2/3] Building CXX object .../rb_compat.cpp.obj
[3/3] Linking CXX shared library unilab_bullet_standalone.dll
```

产物：`unilab_bullet_standalone.dll`，727,040 字节；
`dumpbin /dependents` 仅 `VCRUNTIME140_1.dll` 与 UCRT api-set（Bullet 已静态链接，
无需 conda 运行时 DLL）。`manifest.json` 记录 Bullet 目录、编译器版本、产物 sha256。

smoke 可执行文件：配置时加 `-DUNILAB_BULLET_BUILD_SMOKE_TEST=ON`（loader 对应
`UNILABOS_BULLET_STANDALONE_SMOKE=1`），生成 `unilab_bullet_smoke_test.exe`。

## 最小闭环结果

C++（`unilab_bullet_smoke_test.exe`）：

```
ok   Bullet 标量为 double
ok   ABI 版本为 1
ok   盒体落到地面高度
ok   落地后存在接触流
ok   地面/盒体法向冲量
ok   射线命中地面距离
ok   射线排除指定刚体
ok   铰链保持转轴半径
ok   铰链绕 pivot 旋转约 -0.126 rad
SMOKE OK
```

Python：

```powershell
$env:UNILABOS_BULLET_ROOT="C:\Users\DP\miniforge3\envs\bullet-standalone"
$env:UNILABOS_BULLET_THREADSAFE="1"
$env:UNILABOS_BULLET_STANDALONE_REQUIRE="1"
C:\Users\DP\miniforge3\envs\unilab\python.exe -m pytest tests/blender -q
# 13 passed, 1 skipped   （USD 用例默认跳过）

# USD 集成（usd 环境有 pxr 0.26.8；额外把 numpy/pytest 装到临时 --target 目录）
$env:PYTHONPATH="<scratch>/pyextra;<worktree>"
$env:UNILABOS_BULLET_STANDALONE_USD="1"
C:\Users\DP\miniforge3\envs\usd\python.exe -m pytest tests/blender -q
# 17 passed
```

覆盖内容：

- 0.1 kg 盒体从 0.30 m 落到地面，静止高度 0.02 m，接触合力在 0.5–2.0 N；
- `lab_ray` 命中距离 0.08 m、可排除指定刚体、遵守 `[0.005, 0.1] m` 过滤；
- hinge 保持 0.05 m 转轴半径，角速度按绕 pivot 的转动惯量从 -2.0 降到约 -0.50 rad/s，
  0.25 s 转角约 -0.126 rad；
- 接触容量为 0 时返回 `-1`（Blender 的 `-(已写入+1)` 约定）；
- `bullet_native.py` 源码里出现的每个 `RB_*`/`lab_*` 符号都能从 DLL 解析；
- `compat.World` 的 body/box/step/contacts/names/tick 与 `bullet_native` 一致；
- pxr 建体：Cube → compound + convexHull，Mesh(`approximation=sdf`) → GImpact trimesh，
  均能落地并产生命名接触；`usd_compat.constraint` 的 hinge 轴映射在不依赖 mathutils
  的情况下绕 pivot 正常旋转。

## 接口映射

`compat.py` 可直接替换 `bullet_native` 的模块面：

| bullet_native | 独立实现 |
|---|---|
| `B = CDLL(None)`（Blender 进程内符号） | `B = E = CDLL(<独立 DLL>)` |
| `World()`：重力、1000 次迭代、split impulse、100000 接触缓冲 | 相同参数与 `contacts()` 字段（`normal_force_n = impulse × 240`） |
| `pose` / `velocity` / `quat`（mathutils） | numpy 实现，四元数顺序统一 `(w,x,y,z)` |
| `Contact` 结构 | 同样的 `a/b/position/normal/separation/impulse` 布局 |

`usd_compat.py` 对应 `bullet_usd`，差别只有：不导入 mathutils，约束轴旋转用
`compat.axis_quaternion()`（等价 `Vector.rotation_difference`）。

SZLab 消费者接入（已由 `bullet_native` 的后端选择完成，消费者零改动）：

1. `UNILABOS_BULLET_BACKEND=auto` 时 `bullet_native` 先试原生桥，失败后调用
   `bullet_standalone.loader.load()`，成功后把 `compat` 的
   `B/E/C/V3/V4/Contact/World/pose/velocity/quat` 注入模块面；因此
   `bullet_usd`（`from .bullet_native import *`）和 SZLab
   `blender_bullet/{world,robot,station}.py` 的 import 都不需要修改。
2. `bullet_usd` 的 `Body`/`shape`/`constraint` 直接作用在独立库句柄上；
   `usd_compat.py` 保留给不导入 mathutils 的进程使用（本仓当前无消费者）。
3. `UNILABOS_BULLET_BACKEND=standalone` 可强制独立库（不可用即报错），
   `=bpy` 强制 Blender 公共 API，`=native` 强制原生符号；`auto` 不会把失败
   的后端静默冒充成另一个。

## 与 Blender 内部 ABI / 行为的差异

| 维度 | Blender 内置桥 | 本独立库 | 影响 |
|---|---|---|---|
| Bullet 版本 | 内置 3.07（提交 `d13f752e`） | conda-forge 3.25 | 求解器/GImpact 细节差异，需 A/B 场景验收 |
| 标量精度 | double（`extern/bullet2/CMakeLists.txt`: “Use double precision … `-DBT_USE_DOUBLE_PRECISION`”） | double（`static_assert(sizeof(btScalar)==8)`） | 一致 |
| 对外数组 | `float[3]/float[4]`（内部 `btScalar` double） | 同 | 一致 |
| 线程宏 | bullet2 单线程（未定义 `BT_THREADSAFE`） | conda float64 为 `BT_THREADSAFE=1`，本库同宏编译 | 单线程调用行为一致；仅内联锁代码不同 |
| 对象布局 | `rbDynamicsWorld` 6 字段、`rbRigidBody{body,col_groups}` 等 | 内部布局对齐同一结构；Python 只持有不透明指针 | 无 ABI 暴露 |
| 惯量修正 | Blender 打了 `inertia.patch`（凸多面体/圆锥不再重复计 margin） | 上游 3.25 未打补丁 | 未显式给 USD 惯量的刚体（如 piston）转动惯量略有差异；`lab_inertia` 路径一致 |
| 凸包窄相 | 3.07 默认路径 | 额外调用 `initializePolyhedralFeatures()` | 3.25 在 5e-5 margin 下 GJK/EPA 会抖动/穿透；预生成 polyhedron 后稳定，窄相几何=裸凸包（margin 不再外扩，消费者 margin 仅 5e-5） |
| 凸包 AABB | 带 margin | 上游 3.25 会重复累计 margin（AABB 偏大） | 仅宽相包围盒变大，不影响接触几何；`lab_shape_aabb` 可观测 |
| `RB_body_get_transform_matrix` | 把 `btScalar*` 直接写进 `float[4][4]` | 逐元素转 float | double 构建下本库避免越界写；消费者未使用该函数 |
| GImpact / trimesh | 3.07 GImpact | 3.25 GImpact，仍注册 `btGImpactCollisionAlgorithm` | SDF 网格碰撞行为可能有数值差异 |
| 接触返回约定 | `-count-1` | 同 | 一致 |
| 碰撞分组 | 自定义 `btOverlapFilterCallback` + `col_groups` 位与 | 同 | 一致 |
| 约束马达 | 6DOF：全轴解锁 + 线性/角度 X 马达 | 同（逐行对齐 pivot/orn、hinge softness 0.9/0.3/1.0） | 一致 |
| 静态刚体 | mass=0，不设 `CF_STATIC_OBJECT` | 同 | 一致 |

内存所有权：`RB_dworld_delete`/`RB_body_delete`/`RB_shape_delete`/`RB_constraint_delete`
按 Blender 顺序释放；`RB_shape_delete` 递归释放 compound 子形状与 trimesh 数据。
另外新增只读诊断 `lab_shape_aabb()`，Python 测试用它检查形状包围盒。

## 阻塞 / 后续步骤

Windows x64 上没有阻塞，构建、加载、最小闭环和接口映射均已跑通。剩余事项：

1. **集成到主工作树**：按上节 4 处 import 替换接入 `bullet_native.World/B/E`
   与 `bullet_usd`；保留 Blender 路径作为 A 方案，两者共存。
2. **真实 SZLab 场景 A/B 验收**：本工作树没有 Uni-Lab-SZLab 的资产（`assets`）、
   `embodied_cr7`、`isaac_workflow` 等依赖，也没有 scipy；需要在含这些包与 pxr 的
   环境里跑完整设备流程，并与 Blender 内置桥逐步比对接触力/位姿阈值。未执行，
   不能据此宣称生产等价。
3. **Linux / macOS 实测**：源码、loader 命名（`.so`/`.dylib`）、RPATH 指向
   `Bullet*-float64` 库目录均已处理，但本机只有 Windows；需要在对应 runner 上
   `conda install bullet-cpp cmake ninja clangxx` 后复跑 `pytest tests/blender`。
4. **进一步逼近 Blender 行为**：若 A/B 显示差异不可接受，可改为 FetchContent
   引入 Bullet 3.07 源码 + Blender `inertia.patch` 自行编译 float64 库；
   代价是构建时间与补丁维护，当前未做。
5. **分发**：消费机若没有编译工具链，可把某平台产物放到
   `bullet_standalone/lib/<platform>/` 或用 `UNILABOS_BULLET_STANDALONE` 指定；
   loader 会直接加载而不再构建。仓库当前未提交二进制产物。
6. 测试默认不在 CI 白名单（`tests/ci_python_suite.txt` 未包含 `tests/blender`），
   因为需要 conda 工具链；如需 CI 守护，建议单独的 native job。

## 环境变量速查

| 变量 | 作用 |
|---|---|
| `UNILABOS_BULLET_ROOT` | 含 Bullet 的 conda 前缀（也用于找 cmake/ninja/clang++） |
| `UNILABOS_BULLET_INCLUDE_DIR` / `_LIBRARY_DIR` | 直接指定头文件/库目录 |
| `UNILABOS_BULLET_TOOLS_PREFIX` / `_TOOLS_DIR` | 构建工具前缀/bin 目录 |
| `UNILABOS_BULLET_CMAKE` / `_NINJA` / `_CXX` / `_RC` | 显式指定工具 |
| `UNILABOS_BULLET_THREADSAFE` | `1`/`0`，默认 1（conda float64 构建） |
| `UNILABOS_BULLET_STANDALONE` | 直接加载已有动态库 |
| `UNILABOS_BULLET_STANDALONE_CACHE` | 构建缓存目录 |
| `UNILABOS_BULLET_STANDALONE_REBUILD` | `1` 强制重建 |
| `UNILABOS_BULLET_STANDALONE_SMOKE` | `1` 同时构建 C++ smoke |
| `UNILABOS_BULLET_STANDALONE_REQUIRE` | `1` 时缺工具链由测试失败而非跳过 |
| `UNILABOS_BULLET_STANDALONE_USD` | `1` 时运行 pxr 集成测试 |
