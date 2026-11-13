# 业务演练数据口径

value/reference_value 为合成同单位观测；reference_error 为参考仪绝对误差界。pairings、outages 时段均左闭右开。quality_revisions 在可用时间之前不得用于训练或验证；rain event_id 用于表示连续过程相关性。

各 ID 均为本地合成标识，不包含真实人员或生产数据。时刻使用带时区 ISO 8601；as_of 是分析可知信息的截止时刻，不是业务发生时间上界。晚到事实允许保留在输入中，但只能在 available_at 到达之后采用。source_versions 标识输入版本；baseline_case_id 关联同库既有演示运行。空值表示未知，不应未经说明转成零。
