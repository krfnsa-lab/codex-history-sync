"""Create a local profile without initializing or synchronizing chat history."""
import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import sys

from history_sync import (SOFTWARE_VERSION, atomic_json, decode_output,
                          normalize_config, require_local_drive, resolve_runtime, validate_config)

SCRIPT_DIR = Path(__file__).resolve().parent


def distribution_names(running=False):
    command = ['wsl.exe', '--list']
    if running:
        command.append('--running')
    command.append('--quiet')
    result = subprocess.run(command, capture_output=True, timeout=20)
    if result.returncode:
        raise RuntimeError('无法读取 WSL 发行版列表，请检查 WSL 安装')
    return [line.replace('\x00', '').strip() for line in decode_output(result.stdout).splitlines()
            if line.replace('\x00', '').strip()]


def probe_wsl(distro, user=None, interpreter='/usr/bin/python3'):
    """Only probe an already running distro; never launch a stopped one."""
    if distro not in distribution_names(running=True):
        raise RuntimeError('所选 WSL 未运行；请先自行启动它，再运行配置向导')
    command = ['wsl.exe', '--distribution', distro]
    if user:
        command += ['--user', user]
    code = (
        'import json,os,pathlib,pwd,sys; '
        'h=pathlib.Path.home(); '
        'c=pathlib.Path(os.environ.get("CODEX_HOME",str(h/".codex"))).expanduser(); '
        'print(json.dumps({"user":pwd.getpwuid(os.getuid()).pw_name,'
        '"home":str(h),"codex_home":str(c),"python":sys.executable,'
        '"version":list(sys.version_info[:3]),"has_state_db":(c/"state_5.sqlite").is_file()}))'
    )
    command += ['--exec', interpreter, '-c', code]
    result = subprocess.run(command, capture_output=True, timeout=30)
    if result.returncode:
        raise RuntimeError('无法使用所选 WSL 用户或 Python；请检查用户和解释器路径')
    try:
        value = json.loads(decode_output(result.stdout))
        if not isinstance(value, dict) or any(not isinstance(value.get(key), str) or not value[key]
                for key in ('user', 'home', 'codex_home', 'python')) or not isinstance(value.get('version'), list) or len(value['version']) < 2 or any(
                isinstance(part, bool) or not isinstance(part, int) for part in value['version']):
            raise ValueError('invalid native response')
        if tuple(value['version'][:2]) < (3, 11):
            raise RuntimeError('WSL 需要 Python 3.11 或更新版本')
        if not value['user'] or not value['codex_home'].startswith('/'):
            raise ValueError('invalid native home')
        return value
    except (ValueError, KeyError, TypeError):
        raise RuntimeError('WSL 返回的环境信息无效') from None


def probe_windows_python(interpreter):
    command = [str(interpreter), '-c', 'import json,platform,sys;print(json.dumps({"path":sys.executable,"version":list(sys.version_info[:3]),"implementation":platform.python_implementation()}))']
    result = subprocess.run(command, capture_output=True, timeout=20)
    if result.returncode:
        raise RuntimeError('Windows Python 无法运行')
    try:
        value = json.loads(decode_output(result.stdout))
        if value['implementation'] != 'CPython' or tuple(value['version'][:2]) < (3, 11):
            raise RuntimeError('Windows 需要 CPython 3.11 或更新版本')
        return str(Path(value['path']).resolve())
    except (ValueError, KeyError, TypeError):
        raise RuntimeError('Windows Python 返回的环境信息无效') from None


def choose_distro(names, requested, interactive, input_fn=input):
    if requested:
        if requested not in names:
            raise ValueError('指定的 WSL 发行版不存在，请使用列表中的名称')
        return requested
    if not names:
        raise RuntimeError('没有发现 WSL 发行版，请先安装并启动所需发行版')
    if len(names) == 1:
        return names[0]
    if not interactive:
        raise ValueError('发现多个 WSL 发行版，请通过 --distro 明确指定')
    print('请选择 WSL 发行版：')
    for index, name in enumerate(names, 1):
        print(f'  {index}. {name}')
    answer = input_fn('编号：').strip()
    if not answer.isdigit() or not 1 <= int(answer) <= len(names):
        raise ValueError('发行版编号无效')
    return names[int(answer) - 1]


def prompt_value(label, supplied, default, interactive, input_fn=input):
    if supplied is not None:
        return supplied
    if not interactive:
        return default
    answer = input_fn(f'{label} [{default}]：').strip()
    return answer or default


def positive_integer(value, label):
    try:
        number = int(value)
    except (ValueError, TypeError):
        raise ValueError(f'{label} 须为正整数') from None
    if isinstance(value, bool) or str(number) != str(value).strip() or number <= 0:
        raise ValueError(f'{label} 须为正整数')
    return number


