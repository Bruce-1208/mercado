"""Exercise restart safety without touching the real service or launchd."""
import subprocess
import os
import sys
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'restart_workbench.sh'


def run_shell(body):
    return subprocess.run(['bash', '-c', 'source "$1"\n' + body, 'test', str(SCRIPT)],
                          capture_output=True, text=True)


def test_script_syntax():
    subprocess.run(['bash', '-n', str(SCRIPT)], check=True)


def test_both_launchers_recognized_only_in_this_checkout():
    result = run_shell('''
ps() { printf '%s\\n' "$mock_command"; }
lsof() { printf 'n%s\\n' "$mock_cwd"; }
mock_cwd="$PROJECT_ROOT"
for mock_command in 'python -m bit.bit_interface --role server' 'python -m bit.workbench_services' 'python -m bit.workbench_services --child'; do
    is_workbench_process 123 || exit 10
done
mock_cwd=/some/other/checkout
if is_workbench_process 123; then exit 11; fi
mock_cwd="$PROJECT_ROOT"
mock_command='python -m bit.bit_interface_other'
if is_workbench_process 123; then exit 12; fi
mock_command='python -m bit.durable_tasks --run /persistent/tasks appeal-job'
if is_workbench_process 123; then exit 13; fi
''')
    assert result.returncode == 0, result.stderr


def test_listener_must_belong_to_new_supervisor():
    result = run_shell('''
listener_pids() { echo 101; }
ps() { echo 100; }
is_workbench_process() { return 0; }
[[ "$(child_listener 100 5000)" == 101 ]] || exit 10
if child_listener 999 5000; then exit 11; fi
is_workbench_process() { return 1; }
if child_listener 100 5000; then exit 12; fi
''')
    assert result.returncode == 0, result.stderr


def test_zombie_does_not_block_shutdown():
    result = run_shell('''
ps() { echo Z; }
if process_alive 123; then exit 10; fi
ps() { echo S; }
process_alive 123
''')
    assert result.returncode == 0, result.stderr


def test_existing_http_response_cannot_fake_success():
    result = run_shell('''
launch_target=gui/123/test
launch_plist=/test.plist
launchctl() { [[ "$1" != print ]] || echo 'pid = 999'; }
process_alive() { return 0; }
listener_pids() { echo 101; }
ps() { echo 100; }
is_workbench_process() { return 0; }
curl() { echo 'ERROR: HTTP must not be checked for an unrelated listener'; return 0; }
sleep() { elapsed=999; }
stop_service() { echo cleaned; }
tail() { :; }
start_service python
''')
    assert result.returncode != 0
    assert '[成功]' not in result.stdout
    assert 'ERROR:' not in result.stdout
    assert 'cleaned' in result.stdout


def test_launchd_unloaded_before_stopping_processes():
    result = run_shell('''
launch_target=gui/123/test
workbench_pids() { echo 101; }
launchctl() { echo "launchctl $*"; }
ps() { echo 'python -m bit.workbench_services'; }
kill() { echo "kill $*"; }
wait_for_exit() { return 0; }
is_workbench_process() { return 0; }
rm() { :; }
listener_pids() { :; }
stop_service
''')
    assert result.returncode == 0, result.stderr
    assert result.stdout.index('launchctl bootout') < result.stdout.index('kill -TERM')


def test_disabled_launchd_job_enabled_before_bootstrap():
    result = run_shell('''
launch_target=gui/123/test
launch_plist=/test.plist
enabled=no
launchctl() {
    case "$1" in
        enable) [[ "$2" == "$launch_target" ]] || exit 10; enabled=yes ;;
        bootstrap)
            [[ "$enabled" == yes && "$2" == gui/123 && "$3" == /test.plist ]] || exit 11
            echo bootstrapped ;;
        kickstart) echo started ;;
        print) return 1 ;;
    esac
}
sleep() { elapsed=999; }
stop_service() { :; }
tail() { :; }
start_service python
''')
    assert 'bootstrapped' in result.stdout, result.stderr
    assert 'started' in result.stdout, result.stderr


def test_enable_failure_does_not_attempt_bootstrap():
    result = run_shell('''
launch_target=gui/123/test
launch_plist=/test.plist
launchctl() {
    [[ "$1" != enable ]] || return 1
    echo "unexpected $*"
}
start_service python
''')
    assert result.returncode != 0
    assert '无法启用 launchd 服务' in result.stderr
    assert 'unexpected' not in result.stdout


def test_denied_process_access_fails_with_actionable_message():
    result = run_shell('''
ps() { return 1; }
ensure_process_access
echo unexpected
''')
    assert result.returncode != 0
    assert 'ps 被权限或沙箱限制' in result.stderr
    assert 'unexpected' not in result.stdout


