set -eu
workspace=$(mktemp -d)
trap 'rm -rf "$workspace"' EXIT
export PYTHONDONTWRITEBYTECODE=1
python3 -m unittest discover -s tests/station_assurance -p 'test_*.py' -q
python3 -m unittest discover -s tests -p 'test_analysis_inputs.py' -v
python3 -m unittest discover -s tests -p 'test_calibration_study.py' -q
PYTHONPYCACHEPREFIX="$workspace/cache" python3 -m compileall -q station_mesh_guard tests
python3 -m station_mesh_guard.operations.cli fixtures/demo.json --database "$workspace/operations.sqlite3" > "$workspace/operations.json"
python3 -m json.tool "$workspace/operations.json"
