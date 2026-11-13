测试：python3 -m unittest discover -s tests/station_assurance -p 'test_*.py'
构建：python3 -m compileall -q station_mesh_guard tests/station_assurance
验收：python3 -m station_mesh_guard.operations.cli fixtures/demo.json --database /tmp/station_mesh_guard-acceptance.sqlite3
