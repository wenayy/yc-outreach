#!/usr/bin/env python3
"""Manage the local outreach server as a macOS login service."""
import argparse
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
LABEL = 'com.ycoutreach.local'
PLIST = Path.home() / 'Library' / 'LaunchAgents' / (LABEL + '.plist')
DOMAIN = f'gui/{os.getuid()}'
RUNTIME = Path.home() / 'Library' / 'Application Support' / 'YCOutreach'


def run(*args):
    return subprocess.run(['launchctl', *args], capture_output=True, text=True)


def stage_runtime():
    # launchd cannot reliably access Desktop/Documents under macOS privacy controls.
    RUNTIME.mkdir(parents=True, exist_ok=True)
    RUNTIME.chmod(0o700)
    for relative in ('serve.py', 'outreach.py', 'send_schedule.py', 'catalog_jobs.py', 'contact_emails.py', 'public_contacts.py', 'mailbox_checks.py', 'index.html', 'api/yc.py', 'data/yc_companies.json', 'vendor/tabulator/tabulator.min.js', 'vendor/tabulator/tabulator.min.css'):
        destination = RUNTIME / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(HERE / relative, destination)
    if (HERE / '.env').exists():
        shutil.copy2(HERE / '.env', RUNTIME / '.env')
        (RUNTIME / '.env').chmod(0o600)
    data = RUNTIME / '.outreach'
    data.mkdir(exist_ok=True)
    data.chmod(0o700)
    previous = HERE / '.outreach' / 'queue.sqlite3'
    destination = data / 'queue.sqlite3'
    if previous.exists() and not destination.exists():
        source_db = sqlite3.connect(previous)
        target_db = sqlite3.connect(destination)
        try:
            source_db.backup(target_db)
        finally:
            target_db.close()
            source_db.close()
        destination.chmod(0o600)
    # Manual runs and the service share the same queue after installation.
    (HERE / '.outreach').mkdir(exist_ok=True)
    (HERE / '.outreach' / 'service-data-path').write_text(str(data))
    for name in ('service.log', 'service-error.log'):
        (data / name).touch(exist_ok=True)
        (data / name).chmod(0o600)
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'restart', 'stop', 'status'])
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('This background-service helper is for macOS. Elsewhere, run serve.py with your service manager.')
    target = DOMAIN + '/' + LABEL
    if args.action == 'status':
        result = run('print', target)
        if result.returncode:
            print('Background service is stopped or not installed.')
            return
        for line in result.stdout.splitlines():
            if any(key in line for key in ('state =', 'pid =', 'last exit code =')):
                print(line.strip())
        return
    if args.action == 'stop':
        result = run('bootout', target)
        if result.returncode:
            sys.exit('Service could not be stopped (it may already be stopped).')
        print('Background service stopped. Run install to start it again.')
        return
    if args.action == 'restart':
        stage_runtime()
        result = run('kickstart', '-k', target)
        if result.returncode:
            sys.exit('Service is not loaded. Run: python3 background_service.py install')
        print('Background service restarted; the sending queue starts paused.')
        return
    # Stop our existing service before replacing its runtime.
    if run('print', target).returncode == 0:
        result = run('bootout', target)
        if result.returncode:
            sys.exit('Could not stop the existing background service.')
    logs = stage_runtime()
    config = {
        'Label': LABEL,
        'ProgramArguments': [sys.executable, '-u', str(RUNTIME / 'serve.py')],
        'WorkingDirectory': str(RUNTIME),
        'EnvironmentVariables': {'OUTREACH_DATA_DIR': str(logs)},
        'RunAtLoad': True,
        'KeepAlive': True,
        'ThrottleInterval': 30,
        'ProcessType': 'Background',
        'StandardOutPath': str(logs / 'service.log'),
        'StandardErrorPath': str(logs / 'service-error.log'),
    }
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    with PLIST.open('wb') as f:
        plistlib.dump(config, f)
    PLIST.chmod(0o600)
    result = run('bootstrap', DOMAIN, str(PLIST))
    if result.returncode:
        sys.exit('macOS could not load the service: ' + result.stderr.strip())
    print('Installed background service at ' + str(PLIST))
    print('Open http://localhost:8765. The queue starts paused. The service runs while you are logged in and your Mac is awake.')


if __name__ == '__main__':
    main()
