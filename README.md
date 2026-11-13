# 参考站切换下的传感器校准收益验证

项目使用 Python 3.11 标准库与 SQLite，现有逐案处理入口为 `station_mesh_guard.operations.cli`。保留原有处理器、持久化与测试；分析业务使用本地合成资料，不连接生产系统。无需 pip 安装、下载模型或启动额外服务。

## 现有程序

在仓库根目录运行 `bash scripts/demo.sh`，依次完成现有产品回归、分析输入核对、语法构建和真实业务入口调用。所有运行文件位于临时目录，结束后清理；运行失败立即退出，不使用预存输出代替执行。

单独测试：`python3 -m unittest discover -s tests/station_assurance -p 'test_*.py' -v`。

输入检查：`python3 -m unittest discover -s tests -p test_analysis_inputs.py -v`。

构建检查：`python3 -m compileall -q station_mesh_guard tests`。

业务调用：`python3 -m station_mesh_guard.operations.cli fixtures/demo.json --database /tmp/station_mesh_guard-demo.sqlite3`。

## 分析资料与联调

`fixtures/analysis/input.json` 保存业务演练数据，字段解释在 `fixtures/analysis/fields.md`。以下已核对的数据形态用于分析回归：reference_switch、future_quality、reference_uncertainty。现有输入测试检查这些形态是否真实存在，不代替分析方法或数值正确性的验证。

执行 `bash scripts/feature_demo.sh`：先重跑现有产品、分析输入核对与校准研究回归，再从干净数据库计算分析、同输入重放、核对输出结构并打印完整 JSON。分析 ID 为 `study_id`，其余输出包括 `pairings, candidates, validation, uncertainty, applicability`。方法正确性由 `tests/test_calibration_study.py` 覆盖业务说明中的反例，结构核验不能代替统计验证。

### 校准研究方法

入口：`python3 -m station_mesh_guard.calibration_study --input <sensors_json> --database <station_db> --output <calibration_json>`。

- **候选方法**：`none`（未校准基线）、`mean_offset`（固定均值偏移）、`median_offset`（中位偏移，抗极端值）、`gain_ratio`（乘性增益）。所有参数只用维修完成前的配对观测拟合；维修后观测只做验证，不回流训练。
- **系统偏差与随机误差分开**：`validation[].raw/corrected` 分别给出 bias、std、MAE、RMSE、最大绝对误差与 P90；`improvement` 是逐事件绝对误差改善的事件聚类 bootstrap 区间，不把同一 `event_id` 的多次观测当独立样本。
- **事件聚类**：有效样本按 `rain event` 计数（`uncertainty[].effective_sample_units`、`applicability.batches[].effective_post_events`）。同一设备的连续事件用于训练与验证时按时间窗隔离。
- **可用性门槛**：至少两个维修后事件、至少两个训练事件、参考误差界已知；不满足时标记 `insufficient_evidence`，不硬凑配对。
- **参考仪误差**：`reference_error` 视为对称均匀误差界；`resolution_floor` 给出参考噪声决定的可检测下限，`reference_limited` 标记参考误差主导的结论。
- **可比性**：`naive_before_after` 与 `event_intensity` 同时给出朴素前后均值差及其归因。观测均值下降但配对残差不变时记为 `intensity_confound`，不当作维修收益。
- **晚到质控**：`quality_revisions.available_at` 晚于 `as_of` 的追认不参与本次研究；只有截止时已生效的质量状态才进入可用集。
- **参考停机**：停机区间内的观测不配对、不插补，`applicability.reference_outage_gaps` 明确记为 `evidence: none`。
- **证据版本**：`evidence.calibration.version` 与 `evidence.maintenance.version` 分别由观测/配对/质控修订/停机摘要与维修登记摘要生成，并记录 `source_versions`；接管 `--database` 时同一 `study_id` 命中幂等缓存，重放输出一致。

### 结论边界

- 能支持：在维修后时段，用维修前事件拟合的固定加性偏移可降低支持设备的配对绝对误差，且改善区间按事件聚类。
- 不能支持：不能把前后差异判为维修收益（维修、参考站切换与事件强度在同一时间边界附近同时变化），也不能认证乘性或强度相关校准；参考停机段和维修后事件不足的设备没有证据。
- 维修集中在问题设备或记录覆盖全部设备同一时点时，`applicability.maintenance_concentration` 会说明组间比较范围，不能外推到全网。
- 研究仅做离线统计，不接入真实预警，也不修改在线阈值。

## 离线环境

使用 Python 3.11.2 的 Linux 环境即可复现。容器环境预置解释器后，以只读方式挂载仓库，将文件复制到容器临时目录再运行脚本；运行时关闭网络。基础测试和小规模本地数据适合在 90 秒内展示，正式展示命令为 `bash scripts/feature_demo.sh`。不要在展示脚本里安装依赖或等待外部服务。
