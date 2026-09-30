"""Exercise restart safety without touching the real service or launchd."""
import subprocess
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
