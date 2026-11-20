# 生产控制与证据合同

## 运行状态

每次运行必须有唯一 \`run_id\`，并且只允许按以下状态前进：

\`\`\`text
DISCOVERED
→ INDEXED
→ TRANSCRIBED
→ FACTS_READY
→ CHARACTER_GATE
→ ASSET_GATE
→ SCRIPT_GATE
→ PACKAGE_STAGED
→ VERIFIED
→ CURRENT
\`\`\`

失败状态必须保留：

\`\`\`text
SOURCE_INVALID
DUPLICATE_EPISODE
ASR_FAILED
ANALYSIS_FAILED
IDENTITY_CONFLICT
ASSET_QUALITY_FAILED
SCRIPT_SCHEMA_FAILED
PACKAGE_INCOMPLETE
VERIFY_FAILED
\`\`\`

失败状态不能被“文件存在”覆盖，也不能被自动改写成“无合格素材”。

## 输入门禁

正式输出路径必须通过 `scripts/path_guard.py`：它可以是用户在当前任务明确提供的任意源目录内子目录；未提供时才使用源目录下的 `二次创作发行/`。正式人物图、场景图和脚本不得写到源目录之外；临时预览只能写入源目录 `.short-drama-cache/` 或系统临时目录，不能被生成清单引用。

正式分集选择器必须：

- 默认只扫描源目录直属视频；
- 子目录只有在显式参数开启后才扫描；
- 每个文件必须能解析唯一集数；
- 重复集数直接阻断；
- 无法读取、无视频流、元数据不完整的视频进入失败清单；
- 缺集、额外集和测试片段不得静默混入正式发行；
- 符号链接默认排除；
- 源目录、缓存目录和输出目录必须明确分开。

输入清单至少写入：

\`\`\`text
source
scan_mode
episode_key
relative_path
size
mtime_ns
sha256
duration
width
height
fps
readable
status
errors
\`\`\`

## 依赖指纹

每个 artifact 都必须保存：

\`\`\`text
artifact_id
run_id
episode_key
source_sha256
upstream_artifact_ids
skill_version
image_rule_version
script_template_version
analysis_model_version
prompt_version
generated_at
status
\`\`\`

脚本模板变更只重生成脚本及其下游包；图像规则变更只重做候选、资产和引用包；源视频变更只重跑受影响集及依赖闭包。未变化集必须复用已验证结果。

运行开始前必须先执行 `plan_run.py`。`noop` 表示所有依赖指纹与已验证输出一致，禁止进入模型阶段；`incremental` 必须列出变化集和依赖闭包；`full` 只允许在没有可用已验证输出或证据缺失时使用。计划结果进入 `run_manifest`，用于解释本次为何没有或只做了少量工作。

正式包还必须保存 `delivery_fingerprint`。快速路径复算该指纹；缺少、变化或包文件缺失时，状态只能是 `verify_required` 或 `rebuild_required`，不能把旧图片或旧脚本当作已验证结果。

## 人物身份门禁

人物状态只允许按证据转换：

\`\`\`text
候选 → 待核验 → 已确认
候选 → 待核验 → 无合格单人物图
\`\`\`

\`待核验\` 不得自动降级为 \`无合格单人物图\`。后者必须有逐张排除证据：

- asset_id；
- 集数和时间；
- 排除原因；
- 清晰度结果；
- 是否多人/露边；
- 审核人；
- 审核时间。

已确认人物必须有：

- 身份依据；
- 单人物清晰图；
- 文件名绑定；
- 审核记录；
- 源视频时间点；
- 源 SHA256。

## 图片审核证据

每张正式人物图和场景图都要有独立审核记录：

\`\`\`text
asset_id
asset_type
source_video
source_sha256
episode_key
timecode
output_file
decision
reviewer
reviewed_at
reason
\`\`\`

人物图必须通过单人物、脸部清晰、动作完整和身份一致检查。场景图必须通过地点边界、关键道具、空间关系和无人物干扰检查。缺少审核记录直接阻断。

## 脚本结构门禁

脚本先生成结构化 JSON，再渲染 Markdown。每个镜头必须有：

- 起止时间；
- 景别、焦段或机位；
- 出镜人物；
- 人物图片；
- 场景图片或明确无合格场景图；
- 站位和移动轨迹；
- 手部动作；
- 面部和神态变化；
- 台词、字幕、旁白；
- BGM、环境声和动作音；
- 入帧和尾帧；
- 下一镜头承接；
- 禁止项。

每个 30 秒脚本必须满足：

- 8—12 个镜头；
- 0.0—30.0 秒连续覆盖；
- 15.0 秒有前后段承接；
- 至少 3 次立场或权力变化；
- 至少 2 次信息或情绪转折；
- 至少 1 个动作/道具/口误/表情证据；
- 至少 1 个动作或关系后果；
- 至少 1 个讨论问题和未解决尾帧问题。

## 发行切换

发行包必须先写入本次 \`stage\`，并执行完整验收：

\`\`\`text
写入 stage
→ 校验包集合
→ 校验注册表
→ 校验人物/场景审核证据
→ 校验脚本结构和图片引用
→ 校验源视频指纹
→ 写入 run_manifest
→ 原子切换 current
\`\`\`

验收失败时：

- 保留 stage；
- 写入失败状态和原因；
- 保留上一版 current；
- 支持按集或按依赖闭包恢复；
- 不删除上一次成功结果。

## 运行审计

每次运行必须记录：

- 成功集；
- 失败集；
- 排除视频；
- 重跑集；
- 重跑原因；
- 受影响人物；
- 受影响资产；
- 受影响发行包；
- 每阶段耗时；
- 模型和规则版本；
- 最终验收结果；
- 输出文件清单和哈希。

普通短剧运行只写本地磁盘。GitHub 只用于用户明确要求的 Skill 修改发布，不能上传源视频、截图、缓存、注册表或运行凭证。