def test_residual_project_listener_is_stopped():
    result = run_shell('''
alive=yes
listener_pids() { [[ "$alive" != yes ]] || echo 101; }
is_workbench_process() { [[ "$1" == 101 ]]; }
kill() { echo "kill $*"; alive=no; }
wait_for_exit() { return 0; }
wait_for_port_release 5000
''')
    assert result.returncode == 0, result.stderr
    assert 'kill -TERM 101' in result.stdout


def test_unrelated_listener_is_diagnosed_and_never_killed():
    result = run_shell('''
listener_pids() { echo 101; }
is_workbench_process() { return 1; }
kill() { echo UNEXPECTED_KILL; }
sleep() { elapsed=999; }
ps() { echo '101 1 unrelated-server'; }
lsof() { echo '/other/project'; }
wait_for_port_release 5000
''')
    assert result.returncode != 0
    assert 'UNEXPECTED_KILL' not in result.stdout
    assert '101 1 unrelated-server' in result.stderr
    assert '/other/project' in result.stderr
    assert '端口 5000 仍被占用' in result.stderr


def locked_command(root, code):
    return subprocess.Popen(
        ['bash', '-c', 'source "$1"; with_restart_lock "$2" "$2" -c "$3"',
         'test', str(SCRIPT), sys.executable, code],
        env={**os.environ, 'WORKBENCH_PROJECT_ROOT': str(root)},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def test_restart_lock_rejects_concurrency_and_recovers_after_kill(tmp_path):
    ready = tmp_path / 'ready'
    holder = locked_command(tmp_path,
                            f'from pathlib import Path; import time; '
                            f'Path({str(ready)!r}).touch(); time.sleep(30)')
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            assert holder.poll() is None
            time.sleep(0.02)
        assert ready.exists()
        contender = locked_command(tmp_path, 'print("unexpected")')
        stdout, stderr = contender.communicate(timeout=5)
        assert contender.returncode != 0
        assert '已有重启正在执行' in stderr
        assert 'unexpected' not in stdout
    finally:
        holder.kill()
        holder.communicate(timeout=5)
    recovered = locked_command(tmp_path, 'print("recovered")')
    stdout, stderr = recovered.communicate(timeout=5)
    assert recovered.returncode == 0, stderr
    assert 'recovered' in stdout


def test_legacy_empty_lock_does_not_block_and_normal_exit_releases(tmp_path):
    (tmp_path / '.run' / 'restart.lock').mkdir(parents=True)
    for _ in range(2):
        process = locked_command(tmp_path, 'print("started")')
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0, stderr
        assert 'started' in stdout


def test_system_managed_restart_waits_without_bootstrap_or_bootout(tmp_path, monkeypatch):
    (tmp_path / ".run").mkdir()
    monkeypatch.setenv("WORKBENCH_PROJECT_ROOT", str(tmp_path))
    result = run_shell('''
system_managed=1
launch_target=system/com.zeshun.mercado-workbench
launchctl() {
    [[ "$1" == print ]] || { echo 'ERROR: must leave system job installed'; return 1; }
    echo 'pid = 999'
}
process_alive() { return 0; }
child_listener() { echo 101; }
isolated_children_ready() { return 0; }
curl() { return 0; }
start_service python
''')
    assert result.returncode == 0, result.stderr
    assert 'ERROR:' not in result.stdout
    assert '[成功]' in result.stdout


def test_all_isolated_listeners_are_required():
    result = run_shell('''
child_listener() { [[ "$2" != "$MAINTENANCE_PORT" ]] && echo 101; }
if isolated_children_ready 100; then exit 10; fi
child_listener() { echo 101; }
isolated_children_ready 100
''')
    assert result.returncode == 0, result.stderr


def test_launchd_status_temporarily_unavailable_is_retried(tmp_path, monkeypatch):
    (tmp_path / ".run").mkdir()
    monkeypatch.setenv("WORKBENCH_PROJECT_ROOT", str(tmp_path))
    result = run_shell('''
system_managed=1
launch_target=system/com.zeshun.mercado-workbench
launchctl() {
    [[ "$1" == print ]] || return 1
    if [[ ! -f "$RUN_DIR/polled" ]]; then
        touch "$RUN_DIR/polled"
        return 1
    fi
    echo 'pid = 999'
}
process_alive() { return 0; }
child_listener() { echo 101; }
isolated_children_ready() { return 0; }
curl() { return 0; }
sleep() { :; }
start_service python
''')
    assert result.returncode == 0, result.stderr
    assert '[成功]' in result.stdout
    assert (tmp_path / '.run/mercado-workbench.pid').read_text().strip() == '999'


def test_launchd_status_failure_reaches_timeout_diagnostic():
    result = run_shell('''
system_managed=1
launch_target=system/com.zeshun.mercado-workbench
launchctl() { return 1; }
sleep() { elapsed=999; }
stop_service() { echo UNEXPECTED_STOP; }
tail() { :; }
start_service python
''')
    assert result.returncode != 0
    assert '未在' in result.stderr
    assert 'UNEXPECTED_STOP' not in result.stdout
    assert '[成功]' not in result.stdout
