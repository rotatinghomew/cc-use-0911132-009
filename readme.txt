本项目在本地处理业务记录，执行分阶段规则并保存运行状态、原始证据和结果。测试命令验证现有产品的正常与异常路径，构建命令检查源码语法，业务入口使用本地合成资料。运行不需要在线服务，详细字段说明与分析联调方式见项目根目录中文文档。

测试：python3 -m unittest discover -s tests/station_assurance -p 'test_*.py'
分析测试：python3 -m unittest discover -s tests -p 'test_calibration_study.py'
构建：python3 -m compileall -q station_mesh_guard tests
验收：python3 -m station_mesh_guard.operations.cli fixtures/demo.json --database /tmp/station_mesh_guard-acceptance.sqlite3
