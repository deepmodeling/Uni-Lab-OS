# 外观处理：标准 PBR、AI 重贴图与精确标识

按资产用途选择处理方式。外观逼真本身不能证明几何准确、光学传感有效、
物料身份正确或机械行为可信。

## CAD 网格法线

读取时机：CAD 网格导出 USD、网格重新分组/焊接/变换，或 Blender 出现大平面三角反光斑时。
目标是平面法线一致、曲面法线连续、真实棱边两侧法线分离；保留三角网格本身。

### 生成与写入

1. 优先保留 CAD 内核生成的可靠法线及三角形到源 CAD 面的对应关系。缺失时按源面重建：
   同一平面的各面角使用一致法线；圆柱、圆角等使用曲面法线；真实棱边两侧分别保存，
   相切连接处保持连续。共用几何顶点不意味着共用着色法线。
2. 默认写入 USD Mesh 的 `normals`，设置 `interpolation=faceVarying`，按
   `faceVertexIndices` 的面角顺序保存。已有可靠的等价法线编码可保留，但须通过下述读回检查。
   CAD 离散三角网格明确使用 `subdivisionScheme=none`。
3. 焊接顶点、重排/删除三角形时同步更新面角法线与源面映射。将变换烘焙进顶点时，
   法线按线性变换的逆转置转换并归一化；仅保留为 USD Xform 时不重复烘焙。
   镜像变换另查绕序、朝向和法线的一致性，薄壳/开放面沿用有依据的源面朝向。
4. 只有网格、没有可靠 CAD 面信息时，记录法线重建方法与局限：纯平面可用逐面法线；
   混合曲面按可确认的锐边分离后局部平滑。`Smooth by Angle` 或 `Weighted Normal`
   只能作为经渲染验证的补救方法，不能声称恢复了原 CAD 曲面法线。

### Blender 消费与诊断

- 导入后保留有效的自定义面角法线；导出 USD 时开启 Normals，并重新打开导出物核验。
  导入脚本发现“全部面平滑、没有自定义法线、同时包含大平面和折角”的零件时，列入待修复项，
  不对所有 CAD 零件统一执行 `Shade Smooth`。
- 大平面出现三角反光斑时，先检查平滑方式、法线、面朝向和重叠面。
  用同一几何、材质、相机和光照制作逐面法线对照，区分着色错误与几何错误；
  逐面法线对照可能让曲面分面，不能直接作为所有曲面的最终处理。
- 实体模式中不明显、或 Isaac 渲染正常，都不能证明 Blender 显示通过；
  也不能靠增加粗糙度、取消金属材质或重划三角网格掩盖法线问题。

### 完成判据

- 逐个检查受影响 USD 网格的有效法线、插值方式、数组长度、有限值及单位长度；
  `faceVarying` 的 `normals` 数量须等于 `sum(faceVertexCounts)`。
  记录缺失、异常及无法从来源恢复的网格，未解决项保留为外观缺口。
- 法线专项修复前后核对顶点、拓扑、材质绑定、对象变换与关节，确认这些数据未被顺带改变；
  若本次还授权了几何修复，则单独记录其变化。
- 在 Blender 重新导入交付 USD，核验法线实际被使用；保存并重开 `.blend`。
  用斜向面积光检查代表性大平面、棱边和圆柱/圆角，保存材质预览与至少一张实际渲染。
  同时面向 Isaac 等消费者交付时，用同一资产在该消费者检查对应部位并保留原生渲染证据。
  记录模型摘要、软件版本、相机/灯光、法线来源与实际结果；运行时缺失时明确记为 `not_run`。

## 选择并组合三条路线

| 路线 | 适用内容 | 需要独立保留 |
|---|---|---|
| 标准 PBR | 玻璃、金属、涂层、聚合物、简单色区 | CAD 面分组与具名材质 |
| Meshy 重贴图 | 有参考依据的复杂表面纹理或磨损外观 | 原始几何与生成原件 |
| 精确贴花 | 型号、刻度、试剂标签、安全标识 | 权威图稿或文字、尺寸与位置 |

先建立标准 PBR；需要更丰富的指定外观时再局部使用 AI 纹理。
精确文字和刻度放在独立贴花层；检查 AI 输出是否虚构文字、单位、标志或测量刻线。
无法识别的化学品标签保持未知，不能生成一个看似清晰的身份替代原证据。

视觉参数记录为来源声明、推断或实测。玻璃透射率和 IOR 不能证明实际光电传感器响应。
视觉、碰撞、机械材料与实验过程材料分别保存，避免相互覆盖。

## Retexture 接口与本客户端范围

本包自带 `scripts/meshy_retexture.py`，只依赖 Python 3.10+ 标准库。
`plan` 不调用 API；每次 `submit` 只发一次 POST；`status` 只发一次 GET；
`download` 只读取已保存的完成任务。