def write_profile(path, config, force=False):
    """Persist configuration only; existing ledger identities cannot be reset."""
    validate_config(config)
    old_runtime, old_normal = None, None
    identity = ('windows_home', 'wsl_distro', 'wsl_user', 'wsl_home')
    if path.exists():
        if not force:
            raise ValueError('配置已存在；请编辑原配置，或明确使用 --force 重新配置')
        old = json.loads(path.read_text(encoding='utf-8-sig'))
        validate_config(old)
        old_runtime = resolve_runtime(old, path)
        old_normal = normalize_config(old, path)
        if (old_runtime / 'pending.json').exists():
            raise ValueError('该配置存在未完成导入，暂不允许重新配置')
        if (old_runtime / 'ledger.json').exists():
            new_normal = normalize_config(config, path)
            if any(old_normal[key] != new_normal[key] for key in identity) or old_runtime != resolve_runtime(config, path):
                raise ValueError('已有基线的配置不能更换记录目录或运行目录；请创建独立配置')
    runtime = resolve_runtime(config, path)
    if (runtime / 'pending.json').exists() or (runtime / 'ledger.json').exists():
        new_normal = normalize_config(config, path)
        if old_runtime != runtime or old_normal is None or any(old_normal[key] != new_normal[key] for key in identity):
            raise ValueError('所选运行目录已有基线或未完成导入，请使用新的独立运行目录')
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(path, config)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=SCRIPT_DIR / 'config.json')
    parser.add_argument('--non-interactive', action='store_true')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--distro')
    parser.add_argument('--wsl-user')
    parser.add_argument('--windows-home')
    parser.add_argument('--wsl-home')
    parser.add_argument('--python-windows')
    parser.add_argument('--python-wsl', default='/usr/bin/python3')
    parser.add_argument('--runtime-directory')
    parser.add_argument('--task-name')
    parser.add_argument('--interval-minutes')
    parser.add_argument('--quiet-seconds')
    parser.add_argument('--max-transfers-per-run')
    parser.add_argument('--run-budget-seconds')
    parser.add_argument('--operation-timeout-seconds')
    parser.add_argument('--task-time-limit-minutes')
    return parser


def main(argv=None):
    if sys.stdout is not None and hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    args = build_parser().parse_args(argv)
    try:
        if os.name != 'nt':
            raise RuntimeError('请在 Windows 中运行配置向导')
        if sys.version_info < (3, 11):
            raise RuntimeError('请使用 Python 3.11 或更新版本')
        require_local_drive(SCRIPT_DIR, '程序目录')
        config_path = args.config.expanduser().resolve()
        if config_path.exists() and not args.force:
            raise ValueError('配置已存在；请编辑它，或在确认后使用 --force')
        interactive = not args.non_interactive
        distro = choose_distro(distribution_names(), args.distro, interactive)
        native = probe_wsl(distro, args.wsl_user, args.python_wsl)
        interpreter = probe_windows_python(args.python_windows or sys.executable)
        config = {'windows_home': str(prompt_value('Windows Codex 目录', args.windows_home,
                    os.environ.get('CODEX_HOME', str(Path.home() / '.codex')), interactive)),
                  'wsl_distro': distro, 'wsl_user': native['user'],
                  'wsl_home': str(prompt_value('WSL Codex 目录', args.wsl_home, native['codex_home'], interactive)),
                  'python_windows': interpreter, 'python_wsl': native['python']}
        config['task_name'] = str(prompt_value('计划任务名称', args.task_name, 'CodexHistorySync-Public', interactive))
        config['runtime_directory'] = str(prompt_value('运行数据目录（相对配置文件）', args.runtime_directory,
                                                      'runtime/' + config['task_name'], interactive))
        for key, label, default in (('interval_minutes', '检查间隔（分钟）', 5),
                ('quiet_seconds', '聊天静默时间（秒）', 60),
                ('max_transfers_per_run', '每轮最多尝试条数', 10),
                ('run_budget_seconds', '每轮软预算（秒）', 120),
                ('operation_timeout_seconds', '端点操作超时（秒）', 120)):
            config[key] = positive_integer(prompt_value(label, getattr(args, key), default, interactive), label)
        minimum = math.ceil((config['run_budget_seconds'] + 3 * config['operation_timeout_seconds'] + 60) / 60)
        config['task_time_limit_minutes'] = positive_integer(prompt_value('任务硬上限（分钟）',
            args.task_time_limit_minutes, max(10, minimum), interactive), '任务硬上限')
        config = normalize_config(config, config_path)
        write_profile(config_path, config, args.force)
        print(f'配置已生成（版本 {SOFTWARE_VERSION}）：{config_path}')
        print('未初始化基线或安装任务。下一步：doctor.ps1 → sync-now.ps1 -Initialize → sync-now.ps1 -DryRun')
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, EOFError) as error:
        print('配置未完成：' + str(error))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