[Retexture 官方接口](https://docs.meshy.ai/en/api/retexture)已于 2026-10-21 核对。
实际付费提交前重新确认支持的选项。
API 接受 GLB/glTF/OBJ/FBX/STL；本客户端只接收本地自包含 GLB v2 或 STL，
文件不超过 100,000,000 字节。其他来源先导出；GLB 有外部资源 URI 时拒绝处理，
客户端不会自动打包远程依赖。

三种样式输入选择一种：1–800 字符文字、HTTPS 图片 URL、或 1–4 个 HTTPS 多视图 URL。
多视图第一张为正面，且必须使用 `meshy-7`。
默认模型固定为 `meshy-7`，不使用 `latest`；可设置 PBR、保留原 UV 和 2k/4k/8k。
`meshy-6-lite` 只支持 2k。本客户端尚未实现本地或 base64 参考图片输入。

`enable_original_uv` 只是要求服务保留 UV 布局，**不能证明**顶点、拓扑、单位、
坐标轴、变换、法线或材质分组未变。

## 上传或付费前

先准备确切输入、样式、预计 credits 和请求预览，再依据
[当前价格页](https://docs.meshy.ai/en/api/pricing)及账号可用性估算费用；本包不固化价格。
确认用户授权覆盖这些资产上传至 Meshy、一次付费提交、单次上限和本批工作总额上限。
已有明确授权时直接在授权范围内执行，不重复询问。

`--estimated-credits` 与 `--max-credits` 只做**本地提交前检查**，
不是服务端硬额度限制，也不能保证最终扣费。
`--pricing-source` 记录估算依据，脚本不会自行验证价格。
多任务时，总控 agent 在每次获授权的 POST 前核对已花费额度、待完成任务预计费用和剩余总额。

通过进程环境提供 `MESHY_API_KEY`；不要写入提示词、命令参数、共享文件或日志。
客户端的 plan/state 不保存 API key 或模型 base64 载荷，也不保存原始 API 错误正文。

## 使用命令

在 skill 目录运行。把路径和额度变量替换为已审核输入及当前估算；
以下变量不是价格报价。

```sh
python3 scripts/meshy_retexture.py plan \
  --model /path/to/appearance-input.glb \
  --text-style-prompt 'Brushed stainless steel; plain surfaces; no text or scale marks' \
  --enable-pbr --enable-original-uv --texture-resolution 2k \
  --estimated-credits "$ESTIMATED_CREDITS" --max-credits "$AUTHORIZED_CREDITS" \
  --pricing-source https://docs.meshy.ai/en/api/pricing \
  --output /path/to/new-plan.json
```

确切请求及上传、付费范围已获授权后执行：

```sh
python3 scripts/meshy_retexture.py submit \
  --model /path/to/appearance-input.glb \
  --text-style-prompt 'Brushed stainless steel; plain surfaces; no text or scale marks' \
  --enable-pbr --enable-original-uv --texture-resolution 2k \
  --estimated-credits "$ESTIMATED_CREDITS" --max-credits "$AUTHORIZED_CREDITS" \
  --pricing-source https://docs.meshy.ai/en/api/pricing \
  --state /path/to/new-task.json --allow-upload --execute

python3 scripts/meshy_retexture.py status --state /path/to/new-task.json
python3 scripts/meshy_retexture.py download \
  --state /path/to/new-task.json --output /path/to/new-download-directory
```

图片样式可替换为：`--image-style-url https://example.org/reference.png`。
多视图可替换为：`--multiview-image-urls https://example.org/front.png
https://example.org/back.png --ai-model meshy-7`，实际放在同一条命令中。

## 续查与未知提交恢复

POST 前独占创建 state 并刷盘。只要 state 已存在，`submit` 就拒绝再次提交，
即使前次网络失败或进程中断。工具不自动重试 POST，也不持续轮询。

结果未知时保留原 state，在 Meshy 账号中查找任务。
取得可确认的 ID 后，用 `status --state ... --task-id UUID` 恢复查询。
不要删除回执或改用新 state 路径重试同一次未知提交。
GET 失败会保留原回执；仍无法确认 ID 时，先报告不确定性并核对账号，再考虑下一次付费。

下载仅接受 `assets.meshy.ai` 或 `cdn.meshy.ai` 的 HTTPS URL，拒绝跳转，
不向下载主机发送 API bearer token。文件采用固定本地名称并记录字节数和 SHA-256；
失败时保留部分文件，不解压、不执行。
若 Meshy 更换下载主机，先核对官方响应，再修改允许列表。
state 中的签名 URL 可能过期，可通过 `status` 刷新；state 属于私有记录，不作为公开资产清单。
OBJ/MTL 下载后还需审核并重绑文件引用，优先采用 GLB。

## 透明聚合物的调参边界

材料标为 PP 或“医疗级”不能唯一确定透射、散射或粗糙度；缺少牌号、试片厚度和光学测量时，这些是外观拟合参数。按照片调材质时冻结几何、壁厚、光照和机位，记录前后参数及 USD/网页消费者各自的表现。不要通过减薄壁厚补偿不透明材质，也不要把透明薄壁误删成镂空。调整参数不能提升材料身份或接触资格。

## 结果验证与资产绑定

把源摘要、plan/request 哈希、固定 AI 模型、task ID、选项、下载摘要及可取得的实际
consumed credits 纳入资产证据。下载完成只标记为 `downloaded_unqualified`，不等于验收通过。

对照冻结源检查返回顶点、拓扑、变换和材质绑定。
对应关系可靠时，将外观转移或烘焙回原始几何，生成模型另存；
对应关系不可靠时保留视觉候选，并记录尚未解决的映射。
不能因为启用了 UV 选项就用生成几何替换 CAD、碰撞、关节、惯量或操作几何。

渲染相关正面、斜视和局部近景，检查透明度、文字、接缝、逐面材质及工具/接触区域。
搬移后重新打开交付包，在目标消费者中解析全部图像和材质依赖。
只授予实际检查过的外观能力，并写明消费者和来源版本。
